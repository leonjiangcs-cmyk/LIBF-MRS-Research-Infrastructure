from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import socket
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

RESEARCH_DATA_START = date(2014, 1, 1)
RESEARCH_DATA_END = date(2023, 12, 31)
SOURCE_FROZEN_UNIVERSE_SHA256 = "6fed339e51b07e97021ef6680f22093e07f56f13eb6f71be64d68c57a5838f0c"
RESEARCH_UNIVERSE_SHA256 = "ae6f0ca804b126ac9e982308fee34551d2d6e538f7e9133b93902c09aa2854e2"
EXPECTED_RESEARCH_STOCKS = 5251
BAOSTOCK_HOST = "public-api.baostock.com"
BAOSTOCK_PORT = 10030
BAOSTOCK_FIELDS = "date,code,close,tradestatus,pctChg,isST"
SOURCE_NAME = "BaoStock.query_history_k_data_plus"

PIT_SECURITY_VALIDITY_OVERRIDES = {
    "300114": (None, date(2025, 2, 16)),
    "302132": (date(2025, 2, 17), None),
}

OUTPUT_FIELDS = [
    "trade_date",
    "stock_id",
    "universe_baostock_code",
    "source_code",
    "source_close",
    "source_trading_status",
    "source_daily_return",
    "is_st",
    "source",
]

class ResearchDataError(RuntimeError):
    pass

@dataclass(frozen=True)
class UniverseRow:
    stock_id: str
    baostock_code: str
    ipo_date: date
    delist_date: date | None
    effective_start_date: date
    effective_end_date: date | None

def parse_date(value: object) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None

def canonical_stock_id(value: object) -> str:
    text = str(value).strip().lower()
    if text.startswith(("sh.", "sz.")):
        text = text.split(".", 1)[1]
    elif text.startswith(("sh", "sz")) and len(text) == 8:
        text = text[2:]
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if not text.isdigit():
        raise ResearchDataError(f"invalid stock code: {value!r}")
    text = text.zfill(6)
    sh_prefixes = ("600", "601", "603", "605", "688", "689")
    sz_prefixes = ("000", "001", "002", "003", "300", "301", "302")
    if text.startswith(sh_prefixes) or text.startswith(sz_prefixes):
        return text
    raise ResearchDataError(f"outside frozen Shanghai/Shenzhen A-share universe: {text}")

def baostock_code(stock_id: str) -> str:
    stock_id = canonical_stock_id(stock_id)
    sh_prefixes = ("600", "601", "603", "605", "688", "689")
    return f"sh.{stock_id}" if stock_id.startswith(sh_prefixes) else f"sz.{stock_id}"

def sha256_file(path: Path) -> str:
    d = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            d.update(chunk)
    return d.hexdigest()

def read_universe(path: Path) -> list[UniverseRow]:
    if sha256_file(path) != RESEARCH_UNIVERSE_SHA256:
        raise ResearchDataError("derived research Universe SHA256 mismatch")
    rows: list[UniverseRow] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        code_col = next((x for x in ("code", "stock_id", "symbol") if x in fields), None)
        ipo_col = next((x for x in ("ipoDate", "ipo_date", "list_date") if x in fields), None)
        out_col = next((x for x in ("outDate", "out_date", "delist_date") if x in fields), None)
        if not code_col or not ipo_col or not out_col:
            raise ResearchDataError("Universe schema mismatch")
        for line_no, raw in enumerate(reader, start=2):
            sid = canonical_stock_id(raw.get(code_col, ""))
            if sid in seen:
                raise ResearchDataError(f"duplicate code at line {line_no}: {sid}")
            seen.add(sid)
            ipo = parse_date(raw.get(ipo_col))
            if ipo is None:
                raise ResearchDataError(f"invalid IPO date at line {line_no}: {sid}")
            out = parse_date(raw.get(out_col))
            eff_start, eff_end = ipo, out
            override = PIT_SECURITY_VALIDITY_OVERRIDES.get(sid)
            if override:
                valid_from, valid_to = override
                if valid_from is not None:
                    eff_start = max(eff_start, valid_from)
                if valid_to is not None:
                    eff_end = valid_to if eff_end is None else min(eff_end, valid_to)
            rows.append(UniverseRow(sid, baostock_code(sid), ipo, out, eff_start, eff_end))
    return sorted(rows, key=lambda x: x.stock_id)

def research_universe(path: Path) -> list[UniverseRow]:
    rows = [
        r for r in read_universe(path)
        if r.effective_start_date <= RESEARCH_DATA_END
        and (r.effective_end_date is None or r.effective_end_date >= RESEARCH_DATA_START)
    ]
    if len(rows) != EXPECTED_RESEARCH_STOCKS:
        raise ResearchDataError(
            f"research Universe count mismatch: {len(rows)} != {EXPECTED_RESEARCH_STOCKS}"
        )
    ids = {r.stock_id for r in rows}
    if "300114" not in ids or "302132" in ids:
        raise ResearchDataError("PIT identifier rule failed for 300114/302132")
    return rows

