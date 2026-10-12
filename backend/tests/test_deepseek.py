import json

import httpx
import pytest

from werewolf.deepseek import ADAPTER_VERSION, DeepSeekAdapter, ProviderError, usage_from
from werewolf.profiles import GenerationParams, ModelConnection, SeatProfile
from werewolf.prompts import PromptBuilder
from werewolf.qualification_questions import prepare_question, questions
from werewolf.rulebook import Rulebook


def decision(**params):
    connection = ModelConnection(id="test", provider="deepseek", model="user-chosen-model",
                                 base_url="https://api.deepseek.com/v1", adapter_version=ADAPTER_VERSION,
                                 generation=GenerationParams(**params))
    return prepare_question(PromptBuilder(Rulebook.load()), SeatProfile(seat=1, connection_id="test"),
                            connection, questions()[0])


def envelope(content='{"kind":"inspect","target":2}', **extra):
    return {"choices": [{"finish_reason": "stop", "message": {"content": content,
                          "reasoning_content": "reasoning-canary-never-retain"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 60,
                      "completion_tokens_details": {"reasoning_tokens": 3}}, **extra}


@pytest.mark.asyncio
async def test_official_bounded_request_no_model_fallback_reasoning_or_keys_in_context(monkeypatch):
    sent = []
    monkeypatch.setenv("HTTPS_PROXY", "http://never-use-invalid-proxy:9999")

    def transport(request):
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer offline-canary-key"
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "user-chosen-model"
        assert body["thinking"] == {"type": "disabled"} and body["stream"] is False
        assert body["max_tokens"] == 256 and body["response_format"] == {"type": "json_object"}
        assert "offline-canary-key" not in json.dumps(body)
        assert len(body["messages"]) == 2
        assert "JSON" in body["messages"][0]["content"]
        return httpx.Response(200, json=envelope())

    adapter = DeepSeekAdapter("offline-canary-key", transport=httpx.MockTransport(transport))
    result = await adapter.complete(decision())
    assert len(sent) == 1 and not result.simulated
    assert "reasoning-canary" not in repr(result)
    assert result.usage.input_tokens == 100 and result.usage.cached_input_tokens == 60
    assert result.usage.output_tokens == 10 and result.usage.reasoning_tokens == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,retryable", [(401, "authentication", False), (403, "authentication", False),
    (402, "balance", False), (400, "request_parameters", False), (422, "request_parameters", False),
    (429, "rate_limit", True), (500, "server_error", True), (503, "server_error", True), (302, "http_error", False)])
async def test_http_failures_sanitized_without_redirects_or_hidden_retries(status, code, retryable):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(status, text="secret-response-canary", headers={"Location": "https://evil.example"})

    with pytest.raises(ProviderError) as error:
        await DeepSeekAdapter("fake", transport=httpx.MockTransport(handler)).complete(decision())
    assert error.value.code == code and error.value.retryable is retryable
    assert count == 1 and error.value.usage.input_tokens is None
    assert "secret-response-canary" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code", [
    ({"choices": []}, "invalid_envelope"),
    ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, "truncated_response"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}, "invalid_content"),
])
async def test_malformed_envelope_retains_reported_usage(body, code):
    body["usage"] = {"prompt_tokens": 88, "completion_tokens": 7}
    adapter = DeepSeekAdapter("fake", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(ProviderError) as error:
        await adapter.complete(decision())
    assert error.value.code == code and error.value.usage.input_tokens == 88


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,code", [("timeout", "timeout"), ("connect", "transport"),
                                      ("json", "invalid_envelope"), ("oversized", "oversized_response")])
async def test_timeout_transport_invalid_json_and_bound(kind, code):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("raw-key-canary", request=request)
        if kind == "connect":
            raise httpx.ConnectError("raw-key-canary", request=request)
        return httpx.Response(200, content=b"x" * (1_000_001 if kind == "oversized" else 20))
    with pytest.raises(ProviderError) as error:
        await DeepSeekAdapter("fake", transport=httpx.MockTransport(handler)).complete(decision())
    assert error.value.code == code and "raw-key-canary" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"reasoning_budget": 100}, {"max_output_tokens": 400000}])
async def test_unsupported_generation_fails_before_transport(params):
    def handler(_):
        pytest.fail("unsupported configuration must not make a paid request")
    with pytest.raises(ProviderError):
        await DeepSeekAdapter("fake", transport=httpx.MockTransport(handler)).complete(decision(**params))


def test_usage_missing_partial_aliases_invalid_counts_and_no_double_counting():
    assert usage_from(None).input_tokens is None
    partial = usage_from({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}})
    assert partial.input_tokens == 10 and partial.cached_input_tokens == 8 and partial.output_tokens is None
    malformed = usage_from({"prompt_tokens": True, "completion_tokens": -1, "prompt_cache_hit_tokens": "8"})
    assert malformed.input_tokens is malformed.output_tokens is malformed.cached_input_tokens is None
    beyond = usage_from({"prompt_tokens": 10, "prompt_cache_hit_tokens": 11, "completion_tokens": 2,
                         "completion_tokens_details": {"reasoning_tokens": 3}})
    assert beyond.cached_input_tokens is beyond.reasoning_tokens is None
