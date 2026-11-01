"""Read-only audit of staged/tracked deliverables; never prints credential values."""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / ".env", override=False)
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    errors = []
    for name in filter(None, files):
        path = ROOT / name
        if name == ".env" or name.startswith("runs/") or path.suffix in {".db", ".sqlite"}:
            errors.append("Forbidden tracked path: " + name)
        raw = path.read_bytes()
        secrets = [os.getenv(k, "") for k in ("LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")]
        if any(s and s.encode() in raw for s in secrets) or re.search(rb"\bsk-[A-Za-z0-9_-]{20,}\b", raw):
            errors.append("Potential credential in: " + name)
    reports = []
    for path in (ROOT / "examples/evidence").glob("*/evaluation.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["total"] == 6:
            reports.append(report)
    for mode in ("live", "mock"):
        selected = sorted([r for r in reports if r["execution_mode"] == mode], key=lambda r: r["started_at"])
        if not selected or selected[-1]["passed"] != 6:
            errors.append("Missing successful six-case evidence for: " + mode)
        elif selected:
            report = selected[-1]
            for case in report["cases"]:
                path = ROOT / case["trace"]["events"].replace("\\", "/")
                first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
                manifest = first.get("source_files_sha256")
                if not manifest:
                    errors.append("Missing source provenance: " + str(path.relative_to(ROOT)))
                elif any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest for name, digest in manifest.items()):
                    errors.append("Source differs from final evidence: " + case["case_id"])
    if not files or not files[0]:
        errors.append("No staged/tracked files to audit")
    print(json.dumps({"checked_files": len(list(filter(None, files))), "errors": errors}, indent=2))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
