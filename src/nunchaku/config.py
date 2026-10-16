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


def load_services(path: str | Path) -> list[Service]:
    config_path = Path(path).expanduser().resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return parse_services(raw, config_path)


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
        skill_value = item.get("skill")
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
        command = item.get("command")
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
