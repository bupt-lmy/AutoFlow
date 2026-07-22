"""Semantic source-video edit workflow configuration tests."""

from pathlib import Path

from axonflow.config.loader import load_agent_config, load_workflow_config


def test_semantic_video_edit_workflow_loads() -> None:
    root = Path(__file__).parents[2]
    workflow = load_workflow_config(root / "config/workflows/semantic-video-edit.yaml")
    files = [
        "video-ingest",
        "video-scene-analysis",
        "video-scene-features",
        "source-transcript",
        "video-highlight-scoring",
        "video-highlight-selector",
        "video-interval-refiner",
        "video-highlight-renderer",
        "video-transcription",
        "hard-subtitle",
        "media-quality",
        "media-asset-register",
    ]
    agents = [load_agent_config(root / f"config/agents/{name}.yaml") for name in files]

    assert workflow.flow.entry == "agent-video-ingest"
    assert [agent.id for agent in agents] == workflow.agents
    assert agents[2].tools == ["video_scene_features"]
    assert agents[3].tools == ["video_transcribe"]
    assert agents[4].model.name == "MiniMax-M3"
    assert agents[4].model.api_key_env == "MINIMAX_API_KEY"
    assert agents[4].model.credential_id is None
    assert agents[4].class_path == "axonflow.agents.video_edit.VideoHighlightScoringAgent"
    assert agents[5].class_path == "axonflow.agents.video_edit.VideoHighlightSelectorAgent"
    assert agents[5].model.api_key_env == "MINIMAX_API_KEY"
    assert agents[5].model.credential_id is None
    assert agents[7].tools == ["highlight_render"]
    assert agents[8].tools == ["video_transcribe", "subtitle_create"]
    assert agents[9].tools == ["hard_subtitle_burn"]
    join = workflow.flow.join["agent-video-highlight-scoring"]
    assert join.strategy == "all"
    assert join.wait_for == ["agent-video-scene-features", "agent-source-transcript"]
    assert workflow.flow.terminate_on[0] == {
        "agent": "agent-media-asset-register",
        "status": "success",
    }
    assert len(workflow.flow.terminate_on) == 13
