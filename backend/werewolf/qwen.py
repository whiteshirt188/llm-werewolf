"""Official Beijing Qwen Chat API; frozen models and the shared round/role caps."""

from .deepseek import DeepSeekAdapter, ProviderError
from .profiles import ModelConnection
from .prompts import DecisionRequest

ADAPTER_VERSION = "qwen-json-nonthinking-v1"
THINKING_ADAPTER_VERSION = "qwen-json-thinking-bounded-v1"
OFFICIAL_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LEGACY_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_MODEL = "qwen3.6-plus"
# Commercial models verified against the official API docs on 2026-10-12.
# These support non-stream responses and max_completion_tokens (reasoning + answer).
COMBINED_MODELS = frozenset({
    "qwen3.5-plus", "qwen3.5-plus-2026-02-15", "qwen3.5-flash", "qwen3.5-flash-2026-02-23",
    "qwen3.6-plus", "qwen3.6-plus-2026-04-02", "qwen3.6-flash", "qwen3.6-flash-2026-04-16",
    "qwen3.7-plus", "qwen3.7-plus-2026-05-26", "qwen3.7-max", "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-06-08", "qwen3.7-flash", "qwen3.7-flash-2026-07-15",
    "qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash",
})
LEGACY_MODELS = frozenset({"qwen-plus", "qwen-plus-latest", "qwen-flash"})


def validate_connection(connection: ModelConnection) -> None:
    if connection.provider != "dashscope" or connection.base_url not in {OFFICIAL_URL, LEGACY_URL}:
        raise ProviderError("unsupported_connection")
    if connection.model not in COMBINED_MODELS | LEGACY_MODELS:
        raise ProviderError("unsupported_qwen_model")
    if connection.thinking_enabled and connection.model not in COMBINED_MODELS:
        raise ProviderError("unsupported_qwen_thinking_model")
    if connection.generation.reasoning_budget is not None:
        raise ProviderError("unsupported_reasoning_budget")
    if not all(1 <= n <= 32768 for n in (connection.generation.max_output_tokens,
                                        connection.generation.target_output_tokens)):
        raise ProviderError("invalid_output_limit")


def preflight(request: DecisionRequest) -> None:
    validate_connection(request.connection)
    if not 1 <= request.max_output_tokens <= 32768:
        raise ProviderError("invalid_output_limit")


class QwenAdapter(DeepSeekAdapter):
    # Share bounded transport, safe errors and usage parsing, not DeepSeek parameters.
    official_url = OFFICIAL_URL
    validate = staticmethod(preflight)

    def payload(self, request: DecisionRequest) -> dict:
        c = request.connection
        payload = {"model": c.model, "messages": request.wire_messages(), "stream": False,
                   "enable_thinking": c.thinking_enabled}
        key = "max_completion_tokens" if c.model in COMBINED_MODELS else "max_tokens"
        payload[key] = request.max_output_tokens
        if not c.model.startswith("qwen3.5-") and c.model in COMBINED_MODELS:
            payload["preserve_thinking"] = False
        # Qwen3.5/3.6 JSON mode only supports thinking disabled. The prompt and
        # application Schema validate every thinking response without rewriting it.
        json_with_thinking = c.model.startswith(("qwen3.7-", "qwen3.8-"))
        if not c.thinking_enabled or json_with_thinking:
            payload["response_format"] = {"type": "json_object"}
        if not c.thinking_enabled:
            payload["temperature"] = c.generation.temperature
        return payload
