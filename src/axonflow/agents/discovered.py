"""Workflow slot Agent that discovers and fails over between concrete Agents."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from axonflow.config.models import AgentConfig, DiscoveryConfig
from axonflow.core.agent import BaseAgent, create_agent
from axonflow.core.message import Message
from axonflow.core.protocol import (
    DataItem,
    DelegationRequest,
    Product,
    TaskResult,
    TaskState,
    TaskStatus,
    protocol_context,
)
from axonflow.discovery.local import DiscoveryCandidate, LocalDiscoveryService

logger = structlog.get_logger()


class DiscoveredAgent(BaseAgent):
    """Keep a stable workflow identity while selecting concrete Agents at runtime."""

    def __init__(
        self,
        *args: Any,
        candidate_configs: list[AgentConfig],
        discovery: DiscoveryConfig,
        preferred_template_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.discovery_config = discovery
        self.preferred_template_id = preferred_template_id
        self._candidate_configs = {
            item.id: item.model_copy(deep=True) for item in candidate_configs
        }
        self._discovery = LocalDiscoveryService(self._candidate_configs.values())

    def _candidate_list(self) -> list[DiscoveryCandidate]:
        policy = self.discovery_config
        excluded = list(dict.fromkeys([*policy.exclude_agents]))
        if self.preferred_template_id:
            excluded.append(self.preferred_template_id)
        request = DelegationRequest(
            description=policy.description,
            required_skills=policy.required_skills,
            required_tools=policy.required_tools,
            tags=policy.tags,
            exclude_agents=excluded,
            max_candidates=policy.max_candidates,
            min_score=policy.min_score,
        )
        candidates = self._discovery.discover(request)
        if self.preferred_template_id and self.preferred_template_id in self._candidate_configs:
            candidates.insert(
                0,
                DiscoveryCandidate(
                    agent_id=self.preferred_template_id,
                    score=1.0,
                    reason="preferred workflow Agent; discovery is enabled for failover",
                ),
            )
        return candidates[: policy.max_candidates]

    async def handle_message(self, message: Message) -> dict[str, Any]:
        candidates = self._candidate_list()
        if not candidates:
            return self._failed_result(message, [], "No Agent matched the discovery request")

        attempts: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates, start=1):
            template = self._candidate_configs[candidate.agent_id].model_copy(deep=True)
            template.id = self.id
            template.name = self.name
            template.parameters["discovered_template_id"] = candidate.agent_id
            concrete = create_agent(
                config=template,
                message_bus=self.message_bus,
                llm_gateway=self.llm_gateway,
                tool_registry=self.tool_registry,
                memory_store=self.memory,
                execution_logger=self.execution_logger,
                skills_dir=self._skills_dir,
            )
            context = self.get_context(message.workflow_id)
            if context:
                concrete.set_context(message.workflow_id, context)

            payload = dict(message.payload)
            selected_protocol = protocol_context(
                session_id=message.session_id or message.workflow_id,
                task_id=message.task_id or message.step_id,
                requested_capability=self.discovery_config.description,
                selected_agent=candidate.agent_id,
                attempt=index,
                previous_attempts=attempts,
            )
            original_protocol = payload.get("_protocol")
            if isinstance(original_protocol, dict):
                if original_protocol.get("command"):
                    selected_protocol["command"] = original_protocol["command"]
                if original_protocol.get("parent_task_id"):
                    selected_protocol["parent_task_id"] = original_protocol["parent_task_id"]
            payload["_protocol"] = selected_protocol
            selected_message = Message(
                sender=message.sender,
                receiver=self.id,
                type=message.type,
                payload=payload,
                id=message.id,
                workflow_id=message.workflow_id,
                step_id=message.step_id,
                priority=message.priority,
                context=message.context,
                created_at=message.created_at,
                ttl=message.ttl,
                parent_message_id=message.parent_message_id,
                protocol_version=message.protocol_version,
                session_id=message.session_id,
                task_id=message.task_id,
            )
            try:
                result = await asyncio.wait_for(
                    concrete._process_with_retry(selected_message),
                    timeout=self.discovery_config.timeout_seconds,
                )
            except TimeoutError:
                attempt = {
                    "agent_id": candidate.agent_id,
                    "status": "timeout",
                    "error": f"No response within {self.discovery_config.timeout_seconds}s",
                }
                attempts.append(attempt)
                logger.warning("discovery.agent_timeout", slot_id=self.id, **attempt)
                if not self.discovery_config.fallback_on_timeout:
                    return self._failed_result(message, attempts, attempt["error"])
                continue
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}

            if result.get("status") == "success":
                return self._successful_result(message, result, candidate, attempts)

            attempt = {
                "agent_id": candidate.agent_id,
                "status": str(result.get("status", "error")),
                "error": result.get("error") or result.get("content") or "Agent failed",
            }
            attempts.append(attempt)
            logger.warning("discovery.agent_failed", slot_id=self.id, **attempt)
            if not self.discovery_config.fallback_on_error:
                return self._failed_result(message, attempts, str(attempt["error"]))

        return self._failed_result(message, attempts, "All discovered Agents failed")

    def _successful_result(
        self,
        message: Message,
        result: dict[str, Any],
        candidate: DiscoveryCandidate,
        previous_attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        enriched = dict(result)
        enriched["discovery"] = {
            "slot_id": self.id,
            "selected_agent_id": candidate.agent_id,
            "score": candidate.score,
            "reason": candidate.reason,
            "previous_attempts": previous_attempts,
        }
        content = str(enriched.get("content", ""))
        task_result = TaskResult(
            session_id=message.session_id or message.workflow_id,
            task_id=message.task_id or message.step_id,
            sender_id=self.id,
            status=TaskStatus(state=TaskState.COMPLETED),
            products=(
                [Product(name="Agent result", data_items=[DataItem(type="text", text=content)])]
                if content
                else []
            ),
            metadata={"selected_agent_id": candidate.agent_id},
        )
        enriched["task_result"] = task_result.model_dump(mode="json")
        return enriched

    def _failed_result(
        self,
        message: Message,
        attempts: list[dict[str, Any]],
        error: str,
    ) -> dict[str, Any]:
        task_result = TaskResult(
            session_id=message.session_id or message.workflow_id,
            task_id=message.task_id or message.step_id,
            sender_id=self.id,
            status=TaskStatus(
                state=TaskState.FAILED,
                data_items=[DataItem(type="text", text=error)],
            ),
            metadata={"attempts": attempts},
        )
        return {
            "status": "error",
            "error": error,
            "discovery": {"slot_id": self.id, "attempts": attempts},
            "task_result": task_result.model_dump(mode="json"),
        }
