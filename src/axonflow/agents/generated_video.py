"""Agents for resource-assisted, clearly disclosed AI video generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message
from axonflow.json_utils import parse_json_object


def _generation_request(message: Message) -> tuple[str, dict[str, Any]]:
    task = message.payload.get("task")
    if isinstance(task, dict):
        request = task
    elif isinstance(task, str):
        try:
            request = parse_json_object(task)
        except ValueError:
            request = {"description": task}
    else:
        request = dict(message.payload)
    description = request.get("description") or request.get("prompt")
    return str(description or "").strip(), request


class GenerationResourceSearchAgent(BaseAgent):
    """Collect optional reference links while never blocking pure generation."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        description, request = _generation_request(message)
        if not description:
            return {"status": "error", "error": "Generation request requires description"}
        candidates: list[dict[str, Any]] = []
        warning: str | None = None
        if request.get("collect_resources", True):
            result = await self.tool_registry.execute(
                "web_search",
                {
                    "query": (f"{description} visual reference Wikimedia Commons Creative Commons"),
                    "max_results": 5,
                },
            )
            if result.success:
                values = json.loads(result.output or "[]")
                if isinstance(values, list):
                    candidates = [item for item in values if isinstance(item, dict)]
            else:
                warning = result.error or "Reference search unavailable"
        return {
            "status": "success",
            "content": f"Collected {len(candidates)} optional visual references",
            "task": description,
            "description": description,
            "resource_candidates": candidates,
            "resource_warning": warning,
            "requested_duration": request.get("duration", 6),
            "requested_resolution": request.get("resolution", "768P"),
            "requested_backend": request.get("generation_backend", "storyboard"),
            "requires_disclosure": True,
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("web_search") is None:
            raise RuntimeError("web_search tool is not registered")


class GeneratedVideoDisclosureAgent(BaseAgent):
    """Normalize generated motion and permanently label it as fictional AI content."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        generated = message.payload.get("generated_video")
        if not isinstance(generated, dict) or not isinstance(generated.get("output_path"), str):
            return {"status": "error", "error": "Disclosure Agent requires generated_video"}
        settings = self.parameters.get("delivery", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "generated_video_finalize",
            {
                "path": generated["output_path"],
                "width": settings.get("width", 1920),
                "height": settings.get("height", 1080),
                "fps": settings.get("fps", 30),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Disclosure rendering failed"}
        final_video = json.loads(result.output or "{}")
        artifact = {
            "type": "file",
            "name": Path(final_video["output_path"]).name,
            "uri": final_video["output_path"],
            "media_type": "video/mp4",
            "metadata": final_video,
        }
        return {
            "status": "success",
            "content": "AI disclosure permanently burned into generated video",
            "composed_video": final_video,
            "generated_video": generated,
            "generation_backend": generated.get("generation_backend", "hailuo"),
            "video_prompt": message.payload.get("video_prompt"),
            "artifacts": [*message.payload.get("artifacts", []), artifact],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("generated_video_finalize") is None:
            raise RuntimeError("generated_video_finalize tool is not registered")


class StoryboardMotionRendererAgent(BaseAgent):
    """Convert generated storyboard images into a moving clip."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        images = message.payload.get("storyboard_images")
        if not isinstance(images, list):
            return {"status": "error", "error": "Motion renderer requires storyboard_images"}
        image_paths = [
            image.get("output_path")
            for image in images
            if isinstance(image, dict) and isinstance(image.get("output_path"), str)
        ]
        if len(image_paths) != len(images):
            return {"status": "error", "error": "Every storyboard image needs output_path"}
        settings = self.parameters.get("render", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "storyboard_motion_render",
            {
                "image_paths": image_paths,
                "shot_duration_seconds": message.payload.get("shot_duration_seconds", 2.0),
                "transition_seconds": settings.get("transition_seconds", 0.4),
                "width": settings.get("width", 1920),
                "height": settings.get("height", 1080),
                "fps": settings.get("fps", 30),
                "timeout": settings.get("timeout", 1800),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Storyboard render failed"}
        generated = json.loads(result.output or "{}")
        artifact = {
            "type": "file",
            "name": Path(generated["output_path"]).name,
            "uri": generated["output_path"],
            "media_type": "video/mp4",
            "metadata": generated,
        }
        return {
            "status": "success",
            "content": "Storyboard keyframes rendered with camera motion and transitions",
            "generated_video": generated,
            "project_title": message.payload.get("project_title"),
            "requires_disclosure": True,
            "generation_backend": "storyboard",
            "artifacts": [*message.payload.get("artifacts", []), artifact],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("storyboard_motion_render") is None:
            raise RuntimeError("storyboard_motion_render tool is not registered")
