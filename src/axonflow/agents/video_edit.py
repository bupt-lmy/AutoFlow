"""Agents for semantic source-video highlight editing."""

from __future__ import annotations

import base64
import json
import os
import re
import statistics
from pathlib import Path
from typing import Any

from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message
from axonflow.json_utils import parse_json_object
from axonflow.llm.gateway import LLMTraceContext


def _request(message: Message) -> dict[str, Any]:
    task = message.payload.get("task")
    if isinstance(task, dict):
        return task
    if isinstance(task, str):
        try:
            return parse_json_object(task)
        except ValueError:
            return {"source": task}
    return dict(message.payload)


def _srt_cues(path: str) -> list[dict[str, Any]]:
    """Read the small subset of SRT needed for scene/transcript alignment."""

    content = Path(path).read_text(encoding="utf-8-sig")
    pattern = re.compile(
        r"(?ms)^\s*\d+\s*\n"
        r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
        r"(?P<end>\d{2}:\d{2}:\d{2},\d{3})\s*\n"
        r"(?P<text>.*?)(?=\n\s*\n|\Z)"
    )

    def milliseconds(timestamp: str) -> int:
        hours, minutes, rest = timestamp.split(":")
        seconds, millis = rest.split(",")
        return int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1000 + int(millis)

    return [
        {
            "start_ms": milliseconds(match.group("start")),
            "end_ms": milliseconds(match.group("end")),
            "text": " ".join(match.group("text").split()),
        }
        for match in pattern.finditer(content)
    ]


def _transcription_can_degrade(error: str | None) -> bool:
    """Return whether visual-only editing may continue without local Whisper."""
    detail = (error or "").lower()
    return any(
        marker in detail
        for marker in (
            "no transcribable speech",
            "whisper-cli is not installed",
            "whisper model not found",
        )
    )


def _optional_model_configured(agent: BaseAgent) -> bool:
    """Whether an optional semantic model has credentials worth probing/calling."""
    model = agent.config.model
    if model.credential_id:
        return True
    return not model.api_key_env or bool(os.getenv(model.api_key_env))


class VideoIngestAgent(BaseAgent):
    async def handle_message(self, message: Message) -> dict[str, Any]:
        request = _request(message)
        source = request.get("source") or request.get("url") or request.get("path")
        description = request.get("description") or request.get("prompt")
        if not isinstance(source, str) or not source.strip():
            return {"status": "error", "error": "Video edit request requires source"}
        if not isinstance(description, str) or not description.strip():
            return {"status": "error", "error": "Video edit request requires description"}
        result = await self.tool_registry.execute("video_ingest", {"source": source})
        if not result.success:
            return {"status": "error", "error": result.error or "Video ingest failed"}
        ingested = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": "Video source imported",
            **ingested,
            "description": description,
            "target_duration_seconds": request.get("target_duration_seconds", 30),
            "hard_subtitles": bool(request.get("hard_subtitles", True)),
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("video_ingest") is None:
            raise RuntimeError("video_ingest tool is not registered")


