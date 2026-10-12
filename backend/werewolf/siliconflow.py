"""SiliconFlow Hy4 / GLM-5.3: bounded JSON requests, no model fallback."""

from .deepseek import DeepSeekAdapter, ProviderError
from .profiles import ModelConnection
from .prompts import DecisionRequest

OFFICIAL_URL = "https://api.siliconflow.cn/v1"
HUNYUAN_MODEL = "tencent/Hy4-preview"
GLM_MODEL = "zai-org/GLM-5.3"
SUPPORTED_MODELS = (HUNYUAN_MODEL, GLM_MODEL)
ADAPTER_VERSION = "siliconflow-hy4-json-request-disable-thinking-v1"
GLM_ADAPTER_VERSION = "siliconflow-glm53-json-request-disable-thinking-v1"


def validate_connection(connection: ModelConnection) -> None:
    if connection.provider != "siliconflow" or connection.base_url != OFFICIAL_URL:
        raise ProviderError("unsupported_connection")
    if connection.model not in SUPPORTED_MODELS:
        raise ProviderError("unsupported_model")
    if connection.generation.reasoning_budget is not None:
        raise ProviderError("unsupported_reasoning_budget")
    if connection.generation.thinking_mode != "disabled":
        raise ProviderError("unsupported_thinking_provider")
    # Conservative application cap, not a claim about every model's provider limit.
    if not 1 <= connection.generation.max_output_tokens <= 65536 or not 1 <= connection.generation.target_output_tokens <= 65536:
        raise ProviderError("invalid_output_limit")


def preflight(request: DecisionRequest) -> None:
    validate_connection(request.connection)
    if not 1 <= request.max_output_tokens <= 65536:
        raise ProviderError("invalid_output_limit")
    if request.reasoning_budget is not None and (type(request.reasoning_budget) is not int or not 128 <= request.reasoning_budget <= 32768):
        raise ProviderError("invalid_reasoning_limit")


class SiliconFlowAdapter(DeepSeekAdapter):
    # Reuse only bounded transport/envelope handling, never DeepSeek-specific parameters.
    official_url = OFFICIAL_URL
    validate = staticmethod(preflight)

    def payload(self, request: DecisionRequest) -> dict:
        payload = {
            "model": request.connection.model, "messages": request.wire_messages(), "stream": False,
            "temperature": request.connection.generation.temperature, "max_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"}, "enable_thinking": False,
        }
        if request.reasoning_budget is not None:
            # A safety request for endpoints that still reason despite enable_thinking=False.
            # Never enable thinking or fall back to an uncapped request on rejection.
            payload["thinking_budget"] = request.reasoning_budget
        return payload
