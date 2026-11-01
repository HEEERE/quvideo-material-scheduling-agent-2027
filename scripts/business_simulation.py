"""Synthetic operator actions + actual CLI/model calls; never publishes content."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.models import ScheduleRequest, UserConfirmation
from src.review_evaluation import invoke, trace_links
from src.storage import DraftRepository
from src.tracing import evidence_path

CASES = ["confirmation_lifecycle", "confirmation_slot_changed", "batch_pending", "japan_localization", "manual_claim_review"]


def run(mode: str, output_dir: Path, selected: list[str]) -> Path:
    started = datetime.now(timezone.utc)
    folder = output_dir.resolve() / ("business_" + mode + "_" + started.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    databases = ROOT / "runs" / folder.name
    databases.mkdir(parents=True)
    normal = json.loads((ROOT / "examples/normal.input.json").read_text(encoding="utf-8"))
    conflict = json.loads((ROOT / "examples/conflict.input.json").read_text(encoding="utf-8"))
    cases = []
    for name in selected:
        executions = []
        db = databases / (name + ".db")

        def execute(phase: str, request: dict) -> dict:
            ScheduleRequest.model_validate(request)
            path = folder / (name + "." + phase + ".input.json")
            path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
            args = ["--mode", mode, "--input", str(path), "--db", str(db), "--output-dir", str(folder),
                    "--max-steps", "32", "--max-tool-calls", "48"]
            execution = invoke(args)
            executions.append({"phase": phase, "input": evidence_path(path), **execution})
            return execution["result"]

        try:
            if name in {"confirmation_lifecycle", "confirmation_slot_changed"}:
                request = {**conflict, "request_id": "business-" + name}
                first = execute("propose", request)
                old = first["outcomes"][0]["draft"]
                if old is None or old["confirmation_status"] != "PENDING":
                    raise ValueError("Initial real/model execution did not produce a pending suggestion")
                confirmed = {**request, "user_confirmations": [{"material_id": "M01", "target_time": old["suggested_publish_time"],
                    "confirmed_at": datetime.now(timezone.utc).isoformat(), "source": "explicit_user_input"}]}
                if name == "confirmation_slot_changed":
                    source = folder / (name + ".new_occupancy.json")
                    source.write_text(json.dumps([{"schedule_id": "BUSINESS-CONFIRMATION-RACE", "target_channel": "TikTok",
                        "market": "US", "publish_time": old["suggested_publish_time"], "status": "CONFIRMED"}], indent=2), encoding="utf-8")
                    sync = invoke(["--sync-schedules", str(source), "--db", str(db), "--output-dir", str(folder)])
                    executions.append({"phase": "occupy_after_user_confirmation", "input": evidence_path(source), **sync})
                result = execute("confirm", confirmed)
                out = result["outcomes"][0]
                repository = DraftRepository(db)
                if name == "confirmation_lifecycle":
                    approved = out["draft"]
                    if approved is None:
                        raise ValueError("Explicit confirmation did not produce a persisted draft")
                    before_repeat = repository.all()
                    repeated = execute("repeat_confirmation", confirmed)
                    repeated_out = repeated["outcomes"][0]
                    repeated_draft = repeated_out["draft"] or {}
                    passed = (result["execution_status"] == repeated["execution_status"] == "COMPLETED"
                        and out["status"] == repeated_out["status"] == "DRAFT_SAVED"
                        and approved["draft_status"] == "READY" and approved["confirmation_status"] == "USER_CONFIRMED"
                        and [UserConfirmation.model_validate(item) for item in approved["confirmation_evidence"]]
                            == [UserConfirmation.model_validate(item) for item in confirmed["user_confirmations"]]
                        and datetime.fromisoformat(approved["suggested_publish_time"]) == datetime.fromisoformat(old["suggested_publish_time"])
                        and datetime.fromisoformat(approved["original_publish_time"]) == datetime.fromisoformat(request["requested_publish_time"])
                        and repeated_out["save_status"] == "ALREADY_EXISTS" and repeated_draft.get("draft_id") == approved["draft_id"]
                        and len(before_repeat) == len(repository.all()) == 2 and repeated_out["schedule_check"]["available"])
                    criterion = "Pending suggestion becomes READY only after synthetic explicit confirmation; identical confirmed request reuses its ID. The earlier pending request remains a separate historical row."
                else:
                    new = out.get("draft")
                    legal_pending = (out["status"] == "DRAFT_SAVED" and new is not None
                                     and new["confirmation_status"] == "PENDING" and new["draft_status"] == "AWAITING_CONFIRMATION"
                                     and datetime.fromisoformat(new["suggested_publish_time"]) != datetime.fromisoformat(old["suggested_publish_time"])
                                     and out["schedule_check"]["available"])
                    needs_input = out["status"] == "NEEDS_CONFIRMATION" and bool(out["pending_items"])
                    no_occupied_approval = not any(d["draft_status"] == "READY" and datetime.fromisoformat(d["suggested_publish_time"]) == datetime.fromisoformat(old["suggested_publish_time"]) for d in repository.all())
                    passed = result["execution_status"] == "COMPLETED" and no_occupied_approval and (legal_pending or needs_input)
                    criterion = "Occupancy changes after the synthetic user confirms; do not approve the occupied time or carry approval to a different time."
            elif name == "batch_pending":
                request = {**conflict, "request_id": "business-batch-pending", "material_ids": ["M01", "M07"],
                           "flexible_time_window": {"start": "2027-01-15T18:00:00-05:00", "end": "2027-01-15T23:00:00-05:00"}}
                result = execute("schedule_batch", request)
                outcomes = result["outcomes"]
                repository = DraftRepository(db)
                drafts = repository.all()
                passed = (result["execution_status"] == "COMPLETED" and len(drafts) == 2
                          and all(o["status"] == "DRAFT_SAVED" and o["schedule_check"]["available"] for o in outcomes)
                          and all(d["confirmation_status"] == "PENDING" and d["draft_status"] == "AWAITING_CONFIRMATION" for d in drafts)
                          and abs((datetime.fromisoformat(drafts[0]["suggested_publish_time"])-datetime.fromisoformat(drafts[1]["suggested_publish_time"])).total_seconds()) >= 3600)
                criterion = "M01 and supplementary-license M07 receive mutually compatible pending suggestions within one request."
            elif name == "japan_localization":
                request = {**normal, "request_id": "business-japan-localization", "material_ids": ["M04"], "market": "JP",
                           "target_channel": "Instagram Reels", "timezone": "Asia/Tokyo", "requested_publish_time": "2027-01-15T18:00:00+09:00"}
                result = execute("schedule_localized", request)
                out = result["outcomes"][0]
                repository = DraftRepository(db)
                draft = out.get("draft") or {}
                passed = (result["execution_status"] == "COMPLETED" and out["status"] == "DRAFT_SAVED"
                          and draft.get("timezone") == "Asia/Tokyo" and draft.get("market") == "JP"
                          and draft.get("draft_status") == "READY" and out["schedule_check"]["available"])
                criterion = "Japanese material passes localization and is saved at an explicit Asia/Tokyo time; cancelled formal schedule does not block."
            else:
                result = execute("review_claim", {**normal, "request_id": "business-unverified-claim", "material_ids": ["M05"]})
                out = result["outcomes"][0]
                repository = DraftRepository(db)
                passed = (result["execution_status"] == "COMPLETED" and out["status"] == "NEEDS_REVIEW"
                          and out["review"]["requires_manual_review"] and out["pending_items"] and not repository.all())
                criterion = "Unverified marketing claim requests manual review and creates no draft."
            record = {"case_id": name, "passed": bool(passed), "criterion": criterion, "executions": executions,
                      "database_snapshot": repository.all(), "traces": trace_links(*executions)}
        except (ValueError, KeyError, IndexError, TypeError, OSError, subprocess.SubprocessError) as exc:
            record = {"case_id": name, "passed": False, "error_type": type(exc).__name__, "error_summary": str(exc),
                      "executions": executions, "traces": trace_links(*executions)}
        cases.append(record)
        print(json.dumps({"case_id": name, "passed": record["passed"]}), flush=True)
    report = {"execution_mode": mode, "model_decisions": "real provider calls" if mode == "live" else "offline scripted simulator",
              "user_actions": "synthetic test actor; confirmation records are test fixtures, not real content approval",
              "material_data": "synthetic", "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
              "cases": cases, "passed": sum(c["passed"] for c in cases), "total": len(cases)}
    path = folder / "business.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Report: " + str(path), flush=True)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["live", "mock"])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples/evidence")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=CASES)
    args = parser.parse_args(argv)
    path = run(args.mode, args.output_dir, args.cases)
    data = json.loads(path.read_text(encoding="utf-8"))
    return 0 if data["passed"] == data["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
