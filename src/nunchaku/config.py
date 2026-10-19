from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


SERVICE_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
ENGINES = {"codex", "claude", "hermes", "custom"}
# Placeholders a custom engine's args may use; replaced at run time.
PLACEHOLDERS = ("{prompt}", "{workspace}", "{output}", "{skill}")
MAX_ARGS = 64


def _unquote(value: object) -> object:
    # Windows "Copy as path" wraps paths in double quotes.
    if isinstance(value, str):
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
    return value


@dataclass(frozen=True)
class Service:
    name: str
    description: str
    skill: Path
    engine: str
    host: str
    port: int
    command: str | None = None
    timeout_seconds: int = 180
    args: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Gateway:
    host: str = "127.0.0.1"
    port: int = 8750
    # Require a service key on every request. Turn off only for a loopback-only dev setup.
    auth: bool = True


@dataclass(frozen=True)
class Dashboard:
    # Extra Host values the dashboard accepts, e.g. "47.97.189.132:8768" behind a proxy.
    allowed_hosts: tuple[str, ...] = ()
    # Set the Secure flag on the session cookie; enable when served over HTTPS.
    secure_cookies: bool = False
    # Gateway URL as users reach it, shown in the dashboard and the key hand-off snippet.
    public_gateway_url: str = ""
    # systemd units whose journal holds the logs of processes the dashboard didn't start.
    # Defaults match the unit names in docs/deployment-guide.md; "" turns one off.
    services_unit: str = "nunchaku-services"
    gateway_unit: str = "nunchaku-gateway"


UNIT_NAME = re.compile(r"^[A-Za-z0-9@_.:-]{1,128}$")


def _hosts(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(h, str) and h and len(h) <= 255 for h in value):
        raise ValueError(f"{where}: allowed_hosts must be a list of host[:port] strings")
    return tuple(value)


def _unit(item: dict, field: str, default: str) -> str:
    value = item.get(field, default)
    if not isinstance(value, str) or (value and not UNIT_NAME.fullmatch(value)):
        raise ValueError(f"dashboard: {field} must be a systemd unit name or \"\"")
    return value


def load_dashboard(path: str | Path) -> Dashboard:
    raw = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    item = raw.get("dashboard", {}) if isinstance(raw, dict) else {}
    fields = {"allowed_hosts", "secure_cookies", "public_gateway_url", "services_unit", "gateway_unit"}
    if not isinstance(item, dict) or set(item) - fields:
        raise ValueError(f"dashboard accepts {', '.join(sorted(fields))}")
    secure = item.get("secure_cookies", False)
    if not isinstance(secure, bool):
        raise ValueError("dashboard: secure_cookies must be true or false")
    public = item.get("public_gateway_url", "")
    if not isinstance(public, str) or (public and not public.startswith(("http://", "https://"))):
        raise ValueError("dashboard: public_gateway_url must be an http(s) URL")
    return Dashboard(_hosts(item.get("allowed_hosts", []), "dashboard"), secure, public,
                     _unit(item, "services_unit", Dashboard.services_unit),
                     _unit(item, "gateway_unit", Dashboard.gateway_unit))


def load_services(path: str | Path) -> list[Service]:
    config_path = Path(path).expanduser().resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return parse_services(raw, config_path)


def load_gateway(path: str | Path) -> Gateway:
    config_path = Path(path).expanduser().resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return parse_gateway(raw, parse_services(raw, config_path))


def parse_gateway(raw: object, services: list[Service]) -> Gateway:
    item = raw.get("gateway", {}) if isinstance(raw, dict) else {}
    if not isinstance(item, dict) or set(item) - {"host", "port", "auth"}:
        raise ValueError("gateway must be an object with host, port, and auth")
    host = item.get("host", Gateway.host)
    port = item.get("port", Gateway.port)
    auth = item.get("auth", Gateway.auth)
    if not isinstance(host, str) or not host:
        raise ValueError("gateway: host is required")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("gateway: port must be 1..65535")
    if not isinstance(auth, bool):
        raise ValueError("gateway: auth must be true or false")
    if any(service.port == port for service in services):
        raise ValueError(f"gateway port {port} is already used by a service")
    return Gateway(host, port, auth)


def parse_services(raw: object, config_path: Path) -> list[Service]:
    if not isinstance(raw, dict):
        raise ValueError("config must be an object")
    entries = raw.get("services")
    if not isinstance(entries, list):
        raise ValueError("config.services must be a list")

    result: list[Service] = []
    names: set[str] = set()
    sockets: set[tuple[str, int]] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each service must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
            raise ValueError(f"invalid service name: {name!r}")
        if name in names:
            raise ValueError(f"duplicate service name: {name}")
        names.add(name)

        description = item.get("description")
        if not isinstance(description, str) or not description.strip() or "\n" in description or "\r" in description:
            raise ValueError(f"{name}: description must be a non-empty single line")
        skill_value = _unquote(item.get("skill"))
        if not isinstance(skill_value, str) or not skill_value:
            raise ValueError(f"{name}: skill directory is required")
        skill = (config_path.parent / skill_value).resolve()
        if not skill.is_dir() or not (skill / "SKILL.md").is_file():
            raise ValueError(f"{name}: {skill} must contain SKILL.md")
        for candidate in skill.rglob("*"):
            if candidate.is_symlink():
                raise ValueError(f"{name}: skill may not contain symlinks: {candidate}")

        engine = item.get("engine")
        if engine not in ENGINES:
            raise ValueError(f"{name}: engine must be codex, claude, hermes, or custom")
        host = item.get("host", "127.0.0.1")
        port = item.get("port")
        if not isinstance(host, str) or not host:
            raise ValueError(f"{name}: host is required")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{name}: port must be 1..65535")
        if (host, port) in sockets:
            raise ValueError(f"duplicate host/port: {host}:{port}")
        sockets.add((host, port))
        timeout = item.get("timeout_seconds", 180)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise ValueError(f"{name}: timeout_seconds must be 1..3600")
        command = _unquote(item.get("command"))
        if command is not None and (not isinstance(command, str) or not command.strip()):
            raise ValueError(f"{name}: command must be a binary path or name")
        args = item.get("args")
        if engine == "custom":
            if command is None:
                raise ValueError(f"{name}: custom engine requires command")
            if (not isinstance(args, list) or len(args) > MAX_ARGS
                    or not all(isinstance(a, str) and a and "\x00" not in a for a in args)):
                raise ValueError(f"{name}: custom engine args must be a list of up to {MAX_ARGS} non-empty strings")
            args = tuple(args)
        elif args is not None:
            raise ValueError(f"{name}: args only applies to the custom engine")

        result.append(Service(name, description.strip(), skill, engine, host, port, command, timeout, args))
    return result
