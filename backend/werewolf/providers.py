"""Dispatch by a frozen connection, not by credentials or a cheaper fallback."""

from . import deepseek, qwen, siliconflow
from .deepseek import ProviderError
from .profiles import GenerationParams, ModelConnection
from .prompts import DecisionRequest


def adapter_version(provider: str, model: str | None = None, generation: dict | None = None) -> str:
    if provider == "deepseek":
        if generation is not None and GenerationParams(**generation).thinking_mode == "low":
            return deepseek.THINKING_ADAPTER_VERSION
        return deepseek.ADAPTER_VERSION
    if provider == "siliconflow":
        if model == siliconflow.GLM_MODEL:
            return siliconflow.GLM_ADAPTER_VERSION
        return siliconflow.ADAPTER_VERSION
    if provider == "dashscope":
        if generation is not None and GenerationParams(**generation).thinking_mode == "low":
            return qwen.THINKING_ADAPTER_VERSION
        return qwen.ADAPTER_VERSION
    return f"{provider}-not-connected-v1"


def validate_connection(connection: ModelConnection) -> None:
    if connection.provider == "deepseek":
        deepseek.validate_connection(connection)
    elif connection.provider == "siliconflow":
        siliconflow.validate_connection(connection)
    elif connection.provider == "dashscope":
        qwen.validate_connection(connection)
    else:
        raise ProviderError("unsupported_connection")


def preflight(request: DecisionRequest) -> None:
    if request.connection.provider == "deepseek":
        deepseek.preflight(request)
    elif request.connection.provider == "siliconflow":
        siliconflow.preflight(request)
    elif request.connection.provider == "dashscope":
        qwen.preflight(request)
    else:
        raise ProviderError("unsupported_connection")


def build_adapter(connection: ModelConnection, credential: str):
    if connection.provider == "deepseek":
        if connection.thinking_enabled:
            return deepseek.DeepSeekAdapter(credential, timeout=60)
        return deepseek.DeepSeekAdapter(credential)
    if connection.provider == "siliconflow":
        return siliconflow.SiliconFlowAdapter(credential)
    if connection.provider == "dashscope":
        return qwen.QwenAdapter(credential, timeout=60 if connection.thinking_enabled else 30)
    raise ProviderError("unsupported_connection")
