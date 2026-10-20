from __future__ import annotations

import io
import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx

from nunchaku.admin import ServiceManager, create_app
from nunchaku.auth import AuthStore, auth_path
from nunchaku.config import load_services


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_manage_real_mcp_process_and_export_public_connector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "private"
            skill.mkdir()
            (skill / "SKILL.md").write_text("PRIVATE CANARY INSTRUCTION", encoding="utf-8")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            config = root / "services.json"
            app = create_app(config)
            AuthStore(auth_path(config)).set_admin("root", "correct-horse", create=True)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
                login = await client.post("/api/session/login", json={"name": "root", "password": "correct-horse"})
                self.assertEqual(login.status_code, 200, login.text)
                empty = await client.get("/api/services")
                self.assertEqual(empty.json(), {"services": []})
                self.assertEqual((await client.get("/")).status_code, 200)
                self.assertIn("Skill 服务台", (await client.get("/")).text)

                payload = {"name": "private-demo", "description": "Help with text", "skill": str(skill),
                           "engine": "codex", "host": "127.0.0.1", "port": port,
                           "timeout_seconds": 30}
                bad = await client.post("/api/services", json=payload,
                                        headers={"Origin": "https://other.example"})
                self.assertEqual(bad.status_code, 403)
                created = await client.post("/api/services", json=payload)
                self.assertEqual(created.status_code, 201, created.text)
                self.assertEqual(load_services(config)[0].name, "private-demo")
                payload["description"] = "Improved description"
                edited = await client.put("/api/services/private-demo", json=payload)
                self.assertEqual(edited.status_code, 200, edited.text)
                custom = {**payload, "engine": "custom", "command": "my-agent", "args": ["--cwd", "{workspace}", "{prompt}"]}
                switched = await client.put("/api/services/private-demo", json=custom)
                self.assertEqual(switched.status_code, 200, switched.text)
                self.assertEqual(switched.json()["args"], ["--cwd", "{workspace}", "{prompt}"])
                self.assertEqual(load_services(config)[0].args, ("--cwd", "{workspace}", "{prompt}"))
                reverted = await client.put("/api/services/private-demo", json=payload)
                self.assertEqual(reverted.status_code, 200, reverted.text)
                self.assertIsNone(load_services(config)[0].args)

                exported = await client.get(
                    "/api/services/private-demo/connector",
                    params={"url": f"http://127.0.0.1:{port}/mcp", "mode": "bridge"},
                )
                self.assertEqual(exported.status_code, 200, exported.text[:200])
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    names = archive.namelist()
                    self.assertIn("private-demo-remote/SKILL.md", names)
                    self.assertIn("private-demo-remote/connector.json", names)
                    self.assertNotIn("PRIVATE CANARY INSTRUCTION", repr([archive.read(n) for n in names]))
                    connector = json.loads(archive.read("private-demo-remote/connector.json"))
                    self.assertEqual(connector["name"], "private-demo")

                started = await client.post("/api/services/private-demo/start", json={})
                self.assertEqual(started.status_code, 200, started.text)
                self.assertEqual(started.json()["state"], "running")
                locked = await client.delete("/api/services/private-demo", headers={"Content-Type": "application/json"})
                self.assertEqual(locked.status_code, 409)
                logs = await client.get("/api/services/private-demo/logs")
                self.assertIn("Starting private-demo", logs.json()["logs"])
                stopped = await client.post("/api/services/private-demo/stop", json={})
                self.assertEqual(stopped.status_code, 200)
                self.assertEqual(stopped.json()["state"], "stopped")
                deleted = await client.delete("/api/services/private-demo", headers={"Content-Type": "application/json"})
                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(load_services(config), [])


class CustomEngineSaveTests(unittest.TestCase):
    def test_adding_a_service_next_to_an_existing_custom_engine(self) -> None:
        # Regression: existing custom-engine args were re-validated as tuples and rejected.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("x", encoding="utf-8")
            config = root / "services.json"
            first = {"name": "md2docx", "description": "A", "skill": str(skill), "engine": "custom",
                     "command": "aiko", "args": ["--ci", "-p", "-"], "port": 18771}
            config.write_text(json.dumps({"services": [first]}), encoding="utf-8")
            manager = ServiceManager(config)
            manager.save({"name": "testcase-gen", "description": "B", "skill": str(skill), "engine": "custom",
                          "command": "aiko", "args": ["-p", "-"], "host": "127.0.0.1", "port": 18772,
                          "timeout_seconds": 180})
            # Editing the existing one goes through the same path.
            manager.save({**first, "description": "A2"}, original_name="md2docx")
            saved = json.loads(config.read_text(encoding="utf-8"))["services"]
            self.assertEqual([s["name"] for s in saved], ["md2docx", "testcase-gen"])
            self.assertEqual(saved[0]["args"], ["--ci", "-p", "-"])
            self.assertEqual(load_services(config)[1].args, ("-p", "-"))


if __name__ == "__main__":
    unittest.main()
