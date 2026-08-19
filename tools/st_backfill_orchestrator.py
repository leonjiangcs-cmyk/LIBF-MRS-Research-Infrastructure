from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_STOCKS = 5251
BATCH_SIZE = 250
EXPECTED_BATCHES = 22
EXPECTED_PARTS = 5
EXPECTED_B64_BYTES = 32556
EXPECTED_CIPHERTEXT_SHA256 = "3ec8248fde2a4d4d4687ac88106c2c5e3a2f32870b3baecb1605f12b8ce95741"
EXPECTED_PLAINTEXT_SHA256 = "ae6f0ca804b126ac9e982308fee34551d2d6e538f7e9133b93902c09aa2854e2"

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "secure" / "input"
CHECKPOINT_DIR = ROOT / "secure" / "checkpoints"
FINAL_DIR = ROOT / "secure" / "final"
DIAG_DIR = ROOT / "diagnostics"
STATUS_PATH = DIAG_DIR / "st_backfill_status.json"
TMP = Path("/tmp")
UNIVERSE_CSV = TMP / "universe.csv"
UNIVERSE_GZ = TMP / "universe.csv.gz"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=check)


def git_commit_push(message: str, paths: list[Path]) -> None:
    rels = [str(p.relative_to(ROOT)) for p in paths]
    run(["git", "add", "--", *rels])
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False
    ).returncode
    if staged == 0:
        return
    run(["git", "commit", "-m", message])
    run(["git", "push", "origin", "HEAD:main"])


def checkpoint_files() -> list[Path]:
    return sorted(CHECKPOINT_DIR.glob("st_2014_2023_batch_*.csv.gz.enc"))


def write_status(
    *,
    stage: str,
    status: str,
    last_checkpoint: str | None = None,
    data_gate: str | None = None,
) -> None:
    cps = checkpoint_files()
    payload: dict[str, object] = {
        "workflow": "Historical ST Backfill V3 (Encrypted)",
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "stage": stage,
        "status": status,
        "checkpoint_count": len(cps),
        "expected_batches": EXPECTED_BATCHES,
        "expected_stocks": EXPECTED_STOCKS,
        "final_holdout_2024_plus_used": False,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if last_checkpoint is not None:
        payload["last_checkpoint"] = last_checkpoint
    if cps:
        if len(cps) < EXPECTED_BATCHES:
            payload["stocks_committed_upper_bound"] = min(
                EXPECTED_STOCKS, len(cps) * BATCH_SIZE
            )
        else:
            payload["stocks_committed_upper_bound"] = EXPECTED_STOCKS
    if data_gate is not None:
        payload["data_gate"] = data_gate
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def reconstruct_universe() -> None:
    parts = sorted(INPUT_DIR.glob("research_universe_2014_2023.enc.part???"))
    if len(parts) != EXPECTED_PARTS:
        raise RuntimeError(f"encrypted Universe part count mismatch: {len(parts)}")
    b64 = b"".join(p.read_bytes() for p in parts)
    if len(b64) != EXPECTED_B64_BYTES:
        raise RuntimeError(f"encrypted Universe byte count mismatch: {len(b64)}")
    if sha256_bytes(b64) != EXPECTED_CIPHERTEXT_SHA256:
        raise RuntimeError("encrypted Universe ciphertext SHA256 mismatch")
    raw = base64.b64decode(b64, validate=True)
    if raw[:8] != b"Salted__":
        raise RuntimeError("OpenSSL Salted__ header missing")
    enc_bin = TMP / "universe.enc.bin"
    enc_bin.write_bytes(raw)
    run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(enc_bin),
            "-out",
            str(UNIVERSE_GZ),
            "-pass",
            "env:MRS_DATA_PASSPHRASE",
        ]
    )
    with gzip.open(UNIVERSE_GZ, "rb") as src, UNIVERSE_CSV.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    if sha256_file(UNIVERSE_CSV) != EXPECTED_PLAINTEXT_SHA256:
        raise RuntimeError("research Universe plaintext SHA256 mismatch")
    run(
        [
            sys.executable,
            "tools/st_backfill.py",
            "validate-universe",
            "--universe",
            str(UNIVERSE_CSV),
        ]
    )
    enc_bin.unlink(missing_ok=True)


def baostock_preflight() -> None:
    run([sys.executable, "tools/baostock_probe.py"])


def encrypt_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-salt",
            "-in",
            str(src),
            "-out",
            str(dst),
            "-pass",
            "env:MRS_DATA_PASSPHRASE",
        ]
    )


def decrypt_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(src),
            "-out",
            str(dst),
            "-pass",
            "env:MRS_DATA_PASSPHRASE",
        ]
    )


