"""Explicitly scripted OFFLINE simulator. Never evidence of autonomous LLM behavior."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from .llm import ModelReply
from .models import ScheduleRequest


class MockModel:
    execution_mode = "mock"
    model_name = "offline-scripted-simulator"

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        request = ScheduleRequest.model_validate_json(messages[1]["content"])
        history = []
        arguments = {}
        for message in messages:
            for call in message.get("tool_calls", []):
                arguments[call["id"]] = (call["function"]["name"], json.loads(call["function"]["arguments"]))
            if message["role"] == "tool":
                name, args = arguments[message["tool_call_id"]]
                history.append((name, args, json.loads(message["content"])))

        def action(name, mid, **args):
            return ModelReply({"role": "assistant", "content": "", "tool_calls": [
                {"id": "mock_" + uuid.uuid4().hex, "type": "function", "function": {"name": name,
                 "arguments": json.dumps({"material_id": mid, "reason_summary": "Offline scripted validation: " + name, **args})}}]}, self.model_name)

        for mid in request.material_ids:
            records = [x for x in history if x[1]["material_id"] == mid]
            names = [x[0] for x in records]
            if not records:
                return action("read_material", mid)
            if records[0][2].get("status") == "NOT_FOUND":
                continue
            if "check_review_rules" not in names or names[-1] == "lookup_material_info":
                return action("check_review_rules", mid)
            review = [x[2] for x in records if x[0] == "check_review_rules"][-1]
            if review["review_status"] == "NEEDS_INFO" and "lookup_material_info" not in names:
                return action("lookup_material_info", mid)
            if review["review_status"] != "PASS" or "save_schedule_draft" in names:
                continue
            queries = [x for x in records if x[0] == "query_schedule"]
            if not queries:
                return action("query_schedule", mid, publish_time=request.requested_publish_time.isoformat())
            query = queries[-1]
            if query[2].get("available"):
                target = datetime.fromisoformat(query[1]["publish_time"])
                if any(c.material_id == mid and c.target_time == target for c in request.user_confirmations):
                    confirmation = "USER_CONFIRMED"
                elif target == request.requested_publish_time:
                    confirmation = "ORIGINAL_REQUEST"
                else:
                    confirmation = "PENDING" if request.hard_time_constraint else "WITHIN_FLEXIBILITY"
                return action("save_schedule_draft", mid, suggested_publish_time=target.isoformat(), confirmation_status=confirmation)
            if request.flexible_time_window:
                candidate = request.flexible_time_window.start
                tried = {datetime.fromisoformat(q[1]["publish_time"]) for q in queries}
                while candidate <= request.flexible_time_window.end:
                    if candidate not in tried:
                        return action("query_schedule", mid, publish_time=candidate.isoformat())
                    candidate += timedelta(minutes=query[2].get("minimum_spacing_minutes", 60))
        return ModelReply({"role": "assistant", "content": "Offline scripted simulation finished; see authoritative tool outcomes."}, self.model_name)
