"""Reproduce reviewed business boundaries using independent CLI processes."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .main import ROOT
from .storage import DraftRepository
from .tracing import evidence_path


def invoke(arguments: list[str]) -> dict:
    completed = subprocess.run([sys.executable, "-m", "src.main", *arguments], capture_output=True,
                               text=True, encoding="utf-8", cwd=ROOT,
                               env={**os.environ, "PYTHONUTF8": "1"}, timeout=900)
    result = json.loads(completed.stdout)
    return {"exit_code": completed.returncode, "result": result}


def trace_links(*executions) -> list[dict]:
    return [{k: evidence_path(v) for k, v in item["result"].get("trace_files", {}).items()}
            for item in executions]


def evaluate(mode: str, output_dir: Path) -> Path:
    start = datetime.now(timezone.utc)
    batch = output_dir.resolve() / ("review_" + mode + "_" + start.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    batch.mkdir(parents=True)
    database_dir = ROOT / "runs" / batch.name
    database_dir.mkdir(parents=True)
    request = json.loads((ROOT / "examples/conflict.input.json").read_text(encoding="utf-8"))
    request["request_id"] = "review-changed-slot-repeat-001"
    input_path = batch / "changed_slot.input.json"
    input_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
    db = database_dir / "changed_slot.db"
    args = ["--mode", mode, "--input", str(input_path), "--db", str(db), "--output-dir", str(batch)]
    cases = []
    first = sync = repeated = None
    try:
        first = invoke(args)
        old = first["result"]["outcomes"][0]["draft"]
        if old is None:
            raise ValueError("Initial run did not save a draft")
        formal_input = batch / "new_occupancy.json"
        formal_input.write_text(json.dumps([{"schedule_id": "REVIEW-NEW-OCCUPANCY", "target_channel": request["target_channel"],
            "market": request["market"], "publish_time": old["suggested_publish_time"], "status": "CONFIRMED"}], indent=2), encoding="utf-8")
        sync = invoke(["--sync-schedules", str(formal_input), "--db", str(db), "--output-dir", str(batch)])
        repeated = invoke(args)
        new = repeated["result"]["outcomes"][0]["draft"]
        repository = DraftRepository(db)
        history = repository.history(old["draft_id"])
        outcome = repeated["result"]["outcomes"][0]
        passed = (first["exit_code"] == 0 and sync["exit_code"] == 0 and repeated["exit_code"] == 0
                  and repeated["result"]["execution_status"] == "COMPLETED"
                  and outcome["status"] == "DRAFT_SAVED" and outcome["save_status"] == "SAVED"
                  and new is not None and new["draft_id"] == old["draft_id"]
                  and datetime.fromisoformat(new["suggested_publish_time"]) != datetime.fromisoformat(old["suggested_publish_time"])
                  and new["confirmation_status"] == "PENDING" and new["draft_status"] == "AWAITING_CONFIRMATION"
                  and new["revision"] == 2 and outcome["schedule_check"]["available"]
                  and len(repository.all()) == 1 and len(history) == 1)
        cases.append({"case_id": "changed_slot_repeat", "passed": bool(passed),
                      "input": evidence_path(input_path), "first": first, "sync": sync,
                      "repeat": repeated, "database_snapshot": repository.all(), "draft_history": history,
                      "traces": trace_links(first, sync, repeated)})
    except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError) as exc:
        partial = {key: value for key, value in (("first", first), ("sync", sync), ("repeat", repeated)) if value is not None}
        cases.append({"case_id": "changed_slot_repeat", "passed": False, "error_type": type(exc).__name__,
                      "error_summary": str(exc), **partial, "traces": trace_links(*partial.values())})
    request.update(request_id="review-no-window-001", flexible_time_window=None)
    no_window = batch / "no_window.input.json"
    no_window.write_text(json.dumps(request, indent=2), encoding="utf-8")
    try:
        result = invoke(["--mode", mode, "--input", str(no_window), "--db", str(database_dir / "no_window.db"), "--output-dir", str(batch)])
        outcome = result["result"]["outcomes"][0]
        passed = (result["exit_code"] == 0 and result["result"]["execution_status"] == "COMPLETED"
                  and outcome["status"] == "NEEDS_CONFIRMATION" and outcome["review"]["review_status"] == "PASS"
                  and outcome["draft"] is None and outcome["pending_items"]
                  and outcome["schedule_blockers"][0]["code"] == "REQUESTED_SLOT_UNAVAILABLE")
        cases.append({"case_id": "no_window_confirmation", "passed": bool(passed),
                      "input": evidence_path(no_window), "execution": result, "traces": trace_links(result)})
    except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError) as exc:
        cases.append({"case_id": "no_window_confirmation", "passed": False, "error_type": type(exc).__name__})
    report = {"execution_mode": mode, "started_at": start.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
              "cases": cases, "passed": sum(c["passed"] for c in cases), "total": len(cases)}
    report_path = batch / "regressions.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for case in cases:
        print(json.dumps({"case_id": case["case_id"], "passed": case["passed"]}), flush=True)
    print("Report: " + str(report_path))
    return report_path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["live", "mock"], required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples/evidence")
    args = parser.parse_args(argv)
    path = evaluate(args.mode, args.output_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    return 0 if data["passed"] == data["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
