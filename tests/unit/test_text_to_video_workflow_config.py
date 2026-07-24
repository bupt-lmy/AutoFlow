"""Independent edit and generated-video workflow configuration tests."""

from pathlib import Path

from axonflow.config.loader import load_agent_config, load_workflow_config


def test_text_to_video_generation_workflow_is_independent_from_source_editing() -> None:
    root = Path(__file__).parents[2]
    generated = load_workflow_config(root / "config/workflows/text-to-video-generation.yaml")
    edited = load_workflow_config(root / "config/workflows/semantic-video-edit.yaml")
    files = [
        "generation-resource-search",
        "text-video-prompt-planner",
        "minimax-storyboard-generator",
        "storyboard-motion-renderer",
        "minimax-video-generator",
        "generated-video-disclosure",
        "media-quality",
        "media-asset-register",
    ]
    agents = [load_agent_config(root / f"config/agents/{name}.yaml") for name in files]

    assert generated.flow.entry == "agent-generation-resource-search"
    assert [agent.id for agent in agents] == generated.agents
    assert agents[1].model.name == "MiniMax-M3"
    assert agents[2].tools == ["minimax_image_generate"]
    assert agents[3].tools == ["storyboard_motion_render"]
    assert agents[4].tools == ["minimax_video_generate"]
    assert agents[5].tools == ["generated_video_finalize"]
    assert "agent-video-ingest" not in generated.agents
    assert "agent-minimax-video-generator" not in edited.agents
    assert edited.id == "semantic-video-edit"
    assert generated.id == "text-to-video-generation"
