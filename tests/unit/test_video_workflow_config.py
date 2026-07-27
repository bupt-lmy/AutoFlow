"""The first video workflow remains loadable and structurally safe."""

from pathlib import Path

from axonflow.config.loader import load_agent_config, load_workflow_config


def test_video_edit_mvp_configuration_loads() -> None:
    root = Path(__file__).parents[2]
    workflow = load_workflow_config(root / "config/workflows/video-edit-mvp.yaml")
    agents = [
        load_agent_config(root / f"config/agents/{name}.yaml")
        for name in ["media-inspector", "timeline-planner", "media-renderer"]
    ]

    assert workflow.id == "video-edit-mvp"
    assert workflow.flow.entry == "agent-media-inspector"
    assert workflow.flow.routes["agent-timeline-planner"][0].payload_mapping is not None
    assert [agent.id for agent in agents] == workflow.agents
    assert agents[0].class_path == "axonflow.agents.media.MediaInspectorAgent"
    assert agents[1].model.provider == "minimax"
    assert agents[1].model.name == "MiniMax-M3"
    assert agents[1].model.credential_id == "cred-e29c18f8584a"
    assert agents[2].tools == ["media_render"]


def test_minimax_asset_generation_workflow_configuration_loads() -> None:
    root = Path(__file__).parents[2]
    workflow = load_workflow_config(root / "config/workflows/video-asset-generation-minimax.yaml")
    agent_files = [
        "minimax-creative-brief-planner",
        "minimax-image-generator",
        "minimax-narration-generator",
        "minimax-music-generator",
        "media-asset-manifest",
        "subtitle-generator",
        "media-composer",
        "media-quality",
        "media-asset-register",
    ]
    agents = [load_agent_config(root / f"config/agents/{name}.yaml") for name in agent_files]

    assert workflow.flow.entry == "agent-minimax-creative-brief-planner"
    assert [agent.id for agent in agents] == workflow.agents
    assert workflow.flow.join["agent-media-asset-manifest"].strategy == "all"
    planner_routes = workflow.flow.routes["agent-minimax-creative-brief-planner"]
    assert {route.target for route in planner_routes} == {
        "agent-minimax-image-generator",
        "agent-minimax-narration-generator",
        "agent-minimax-music-generator",
    }
    assert all(route.payload_mapping is not None for route in planner_routes)
    assert agents[0].model.name == "MiniMax-M3"
    by_id = {agent.id: agent for agent in agents}
    assert by_id["agent-media-asset-manifest"].class_path == (
        "axonflow.agents.media.MediaAssetManifestAgent"
    )
    assert workflow.flow.terminate_on == [
        {"agent": "agent-media-asset-register", "status": "success"}
    ]
    assert by_id["agent-subtitle-generator"].class_path == "axonflow.agents.media.SubtitleAgent"
    assert by_id["agent-media-composer"].class_path == "axonflow.agents.media.MediaComposerAgent"
    assert by_id["agent-media-quality"].class_path == "axonflow.agents.media.MediaQualityAgent"
    assert by_id["agent-media-asset-register"].class_path == (
        "axonflow.agents.media.MediaAssetRegisterAgent"
    )
