"""Install the generic nunchaku-remote connector Skill shipped inside the wheel."""
from __future__ import annotations

import shutil
import sys
from importlib.resources import as_file, files
from pathlib import Path

SKILL_NAME = "nunchaku-remote"

# Known Agent clients and where they look for user Skills.
CLIENTS = (
    ("Claude Code", Path("~/.claude/skills")),
    ("AIKO", Path("~/.sdw/skills")),
    ("Codex", Path("~/.agents/skills")),
)


def candidates() -> list[tuple[str, Path]]:
    """Clients whose config directory exists on this machine."""
    return [(label, path.expanduser()) for label, path in CLIENTS if path.expanduser().parent.is_dir()]


def install(target: Path, force: bool = False) -> Path:
    destination = target.expanduser() / SKILL_NAME
    if destination.exists():
        if not force:
            raise ValueError(f"{destination} already exists; pass --force to replace it")
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with as_file(files("nunchaku").joinpath("data", "skills", SKILL_NAME)) as source:
        shutil.copytree(source, destination)
    return destination


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:  # Windows reports NUL as a TTY, so isatty() alone can't rule this out.
        raise ValueError("no answer on stdin; pass --path to install non-interactively") from None


def choose(found: list[tuple[str, Path]]) -> list[Path]:
    """Ask which Skill directories to install into. Empty input selects all."""
    for index, (label, path) in enumerate(found, 1):
        print(f"  {index}. {label:<12} {path}")
    print(f"  {len(found) + 1}. other directory")
    answer = _ask(f"Install into [1-{len(found) + 1}, comma-separated, Enter = all found]: ")
    if not answer:
        if not found:
            raise ValueError("no Skill directory chosen")
        return [path for _, path in found]
    chosen: list[Path] = []
    for part in answer.replace(" ", "").split(","):
        if not part.isdigit() or not 1 <= int(part) <= len(found) + 1:
            raise ValueError(f"invalid choice: {part}")
        if int(part) == len(found) + 1:
            custom = _ask("Skill directory: ").strip("\"'")
            if not custom:
                raise ValueError("no directory given")
            chosen.append(Path(custom))
        else:
            chosen.append(found[int(part) - 1][1])
    return chosen


def registration_hint(url: str | None) -> str:
    command = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else "nunchaku"
    url = url or "<gateway URL from your provider, e.g. http://127.0.0.1:8750/mcp>"
    return (
        "Next, register the MCP server under the name \"nunchaku\":\n\n"
        f"  {command} bridge --url {url} --root <your project directory>\n\n"
        "Use it as a STDIO server, or connect directly over HTTP to the same URL."
    )
