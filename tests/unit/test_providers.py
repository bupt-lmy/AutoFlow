"""Provider metadata tests."""

from __future__ import annotations

from axonflow.llm.providers import provider_catalog


def test_provider_catalog_exposes_unique_common_model_suggestions() -> None:
    catalog = provider_catalog()
    providers = {item["id"]: item for item in catalog}

    assert "MiniMax-M3" in providers["minimax"]["common_models"]
    assert "MiniMax-M2.7-highspeed" in providers["minimax"]["common_models"]
    assert "MiniMax-M2.1-highspeed" in providers["minimax"]["common_models"]
    assert "M2-her" in providers["minimax"]["common_models"]
    assert "deepseek-v4-flash" in providers["deepseek"]["common_models"]
    assert providers["openai_compatible"]["common_models"] == []
    for provider in catalog:
        models = provider["common_models"]
        assert isinstance(models, list)
        assert len(models) == len(set(models))
