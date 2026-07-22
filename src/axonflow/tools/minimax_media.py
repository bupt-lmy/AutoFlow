"""MiniMax media generation tools with encrypted-credential support."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from axonflow.tools.base import Tool, ToolResult

CredentialResolver = Callable[[str], dict[str, str]]


class _MiniMaxMediaTool(Tool):
    """Shared authenticated transport and safe artifact persistence."""

    artifact_prefix = "minimax-media"

    def __init__(
        self,
        output_dir: str | Path = "workspace/media/generated",
        credential_resolver: CredentialResolver | None = None,
        base_url: str = "https://api.minimaxi.com/v1",
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.credential_resolver = credential_resolver
        self.base_url = base_url.rstrip("/")

    def _resolve_api_key(self, credential_id: str | None, api_key_env: str) -> str:
        if credential_id:
            if self.credential_resolver is None:
                raise RuntimeError("Encrypted credential storage is unavailable")
            resolved = self.credential_resolver(credential_id)
            api_key = resolved.get("secret")
        else:
            api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError("MiniMax API key is not configured")
        return api_key

    async def _post_json(
        self,
        endpoint: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.post(f"{self.base_url}{endpoint}", headers=headers, json=payload) as response,
        ):
            response_text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {response_text[:500]}")
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MiniMax returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("MiniMax returned an invalid JSON response")
        return parsed

    async def _get_json(
        self,
        endpoint: str,
        api_key: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        headers = {"Authorization": f"Bearer {api_key}"}
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.get(f"{self.base_url}{endpoint}", headers=headers, params=params) as response,
        ):
            response_text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {response_text[:500]}")
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MiniMax returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("MiniMax returned an invalid JSON response")
        return parsed

    @staticmethod
    async def _download_bytes(url: str, timeout: float, max_bytes: int) -> bytes:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        chunks: list[bytes] = []
        size = 0
        async with (
            aiohttp.ClientSession(timeout=client_timeout) as session,
            session.get(url) as response,
        ):
            if response.status >= 400:
                detail = (await response.text())[:500]
                raise RuntimeError(f"Download HTTP {response.status}: {detail}")
            async for chunk in response.content.iter_chunked(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError("Generated video exceeds the maximum artifact size")
                chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _validate_response(response: dict[str, Any]) -> None:
        base_response = response.get("base_resp")
        if not isinstance(base_response, dict):
            return
        status_code = base_response.get("status_code", 0)
        if str(status_code) not in {"0", "1000"}:
            message = base_response.get("status_msg") or "unknown MiniMax error"
            raise RuntimeError(f"API status {status_code}: {message}")

    def _output_path(self, output_name: str | None, suffix: str) -> Path:
        if output_name:
            candidate = Path(output_name)
            if candidate.name != output_name or output_name in {".", ".."}:
                raise ValueError("output_name must be a filename without directories")
            stem = candidate.stem.strip()
            if not stem:
                raise ValueError("output_name must contain a filename")
        else:
            stem = f"{self.artifact_prefix}-{uuid.uuid4().hex[:12]}"
        output_path = (self.output_dir / f"{stem}{suffix}").resolve()
        if output_path.parent != self.output_dir:
            raise ValueError("Output path escapes the generated media directory")
        if output_path.exists():
            raise FileExistsError(f"Output already exists: {output_path.name}")
        return output_path

    @staticmethod
    def _write_bytes(output_path: Path, data: bytes) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(output_path)
        finally:
            temporary.unlink(missing_ok=True)


class MiniMaxImageGenerateTool(_MiniMaxMediaTool):
    """Generate a local image through MiniMax's image generation API."""

    name = "minimax_image_generate"
    description = "使用 MiniMax image-01 生成影视画面，并保存为本地图片文件"
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "图片生成提示词"},
            "aspect_ratio": {
                "type": "string",
                "enum": ["1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"],
                "default": "16:9",
            },
            "output_name": {
                "type": "string",
                "description": "可选的输出文件名；必须是不含目录的安全文件名",
            },
        },
        "required": ["prompt"],
    }

    artifact_prefix = "minimax-image"

    async def execute(
        self,
        prompt: str,
        aspect_ratio: str = "16:9",
        output_name: str | None = None,
        model: str = "image-01",
        credential_id: str | None = None,
        api_key_env: str = "MINIMAX_API_KEY",
        timeout: float = 180,
        **_kwargs: Any,
    ) -> ToolResult:
        prompt = prompt.strip()
        if not prompt:
            return ToolResult(success=False, error="Image prompt cannot be empty")
        if aspect_ratio not in {"1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"}:
            return ToolResult(success=False, error=f"Unsupported aspect ratio: {aspect_ratio}")

        try:
            api_key = self._resolve_api_key(credential_id, api_key_env)
            response = await self._post_json(
                "/image_generation",
                api_key,
                {
                    "model": model,
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "response_format": "base64",
                },
                timeout,
            )
            self._validate_response(response)
            image_data = self._extract_image(response)
            suffix, media_type = self._detect_image_type(image_data)
            output_path = self._output_path(output_name, suffix)
            self._write_bytes(output_path, image_data)
        except Exception as exc:
            return ToolResult(success=False, error=f"MiniMax image generation failed: {exc}")

        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output_path),
                    "media_type": media_type,
                    "model": model,
                    "aspect_ratio": aspect_ratio,
                    "size_bytes": len(image_data),
                },
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _extract_image(response: dict[str, Any]) -> bytes:
        data = response.get("data")
        values = data.get("image_base64") if isinstance(data, dict) else None
        encoded = values[0] if isinstance(values, list) and values else values
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("MiniMax response did not contain image_base64")
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise RuntimeError("MiniMax returned invalid base64 image data") from exc

    @staticmethod
    def _detect_image_type(data: bytes) -> tuple[str, str]:
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg", "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png", "image/png"
        raise RuntimeError("MiniMax returned an unsupported image format")


