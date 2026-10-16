"""Optional real Codex UX smoke test; requires a working Codex CLI account."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nunchaku.cli import main


def main_test() -> None:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("codex CLI is required")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        private = root / "private"
        private.mkdir()
        (private / "SKILL.md").write_text(
            "---\nname: demo-polish\ndescription: Improve README text.\n---\nEdit README.md when asked.\n"
        )
        fake_engine = root / "fake-codex"
        fake_engine.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "pathlib.Path('README.md').write_text('remote-processed\\n')\n"
            "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('done')\n"
        )
        fake_engine.chmod(0o755)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = root / "services.json"
        config.write_text(json.dumps({"services": [{
            "name": "demo-polish", "description": "Improve README text using the remote skill.",
            "skill": str(private), "engine": "codex", "command": str(fake_engine), "port": port,
        }]}))
        server = subprocess.Popen(
            [sys.executable, "-m", "nunchaku", "--config", str(config), "serve", "demo-polish"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("remote server exited")
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.1)
            else:
                raise RuntimeError("remote server did not start")

            project = root / "client"
            project.mkdir()
            (project / "README.md").write_text("before\n")
            connector = project / ".agents" / "skills" / "demo-polish-remote"
            connector.parent.mkdir(parents=True)
            assert main([
                "--config", str(config), "client-skill", "demo-polish",
                "--url", f"http://127.0.0.1:{port}/mcp", "--output", str(connector),
            ]) == 0
            result_file = root / "answer.txt"
            bridge_args = [
                "-m", "nunchaku", "bridge", "--connector", str(connector / "connector.json"),
                "--root", str(project),
            ]
            command = [
                codex, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                "-s", "workspace-write", "-C", str(project),
                "-c", f"mcp_servers.nunchaku-demo-polish.command={json.dumps(sys.executable)}",
                "-c", f"mcp_servers.nunchaku-demo-polish.args={json.dumps(bridge_args)}",
                "-c", 'mcp_servers.nunchaku-demo-polish.tools.run_skill.approval_mode="approve"',
                "-c", "mcp_servers.nunchaku-demo-polish.tool_timeout_sec=210",
                "-o", str(result_file), "-",
            ]
            done = subprocess.run(
                command,
                input="Use the demo-polish remote skill to improve README.md. The remote service must perform the edit.\n",
                text=True, capture_output=True, timeout=120,
            )
            actual = (project / "README.md").read_text()
            if done.returncode or actual != "remote-processed\n":
                answer = result_file.read_text() if result_file.exists() else ""
                raise AssertionError(f"Codex UX smoke failed: exit={done.returncode}, file={actual!r}, answer={answer!r}")
            print("Codex selected the public Skill and local bridge; remote edit applied successfully")
        finally:
            server.terminate()
            server.wait(timeout=5)


if __name__ == "__main__":
    main_test()