class VideoSceneAnalysisAgent(BaseAgent):
    async def handle_message(self, message: Message) -> dict[str, Any]:
        source = message.payload.get("source_path")
        if not isinstance(source, str):
            return {"status": "error", "error": "Scene analysis requires source_path"}
        settings = self.parameters.get("scene_detection", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "video_scene_detect",
            {
                "path": source,
                "threshold": settings.get("threshold", 8),
                "min_scene_ms": settings.get("min_scene_ms", 500),
                "max_scenes": settings.get("max_scenes", 60),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Scene detection failed"}
        analysis = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": f"Detected {len(analysis['scenes'])} scenes",
            **analysis,
            "description": message.payload.get("description"),
            "target_duration_seconds": message.payload.get("target_duration_seconds", 30),
            "hard_subtitles": message.payload.get("hard_subtitles", True),
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("video_scene_detect") is None:
            raise RuntimeError("video_scene_detect tool is not registered")


class VideoSceneFeatureAgent(BaseAgent):
    """Enrich every detected scene with multi-frame and deterministic activity features."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        source = message.payload.get("source_path")
        scenes = message.payload.get("scenes")
        if not isinstance(source, str) or not isinstance(scenes, list):
            return {"status": "error", "error": "Scene features require source_path and scenes"}
        settings = self.parameters.get("features", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "video_scene_features",
            {
                "source_path": source,
                "scenes": scenes,
                "samples_per_scene": settings.get("samples_per_scene", 5),
                "analysis_fps": settings.get("analysis_fps", 4),
                "timeout": settings.get("timeout", 3600),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Scene feature analysis failed"}
        analysis = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": f"Analyzed multi-frame features for {len(analysis['scenes'])} scenes",
            **analysis,
            "description": message.payload.get("description"),
            "target_duration_seconds": message.payload.get("target_duration_seconds", 30),
            "hard_subtitles": message.payload.get("hard_subtitles", True),
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("video_scene_features") is None:
            raise RuntimeError("video_scene_features tool is not registered")


class SourceTranscriptAgent(BaseAgent):
    """Transcribe source dialogue before selection so spoken topics are searchable."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        source = message.payload.get("source_path")
        if not isinstance(source, str):
            return {"status": "error", "error": "Source transcription requires source_path"}
        settings = self.parameters.get("transcription", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "video_transcribe",
            {
                "path": source,
                "language": settings.get("language", "auto"),
                "timeout": settings.get("timeout", 7200),
            },
        )
        transcript: dict[str, Any] | None = None
        cues: list[dict[str, Any]] = []
        if result.success:
            transcript = json.loads(result.output or "{}")
            cues = _srt_cues(transcript["output_path"])
        elif not _transcription_can_degrade(result.error):
            return {"status": "error", "error": result.error or "Source transcription failed"}
        return {
            "status": "success",
            "content": f"Aligned {len(cues)} source dialogue cues",
            "source_path": source,
            "duration_ms": message.payload.get("duration_ms"),
            "scenes": message.payload.get("scenes", []),
            "source_transcript": transcript,
            "transcript_cues": cues,
            "transcript_warning": result.error if not result.success else None,
            "description": message.payload.get("description"),
            "target_duration_seconds": message.payload.get("target_duration_seconds", 30),
            "hard_subtitles": message.payload.get("hard_subtitles", True),
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("video_transcribe") is None:
            raise RuntimeError("video_transcribe tool is not registered")


class VideoHighlightScoringAgent(BaseAgent):
    """Combine deterministic activity features with M3 multi-frame semantics."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        request = self._merged_request(message.payload)
        scenes = request.get("scenes")
        description = request.get("description")
        if not isinstance(scenes, list) or not scenes or not isinstance(description, str):
            return {
                "status": "error",
                "error": "Highlight scoring requires enriched scenes and description",
            }
        cues = request.get("transcript_cues", [])
        if not isinstance(cues, list):
            cues = []
        batch_size = max(1, int(self.parameters.get("max_scenes_per_batch", 5)))
        max_images = max(1, int(self.parameters.get("max_images_per_batch", 20)))
        max_frames = max(1, int(self.parameters.get("max_frames_per_scene", 4)))
        max_semantic_scenes = max(1, int(self.parameters.get("max_semantic_scenes", 30)))
        semantic_scenes = self._prefilter_scenes(scenes, cues, max_semantic_scenes)
        semantic_by_id: dict[str, dict[str, Any]] = {}
        batch_warnings: list[str] = []
        if not _optional_model_configured(self):
            batch_warnings.append(
                f"semantic scoring skipped: {self.config.model.api_key_env} is not set"
            )
        else:
            for batch in self._scene_batches(
                semantic_scenes, batch_size, max_images, max_frames
            ):
                content = self._batch_content(
                    batch,
                    description,
                    cues,
                    max_frames_per_scene=max_frames,
                )
                try:
                    result = await self._score_batch(message, content)
                except Exception as exc:
                    batch_warnings.append(f"semantic batch degraded: {type(exc).__name__}")
                    continue
                values = result.get("scene_scores", [])
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, dict) and isinstance(value.get("scene_id"), str):
                            semantic_by_id[value["scene_id"]] = value

        action_brief = bool(
            re.search(
                r"动作|运动|追逐|打斗|冲突|爆炸|碰撞|进球|射门|奔跑|跳跃|高潮|精彩|action|motion|fight|chase",
                description,
                re.IGNORECASE,
            )
        )
        scored = [
            self._score_scene(scene, semantic_by_id.get(str(scene.get("id")), {}), action_brief)
            for scene in scenes
        ]
        ranked = sorted(scored, key=lambda scene: scene["highlight_score"], reverse=True)
        return {
            "status": "success",
            "content": f"Scored {len(scored)} scenes using motion, audio and optional M3 semantics",
            "source_path": request.get("source_path"),
            "duration_ms": request.get("duration_ms"),
            "description": description,
            "target_duration_seconds": request.get("target_duration_seconds", 30),
            "hard_subtitles": request.get("hard_subtitles", True),
            "transcript_cues": cues,
            "source_transcript": request.get("source_transcript"),
            "scenes": scored,
            "scoring_summary": {
                "action_brief": action_brief,
                "ranked_scene_ids": [scene.get("id") for scene in ranked],
                "top_score": ranked[0]["highlight_score"],
                "total_scene_count": len(scenes),
                "semantic_scene_count": len(semantic_scenes),
                "semantic_scored_scene_count": len(semantic_by_id),
                "semantic_scene_ids": [scene.get("id") for scene in semantic_scenes],
                "batch_warnings": batch_warnings,
            },
        }

    async def _health_probe(self) -> None:
        if _optional_model_configured(self):
            await super()._health_probe()

    @staticmethod
    def _merged_request(payload: dict[str, Any]) -> dict[str, Any]:
        feature_payload = payload.get("agent-video-scene-features")
        transcript_payload = payload.get("agent-source-transcript")
        if isinstance(feature_payload, dict) and isinstance(transcript_payload, dict):
            return {
                **transcript_payload,
                **feature_payload,
                "transcript_cues": transcript_payload.get("transcript_cues", []),
            }
        return dict(payload)

    def _batch_content(
        self,
        scenes: list[dict[str, Any]],
        description: str,
        transcript_cues: list[dict[str, Any]],
        *,
        max_frames_per_scene: int = 4,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "你是影视精彩度分析师。依据用户描述、按时间排列的多帧画面、同期对白及"
                    "确定性运动/音频指标，为每个镜头独立评分。无对白不是缺点；追逐、打斗、"
                    "射门、碰撞等静音动作可获得高分。所有分数为0到1。只输出严格 JSON："
                    '{"scene_scores":[{"scene_id":"scene-001","semantic_relevance":0.8,'
                    '"action_confidence":0.8,"emotion_intensity":0.5,"aesthetic_quality":0.7,'
                    '"dialogue_relevance":0.0,"reason":"..."}]}'
                    f"\n用户描述：{description}"
                ),
            }
        ]
        for scene in scenes:
            features = scene.get("features", {})
            dialogue = " / ".join(
                str(cue.get("text", ""))
                for cue in transcript_cues
                if int(cue.get("end_ms", 0)) > int(scene["start_ms"])
                and int(cue.get("start_ms", 0)) < int(scene["end_ms"])
            )
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"{scene.get('id')}，{scene['start_ms'] / 1000:.2f}s-"
                        f"{scene['end_ms'] / 1000:.2f}s；确定性特征："
                        f"{json.dumps(features, ensure_ascii=False)}；"
                        f"同期对白：{dialogue or '（无对白）'}；以下为按时间排列的多帧。"
                    ),
                }
            )
            frames = scene.get("sample_frames", [])
            if not isinstance(frames, list):
                frames = []
            for frame in frames[:max_frames_per_scene]:
                if not isinstance(frame, dict):
                    continue
                path = Path(str(frame.get("path", "")))
                if not path.is_file():
                    continue
                content.append(
                    {
                        "type": "text",
                        "text": f"时间点 {int(frame.get('timestamp_ms', 0)) / 1000:.2f}s",
                    }
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                "data:image/jpeg;base64,"
                                f"{base64.b64encode(path.read_bytes()).decode()}"
                            )
                        },
                    }
                )
        return content

    @staticmethod
    def _scene_batches(
        scenes: list[dict[str, Any]],
        max_scenes: int,
        max_images: int,
        max_frames_per_scene: int,
    ) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_images = 0
        for scene in scenes:
            frames = scene.get("sample_frames", [])
            image_count = min(len(frames) if isinstance(frames, list) else 0, max_frames_per_scene)
            if current and (
                len(current) >= max_scenes or current_images + image_count > max_images
            ):
                batches.append(current)
                current = []
                current_images = 0
            current.append(scene)
            current_images += image_count
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _prefilter_scenes(
        scenes: list[dict[str, Any]],
        transcript_cues: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        if len(scenes) <= limit:
            return scenes

        def feature(scene: dict[str, Any], key: str) -> float:
            values = scene.get("features", {})
            if not isinstance(values, dict):
                return 0.0
            try:
                return float(values.get(key, 0.0))
            except (TypeError, ValueError):
                return 0.0

        def activity(scene: dict[str, Any]) -> float:
            return (
                feature(scene, "motion_intensity") * 0.5
                + feature(scene, "visual_change") * 0.2
                + feature(scene, "audio_impact") * 0.2
                + feature(scene, "audio_energy") * 0.1
                - feature(scene, "freeze_ratio") * 0.05
                - feature(scene, "black_ratio") * 0.25
            )

        def dialogue_size(scene: dict[str, Any]) -> int:
            return sum(
                len(str(cue.get("text", "")))
                for cue in transcript_cues
                if int(cue.get("end_ms", 0)) > int(scene["start_ms"])
                and int(cue.get("start_ms", 0)) < int(scene["end_ms"])
            )

        selected: dict[str, dict[str, Any]] = {}

        def add(scene: dict[str, Any]) -> None:
            scene_id = str(scene.get("id"))
            if len(selected) < limit and scene_id not in selected:
                selected[scene_id] = scene

        activity_slots = max(1, round(limit * 0.65))
        dialogue_slots = max(1, round(limit * 0.15))
        for scene in sorted(scenes, key=activity, reverse=True)[:activity_slots]:
            add(scene)
        for scene in sorted(scenes, key=dialogue_size, reverse=True)[:dialogue_slots]:
            add(scene)
        uniform_slots = max(2, limit - len(selected))
        for index in range(uniform_slots):
            position = round(index * (len(scenes) - 1) / max(1, uniform_slots - 1))
            add(scenes[position])
        for scene in sorted(scenes, key=activity, reverse=True):
            add(scene)
        return sorted(selected.values(), key=lambda scene: int(scene["start_ms"]))

    async def _score_batch(
        self, message: Message, content: list[dict[str, Any]]
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "只输出一个完整、严格、简短的 JSON 对象，不输出 Markdown。",
            },
            {"role": "user", "content": content},
        ]
        last_error: ValueError | None = None
        for attempt in range(2):
            response = await self.llm_gateway.chat(
                messages=messages,
                model_config=self.config.model,
                prefer_default=False,
                max_tokens=max(self.config.model.max_tokens, 4096),
                temperature=0 if attempt else self.config.model.temperature,
                trace_context=LLMTraceContext(
                    workflow_id=message.workflow_id,
                    execution_id=message.workflow_id,
                    agent_id=self.id,
                ),
            )
            try:
                return parse_json_object(response.content)
            except ValueError as exc:
                last_error = exc
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": "响应无法解析，请只返回包含 scene_scores 数组的严格 JSON。",
                        },
                    ]
                )
        raise ValueError(f"Invalid scene scoring JSON after retry: {last_error}")

    @staticmethod
    def _score_scene(
        scene: dict[str, Any], semantic: dict[str, Any], action_brief: bool
    ) -> dict[str, Any]:
        features = scene.get("features", {})
        if not isinstance(features, dict):
            features = {}

        def value(source: dict[str, Any], key: str, default: float = 0.0) -> float:
            try:
                return max(0.0, min(1.0, float(source.get(key, default))))
            except (TypeError, ValueError):
                return default

        components = {
            "semantic_relevance": value(semantic, "semantic_relevance", 0.15),
            "action_confidence": value(semantic, "action_confidence", 0.0),
            "emotion_intensity": value(semantic, "emotion_intensity", 0.0),
            "aesthetic_quality": value(semantic, "aesthetic_quality", 0.5),
            "dialogue_relevance": value(semantic, "dialogue_relevance", 0.0),
            "motion_intensity": value(features, "motion_intensity"),
            "visual_change": value(features, "visual_change"),
            "audio_impact": value(features, "audio_impact"),
        }
        if action_brief:
            weights = {
                "semantic_relevance": 0.28,
                "motion_intensity": 0.30,
                "visual_change": 0.10,
                "audio_impact": 0.08,
                "emotion_intensity": 0.07,
                "aesthetic_quality": 0.05,
                "action_confidence": 0.12,
            }
        else:
            weights = {
                "semantic_relevance": 0.38,
                "motion_intensity": 0.18,
                "visual_change": 0.10,
                "audio_impact": 0.08,
                "emotion_intensity": 0.10,
                "aesthetic_quality": 0.08,
                "action_confidence": 0.08,
            }
        penalty = value(features, "freeze_ratio") * 0.15 + value(
            features, "black_ratio"
        ) * 0.35
        score = sum(components[key] * weight for key, weight in weights.items()) - penalty
        return {
            **scene,
            **components,
            "quality_penalty": round(penalty, 4),
            "highlight_score": round(max(0.0, min(1.0, score)), 4),
            "score_reason": semantic.get("reason") or "deterministic feature fallback",
        }


class VideoHighlightSelectorAgent(BaseAgent):
    """Use MiniMax-M3 vision to rank keyframed scenes against the edit brief."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        scenes = message.payload.get("scenes")
        description = message.payload.get("description")
        if not isinstance(scenes, list) or not scenes or not isinstance(description, str):
            return {
                "status": "error",
                "error": "Highlight selection requires scenes and description",
            }
        target_seconds = float(message.payload.get("target_duration_seconds", 30))
        if all(isinstance(scene, dict) and "highlight_score" in scene for scene in scenes):
            return self._select_scored_scenes(message, scenes, description, target_seconds)
        batch_size = max(1, int(self.parameters.get("max_visual_scenes", 24)))
        transcript_cues = message.payload.get("transcript_cues", [])
        if not isinstance(transcript_cues, list):
            transcript_cues = []
        batch_selections: list[dict[str, Any]] = []
        multiple_batches = len(scenes) > batch_size
        for offset in range(0, len(scenes), batch_size):
            candidates = scenes[offset : offset + batch_size]
            content: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": (
                        "你是影视剪辑选片师。根据用户描述，从按时间排列的场景关键帧中选择"
                        "最相关、最精彩且动作连续的候选片段。不要选择静态空镜，除非描述明确要求。"
                        f"\n用户描述：{description}\n目标总时长：约 {target_seconds:g} 秒。"
                        + (
                            "\n这是长片的一个分批，最多返回本批最相关的 3 个候选；"
                            "没有则返回空数组。"
                            if multiple_batches
                            else ""
                        )
                        + "\n只输出 JSON："
                        '{"verdict":"passed","selected_scene_ids":["scene-001"],'
                        '"reasons":{"scene-001":"..."},"summary":"..."}'
                    ),
                }
            ]
            for scene in candidates:
                frame_path = Path(str(scene.get("keyframe_path", "")))
                if not frame_path.is_file():
                    continue
                content.append(
                    {
                        "type": "text",
                        "text": self._scene_label(scene, transcript_cues),
                    }
                )
                encoded = base64.b64encode(frame_path.read_bytes()).decode()
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                    }
                )
            batch_selections.append(await self._model_selection(message, content))

        if not multiple_batches:
            selection = batch_selections[0]
        else:
            by_id = {scene.get("id"): scene for scene in scenes}
            candidate_pool: list[dict[str, Any]] = []
            for batch in batch_selections:
                ids = batch.get("selected_scene_ids", [])
                reasons = batch.get("reasons", {})
                if not isinstance(ids, list):
                    continue
                for scene_id in ids:
                    scene = by_id.get(scene_id)
                    if not isinstance(scene, dict):
                        continue
                    candidate_pool.append(
                        {
                            "scene_id": scene_id,
                            "start_ms": scene["start_ms"],
                            "end_ms": scene["end_ms"],
                            "duration_ms": scene["duration_ms"],
                            "reason": reasons.get(scene_id) if isinstance(reasons, dict) else "",
                        }
                    )
            if not candidate_pool:
                return {"status": "error", "error": "Highlight selector returned no scenes"}
            selection = await self._model_selection(
                message,
                (
                    "你是总剪辑师。根据用户描述，从分批视觉分析产生的候选中做全片最终选择。"
                    "按叙事连贯性排列 selected_scene_ids，总时长尽量接近目标但不要超过。"
                    f"\n用户描述：{description}\n目标总时长：{target_seconds:g} 秒。"
                    f"\n候选：{json.dumps(candidate_pool, ensure_ascii=False)}"
                    "\n只输出 JSON："
                    '{"verdict":"passed","selected_scene_ids":["scene-001"],'
                    '"reasons":{"scene-001":"..."},"summary":"..."}'
                ),
            )

        selected_ids = selection.get("selected_scene_ids")
        if not isinstance(selected_ids, list) or not selected_ids:
            return {"status": "error", "error": "Highlight selector returned no scenes"}
        by_id = {scene.get("id"): scene for scene in scenes}
        selected: list[dict[str, Any]] = []
        remaining_ms = round(target_seconds * 1000)
        reasons = selection.get("reasons", {})
        for scene_id in selected_ids:
            scene = by_id.get(scene_id)
            if not isinstance(scene, dict) or remaining_ms <= 0:
                continue
            duration = min(int(scene["duration_ms"]), remaining_ms)
            selected.append(
                {
                    "scene_id": scene_id,
                    "start_ms": int(scene["start_ms"]),
                    "end_ms": int(scene["start_ms"]) + duration,
                    "reason": reasons.get(scene_id) if isinstance(reasons, dict) else None,
                }
            )
            remaining_ms -= duration
        if not selected:
            return {"status": "error", "error": "No valid selected scenes matched the source"}
        return {
            "status": "success",
            "content": selection.get("summary", "Highlight selection completed"),
            "source_path": message.payload.get("source_path"),
            "description": description,
            "selected_clips": selected,
            "target_duration_seconds": target_seconds,
            "hard_subtitles": message.payload.get("hard_subtitles", True),
            "selection": selection,
        }

    async def _health_probe(self) -> None:
        if _optional_model_configured(self):
            await super()._health_probe()

    @staticmethod
    def _select_scored_scenes(
        message: Message,
        scenes: list[dict[str, Any]],
        description: str,
        target_seconds: float,
    ) -> dict[str, Any]:
        eligible = [
            scene
            for scene in scenes
            if float(scene.get("highlight_score", 0)) > 0
            and float(scene.get("black_ratio", scene.get("features", {}).get("black_ratio", 0)))
            < 0.8
        ]
        ranked = sorted(eligible, key=lambda scene: float(scene["highlight_score"]), reverse=True)
        candidate_clips: list[dict[str, Any]] = []
        candidate_duration_ms = 0
        candidate_target_ms = max(round(target_seconds * 2000), round(target_seconds * 1000))
        for scene in ranked:
            if candidate_duration_ms >= candidate_target_ms:
                break
            candidate = {
                "scene_id": scene.get("id"),
                "start_ms": int(scene["start_ms"]),
                "end_ms": int(scene["end_ms"]),
                "score": float(scene["highlight_score"]),
                "reason": scene.get("score_reason"),
            }
            candidate_clips.append(candidate)
            candidate_duration_ms += candidate["end_ms"] - candidate["start_ms"]
        selected: list[dict[str, Any]] = []
        remaining_ms = round(target_seconds * 1000)
        for scene in ranked:
            if remaining_ms <= 0:
                break
            duration = min(int(scene["end_ms"]) - int(scene["start_ms"]), remaining_ms)
            selected.append(
                {
                    "scene_id": scene.get("id"),
                    "start_ms": int(scene["start_ms"]),
                    "end_ms": int(scene["start_ms"]) + duration,
                    "score": float(scene["highlight_score"]),
                    "reason": scene.get("score_reason"),
                }
            )
            remaining_ms -= duration
        selected.sort(key=lambda clip: clip["start_ms"])
        if not selected:
            return {"status": "error", "error": "No scored scenes passed selection"}
        return {
            "status": "success",
            "content": f"Selected {len(selected)} scored scenes",
            "source_path": message.payload.get("source_path"),
            "description": description,
            "selected_clips": selected,
            "candidate_clips": candidate_clips,
            "scored_scenes": scenes,
            "target_duration_seconds": target_seconds,
            "hard_subtitles": message.payload.get("hard_subtitles", True),
            "selection": {
                "mode": "composite_score",
                "selected_scene_ids": [clip["scene_id"] for clip in selected],
            },
        }

    async def _model_selection(
        self, message: Message, content: str | list[dict[str, Any]]
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": "你只输出一个完整、严格、简短的 JSON 对象，不输出思考或 Markdown。",
            },
            {"role": "user", "content": content},
        ]
        last_error: ValueError | None = None
        for attempt in range(2):
            response = await self.llm_gateway.chat(
                messages=messages,
                model_config=self.config.model,
                prefer_default=False,
                max_tokens=max(self.config.model.max_tokens, 4096),
                temperature=0 if attempt else self.config.model.temperature,
                trace_context=LLMTraceContext(
                    workflow_id=message.workflow_id,
                    execution_id=message.workflow_id,
                    agent_id=self.id,
                ),
            )
            try:
                return parse_json_object(response.content)
            except ValueError as exc:
                last_error = exc
                messages.extend(
                    [
                        {"role": "assistant", "content": response.content},
                        {
                            "role": "user",
                            "content": (
                                "上一回复无法解析。请重新完成同一选片任务，只返回一个完整 JSON；"
                                "selected_scene_ids 必须是数组，reasons 必须是对象。"
                            ),
                        },
                    ]
                )
        raise ValueError(f"Invalid highlight selection JSON after retry: {last_error}")

    @staticmethod
    def _scene_label(scene: dict[str, Any], transcript_cues: list[dict[str, Any]]) -> str:
        dialogue = " / ".join(
            str(cue.get("text", ""))
            for cue in transcript_cues
            if int(cue.get("end_ms", 0)) > int(scene["start_ms"])
            and int(cue.get("start_ms", 0)) < int(scene["end_ms"])
        )
        return (
            f"{scene['id']}，{scene['start_ms'] / 1000:.2f}s-"
            f"{scene['end_ms'] / 1000:.2f}s；同期对白：{dialogue or '（无对白）'}"
        )