class MiniMaxSpeechGenerateTool(_MiniMaxMediaTool):
    """Synthesize a local narration track through MiniMax speech generation."""

    name = "minimax_speech_generate"
    description = "使用 MiniMax speech-2.8-hd 将中文旁白合成为本地 MP3"
    artifact_prefix = "minimax-speech"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "需要合成的旁白文本"},
            "voice_id": {
                "type": "string",
                "description": "MiniMax 系统音色 ID",
                "default": "male-qn-jingying",
            },
            "speed": {"type": "number", "minimum": 0.5, "maximum": 2, "default": 1},
            "output_name": {"type": "string", "description": "可选的安全输出文件名"},
        },
        "required": ["text"],
    }

    async def execute(
        self,
        text: str,
        voice_id: str = "male-qn-jingying",
        speed: float = 1.0,
        volume: float = 1.0,
        pitch: int = 0,
        output_name: str | None = None,
        model: str = "speech-2.8-hd",
        credential_id: str | None = None,
        api_key_env: str = "MINIMAX_API_KEY",
        timeout: float = 180,
        **_kwargs: Any,
    ) -> ToolResult:
        text = text.strip()
        if not text:
            return ToolResult(success=False, error="Narration text cannot be empty")
        if not 0.5 <= speed <= 2:
            return ToolResult(success=False, error="Speech speed must be between 0.5 and 2")

        try:
            api_key = self._resolve_api_key(credential_id, api_key_env)
            response = await self._post_json(
                "/t2a_v2",
                api_key,
                {
                    "model": model,
                    "text": text,
                    "stream": False,
                    "language_boost": "Chinese",
                    "voice_setting": {
                        "voice_id": voice_id,
                        "speed": speed,
                        "vol": volume,
                        "pitch": pitch,
                    },
                    "audio_setting": {
                        "sample_rate": 32000,
                        "bitrate": 128000,
                        "format": "mp3",
                        "channel": 1,
                    },
                },
                timeout,
            )
            self._validate_response(response)
            audio_data, extra_info = self._extract_audio(response)
            output_path = self._output_path(output_name, ".mp3")
            self._write_bytes(output_path, audio_data)
        except Exception as exc:
            return ToolResult(success=False, error=f"MiniMax speech generation failed: {exc}")

        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output_path),
                    "media_type": "audio/mpeg",
                    "model": model,
                    "voice_id": voice_id,
                    "size_bytes": len(audio_data),
                    "duration_ms": extra_info.get("audio_length"),
                    "sample_rate": extra_info.get("audio_sample_rate", 32000),
                },
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _extract_audio(response: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        data = response.get("data")
        encoded = data.get("audio") if isinstance(data, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("MiniMax response did not contain audio")
        try:
            audio = bytes.fromhex(encoded)
        except ValueError as exc:
            raise RuntimeError("MiniMax returned invalid hexadecimal audio data") from exc
        extra_info = response.get("extra_info")
        return audio, extra_info if isinstance(extra_info, dict) else {}


class MiniMaxMusicGenerateTool(_MiniMaxMediaTool):
    """Generate a local instrumental music track through MiniMax."""

    name = "minimax_music_generate"
    description = "使用 MiniMax music-2.6 生成无人声影视背景音乐，并保存为本地 MP3"
    artifact_prefix = "minimax-music"
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "配乐的风格、情绪和场景描述"},
            "output_name": {"type": "string", "description": "可选的安全输出文件名"},
        },
        "required": ["prompt"],
    }

    async def execute(
        self,
        prompt: str,
        output_name: str | None = None,
        model: str = "music-2.6",
        credential_id: str | None = None,
        api_key_env: str = "MINIMAX_API_KEY",
        timeout: float = 300,
        **_kwargs: Any,
    ) -> ToolResult:
        prompt = prompt.strip()
        if not prompt:
            return ToolResult(success=False, error="Music prompt cannot be empty")
        if len(prompt) > 2000:
            return ToolResult(success=False, error="Music prompt cannot exceed 2000 characters")

        try:
            api_key = self._resolve_api_key(credential_id, api_key_env)
            response = await self._post_json(
                "/music_generation",
                api_key,
                {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "output_format": "hex",
                    "is_instrumental": True,
                    "aigc_watermark": False,
                    "audio_setting": {
                        "sample_rate": 44100,
                        "bitrate": 256000,
                        "format": "mp3",
                    },
                },
                timeout,
            )
            self._validate_response(response)
            audio_data, extra_info = self._extract_audio(response)
            output_path = self._output_path(output_name, ".mp3")
            self._write_bytes(output_path, audio_data)
        except Exception as exc:
            return ToolResult(success=False, error=f"MiniMax music generation failed: {exc}")

        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output_path),
                    "media_type": "audio/mpeg",
                    "model": model,
                    "instrumental": True,
                    "size_bytes": len(audio_data),
                    "duration_ms": extra_info.get("music_duration"),
                    "sample_rate": extra_info.get("music_sample_rate", 44100),
                    "channels": extra_info.get("music_channel"),
                    "bitrate": extra_info.get("bitrate", 256000),
                },
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _extract_audio(response: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
        data = response.get("data")
        encoded = data.get("audio") if isinstance(data, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("MiniMax response did not contain audio")
        try:
            audio = bytes.fromhex(encoded)
        except ValueError as exc:
            raise RuntimeError("MiniMax returned invalid hexadecimal audio data") from exc
        extra_info = response.get("extra_info")
        return audio, extra_info if isinstance(extra_info, dict) else {}


class MiniMaxVideoGenerateTool(_MiniMaxMediaTool):
    """Generate and download a real motion clip through MiniMax Hailuo."""

    name = "minimax_video_generate"
    description = "使用 MiniMax Hailuo 文生视频异步接口生成真实动态 MP4，并轮询下载到本地"
    artifact_prefix = "minimax-video"
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "视频动态、人物动作和运镜提示词"},
            "duration": {"type": "integer", "enum": [6, 10], "default": 6},
            "resolution": {
                "type": "string",
                "enum": ["768P", "1080P"],
                "default": "768P",
            },
            "output_name": {"type": "string"},
        },
        "required": ["prompt"],
    }

    async def execute(
        self,
        prompt: str,
        duration: int = 6,
        resolution: str = "768P",
        output_name: str | None = None,
        model: str = "MiniMax-Hailuo-2.3",
        credential_id: str | None = None,
        api_key_env: str = "MINIMAX_API_KEY",
        timeout: float = 1200,
        poll_interval: float = 5,
        **_kwargs: Any,
    ) -> ToolResult:
        prompt = prompt.strip()
        if not prompt:
            return ToolResult(success=False, error="Video prompt cannot be empty")
        if len(prompt) > 2000:
            return ToolResult(success=False, error="Video prompt cannot exceed 2000 characters")
        if duration not in {6, 10}:
            return ToolResult(success=False, error="Video duration must be 6 or 10 seconds")
        if resolution not in {"768P", "1080P"}:
            return ToolResult(success=False, error="Video resolution must be 768P or 1080P")
        if duration == 10 and resolution == "1080P":
            return ToolResult(success=False, error="10-second video supports 768P only")
        if poll_interval <= 0:
            return ToolResult(success=False, error="poll_interval must be positive")

        try:
            api_key = self._resolve_api_key(credential_id, api_key_env)
            created = await self._post_json(
                "/video_generation",
                api_key,
                {
                    "model": model,
                    "prompt": prompt,
                    "duration": duration,
                    "resolution": resolution,
                    "prompt_optimizer": True,
                    "fast_pretreatment": True,
                    "aigc_watermark": True,
                },
                min(timeout, 120),
            )
            self._validate_response(created)
            task_id = created.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError("MiniMax response did not contain task_id")
            deadline = time.monotonic() + timeout
            task: dict[str, Any] = {}
            while time.monotonic() < deadline:
                task = await self._get_json(
                    "/query/video_generation",
                    api_key,
                    {"task_id": task_id},
                    min(60, max(deadline - time.monotonic(), 1)),
                )
                self._validate_response(task)
                status = str(task.get("status", "")).lower()
                if status == "success":
                    break
                if status in {"fail", "failed"}:
                    detail = task.get("base_resp", {}).get("status_msg", "generation failed")
                    raise RuntimeError(f"Video task failed: {detail}")
                await asyncio.sleep(min(poll_interval, max(deadline - time.monotonic(), 0)))
            else:
                raise TimeoutError("Video generation task timed out")
            file_id = task.get("file_id")
            if file_id in {None, ""}:
                raise RuntimeError("Successful video task did not contain file_id")
            retrieved = await self._get_json(
                "/files/retrieve",
                api_key,
                {"file_id": file_id},
                min(timeout, 120),
            )
            self._validate_response(retrieved)
            file_info = retrieved.get("file")
            download_url = file_info.get("download_url") if isinstance(file_info, dict) else None
            if not isinstance(download_url, str) or not download_url.startswith("http"):
                raise RuntimeError("MiniMax file response did not contain download_url")
            video_data = await self._download_bytes(
                download_url, min(timeout, 300), max_bytes=200 * 1024 * 1024
            )
            if len(video_data) < 12 or b"ftyp" not in video_data[:32]:
                raise RuntimeError("MiniMax returned an invalid MP4 file")
            output_path = self._output_path(output_name, ".mp4")
            self._write_bytes(output_path, video_data)
        except Exception as exc:
            return ToolResult(success=False, error=f"MiniMax video generation failed: {exc}")

        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output_path),
                    "media_type": "video/mp4",
                    "model": model,
                    "duration_seconds": duration,
                    "resolution": resolution,
                    "task_id": task_id,
                    "file_id": str(file_id),
                    "size_bytes": len(video_data),
                    "aigc_watermark": True,
                },
                ensure_ascii=False,
            ),
        )