def patch_baostock_endpoint() -> None:
    import baostock.common.contants as constants
    constants.BAOSTOCK_SERVER_IP = BAOSTOCK_HOST
    constants.BAOSTOCK_SERVER_PORT = BAOSTOCK_PORT

def login_baostock():
    socket.setdefaulttimeout(30)
    import baostock as bs
    patch_baostock_endpoint()
    lg = bs.login()
    if lg.error_code != "0":
        raise ResearchDataError(f"BaoStock login failed: {lg.error_code} {lg.error_msg}")
    return bs

def fetch_stock(bs, row: UniverseRow, retries: int = 3) -> list[dict[str, str]]:
    start = max(RESEARCH_DATA_START, row.effective_start_date)
    end = RESEARCH_DATA_END
    if row.effective_end_date is not None:
        end = min(end, row.effective_end_date)
    if start > end:
        return []
    last = ""
    for attempt in range(1, retries + 1):
        rs = bs.query_history_k_data_plus(
            row.baostock_code,
            BAOSTOCK_FIELDS,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag="3",
        )
        if rs.error_code == "0":
            fields = list(rs.fields)
            required = ["date", "code", "close", "tradestatus", "pctChg", "isST"]
            if any(x not in fields for x in required):
                raise ResearchDataError(f"missing BaoStock fields for {row.stock_id}: {fields}")
            pos = {x: fields.index(x) for x in required}
            out_rows: list[dict[str, str]] = []
            seen_dates: set[str] = set()
            while rs.next():
                vals = rs.get_row_data()
                d = parse_date(vals[pos["date"]])
                if d is None or d > RESEARCH_DATA_END:
                    raise ResearchDataError(f"invalid/post-2023 date for {row.stock_id}")
                dt = d.isoformat()
                if dt in seen_dates:
                    raise ResearchDataError(f"duplicate date for {row.stock_id}: {dt}")
                seen_dates.add(dt)
                src = str(vals[pos["code"]]).strip()
                if canonical_stock_id(src) != row.stock_id:
                    raise ResearchDataError(
                        f"source code mismatch for {row.stock_id}: {src}"
                    )
                is_st = str(vals[pos["isST"]]).strip()
                if is_st not in {"", "0", "1"}:
                    raise ResearchDataError(
                        f"invalid isST for {row.stock_id} {dt}: {is_st!r}"
                    )
                out_rows.append({
                    "trade_date": dt,
                    "stock_id": row.stock_id,
                    "universe_baostock_code": row.baostock_code,
                    "source_code": src,
                    "source_close": str(vals[pos["close"]]).strip(),
                    "source_trading_status": str(vals[pos["tradestatus"]]).strip(),
                    "source_daily_return": str(vals[pos["pctChg"]]).strip(),
                    "is_st": is_st,
                    "source": SOURCE_NAME,
                })
            if not out_rows:
                raise ResearchDataError(f"no research-window history returned for {row.stock_id}")
            return out_rows
        last = f"{rs.error_code} {rs.error_msg}"
        time.sleep(attempt)
    raise ResearchDataError(f"BaoStock query failed for {row.stock_id}: {last}")

