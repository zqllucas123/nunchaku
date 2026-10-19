from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nunchaku import journal
from nunchaku.admin import GATEWAY_LOG, ServiceManager
from nunchaku.config import load_dashboard


def _entries(*messages: object) -> bytes:
    return b"\n".join(json.dumps({"MESSAGE": m, "_PID": "1"}).encode() for m in messages) + b"\n"


def _fake_run(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    def run(args, **_kwargs):
        run.args = args
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)
    return run


class ReadUnitTests(unittest.TestCase):
    def test_filters_by_service_tag_and_keeps_order(self) -> None:
        output = _entries(
            "INFO:     Started server process [42]",
            "10:00:01 INFO    [alpha] run started: 1 input file(s)",
            "10:00:02 INFO    [beta] run started: 1 input file(s)",
            "10:00:09 INFO    [alpha] run finished in 8.0s",
            list("[alpha] 中文".encode("utf-8")),  # journald's byte-array form
        )
        fake = _fake_run(output)
        with mock.patch.object(journal.shutil, "which", return_value="/bin/journalctl"), \
                mock.patch.object(journal.subprocess, "run", fake):
            text = journal.read_unit("nunchaku-services", "alpha")
            self.assertEqual(text.splitlines(), [
                "10:00:01 INFO    [alpha] run started: 1 input file(s)",
                "10:00:09 INFO    [alpha] run finished in 8.0s",
                "[alpha] 中文",
            ])
            self.assertIn("--unit", fake.args)
            self.assertEqual(fake.args[fake.args.index("--unit") + 1], "nunchaku-services")
            # Without a tag (the gateway's own unit) every line is kept.
            self.assertEqual(len(journal.read_unit("nunchaku-gateway").splitlines()), 5)

    def test_reports_permission_and_missing_journalctl(self) -> None:
        denied = _fake_run(stderr=b"No journal files were opened due to insufficient permissions.", returncode=1)
        with mock.patch.object(journal.shutil, "which", return_value="/bin/journalctl"), \
                mock.patch.object(journal.subprocess, "run", denied):
            with self.assertRaisesRegex(journal.JournalUnavailable, "insufficient permissions"):
                journal.read_unit("nunchaku-services", "alpha")
        with mock.patch.object(journal.shutil, "which", return_value=None):
            with self.assertRaisesRegex(journal.JournalUnavailable, "not found"):
                journal.read_unit("nunchaku-services")


class DashboardLogSourceTests(unittest.TestCase):
    def _setup(self, directory: str, extra: dict | None = None) -> tuple[ServiceManager, socket.socket, int]:
        root = Path(directory)
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("x", encoding="utf-8")
        # Something already listening on the port stands in for a systemd-run service.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = root / "services.json"
        config.write_text(json.dumps({**(extra or {}), "services": [
            {"name": "alpha", "description": "A", "skill": str(skill), "engine": "codex", "port": port}]}),
            encoding="utf-8")
        return ServiceManager(config), listener, port

    def test_external_service_reads_its_journal_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, listener, _ = self._setup(directory)
            with listener, mock.patch.object(journal, "read_unit", return_value="[alpha] run finished") as read:
                result = manager.logs("alpha")
            read.assert_called_once_with("nunchaku-services", "alpha")
            self.assertEqual(result, {"logs": "[alpha] run finished", "source": "journal",
                                      "unit": "nunchaku-services"})

    def test_unreadable_journal_explains_how_to_see_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, listener, _ = self._setup(directory)
            failure = journal.JournalUnavailable("insufficient permissions")
            with listener, mock.patch.object(journal, "read_unit", side_effect=failure):
                result = manager.logs("alpha")
            self.assertEqual(result["source"], "journal-unavailable")
            self.assertIn("journalctl -u nunchaku-services -f", result["logs"])

    def test_configured_units_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            extra = {"dashboard": {"services_unit": "skills@prod", "gateway_unit": ""}}
            manager, listener, _ = self._setup(directory, extra)
            settings = load_dashboard(manager.path)
            self.assertEqual((settings.services_unit, settings.gateway_unit), ("skills@prod", ""))
            with listener, mock.patch.object(journal, "read_unit", return_value="x") as read:
                manager.logs("alpha")
                read.assert_called_once_with("skills@prod", "alpha")
                # An empty unit name turns the journal off; the gateway falls back to its log file.
                self.assertEqual(manager.logs(GATEWAY_LOG)["source"], "file")

    def test_invalid_unit_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "services.json"
            config.write_text(json.dumps({"dashboard": {"services_unit": "x; rm -rf /"}, "services": []}),
                              encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "systemd unit name"):
                load_dashboard(config)

    def test_stopped_service_still_uses_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, listener, _ = self._setup(directory)
            listener.close()  # nothing on the port: not external
            with mock.patch.object(journal, "read_unit") as read:
                self.assertEqual(manager.logs("alpha"), {"logs": "尚无日志。", "source": "file"})
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
