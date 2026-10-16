from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nunchaku.bridge import _apply, _snapshot
from nunchaku.config import Service, load_services
from nunchaku.runner import _run_sync
from nunchaku.server import SkillResult


class ConfigTests(unittest.TestCase):
    def test_loads_multiple_services_and_rejects_duplicate_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "private"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: private\n---\n")
            config = root / "services.json"
            data = {"services": [
                {"name": "first", "description": "First", "skill": "private", "engine": "codex", "port": 8765},
                {"name": "second", "description": "Second", "skill": "private", "engine": "claude", "port": 8766},
            ]}
            config.write_text(json.dumps(data))
            self.assertEqual([s.name for s in load_services(config)], ["first", "second"])
            data["services"][1]["port"] = 8765
            config.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "duplicate host/port"):
                load_services(config)

    def test_custom_engine_requires_command_and_args(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "private").mkdir()
            (root / "private" / "SKILL.md").write_text("x")
            config = root / "services.json"
            entry = {"name": "mine", "description": "Mine", "skill": "private", "engine": "custom", "port": 8765}
            for extra, error in [({"args": ["{prompt}"]}, "requires command"),
                                 ({"command": "my-agent"}, "args must be"),
                                 ({"command": "my-agent", "args": [""]}, "args must be")]:
                with self.subTest(extra=extra):
                    config.write_text(json.dumps({"services": [{**entry, **extra}]}))
                    with self.assertRaisesRegex(ValueError, error):
                        load_services(config)
            config.write_text(json.dumps({"services": [{**entry, "engine": "codex", "args": ["x"]}]}))
            with self.assertRaisesRegex(ValueError, "only applies"):
                load_services(config)
            config.write_text(json.dumps({"services": [{**entry, "command": "my-agent", "args": ["--cwd", "{workspace}"]}]}))
            self.assertEqual(load_services(config)[0].args, ("--cwd", "{workspace}"))


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        skill = Path(self.temp.name) / "private"
        skill.mkdir()
        (skill / "SKILL.md").write_text("secret internal instructions", encoding="utf-8")
        self.service = Service("private", "do a task", skill, "codex", "127.0.0.1", 8765)

    def test_snapshot_changes_and_private_skill_is_not_returned(self) -> None:
        def fake_run(command, **kwargs):
            workspace = Path(kwargs["cwd"])
            self.assertEqual((workspace / "README.md").read_text(), "before")
            self.assertTrue((workspace / ".agents/skills/private/SKILL.md").is_file())
            (workspace / "README.md").write_text("after")
            (workspace / "new.txt").write_text("new")
            (workspace / ".nunchaku/answer.txt").write_text(f"Edited {workspace / 'README.md'}")
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("nunchaku.runner.subprocess.run", side_effect=fake_run):
            result = _run_sync(self.service, "edit file", [{"path": "README.md", "content": "before"}])
        self.assertEqual(result["answer"], "Edited README.md")
        self.assertEqual(result["changed_files"], [
            {"path": "README.md", "content": "after", "base_sha256": hashlib.sha256(b"before").hexdigest()},
            {"path": "new.txt", "content": "new", "base_sha256": None},
        ])
        self.assertEqual(result["deleted_files"], [])
        self.assertNotIn("secret internal instructions", repr(result))

    def test_rejects_parent_and_reserved_paths(self) -> None:
        for name in ["../private/SKILL.md", "/etc/passwd", ".agents/skills/x", "a//b", "a\\b"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                _run_sync(self.service, "do task", [{"path": name, "content": "x"}])

    def test_deletions_are_explicit(self) -> None:
        def fake_run(command, **kwargs):
            workspace = Path(kwargs["cwd"])
            (workspace / "old.txt").unlink()
            return type("Result", (), {"returncode": 0, "stdout": "done", "stderr": ""})()

        with patch("nunchaku.runner.subprocess.run", side_effect=fake_run):
            result = _run_sync(self.service, "delete file", [{"path": "old.txt", "content": "x"}])
        self.assertEqual(result["deleted_files"], [
            {"path": "old.txt", "base_sha256": hashlib.sha256(b"x").hexdigest()}
        ])

    def test_engine_error_does_not_return_private_stderr(self) -> None:
        def fake_run(command, **kwargs):
            return type("Result", (), {"returncode": 1, "stdout": "", "stderr": "secret internal instructions"})()

        with patch("nunchaku.runner.subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as caught:
                _run_sync(self.service, "task", [])
        self.assertNotIn("secret internal instructions", str(caught.exception))

    def test_custom_engine_substitutes_placeholders(self) -> None:
        service = Service("private", "do a task", self.service.skill, "custom", "127.0.0.1", 8765,
                          "my-agent", args=("run", "--cwd={workspace}", "-o", "{output}", "--skill", "{skill}", "{prompt}"))
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs["input"]))
            workspace = Path(kwargs["cwd"])
            self.assertEqual(command[2], f"--cwd={workspace}")
            self.assertTrue((Path(command[6]) / "SKILL.md").is_file())
            Path(command[4]).write_text("custom answer")
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("nunchaku.runner.subprocess.run", side_effect=fake_run):
            result = _run_sync(service, "literal {workspace} stays", [])
        command, stdin = calls[0]
        self.assertEqual(command[:2], ["my-agent", "run"])
        self.assertIn("literal {workspace} stays", command[-1])
        self.assertIsNone(stdin)
        self.assertEqual(result["answer"], "custom answer")
        self.assertEqual(result["engine"], "custom")

    def test_custom_engine_without_prompt_placeholder_uses_stdin(self) -> None:
        service = Service("private", "do a task", self.service.skill, "custom", "127.0.0.1", 8765,
                          "my-agent", args=("--stdin",))

        def fake_run(command, **kwargs):
            self.assertEqual(command, ["my-agent", "--stdin"])
            self.assertIn("User task:\ntask", kwargs["input"])
            return type("Result", (), {"returncode": 0, "stdout": "from stdout\n", "stderr": ""})()

        with patch("nunchaku.runner.subprocess.run", side_effect=fake_run):
            self.assertEqual(_run_sync(service, "task", [])["answer"], "from stdout")


class BridgeTests(unittest.TestCase):
    def test_concurrent_edit_prevents_auto_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "README.md"
            target.write_text("before")
            self.assertEqual(_snapshot(root, ["README.md"])[0]["content"], "before")
            target.write_text("concurrent edit")
            proposal = SkillResult.model_validate({
                "service": "demo", "engine": "codex", "answer": "done",
                "changed_files": [{
                    "path": "README.md", "content": "remote edit",
                    "base_sha256": hashlib.sha256(b"before").hexdigest(),
                }],
                "deleted_files": [], "note": "proposal",
            })
            result = _apply(root, proposal, True)
            self.assertEqual(result.conflicts, ["README.md"])
            self.assertEqual(target.read_text(), "concurrent edit")
            self.assertEqual(result.proposed_changes[0]["content"], "remote edit")

    def test_bridge_rejects_symlink_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside.txt"
            (root / "link.txt").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                _snapshot(root, ["link.txt"])


if __name__ == "__main__":
    unittest.main()
