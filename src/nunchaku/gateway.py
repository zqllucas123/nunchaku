"""Single public MCP endpoint in front of the per-service processes.

Each configured service keeps its own process and port. The gateway lists one
tool per reachable service and forwards ``tools/call`` by tool name.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from mcp import types
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route

from .bridge import META_KEY, invoke_remote
from .config import Gateway, Service, load_gateway, load_services
from .server import MAX_REQUEST_BYTES

HEALTH_TTL_SECONDS = 3.0

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "The user's task in natural language."},
        "files": {
            "type": "array",
            "description": "Relevant local files. Use [] when the task needs none.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative POSIX path."},
                    "content": {"type": "string"},
                    "encoding": {"type": "string", "enum": ["utf-8", "base64"], "default": "utf-8"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "required": ["task"],
}


def backend_url(service: Service) -> str:
    host = "127.0.0.1" if service.host in {"0.0.0.0", "::", ""} else service.host
    return f"http://{host}:{service.port}/mcp"


def describe(service: Service) -> types.Tool:
    return types.Tool(
        name=service.name,
        description=(
            f"{service.description} Runs the provider's private skill remotely. "
            "Pass the user's task and relevant files as {path, content, encoding}: utf-8 for text, "
            "base64 for binary files such as .docx/.xlsx. Returns an answer plus changed_files and "
            "deleted_files to review and apply locally. Do not send secrets or unrelated files."
        ),
        inputSchema=INPUT_SCHEMA,
        _meta={META_KEY: {
            "service": service.name,
            "description": service.description,
            "timeout_seconds": service.timeout_seconds,
        }},
    )


class Registry:
    """services.json as the service registry, reloaded when the file changes."""

    def __init__(self, config_path: str | Path):
        self.path = Path(config_path).expanduser().resolve()
        self._mtime: float | None = None
        self._services: dict[str, Service] = {}
        self._health: dict[str, tuple[float, bool]] = {}

    def services(self) -> dict[str, Service]:
        mtime = self.path.stat().st_mtime
        if mtime != self._mtime:
            self._services = {service.name: service for service in load_services(self.path)}
            self._mtime = mtime
            self._health.clear()
        return self._services

    async def _alive(self, service: Service) -> bool:
        cached = self._health.get(service.name)
        if cached and time.monotonic() - cached[0] < HEALTH_TTL_SECONDS:
            return cached[1]
        host = "127.0.0.1" if service.host in {"0.0.0.0", "::", ""} else service.host
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, service.port), timeout=0.5)
            writer.close()
            alive = True
        except (OSError, asyncio.TimeoutError):
            alive = False
        self._health[service.name] = (time.monotonic(), alive)
        return alive

    async def online(self) -> list[Service]:
        services = list(self.services().values())
        alive = await asyncio.gather(*(self._alive(service) for service in services))
        return [service for service, ok in zip(services, alive) if ok]


def build_gateway(config_path: str | Path, gateway: Gateway) -> Starlette:
    registry = Registry(config_path)
    server: Server = Server(
        "nunchaku",
        instructions=(
            "Each tool is a remote skill service; choose one by its description. "
            "Send only relevant files and apply returned changes locally."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [describe(service) for service in await registry.online()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        service = registry.services().get(name)
        if service is None:
            raise ValueError(f"unknown service: {name}")
        result = await invoke_remote(
            backend_url(service), service.timeout_seconds,
            arguments.get("task", ""), arguments.get("files") or [],
        )
        return result.model_dump()

    # Same DNS-rebinding defaults FastMCP applies to loopback servers.
    security = None
    if gateway.host in {"127.0.0.1", "localhost", "::1"}:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True,
        security_settings=security, max_request_body_size=MAX_REQUEST_BYTES,
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))], lifespan=lifespan)


def serve_gateway(config_path: str | Path) -> None:
    gateway = load_gateway(config_path)
    uvicorn.run(build_gateway(config_path, gateway), host=gateway.host, port=gateway.port, log_level="info")
