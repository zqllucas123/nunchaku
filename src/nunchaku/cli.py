from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .bridge import serve_bridge
from .config import SERVICE_NAME, Service, load_services
from .server import serve


def _select(services: list[Service], name: str) -> Service:
    for service in services:
        if service.name == name:
            return service
    raise ValueError(f"unknown service: {name}")


def _client_skill(service: Service, url: str, mode: str) -> str:
    if mode == "bridge":
        instructions = """1. Read only enough local context to identify the relevant project-relative
   file paths (text or binary such as .docx/.xlsx). Never send secrets or unrelated files.
2. Call `run_skill` on the `nunchaku-{name}` MCP server with `task` and
   `paths` (for example `[\"README.md\"]`). The local bridge reads those files,
   calls the private remote Skill, checks for conflicts, and applies returned
   changes locally, including generated binary files. For a question without
   files, use `paths: []`.
3. If `conflicts` is non-empty, inspect `proposed_changes` and resolve those
   paths with normal local editing tools. Binary proposals have no inline
   content; the proposed file is saved at `proposed_file`. Do not overwrite
   concurrent edits.
4. Summarize the answer and applied changes for the user. Set
   `apply_changes: false` when the user asks to review before writing.
""".replace("{name}", service.name)
    else:
        instructions = """1. Ask the user for missing task details only when needed. Read only the local
   files relevant to the task. Never send secrets, credentials, or unrelated files.
2. Call `run_skill` with `task` and `files`, each file having a project-relative
   POSIX `path`, `content`, and `encoding`: `utf-8` for text, or `base64` for
   binary files such as .docx/.xlsx. For a task without local files, send `[]`.
3. The result contains `answer`, `changed_files`, and `deleted_files`.
   Changed files use the same `encoding` convention; decode `base64` content
   to bytes before writing it.
   Check the returned paths and content. For existing files, compare each
   `base_sha256` with the current local file before applying the change;
   resolve conflicts if the file changed since it was sent. Then apply with
   your normal local editing tools. Show the user the final result and any
   changes you could not apply.
4. If context was insufficient, read the specific additional files needed and
   call again. The remote run has no access to other local files or previous
   calls unless you include that context.
"""
    return f"""---
name: {service.name}-remote
description: {service.description} Use this when the user wants this capability through the provider's remote service.
---

# {service.name} remote connector

The provider keeps the working Skill on its server. Use the `run_skill` tool on
the `nunchaku-{service.name}` MCP server for requests matching this description.

{instructions}

MCP endpoint: `{url}`
"""


def _codex_metadata(service: Service, url: str, mode: str) -> str:
    metadata = (
        "interface:\n"
        f"  display_name: {json.dumps(service.name + ' remote', ensure_ascii=False)}\n"
        f"  short_description: {json.dumps(service.description, ensure_ascii=False)}\n"
    )
    if mode == "bridge":
        return metadata
    return metadata + (
        "dependencies:\n"
        "  tools:\n"
        "    - type: mcp\n"
        f"      value: {json.dumps('nunchaku-' + service.name)}\n"
        f"      description: {json.dumps(service.description, ensure_ascii=False)}\n"
        "      transport: streamable_http\n"
        f"      url: {json.dumps(url)}\n"
    )


def _validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("URL must be an http(s) MCP endpoint without embedded credentials or fragment")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nunchaku")
    parser.add_argument("--config", default="services.json", help="service configuration JSON")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="validate and list services")
    single = commands.add_parser("serve", help="serve one MCP endpoint")
    single.add_argument("service")
    commands.add_parser("serve-all", help="serve every configured endpoint")
    dashboard = commands.add_parser("dashboard", help="open the local service management UI")
    dashboard.add_argument("--port", type=int, default=8787, help="local dashboard port (default: 8787)")
    client = commands.add_parser("client-skill", help="generate a public local connector Skill")
    client.add_argument("service")
    client.add_argument("--url", required=True, help="customer-visible MCP URL")
    client.add_argument("--output", required=True, help="destination Skill directory")
    client.add_argument("--mode", choices=["bridge", "direct"], default="bridge")
    bridge = commands.add_parser("bridge", help="run a local stdio MCP bridge for one project")
    bridge.add_argument("--connector", required=True, help="client-skill's connector.json")
    bridge.add_argument("--root", required=True, help="absolute local project root")
    args = parser.parse_args(argv)

    try:
        if args.action == "dashboard":
            from .admin import serve_dashboard
            print(f"Dashboard: http://127.0.0.1:{args.port}/", flush=True)
            serve_dashboard(args.config, args.port)
            return 0
        if args.action == "bridge":
            connector = json.loads(Path(args.connector).expanduser().read_text(encoding="utf-8"))
            name = connector["name"]
            description = connector["description"]
            url = _validate_url(connector["url"])
            if (not isinstance(name, str) or not SERVICE_NAME.fullmatch(name)
                    or not isinstance(description, str) or not description.strip()
                    or "\n" in description or "\r" in description):
                raise ValueError("connector needs name, description, and url")
            timeout = connector.get("timeout_seconds", 180)
            if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
                raise ValueError("connector timeout_seconds must be 1..3600")
            serve_bridge(name, description, url, Path(args.root), timeout)
            return 0
        services = load_services(args.config)
        if args.action == "list":
            for service in services:
                print(f"{service.name}\t{service.engine}\t{service.host}:{service.port}\t{service.description}")
            return 0
        if args.action == "serve":
            serve(_select(services, args.service))
            return 0
        if args.action == "client-skill":
            service = _select(services, args.service)
            url = _validate_url(args.url)
            destination = Path(args.output).expanduser()
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "SKILL.md").write_text(_client_skill(service, url, args.mode), encoding="utf-8")
            (destination / "agents").mkdir()
            (destination / "agents" / "openai.yaml").write_text(_codex_metadata(service, url, args.mode), encoding="utf-8")
            (destination / "connector.json").write_text(json.dumps({
                "name": service.name,
                "description": service.description,
                "url": url,
                "timeout_seconds": service.timeout_seconds,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(destination.resolve())
            return 0
        processes = [multiprocessing.Process(target=serve, args=(service,), name=service.name) for service in services]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join()
        except KeyboardInterrupt:
            for process in processes:
                process.terminate()
            for process in processes:
                process.join()
        return 0 if all(process.exitcode == 0 for process in processes) else 1
    except (OSError, ValueError) as exc:
        print(f"nunchaku: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
