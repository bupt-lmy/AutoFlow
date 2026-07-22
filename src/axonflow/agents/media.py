"""Deterministic media Agents used at workflow execution boundaries."""

from __future__ import annotations

import json
from typing import Any

from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message
from axonflow.json_utils import parse_json_object


def _request_payload(message: Message) -> dict[str, Any]:
    task = message.payload.get("task")
    if isinstance(task, dict):
        return task
    if isinstance(task, str):
        return parse_json_object(task)
    return dict(message.payload)


class MediaInspectorAgent(BaseAgent):
    """Probe every asset in a structured workflow request without using an LLM."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        try:
            request = _request_payload(message)
        except ValueError as exc:
            return {"status": "error", "error": f"Media request must be JSON: {exc}"}
        assets = request.get("assets")
        if not isinstance(assets, dict) or not assets:
            return {
                "status": "error",
                "error": "Media request requires a non-empty asset_id-to-path 'assets' object",
            }

        probe_results: dict[str, Any] = {}
        for asset_id, path in assets.items():
            if not isinstance(asset_id, str) or not isinstance(path, str):
                return {"status": "error", "error": "Asset IDs and paths must be strings"}
            result = await self.tool_registry.execute("media_probe", {"path": path})
            if not result.success:
                return {
                    "status": "error",
                    "error": f"Failed to probe {asset_id}: {result.error}",
                    "asset_id": asset_id,
                }
            probe_results[asset_id] = json.loads(result.output or "{}")

        return {
            "status": "success",
            "content": "Media inspection completed",
            "assets": assets,
            "output_path": request.get("output_path"),
            "target": request.get("target", {}),
            "probe_results": probe_results,
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("media_probe") is None:
            raise RuntimeError("media_probe tool is not registered")


class MediaRendererAgent(BaseAgent):
    """Execute a validated Timeline through media_render without LLM mediation."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        timeline = message.payload.get("timeline")
        assets = message.payload.get("assets")
        output_path = message.payload.get("output_path")
        if not isinstance(timeline, dict):
            return {"status": "error", "error": "Renderer requires a Timeline object"}
        if not isinstance(assets, dict):
            return {"status": "error", "error": "Renderer requires an assets object"}
        if not isinstance(output_path, str) or not output_path.strip():
            return {"status": "error", "error": "Renderer requires output_path"}

        result = await self.tool_registry.execute(
            "media_render",
            {
                "timeline": timeline,
                "assets": assets,
                "output_path": output_path,
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Media rendering failed"}
        rendered = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": "Media rendering completed",
            "render": rendered,
            "artifacts": [
                {
                    "type": "file",
                    "name": "rendered-video.mp4",
                    "uri": rendered["output_path"],
                    "media_type": "video/mp4",
                    "metadata": rendered,
                }
            ],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("media_render") is None:
            raise RuntimeError("media_render tool is not registered")


class MediaAssetManifestAgent(BaseAgent):
    """Validate fan-in generation results and publish one asset manifest."""

    required_agents = {
        "agent-minimax-image-generator": "image",
        "agent-minimax-narration-generator": "narration",
        "agent-minimax-music-generator": "music",
    }

    async def handle_message(self, message: Message) -> dict[str, Any]:
        manifest: dict[str, Any] = {}
        artifacts: list[dict[str, Any]] = []
        for agent_id, category in self.required_agents.items():
            result = message.payload.get(agent_id)
            if not isinstance(result, dict):
                return {"status": "error", "error": f"Missing generation result: {agent_id}"}
            if result.get("status") != "success":
                error = result.get("error", "unknown error")
                return {
                    "status": "error",
                    "error": f"Generation failed at {agent_id}: {error}",
                }
            generated_artifacts = result.get("artifacts")
            if not isinstance(generated_artifacts, list) or not generated_artifacts:
                return {"status": "error", "error": f"No artifact returned by {agent_id}"}
            artifact = generated_artifacts[0]
            if not isinstance(artifact, dict) or not isinstance(artifact.get("uri"), str):
                return {"status": "error", "error": f"Invalid artifact returned by {agent_id}"}
            manifest[category] = artifact
            artifacts.extend(item for item in generated_artifacts if isinstance(item, dict))

        return {
            "status": "success",
            "content": "Generated media asset manifest completed",
            "asset_manifest": manifest,
            "artifacts": artifacts,
        }

    async def _health_probe(self) -> None:
        return None


class MediaComposerAgent(BaseAgent):
    """Compose a generated asset manifest into one deterministic MP4."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        manifest = message.payload.get("asset_manifest")
        if not isinstance(manifest, dict):
            return {"status": "error", "error": "Composer requires asset_manifest"}
        try:
            image_path = manifest["image"]["uri"]
            narration_path = manifest["narration"]["uri"]
            music_path = manifest["music"]["uri"]
        except (KeyError, TypeError):
            return {"status": "error", "error": "Asset manifest is incomplete"}
        settings = self.parameters.get("composition", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "media_compose",
            {
                "image_path": image_path,
                "narration_path": narration_path,
                "music_path": music_path,
                "subtitle_path": (
                    manifest.get("subtitle", {}).get("uri")
                    if isinstance(manifest.get("subtitle"), dict)
                    else None
                ),
                "duration_seconds": settings.get("duration_seconds", 12),
                "width": settings.get("width", 1920),
                "height": settings.get("height", 1080),
                "fps": settings.get("fps", 30),
                "music_volume": settings.get("music_volume", 0.16),
                "narration_volume": settings.get("narration_volume", 1.0),
                "narration_delay_ms": settings.get("narration_delay_ms", 500),
                "timeout": settings.get("timeout", 600),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Media composition failed"}
        composed = json.loads(result.output or "{}")
        artifact = {
            "type": "file",
            "name": composed["output_path"].rsplit("/", 1)[-1],
            "uri": composed["output_path"],
            "media_type": "video/mp4",
            "metadata": composed,
        }
        return {
            "status": "success",
            "content": "Media composition completed",
            "composed_video": composed,
            "artifacts": [artifact],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("media_compose") is None:
            raise RuntimeError("media_compose tool is not registered")


class MediaQualityAgent(BaseAgent):
    """Run a deterministic technical delivery gate over a composed video."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        composed = message.payload.get("composed_video")
        if not isinstance(composed, dict) or not isinstance(composed.get("output_path"), str):
            return {"status": "error", "error": "Quality Agent requires composed_video"}
        result = await self.tool_registry.execute(
            "media_quality_check",
            {
                "path": composed["output_path"],
                "expected_width": composed.get("width", 1920),
                "expected_height": composed.get("height", 1080),
                "expected_duration_ms": composed.get("duration_ms", 12000),
                "expect_subtitles": composed.get("has_subtitles", False),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Media quality check failed"}
        report = json.loads(result.output or "{}")
        output = {
            "status": "success",
            "content": "Media quality gate passed",
            "quality_report": report,
            "composed_video": composed,
            "artifacts": message.payload.get("artifacts", []),
        }
        for field in (
            "generated_video",
            "generation_backend",
            "selected_clips",
            "refinement_report",
            "description",
            "subtitle",
        ):
            if field in message.payload:
                output[field] = message.payload[field]
        return output

    async def _health_probe(self) -> None:
        if self.tool_registry.get("media_quality_check") is None:
            raise RuntimeError("media_quality_check tool is not registered")


class SubtitleAgent(BaseAgent):
    """Create an SRT sidecar from narration preserved in the asset manifest."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        manifest = message.payload.get("asset_manifest")
        if not isinstance(manifest, dict):
            return {"status": "error", "error": "Subtitle Agent requires asset_manifest"}
        narration = manifest.get("narration")
        metadata = narration.get("metadata") if isinstance(narration, dict) else None
        text = metadata.get("text") if isinstance(metadata, dict) else None
        if not isinstance(text, str) or not text.strip():
            return {"status": "error", "error": "Narration text is missing from the manifest"}
        settings = self.parameters.get("subtitles", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "subtitle_create",
            {"text": text, "duration_ms": settings.get("duration_ms", 12000)},
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Subtitle generation failed"}
        subtitle = json.loads(result.output or "{}")
        artifact = {
            "type": "file",
            "name": subtitle["output_path"].rsplit("/", 1)[-1],
            "uri": subtitle["output_path"],
            "media_type": subtitle["media_type"],
            "metadata": subtitle,
        }
        updated_manifest = dict(manifest)
        updated_manifest["subtitle"] = artifact
        return {
            "status": "success",
            "content": "Subtitle generation completed",
            "asset_manifest": updated_manifest,
            "subtitle": subtitle,
            "artifacts": [*message.payload.get("artifacts", []), artifact],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("subtitle_create") is None:
            raise RuntimeError("subtitle_create tool is not registered")


class MediaAssetRegisterAgent(BaseAgent):
    """Persist a quality-approved composition in the platform asset catalog."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        report = message.payload.get("quality_report")
        composed = message.payload.get("composed_video")
        if not isinstance(report, dict) or report.get("verdict") != "passed":
            return {"status": "error", "error": "Only quality-approved videos can be registered"}
        if not isinstance(composed, dict) or not isinstance(composed.get("output_path"), str):
            return {"status": "error", "error": "Registrar requires composed_video"}
        result = await self.tool_registry.execute(
            "media_register", {"path": composed["output_path"]}
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Asset registration failed"}
        asset = json.loads(result.output or "{}")
        output = {
            "status": "success",
            "content": "Composed video registered as a media asset",
            "registered_asset": asset,
            "quality_report": report,
            "composed_video": composed,
            "artifacts": message.payload.get("artifacts", []),
        }
        for field in (
            "generated_video",
            "generation_backend",
            "selected_clips",
            "refinement_report",
            "description",
            "subtitle",
        ):
            if field in message.payload:
                output[field] = message.payload[field]
        return output

    async def _health_probe(self) -> None:
        if self.tool_registry.get("media_register") is None:
            raise RuntimeError("media_register tool is not registered")
