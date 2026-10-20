"""serve-all must not spawn a second copy of a service somebody else already serves.

Background: nunchaku-services runs the services in the config as of unit start. A service
added later is started from the dashboard instead. If the unit is then restarted, its
snapshot includes that service too — and spawning it again loses the port race: uvicorn
dies with EADDRINUSE while the unit still reports active, and the port never comes back
under its original owner. So serve-all must skip anything already listening.
"""
from __future__ import annotations

import socket
import unittest
from pathlib import Path

from nunchaku.cli import _split_by_owner
from nunchaku.config import Service


def _service(name: str, port: int, host: str = "127.0.0.1") -> Service:
    return Service(name, "description", Path("skill"), "codex", host, port)


class SplitByOwnerTests(unittest.TestCase):
    def test_a_served_service_is_skipped_and_free_ones_are_kept(self) -> None:
        with socket.socket() as served, socket.socket() as idle:
            served.bind(("127.0.0.1", 0))
            served.listen()
            # Bound but never listened on, so nothing answers there: a stand-in for a
            # port that is genuinely free. Holding the socket keeps it that way.
            idle.bind(("127.0.0.1", 0))
            free, taken = _split_by_owner([_service("served", served.getsockname()[1]),
                                           _service("idle", idle.getsockname()[1])])
        self.assertEqual([service.name for service in free], ["idle"])
        self.assertEqual([service.name for service in taken], ["served"])

    def test_wildcard_host_is_probed_on_loopback(self) -> None:
        """A 0.0.0.0 listener must still be detected, not silently spawned twice."""
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            free, taken = _split_by_owner([_service("svc", holder.getsockname()[1], host="0.0.0.0")])
        self.assertEqual(free, [])
        self.assertEqual([service.name for service in taken], ["svc"])

    def test_nothing_is_skipped_when_every_port_is_free(self) -> None:
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            free, taken = _split_by_owner([_service("a", first.getsockname()[1]),
                                           _service("b", second.getsockname()[1])])
        self.assertEqual([service.name for service in free], ["a", "b"])
        self.assertEqual(taken, [])


if __name__ == "__main__":
    unittest.main()