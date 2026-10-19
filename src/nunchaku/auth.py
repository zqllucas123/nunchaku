"""Admin accounts, dashboard sessions, and service keys, stored in SQLite.

The dashboard and the gateway are separate processes that both open this
database, so writes go through short transactions and WAL mode.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import SERVICE_NAME

KEY_PREFIX = "nk_"
SESSION_TTL_SECONDS = 12 * 3600
# Lock an account after this many consecutive failures, for this long.
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
# Coarsen last_used_at writes so a busy key doesn't write on every call.
USAGE_WRITE_INTERVAL = 30.0
ADMIN_NAME_MAX = 64
MIN_PASSWORD = 8

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    name TEXT PRIMARY KEY,
    password TEXT NOT NULL,
    created_at REAL NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    admin TEXT NOT NULL REFERENCES admins(name) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS service_keys (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    owner TEXT NOT NULL,
    department TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    all_services INTEGER NOT NULL DEFAULT 0,
    services TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    expires_at REAL,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL,
    last_used_at REAL,
    use_count INTEGER NOT NULL DEFAULT 0
);
"""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(candidate.hex(), digest)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ServiceKey:
    id: str
    prefix: str
    owner: str
    department: str
    email: str
    note: str
    all_services: bool
    services: tuple[str, ...]
    enabled: bool
    expires_at: float | None
    created_at: float
    created_by: str
    last_used_at: float | None
    use_count: int

    def allows(self, service: str) -> bool:
        return self.all_services or service in self.services

    def usable(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.enabled and (self.expires_at is None or self.expires_at > now)

    def public(self) -> dict:
        return {
            "id": self.id, "prefix": self.prefix, "owner": self.owner, "department": self.department,
            "email": self.email, "note": self.note, "all_services": self.all_services,
            "services": list(self.services), "enabled": self.enabled, "expires_at": self.expires_at,
            "created_at": self.created_at, "created_by": self.created_by,
            "last_used_at": self.last_used_at, "use_count": self.use_count,
            "status": ("disabled" if not self.enabled else
                       "expired" if self.expires_at is not None and self.expires_at <= time.time() else "active"),
        }


def _row_to_key(row: sqlite3.Row) -> ServiceKey:
    return ServiceKey(
        id=row["id"], prefix=row["prefix"], owner=row["owner"], department=row["department"],
        email=row["email"], note=row["note"], all_services=bool(row["all_services"]),
        services=tuple(json.loads(row["services"])), enabled=bool(row["enabled"]),
        expires_at=row["expires_at"], created_at=row["created_at"], created_by=row["created_by"],
        last_used_at=row["last_used_at"], use_count=row["use_count"],
    )


def _text(value: object, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field} must be text up to {limit} characters")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    return value


class AuthStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        self._lock = threading.RLock()
        self._pending: dict[str, int] = {}
        self._written: dict[str, float] = {}
        with self._connect() as db:
            db.executescript(SCHEMA)
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            yield db
        finally:
            db.close()

    @contextmanager
    def _tx(self):
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # --- admins -----------------------------------------------------------

    def has_admins(self) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM admins LIMIT 1").fetchone() is not None

    def admins(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT name, created_at, locked_until FROM admins ORDER BY name").fetchall()
        return [{"name": r["name"], "created_at": r["created_at"], "locked": r["locked_until"] > time.time()}
                for r in rows]

    def set_admin(self, name: str, password: str, create: bool) -> None:
        name = _text(name, "admin name", ADMIN_NAME_MAX, required=True)
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        hashed = hash_password(password)
        with self._tx() as db:
            exists = db.execute("SELECT 1 FROM admins WHERE name = ?", (name,)).fetchone() is not None
            if create and exists:
                raise ValueError(f"admin already exists: {name}")
            if not create and not exists:
                raise KeyError(f"unknown admin: {name}")
            if exists:
                db.execute("UPDATE admins SET password = ?, failures = 0, locked_until = 0 WHERE name = ?",
                           (hashed, name))
                db.execute("DELETE FROM sessions WHERE admin = ?", (name,))
            else:
                db.execute("INSERT INTO admins (name, password, created_at) VALUES (?, ?, ?)",
                           (name, hashed, time.time()))

    def remove_admin(self, name: str) -> None:
        with self._tx() as db:
            if db.execute("DELETE FROM admins WHERE name = ?", (name,)).rowcount == 0:
                raise KeyError(f"unknown admin: {name}")

    def change_password(self, name: str, current: str, new: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT password FROM admins WHERE name = ?", (name,)).fetchone()
        if row is None or not verify_password(current, row["password"]):
            raise PermissionError("current password is incorrect")
        self.set_admin(name, new, create=False)

    def login(self, name: str, password: str) -> str:
        """Return a new session token, or raise PermissionError."""
        now = time.time()
        # Decide inside the transaction, raise after it: raising inside would roll back the failure count.
        error: str | None = None
        with self._tx() as db:
            row = db.execute("SELECT password, failures, locked_until FROM admins WHERE name = ?",
                             (name,)).fetchone()
            if row is None:
                # Spend comparable time so unknown names aren't distinguishable by timing.
                verify_password(password, hash_password("x" * MIN_PASSWORD))
                error = "invalid name or password"
            elif row["locked_until"] > now:
                error = "account is temporarily locked; try again later"
            elif not verify_password(password, row["password"]):
                failures = row["failures"] + 1
                locked = now + LOCKOUT_SECONDS if failures >= MAX_FAILURES else 0
                db.execute("UPDATE admins SET failures = ?, locked_until = ? WHERE name = ?",
                           (0 if locked else failures, locked, name))
                error = "invalid name or password"
            else:
                db.execute("UPDATE admins SET failures = 0, locked_until = 0 WHERE name = ?", (name,))
                db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO sessions (token_hash, admin, expires_at) VALUES (?, ?, ?)",
                           (_sha256(token), name, now + SESSION_TTL_SECONDS))
        if error:
            raise PermissionError(error)
        return token

    def session_admin(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._connect() as db:
            row = db.execute("SELECT admin, expires_at FROM sessions WHERE token_hash = ?",
                             (_sha256(token),)).fetchone()
        if row is None or row["expires_at"] <= time.time():
            return None
        return row["admin"]

    def logout(self, token: str | None) -> None:
        if token:
            with self._tx() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha256(token),))

    # --- service keys -----------------------------------------------------

    def _validate_grant(self, item: dict, known: set[str] | None) -> tuple[bool, list[str]]:
        all_services = item.get("all_services", False)
        if not isinstance(all_services, bool):
            raise ValueError("all_services must be true or false")
        services = item.get("services", [])
        if not isinstance(services, list) or not all(isinstance(s, str) and SERVICE_NAME.fullmatch(s)
                                                      for s in services):
            raise ValueError("services must be a list of service names")
        services = sorted(set(services))
        if known is not None and set(services) - known:
            raise ValueError(f"unknown services: {sorted(set(services) - known)}")
        if not all_services and not services:
            raise ValueError("grant at least one service, or all services")
        return all_services, [] if all_services else services

    @staticmethod
    def _expiry(value: object) -> float | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= time.time():
            raise ValueError("expires_at must be a future Unix timestamp")
        return float(value)

    def create_key(self, item: dict, created_by: str, known: set[str] | None = None) -> tuple[str, ServiceKey]:
        """Create a key. Returns the plaintext key (shown once) and its record."""
        if not isinstance(item, dict):
            raise ValueError("key must be an object")
        owner = _text(item.get("owner"), "owner", 100, required=True)
        department = _text(item.get("department"), "department", 100)
        email = _text(item.get("email"), "email", 200)
        note = _text(item.get("note"), "note", 500)
        all_services, services = self._validate_grant(item, known)
        expires_at = self._expiry(item.get("expires_at"))
        plaintext = KEY_PREFIX + secrets.token_hex(32)
        key_id = secrets.token_hex(8)
        with self._tx() as db:
            db.execute(
                "INSERT INTO service_keys (id, key_hash, prefix, owner, department, email, note, all_services,"
                " services, enabled, expires_at, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)",
                (key_id, _sha256(plaintext), plaintext[:len(KEY_PREFIX) + 6], owner, department, email, note,
                 int(all_services), json.dumps(services), expires_at, time.time(), created_by),
            )
        return plaintext, self.key(key_id)

    def update_key(self, key_id: str, item: dict, known: set[str] | None = None) -> ServiceKey:
        if not isinstance(item, dict):
            raise ValueError("key must be an object")
        current = self.key(key_id)
        fields: dict[str, object] = {}
        for name, limit in (("owner", 100), ("department", 100), ("email", 200), ("note", 500)):
            if name in item:
                fields[name] = _text(item[name], name, limit, required=name == "owner")
        if "all_services" in item or "services" in item:
            merged = {"all_services": item.get("all_services", current.all_services),
                      "services": item.get("services", list(current.services))}
            all_services, services = self._validate_grant(merged, known)
            fields["all_services"] = int(all_services)
            fields["services"] = json.dumps(services)
        if "enabled" in item:
            if not isinstance(item["enabled"], bool):
                raise ValueError("enabled must be true or false")
            fields["enabled"] = int(item["enabled"])
        if "expires_at" in item:
            fields["expires_at"] = self._expiry(item["expires_at"])
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            with self._tx() as db:
                db.execute(f"UPDATE service_keys SET {assignments} WHERE id = ?", (*fields.values(), key_id))
        return self.key(key_id)

    def delete_key(self, key_id: str) -> None:
        with self._tx() as db:
            if db.execute("DELETE FROM service_keys WHERE id = ?", (key_id,)).rowcount == 0:
                raise KeyError(f"unknown key: {key_id}")

    def key(self, key_id: str) -> ServiceKey:
        with self._connect() as db:
            row = db.execute("SELECT * FROM service_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown key: {key_id}")
        return _row_to_key(row)

    def keys(self) -> list[ServiceKey]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM service_keys ORDER BY created_at DESC").fetchall()
        return [_row_to_key(row) for row in rows]

    def resolve(self, plaintext: str | None) -> ServiceKey | None:
        """The usable key matching a presented secret, or None."""
        if not plaintext or not plaintext.startswith(KEY_PREFIX):
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM service_keys WHERE key_hash = ?", (_sha256(plaintext),)).fetchone()
        if row is None:
            return None
        key = _row_to_key(row)
        return key if key.usable() else None

    def record_use(self, key_id: str) -> None:
        now = time.time()
        with self._lock:
            self._pending[key_id] = self._pending.get(key_id, 0) + 1
            if now - self._written.get(key_id, 0.0) < USAGE_WRITE_INTERVAL:
                return
            count = self._pending.pop(key_id)
            self._written[key_id] = now
        self._write_usage({key_id: count}, now)

    def flush_usage(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        if pending:
            self._write_usage(pending, time.time())

    def _write_usage(self, counts: dict[str, int], now: float) -> None:
        with self._tx() as db:
            for key_id, count in counts.items():
                db.execute("UPDATE service_keys SET last_used_at = ?, use_count = use_count + ? WHERE id = ?",
                           (now, count, key_id))

    # --- service lifecycle ------------------------------------------------

    def rename_service(self, old: str, new: str) -> int:
        """Rewrite grants after a service rename. Returns how many keys changed."""
        changed = 0
        with self._tx() as db:
            for row in db.execute("SELECT id, services FROM service_keys").fetchall():
                services = json.loads(row["services"])
                if old in services:
                    services = sorted({new if s == old else s for s in services})
                    db.execute("UPDATE service_keys SET services = ? WHERE id = ?", (json.dumps(services), row["id"]))
                    changed += 1
        return changed

    def remove_service(self, name: str) -> int:
        """Drop a deleted service from every grant. Keys left with no grant are disabled."""
        changed = 0
        with self._tx() as db:
            for row in db.execute("SELECT id, services, all_services FROM service_keys").fetchall():
                services = json.loads(row["services"])
                if name in services:
                    services.remove(name)
                    enabled_clause = ", enabled = 0" if not services and not row["all_services"] else ""
                    db.execute(f"UPDATE service_keys SET services = ?{enabled_clause} WHERE id = ?",
                               (json.dumps(services), row["id"]))
                    changed += 1
        return changed


def auth_path(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve().parent / ".nunchaku" / "auth.db"
