"""Provider metadata and LiteLLM model-name resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    label: str
    litellm_prefix: str
    default_key_env: str | None
    supports_api_base: bool = False
    common_models: tuple[str, ...] = ()


_PROVIDERS = (
    ProviderDefinition(
        "openai",
        "OpenAI",
        "openai",
        "OPENAI_API_KEY",
        supports_api_base=True,
        common_models=("gpt-5.1", "gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini"),
    ),
    ProviderDefinition(
        "dashscope",
        "Qwen / DashScope",
        "dashscope",
        "DASHSCOPE_API_KEY",
        common_models=(
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen-plus",
            "qwen-flash",
            "qwen3-coder-next",
        ),
    ),
    ProviderDefinition(
        "minimax",
        "MiniMax",
        "minimax",
        "MINIMAX_API_KEY",
        common_models=(
            "MiniMax-M3",
            "MiniMax-M2.7",
            "MiniMax-M2.7-highspeed",
            "MiniMax-M2.5",
            "MiniMax-M2.5-highspeed",
            "MiniMax-M2.1",
            "MiniMax-M2.1-highspeed",
            "MiniMax-M2",
            "M2-her",
        ),
    ),
    ProviderDefinition(
        "deepseek",
        "DeepSeek",
        "deepseek",
        "DEEPSEEK_API_KEY",
        common_models=(
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-chat",
            "deepseek-reasoner",
        ),
    ),
    ProviderDefinition(
        "anthropic",
        "Anthropic",
        "anthropic",
        "ANTHROPIC_API_KEY",
        common_models=(
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ),
    ),
    ProviderDefinition(
        "gemini",
        "Google Gemini",
        "gemini",
        "GEMINI_API_KEY",
        common_models=(
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ),
    ),
    ProviderDefinition(
        "groq",
        "Groq",
        "groq",
        "GROQ_API_KEY",
        common_models=(
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3.6-27b",
            "llama-3.1-8b-instant",
        ),
    ),
    ProviderDefinition(
        "ollama",
        "Ollama",
        "ollama",
        None,
        supports_api_base=True,
        common_models=("qwen3.5", "gemma4", "qwen3", "deepseek-r1", "llama3.2"),
    ),
    ProviderDefinition(
        "openai_compatible",
        "OpenAI Compatible",
        "openai",
        None,
        supports_api_base=True,
    ),
)

_ALIASES = {
    "qwen": "dashscope",
    "openai-compatible": "openai_compatible",
    "custom": "openai_compatible",
}
_BY_ID = {provider.id: provider for provider in _PROVIDERS}


def normalize_provider(provider: str) -> str:
    return _ALIASES.get(provider.lower().strip(), provider.lower().strip())


def get_provider(provider: str) -> ProviderDefinition:
    normalized = normalize_provider(provider)
    return _BY_ID.get(
        normalized,
        ProviderDefinition(normalized, provider, normalized, None, True),
    )


def provider_catalog() -> list[dict[str, str | None | bool | list[str]]]:
    return [
        {
            "id": provider.id,
            "label": provider.label,
            "default_key_env": provider.default_key_env,
            "supports_api_base": provider.supports_api_base,
            "common_models": list(provider.common_models),
        }
        for provider in _PROVIDERS
    ]


def resolve_model_name(provider: str, name: str, api_base: str | None = None) -> str:
    definition = get_provider(provider)
    normalized = normalize_provider(provider)
    if normalized == "openai" and not api_base:
        return name
    return f"{definition.litellm_prefix}/{name}"
