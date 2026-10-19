"""Single public MCP endpoint in front of the per-service processes.

Each configured service keeps its own process and port. The gateway lists one
tool per reachable service and forwards ``tools/call`` by tool name.
"""
from __future__ import annotations

import asyncio
import contextvars
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
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import AuthStore, ServiceKey, auth_path
from .bridge import META_KEY, invoke_remote
from .config import Gateway, Service, load_gateway, load_services
from .log import logger, preview
from .server import MAX_REQUEST_BYTES

LOG = logger("gateway")
HEALTH_TTL_SECONDS = 3.0
# The service key that authenticated the current request; None when auth is off.
CURRENT_KEY: contextvars.ContextVar[ServiceKey | None] = contextvars.ContextVar("nunchaku_key", default=None)

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
            previous = set(self._services)
            self._services = {service.name: service for service in load_services(self.path)}
            self._mtime = mtime
            self._health.clear()
            added, removed = set(self._services) - previous, previous - set(self._services)
            LOG.info("registry loaded: %d service(s)%s%s", len(self._services),
                     f", added {sorted(added)}" if added else "", f", removed {sorted(removed)}" if removed else "")
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
        online = [service for service, ok in zip(services, alive) if ok]
        LOG.debug("online services: %s", [service.name for service in online])
        return online


def build_gateway(config_path: str | Path, gateway: Gateway, store: AuthStore | None = None) -> Starlette:
    registry = Registry(config_path)
    if gateway.auth and store is None:
        store = AuthStore(auth_path(config_path))
    server: Server = Server(
        "nunchaku",
        instructions=(
            "Each tool is a remote skill service; choose one by its description. "
            "Send only relevant files and apply returned changes locally."
        ),
    )

    def caller() -> tuple[ServiceKey | None, str]:
        key = CURRENT_KEY.get()
        return key, (f"{key.owner} ({key.prefix}…)" if key else "anonymous")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        key, who = caller()
        services = [s for s in await registry.online() if key is None or key.allows(s.name)]
        LOG.info("tools/list by %s: offering %s", who, [service.name for service in services] or "no services")
        return [describe(service) for service in services]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        key, who = caller()
        service = registry.services().get(name)
        if service is None:
            LOG.warning("tools/call by %s: unknown service %r", who, name)
            raise ValueError(f"unknown service: {name}")
        if key is not None and not key.allows(name):
            LOG.warning("tools/call by %s: denied access to %s", who, name)
            raise PermissionError(f"this service key may not use {name}")
        files = arguments.get("files") or []
        LOG.info("[%s] call started by %s: %d file(s), task=%r",
                 service.name, who, len(files), preview(arguments.get("task", "")))
        if key is not None and store is not None:
            await asyncio.to_thread(store.record_use, key.id)
        started = time.monotonic()
        try:
            result = await invoke_remote(
                backend_url(service), service.timeout_seconds,
                arguments.get("task", ""), files,
            )
        except Exception as exc:
            LOG.error("[%s] call failed after %.1fs: %s: %s",
                      service.name, time.monotonic() - started, type(exc).__name__, str(exc)[:400])
            raise
        LOG.info("[%s] call finished in %.1fs: %d changed, %d deleted",
                 service.name, time.monotonic() - started, len(result.changed_files), len(result.deleted_files))
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
    mcp_app = StreamableHTTPASGIApp(manager)

    class Endpoint:
        """Resolve the service key before MCP runs, so a bad key gets a plain HTTP 401.

        A class instance (not a function) so Starlette routes every method to it as raw ASGI.
        """

        async def __call__(self, scope, receive, send) -> None:
            if store is None:
                await mcp_app(scope, receive, send)
                return
            header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            secret = header[7:].strip() if header[:7].lower() == "bearer " else ""
            key = await asyncio.to_thread(store.resolve, secret)
            if key is None:
                LOG.warning("rejected request: %s service key", "invalid" if secret else "missing")
                await JSONResponse({"error": "invalid or missing service key"}, status_code=401,
                                   headers={"WWW-Authenticate": 'Bearer realm="nunchaku"'})(scope, receive, send)
                return
            token = CURRENT_KEY.set(key)
            try:
                await mcp_app(scope, receive, send)
            finally:
                CURRENT_KEY.reset(token)

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            try:
                yield
            finally:
                if store is not None:
                    store.flush_usage()

    return Starlette(routes=[Route("/mcp", endpoint=Endpoint())], lifespan=lifespan)


def serve_gateway(config_path: str | Path) -> None:
    gateway = load_gateway(config_path)
    services = load_services(config_path)
    LOG.info("gateway starting on http://%s:%s/mcp with %d configured service(s): %s; service keys %s",
             gateway.host, gateway.port, len(services), [service.name for service in services] or "none",
             "required" if gateway.auth else "NOT required (auth disabled)")
    uvicorn.run(build_gateway(config_path, gateway), host=gateway.host, port=gateway.port, log_level="info")
