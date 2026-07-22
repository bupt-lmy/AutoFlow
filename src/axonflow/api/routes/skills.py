"""Managed directory-based Skill packages."""

from __future__ import annotations

import base64
import binascii
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from axonflow.api.deps import get_config_dir

router = APIRouter(prefix="/api/skills", tags=["skills"])

_SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_MAX_ENTRY_BYTES = 100_000
_MAX_TEXT_FILE_BYTES = 1_000_000
_MAX_FILE_BYTES = 5_000_000
_MAX_PACKAGE_BYTES = 20_000_000
_MAX_PACKAGE_FILES = 500


class SkillRequest(BaseModel):
    content: str = Field(min_length=1, max_length=_MAX_ENTRY_BYTES)


class SkillFileRequest(BaseModel):
    content: str = Field(max_length=_MAX_TEXT_FILE_BYTES)


class SkillImportFile(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content_base64: str = Field(max_length=7_000_000)


class SkillImportRequest(BaseModel):
    id: str
    files: list[SkillImportFile] = Field(min_length=1, max_length=_MAX_PACKAGE_FILES)
    overwrite: bool = False


def _skills_dir() -> Path:
    return get_config_dir() / "skills"


def _validate_id(skill_id: str) -> None:
    if not _SKILL_ID.fullmatch(skill_id):
        raise HTTPException(status_code=422, detail="Invalid Skill ID")


def _skill_path(skill_id: str) -> Path:
    _validate_id(skill_id)
    return _skills_dir() / skill_id


def _document_path(skill_id: str) -> tuple[Path, Path]:
    path = _skill_path(skill_id)
    if path.is_dir() and (path / "SKILL.md").is_file():
        return path, path / "SKILL.md"
    legacy = path.with_suffix(".md")
    if legacy.is_file():
        return legacy, legacy
    raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")


def _safe_relative_path(raw_path: str) -> PurePosixPath:
    normalized = raw_path.replace("\\", "/").strip("/")
    raw_parts = normalized.split("/")
    if (
        not normalized
        or raw_path.startswith(("/", "\\"))
        or any(part in {"", ".", ".."} for part in raw_parts)
        or raw_parts[0].endswith(":")
        or "\x00" in normalized
    ):
        raise HTTPException(status_code=422, detail=f"Unsafe Skill file path: {raw_path}")
    if any(part in {".git", "__pycache__"} for part in raw_parts):
        raise HTTPException(status_code=422, detail=f"Unsupported Skill file path: {raw_path}")
    return PurePosixPath(*raw_parts)


def _target_in_package(package: Path, relative: PurePosixPath) -> Path:
    target = (package / relative.as_posix()).resolve()
    package_root = package.resolve()
    if target != package_root and not target.is_relative_to(package_root):
        raise HTTPException(status_code=422, detail="Skill file path escapes its package")
    return target


def _package_files(path: Path) -> list[dict[str, Any]]:
    if path.is_file():
        return [
            {
                "path": "SKILL.md",
                "size": path.stat().st_size,
                "kind": "entry",
                "binary": False,
            }
        ]
    files: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if not item.is_file() or item.is_symlink():
            continue
        relative = item.relative_to(path).as_posix()
        files.append(
            {
                "path": relative,
                "size": item.stat().st_size,
                "kind": _file_kind(relative),
                "binary": _is_binary(item),
            }
        )
    return files


def _file_kind(relative_path: str) -> str:
    if relative_path == "SKILL.md":
        return "entry"
    first = relative_path.split("/", 1)[0]
    return first if first in {"scripts", "references", "assets"} else "file"


def _is_binary(path: Path) -> bool:
    with path.open("rb") as stream:
        sample = stream.read(8192)
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _entry_metadata(content: str, skill_id: str) -> tuple[str, str]:
    title = skill_id
    description = ""
    body = content
    if content.startswith("---\n"):
        closing = content.find("\n---", 4)
        if closing >= 0:
            try:
                frontmatter = yaml.safe_load(content[4:closing]) or {}
            except yaml.YAMLError:
                frontmatter = {}
            if isinstance(frontmatter, dict):
                title = str(frontmatter.get("name") or title)
                description = str(frontmatter.get("description") or "").strip()
            body = content[closing + 4 :]
    if title == skill_id:
        heading = next(
            (line.lstrip("#").strip() for line in body.splitlines() if line.startswith("#")),
            "",
        )
        if heading:
            title = heading
    if not description:
        paragraphs = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not line.startswith(("#", "```", "---"))
        ]
        description = paragraphs[0] if paragraphs else ""
    return title[:120], description[:500]


def _response(skill_id: str, path: Path) -> dict[str, Any]:
    document = path / "SKILL.md" if path.is_dir() else path
    if not document.exists():
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
    try:
        content = document.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="SKILL.md must be UTF-8 text") from exc
    files = _package_files(path)
    title, description = _entry_metadata(content, skill_id)
    components = sorted({item["kind"] for item in files if item["kind"] != "entry"})
    return {
        "id": skill_id,
        "title": title,
        "description": description,
        "content": content,
        "entrypoint": "SKILL.md",
        "files": files,
        "file_count": len(files),
        "total_size": sum(item["size"] for item in files),
        "components": components,
        "has_scripts": "scripts" in components,
        "has_references": "references" in components,
        "has_assets": "assets" in components,
    }


