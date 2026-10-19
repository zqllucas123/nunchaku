from __future__ import annotations

import hashlib
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .log import logger, preview
from .runner import MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, _safe_path, decode_content, encode_content
from .server import SkillResult

LOG = logger("bridge")
PROPOSED_DIR = (".nunchaku", "proposed")
# Tool _meta key the gateway uses to mark its service tools.
META_KEY = "nunchaku"


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
        content, encoding = encode_content(path.read_bytes())
        files.append({"path": relative, "content": content, "encoding": encoding})
    return files


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _stash_proposal(root: Path, change: dict[str, Any]) -> dict[str, Any]:
    """Keep base64 payloads out of the Agent's context: write them under .nunchaku/proposed/."""
    if change["encoding"] != "base64":
        return change
    target = root.joinpath(*PROPOSED_DIR, *_safe_path(change["path"]).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(decode_content(change["content"], "base64"))
    proposal = {key: value for key, value in change.items() if key != "content"}
    proposal["proposed_file"] = target.relative_to(root).as_posix()
    return proposal


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
            path.write_bytes(decode_content(change.content, change.encoding))
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
        proposed_changes=[] if applied or removed else [_stash_proposal(root, p) for p in proposals],
        proposed_deletions=[] if applied or removed else deletions,
    )


def _headers(token: str | None) -> dict[str, str] | None:
    return {"Authorization": f"Bearer {token}"} if token else None


async def invoke_remote(url: str, timeout_seconds: int, task: str, files: list[dict[str, str]],
                        tool: str = "run_skill", token: str | None = None) -> SkillResult:
    async with httpx.AsyncClient(timeout=timeout_seconds + 10, headers=_headers(token)) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                response = await client.call_tool(
                    tool,
                    {"task": task, "files": files},
                    read_timeout_seconds=timedelta(seconds=timeout_seconds + 10),
                )
    # Raise outside the client's task group, which would otherwise hide the message in an ExceptionGroup.
    if response.isError:
        detail = " ".join(getattr(item, "text", "") for item in response.content)
        raise RuntimeError(f"remote skill failed: {detail[:2000]}")
    if response.structuredContent is None:
        raise RuntimeError("remote service did not return structured content")
    return SkillResult.model_validate(response.structuredContent)


def build_bridge(name: str, description: str, url: str, root: Path, timeout_seconds: int = 180) -> FastMCP:
    root = _check_root(root)
    mcp = FastMCP(
        name=f"nunchaku-{name}",
        instructions=(
            f"Use run_skill for: {description}. Give project-relative file paths (text or binary); "
            "the local bridge reads them and applies returned changes after conflict checks."
        ),
    )
    _register(mcp, root, url, "run_skill", "run_skill", name, description, timeout_seconds)
    return mcp


def _check_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    return root


def _register(mcp: FastMCP, root: Path, url: str, local_name: str, remote_tool: str,
              service: str, description: str, timeout_seconds: int, token: str | None = None) -> None:
    async def execute(task: str, paths: list[str] | None = None, apply_changes: bool = True) -> BridgeResult:
        files = _snapshot(root, paths or [])
        LOG.info("[%s] call started: %d file(s) %s, apply_changes=%s, task=%r",
                 service, len(files), [item["path"] for item in files], apply_changes, preview(task))
        started = time.monotonic()
        try:
            remote = await invoke_remote(url, timeout_seconds, task, files, remote_tool, token)
        except Exception as exc:
            LOG.error("[%s] remote call failed after %.1fs: %s: %s",
                      service, time.monotonic() - started, type(exc).__name__, str(exc)[:400])
            raise
        remote_seconds = time.monotonic() - started
        if remote.service != service:
            raise RuntimeError(f"unexpected remote service: {remote.service}")
        result = _apply(root, remote, apply_changes)
        LOG.info("[%s] done in %.1fs (remote %.1fs): applied %s, deleted %s, conflicts %s",
                 service, time.monotonic() - started, remote_seconds,
                 result.applied_files, result.deleted_files, result.conflicts)
        if result.conflicts:
            LOG.warning("[%s] conflicts left for the Agent: %s", service, result.conflicts)
        return result

    mcp.add_tool(
        execute,
        name=local_name,
        structured_output=True,
        description=(
            f"{description} Execute the provider's private Skill remotely. "
            "Pass the user's task and paths of relevant local files (text or binary, e.g. .docx/.xlsx). "
            "The bridge sends their contents and applies returned changes locally by default. "
            "Set apply_changes=false to inspect proposed changes first; binary proposals are saved "
            "under .nunchaku/proposed/ (see proposed_file) instead of being returned inline."
        ),
    )


async def discover(url: str, token: str | None = None) -> list[types.Tool]:
    async with httpx.AsyncClient(timeout=30, headers=_headers(token)) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                return (await client.list_tools()).tools


def build_gateway_bridge(url: str, root: Path, tools: list[types.Tool], token: str | None = None) -> FastMCP:
    """Mirror every gateway tool locally, swapping file contents for project-relative paths."""
    root = _check_root(root)
    mcp = FastMCP(
        name="nunchaku",
        instructions=(
            "Each tool is a remote skill service; choose one by its description. Give project-relative "
            "file paths (text or binary); the local bridge reads them and applies returned changes "
            "after conflict checks."
        ),
    )
    for tool in tools:
        info = (tool.meta or {}).get(META_KEY)
        if not isinstance(info, dict):
            continue  # not a nunchaku service tool
        timeout = info.get("timeout_seconds", 180)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            timeout = 180
        _register(mcp, root, url, tool.name, tool.name, str(info.get("service", tool.name)),
                  str(info.get("description", "")), timeout + 15, token)
    return mcp


def serve_bridge(name: str, description: str, url: str, root: Path, timeout_seconds: int = 180) -> None:
    build_bridge(name, description, url, root, timeout_seconds).run(transport="stdio")


def serve_gateway_bridge(url: str, root: Path, token: str | None = None) -> None:
    root = _check_root(root)
    LOG.info("bridge starting: gateway=%s root=%s token=%s", url, root, "set" if token else "none")
    try:
        tools = anyio.run(discover, url, token)
    except Exception as exc:
        raise OSError(f"cannot reach gateway {url}: {_explain(exc)}") from exc
    names = [tool.name for tool in tools if (tool.meta or {}).get(META_KEY)]
    LOG.info("bridge discovered %d service tool(s): %s", len(names), names or "none")
    build_gateway_bridge(url, root, tools, token).run(transport="stdio")


def _explain(exc: BaseException) -> str:
    """Unwrap ExceptionGroups and name the usual HTTP failures."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return "401 Unauthorized: check NUNCHAKU_TOKEN"
        return f"HTTP {status}"
    return f"{type(exc).__name__}: {exc}"