class VideoIntervalRefinerAgent(BaseAgent):
    """Find activity peaks inside candidate scenes and enforce the target duration."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        candidates = message.payload.get("candidate_clips") or message.payload.get(
            "selected_clips"
        )
        scored_scenes = message.payload.get("scored_scenes")
        source = message.payload.get("source_path")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(scored_scenes, list)
            or not isinstance(source, str)
        ):
            return {
                "status": "error",
                "error": "Interval refinement requires candidates, scored_scenes and source_path",
            }
        settings = self.parameters.get("refinement", {})
        if not isinstance(settings, dict):
            settings = {}
        target_ms = round(float(message.payload.get("target_duration_seconds", 30)) * 1000)
        min_clip_ms = int(settings.get("min_clip_ms", 800))
        max_clip_ms = int(settings.get("max_clip_ms", 8000))
        pre_roll_ms = int(settings.get("pre_roll_ms", 350))
        post_roll_ms = int(settings.get("post_roll_ms", 550))
        fps = int(settings.get("fps", 30))
        by_id = {scene.get("id"): scene for scene in scored_scenes if isinstance(scene, dict)}
        refined: list[dict[str, Any]] = []
        remaining_ms = target_ms
        reports: list[dict[str, Any]] = []
        for candidate in candidates:
            if remaining_ms < min_clip_ms or not isinstance(candidate, dict):
                break
            scene = by_id.get(candidate.get("scene_id"))
            if not isinstance(scene, dict):
                continue
            interval, report = self._refine_scene(
                scene,
                min_clip_ms=min_clip_ms,
                max_clip_ms=max_clip_ms,
                pre_roll_ms=pre_roll_ms,
                post_roll_ms=post_roll_ms,
                fps=fps,
            )
            duration = interval[1] - interval[0]
            if duration > remaining_ms:
                interval = self._fit_around_peak(
                    interval,
                    int(report["peak_ms"]),
                    remaining_ms,
                    int(scene["start_ms"]),
                    int(scene["end_ms"]),
                    fps,
                )
                duration = interval[1] - interval[0]
            if duration < min_clip_ms:
                continue
            refined.append(
                {
                    "scene_id": scene.get("id"),
                    "start_ms": interval[0],
                    "end_ms": interval[1],
                    "peak_ms": int(report["peak_ms"]),
                    "score": float(scene.get("highlight_score", candidate.get("score", 0))),
                    "reason": scene.get("score_reason") or candidate.get("reason"),
                }
            )
            reports.append(report)
            remaining_ms -= duration

        if not refined:
            return {"status": "error", "error": "No valid frame-level intervals were produced"}
        refined.sort(key=lambda clip: clip["start_ms"])
        actual_ms = sum(clip["end_ms"] - clip["start_ms"] for clip in refined)
        return {
            "status": "success",
            "content": f"Refined {len(refined)} frame-level highlight intervals",
            "source_path": source,
            "description": message.payload.get("description"),
            "selected_clips": refined,
            "target_duration_seconds": target_ms / 1000,
            "hard_subtitles": message.payload.get("hard_subtitles", True),
            "refinement_report": {
                "target_duration_ms": target_ms,
                "actual_duration_ms": actual_ms,
                "duration_delta_ms": actual_ms - target_ms,
                "intervals": reports,
            },
        }

    async def _health_probe(self) -> None:
        settings = self.parameters.get("refinement", {})
        if not isinstance(settings, dict):
            raise RuntimeError("refinement parameters must be an object")
        min_clip_ms = int(settings.get("min_clip_ms", 800))
        max_clip_ms = int(settings.get("max_clip_ms", 8000))
        fps = int(settings.get("fps", 30))
        if min_clip_ms <= 0:
            raise RuntimeError("min_clip_ms must be positive")
        if max_clip_ms < min_clip_ms:
            raise RuntimeError("max_clip_ms must be greater than or equal to min_clip_ms")
        if fps <= 0:
            raise RuntimeError("fps must be positive")

    @classmethod
    def _refine_scene(
        cls,
        scene: dict[str, Any],
        *,
        min_clip_ms: int,
        max_clip_ms: int,
        pre_roll_ms: int,
        post_roll_ms: int,
        fps: int,
    ) -> tuple[tuple[int, int], dict[str, Any]]:
        scene_start = int(scene["start_ms"])
        scene_end = int(scene["end_ms"])
        rows = scene.get("feature_samples", [])
        if not isinstance(rows, list):
            rows = []
        activity_rows: list[tuple[int, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            timestamp = int(row.get("timestamp_ms", scene_start))
            difference = max(0.0, min(1.0, float(row.get("frame_difference", 0)) / 24))
            audio = max(
                0.0,
                min(1.0, (float(row.get("audio_rms_db", -120)) + 60) / 60),
            )
            activity_rows.append((timestamp, difference * 0.75 + audio * 0.25))
        if activity_rows:
            peak_activity = max(value for _timestamp, value in activity_rows)
            peak_times = [
                timestamp
                for timestamp, value in activity_rows
                if value >= peak_activity * 0.98
            ]
            peak_ms = round(statistics.median(peak_times))
        else:
            peak_ms = (scene_start + scene_end) // 2
            peak_activity = 0.0
        threshold = max(0.08, peak_activity * 0.35)
        active = [timestamp for timestamp, value in activity_rows if value >= threshold]
        if active:
            # Keep only the activity cluster around the strongest time point.
            spacing = cls._median_spacing(activity_rows)
            clusters: list[list[int]] = [[active[0]]]
            for timestamp in active[1:]:
                if timestamp - clusters[-1][-1] <= spacing * 2.1:
                    clusters[-1].append(timestamp)
                else:
                    clusters.append([timestamp])
            cluster = next(
                (values for values in clusters if values[0] <= peak_ms <= values[-1]),
                [],
            )
            active_start = min(cluster or [peak_ms])
            active_end = max(cluster or [peak_ms]) + spacing
            start = active_start - pre_roll_ms
            end = active_end + post_roll_ms
        else:
            start = peak_ms - min_clip_ms // 2
            end = start + min_clip_ms
        if end - start < min_clip_ms:
            missing = min_clip_ms - (end - start)
            start -= missing // 2
            end += missing - missing // 2
        if end - start > max_clip_ms:
            start = peak_ms - max_clip_ms // 2
            end = start + max_clip_ms
        start, end = cls._clamp_interval(start, end, scene_start, scene_end)
        start = cls._align_ms(start, fps)
        end = cls._align_ms(end, fps)
        if end <= start:
            end = start + max(1, round(1000 / fps))
        return (start, end), {
            "scene_id": scene.get("id"),
            "scene_start_ms": scene_start,
            "scene_end_ms": scene_end,
            "peak_ms": peak_ms,
            "peak_activity": round(peak_activity, 4),
            "activity_threshold": round(threshold, 4),
            "refined_start_ms": start,
            "refined_end_ms": end,
        }

    @staticmethod
    def _median_spacing(rows: list[tuple[int, float]]) -> int:
        differences = [right[0] - left[0] for left, right in zip(rows, rows[1:], strict=False)]
        return max(1, round(statistics.median(differences))) if differences else 250

    @staticmethod
    def _clamp_interval(start: int, end: int, minimum: int, maximum: int) -> tuple[int, int]:
        duration = end - start
        if start < minimum:
            start = minimum
            end = min(maximum, start + duration)
        if end > maximum:
            end = maximum
            start = max(minimum, end - duration)
        return start, end

    @classmethod
    def _fit_around_peak(
        cls,
        interval: tuple[int, int],
        peak_ms: int,
        duration_ms: int,
        scene_start: int,
        scene_end: int,
        fps: int,
    ) -> tuple[int, int]:
        start = peak_ms - duration_ms // 2
        end = start + duration_ms
        start, end = cls._clamp_interval(start, end, scene_start, scene_end)
        start = cls._align_ms(start, fps)
        end = cls._align_ms(end, fps)
        if end - start > duration_ms:
            end -= max(0, end - start - duration_ms)
        return start, end

    @staticmethod
    def _align_ms(value: int, fps: int) -> int:
        return round(round(value * fps / 1000) * 1000 / fps)


class VideoHighlightRendererAgent(BaseAgent):
    async def handle_message(self, message: Message) -> dict[str, Any]:
        source = message.payload.get("source_path")
        clips = message.payload.get("selected_clips")
        if not isinstance(source, str) or not isinstance(clips, list):
            return {"status": "error", "error": "Highlight renderer requires source and clips"}
        settings = self.parameters.get("render", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "highlight_render",
            {
                "source_path": source,
                "clips": clips,
                "subtitle_path": message.payload.get("subtitle_path"),
                "width": settings.get("width", 1920),
                "height": settings.get("height", 1080),
                "fps": settings.get("fps", 30),
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Highlight rendering failed"}
        rendered = json.loads(result.output or "{}")
        return {
            "status": "success",
            "content": "Selected source clips rendered",
            "composed_video": rendered,
            "selected_clips": clips,
            "refinement_report": message.payload.get("refinement_report"),
            "description": message.payload.get("description"),
            "hard_subtitles": message.payload.get("hard_subtitles", True),
            "artifacts": [
                {
                    "type": "file",
                    "name": Path(rendered["output_path"]).name,
                    "uri": rendered["output_path"],
                    "media_type": "video/mp4",
                    "metadata": rendered,
                }
            ],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("highlight_render") is None:
            raise RuntimeError("highlight_render tool is not registered")


class VideoTranscriptionAgent(BaseAgent):
    """Create timestamped subtitles from the actual edited source audio."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        composed = message.payload.get("composed_video")
        if not isinstance(composed, dict) or not isinstance(composed.get("output_path"), str):
            return {"status": "error", "error": "Transcription requires composed_video"}
        settings = self.parameters.get("transcription", {})
        if not isinstance(settings, dict):
            settings = {}
        result = await self.tool_registry.execute(
            "video_transcribe",
            {
                "path": composed["output_path"],
                "language": settings.get("language", "auto"),
                "timeout": settings.get("timeout", 3600),
            },
        )
        if not result.success:
            if not _transcription_can_degrade(result.error):
                return {"status": "error", "error": result.error or "Video transcription failed"}
            clips = message.payload.get("selected_clips", [])
            captions = [
                str(clip.get("reason"))
                for clip in clips
                if isinstance(clip, dict) and clip.get("reason")
            ]
            fallback_text = "。".join(captions) or str(
                message.payload.get("description", "精彩片段")
            )
            fallback = await self.tool_registry.execute(
                "subtitle_create",
                {
                    "text": fallback_text,
                    "duration_ms": composed.get("duration_ms", 3000),
                    "start_ms": 100,
                    "end_padding_ms": 100,
                },
            )
            if not fallback.success:
                return {
                    "status": "error",
                    "error": fallback.error or "Caption fallback failed",
                }
            subtitle = json.loads(fallback.output or "{}")
            subtitle["caption_source"] = "visual_selection_reason"
            subtitle["transcript_warning"] = result.error
        else:
            subtitle = json.loads(result.output or "{}")
            subtitle["caption_source"] = "source_dialogue"
        subtitle_artifact = {
            "type": "file",
            "name": Path(subtitle["output_path"]).name,
            "uri": subtitle["output_path"],
            "media_type": subtitle["media_type"],
            "metadata": subtitle,
        }
        return {
            "status": "success",
            "content": f"Generated {subtitle['cue_count']} subtitle cues",
            "composed_video": composed,
            "subtitle": subtitle,
            "selected_clips": message.payload.get("selected_clips", []),
            "refinement_report": message.payload.get("refinement_report"),
            "description": message.payload.get("description"),
            "hard_subtitles": message.payload.get("hard_subtitles", True),
            "artifacts": [*message.payload.get("artifacts", []), subtitle_artifact],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("video_transcribe") is None:
            raise RuntimeError("video_transcribe tool is not registered")
        if self.tool_registry.get("subtitle_create") is None:
            raise RuntimeError("subtitle_create tool is not registered")


class HardSubtitleAgent(BaseAgent):
    """Burn generated subtitles into the video so every player displays them."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        composed = message.payload.get("composed_video")
        subtitle = message.payload.get("subtitle")
        if not isinstance(composed, dict) or not isinstance(composed.get("output_path"), str):
            return {"status": "error", "error": "Subtitle burn requires composed_video"}
        if not isinstance(subtitle, dict) or not isinstance(subtitle.get("output_path"), str):
            return {"status": "error", "error": "Subtitle burn requires subtitle"}
        if not message.payload.get("hard_subtitles", True):
            return {
                "status": "success",
                "content": "Hard subtitles disabled; keeping SRT sidecar",
                "composed_video": composed,
                "subtitle": subtitle,
                "selected_clips": message.payload.get("selected_clips", []),
                "refinement_report": message.payload.get("refinement_report"),
                "artifacts": message.payload.get("artifacts", []),
            }
        result = await self.tool_registry.execute(
            "hard_subtitle_burn",
            {
                "video_path": composed["output_path"],
                "subtitle_path": subtitle["output_path"],
            },
        )
        if not result.success:
            return {"status": "error", "error": result.error or "Hard subtitle burn failed"}
        final_video = json.loads(result.output or "{}")
        final_artifact = {
            "type": "file",
            "name": Path(final_video["output_path"]).name,
            "uri": final_video["output_path"],
            "media_type": "video/mp4",
            "metadata": final_video,
        }
        return {
            "status": "success",
            "content": "Subtitles permanently burned into video frames",
            "composed_video": final_video,
            "subtitle": subtitle,
            "selected_clips": message.payload.get("selected_clips", []),
            "refinement_report": message.payload.get("refinement_report"),
            "artifacts": [*message.payload.get("artifacts", []), final_artifact],
        }

    async def _health_probe(self) -> None:
        if self.tool_registry.get("hard_subtitle_burn") is None:
            raise RuntimeError("hard_subtitle_burn tool is not registered")
