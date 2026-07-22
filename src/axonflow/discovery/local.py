"""Deterministic local ADP-lite discovery over registered Agent manifests."""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from axonflow.config.models import AgentConfig
from axonflow.core.protocol import DelegationRequest
from axonflow.platform.models import AgentManifest


class DiscoveryCandidate(BaseModel):
    agent_id: str
    score: float
    matched_terms: list[str] = Field(default_factory=list)
    reason: str = ""


def _terms(value: str) -> set[str]:
    normalized = value.casefold()
    latin = set(re.findall(r"[a-z0-9][a-z0-9._+-]*", normalized))
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk: set[str] = set()
    for run in cjk_runs:
        cjk.update(run[index : index + 2] for index in range(max(1, len(run) - 1)))
        cjk.update(char for char in run if len(run) == 1)
    return latin | cjk


class LocalDiscoveryService:
    """Discover local Agents by hard constraints plus lexical capability relevance."""

    def __init__(self, agent_configs: Iterable[AgentConfig]) -> None:
        self._configs = {config.id: config for config in agent_configs}

    def discover(self, request: DelegationRequest) -> list[DiscoveryCandidate]:
        query_terms = _terms(
            " ".join(
                [
                    request.description,
                    *request.required_skills,
                    *request.required_tools,
                    *request.tags,
                ]
            )
        )
        excluded = set(request.exclude_agents)
        candidates: list[DiscoveryCandidate] = []
        for config in self._configs.values():
            if config.id in excluded:
                continue
            manifest = AgentManifest.from_agent_config(config)
            if not set(request.required_tools).issubset(manifest.tools):
                continue
            if not set(request.required_skills).issubset(manifest.skills):
                continue
            if request.tags and not set(request.tags).intersection(manifest.tags):
                continue

            capability_text = " ".join(
                [
                    manifest.name,
                    manifest.description,
                    *manifest.tags,
                    *manifest.tools,
                    *manifest.skills,
                ]
            )
            capability_terms = _terms(capability_text)
            matched = sorted(query_terms.intersection(capability_terms))
            lexical = len(matched) / max(1, len(query_terms))
            phrase_bonus = (
                0.2 if request.description.casefold() in capability_text.casefold() else 0
            )
            constraint_bonus = min(
                0.25,
                0.05
                * (
                    len(request.required_tools)
                    + len(request.required_skills)
                    + len(request.tags)
                ),
            )
            score = min(1.0, lexical + phrase_bonus + constraint_bonus)
            if score < request.min_score:
                continue
            candidates.append(
                DiscoveryCandidate(
                    agent_id=config.id,
                    score=round(score, 4),
                    matched_terms=matched,
                    reason=(
                        f"matched {len(matched)} capability terms"
                        f"; tools={len(request.required_tools)}"
                        f"; skills={len(request.required_skills)}"
                    ),
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.agent_id))
        return candidates[: request.max_candidates]

    def get_config(self, agent_id: str) -> AgentConfig | None:
        return self._configs.get(agent_id)
