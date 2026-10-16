from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from .config import Service
from .runner import run_skill


class ChangedFile(BaseModel):
    path: str
    content: str
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
            "Send only relevant UTF-8 workspace files in files. "
            "Review and apply returned changed_files and deleted_files locally."
        ),
        host=service.host,
        port=service.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

    @mcp.tool(
        name="run_skill",
        structured_output=True,
        description=(
            f"{service.description} Runs the private skill on the provider's server. "
            "Provide the user's task and any relevant UTF-8 files from the local workspace. "
            "Returns an answer and proposed file changes to review and apply locally. "
            "File paths must be relative to the local project root; do not send secrets or unrelated files."
        ),
    )
    async def execute(task: str, files: list[dict[str, str]] | None = None) -> SkillResult:
        return SkillResult.model_validate(await run_skill(service, task, files or []))

    return mcp


def serve(service: Service) -> None:
    build_server(service).run(transport="streamable-http")
