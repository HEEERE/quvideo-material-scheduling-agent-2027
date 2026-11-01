import json
from pathlib import Path

import pytest

from src.agent import AgentRuntime
from src.llm import ModelError, ModelReply
from src.main import ROOT, main, run_case
from src.models import ScheduleRequest
from src.tracing import TraceRecorder, safe


class ScriptedModel:
    execution_mode = "mock"
    model_name = "controlled-test-double"

    def __init__(self, messages):
        self.messages = iter(messages)
        self.observed = []

    def complete(self, messages, tools):
        self.observed.append(list(messages))
        reply = next(self.messages)
        if isinstance(reply, Exception):
            raise reply
        return ModelReply(reply, self.model_name)


def action(name, args="{}", identifier="a"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": identifier, "type": "function", "function": {"name": name, "arguments": args}}]}


@pytest.mark.parametrize("case,expected", [("normal", "DRAFT_SAVED"), ("missing", "NEEDS_INFO"),
    ("risk", "BLOCKED"), ("conflict", "DRAFT_SAVED"), ("save_failure", "SAVE_FAILED"), ("manual_review", "NEEDS_REVIEW"), ("supplemented", "DRAFT_SAVED")])
def test_offline_scenarios(tmp_path, case, expected):
    request = ScheduleRequest.model_validate_json((ROOT / "examples" / f"{case}.input.json").read_text())
    result = run_case(request, mode="mock", db_path=tmp_path / "case.db", output_dir=tmp_path / "traces", simulate_save_failure=case == "save_failure")
    assert result["execution_status"] == "COMPLETED"
    assert result["outcomes"][0]["status"] == expected
    events = [json.loads(line) for line in Path(result["trace_files"]["events"]).read_text().splitlines()]
    tools = [e for e in events if e["event_type"] == "tool_result"]
    assert all(e["execution_mode"] == "mock" for e in events)
    for event in tools:
        assert {"step_index", "tool_name", "tool_arguments", "tool_response_summary", "decision_reason_summary", "execution_status", "error_summary", "next_action"} <= event.keys()
    if case == "conflict":
        assert sum(e["tool_name"] == "query_schedule" for e in tools) >= 2
        assert result["outcomes"][0]["draft"]["confirmation_status"] == "PENDING"
    if case == "save_failure":
        assert not result["outcomes"][0]["draft"]


def test_model_observes_invalid_arguments_and_tool_budget(schedule_request, registry_factory, tmp_path):
    model = ScriptedModel([action("read_material", "not json"), action("missing_tool")])
    trace = TraceRecorder(tmp_path, "invalid", "mock", model.model_name)
    result = AgentRuntime(model, registry_factory(schedule_request), trace, max_steps=3, max_tool_calls=1).run()
    assert result["execution_status"] == "INCOMPLETE"
    assert result["tool_calls"] == 1
    observation = json.loads(model.observed[1][-1]["content"])
    assert observation["status"] == "INVALID_ARGUMENTS"
    assert any(e["code"] == "TOOL_BUDGET_EXHAUSTED" for e in result["errors"])


def test_step_budget(schedule_request, registry_factory, tmp_path):
    model = ScriptedModel([action("read_material", json.dumps({"material_id": "M01", "reason_summary": "Read"}))])
    result = AgentRuntime(model, registry_factory(schedule_request), TraceRecorder(tmp_path, "budget", "mock", model.model_name), max_steps=1).run()
    assert result["execution_status"] == "INCOMPLETE"
    assert result["decision_steps"] == 1


def test_model_claim_is_not_database_success(schedule_request, registry_factory, tmp_path):
    model = ScriptedModel([{"role": "assistant", "content": "Successfully saved and published!"}])
    result = AgentRuntime(model, registry_factory(schedule_request), TraceRecorder(tmp_path, "claim", "mock", model.model_name)).run()
    assert result["execution_status"] == "INCOMPLETE"
    assert result["outcomes"][0]["draft"] is None
    assert "published" not in json.dumps(result)


def test_timeout_is_structured_failure(schedule_request, registry_factory, tmp_path):
    model = ScriptedModel([ModelError("MODEL_TIMEOUT", "Timed out")])
    result = AgentRuntime(model, registry_factory(schedule_request), TraceRecorder(tmp_path, "timeout", "mock", model.model_name)).run()
    assert result["execution_status"] == "FAILED"
    assert result["errors"][0]["code"] == "MODEL_TIMEOUT"


def test_multiple_materials_independently_processed(request_data, tmp_path):
    request = ScheduleRequest.model_validate({**request_data, "material_ids": ["M01", "M03", "M02"]})
    result = run_case(request, mode="mock", db_path=tmp_path / "multi.db", output_dir=tmp_path)
    assert [o["status"] for o in result["outcomes"]] == ["DRAFT_SAVED", "BLOCKED", "NEEDS_INFO"]


def test_secrets_are_redacted(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "example-secret-credential")
    assert safe({"note": "example-secret-credential", "api_key": "private", "authorization_scope": ["TikTok:US"]}) == {
        "note": "[REDACTED]", "api_key": "[REDACTED]", "authorization_scope": ["TikTok:US"]}


def test_storage_verification_failure_cannot_be_success(schedule_request, registry_factory, monkeypatch):
    from conftest import prepare, call
    registry = registry_factory(schedule_request)
    prepare(registry)
    assert call(registry, "save_schedule_draft", suggested_publish_time=schedule_request.requested_publish_time.isoformat(), confirmation_status="ORIGINAL_REQUEST")["status"] == "SAVED"
    def unavailable(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(registry.repository, "get", unavailable)
    assert registry.outcomes()[0]["status"] == "INCOMPLETE"
    assert registry.outcomes()[0]["verification_error"]["code"] == "DATABASE_VERIFICATION_FAILED"


def test_preflight_failure_leaves_trace(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_API_KEY", "")
    code = main(["--mode", "live", "--input", str(ROOT / "examples/normal.input.json"), "--output-dir", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert code == 2
    assert result["errors"][0]["code"] == "MODEL_NOT_CONFIGURED"
    assert Path(result["trace_files"]["events"]).exists()
