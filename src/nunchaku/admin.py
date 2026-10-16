from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import Service, load_services, parse_services


def _entry(service: Service) -> dict:
    result = asdict(service)
    result["skill"] = str(service.skill)
    return result


class ServiceManager:
    def __init__(self, config_path: str | Path):
        self.path = Path(config_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])
        self.services = load_services(self.path)
        self.processes: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()
        self.logs_dir = self.path.parent / ".nunchaku" / "logs"

    def _write(self, services: list[Service]) -> None:
        payload = json.dumps({"services": [_entry(s) for s in services]}, ensure_ascii=False, indent=2) + "\n"
        descriptor, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _find(self, name: str) -> Service:
        for service in self.services:
            if service.name == name:
                return service
        raise KeyError(f"unknown service: {name}")

    def _state(self, name: str) -> tuple[str, int | None]:
        process = self.processes.get(name)
        if process is None:
            return "stopped", None
        code = process.poll()
        return ("running", None) if code is None else ("exited", code)

    def list(self) -> list[dict]:
        with self.lock:
            result = []
            for service in self.services:
                state, exit_code = self._state(service.name)
                entry = _entry(service)
                entry.update(state=state, exit_code=exit_code,
                             endpoint=f"http://{service.host}:{service.port}/mcp")
                result.append(entry)
            return result

    def save(self, item: object, original_name: str | None = None) -> dict:
        if not isinstance(item, dict):
            raise ValueError("service must be an object")
        allowed = {"name", "description", "skill", "engine", "host", "port", "command", "timeout_seconds"}
        if set(item) - allowed:
            raise ValueError("unknown service field")
        with self.lock:
            if original_name is not None:
                self._find(original_name)
                if self._state(original_name)[0] == "running":
                    raise RuntimeError("stop the service before editing")
                updated = [s for s in self.services if s.name != original_name]
                insert_at = next(i for i, s in enumerate(self.services) if s.name == original_name)
            else:
                updated = list(self.services)
                insert_at = len(updated)
            candidates = [_entry(s) for s in updated]
            candidates.insert(insert_at, item)
            validated = parse_services({"services": candidates}, self.path)
            self._write(validated)
            self.services = validated
            if original_name and original_name != item.get("name"):
                self.processes.pop(original_name, None)
            return next(entry for entry in self.list() if entry["name"] == item["name"])

    def delete(self, name: str) -> None:
        with self.lock:
            self._find(name)
            if self._state(name)[0] == "running":
                raise RuntimeError("stop the service before deleting")
            updated = [s for s in self.services if s.name != name]
            self._write(updated)
            self.services = updated
            self.processes.pop(name, None)

    def start(self, name: str) -> dict:
        with self.lock:
            service = self._find(name)
            if self._state(name)[0] == "running":
                raise RuntimeError("service is already running")
            try:
                with socket.create_connection((service.host, service.port), timeout=0.2):
                    raise RuntimeError(f"port {service.port} is already in use")
            except OSError:
                pass
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with (self.logs_dir / f"{name}.log").open("ab") as log:
                log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting {name}\n".encode())
                log.flush()
                process = subprocess.Popen(
                    [sys.executable, "-m", "nunchaku", "--config", str(self.path), "serve", name],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.processes[name] = process
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"service exited with code {process.returncode}; see logs")
            try:
                with socket.create_connection((service.host, service.port), timeout=0.1):
                    return next(x for x in self.list() if x["name"] == name)
            except OSError:
                time.sleep(0.08)
        raise RuntimeError("service did not become ready within 3 seconds; see logs")

    def stop(self, name: str) -> dict:
        with self.lock:
            self._find(name)
            process = self.processes.get(name)
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.processes.pop(name, None)
            return next(x for x in self.list() if x["name"] == name)

    def logs(self, name: str) -> str:
        with self.lock:
            self._find(name)
            path = self.logs_dir / f"{name}.log"
            if not path.exists():
                return "尚无日志。"
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 65536))
                return stream.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        for name in list(self.processes):
            self.stop(name)


def create_app(config_path: str | Path) -> Starlette:
    manager = ServiceManager(config_path)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        manager.close()

    def check_write(request: Request) -> None:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise ValueError("Content-Type must be application/json")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise PermissionError("cross-origin request rejected")

    async def body(request: Request) -> object:
        check_write(request)
        if int(request.headers.get("content-length", "0")) > 65536:
            raise ValueError("request is too large")
        return await request.json()

    async def api(request: Request) -> Response:
        name = request.path_params.get("name")
        action = request.path_params.get("action")
        try:
            if request.method == "GET":
                if action == "logs":
                    return JSONResponse({"logs": manager.logs(name)})
                if name is not None:
                    return JSONResponse({"error": "unsupported operation"}, status_code=405)
                return JSONResponse({"services": manager.list()})
            if action == "connector":
                raise ValueError("invalid method")
            check_write(request)
            if request.method == "POST" and action == "start":
                return JSONResponse(manager.start(name))
            if request.method == "POST" and action == "stop":
                return JSONResponse(manager.stop(name))
            if request.method == "POST" and name is None:
                return JSONResponse(manager.save(await body(request)), status_code=201)
            if request.method == "PUT" and name:
                return JSONResponse(manager.save(await body(request), name))
            if request.method == "DELETE" and name:
                manager.delete(name)
                return Response(status_code=204)
            return JSONResponse({"error": "unsupported operation"}, status_code=405)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def connector(request: Request) -> Response:
        from .cli import _client_skill, _codex_metadata, _validate_url

        name = request.path_params["name"]
        try:
            service = manager._find(name)
            url = _validate_url(request.query_params.get("url", ""))
            mode = request.query_params.get("mode", "bridge")
            if mode not in {"bridge", "direct"}:
                raise ValueError("mode must be bridge or direct")
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                folder = f"{name}-remote/"
                archive.writestr(folder + "SKILL.md", _client_skill(service, url, mode))
                archive.writestr(folder + "agents/openai.yaml", _codex_metadata(service, url, mode))
                archive.writestr(folder + "connector.json", json.dumps({
                    "name": name, "description": service.description,
                    "url": url, "timeout_seconds": service.timeout_seconds,
                }, ensure_ascii=False, indent=2) + "\n")
            return Response(output.getvalue(), media_type="application/zip", headers={
                "Content-Disposition": f'attachment; filename="{name}-remote.zip"',
                "Cache-Control": "no-store",
            })
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def asset(request: Request) -> Response:
        name = request.path_params.get("asset", "index.html")
        if name not in {"index.html", "style.css", "app.js"}:
            return Response(status_code=404)
        media = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript"}[name]
        content = files("nunchaku").joinpath("ui", name).read_bytes()
        return Response(content, media_type=media, headers={"Cache-Control": "no-store"})

    return Starlette(routes=[
        Route("/", asset), Route("/assets/{asset}", asset),
        Route("/api/services", api, methods=["GET", "POST"]),
        Route("/api/services/{name}/connector", connector, methods=["GET"]),
        Route("/api/services/{name}/{action}", api, methods=["GET", "POST"]),
        Route("/api/services/{name}", api, methods=["PUT", "DELETE"]),
    ], lifespan=lifespan, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])


def serve_dashboard(config_path: str | Path, port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("dashboard port must be 1..65535")
    uvicorn.run(create_app(config_path), host="127.0.0.1", port=port, log_level="warning")
