"""LLM 统一调用网关 — 基于 LiteLLM 适配多模型"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import litellm
import structlog
from aiohttp import ClientSession, ClientTimeout

from axonflow.config.models import ModelConfig
from axonflow.llm.providers import get_provider, resolve_model_name
from axonflow.llm.token_tracker import TokenTracker
from axonflow.observability.langsmith import LangSmithReporter

logger = structlog.get_logger()


class BudgetExceededError(Exception):
    """Token 预算超限"""


class LLMUnavailableError(Exception):
    """所有 LLM 模型不可用"""


@dataclass
class LLMResponse:
    """LLM 调用结果"""

    content: str
    model: str
    tool_calls: list[dict] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class LLMTraceContext:
    """Associates a model invocation with the product-facing workflow run."""

    workflow_id: str
    execution_id: str
    agent_id: str
    run_id: str | None = None
    trace_kind: str = "agent"


class LLMGateway:
    """统一 LLM 调用网关

    - 通过 LiteLLM 适配 OpenAI / Anthropic / Ollama 等多种后端
    - 支持模型降级（主模型不可用时自动切换备用模型）
    - Token 用量追踪与预算控制
    """

    def __init__(
        self,
        default_model: ModelConfig | None = None,
        token_budget: int | None = None,
        credential_resolver: Callable[[str], dict[str, str]] | None = None,
        span_store: Any | None = None,
    ) -> None:
        self._default_model = default_model or ModelConfig()
        self.token_tracker = TokenTracker(budget=token_budget)
        self._credential_resolver = credential_resolver
        self._span_store = span_store
        self._langsmith = LangSmithReporter(
            span_store.get_observability_settings if span_store else None,
            credential_resolver,
        )
        self._workflow_traces: dict[str, Any] = {}

        # 关闭 litellm 自带的冗余日志
        litellm.suppress_debug_info = True

    def _validate_model_config(self, config: ModelConfig | None) -> tuple[bool, str | None]:
        """检查模型配置是否可用"""
        if config is None:
            return False, "missing_config"
        if not config.provider or not config.provider.strip():
            return False, "missing_provider"
        if not config.name or not config.name.strip():
            return False, "missing_model_name"
        if config.max_tokens <= 0:
            return False, "invalid_max_tokens"
        if config.timeout <= 0:
            return False, "invalid_timeout"
        if config.api_key_env is not None and not config.api_key_env.strip():
            return False, "invalid_api_key_env"
        return True, None

    def _select_model_config(
        self,
        override: ModelConfig | None,
        prefer_default: bool,
    ) -> ModelConfig:
        """Resolve Agent overrides before the global default model."""
        if override:
            override_ok, override_reason = self._validate_model_config(override)
            if override_ok:
                return override
            logger.error(
                "llm.agent_model_invalid",
                reason=override_reason,
            )

        default_ok, default_reason = self._validate_model_config(self._default_model)
        if default_ok:
            return self._default_model
        logger.warning("llm.default_model_invalid", reason=default_reason)

        raise LLMUnavailableError("No valid LLM model configuration available")

    def _resolve_model_string(self, config: ModelConfig) -> str:
        """构建 litellm 识别的模型字符串

        例如: openai/gpt-4o, anthropic/claude-3-opus, ollama/llama3

        当使用自定义 api_base 时，加上 openai/ 前缀确保 litellm
        走 OpenAI 兼容协议路径。
        """
        return resolve_model_name(config.provider, config.name, config.api_base)

    async def start_workflow_trace(self, run_id: str, workflow_id: str, input_data: str) -> None:
        """Create the LangSmith root span before child Agent/LLM spans are emitted."""
        root_run = await self._langsmith.start_workflow(
            trace_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            input_data=input_data,
        )
        if root_run is not None:
            self._workflow_traces[run_id] = root_run

    async def finish_workflow_trace(
        self,
        run_id: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        root_run = self._workflow_traces.pop(run_id, None)
        await self._langsmith.finish(root_run, outputs=result, error=error)

    def _setup_auth(self, config: ModelConfig) -> tuple[dict[str, str], str | None]:
        """Resolve a platform credential first, then retain environment variable compatibility."""
        extra_kwargs: dict = {}
        credential_id: str | None = None
        if config.credential_id:
            if self._credential_resolver is None:
                raise LLMUnavailableError("Credential storage is unavailable")
            credential = self._credential_resolver(config.credential_id)
            extra_kwargs["api_key"] = credential["secret"]
            credential_id = credential["id"]
        else:
            env_var = config.api_key_env or get_provider(config.provider).default_key_env
            key = os.environ.get(env_var) if env_var else None
            if not key:
                safe_env_var = (
                    env_var
                    if env_var and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_var)
                    else "[invalid environment variable]"
                )
                logger.warning(
                    "llm.api_key_missing",
                    env_var=safe_env_var,
                )
            else:
                extra_kwargs["api_key"] = key
        if config.api_base:
            extra_kwargs["api_base"] = config.api_base
        return extra_kwargs, credential_id

    async def _complete_provider_request(self, config: ModelConfig, call_kwargs: dict) -> Any:
        """Use a provider-native request where LiteLLM has no implementation."""
        if config.provider.lower() == "minimax" and config.name == "MiniMax-M3":
            return await self._minimax_m3_completion(config, call_kwargs)
        return await litellm.acompletion(**call_kwargs)

    async def _minimax_m3_completion(self, config: ModelConfig, call_kwargs: dict) -> Any:
        """Call the MiniMax M3 native endpoint and normalize it to LiteLLM's response shape."""
        api_base = (config.api_base or "https://api.minimaxi.com/v1").rstrip("/")
        api_key = call_kwargs.get("api_key")
        if not api_key:
            raise LLMUnavailableError("MiniMax M3 requires an API key")

        payload: dict[str, Any] = {
            "model": config.name,
            "messages": call_kwargs["messages"],
            "temperature": config.temperature,
            "max_completion_tokens": config.max_tokens,
        }
        if call_kwargs.get("tools"):
            payload["tools"] = call_kwargs["tools"]

        timeout = ClientTimeout(total=config.timeout)
        async with ClientSession(timeout=timeout) as session, session.post(
            f"{api_base}/text/chatcompletion_v2",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                raise LLMUnavailableError(
                    f"MiniMax M3 HTTP {response.status}: {response_text[:500]}"
                )

        try:
            data = json.loads(response_text)
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMUnavailableError("MiniMax M3 returned an invalid response") from exc

        usage = data.get("usage", {})
        normalized_message = SimpleNamespace(
            content=message.get("content"),
            tool_calls=message.get("tool_calls"),
            reasoning_content=message.get("reasoning_content"),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=normalized_message)],
            usage=SimpleNamespace(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            ),
        )

    @staticmethod
    def _normalize_tool_call(tool_call: Any) -> dict[str, Any]:
        """Normalize both LiteLLM objects and provider-native dictionaries."""
        if isinstance(tool_call, dict):
            function = tool_call.get("function") or {}
            if not isinstance(function, dict):
                raise ValueError("Tool call function must be an object")
            call_id = tool_call.get("id")
            call_type = tool_call.get("type", "function")
            name = function.get("name")
            arguments = function.get("arguments", "{}")
        else:
            function = tool_call.function
            call_id = tool_call.id
            call_type = getattr(tool_call, "type", "function")
            name = function.name
            arguments = function.arguments

        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Tool call id must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ValueError("Tool call function name must be a non-empty string")
        return {
            "id": call_id,
            "function": {"name": name, "arguments": arguments},
            "type": str(call_type or "function"),
        }

    @staticmethod
    def _preview_messages(messages: list[dict], content_policy: str) -> str | None:
        if content_policy == "metadata_only":
            return None
        parts = [str(message.get("content", "")) for message in messages if message.get("content")]
        preview = "\n".join(parts)
        if content_policy == "masked_content":
            return f"{len(messages)} messages, {len(preview)} characters"
        return preview[:2000]

    async def chat(
        self,
        messages: list[dict],
        model_config: ModelConfig | None = None,
        tools: list[dict] | None = None,
        prefer_default: bool = True,
        trace_context: LLMTraceContext | None = None,
        **kwargs,
    ) -> LLMResponse:
        """统一 LLM 调用入口

        Args:
            messages: OpenAI 格式的消息列表
            model_config: 模型配置，不传则使用默认配置
            tools: Function Calling 工具定义
            prefer_default: 是否优先尝试全局默认模型
            **kwargs: 传递给 litellm 的额外参数

        Returns:
            LLMResponse 包含回复内容和 Token 用量
        """
        config = self._select_model_config(model_config, prefer_default=prefer_default)

        # 预算检查
        if self.token_tracker.is_budget_exceeded():
            raise BudgetExceededError(f"Token budget exceeded: {self.token_tracker.total_tokens}")

        env_kwargs, credential_id = self._setup_auth(config)
        model_str = self._resolve_model_string(config)
        started_at = datetime.now(UTC)
        span_id = str(uuid.uuid4())
        content_policy = "masked_content"
        if self._span_store is not None:
            content_policy = self._span_store.get_observability_settings().get(
                "content_policy", content_policy
            )
        span = {
            "id": span_id,
            "run_id": trace_context.run_id if trace_context else None,
            "workflow_id": trace_context.workflow_id if trace_context else None,
            "execution_id": trace_context.execution_id if trace_context else None,
            "agent_id": trace_context.agent_id if trace_context else None,
            "trace_kind": trace_context.trace_kind if trace_context else "unscoped",
            "provider": config.provider,
            "model": model_str,
            "credential_id": credential_id,
            "started_at": started_at.isoformat(),
            "input_preview": self._preview_messages(messages, content_policy),
            "metadata": {
                "provider": config.provider,
                "model": model_str,
                "content_policy": content_policy,
            },
        }
        if self._span_store is not None:
            self._span_store.create_llm_span(span)
        langsmith_run = await self._langsmith.start(
            span,
            (
                {"messages": messages}
                if content_policy == "full_content"
                else {"message_count": len(messages)}
            ),
            parent=(
                self._workflow_traces.get(trace_context.run_id)
                if trace_context and trace_context.run_id
                else None
            ),
        )

        call_kwargs: dict = {
            "model": model_str,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": config.timeout,
            **env_kwargs,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        try:
            response = await asyncio.wait_for(
                self._complete_provider_request(config, call_kwargs),
                timeout=config.timeout,
            )

            input_tokens = response.usage.prompt_tokens if response.usage else 0
            output_tokens = response.usage.completion_tokens if response.usage else 0

            self.token_tracker.record(
                model=model_str,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            msg = response.choices[0].message
            content = msg.content or ""

            # 解析 tool_calls（先于 content 处理，因为 content 的回退策略依赖是否有 tool_calls）
            tool_calls = None
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tool_calls = [self._normalize_tool_call(tc) for tc in msg.tool_calls]

            # thinking 模型（如 Qwen3）在有 tool_calls 时 content 为空白，这是正常的。
            # 只有在：(1) 无 tool_calls 且 (2) content 真正为空时，才回退到 reasoning_content。
            # 这样避免把推理链污染进 assistant 的 content 消息。
            if (
                not tool_calls
                and not content.strip()
                and hasattr(msg, "reasoning_content")
                and msg.reasoning_content
            ):
                content = msg.reasoning_content

            logger.info(
                "llm.call_completed",
                model=model_str,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                has_tool_calls=tool_calls is not None,
            )

            await self._complete_span(
                span_id,
                started_at,
                input_tokens,
                output_tokens,
                self._preview_messages([{"content": content}], content_policy),
                None,
            )
            await self._langsmith.finish(
                langsmith_run,
                {"content": content} if content_policy == "full_content" else {"status": "success"},
            )

            return LLMResponse(
                content=content,
                model=model_str,
                tool_calls=tool_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )

        except Exception as e:
            logger.error("llm.call_failed", model=model_str, error=str(e))
            await self._complete_span(span_id, started_at, 0, 0, None, str(e))
            await self._langsmith.finish(langsmith_run, error=str(e))
            # 尝试降级
            if config.fallback_models:
                return await self._fallback(
                    messages, config, e, tools=tools, trace_context=trace_context, **kwargs
                )
            raise LLMUnavailableError(f"LLM call failed: {e}") from e

    async def _complete_span(
        self,
        span_id: str,
        started_at: datetime,
        input_tokens: int,
        output_tokens: int,
        output_preview: str | None,
        error: str | None,
    ) -> None:
        if self._span_store is None:
            return
        completed_at = datetime.now(UTC)
        self._span_store.complete_llm_span(
            span_id,
            {
                "status": "error" if error else "completed",
                "completed_at": completed_at.isoformat(),
                "latency_ms": int((completed_at - started_at).total_seconds() * 1000),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "output_preview": output_preview,
                "error": error,
                "langsmith_trace_url": None,
            },
        )

    async def _fallback(
        self,
        messages: list[dict],
        original_config: ModelConfig,
        error: Exception,
        **kwargs,
    ) -> LLMResponse:
        """模型降级"""
        for fallback_model_name in original_config.fallback_models:
            try:
                logger.warning(
                    "llm.fallback",
                    original_error=str(error),
                    fallback_model=fallback_model_name,
                )
                fallback_config = ModelConfig(
                    provider=original_config.provider,
                    name=fallback_model_name,
                    temperature=original_config.temperature,
                    max_tokens=original_config.max_tokens,
                    timeout=original_config.timeout,
                    api_base=original_config.api_base,
                    api_key_env=original_config.api_key_env,
                    credential_id=original_config.credential_id,
                )
                return await self.chat(
                    messages,
                    model_config=fallback_config,
                    prefer_default=False,
                    **kwargs,
                )
            except Exception as fallback_err:
                logger.error(
                    "llm.fallback_failed",
                    model=fallback_model_name,
                    error=str(fallback_err),
                )
                continue

        raise LLMUnavailableError("All LLM models unavailable") from error