def _decode_import_files(files: list[SkillImportFile]) -> dict[str, bytes]:
    decoded: dict[str, bytes] = {}
    seen_paths: set[str] = set()
    total_size = 0
    entry_seen = False
    for item in files:
        relative = _safe_relative_path(item.path)
        path = relative.as_posix()
        if path.casefold() == "skill.md":
            path = "SKILL.md"
            entry_seen = True
        if path.casefold() in seen_paths:
            raise HTTPException(status_code=422, detail=f"Duplicate Skill file path: {path}")
        seen_paths.add(path.casefold())
        try:
            content = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid base64 for Skill file: {path}",
            ) from exc
        if len(content) > _MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Skill file is too large: {path}")
        total_size += len(content)
        if total_size > _MAX_PACKAGE_BYTES:
            raise HTTPException(status_code=413, detail="Skill package exceeds 20 MB")
        decoded[path] = content
    if not entry_seen:
        raise HTTPException(
            status_code=422,
            detail="Imported folder must contain SKILL.md at its root",
        )
    try:
        entry = decoded["SKILL.md"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="SKILL.md must be UTF-8 text") from exc
    if not entry.strip():
        raise HTTPException(status_code=422, detail="SKILL.md cannot be blank")
    if len(decoded["SKILL.md"]) > _MAX_ENTRY_BYTES:
        raise HTTPException(status_code=413, detail="SKILL.md exceeds 100 KB")
    return decoded


@router.get("")
async def list_skills() -> list[dict[str, Any]]:
    directory = _skills_dir()
    if not directory.exists():
        return []
    skills = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith("."):
            continue
        if path.is_dir() and (path / "SKILL.md").exists():
            skills.append(_response(path.name, path))
        elif path.is_file() and path.suffix == ".md":
            skills.append(_response(path.stem, path))
    return skills


@router.post("/import", status_code=201)
async def import_skill(body: SkillImportRequest) -> dict[str, Any]:
    _validate_id(body.id)
    decoded = _decode_import_files(body.files)
    target = _skill_path(body.id)
    legacy = target.with_suffix(".md")
    if (target.exists() or legacy.exists()) and not body.overwrite:
        raise HTTPException(status_code=409, detail=f"Skill already exists: {body.id}")

    skills_dir = _skills_dir()
    skills_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{body.id}-", dir=skills_dir))
    try:
        for relative, content in decoded.items():
            destination = temporary / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if legacy.exists():
            legacy.unlink()
        temporary.replace(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _response(body.id, target)


@router.post("/{skill_id}", status_code=201)
async def create_skill(skill_id: str, body: SkillRequest) -> dict[str, Any]:
    path = _skill_path(skill_id)
    if path.exists() or path.with_suffix(".md").exists():
        raise HTTPException(status_code=409, detail=f"Skill already exists: {skill_id}")
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(body.content, encoding="utf-8")
    return _response(skill_id, path)


@router.get("/{skill_id}/files/{file_path:path}")
async def get_skill_file(skill_id: str, file_path: str) -> dict[str, Any]:
    path, _ = _document_path(skill_id)
    relative = _safe_relative_path(file_path)
    if path.is_file():
        if relative.as_posix() != "SKILL.md":
            raise HTTPException(status_code=404, detail="Skill file not found")
        target = path
    else:
        target = _target_in_package(path, relative)
    if not target.is_file() or target.is_symlink():
        raise HTTPException(status_code=404, detail="Skill file not found")
    size = target.stat().st_size
    binary = _is_binary(target)
    return {
        "path": relative.as_posix(),
        "size": size,
        "binary": binary,
        "content": None if binary or size > _MAX_TEXT_FILE_BYTES else target.read_text("utf-8"),
    }


@router.put("/{skill_id}/files/{file_path:path}")
async def update_skill_file(
    skill_id: str,
    file_path: str,
    body: SkillFileRequest,
) -> dict[str, Any]:
    path, _ = _document_path(skill_id)
    relative = _safe_relative_path(file_path)
    if path.is_file():
        if relative.as_posix() != "SKILL.md":
            raise HTTPException(status_code=409, detail="Legacy Skills only contain SKILL.md")
        target = path
    else:
        target = _target_in_package(path, relative)
    if relative.as_posix() == "SKILL.md" and not body.content.strip():
        raise HTTPException(status_code=422, detail="SKILL.md cannot be blank")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    package_path = path if path.is_dir() else path
    return _response(skill_id, package_path)


@router.delete("/{skill_id}/files/{file_path:path}", status_code=204)
async def delete_skill_file(skill_id: str, file_path: str) -> None:
    path, _ = _document_path(skill_id)
    relative = _safe_relative_path(file_path)
    if relative.as_posix() == "SKILL.md":
        raise HTTPException(status_code=409, detail="SKILL.md is the required Skill entrypoint")
    if path.is_file():
        raise HTTPException(status_code=404, detail="Skill file not found")
    target = _target_in_package(path, relative)
    if not target.is_file() or target.is_symlink():
        raise HTTPException(status_code=404, detail="Skill file not found")
    target.unlink()
    parent = target.parent
    while parent != path and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


@router.get("/{skill_id}")
async def get_skill(skill_id: str) -> dict[str, Any]:
    path, _ = _document_path(skill_id)
    return _response(skill_id, path)


@router.put("/{skill_id}")
async def update_skill(skill_id: str, body: SkillRequest) -> dict[str, Any]:
    path, document = _document_path(skill_id)
    document.write_text(body.content, encoding="utf-8")
    return _response(skill_id, path)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: str) -> None:
    path = _skill_path(skill_id)
    file_path = path.with_suffix(".md")
    if path.is_dir():
        shutil.rmtree(path)
        return
    if file_path.exists():
        file_path.unlink()
        return
    raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