def run_batch(universe_path: Path, output_path: Path, batch_index: int, batch_size: int) -> dict:
    universe = research_universe(universe_path)
    start_i = batch_index * batch_size
    end_i = min(len(universe), start_i + batch_size)
    if start_i >= len(universe):
        raise ResearchDataError("batch index outside research Universe")
    selected = universe[start_i:end_i]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bs = login_baostock()
    rows_written = 0
    stocks_written = 0
    try:
        with gzip.open(output_path, "wt", encoding="utf-8", newline="") as gz:
            writer = csv.DictWriter(gz, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            for n, row in enumerate(selected, start=1):
                data = fetch_stock(bs, row)
                writer.writerows(data)
                rows_written += len(data)
                stocks_written += 1
                if n % 25 == 0:
                    print(
                        json.dumps(
                            {
                                "progress_batch": batch_index,
                                "stocks_done": n,
                                "stocks_in_batch": len(selected),
                                "rows_written": rows_written,
                            }
                        ),
                        flush=True,
                    )
                time.sleep(0.05)
    finally:
        bs.logout()
    result = {
        "batch_index": batch_index,
        "batch_size": batch_size,
        "stock_start_index": start_i,
        "stock_end_index_exclusive": end_i,
        "stocks_written": stocks_written,
        "rows_written": rows_written,
        "output_sha256": sha256_file(output_path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result

def audit_and_combine(checkpoint_dir: Path, output_path: Path, manifest_path: Path) -> dict:
    files = sorted(checkpoint_dir.glob("*.csv.gz"))
    if not files:
        raise ResearchDataError("no decrypted checkpoints")
    seen_stock_date: set[tuple[str, str]] = set()
    stocks: set[str] = set()
    rows = 0
    invalid_is_st = 0
    invalid_date = 0
    duplicate = 0
    post_2023 = 0
    missing_st_trading = 0
    min_date: date | None = None
    max_date: date | None = None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for cp in files:
            with gzip.open(cp, "rt", encoding="utf-8", newline="") as inp:
                reader = csv.DictReader(inp)
                if reader.fieldnames != OUTPUT_FIELDS:
                    raise ResearchDataError(f"checkpoint schema mismatch: {cp.name}")
                for row in reader:
                    rows += 1
                    sid = row["stock_id"].strip()
                    dt = row["trade_date"].strip()
                    stocks.add(sid)
                    key = (sid, dt)
                    if key in seen_stock_date:
                        duplicate += 1
                    seen_stock_date.add(key)
                    d = parse_date(dt)
                    if d is None:
                        invalid_date += 1
                    else:
                        if d > RESEARCH_DATA_END:
                            post_2023 += 1
                        min_date = d if min_date is None else min(min_date, d)
                        max_date = d if max_date is None else max(max_date, d)
                    is_st = row["is_st"].strip()
                    if is_st not in {"", "0", "1"}:
                        invalid_is_st += 1
                    if row["source_trading_status"].strip() == "1" and is_st == "":
                        missing_st_trading += 1
                    writer.writerow({k: row.get(k, "") for k in OUTPUT_FIELDS})
    empty_count = max(0, EXPECTED_RESEARCH_STOCKS - len(stocks))
    gate_metrics = {
        "query_error_count": 0,
        "empty_or_no_history_stock_count": empty_count,
        "invalid_is_st_count": invalid_is_st,
        "invalid_date_count": invalid_date,
        "duplicate_date_stock_count": duplicate,
        "post_2023_row_count": post_2023,
        "is_st_missing_trading_rows": missing_st_trading,
    }
    data_gate = "PASS" if all(v == 0 for v in gate_metrics.values()) else "FAIL"
    manifest = {
        "purpose": "MRS historical PIT ST remediation only",
        "research_window": ["2014-01-01", "2023-12-31"],
        "final_holdout_2024_plus_used": False,
        "source_frozen_universe_sha256": SOURCE_FROZEN_UNIVERSE_SHA256,
        "research_universe_sha256": RESEARCH_UNIVERSE_SHA256,
        "expected_research_stocks": EXPECTED_RESEARCH_STOCKS,
        "stocks_with_rows": len(stocks),
        "rows": rows,
        "min_date": None if min_date is None else min_date.isoformat(),
        "max_date": None if max_date is None else max_date.isoformat(),
        "gate_metrics": gate_metrics,
        "data_gate": data_gate,
        "combined_output_sha256": sha256_file(output_path),
        "checkpoint_count": len(files),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"combine_complete": True, "manifest_written": str(manifest_path)}))
    return manifest

def main() -> int:
    p = argparse.ArgumentParser(description="LIBF MRS historical ST remediation infrastructure")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate-universe")
    v.add_argument("--universe", required=True)

    b = sub.add_parser("batch")
    b.add_argument("--universe", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--batch-index", type=int, required=True)
    b.add_argument("--batch-size", type=int, default=250)

    c = sub.add_parser("combine")
    c.add_argument("--checkpoint-dir", required=True)
    c.add_argument("--output", required=True)
    c.add_argument("--manifest", required=True)

    args = p.parse_args()
    try:
        if args.cmd == "validate-universe":
            rows = research_universe(Path(args.universe))
            print(json.dumps({
                "status": "PASS",
                "source_frozen_universe_sha256": SOURCE_FROZEN_UNIVERSE_SHA256,
                "research_universe_sha256": sha256_file(Path(args.universe)),
                "research_stock_count": len(rows),
                "contains_300114": any(r.stock_id == "300114" for r in rows),
                "contains_302132": any(r.stock_id == "302132" for r in rows),
            }, indent=2))
            return 0
        if args.cmd == "batch":
            run_batch(Path(args.universe), Path(args.output), args.batch_index, args.batch_size)
            return 0
        if args.cmd == "combine":
            audit_and_combine(Path(args.checkpoint_dir), Path(args.output), Path(args.manifest))
            return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": repr(exc)}, ensure_ascii=False, indent=2))
        return 2
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