def run_batches() -> None:
    if (EXPECTED_STOCKS + BATCH_SIZE - 1) // BATCH_SIZE != EXPECTED_BATCHES:
        raise RuntimeError("batch geometry mismatch")
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(EXPECTED_BATCHES):
        idx = f"{i:03d}"
        enc = CHECKPOINT_DIR / f"st_2014_2023_batch_{idx}.csv.gz.enc"
        if enc.exists():
            print(f"Batch {idx}: encrypted checkpoint exists; skip.", flush=True)
            continue
        plain = TMP / f"st_batch_{idx}.csv.gz"
        print(f"Batch {idx}: starting.", flush=True)
        run(
            [
                sys.executable,
                "tools/st_backfill.py",
                "batch",
                "--universe",
                str(UNIVERSE_CSV),
                "--output",
                str(plain),
                "--batch-index",
                str(i),
                "--batch-size",
                str(BATCH_SIZE),
            ]
        )
        with gzip.open(plain, "rb") as f:
            while f.read(1024 * 1024):
                pass
        encrypt_file(plain, enc)
        plain.unlink(missing_ok=True)
        write_status(stage="checkpoint_committed", status="RUNNING", last_checkpoint=idx)
        git_commit_push(f"Add encrypted ST checkpoint {idx}", [enc, STATUS_PATH])


def final_audit() -> str:
    cps = checkpoint_files()
    if len(cps) != EXPECTED_BATCHES:
        raise RuntimeError(
            f"checkpoint count mismatch before Data Gate: {len(cps)} != {EXPECTED_BATCHES}"
        )
    cp_dir = TMP / "st_checkpoints"
    shutil.rmtree(cp_dir, ignore_errors=True)
    cp_dir.mkdir(parents=True, exist_ok=True)
    for enc in cps:
        plain = cp_dir / enc.name.removesuffix(".enc")
        decrypt_file(enc, plain)
        with gzip.open(plain, "rb") as f:
            while f.read(1024 * 1024):
                pass
    combined = TMP / "historical_st_2014_2023.csv.gz"
    manifest = TMP / "st_manifest.json"
    run(
        [
            sys.executable,
            "tools/st_backfill.py",
            "combine",
            "--checkpoint-dir",
            str(cp_dir),
            "--output",
            str(combined),
            "--manifest",
            str(manifest),
        ]
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    gate = str(data.get("data_gate", "UNKNOWN"))
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    enc_manifest = FINAL_DIR / "st_manifest_2014_2023.json.enc"
    encrypt_file(manifest, enc_manifest)
    git_commit_push("Add encrypted ST Data Gate manifest", [enc_manifest])
    return gate


def cleanup() -> None:
    for p in [
        TMP / "universe.enc.bin",
        UNIVERSE_GZ,
        UNIVERSE_CSV,
        TMP / "historical_st_2014_2023.csv.gz",
        TMP / "st_manifest.json",
    ]:
        p.unlink(missing_ok=True)
    shutil.rmtree(TMP / "st_checkpoints", ignore_errors=True)
    for p in TMP.glob("st_batch_*.csv.gz"):
        p.unlink(missing_ok=True)


def main() -> int:
    stage = "initialize"
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("MRS_DATA_PASSPHRASE"):
        raise SystemExit("MRS_DATA_PASSPHRASE missing")
    try:
        stage = "decrypt_universe"
        reconstruct_universe()
        stage = "baostock_preflight"
        baostock_preflight()
        stage = "preflight_passed"
        write_status(stage=stage, status="RUNNING")
        git_commit_push("ST backfill V3 preflight passed", [STATUS_PATH])

        stage = "backfill"
        run_batches()

        stage = "data_gate"
        gate = final_audit()
        if gate != "PASS":
            write_status(stage="data_gate", status="FAIL", data_gate=gate)
            git_commit_push("Record historical ST Data Gate failure", [STATUS_PATH])
            return 2

        stage = "complete"
        write_status(stage=stage, status="PASS", data_gate="PASS")
        git_commit_push("Mark historical ST backfill V3 PASS", [STATUS_PATH])
        return 0
    except Exception as exc:
        print(f"ST BACKFILL FAILURE at {stage}: {exc!r}", file=sys.stderr, flush=True)
        try:
            write_status(stage=stage, status="FAIL")
            git_commit_push(f"Record ST backfill V3 failure stage: {stage}", [STATUS_PATH])
        except Exception as status_exc:
            print(f"Could not publish failure status: {status_exc!r}", file=sys.stderr)
        return 2
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
