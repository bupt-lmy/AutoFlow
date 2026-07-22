"""HTTP-backed Agent for external generation and project services."""

from __future__ import annotations

import json
import os
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message


class RemoteAgent(BaseAgent):
    """Delegate a workflow task to a service behind an explicit HTTP contract.

    ``parameters.remote`` accepts ``endpoint``, optional ``credential_id`` or
    ``api_key_env``, ``timeout`` and an ``auth_header``/``auth_scheme`` pair.
    The service receives a JSON document with the task payload and must return
    JSON (or text) synchronously. Long-running services should wait/poll on
    their side and return artifact URLs in their final response.
    """

    async def handle_message(self, message: Message) -> dict[str, Any]:
        remote = self.config.parameters.get("remote", {})
        if not isinstance(remote, dict) or not isinstance(remote.get("endpoint"), str):
            return {"status": "error", "error": "Remote Agent requires parameters.remote.endpoint"}

        headers = {"Content-Type": "application/json"}
        api_key = self._resolve_api_key(remote)
        if api_key:
            header_name = str(remote.get("auth_header", "Authorization"))
            scheme = str(remote.get("auth_scheme", "Bearer")).strip()
            headers[header_name] = f"{scheme} {api_key}".strip()

        payload = {
            "task": message.payload,
            "workflow_id": message.workflow_id,
            "agent_id": self.id,
        }
        timeout = ClientTimeout(total=float(remote.get("timeout", 600)))
        method = str(remote.get("method", "POST")).upper()
        try:
            async with ClientSession(timeout=timeout) as session, session.request(
                method,
                remote["endpoint"],
                headers=headers,
                json=payload,
            ) as response:
                response_text = await response.text()
                if response.status >= 400:
                    return {
                        "status": "error",
                        "error": f"Remote Agent HTTP {response.status}: {response_text[:500]}",
                    }
        except Exception as exc:
            return {"status": "error", "error": f"Remote Agent request failed: {exc}"}

        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            result = {"content": response_text}
        if not isinstance(result, dict):
            result = {"content": result}
        result.setdefault("status", "success")
        result.setdefault("content", json.dumps(result, ensure_ascii=False))
        return result

    async def _health_probe(self) -> None:
        """Send a lightweight health command to the configured remote service."""
        remote = self.config.parameters.get("remote", {})
        if not isinstance(remote, dict) or not isinstance(remote.get("endpoint"), str):
            raise RuntimeError("Remote Agent requires parameters.remote.endpoint")

        headers = {"Content-Type": "application/json"}
        api_key = self._resolve_api_key(remote)
        if api_key:
            header_name = str(remote.get("auth_header", "Authorization"))
            scheme = str(remote.get("auth_scheme", "Bearer")).strip()
            headers[header_name] = f"{scheme} {api_key}".strip()
        endpoint = str(remote.get("health_endpoint") or remote["endpoint"])
        timeout = ClientTimeout(total=float(remote.get("health_timeout", 15)))
        async with ClientSession(timeout=timeout) as session, session.request(
            str(remote.get("health_method", "POST")).upper(),
            endpoint,
            headers=headers,
            json={"type": "health_check", "command": "ping", "agent_id": self.id},
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Remote Agent health HTTP {response.status}: {response_text[:500]}"
                )

    def _resolve_api_key(self, remote: dict[str, Any]) -> str | None:
        credential_id = remote.get("credential_id")
        if isinstance(credential_id, str) and credential_id:
            resolver = getattr(self.llm_gateway, "_credential_resolver", None)
            if resolver is None:
                raise RuntimeError("Credential storage is unavailable")
            return resolver(credential_id)["secret"]
        environment = remote.get("api_key_env")
        return os.environ.get(environment) if isinstance(environment, str) else None
