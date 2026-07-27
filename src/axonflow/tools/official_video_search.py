"""Search public video indexes without downloading the selected media."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from axonflow.tools.base import Tool, ToolResult


class OfficialVideoSearchTool(Tool):
    """Use yt-dlp's search extractor to return auditable video candidates."""

    name = "official_video_search"
    description = "搜索公开视频候选并返回标题、频道、时长和可下载页面地址，不下载视频"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "default": 8},
        },
        "required": ["query"],
    }

    async def execute(
        self,
        query: str,
        max_results: int = 8,
        timeout: int = 180,
        **_kwargs: Any,
    ) -> ToolResult:
        query = query.strip()
        if not query:
            return ToolResult(success=False, error="Official video search requires a query")
        if not 1 <= max_results <= 20:
            return ToolResult(success=False, error="max_results must be between 1 and 20")
        yt_dlp = shutil.which("yt-dlp")
        if yt_dlp is None:
            return ToolResult(success=False, error="yt-dlp is required for video search")
        process = await asyncio.create_subprocess_exec(
            yt_dlp,
            "--flat-playlist",
            "--dump-single-json",
            "--no-warnings",
            "--playlist-end",
            str(max_results),
            f"ytsearch{max_results}:{query}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(success=False, error="Official video search timed out")
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            return ToolResult(
                success=False,
                error=f"Official video search failed: {detail[-1000:]}",
            )
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ToolResult(success=False, error=f"Invalid video search response: {exc}")
        entries = payload.get("entries", [])
        if not isinstance(entries, list):
            entries = []
        candidates: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("id")
            url = entry.get("webpage_url") or entry.get("url")
            if isinstance(video_id, str) and (
                not isinstance(url, str) or not url.startswith("http")
            ):
                url = f"https://www.youtube.com/watch?v={video_id}"
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            candidates.append(
                {
                    "id": video_id,
                    "title": entry.get("title") or "",
                    "url": url,
                    "channel": entry.get("channel") or entry.get("uploader") or "",
                    "channel_id": entry.get("channel_id") or entry.get("uploader_id") or "",
                    "channel_url": entry.get("channel_url") or entry.get("uploader_url"),
                    "duration_seconds": entry.get("duration"),
                    "view_count": entry.get("view_count"),
                    "source_platform": entry.get("extractor_key") or "YouTube",
                    "search_query": query,
                }
            )
        return ToolResult(
            success=True,
            output=json.dumps({"query": query, "candidates": candidates}, ensure_ascii=False),
        )
