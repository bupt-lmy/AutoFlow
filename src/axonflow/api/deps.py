"""FastAPI 依赖注入"""

from __future__ import annotations

from pathlib import Path

from axonflow.engine import AxonFlowEngine
from axonflow.media.jobs import RenderJobRunner
from axonflow.media.storage import LocalMediaStorage
from axonflow.platform.store import PlatformStore

_engine: AxonFlowEngine | None = None
_config_dir: Path = Path("config")
_platform_store: PlatformStore | None = None
_media_storage: LocalMediaStorage | None = None
_render_job_runner: RenderJobRunner | None = None


def set_engine(engine: AxonFlowEngine) -> None:
    global _engine
    _engine = engine


def get_engine() -> AxonFlowEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized")
    return _engine


def set_config_dir(config_dir: Path) -> None:
    global _config_dir
    _config_dir = config_dir


def get_config_dir() -> Path:
    return _config_dir


def set_platform_store(store: PlatformStore) -> None:
    global _platform_store
    _platform_store = store


def get_platform_store() -> PlatformStore:
    if _platform_store is None:
        raise RuntimeError("Platform store not initialized")
    return _platform_store


def set_media_storage(storage: LocalMediaStorage) -> None:
    global _media_storage
    _media_storage = storage


def get_media_storage() -> LocalMediaStorage:
    if _media_storage is None:
        raise RuntimeError("Media storage not initialized")
    return _media_storage


def set_render_job_runner(runner: RenderJobRunner) -> None:
    global _render_job_runner
    _render_job_runner = runner


def get_render_job_runner() -> RenderJobRunner:
    if _render_job_runner is None:
        raise RuntimeError("Render job runner not initialized")
    return _render_job_runner
