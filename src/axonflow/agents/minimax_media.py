"""Deterministic workflow Agents backed by MiniMax media-generation tools."""

from __future__ import annotations

import json
import uuid
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


class MiniMaxImageAgent(BaseAgent):
    """Turn a workflow image brief into a local MiniMax-generated artifact."""

    tool_name = "minimax_image_generate"

    async def handle_message(self, message: Message) -> dict[str, Any]:
        try:
            request = _request_payload(message)
        except ValueError as exc:
            return {"status": "error", "error": f"Image request must be JSON: {exc}"}

        prompt = request.get("prompt") or request.get("image_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "error", "error": "Image request requires a non-empty prompt"}

        settings = self.parameters.get("minimax", {})
        if not isinstance(settings, dict):
            return {"status": "error", "error": "parameters.minimax must be an object"}
        result = await self.tool_registry.execute(
            self.tool_name,
            {
                "prompt": prompt,
                "aspect_ratio": request.get("aspect_ratio", settings.get("aspect_ratio", "16:9")),
                "output_name": request.get("output_name"),
                "model": settings.get("model", "image-01"),
                "credential_id": settings.get("credential_id"),
                "api_key_env": settings.get("api_key_env", "MINIMAX_API_KEY"),
                "timeout": settings.get("timeout", 180),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Image generation failed"}

        generated = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": "MiniMax image generation completed",
            "generated_image": generated,
            "artifacts": [
                {
                    "type": "file",
                    "name": generated["output_path"].rsplit("/", 1)[-1],
                    "uri": generated["output_path"],
                    "media_type": generated["media_type"],
                    "metadata": generated,
                }
            ],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get(self.tool_name) is None:
            raise RuntimeError(f"{self.tool_name} tool is not registered")


class MiniMaxNarrationAgent(BaseAgent):
    """Turn narration text into a local MiniMax speech artifact."""

    tool_name = "minimax_speech_generate"

    async def handle_message(self, message: Message) -> dict[str, Any]:
        try:
            request = _request_payload(message)
        except ValueError as exc:
            return {"status": "error", "error": f"Narration request must be JSON: {exc}"}

        text = request.get("text") or request.get("narration") or request.get("script")
        if not isinstance(text, str) or not text.strip():
            return {"status": "error", "error": "Narration request requires non-empty text"}

        settings = self.parameters.get("minimax", {})
        if not isinstance(settings, dict):
            return {"status": "error", "error": "parameters.minimax must be an object"}
        result = await self.tool_registry.execute(
            self.tool_name,
            {
                "text": text,
                "voice_id": request.get("voice_id", settings.get("voice_id", "male-qn-jingying")),
                "speed": request.get("speed", settings.get("speed", 1.0)),
                "volume": request.get("volume", settings.get("volume", 1.0)),
                "pitch": request.get("pitch", settings.get("pitch", 0)),
                "output_name": request.get("output_name"),
                "model": settings.get("model", "speech-2.8-hd"),
                "credential_id": settings.get("credential_id"),
                "api_key_env": settings.get("api_key_env", "MINIMAX_API_KEY"),
                "timeout": settings.get("timeout", 180),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Narration generation failed"}

        generated = json.loads(result.output or "{}")
        generated["text"] = text
        return {
            "status": "success",
            "content": "MiniMax narration generation completed",
            "generated_narration": generated,
            "artifacts": [
                {
                    "type": "file",
                    "name": generated["output_path"].rsplit("/", 1)[-1],
                    "uri": generated["output_path"],
                    "media_type": generated["media_type"],
                    "metadata": generated,
                }
            ],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get(self.tool_name) is None:
            raise RuntimeError(f"{self.tool_name} tool is not registered")


class MiniMaxMusicAgent(BaseAgent):
    """Turn a film music brief into a local instrumental audio artifact."""

    tool_name = "minimax_music_generate"

    async def handle_message(self, message: Message) -> dict[str, Any]:
        try:
            request = _request_payload(message)
        except ValueError as exc:
            return {"status": "error", "error": f"Music request must be JSON: {exc}"}

        prompt = request.get("prompt") or request.get("music_prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "error", "error": "Music request requires a non-empty prompt"}

        settings = self.parameters.get("minimax", {})
        if not isinstance(settings, dict):
            return {"status": "error", "error": "parameters.minimax must be an object"}
        result = await self.tool_registry.execute(
            self.tool_name,
            {
                "prompt": prompt,
                "output_name": request.get("output_name"),
                "model": settings.get("model", "music-2.6"),
                "credential_id": settings.get("credential_id"),
                "api_key_env": settings.get("api_key_env", "MINIMAX_API_KEY"),
                "timeout": settings.get("timeout", 300),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Music generation failed"}

        generated = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": "MiniMax music generation completed",
            "generated_music": generated,
            "artifacts": [
                {
                    "type": "file",
                    "name": generated["output_path"].rsplit("/", 1)[-1],
                    "uri": generated["output_path"],
                    "media_type": generated["media_type"],
                    "metadata": generated,
                }
            ],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get(self.tool_name) is None:
            raise RuntimeError(f"{self.tool_name} tool is not registered")


class MiniMaxVideoAgent(BaseAgent):
    """Turn a cinematic prompt into a real MiniMax Hailuo motion clip."""

    tool_name = "minimax_video_generate"

    async def handle_message(self, message: Message) -> dict[str, Any]:
        try:
            request = _request_payload(message)
        except ValueError as exc:
            return {"status": "error", "error": f"Video request must be JSON: {exc}"}
        prompt = request.get("video_prompt") or request.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "error", "error": "Video request requires video_prompt"}
        settings = self.parameters.get("minimax", {})
        if not isinstance(settings, dict):
            return {"status": "error", "error": "parameters.minimax must be an object"}
        result = await self.tool_registry.execute(
            self.tool_name,
            {
                "prompt": prompt,
                "duration": request.get("duration", settings.get("duration", 6)),
                "resolution": request.get("resolution", settings.get("resolution", "768P")),
                "model": settings.get("model", "MiniMax-Hailuo-2.3"),
                "credential_id": settings.get("credential_id"),
                "api_key_env": settings.get("api_key_env", "MINIMAX_API_KEY"),
                "timeout": settings.get("timeout", 1200),
                "poll_interval": settings.get("poll_interval", 5),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Video generation failed"}
        generated = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": "MiniMax Hailuo video generation completed",
            "generated_video": generated,
            "video_prompt": prompt,
            "project_title": request.get("project_title"),
            "requires_disclosure": True,
            "artifacts": [
                {
                    "type": "file",
                    "name": generated["output_path"].rsplit("/", 1)[-1],
                    "uri": generated["output_path"],
                    "media_type": "video/mp4",
                    "metadata": generated,
                }
            ],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get(self.tool_name) is None:
            raise RuntimeError(f"{self.tool_name} tool is not registered")


class MiniMaxStoryboardAgent(BaseAgent):
    """Generate a sequence of related keyframes with the image entitlement."""

    tool_name = "minimax_image_generate"

    async def handle_message(self, message: Message) -> dict[str, Any]:
        try:
            request = _request_payload(message)
        except ValueError as exc:
            return {"status": "error", "error": f"Storyboard request must be JSON: {exc}"}
        prompts = request.get("shot_prompts")
        if not isinstance(prompts, list) or not 2 <= len(prompts) <= 8:
            return {"status": "error", "error": "Storyboard requires 2 to 8 shot_prompts"}
        if any(not isinstance(prompt, str) or not prompt.strip() for prompt in prompts):
            return {"status": "error", "error": "Every shot prompt must be non-empty text"}
        settings = self.parameters.get("minimax", {})
        if not isinstance(settings, dict):
            return {"status": "error", "error": "parameters.minimax must be an object"}

        generated_images: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        for index, prompt in enumerate(prompts, start=1):
            result = await self.tool_registry.execute(
                self.tool_name,
                {
                    "prompt": prompt,
                    "aspect_ratio": settings.get("aspect_ratio", "16:9"),
                    "output_name": f"storyboard-{index}-{uuid.uuid4().hex[:10]}.jpg",
                    "model": settings.get("model", "image-01"),
                    "credential_id": settings.get("credential_id"),
                    "api_key_env": settings.get("api_key_env", "MINIMAX_API_KEY"),
                    "timeout": settings.get("timeout", 180),
                },
            )
            if not result.success:
                return {
                    "status": "error",
                    "error": f"Storyboard shot {index} generation failed: {result.error}",
                    "completed_shots": len(generated_images),
                }
            generated = json.loads(result.output or "{}")
            generated["shot_index"] = index
            generated["prompt"] = prompt
            generated_images.append(generated)
            artifacts.append(
                {
                    "type": "file",
                    "name": generated["output_path"].rsplit("/", 1)[-1],
                    "uri": generated["output_path"],
                    "media_type": generated["media_type"],
                    "metadata": generated,
                }
            )
        transition_seconds = float(settings.get("transition_seconds", 0.4))
        requested_duration = request.get("duration")
        if isinstance(requested_duration, (int, float)) and requested_duration > 0:
            shot_duration_seconds = (
                float(requested_duration) + (len(prompts) - 1) * transition_seconds
            ) / len(prompts)
        else:
            shot_duration_seconds = float(request.get("shot_duration_seconds", 2.0))
        if not 0.75 <= shot_duration_seconds <= 10:
            return {
                "status": "error",
                "error": "Requested duration cannot be represented by 2 to 8 storyboard shots",
            }
        return {
            "status": "success",
            "content": f"Generated {len(generated_images)} MiniMax storyboard keyframes",
            "storyboard_images": generated_images,
            "shot_duration_seconds": shot_duration_seconds,
            "project_title": request.get("project_title"),
            "requires_disclosure": True,
            "generation_backend": "storyboard",
            "artifacts": artifacts,
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get(self.tool_name) is None:
            raise RuntimeError(f"{self.tool_name} tool is not registered")
