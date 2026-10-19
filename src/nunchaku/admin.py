from __future__ import annotations

import asyncio
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
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import journal
from .auth import SESSION_TTL_SECONDS, AuthStore, auth_path
from .config import Service, load_dashboard, load_gateway, load_services, parse_services

# Service names start with a letter, so this log file can't collide with one.
GATEWAY_LOG = "_gateway"
SESSION_COOKIE = "nunchaku_session"
ASSETS = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript",
          "login.js": "text/javascript"}
# Served without a session so the login page can render.
PUBLIC_ASSETS = {"style.css", "login.js"}


def _entry(service: Service) -> dict:
    result = asdict(service)
    result["skill"] = str(service.skill)
    return result


def _listening(host: str, port: int) -> bool:
    """Something already serves this port, e.g. a systemd-managed process."""
    host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_closed(host: str, port: int, timeout: float = 3.0) -> None:
    """The OS can keep accepting briefly after the process exits; don't report it as external."""
    deadline = time.monotonic() + timeout
    while _listening(host, port) and time.monotonic() < deadline:
        time.sleep(0.1)


class ServiceManager:
    def __init__(self, config_path: str | Path, store: AuthStore | None = None):
        self.path = Path(config_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])
        self.services = load_services(self.path)
        self.processes: dict[str, subprocess.Popen] = {}
        self.gateway_process: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.logs_dir = self.path.parent / ".nunchaku" / "logs"
        self.store = store or AuthStore(auth_path(self.path))

    def _write(self, services: list[Service]) -> None:
        document: dict = {"services": [_entry(s) for s in services]}
        if self.path.exists():
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                for section in ("gateway", "dashboard"):
                    if section in current:
                        document[section] = current[section]
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
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
                if state == "stopped" and _listening(service.host, service.port):
                    state = "external"
                entry = _entry(service)
                entry.update(state=state, exit_code=exit_code,
                             endpoint=f"http://{service.host}:{service.port}/mcp")
                result.append(entry)
            return result

    def save(self, item: object, original_name: str | None = None) -> dict:
        if not isinstance(item, dict):
            raise ValueError("service must be an object")
        allowed = {"name", "description", "skill", "engine", "host", "port", "command", "timeout_seconds", "args"}
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
                # Grants are stored by name, so carry them over to the new name.
                self.store.rename_service(original_name, item["name"])
            return next(entry for entry in self.list() if entry["name"] == item["name"])

    def delete(self, name: str) -> None:
        with self.lock:
            self._find(name)
            if self._state(name)[0] == "running":
                raise RuntimeError("stop the service before deleting")
            updated = [s for s in self.services if s.name != name]
            self._write(updated)
            self.services = updated
            self.store.remove_service(name)
            self.processes.pop(name, None)

    def start(self, name: str) -> dict:
        with self.lock:
            service = self._find(name)
            if self._state(name)[0] == "running":
                raise RuntimeError("service is already running")
            self.processes[name] = process = self._spawn(name, ["serve", name], service.host, service.port)
        self._wait_ready(process, service.host, service.port, "service")
        return next(x for x in self.list() if x["name"] == name)

    def _spawn(self, log_name: str, command: list[str], host: str, port: int) -> subprocess.Popen:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                raise RuntimeError(f"port {port} is already in use")
        except OSError:
            pass
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        with (self.logs_dir / f"{log_name}.log").open("ab") as log:
            log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting {log_name}\n".encode())
            log.flush()
            return subprocess.Popen(
                [sys.executable, "-m", "nunchaku", "--config", str(self.path), *command],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    @staticmethod
    def _wait_ready(process: subprocess.Popen, host: str, port: int, kind: str) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{kind} exited with code {process.returncode}; see logs")
            try:
                with socket.create_connection((host, port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.08)
        raise RuntimeError(f"{kind} did not become ready within 3 seconds; see logs")

    def gateway(self) -> dict:
        with self.lock:
            settings = load_gateway(self.path)
            process = self.gateway_process
            code = None if process is None else process.poll()
            state = "stopped" if process is None else ("running" if code is None else "exited")
            host = "127.0.0.1" if settings.host in {"0.0.0.0", "::"} else settings.host
            if state == "stopped" and _listening(host, settings.port):
                state = "external"
            return {"host": settings.host, "port": settings.port, "state": state, "exit_code": code,
                    "endpoint": f"http://{host}:{settings.port}/mcp", "auth": settings.auth,
                    "public_endpoint": load_dashboard(self.path).public_gateway_url}

    def start_gateway(self) -> dict:
        with self.lock:
            if self.gateway()["state"] == "running":
                raise RuntimeError("gateway is already running")
            settings = load_gateway(self.path)
            self.gateway_process = process = self._spawn(GATEWAY_LOG, ["gateway"], settings.host, settings.port)
        self._wait_ready(process, settings.host, settings.port, "gateway")
        return self.gateway()

    def stop_gateway(self) -> dict:
        with self.lock:
            if self.gateway_process is not None:
                self._terminate(self.gateway_process)
                settings = load_gateway(self.path)
                _wait_closed(settings.host, settings.port)
            self.gateway_process = None
            return self.gateway()

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def stop(self, name: str) -> dict:
        with self.lock:
            service = self._find(name)
            process = self.processes.get(name)
            if process:
                self._terminate(process)
                _wait_closed(service.host, service.port)
            self.processes.pop(name, None)
            return next(x for x in self.list() if x["name"] == name)

    def logs(self, name: str) -> dict:
        """Logs for a service or the gateway, with where they came from.

        Processes this dashboard started log to files under logs_dir. Anything else
        running on the port (typically systemd) logs to the journal instead.
        """
        with self.lock:
            if name == GATEWAY_LOG:
                settings = load_gateway(self.path)
                running_here = self.gateway_process is not None and self.gateway_process.poll() is None
                external = not running_here and _listening(settings.host, settings.port)
                unit, tag = load_dashboard(self.path).gateway_unit, None
            else:
                service = self._find(name)
                external = self._state(name)[0] != "running" and _listening(service.host, service.port)
                unit, tag = load_dashboard(self.path).services_unit, name
            path = self.logs_dir / f"{name}.log"
        if external and unit:
            try:
                text = journal.read_unit(unit, tag)
                return {"logs": text or "日志中暂无该服务的记录。", "source": "journal", "unit": unit}
            except journal.JournalUnavailable as exc:
                hint = (f"该服务由外部进程运行，日志在 systemd journal（{unit}）中，但管理台无法读取：{exc}\n"
                        f"请在服务器上执行 journalctl -u {unit} -f 查看；"
                        "或按部署教程为管理台授予读取 journal 的权限。")
                return {"logs": hint, "source": "journal-unavailable", "unit": unit}
        if not path.exists():
            return {"logs": "尚无日志。", "source": "file"}
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            return {"logs": stream.read().decode("utf-8", errors="replace"), "source": "file"}

    def close(self) -> None:
        self.stop_gateway()
        for name in list(self.processes):
            self.stop(name)


def create_app(config_path: str | Path, store: AuthStore | None = None) -> Starlette:
    manager = ServiceManager(config_path, store)
    store = manager.store
    settings = load_dashboard(config_path)
    allowed_hosts = ["127.0.0.1", "localhost", *settings.allowed_hosts]
    host_names = {h.rsplit(":", 1)[0] if h.count(":") == 1 else h for h in allowed_hosts}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        manager.close()

    def check_write(request: Request) -> None:
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise ValueError("Content-Type must be application/json")
        origin = request.headers.get("origin")
        # Behind a proxy request.base_url is the internal address, so compare hosts, not full origins.
        if origin and urlsplit(origin).hostname not in host_names:
            raise PermissionError("cross-origin request rejected")

    async def body(request: Request) -> object:
        check_write(request)
        if int(request.headers.get("content-length", "0")) > 65536:
            raise ValueError("request is too large")
        return await request.json()

    def admin_of(request: Request) -> str | None:
        return store.session_admin(request.cookies.get(SESSION_COOKIE))

    def session_response(payload: dict, token: str | None) -> JSONResponse:
        response = JSONResponse(payload)
        if token is None:
            response.delete_cookie(SESSION_COOKIE, path="/")
        else:
            response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, path="/", httponly=True,
                                samesite="strict", secure=settings.secure_cookies)
        return response

    async def session_api(request: Request) -> Response:
        action = request.path_params.get("action")
        try:
            if request.method == "GET":
                return JSONResponse({"admin": admin_of(request), "setup_required": not store.has_admins()})
            item = await body(request)
            if not isinstance(item, dict):
                raise ValueError("expected an object")
            if action == "login":
                name, password = item.get("name"), item.get("password")
                if not isinstance(name, str) or not isinstance(password, str):
                    raise ValueError("name and password are required")
                token = await asyncio.to_thread(store.login, name.strip(), password)
                return session_response({"admin": name.strip()}, token)
            if action == "logout":
                store.logout(request.cookies.get(SESSION_COOKIE))
                return session_response({"admin": None}, None)
            if action == "password":
                admin = admin_of(request)
                if admin is None:
                    return JSONResponse({"error": "login required"}, status_code=401)
                current, new = item.get("current"), item.get("new")
                if not isinstance(current, str) or not isinstance(new, str):
                    raise ValueError("current and new passwords are required")
                await asyncio.to_thread(store.change_password, admin, current, new)
                # Changing the password ends every session, including this one; issue a fresh one.
                token = await asyncio.to_thread(store.login, admin, new)
                return session_response({"admin": admin}, token)
            return JSONResponse({"error": "unsupported operation"}, status_code=405)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401 if action == "login" else 403)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def keys_api(request: Request) -> Response:
        key_id = request.path_params.get("key_id")
        known = {service.name for service in manager.services}
        try:
            if request.method == "GET":
                return JSONResponse({"keys": [key.public() for key in store.keys()]})
            if request.method == "POST" and key_id is None:
                secret, key = store.create_key(await body(request), request.state.admin, known)
                # The only time the plaintext key leaves the server.
                return JSONResponse({"key": key.public(), "secret": secret}, status_code=201)
            if request.method == "PUT" and key_id:
                return JSONResponse(store.update_key(key_id, await body(request), known).public())
            if request.method == "DELETE" and key_id:
                check_write(request)
                store.delete_key(key_id)
                return Response(status_code=204)
            return JSONResponse({"error": "unsupported operation"}, status_code=405)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def api(request: Request) -> Response:
        name = request.path_params.get("name")
        action = request.path_params.get("action")
        try:
            if request.method == "GET":
                if action == "logs":
                    return JSONResponse(await asyncio.to_thread(manager.logs, name))
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

    async def gateway_api(request: Request) -> Response:
        action = request.path_params.get("action")
        try:
            if request.method == "GET" and action is None:
                return JSONResponse(manager.gateway())
            if request.method == "GET" and action == "logs":
                return JSONResponse(await asyncio.to_thread(manager.logs, GATEWAY_LOG))
            check_write(request)
            if request.method == "POST" and action == "start":
                return JSONResponse(manager.start_gateway())
            if request.method == "POST" and action == "stop":
                return JSONResponse(manager.stop_gateway())
            return JSONResponse({"error": "unsupported operation"}, status_code=405)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
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
        if name not in ASSETS:
            return Response(status_code=404)
        content = files("nunchaku").joinpath("ui", name).read_bytes()
        return Response(content, media_type=ASSETS[name], headers={"Cache-Control": "no-store"})

    async def require_admin(request: Request, call_next):
        """Everything except the login page, its assets, and /api/session needs a session."""
        path = request.url.path
        public = path in {"/login", "/api/session", "/api/session/login"} or (
            path.startswith("/assets/") and path.rsplit("/", 1)[-1] in PUBLIC_ASSETS)
        if public:
            return await call_next(request)
        admin = await asyncio.to_thread(admin_of, request)
        if admin is None:
            if path.startswith("/api/"):
                return JSONResponse({"error": "login required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        request.state.admin = admin
        return await call_next(request)

    async def login_page(request: Request) -> Response:
        content = files("nunchaku").joinpath("ui", "login.html").read_bytes()
        return Response(content, media_type="text/html", headers={"Cache-Control": "no-store"})

    return Starlette(routes=[
        Route("/", asset), Route("/assets/{asset}", asset), Route("/login", login_page),
        Route("/api/session", session_api, methods=["GET"]),
        Route("/api/session/{action}", session_api, methods=["POST"]),
        Route("/api/keys", keys_api, methods=["GET", "POST"]),
        Route("/api/keys/{key_id}", keys_api, methods=["PUT", "DELETE"]),
        Route("/api/services", api, methods=["GET", "POST"]),
        Route("/api/gateway", gateway_api, methods=["GET"]),
        Route("/api/gateway/{action}", gateway_api, methods=["GET", "POST"]),
        Route("/api/services/{name}/connector", connector, methods=["GET"]),
        Route("/api/services/{name}/{action}", api, methods=["GET", "POST"]),
        Route("/api/services/{name}", api, methods=["PUT", "DELETE"]),
    ], lifespan=lifespan, middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=sorted(host_names)),
        Middleware(BaseHTTPMiddleware, dispatch=require_admin),
    ])


def serve_dashboard(config_path: str | Path, port: int, host: str = "127.0.0.1") -> None:
    if not 1 <= port <= 65535:
        raise ValueError("dashboard port must be 1..65535")
    store = AuthStore(auth_path(config_path))
    if not store.has_admins():
        print("nunchaku: no admin account yet. Create one first:\n"
              f"  nunchaku --config {config_path} admin add <name>", file=sys.stderr)
    uvicorn.run(create_app(config_path, store), host=host, port=port, log_level="warning",
                proxy_headers=False)
