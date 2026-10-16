from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from nunchaku.admin import ServiceManager
from nunchaku.config import load_gateway
from nunchaku.skills import SKILL_NAME, install

# A portable fake engine: the custom engine runs it with the interpreter, so no shebang is needed.
FAKE_ENGINE = (
    "import pathlib, sys\n"
    "name = sys.argv[1]\n"
    "pathlib.Path('README.md').write_text(f'after {name}', encoding='utf-8')\n"
    "print(f'done by {name}')\n"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _service(root: Path, name: str, port: int) -> dict:
    skill = root / f"skill-{name}"
    skill.mkdir()
    (skill / "SKILL.md").write_text("PRIVATE", encoding="utf-8")
    return {"name": name, "description": f"Service {name}", "skill": str(skill), "engine": "custom",
            "command": sys.executable, "args": [str(root / "engine.py"), name], "port": port,
            "timeout_seconds": 30}


async def _wait_port(port: int, process: subprocess.Popen) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise AssertionError(f"process exited: {process.stderr.read() if process.stderr else ''}")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.1)
    raise AssertionError(f"port {port} never opened")


@contextmanager
def _spawn(config: Path, *command: str):
    process = subprocess.Popen([sys.executable, "-m", "nunchaku", "--config", str(config), *command],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_endpoint_routes_by_tool_name_and_picks_up_new_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "engine.py").write_text(FAKE_ENGINE, encoding="utf-8")
            alpha, beta, gateway = _free_port(), _free_port(), _free_port()
            config = root / "services.json"
            config.write_text(json.dumps({
                "gateway": {"port": gateway},
                "services": [_service(root, "alpha", alpha), _service(root, "beta", beta)],
            }), encoding="utf-8")
            url = f"http://127.0.0.1:{gateway}/mcp"

            async def tool_names() -> list[str]:
                async with streamable_http_client(url) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        return sorted(tool.name for tool in (await client.list_tools()).tools)

            with _spawn(config, "serve", "alpha") as a, _spawn(config, "gateway") as g:
                await _wait_port(alpha, a)
                await _wait_port(gateway, g)
                # beta is configured but not running, so it is not offered.
                self.assertEqual(await tool_names(), ["alpha"])

                async with streamable_http_client(url) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool("alpha", {
                            "task": "Do it", "files": [{"path": "README.md", "content": "before"}]})
                        self.assertFalse(result.isError, result.content)
                        self.assertEqual(result.structuredContent["service"], "alpha")
                        self.assertEqual(result.structuredContent["changed_files"][0]["content"], "after alpha")
                        missing = await client.call_tool("nope", {"task": "x"})
                        self.assertTrue(missing.isError)

                with _spawn(config, "serve", "beta") as b:
                    await _wait_port(beta, b)
                    await asyncio.sleep(3.2)  # let the cached "beta is down" health check expire
                    self.assertEqual(await tool_names(), ["alpha", "beta"])

                    project = root / "project"
                    project.mkdir()
                    (project / "README.md").write_text("before", encoding="utf-8")
                    parameters = StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "nunchaku", "bridge", "--url", url, "--root", str(project)])
                    async with stdio_client(parameters) as (read, write):
                        async with ClientSession(read, write) as client:
                            await client.initialize()
                            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                            self.assertEqual(sorted(tools), ["alpha", "beta"])
                            self.assertIn("paths", tools["beta"].inputSchema["properties"])
                            result = await client.call_tool("beta", {"task": "Do it", "paths": ["README.md"]})
                            self.assertFalse(result.isError, result.content)
                            self.assertEqual(result.structuredContent["applied_files"], ["README.md"])
                    self.assertEqual((project / "README.md").read_text(encoding="utf-8"), "after beta")

    async def test_bridge_reports_unreachable_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-m", "nunchaku", "bridge", "--url", f"http://127.0.0.1:{_free_port()}/mcp",
                 "--root", directory],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("cannot reach gateway", completed.stderr)


class GatewayConfigTests(unittest.TestCase):
    def test_gateway_defaults_and_port_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "services.json"
            config.write_text(json.dumps({"services": [_service(root, "alpha", 8750)]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already used by a service"):
                load_gateway(config)
            config.write_text(json.dumps({"services": []}), encoding="utf-8")
            self.assertEqual(load_gateway(config).port, 8750)
            config.write_text(json.dumps({"gateway": {"port": 9000, "extra": 1}, "services": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_gateway(config)

    def test_dashboard_keeps_gateway_settings_when_saving_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "services.json"
            config.write_text(json.dumps({"gateway": {"port": 9100}, "services": []}), encoding="utf-8")
            manager = ServiceManager(config)
            manager.save(_service(root, "alpha", 8765))
            saved = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(saved["gateway"], {"port": 9100})
            self.assertEqual([s["name"] for s in saved["services"]], ["alpha"])
            self.assertEqual(manager.gateway()["endpoint"], "http://127.0.0.1:9100/mcp")


class InstallSkillTests(unittest.TestCase):
    def test_installs_generic_skill_without_service_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "skills"
            installed = install(target)
            self.assertEqual(installed, target / SKILL_NAME)
            text = (installed / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"name: {SKILL_NAME}", text)
            self.assertNotIn("md2docx", text)
            self.assertTrue((installed / "agents" / "openai.yaml").is_file())
            (installed / "SKILL.md").write_text("user edit", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                install(target)
            self.assertEqual((installed / "SKILL.md").read_text(encoding="utf-8"), "user edit")
            install(target, force=True)
            self.assertNotEqual((installed / "SKILL.md").read_text(encoding="utf-8"), "user edit")

    def test_cli_install_requires_path_when_not_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = subprocess.run([sys.executable, "-m", "nunchaku", "install-skill"],
                                     capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(missing.returncode, 2)
            self.assertIn("--path", missing.stderr)
            done = subprocess.run([sys.executable, "-m", "nunchaku", "install-skill", "--path", directory,
                                   "--url", "http://127.0.0.1:8750/mcp"],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertTrue((Path(directory) / SKILL_NAME / "SKILL.md").is_file())
            self.assertIn("bridge --url http://127.0.0.1:8750/mcp", done.stdout)


if __name__ == "__main__":
    unittest.main()
