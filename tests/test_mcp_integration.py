from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_initialize_discover_and_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "private"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: private\ndescription: Test\n---\nPrivate instructions")
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "args = sys.argv\n"
                "assert pathlib.Path('.agents/skills/smoke/SKILL.md').is_file()\n"
                "assert 'Do the task' in sys.stdin.read()\n"
                "pathlib.Path('README.md').write_text('after', encoding='utf-8')\n"
                "pathlib.Path(args[args.index('-o') + 1]).write_text('done', encoding='utf-8')\n"
            )
            fake_codex.chmod(0o755)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = root / "services.json"
            config.write_text(json.dumps({"services": [{
                "name": "smoke", "description": "Test service", "skill": str(skill),
                "engine": "codex", "command": str(fake_codex), "port": port,
            }]}))
            process = subprocess.Popen(
                [sys.executable, "-m", "nunchaku", "--config", str(config), "serve", "smoke"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise AssertionError(f"server exited: {process.stderr.read()}")
                    with socket.socket() as probe:
                        if probe.connect_ex(("127.0.0.1", port)) == 0:
                            break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("MCP server did not start")

                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = await client.list_tools()
                        self.assertIn("run_skill", [tool.name for tool in tools.tools])
                        result = await client.call_tool("run_skill", {
                            "task": "Do the task", "files": [{"path": "README.md", "content": "before"}],
                        })
                        self.assertFalse(result.isError, result.content)
                        payload = result.structuredContent
                        self.assertIsNotNone(payload)
                        self.assertEqual(payload["answer"], "done")
                        self.assertEqual(payload["changed_files"][0]["content"], "after")

                project = root / "customer-project"
                project.mkdir()
                (project / "README.md").write_text("before")
                connector = root / "connector.json"
                connector.write_text(json.dumps({
                    "name": "smoke", "description": "Test service",
                    "url": f"http://127.0.0.1:{port}/mcp", "timeout_seconds": 30,
                }))
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "nunchaku", "bridge", "--connector", str(connector), "--root", str(project)],
                )
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = await client.list_tools()
                        self.assertIn("run_skill", [tool.name for tool in tools.tools])
                        result = await client.call_tool("run_skill", {
                            "task": "Do the task", "paths": ["README.md"],
                        })
                        self.assertFalse(result.isError, result.content)
                        self.assertEqual(result.structuredContent["applied_files"], ["README.md"])
                        self.assertEqual((project / "README.md").read_text(), "after")
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
