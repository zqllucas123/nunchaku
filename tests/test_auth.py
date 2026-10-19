from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from nunchaku.admin import SESSION_COOKIE, create_app
from nunchaku.auth import KEY_PREFIX, MAX_FAILURES, AuthStore, auth_path


def _store(directory: str) -> AuthStore:
    return AuthStore(Path(directory) / "auth.db")


class AuthStoreTests(unittest.TestCase):
    def test_admin_login_lockout_and_password_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            self.assertFalse(store.has_admins())
            with self.assertRaises(ValueError):
                store.set_admin("root", "short", create=True)
            store.set_admin("root", "correct-horse", create=True)
            with self.assertRaises(ValueError):
                store.set_admin("root", "another-one", create=True)

            token = store.login("root", "correct-horse")
            self.assertEqual(store.session_admin(token), "root")
            self.assertIsNone(store.session_admin("forged"))
            with self.assertRaises(PermissionError):
                store.login("nobody", "correct-horse")

            for _ in range(MAX_FAILURES):
                with self.assertRaises(PermissionError):
                    store.login("root", "wrong-password")
            with self.assertRaisesRegex(PermissionError, "locked"):
                store.login("root", "correct-horse")  # right password, still locked
            # A CLI reset clears the lock and signs out existing sessions.
            store.set_admin("root", "new-password", create=False)
            self.assertIsNone(store.session_admin(token))
            token = store.login("root", "new-password")

            with self.assertRaises(PermissionError):
                store.change_password("root", "wrong", "whatever-long")
            store.change_password("root", "new-password", "newer-password")
            self.assertIsNone(store.session_admin(token))
            store.logout(store.login("root", "newer-password"))

    def test_keys_are_hashed_scoped_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            known = {"alpha", "beta"}
            with self.assertRaisesRegex(ValueError, "at least one"):
                store.create_key({"owner": "zhang"}, "root", known)
            with self.assertRaisesRegex(ValueError, "unknown services"):
                store.create_key({"owner": "zhang", "services": ["gamma"]}, "root", known)

            secret, key = store.create_key({"owner": "zhang", "department": "R&D", "services": ["alpha"]},
                                           "root", known)
            self.assertTrue(secret.startswith(KEY_PREFIX))
            self.assertTrue(secret.startswith(key.prefix))
            # Only a hash is stored.
            self.assertNotIn(secret.encode(), Path(directory, "auth.db").read_bytes())
            resolved = store.resolve(secret)
            self.assertEqual(resolved.id, key.id)
            self.assertTrue(resolved.allows("alpha"))
            self.assertFalse(resolved.allows("beta"))
            self.assertIsNone(store.resolve("nk_wrong"))
            self.assertIsNone(store.resolve(secret[len(KEY_PREFIX):]))

            store.update_key(key.id, {"enabled": False})
            self.assertIsNone(store.resolve(secret))
            store.update_key(key.id, {"enabled": True, "all_services": True})
            self.assertTrue(store.resolve(secret).allows("anything-added-later"))
            with self.assertRaises(ValueError):
                store.update_key(key.id, {"expires_at": time.time() - 10})

            store.record_use(key.id)
            store.record_use(key.id)
            store.flush_usage()
            self.assertEqual(store.key(key.id).use_count, 2)
            self.assertIsNotNone(store.key(key.id).last_used_at)

            store.delete_key(key.id)
            self.assertIsNone(store.resolve(secret))

    def test_expired_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            secret, key = store.create_key({"owner": "zhang", "all_services": True,
                                            "expires_at": time.time() + 60}, "root")
            self.assertIsNotNone(store.resolve(secret))
            self.assertFalse(store.key(key.id).usable(now=time.time() + 120))

    def test_service_rename_and_delete_follow_grants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _store(directory)
            _, both = store.create_key({"owner": "a", "services": ["alpha", "beta"]}, "root")
            _, only = store.create_key({"owner": "b", "services": ["alpha"]}, "root")
            self.assertEqual(store.rename_service("alpha", "alpha2"), 2)
            self.assertEqual(store.key(both.id).services, ("alpha2", "beta"))
            store.remove_service("alpha2")
            self.assertEqual(store.key(both.id).services, ("beta",))
            # Left with nothing granted, the key is disabled rather than silently allowing nothing.
            self.assertFalse(store.key(only.id).enabled)


class DashboardAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_gate_and_key_management(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("x", encoding="utf-8")
            config = root / "services.json"
            config.write_text(json.dumps({
                "dashboard": {"allowed_hosts": ["nunchaku.example.com:8768"],
                              "public_gateway_url": "https://nunchaku.example.com/mcp"},
                "services": [{"name": "alpha", "description": "A", "skill": str(skill), "engine": "codex",
                              "port": 9}],
            }), encoding="utf-8")
            app = create_app(config)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client:
                state = (await client.get("/api/session")).json()
                self.assertEqual(state, {"admin": None, "setup_required": True})
                self.assertEqual((await client.get("/api/services")).status_code, 401)
                self.assertEqual((await client.get("/api/keys")).status_code, 401)
                page = await client.get("/")
                self.assertEqual(page.status_code, 303)
                self.assertEqual(page.headers["location"], "/login")
                self.assertEqual((await client.get("/login")).status_code, 200)
                self.assertEqual((await client.get("/assets/login.js")).status_code, 200)
                self.assertEqual((await client.get("/assets/app.js")).status_code, 303)

                AuthStore(auth_path(config)).set_admin("root", "correct-horse", create=True)
                bad = await client.post("/api/session/login", json={"name": "root", "password": "nope"})
                self.assertEqual(bad.status_code, 401)
                good = await client.post("/api/session/login", json={"name": "root", "password": "correct-horse"})
                self.assertEqual(good.status_code, 200)
                cookie = good.headers["set-cookie"]
                self.assertIn(f"{SESSION_COOKIE}=", cookie)
                self.assertIn("HttpOnly", cookie)
                self.assertIn("samesite=strict", cookie.lower())
                self.assertEqual((await client.get("/api/session")).json()["admin"], "root")
                self.assertEqual((await client.get("/")).status_code, 200)
                gateway = (await client.get("/api/gateway")).json()
                self.assertEqual(gateway["public_endpoint"], "https://nunchaku.example.com/mcp")
                self.assertTrue(gateway["auth"])

                created = await client.post("/api/keys", json={"owner": "张三", "services": ["alpha"]})
                self.assertEqual(created.status_code, 201, created.text)
                secret = created.json()["secret"]
                key_id = created.json()["key"]["id"]
                listed = (await client.get("/api/keys")).json()["keys"]
                self.assertEqual(listed[0]["owner"], "张三")
                self.assertNotIn(secret, json.dumps(listed))  # never shown again
                unknown = await client.post("/api/keys", json={"owner": "x", "services": ["nope"]})
                self.assertEqual(unknown.status_code, 400)
                toggled = await client.put(f"/api/keys/{key_id}", json={"enabled": False})
                self.assertEqual(toggled.json()["status"], "disabled")
                # Cross-site writes are refused even with a valid session.
                forged = await client.post("/api/keys", json={"owner": "x", "all_services": True},
                                           headers={"Origin": "https://evil.example"})
                self.assertEqual(forged.status_code, 403)

                renamed = await client.put("/api/services/alpha", json={
                    "name": "alpha2", "description": "A", "skill": str(skill), "engine": "codex",
                    "host": "127.0.0.1", "port": 9, "timeout_seconds": 180})
                self.assertEqual(renamed.status_code, 200, renamed.text)
                self.assertEqual((await client.get("/api/keys")).json()["keys"][0]["services"], ["alpha2"])
                # The dashboard keeps the gateway/dashboard sections when it rewrites the file.
                self.assertIn("dashboard", json.loads(config.read_text(encoding="utf-8")))

                self.assertEqual((await client.delete(f"/api/keys/{key_id}",
                                                      headers={"Content-Type": "application/json"})).status_code, 204)
                await client.post("/api/session/logout", json={})
                self.assertEqual((await client.get("/api/keys")).status_code, 401)

            # A proxied Host is accepted only when configured.
            async with httpx.AsyncClient(transport=transport, base_url="http://nunchaku.example.com:8768") as proxied:
                self.assertEqual((await proxied.get("/api/session")).status_code, 200)
            async with httpx.AsyncClient(transport=transport, base_url="http://attacker.example") as other:
                self.assertEqual((await other.get("/api/session")).status_code, 400)


class AdminCliTests(unittest.TestCase):
    def test_add_list_passwd_remove(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "services.json"
            config.write_text(json.dumps({"services": []}), encoding="utf-8")

            def cli(*args: str, password: str | None = None) -> subprocess.CompletedProcess:
                return subprocess.run([sys.executable, "-m", "nunchaku", "--config", str(config), "admin", *args],
                                      input=password, capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=60)

            self.assertEqual(cli("add", "root", password="correct-horse\n").returncode, 0)
            self.assertEqual(cli("add", "root", password="correct-horse\n").returncode, 2)
            self.assertEqual(cli("add", "weak", password="short\n").returncode, 2)
            self.assertIn("root", cli("list").stdout)
            self.assertEqual(cli("passwd", "root", password="another-pass\n").returncode, 0)
            self.assertEqual(AuthStore(auth_path(config)).session_admin(
                AuthStore(auth_path(config)).login("root", "another-pass")), "root")
            self.assertEqual(cli("passwd", "ghost", password="another-pass\n").returncode, 2)
            self.assertEqual(cli("remove", "root").returncode, 0)
            self.assertNotIn("root", cli("list").stdout)


if __name__ == "__main__":
    unittest.main()
