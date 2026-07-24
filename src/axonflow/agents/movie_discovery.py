"""Agents for title-driven, rights-aware discovery of editable movie media."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from axonflow.core.agent import BaseAgent
from axonflow.core.message import Message
from axonflow.json_utils import parse_json_object
from axonflow.llm.gateway import LLMTraceContext


def _request(message: Message) -> dict[str, Any]:
    task = message.payload.get("task")
    if isinstance(task, dict):
        return dict(task)
    if isinstance(task, str):
        try:
            return parse_json_object(task)
        except ValueError:
            return {"title": task}
    return dict(message.payload)


def _model_configured(agent: BaseAgent) -> bool:
    model = agent.config.model
    if model.credential_id:
        return True
    return not model.api_key_env or bool(os.getenv(model.api_key_env))


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w\u4e00-\u9fff]+", value.lower())
        if len(token) > 1 and token not in {"official", "trailer", "movie", "video"}
    }


class MovieTitleResolverAgent(BaseAgent):
    """Normalize a user-facing movie name into aliases and search queries."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        request = _request(message)
        title = request.get("title") or request.get("movie_name") or request.get("name")
        if not isinstance(title, str) or not title.strip():
            return {"status": "error", "error": "Movie discovery requires title"}
        title = title.strip()
        resolved: dict[str, Any] = {}
        warning: str | None = None
        if _model_configured(self):
            prompt = (
                "识别用户给出的影视名称，只输出严格 JSON。不要编造不存在的作品。"
                '格式：{"canonical_title":"中文正式名","original_title":"原始语言片名",'
                '"year":2018,"aliases":["别名"],"studios":["版权方或发行方"],'
                '"search_queries":["适合寻找官方预告片的英文检索词"]}。'
                f"\n用户输入：{title}"
            )
            try:
                response = await self.llm_gateway.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": "你只输出一个完整、严格、简短的 JSON 对象。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    model_config=self.config.model,
                    prefer_default=False,
                    max_tokens=max(2048, self.config.model.max_tokens),
                    temperature=0,
                    trace_context=LLMTraceContext(
                        workflow_id=message.workflow_id,
                        execution_id=message.workflow_id,
                        agent_id=self.id,
                    ),
                )
                resolved = parse_json_object(response.content)
            except Exception as exc:
                warning = f"title model degraded: {type(exc).__name__}"
        canonical = str(resolved.get("canonical_title") or title).strip()
        original = str(resolved.get("original_title") or "").strip()
        aliases = resolved.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        aliases = list(
            dict.fromkeys(
                value.strip()
                for value in [title, canonical, original, *map(str, aliases)]
                if value.strip()
            )
        )
        queries = resolved.get("search_queries", [])
        if not isinstance(queries, list):
            queries = []
        search_title = original or canonical
        queries = list(
            dict.fromkeys(
                [
                    *[str(value).strip() for value in queries if str(value).strip()],
                    f"{search_title} official trailer",
                    f"{search_title} official trailer studio",
                ]
            )
        )[:4]
        description = request.get("description")
        if not isinstance(description, str) or not description.strip():
            description = (
                f"从《{canonical}》相关官方素材中选择最有视觉冲击力、人物动作明显、"
                "节奏强且具有起承转合的精彩片段，排除片头片尾、黑场和重复画面。"
            )
        return {
            "status": "success",
            "content": f"Resolved movie title to {original or canonical}",
            "title": title,
            "canonical_title": canonical,
            "original_title": original or None,
            "release_year": resolved.get("year"),
            "aliases": aliases,
            "studios": resolved.get("studios", []),
            "search_queries": queries,
            "title_resolution_warning": warning,
            "description": description,
            "target_duration_seconds": request.get("target_duration_seconds", 30),
            "hard_subtitles": bool(request.get("hard_subtitles", True)),
            "authorized_source": request.get("authorized_source"),
            "rights_confirmed": bool(request.get("rights_confirmed", False)),
            "usage": request.get("usage", "internal_demo"),
        }

    async def _health_probe(self) -> None:
        if _model_configured(self):
            await super()._health_probe()


