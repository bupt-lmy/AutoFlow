"""Title-driven official media discovery and rights-gate tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

from axonflow.agents.movie_discovery import (
    MediaRightsGateAgent,
    MovieTitleResolverAgent,
    OfficialMediaDiscoveryAgent,
)
from axonflow.config.models import AgentConfig
from axonflow.core.message import Message, MessageType
from axonflow.tools.base import Tool, ToolRegistry, ToolResult


def _message(payload: dict, receiver: str = "agent") -> Message:
    return Message(
        type=MessageType.TASK_REQUEST,
        sender="orchestrator",
        receiver=receiver,
        workflow_id="movie-discovery-test",
        payload=payload,
    )


class OfficialSearchFixtureTool(Tool):
    name = "official_video_search"
    description = "official video fixture"

    async def execute(self, **_kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            output=json.dumps(
                {
                    "candidates": [
                        {
                            "id": "official",
                            "title": "Marvel Studios Avengers Infinity War Official Trailer",
                            "url": "https://www.youtube.com/watch?v=official",
                            "channel": "Marvel Entertainment",
                            "channel_id": "marvel",
                            "duration_seconds": 150,
                            "view_count": 1000,
                        },
                        {
                            "id": "fan",
                            "title": "Avengers Infinity War fan trailer",
                            "url": "https://www.youtube.com/watch?v=fan",
                            "channel": "Random Fan",
                            "channel_id": "random",
                            "duration_seconds": 90,
                            "view_count": 9000,
                        },
                    ]
                }
            ),
        )


class WebSearchFixtureTool(Tool):
    name = "web_search"
    description = "official page fixture"

    async def execute(self, **_kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            output=json.dumps(
                [
                    {
                        "title": "Avengers: Infinity War",
                        "url": "https://www.marvel.com/movies/avengers-infinity-war",
                    }
                ]
            ),
        )


async def test_title_resolver_degrades_without_model_key(monkeypatch) -> None:
    monkeypatch.delenv("TEST_MOVIE_MODEL_KEY", raising=False)
    gateway = Mock()
    gateway.chat = AsyncMock(side_effect=AssertionError("model must not be called"))
    agent = MovieTitleResolverAgent(
        AgentConfig(
            id="resolver",
            name="Resolver",
            model={
                "provider": "minimax",
                "name": "MiniMax-M3",
                "api_key_env": "TEST_MOVIE_MODEL_KEY",
            },
            memory={"enabled": False},
        ),
        Mock(),
        gateway,
        ToolRegistry(),
    )

    result = await agent.handle_message(_message({"task": "复仇者联盟3"}))

    assert result["status"] == "success"
    assert result["canonical_title"] == "复仇者联盟3"
    assert "复仇者联盟3 official trailer" in result["search_queries"]
    assert "视觉冲击力" in result["description"]
    gateway.chat.assert_not_awaited()


async def test_discovery_keeps_official_channel_and_filters_fan_upload() -> None:
    registry = ToolRegistry()
    registry.register(OfficialSearchFixtureTool())
    registry.register(WebSearchFixtureTool())
    agent = OfficialMediaDiscoveryAgent(
        AgentConfig(
            id="discovery",
            name="Discovery",
            parameters={"trusted_channels": ["Marvel Entertainment"]},
            memory={"enabled": False},
        ),
        Mock(),
        Mock(),
        registry,
    )

    result = await agent.handle_message(
        _message(
            {
                "canonical_title": "复仇者联盟3：无限战争",
                "original_title": "Avengers: Infinity War",
                "aliases": ["复仇者联盟3"],
                "search_queries": ["Avengers Infinity War official trailer"],
                "description": "选择战斗高潮",
            }
        )
    )

    assert result["status"] == "success"
    assert len(result["source_candidates"]) == 1
    assert result["source_candidates"][0]["channel"] == "Marvel Entertainment"
    assert result["source_candidates"][0]["official_channel"] is True
    assert result["source_evidence"][0]["url"].startswith("https://www.marvel.com/")


async def test_rights_gate_allows_official_internal_demo() -> None:
    agent = MediaRightsGateAgent(
        AgentConfig(id="rights", name="Rights", memory={"enabled": False}),
        Mock(),
        Mock(),
        ToolRegistry(),
    )
    result = await agent.handle_message(
        _message(
            {
                "canonical_title": "复仇者联盟3：无限战争",
                "usage": "internal_demo",
                "source_candidates": [
                    {
                        "url": "https://www.youtube.com/watch?v=official",
                        "source_kind": "official_promotional",
                        "official_channel": True,
                    }
                ],
                "description": "选择战斗高潮",
                "target_duration_seconds": 30,
            }
        )
    )

    assert result["status"] == "success"
    assert result["source"].endswith("official")
    assert result["source_provenance"]["publication_allowed"] is False


async def test_rights_gate_rejects_unconfirmed_user_source_or_publication() -> None:
    agent = MediaRightsGateAgent(
        AgentConfig(id="rights", name="Rights", memory={"enabled": False}),
        Mock(),
        Mock(),
        ToolRegistry(),
    )
    user_source = await agent.handle_message(
        _message(
            {
                "source_candidates": [
                    {
                        "url": "/movies/infinity-war.mp4",
                        "source_kind": "user_authorized",
                    }
                ],
                "usage": "internal_demo",
            }
        )
    )
    publication = await agent.handle_message(
        _message(
            {
                "source_candidates": [
                    {
                        "url": "https://www.youtube.com/watch?v=official",
                        "source_kind": "official_promotional",
                        "official_channel": True,
                    }
                ],
                "usage": "publish",
            }
        )
    )

    assert user_source["status"] == "error"
    assert "rights_confirmed=true" in user_source["error"]
    assert publication["status"] == "error"
    assert "explicit rights confirmation" in publication["error"]
