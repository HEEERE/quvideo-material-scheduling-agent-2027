from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .agent import AgentRuntime
from .llm import LiveModel, ModelError
from .mock import MockModel
from .models import ScheduleRequest
from .rules import DATA_DIR, MaterialService, ReviewService
from .scheduler import ScheduleService
from .storage import DraftRepository
from .tools import ToolRegistry
from .tracing import TraceRecorder, safe

ROOT = Path(__file__).resolve().parents[1]


def run_case(request: ScheduleRequest, *, mode: str, db_path: Path, output_dir: Path,
             case_id: str = "cli", max_steps: int = 16, max_tool_calls: int = 24,
             timeout: float | None = None, simulate_save_failure: bool = False, model=None) -> dict:
    model = model or (LiveModel(timeout=timeout) if mode == "live" else MockModel())
    reviewer = ReviewService()
    repository = DraftRepository(db_path)
    registry = ToolRegistry(request, repository, reviewer, MaterialService(reviewer),
                            ScheduleService(reviewer.rules["conflict_window_minutes"]), simulate_save_failure)
    trace = TraceRecorder(output_dir, case_id, model.execution_mode, model.model_name)
    return AgentRuntime(model, registry, trace, max_steps, max_tool_calls).run()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Synthetic material review and draft scheduling. Never publishes.")
    parser.add_argument("--mode", choices=["live", "mock"])
    parser.add_argument("--input", type=Path)
    parser.add_argument("--sync-schedules", type=Path, help="Standalone explicit upsert of formal schedule times/statuses; no model call")
    parser.add_argument("--db", type=Path, default=ROOT / "runs" / "drafts.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--max-tool-calls", type=int, default=24)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--simulate-save-failure", action="store_true", help="Explicit fault injection: rollback the insert before commit")
    args = parser.parse_args(argv)
    if args.sync_schedules:
        if args.mode or args.input:
            parser.error("--sync-schedules is a standalone operation; omit --mode and --input")
    elif not args.mode or not args.input:
        parser.error("--mode and --input are required unless --sync-schedules is used")
    load_dotenv(ROOT / ".env", override=False)
    try:
        if args.sync_schedules:
            repository = DraftRepository(args.db, schedules_path=None)
            sync_result = repository.sync_schedules(args.sync_schedules)
            trace = TraceRecorder(args.output_dir, "schedule_sync", "maintenance", "no-model")
            trace.event("schedule_sync", step_index=0, selected_action="sync_schedules",
                        execution_status=sync_result["status"], tool_response=sync_result)
            result = trace.finish({"run_id": trace.run_id, "execution_mode": "maintenance", "execution_status": "COMPLETED",
                                   "decision_steps": 0, "tool_calls": 0, "schedule_sync": sync_result, "outcomes": [], "errors": []})
        else:
            request = ScheduleRequest.model_validate_json(args.input.read_text(encoding="utf-8-sig"))
            result = run_case(request, mode=args.mode, db_path=args.db, output_dir=args.output_dir,
                              case_id=args.input.stem, max_steps=args.max_steps, max_tool_calls=args.max_tool_calls,
                              timeout=args.timeout, simulate_save_failure=args.simulate_save_failure)
    except ModelError as exc:
        result = {"execution_mode": args.mode, "execution_status": "FAILED", "errors": [{"code": exc.code, "message": str(exc)}]}
    except ValidationError as exc:
        result = {"execution_status": "FAILED", "errors": [{"code": "INVALID_INPUT", "fields": [list(e["loc"]) for e in exc.errors()]}]}
    except (OSError, ValueError) as exc:
        result = {"execution_status": "FAILED", "errors": [{"code": "CONFIG_OR_IO_ERROR", "type": type(exc).__name__}]}
    except Exception as exc:
        result = {"execution_status": "FAILED", "errors": [{"code": "UNEXPECTED_ERROR", "type": type(exc).__name__}]}
    if "run_id" not in result:
        # Even preflight failure is an execution, and must leave inspectable evidence.
        try:
            trace = TraceRecorder(args.output_dir, args.input.stem if args.input else "schedule_sync", args.mode or "maintenance",
                                  __import__("os").getenv("LLM_MODEL", "unconfigured") if args.mode == "live" else MockModel.model_name if args.mode == "mock" else "no-model")
            trace.event("runtime_error", step_index=0, selected_action="preflight_error",
                        execution_status="FAILED", error_summary=result["errors"])
            result = trace.finish({**result, "run_id": trace.run_id, "execution_mode": args.mode or "maintenance",
                                   "decision_steps": 0, "tool_calls": 0, "outcomes": []})
        except OSError:
            result["trace_error"] = "Trace directory is not writable"
    print(json.dumps(safe(result), ensure_ascii=False, indent=2))
    if result["execution_status"] == "FAILED":
        return 2
    if result["execution_status"] == "INCOMPLETE":
        return 3
    if any(o["status"] == "SAVE_FAILED" for o in result.get("outcomes", [])):
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
