from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class ModelError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ModelReply:
    message: dict
    model: str
    usage: dict | str = "unavailable"
    response_id: str | None = None


class LiveModel:
    execution_mode = "live"

    def __init__(self, timeout: float | None = None, transport=None):
        self.key = os.getenv("LLM_API_KEY", "")
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model_name = os.getenv("LLM_MODEL", "")
        if not self.key or not self.model_name:
            raise ModelError("MODEL_NOT_CONFIGURED", "Set LLM_API_KEY and LLM_MODEL in local environment or .env; no mock fallback")
        url = urlsplit(self.base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ModelError("INVALID_BASE_URL", "Use an HTTPS API base URL without embedded credentials, query or fragment")
        try:
            self.timeout = float(timeout if timeout is not None else os.getenv("LLM_TIMEOUT_SECONDS", "45"))
            if not 0 < self.timeout <= 300:
                raise ValueError()
            self.extra_body = json.loads(os.getenv("LLM_EXTRA_BODY", "{}"))
            if not isinstance(self.extra_body, dict) or any(k in self.extra_body for k in ("model", "messages", "tools", "tool_choice", "stream", "parallel_tool_calls")):
                raise ValueError()
        except (ValueError, TypeError):
            raise ModelError("INVALID_MODEL_CONFIG", "Invalid timeout or LLM_EXTRA_BODY") from None
        self.transport = transport

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        body = {"model": self.model_name, "messages": messages, "tools": tools,
                "tool_choice": "auto", "parallel_tool_calls": False, "max_tokens": 2048, **self.extra_body}
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
                response = client.post(self.base_url + "/chat/completions", headers={"Authorization": "Bearer " + self.key}, json=body)
            if response.status_code != 200:
                raise ModelError("MODEL_HTTP_ERROR", f"Model API returned HTTP {response.status_code}; no automatic retry or mock fallback")
            data = response.json()
            original = data["choices"][0]["message"]
            if original.get("role") != "assistant" or not isinstance(original.get("content") or "", str):
                raise ValueError("Invalid assistant message")
            if data["choices"][0].get("finish_reason") in {"length", "content_filter"}:
                raise ModelError("MODEL_RESPONSE_INCOMPLETE", "Model response was truncated or filtered")
            message = {"role": "assistant", "content": original.get("content") or ""}
            calls = original.get("tool_calls", [])
            if calls:
                if len(calls) > 20 or len({c["id"] for c in calls}) != len(calls):
                    raise ValueError("Invalid tool call batch")
                message["tool_calls"] = []
                for c in calls:
                    f = c["function"]
                    if c["type"] != "function" or not isinstance(c["id"], str) or not isinstance(f["arguments"], str) or not isinstance(f["name"], str):
                        raise ValueError("Malformed function call")
                    message["tool_calls"].append({"id": c["id"], "type": "function", "function": {"name": f["name"], "arguments": f["arguments"]}})
            # Intentionally do not read, persist or request hidden reasoning fields.
            return ModelReply(message, data.get("model", self.model_name), data.get("usage") or "unavailable", data.get("id"))
        except httpx.TimeoutException:
            raise ModelError("MODEL_TIMEOUT", "Single model request exceeded configured timeout") from None
        except httpx.HTTPError:
            raise ModelError("MODEL_NETWORK_ERROR", "Model connection failed; endpoint details omitted") from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("MODEL_PROTOCOL_ERROR", "Response does not conform to Chat Completions tool calling") from None