class OfficialMediaDiscoveryAgent(BaseAgent):
    """Search official channels and retain source evidence for the rights gate."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        if isinstance(message.payload.get("authorized_source"), str):
            return {
                **message.payload,
                "status": "success",
                "content": "Using user-provided authorized source candidate",
                "source_candidates": [
                    {
                        "title": message.payload.get("canonical_title"),
                        "url": message.payload["authorized_source"],
                        "source_kind": "user_authorized",
                        "official_channel": False,
                        "source_score": 1.0,
                    }
                ],
                "source_evidence": [],
            }
        queries = message.payload.get("search_queries", [])
        if not isinstance(queries, list) or not queries:
            return {"status": "error", "error": "Media discovery requires search queries"}
        trusted = self.parameters.get(
            "trusted_channels",
            [
                "Marvel Entertainment",
                "Marvel Studios",
                "Walt Disney Studios",
                "Disney",
                "Pixar",
                "Warner Bros. Pictures",
                "Universal Pictures",
                "Sony Pictures Entertainment",
                "Paramount Pictures",
            ],
        )
        trusted_values = [
            str(value).strip().lower() for value in trusted if str(value).strip()
        ]
        raw: list[dict[str, Any]] = []
        warnings: list[str] = []
        for query in queries[:3]:
            result = await self.tool_registry.execute(
                "official_video_search",
                {
                    "query": str(query),
                    "max_results": self.parameters.get("max_results_per_query", 8),
                    "timeout": self.parameters.get("timeout", 180),
                },
            )
            if not result.success:
                warnings.append(result.error or f"search failed: {query}")
                continue
            payload = json.loads(result.output or "{}")
            values = payload.get("candidates", [])
            if isinstance(values, list):
                raw.extend(value for value in values if isinstance(value, dict))

        evidence: list[dict[str, Any]] = []
        evidence_title = (
            message.payload.get("original_title")
            or message.payload.get("canonical_title")
        )
        evidence_result = await self.tool_registry.execute(
            "web_search",
            {
                "query": f"{evidence_title} official movie trailer studio",
                "max_results": 5,
            },
        )
        if evidence_result.success:
            values = json.loads(evidence_result.output or "[]")
            if isinstance(values, list):
                evidence = [value for value in values if isinstance(value, dict)]
        else:
            warnings.append(evidence_result.error or "official page evidence search failed")

        title_terms = _tokens(
            " ".join(
                str(value)
                for value in [
                    message.payload.get("canonical_title"),
                    message.payload.get("original_title"),
                    *message.payload.get("aliases", []),
                ]
                if value
            )
        )
        candidates: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for value in raw:
            url = str(value.get("url") or "")
            if not url or url in seen_urls:
                continue
            channel = str(value.get("channel") or "").strip()
            channel_id = str(value.get("channel_id") or "").strip()
            official = any(
                trusted_name in channel.lower() or trusted_name in channel_id.lower()
                for trusted_name in trusted_values
            )
            if not official:
                continue
            result_terms = _tokens(str(value.get("title") or ""))
            relevance = (
                len(title_terms & result_terms) / max(1, min(len(title_terms), 6))
                if title_terms
                else 0.0
            )
            score = 0.35 + 0.30 + 0.10 + min(0.15, relevance * 0.15) + 0.05
            candidates.append(
                {
                    **value,
                    "official_channel": True,
                    "source_kind": "official_promotional",
                    "authorization_clarity": "public official channel; reuse rights not implied",
                    "source_score": round(min(1.0, score), 4),
                }
            )
            seen_urls.add(url)
        candidates.sort(
            key=lambda item: (
                float(item.get("source_score", 0)),
                int(item.get("view_count") or 0),
            ),
            reverse=True,
        )
        if not candidates:
            return {
                "status": "error",
                "error": "No trusted official video source passed discovery",
                "source_warnings": warnings,
            }
        return {
            **message.payload,
            "status": "success",
            "content": f"Found {len(candidates)} trusted official video candidates",
            "source_candidates": candidates,
            "source_evidence": evidence,
            "source_warnings": warnings,
        }

    async def _health_probe(self) -> None:
        for name in ("official_video_search", "web_search"):
            if self.tool_registry.get(name) is None:
                raise RuntimeError(f"{name} tool is not registered")


class MediaRightsGateAgent(BaseAgent):
    """Require a clear processing basis before a discovered URL reaches ingest."""

    async def handle_message(self, message: Message) -> dict[str, Any]:
        candidates = message.payload.get("source_candidates", [])
        if not isinstance(candidates, list) or not candidates:
            return {"status": "error", "error": "Rights gate requires source candidates"}
        usage = str(message.payload.get("usage", "internal_demo"))
        rights_confirmed = bool(message.payload.get("rights_confirmed", False))
        selected = next(
            (item for item in candidates if isinstance(item, dict)),
            None,
        )
        if not isinstance(selected, dict) or not isinstance(selected.get("url"), str):
            return {"status": "error", "error": "No usable source candidate"}
        source_kind = selected.get("source_kind")
        if source_kind == "user_authorized" and not rights_confirmed:
            return {
                "status": "error",
                "error": "User-provided source requires rights_confirmed=true",
            }
        if usage in {"publish", "commercial"} and not rights_confirmed:
            return {
                "status": "error",
                "error": "Publishing or commercial use requires explicit rights confirmation",
            }
        if source_kind == "official_promotional" and not selected.get("official_channel"):
            return {"status": "error", "error": "Promotional source is not from a trusted channel"}
        basis = (
            "user_confirmed_authorization"
            if rights_confirmed
            else "official_public_promotional_source_for_internal_demo"
        )
        provenance = {
            "movie_title": message.payload.get("canonical_title"),
            "original_title": message.payload.get("original_title"),
            "selected_source": selected,
            "authorization_basis": basis,
            "usage": usage,
            "publication_allowed": rights_confirmed,
            "evidence": message.payload.get("source_evidence", []),
            "warnings": message.payload.get("source_warnings", []),
        }
        return {
            "status": "success",
            "content": f"Source approved for {usage}",
            "source": selected["url"],
            "description": message.payload.get("description"),
            "target_duration_seconds": message.payload.get("target_duration_seconds", 30),
            "hard_subtitles": message.payload.get("hard_subtitles", True),
            "source_provenance": provenance,
        }
