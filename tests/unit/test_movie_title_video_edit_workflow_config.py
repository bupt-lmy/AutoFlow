"""Configuration contract for title-driven official-source editing."""

from pathlib import Path

from axonflow.config.loader import load_agent_config, load_workflow_config


def test_movie_title_video_edit_workflow_loads() -> None:
    root = Path(__file__).parents[2]
    workflow = load_workflow_config(
        root / "config/workflows/movie-title-video-edit.yaml"
    )
    discovery_agents = [
        load_agent_config(root / "config/agents/movie-title-resolver.yaml"),
        load_agent_config(root / "config/agents/official-media-discovery.yaml"),
        load_agent_config(root / "config/agents/media-rights-gate.yaml"),
    ]

    assert workflow.flow.entry == "agent-movie-title-resolver"
    assert workflow.agents[:3] == [agent.id for agent in discovery_agents]
    assert workflow.agents[3] == "agent-video-ingest"
    assert workflow.agents[-1] == "agent-media-asset-register"
    assert discovery_agents[0].model.name == "MiniMax-M3"
    assert discovery_agents[1].tools == ["official_video_search", "web_search"]
    assert workflow.flow.routes["agent-media-rights-gate"][0].target == "agent-video-ingest"
    join = workflow.flow.join["agent-video-highlight-scoring"]
    assert join.wait_for == ["agent-video-scene-features", "agent-source-transcript"]
    assert len(workflow.flow.terminate_on) == 16
