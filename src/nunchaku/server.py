from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .config import Service
from .runner import MAX_TOTAL_BYTES, run_skill

# Base64 inflates payloads by 4/3; leave headroom for JSON-RPC framing.
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES * 4 // 3 + 1_000_000


class ChangedFile(BaseModel):
    path: str
    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    base_sha256: str | None


class DeletedFile(BaseModel):
    path: str
    base_sha256: str


class SkillResult(BaseModel):
    service: str
    engine: str
    answer: str
    changed_files: list[ChangedFile]
    deleted_files: list[DeletedFile]
    note: str


def build_server(service: Service) -> FastMCP:
    mcp = FastMCP(
        name=f"nunchaku-{service.name}",
        instructions=(
            f"Use run_skill when the user asks for: {service.description}. "
            "Send only relevant workspace files in files; binary files use encoding=base64. "
            "Review and apply returned changed_files and deleted_files locally."
        ),
        host=service.host,
        port=service.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BYTES,
    )

    @mcp.tool(
        name="run_skill",
        structured_output=True,
        description=(
            f"{service.description} Runs the private skill on the provider's server. "
            "Provide the user's task and any relevant files from the local workspace: each file is "
            "{path, content, encoding}, where encoding is utf-8 (default) for text or base64 for binary files "
            "such as .docx/.xlsx. "
            "Returns an answer and proposed file changes (same encoding convention) to review and apply locally. "
            "File paths must be relative to the local project root; do not send secrets or unrelated files."
        ),
    )
    async def execute(task: str, files: list[dict[str, str]] | None = None) -> SkillResult:
        return SkillResult.model_validate(await run_skill(service, task, files or []))

    return mcp


def serve(service: Service) -> None:
    build_server(service).run(transport="streamable-http")
