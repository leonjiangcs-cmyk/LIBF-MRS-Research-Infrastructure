from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DIR = ROOT / "secure" / "checkpoints"
DIAG_DIR = ROOT / "diagnostics"
STATUS_PATH = DIAG_DIR / "st_snapshot_export_status.json"
TMP_ROOT = Path("/tmp/libf_mrs_st_snapshot_v0_1")
PLAIN_CP_DIR = TMP_ROOT / "checkpoints"
EXPORT_DIR = Path("/tmp/libf_mrs_st_snapshot_export_v0_1")
EXPECTED_BATCHES = 22
EXPECTED_STOCKS = 5251
RESEARCH_END = "2023-12-31"
ARTIFACT_NAME = "LIBF_MRS_Internal_Fragility_Research_Snapshot_V0_1_ENCRYPTED"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def decrypt(src: Path, dst: Path) -> None:
    run([
        "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
        "-in", str(src), "-out", str(dst), "-pass", "env:MRS_DATA_PASSPHRASE",
    ])


def encrypt(src: Path, dst: Path) -> None:
    run([
        "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
        "-in", str(src), "-out", str(dst), "-pass", "env:MRS_DATA_PASSPHRASE",
    ])


def write_status(payload: dict[str, object]) -> None:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    if not os.environ.get("MRS_DATA_PASSPHRASE"):
        raise SystemExit("MRS_DATA_PASSPHRASE missing")

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    shutil.rmtree(EXPORT_DIR, ignore_errors=True)
    PLAIN_CP_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    cps = sorted(CHECKPOINT_DIR.glob("st_2014_2023_batch_*.csv.gz.enc"))
    if len(cps) != EXPECTED_BATCHES:
        raise SystemExit(f"checkpoint count mismatch: {len(cps)} != {EXPECTED_BATCHES}")

    try:
        for enc in cps:
            plain = PLAIN_CP_DIR / enc.name.removesuffix(".enc")
            decrypt(enc, plain)
            with gzip.open(plain, "rb") as f:
                while f.read(1024 * 1024):
                    pass

        combined = TMP_ROOT / "historical_st_status_2014_2023.csv.gz"
        manifest = TMP_ROOT / "historical_st_manifest_2014_2023.json"
        run([
            sys.executable, "tools/st_backfill.py", "combine",
            "--checkpoint-dir", str(PLAIN_CP_DIR),
            "--output", str(combined),
            "--manifest", str(manifest),
        ])

        m = json.loads(manifest.read_text(encoding="utf-8"))
        required = {
            "data_gate": "PASS",
            "expected_research_stocks": EXPECTED_STOCKS,
            "stocks_with_rows": EXPECTED_STOCKS,
            "checkpoint_count": EXPECTED_BATCHES,
            "final_holdout_2024_plus_used": False,
            "max_date": RESEARCH_END,
        }
        for key, expected in required.items():
            if m.get(key) != expected:
                raise SystemExit(f"snapshot gate mismatch for {key}: {m.get(key)!r} != {expected!r}")

        if sha256_file(combined) != m.get("combined_output_sha256"):
            raise SystemExit("combined output SHA256 mismatch")

        combined_enc = EXPORT_DIR / "historical_st_status_2014_2023.csv.gz.enc"
        manifest_enc = EXPORT_DIR / "historical_st_manifest_2014_2023.json.enc"
        encrypt(combined, combined_enc)
        encrypt(manifest, manifest_enc)

        transport = {
            "snapshot": "LIBF_MRS_Internal_Fragility_Research_Snapshot_V0_1",
            "purpose": "encrypted transport copy of historical PIT ST remediation for private research storage",
            "research_window": ["2014-01-01", "2023-12-31"],
            "checkpoint_count": EXPECTED_BATCHES,
            "expected_research_stocks": EXPECTED_STOCKS,
            "data_gate": "PASS",
            "final_holdout_2024_plus_used": False,
            "combined_plaintext_sha256": m["combined_output_sha256"],
            "combined_encrypted_sha256": sha256_file(combined_enc),
            "manifest_encrypted_sha256": sha256_file(manifest_enc),
            "combined_encrypted_bytes": combined_enc.stat().st_size,
            "manifest_encrypted_bytes": manifest_enc.stat().st_size,
            "source_commit": os.environ.get("GITHUB_SHA"),
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
            "artifact_name": ARTIFACT_NAME,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        (EXPORT_DIR / "transport_manifest.json").write_text(
            json.dumps(transport, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        write_status({
            "workflow": "Export ST Research Snapshot V0.1 (Encrypted)",
            "status": "EXPORT_READY",
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "artifact_name": ARTIFACT_NAME,
            "checkpoint_count": EXPECTED_BATCHES,
            "expected_stocks": EXPECTED_STOCKS,
            "data_gate": "PASS",
            "final_holdout_2024_plus_used": False,
            "combined_plaintext_sha256": m["combined_output_sha256"],
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        print(json.dumps(transport, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        shutil.rmtree(TMP_ROOT, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
