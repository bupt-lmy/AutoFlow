"""Deterministic narration-to-SRT subtitle generation."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from axonflow.tools.base import Tool, ToolResult


class SubtitleCreateTool(Tool):
    name = "subtitle_create"
    description = "将旁白文本按标点切分并生成与成片时长匹配的 UTF-8 SRT 字幕"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "duration_ms": {"type": "integer", "default": 12000},
            "output_name": {"type": "string"},
        },
        "required": ["text"],
    }

    def __init__(self, output_dir: str | Path = "workspace/media/subtitles") -> None:
        self.output_dir = Path(output_dir).resolve()

    async def execute(
        self,
        text: str,
        duration_ms: int = 12000,
        start_ms: int = 500,
        end_padding_ms: int = 500,
        output_name: str | None = None,
        **_kwargs: Any,
    ) -> ToolResult:
        sentences = self._sentences(text)
        if not sentences:
            return ToolResult(success=False, error="Subtitle text cannot be empty")
        if duration_ms < 1000 or start_ms + end_padding_ms >= duration_ms:
            return ToolResult(success=False, error="Invalid subtitle timing range")
        try:
            output_path = self._output_path(output_name)
            cues = self._timed_cues(sentences, start_ms, duration_ms - end_padding_ms)
            content = "\n\n".join(
                f"{index}\n{self._timestamp(start)} --> {self._timestamp(end)}\n{cue}"
                for index, (start, end, cue) in enumerate(cues, 1)
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(f"{content}\n", encoding="utf-8")
        except ValueError as exc:
            return ToolResult(success=False, error=f"Subtitle generation failed: {exc}")
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "output_path": str(output_path),
                    "media_type": "application/x-subrip",
                    "cue_count": len(cues),
                    "duration_ms": duration_ms,
                    "size_bytes": output_path.stat().st_size,
                },
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _sentences(text: str) -> list[str]:
        normalized = re.sub(r"\s+", "", text.strip())
        return [part for part in re.split(r"(?<=[。！？!?；;])", normalized) if part]

    @staticmethod
    def _timed_cues(sentences: list[str], start_ms: int, end_ms: int) -> list[tuple[int, int, str]]:
        weights = [max(len(re.sub(r"\W", "", sentence)), 1) for sentence in sentences]
        total_weight = sum(weights)
        available = end_ms - start_ms
        cues: list[tuple[int, int, str]] = []
        cursor = start_ms
        consumed = 0
        for index, (sentence, weight) in enumerate(zip(sentences, weights, strict=True)):
            consumed += weight
            cue_end = (
                end_ms
                if index == len(sentences) - 1
                else start_ms + round(available * consumed / total_weight)
            )
            cues.append((cursor, cue_end, sentence))
            cursor = cue_end
        return cues

    def _output_path(self, output_name: str | None) -> Path:
        name = output_name or f"narration-{uuid.uuid4().hex[:12]}.srt"
        candidate = Path(name)
        if candidate.name != name:
            raise ValueError("output_name must not contain directories")
        output = (self.output_dir / f"{candidate.stem}.srt").resolve()
        if output.parent != self.output_dir or output.exists():
            raise ValueError("unsafe or existing subtitle output path")
        return output

    @staticmethod
    def _timestamp(milliseconds: int) -> str:
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"
