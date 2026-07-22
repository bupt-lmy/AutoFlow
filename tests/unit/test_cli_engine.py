"""CLI engine construction tests."""

from __future__ import annotations

from axonflow.cli.app import _get_engine


def test_cli_engine_uses_workspace_platform_store(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "axonflow.yaml").write_text(
        'workspace_dir: "./workspace"\nagent_health:\n  enabled: false\n',
        encoding="utf-8",
    )

    engine, store = _get_engine(str(config_dir))

    try:
        assert engine._platform_store is store
        assert (tmp_path / "workspace" / "axonflow.db").is_file()
    finally:
        store.close()
