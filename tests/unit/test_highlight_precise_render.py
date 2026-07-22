"""Precise highlight filtergraph tests."""

from pathlib import Path

from axonflow.tools.video_edit import HighlightRenderTool


def test_highlight_render_uses_single_input_and_filter_level_trim(tmp_path) -> None:
    source = Path("/source.mp4")
    output = tmp_path / "output.mp4"
    arguments = HighlightRenderTool.build_arguments(
        source,
        [(1233, 2867), (4100, 5233)],
        output,
        has_audio=True,
        subtitle=None,
        width=1920,
        height=1080,
        fps=30,
    )

    assert arguments.count("-i") == 1
    assert "-ss" not in arguments
    assert "-t" not in arguments
    filtergraph = arguments[arguments.index("-filter_complex") + 1]
    assert "split=2[vsrc0][vsrc1]" in filtergraph
    assert "asplit=2[asrc0][asrc1]" in filtergraph
    assert "trim=start=1.233:end=2.867" in filtergraph
    assert "atrim=start=4.100:end=5.233" in filtergraph
