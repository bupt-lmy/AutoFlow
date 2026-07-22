"""Directory Skill package API tests."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi import HTTPException

from axonflow.api.routes import skills as skill_routes


def _file(path: str, content: str | bytes) -> skill_routes.SkillImportFile:
    raw = content.encode() if isinstance(content, str) else content
    return skill_routes.SkillImportFile(
        path=path,
        content_base64=base64.b64encode(raw).decode(),
    )


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "config"
    monkeypatch.setattr(skill_routes, "get_config_dir", lambda: directory)
    return directory


async def test_import_preserves_complete_skill_folder(config_dir: Path) -> None:
    response = await skill_routes.import_skill(
        skill_routes.SkillImportRequest(
            id="release-check",
            files=[
                _file(
                    "skill.md",
                    "---\nname: Release Check\ndescription: Validate a release.\n---\n# Steps",
                ),
                _file("scripts/verify.sh", "#!/bin/sh\necho ok\n"),
                _file("references/policy.md", "# Policy\nRequire tests."),
                _file("assets/logo.png", b"\x89PNG\x00binary"),
            ],
        )
    )

    package = config_dir / "skills" / "release-check"
    assert (package / "SKILL.md").is_file()
    assert (package / "scripts" / "verify.sh").read_text() == "#!/bin/sh\necho ok\n"
    assert (package / "assets" / "logo.png").read_bytes() == b"\x89PNG\x00binary"
    assert response["title"] == "Release Check"
    assert response["description"] == "Validate a release."
    assert response["file_count"] == 4
    assert response["components"] == ["assets", "references", "scripts"]
    assert response["has_scripts"] is True
    assert next(item for item in response["files"] if item["path"] == "assets/logo.png")[
        "binary"
    ] is True


async def test_import_requires_root_skill_document(config_dir: Path) -> None:
    with pytest.raises(HTTPException) as captured:
        await skill_routes.import_skill(
            skill_routes.SkillImportRequest(
                id="missing-entry",
                files=[_file("docs/SKILL.md", "# Nested only")],
            )
        )

    assert captured.value.status_code == 422
    assert "root" in captured.value.detail


async def test_import_rejects_path_traversal(config_dir: Path) -> None:
    with pytest.raises(HTTPException) as captured:
        await skill_routes.import_skill(
            skill_routes.SkillImportRequest(
                id="unsafe-skill",
                files=[
                    _file("SKILL.md", "# Safe entry"),
                    _file("scripts/../../outside.sh", "bad"),
                ],
            )
        )

    assert captured.value.status_code == 422
    assert "Unsafe" in captured.value.detail
    assert not (config_dir / "outside.sh").exists()


async def test_import_does_not_replace_existing_skill_without_consent(config_dir: Path) -> None:
    original = skill_routes.SkillImportRequest(
        id="existing-skill",
        files=[_file("SKILL.md", "# Original")],
    )
    await skill_routes.import_skill(original)

    with pytest.raises(HTTPException) as captured:
        await skill_routes.import_skill(
            skill_routes.SkillImportRequest(
                id="existing-skill",
                files=[_file("SKILL.md", "# Replacement")],
            )
        )

    assert captured.value.status_code == 409
    entry = config_dir / "skills" / "existing-skill" / "SKILL.md"
    assert entry.read_text() == "# Original"


async def test_import_can_explicitly_replace_complete_package(config_dir: Path) -> None:
    await skill_routes.import_skill(
        skill_routes.SkillImportRequest(
            id="replace-skill",
            files=[_file("SKILL.md", "# Original"), _file("assets/old.txt", "old")],
        )
    )

    response = await skill_routes.import_skill(
        skill_routes.SkillImportRequest(
            id="replace-skill",
            overwrite=True,
            files=[_file("SKILL.md", "# Replacement"), _file("scripts/new.py", "print('ok')")],
        )
    )

    package = config_dir / "skills" / "replace-skill"
    assert not (package / "assets" / "old.txt").exists()
    assert (package / "scripts" / "new.py").is_file()
    assert response["title"] == "Replacement"


async def test_text_files_can_be_read_updated_and_deleted(config_dir: Path) -> None:
    await skill_routes.import_skill(
        skill_routes.SkillImportRequest(
            id="editable-skill",
            files=[_file("SKILL.md", "# Editable"), _file("references/guide.md", "old")],
        )
    )

    loaded = await skill_routes.get_skill_file("editable-skill", "references/guide.md")
    assert loaded["content"] == "old"
    assert loaded["binary"] is False

    response = await skill_routes.update_skill_file(
        "editable-skill",
        "references/guide.md",
        skill_routes.SkillFileRequest(content="new guidance"),
    )
    assert response["file_count"] == 2
    assert (
        config_dir / "skills" / "editable-skill" / "references" / "guide.md"
    ).read_text() == "new guidance"

    await skill_routes.delete_skill_file("editable-skill", "references/guide.md")
    assert response["file_count"] == 2
    assert not (config_dir / "skills" / "editable-skill" / "references").exists()


async def test_required_entrypoint_cannot_be_deleted(config_dir: Path) -> None:
    await skill_routes.create_skill(
        "protected-skill",
        skill_routes.SkillRequest(content="# Protected"),
    )

    with pytest.raises(HTTPException) as captured:
        await skill_routes.delete_skill_file("protected-skill", "SKILL.md")

    assert captured.value.status_code == 409
