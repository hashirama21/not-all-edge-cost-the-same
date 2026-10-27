"""Audit de provenance et détection de fuites."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import data_dir
from .time import utc_now_iso


SENSITIVE_PATTERNS = [
    # crude patterns - will be refined
]


def audit_provenance() -> dict:
    report = {"generated_at_utc": utc_now_iso("seconds"), "checks": {}}
    raw = data_dir("raw")
    # Check manifest exists and has timestamps
    manifests = list(raw.rglob("manifest.jsonl"))
    report["checks"]["manifests_count"] = len(manifests)
    total = 0
    missing_ts = 0
    for m in manifests:
        with m.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                    if not rec.get("fetched_at") and not rec.get("fetched_at_utc"):
                        missing_ts += 1
                except Exception:
                    pass
    report["checks"]["raw_entries"] = total
    report["checks"]["raw_missing_timestamps"] = missing_ts

    # Check processed has required provenance
    proc = data_dir("processed")
    for name in ("avis", "soumissions", "attributions", "sanctions", "entreprises", "aretes"):
        p = proc / f"{name}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            report["checks"][f"{name}_rows"] = len(df)
            report["checks"][f"{name}_has_written_at"] = "written_at_utc" in df.columns
        else:
            report["checks"][f"{name}_rows"] = 0
            report["checks"][f"{name}_has_written_at"] = False

    # Basic leak detection - check for obvious unredacted patterns in public tables
    leaks = []
    for name in ("entreprises", "soumissions", "attributions"):
        p = proc / f"{name}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            # Check emails/phones not hashed
            for col in ("email", "telephone", "emails", "telephones"):
                if col in df.columns:
                    for v in df[col].dropna().astype(str).head(100):
                        if v and not v.startswith("h_") and ("@" in v or v.replace("+", "").replace(" ", "").isdigit()):
                            leaks.append(f"{name}.{col}: possible_unredacted")
                            break
            for col in ("representant", "representants"):
                if col in df.columns:
                    for v in df[col].dropna().astype(str).head(100):
                        if v and not v.startswith("h_") and len(v) > 5 and "[" not in v:
                            leaks.append(f"{name}.{col}: possible_unredacted")
                            break
    report["leaks"] = leaks
    report["ok"] = missing_ts == 0 and len(leaks) == 0

    out = data_dir("reports") / "provenance_audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
