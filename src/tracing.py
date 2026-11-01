from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


def safe(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if any(s in k.lower() for s in ("api_key", "authorization", "password", "auth_token")) and k != "authorization_scope" else safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(v) for v in value]
    if isinstance(value, str):
        for name in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            secret = os.getenv(name)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)
    return value


class TraceRecorder:
    def __init__(self, output_dir: Path, case_id: str, execution_mode: str, model_name: str):
        self.run_id = "run_" + uuid.uuid4().hex
        self.directory = Path(output_dir) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.case_id, self.mode, self.model = case_id, execution_mode, model_name
        self.events = []
        root = Path(__file__).resolve().parents[1]
        files = sorted([*root.joinpath("src").glob("*.py"), *root.joinpath("data").glob("*.json")])
        hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        self.event("source_snapshot", step_index=0, selected_action="record_provenance",
                   execution_status="RUNNING", source_fingerprint=digest, source_files_sha256=hashes)

    def event(self, event_type: str, **fields):
        event = safe({"run_id": self.run_id, "case_id": self.case_id, "execution_mode": self.mode,
                      "timestamp": datetime.now(timezone.utc).isoformat(), "model_name": self.model,
                      "event_type": event_type, **fields})
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def finish(self, result: dict) -> dict:
        result = safe(result)
        result["trace_files"] = {"events": str(self.directory / "events.jsonl"),
                                 "trace": str(self.directory / "trace.json"), "result": str(self.directory / "result.json")}
        self.event("run_finished", step_index=result.get("decision_steps", 0), selected_action="finish",
                   execution_status=result["execution_status"], error_summary=result.get("errors", []))
        compact = []
        actions = [e for e in self.events if e["event_type"] in {"tool_result", "model_final", "runtime_error", "budget_exhausted", "schedule_sync"}]
        for i, event in enumerate(actions):
            entry = {k: v for k, v in event.items() if k not in {"tool_response", "model_public_content"}}
            entry["next_action"] = actions[i + 1].get("selected_action", "finish") if i + 1 < len(actions) else "finish"
            compact.append(entry)
        (self.directory / "trace.json").write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
