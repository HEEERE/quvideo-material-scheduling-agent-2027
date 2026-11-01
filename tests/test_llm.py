import json

import httpx
import pytest

from src.llm import LiveModel, ModelError


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key-never-live")
    monkeypatch.setenv("LLM_MODEL", "configured-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("LLM_EXTRA_BODY", raising=False)


def test_native_tool_protocol_and_reasoning_exclusion(configured):
    def handle(request):
        body = json.loads(request.content)
        assert body["tool_choice"] == "auto"
        assert body["parallel_tool_calls"] is False
        return httpx.Response(200, json={"id": "real-response-format", "model": "reported-model", "usage": {"total_tokens": 10},
            "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "reasoning_content": "DO NOT RECORD", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "read_material", "arguments": "{}"}}]}}]})
    reply = LiveModel(transport=httpx.MockTransport(handle)).complete([], [])
    assert reply.message["tool_calls"][0]["function"]["name"] == "read_material"
    assert "reasoning_content" not in reply.message
    assert reply.usage == {"total_tokens": 10}


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_error_no_fallback_or_body_logging(configured, status):
    model = LiveModel(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="private provider details")))
    with pytest.raises(ModelError) as exc:
        model.complete([], [])
    assert exc.value.code == "MODEL_HTTP_ERROR"
    assert "private provider" not in str(exc.value)


def test_timeout(configured):
    def timeout(request):
        raise httpx.ReadTimeout("sensitive endpoint", request=request)
    with pytest.raises(ModelError) as exc:
        LiveModel(transport=httpx.MockTransport(timeout)).complete([], [])
    assert exc.value.code == "MODEL_TIMEOUT"


def test_missing_credentials_explicit(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ModelError) as exc:
        LiveModel()
    assert exc.value.code == "MODEL_NOT_CONFIGURED"


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": {}, "finish_reason": "length"}]}])
def test_invalid_response(configured, payload):
    with pytest.raises(ModelError):
        LiveModel(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))).complete([], [])
