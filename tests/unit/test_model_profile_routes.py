"""Model profile API boundary tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from axonflow.api.routes.model_profiles import ModelProfileRequest, _safe_profile_response


def test_model_profile_rejects_secret_in_environment_variable_field() -> None:
    with pytest.raises(ValidationError, match="environment variable name"):
        ModelProfileRequest.model_validate(
            {
                "name": "invalid-secret-profile",
                "config": {
                    "provider": "minimax",
                    "name": "MiniMax-M3",
                    "api_key_env": "sk-secret-value",
                },
            }
        )


def test_model_profile_response_masks_legacy_invalid_environment_value() -> None:
    response = _safe_profile_response(
        {
            "id": "model-1",
            "name": "legacy",
            "config": {"provider": "minimax", "name": "MiniMax-M3", "api_key_env": "sk-old"},
        }
    )

    assert response["config"]["api_key_env"] is None
    assert response["api_key_env_invalid"] is True
