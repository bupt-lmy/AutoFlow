"""Agent implementation that turns a model JSON verdict into workflow status."""

from __future__ import annotations

from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message
from axonflow.json_utils import parse_json_object


class StructuredResultAgent(BaseAgent):
    """Promote configured JSON fields and enforce a business-level verdict."""

    async def handle_message(self, message: Message) -> dict:
        result = await super().handle_message(message)
        if result.get("status") != "success":
            return result

        settings = self.config.parameters.get("structured_result", {})
        if not isinstance(settings, dict):
            settings = {}
        strict = bool(settings.get("strict", True))

        try:
            structured = parse_json_object(str(result.get("content", "")))
        except ValueError as exc:
            if not strict:
                return result
            result.update(status="error", error=str(exc))
            return result

        result["structured"] = structured
        for field in settings.get("promote_fields", []):
            if isinstance(field, str) and field in structured:
                result[field] = structured[field]

        verdict_field = str(settings.get("verdict_field", "verdict"))
        verdict = str(structured.get(verdict_field, "")).strip().lower()
        success_values = {
            str(value).strip().lower() for value in settings.get("success_values", ["passed"])
        }
        error_values = {
            str(value).strip().lower()
            for value in settings.get("error_values", ["failed", "blocked"])
        }

        if verdict in error_values:
            result["status"] = "error"
            result["error"] = str(
                structured.get("summary")
                or structured.get("problem")
                or f"Business verdict: {verdict}"
            )
        elif success_values and verdict not in success_values:
            result["status"] = "error"
            result["error"] = f"Unexpected business verdict: {verdict or '<missing>'}"
        return result
