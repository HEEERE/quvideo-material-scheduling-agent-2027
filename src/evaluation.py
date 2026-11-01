"""Run real or explicitly offline scenarios and preserve immutable evidence."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .main import ROOT, run_case
from .models import ScheduleRequest
from .storage import DraftRepository

CASES = ["normal", "missing", "risk", "conflict", "duplicate", "save_failure"]


def assess(case: str, result: dict, drafts: list[dict], previous: dict | None = None) -> tuple[bool, str]:
    if result["execution_status"] != "COMPLETED":
        return False, "Run did not complete"
    out = result["outcomes"][0]
    if case == "normal":
        return out["status"] == "DRAFT_SAVED" and len(drafts) == 1 and out["draft"]["draft_status"] == "READY", "PASS review + committed READY draft"
    if case == "missing":
        return out["status"] == "NEEDS_INFO" and not drafts, "Missing authorization retained; no draft"
    if case == "risk":
        return out["status"] == "BLOCKED" and not drafts, "Explicit license prohibition blocked"
    if case == "conflict":
        draft = out.get("draft") or {}
        return (out["status"] == "DRAFT_SAVED" and draft.get("confirmation_status") == "PENDING"
                and draft.get("draft_status") == "AWAITING_CONFIRMATION"
                and draft.get("original_publish_time") != draft.get("suggested_publish_time")), "Hard-time alternative preserved pending confirmation"
    if case == "duplicate":
        prior_id = previous["outcomes"][0]["draft"]["draft_id"] if previous and previous["outcomes"][0].get("draft") else None
        return out["save_status"] == "ALREADY_EXISTS" and len(drafts) == 1 and (out.get("draft") or {}).get("draft_id") == prior_id, "Second independent process returns same committed draft"
    return out["status"] == "SAVE_FAILED" and not drafts, "Injected failure rolled back; no persisted draft"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["live", "mock"])
    parser.add_argument("--cases", nargs="+", choices=CASES, default=CASES)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples" / "evidence")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    batch = args.output_dir / (args.mode + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    batch.mkdir(parents=True)
    start = datetime.now(timezone.utc)
    records = []
    for case in args.cases:
        input_path = ROOT / "examples" / f"{case}.input.json"
        request = ScheduleRequest.model_validate_json(input_path.read_text(encoding="utf-8"))
        db = ROOT / "runs" / batch.name / f"{case}.db"
        previous = None
        try:
            # Each invocation, including the duplicate repeat, is an independent Python process.
            repeats = 2 if case == "duplicate" else 1
            for repeat in range(repeats):
                command = [sys.executable, "-m", "src.main", "--mode", args.mode, "--input", str(input_path),
                           "--db", str(db), "--output-dir", str(batch)]
                if case == "save_failure":
                    command.append("--simulate-save-failure")
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
                                           env={**__import__("os").environ, "PYTHONUTF8": "1"}, timeout=900)
                result = json.loads(completed.stdout)
                if repeat == 0 and repeats == 2:
                    previous = result
            drafts = DraftRepository(db).all()
            passed, criterion = assess(case, result, drafts, previous)
            events = []
            if result.get("trace_files"):
                events = [json.loads(line) for line in Path(result["trace_files"]["events"]).read_text(encoding="utf-8").splitlines()]
            tools = [e for e in events if e["event_type"] == "tool_result"]
            if case == "conflict":
                queries = [e for e in tools if e["tool_name"] == "query_schedule"]
                passed = passed and any(not e["tool_response"].get("available", True) for e in queries) and any(e["tool_response"].get("available") for e in queries)
            if case == "missing":
                passed = passed and any(e["tool_name"] == "lookup_material_info" for e in tools)
            trace = result.get("trace_files", {})
            record = {"case_id": case, "passed": passed, "criterion": criterion, "input": str(input_path.relative_to(ROOT)),
                      "exit_code": completed.returncode, "result": result, "database_snapshot": drafts,
                      "tool_path": [e["tool_name"] for e in tools], "previous_result": previous,
                      "trace": {k: str(Path(v).relative_to(ROOT)) for k, v in trace.items()}}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            record = {"case_id": case, "passed": False, "error_type": type(exc).__name__}
        records.append(record)
        print(json.dumps({"case_id": case, "passed": record["passed"], "tool_path": record.get("tool_path", [])}), flush=True)
    report = {"execution_mode": args.mode, "started_at": start.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "cases": records, "passed": sum(r["passed"] for r in records), "total": len(records)}
    (batch / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Report: " + str(batch / "evaluation.json"))
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
