from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .runner import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, _safe_path
from .server import SkillResult


class BridgeResult(BaseModel):
    answer: str
    service: str
    engine: str
    applied_files: list[str]
    deleted_files: list[str]
    conflicts: list[str]
    proposed_changes: list[dict[str, Any]]
    proposed_deletions: list[dict[str, Any]]


def _target(root: Path, relative: str) -> Path:
    path = _safe_path(relative)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink paths are unsupported: {relative}")
    return current


def _snapshot(root: Path, paths: list[str]) -> list[dict[str, str]]:
    if len(paths) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} files may be sent")
    files: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for relative in paths:
        path = _target(root, relative)
        if relative in seen:
            raise ValueError(f"duplicate path: {relative}")
        seen.add(relative)
        if not path.is_file():
            raise ValueError(f"not a regular file: {relative}")
        size = path.stat().st_size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("file payload exceeds POC size limit")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"binary file is unsupported: {relative}") from exc
        files.append({"path": relative, "content": content})
    return files


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _apply(root: Path, result: SkillResult, apply_changes: bool) -> BridgeResult:
    proposals = [change.model_dump() for change in result.changed_files]
    deletions = [deletion.model_dump() for deletion in result.deleted_files]
    conflicts: list[str] = []
    for change in result.changed_files:
        path = _target(root, change.path)
        if _digest(path) != change.base_sha256:
            conflicts.append(change.path)
    for deletion in result.deleted_files:
        path = _target(root, deletion.path)
        if _digest(path) != deletion.base_sha256:
            conflicts.append(deletion.path)

    applied: list[str] = []
    removed: list[str] = []
    if apply_changes and not conflicts:
        for change in result.changed_files:
            path = _target(root, change.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(change.content, encoding="utf-8")
            applied.append(change.path)
        for deletion in result.deleted_files:
            _target(root, deletion.path).unlink()
            removed.append(deletion.path)

    return BridgeResult(
        answer=result.answer,
        service=result.service,
        engine=result.engine,
        applied_files=applied,
        deleted_files=removed,
        conflicts=conflicts,
        proposed_changes=[] if applied or removed else proposals,
        proposed_deletions=[] if applied or removed else deletions,
    )


async def invoke_remote(url: str, timeout_seconds: int, task: str, files: list[dict[str, str]]) -> SkillResult:
    async with httpx.AsyncClient(timeout=timeout_seconds + 10) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                response = await client.call_tool(
                    "run_skill",
                    {"task": task, "files": files},
                    read_timeout_seconds=timedelta(seconds=timeout_seconds + 10),
                )
                if response.isError:
                    detail = " ".join(getattr(item, "text", "") for item in response.content)
                    raise RuntimeError(f"remote skill failed: {detail[:2000]}")
                if response.structuredContent is None:
                    raise RuntimeError("remote service did not return structured content")
                return SkillResult.model_validate(response.structuredContent)


def build_bridge(name: str, description: str, url: str, root: Path, timeout_seconds: int = 180) -> FastMCP:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    mcp = FastMCP(
        name=f"nunchaku-{name}",
        instructions=(
            f"Use run_skill for: {description}. Give project-relative text file paths; "
            "the local bridge reads them and applies returned changes after conflict checks."
        ),
    )

    @mcp.tool(
        name="run_skill",
        structured_output=True,
        description=(
            f"{description} Execute the provider's private Skill remotely. "
            "Pass the user's task and paths of relevant local UTF-8 files. "
            "The bridge sends their contents and applies returned changes locally by default. "
            "Set apply_changes=false to inspect proposed changes first."
        ),
    )
    async def execute(task: str, paths: list[str] | None = None, apply_changes: bool = True) -> BridgeResult:
        files = _snapshot(root, paths or [])
        remote = await invoke_remote(url, timeout_seconds, task, files)
        if remote.service != name:
            raise RuntimeError(f"unexpected remote service: {remote.service}")
        return _apply(root, remote, apply_changes)

    return mcp


def serve_bridge(name: str, description: str, url: str, root: Path, timeout_seconds: int = 180) -> None:
    build_bridge(name, description, url, root, timeout_seconds).run(transport="stdio")
