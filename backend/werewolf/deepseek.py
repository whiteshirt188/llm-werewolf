"""One bounded official DeepSeek request. Retries belong to the caller's ledger."""

import httpx

from .agent import ModelResponse, ModelUsage
from .profiles import ModelConnection
from .prompts import DecisionRequest

ADAPTER_VERSION = "deepseek-json-nonthinking-v1"
THINKING_ADAPTER_VERSION = "deepseek-json-thinking-low-v1"
THINKING_MODELS = frozenset({"deepseek-flash", "deepseek-v4-pro"})
OFFICIAL_URL = "https://api.deepseek.com/v1"


class ProviderError(Exception):
    def __init__(self, code: str, *, retryable: bool = False, usage: ModelUsage | None = None):
        # Never retain provider bodies, request headers, API keys or reasoning content.
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.usage = usage or ModelUsage()


def validate_connection(connection: ModelConnection) -> None:
    if connection.provider != "deepseek" or connection.base_url != OFFICIAL_URL:
        raise ProviderError("unsupported_connection")
    if connection.generation.reasoning_budget is not None:
        raise ProviderError("unsupported_reasoning_budget")
    if connection.thinking_enabled and connection.model not in THINKING_MODELS:
        raise ProviderError("unsupported_thinking_model")
    if not 1 <= connection.generation.max_output_tokens <= 393216 or not 1 <= connection.generation.target_output_tokens <= 393216:
        raise ProviderError("invalid_output_limit")


def preflight(request: DecisionRequest) -> None:
    validate_connection(request.connection)
    if not 1 <= request.max_output_tokens <= 393216:
        raise ProviderError("invalid_output_limit")


def usage_from(data: object) -> ModelUsage:
    if not isinstance(data, dict):
        return ModelUsage()

    def number(value):
        return value if type(value) is int and value >= 0 else None

    incoming, outgoing = number(data.get("prompt_tokens")), number(data.get("completion_tokens"))
    details = data.get("prompt_tokens_details")
    cached = number(data.get("prompt_cache_hit_tokens"))
    if cached is None and isinstance(details, dict):
        cached = number(details.get("cached_tokens"))
    output_details = data.get("completion_tokens_details")
    reasoning = number(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    if incoming is not None and cached is not None and cached > incoming:
        cached = None
    if outgoing is not None and reasoning is not None and reasoning > outgoing:
        reasoning = None
    # Cache and reasoning are subsets of input/output, not extra tokens.
    return ModelUsage(incoming, outgoing, cached, reasoning)


class DeepSeekAdapter:
    official_url = OFFICIAL_URL
    validate = staticmethod(preflight)

    def __init__(self, credential: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 simulated: bool = False, timeout: float = 30):
        if not credential or not credential.isascii() or any(c.isspace() for c in credential):
            raise ProviderError("invalid_credential_format")
        self._credential = credential
        self.simulated = simulated
        self._transport, self._simulated, self._timeout = transport, simulated, timeout

    def payload(self, request: DecisionRequest) -> dict:
        payload = {
            "model": request.connection.model, "messages": request.wire_messages(), "stream": False,
            "temperature": request.connection.generation.temperature, "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
        }
        if request.connection.thinking_enabled:
            payload.pop("temperature")  # Ignored by the provider in thinking mode.
            payload.update(thinking={"type": "enabled"}, reasoning_effort="low")
        return payload

    async def complete(self, request: DecisionRequest) -> ModelResponse:
        self.validate(request)
        payload = self.payload(request)
        try:
            # No proxy environment, redirects, SDK retries or fallback model.
            async with httpx.AsyncClient(transport=self._transport, trust_env=False, follow_redirects=False,  # noqa: SIM117
                                         timeout=max(self._timeout, 30 * (request.timeout_multiplier or 0))) as client:
                async with client.stream("POST", f"{self.official_url}/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {self._credential}"}) as response:
                    if response.status_code != 200:
                        status = response.status_code
                        code = {401: "authentication", 403: "authentication", 402: "balance",
                                400: "request_parameters", 422: "request_parameters", 429: "rate_limit"}.get(
                            status, "server_error" if status >= 500 else "http_error"
                        )
                        raise ProviderError(code, retryable=status == 429 or status >= 500)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1_000_000:
                            raise ProviderError("oversized_response", retryable=True)
                    import json
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeError):
                        raise ProviderError("invalid_envelope", retryable=True) from None
        except httpx.TimeoutException:
            raise ProviderError("timeout", retryable=True) from None
        except httpx.TransportError:
            raise ProviderError("transport", retryable=True) from None
        if not isinstance(data, dict):
            raise ProviderError("invalid_envelope", retryable=True)
        usage = usage_from(data.get("usage"))
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError("invalid_envelope", retryable=True, usage=usage)
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise ProviderError("truncated_response", retryable=True, usage=usage)
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip() or len(content) > 16_000:
            raise ProviderError("invalid_content", retryable=True, usage=usage)
        # Reasoning text is deliberately not read, retained or returned.
        return ModelResponse(content, usage, simulated=self._simulated)
