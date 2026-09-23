"""Private PostgreSQL handoff for one local LangGraph run directory.

The local SQLite saver remains the LangGraph checkpointer. PostgreSQL stores
consistent snapshots of that database together with its audit evidence. A
session-level advisory lock permits only one host to advance a run at a time.
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from urllib.parse import parse_qs, urlparse
import zipfile

from langgraph.checkpoint.sqlite import SqliteSaver

from .audit import read_json, replace_json


class SharedStateError(RuntimeError):
    """A handoff cannot proceed safely; never include a connection URL here."""


def _archive(run_dir: Path, connection: sqlite3.Connection | None = None) -> bytes:
    """Snapshot SQLite through its backup API, including committed WAL content."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(run_dir.rglob("*")):
            if any(part.startswith(".") for part in path.relative_to(run_dir).parts):
                continue
            if path.is_symlink():
                raise SharedStateError("run evidence contains a symlink; refusing shared handoff")
            if (
                not path.is_file() or path.name == "auth.json" or path.suffix == ".tmp"
            ):
                continue
            relative = path.relative_to(run_dir).as_posix()
            if relative in {"checkpoint.sqlite", "checkpoint.sqlite-wal", "checkpoint.sqlite-shm"}:
                continue
            archive.write(path, relative)
        checkpoint = run_dir / "checkpoint.sqlite"
        if checkpoint.exists():
            with tempfile.NamedTemporaryFile(
                prefix=".checkpoint-snapshot-", suffix=".sqlite", dir=run_dir, delete=False
            ) as temporary:
                backup_path = Path(temporary.name)
            try:
                backup = sqlite3.connect(backup_path)
                try:
                    if connection is not None:
                        connection.backup(backup)
                    else:
                        source = sqlite3.connect(checkpoint)
                        try:
                            source.backup(backup)
                        finally:
                            source.close()
                finally:
                    backup.close()
                archive.write(backup_path, "checkpoint.sqlite")
            finally:
                backup_path.unlink(missing_ok=True)
    return output.getvalue()


def _extract(snapshot: bytes, run_dir: Path) -> None:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SharedStateError("shared resume requires a new empty local --run-dir")
    run_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
        for item in archive.infolist():
            relative = Path(item.filename)
            if (
                item.is_dir() or relative.is_absolute() or ".." in relative.parts
                or item.filename.replace("\\", "/") != item.filename
            ):
                raise SharedStateError("shared snapshot contains an unsafe path")
            target = run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as destination:
                while block := source.read(1024 * 1024):
                    destination.write(block)


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured) if configured else Path.home() / ".codex").resolve()


def _valid_session_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value))


class SharedRunStore:
    """One direct SSL PostgreSQL connection and exclusive run lock."""

    def __init__(self, run_id: str, url: str, run_dir: Path):
        if not url:
            raise SharedStateError("ASTRA_STATE_DATABASE_URL is required for shared runs")
        parsed = urlparse(url)
        sslmode = parse_qs(parsed.query).get("sslmode", [""])[0].lower()
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise SharedStateError("shared state requires a PostgreSQL connection URL")
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise SharedStateError("shared PostgreSQL URL must require SSL")
        if "-pooler" in parsed.hostname:
            raise SharedStateError("shared runs require a direct PostgreSQL endpoint, not a pooler")
        connection = None
        try:
            import psycopg

            connection = psycopg.connect(url, autocommit=True, connect_timeout=30)
            self.connection = connection
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS astra_shared_runs ("
                "run_id TEXT PRIMARY KEY, snapshot BYTEA NOT NULL, sha256 TEXT NOT NULL, "
                "version BIGINT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS astra_shared_sessions ("
                "run_id TEXT NOT NULL, session_id TEXT NOT NULL, relative_path TEXT NOT NULL, "
                "content BYTEA NOT NULL, sha256 TEXT NOT NULL, "
                "PRIMARY KEY (run_id, session_id, relative_path))"
            )
            locked = self.connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (run_id,)
            ).fetchone()[0]
            if not locked:
                self.connection.close()
                raise SharedStateError("this shared run is already active on another host")
        except SharedStateError:
            raise
        except Exception as error:
            if connection is not None:
                connection.close()
            raise SharedStateError("could not connect to private shared PostgreSQL") from error
        self.run_id = run_id
        self.run_dir = run_dir.resolve()
        self.sqlite_connection: sqlite3.Connection | None = None
        self.lock = threading.RLock()
        self.expected_sha: str | None = None

    def _record_base(self, digest: str) -> None:
        replace_json(self.run_dir / ".shared-base.json", {
            "run_id": self.run_id, "snapshot_sha256": digest,
        })

    def exists(self) -> bool:
        try:
            return self.connection.execute(
                "SELECT 1 FROM astra_shared_runs WHERE run_id = %s", (self.run_id,)
            ).fetchone() is not None
        except Exception as error:
            raise SharedStateError("could not read shared run") from error

    def fetch(self, *, restore_sessions: bool = True) -> None:
        try:
            row = self.connection.execute(
                "SELECT snapshot, sha256 FROM astra_shared_runs WHERE run_id = %s",
                (self.run_id,),
            ).fetchone()
            if row is None:
                raise SharedStateError("shared run ID was not found")
            snapshot = bytes(row[0])
            if hashlib.sha256(snapshot).hexdigest() != row[1]:
                raise SharedStateError("shared run snapshot hash mismatch")
            _extract(snapshot, self.run_dir)
            self.expected_sha = row[1]
            self._record_base(row[1])
            if restore_sessions:
                self.restore_sessions()
        except SharedStateError:
            raise
        except Exception as error:
            raise SharedStateError("could not restore shared run") from error

    def save(self) -> None:
        with self.lock:
            snapshot = _archive(self.run_dir, self.sqlite_connection)
            digest = hashlib.sha256(snapshot).hexdigest()
            try:
                row = self.connection.execute(
                    "SELECT sha256 FROM astra_shared_runs WHERE run_id = %s", (self.run_id,)
                ).fetchone()
                current = row[0] if row else None
                if current != self.expected_sha:
                    raise SharedStateError("shared run changed on another host; refusing stale overwrite")
                self.connection.execute(
                    "INSERT INTO astra_shared_runs (run_id, snapshot, sha256) VALUES (%s, %s, %s) "
                    "ON CONFLICT (run_id) DO UPDATE SET snapshot = EXCLUDED.snapshot, "
                    "sha256 = EXCLUDED.sha256, version = astra_shared_runs.version + 1, "
                    "updated_at = now()",
                    (self.run_id, snapshot, digest),
                )
                self.expected_sha = digest
                self._record_base(digest)
            except SharedStateError:
                raise
            except Exception as error:
                raise SharedStateError("could not persist shared run") from error

    def repair_from_local(self) -> None:
        """Publish a local crash-window receipt only if the remote base is unchanged."""
        base = read_json(self.run_dir / ".shared-base.json")
        if base.get("run_id") != self.run_id:
            raise SharedStateError("local cache belongs to another shared run")
        digest = base.get("snapshot_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise SharedStateError("local cache has no valid shared base")
        self.expected_sha = digest
        self.save()

    def save_session(self, session_id: str) -> bool:
        """Transfer only the named Astra/Sol rollout, never auth/config files."""
        if not _valid_session_id(session_id):
            return False
        sessions = _codex_home() / "sessions"
        matches = list(sessions.rglob(f"rollout-*-{session_id}.jsonl")) if sessions.is_dir() else []
        if len(matches) != 1:
            return False
        path = matches[0]
        relative = path.relative_to(sessions).as_posix()
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        try:
            with self.lock:
                self.connection.execute(
                    "INSERT INTO astra_shared_sessions "
                    "(run_id, session_id, relative_path, content, sha256) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (run_id, session_id, relative_path) DO UPDATE SET "
                    "content = EXCLUDED.content, sha256 = EXCLUDED.sha256",
                    (self.run_id, session_id, relative, content, digest),
                )
        except Exception as error:
            raise SharedStateError("could not persist Codex session rollout") from error
        return True

    def restore_sessions(self) -> None:
        sessions = _codex_home() / "sessions"
        try:
            rows = self.connection.execute(
                "SELECT session_id, relative_path, content, sha256 "
                "FROM astra_shared_sessions WHERE run_id = %s",
                (self.run_id,),
            ).fetchall()
            for session_id, relative_name, content_value, digest in rows:
                relative = Path(relative_name)
                if (
                    not _valid_session_id(session_id)
                    or relative_name.replace("\\", "/") != relative_name
                    or relative.is_absolute() or relative.drive or ".." in relative.parts
                    or relative.suffix != ".jsonl"
                    or not relative.name.startswith("rollout-")
                    or not relative.name.endswith(f"-{session_id}.jsonl")
                ):
                    raise SharedStateError("unsafe Codex session path in shared storage")
                content = bytes(content_value)
                if hashlib.sha256(content).hexdigest() != digest:
                    raise SharedStateError("Codex session rollout hash mismatch")
                target = sessions / relative
                if not target.resolve().is_relative_to(sessions.resolve()):
                    raise SharedStateError("Codex session path escapes its local directory")
                if target.exists():
                    existing = target.read_bytes()
                    if existing == content:
                        continue
                    if not content.startswith(existing):
                        raise SharedStateError("local Codex session conflicts with shared rollout")
                    with tempfile.NamedTemporaryFile(
                        prefix=".astra-rollout-", suffix=".tmp", dir=target.parent, delete=False
                    ) as temporary:
                        temporary.write(content)
                        replacement = Path(temporary.name)
                    os.replace(replacement, target)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as destination:
                    destination.write(content)
        except SharedStateError:
            raise
        except Exception as error:
            raise SharedStateError("could not restore Codex sessions") from error

    def ensure_session(self, session_id: str) -> None:
        if not _valid_session_id(session_id):
            raise SharedStateError("Codex session ID is not a portable UUID")
        sessions = _codex_home() / "sessions"
        if not sessions.is_dir() or not any(sessions.rglob(f"rollout-*-{session_id}.jsonl")):
            raise SharedStateError(
                "Codex session rollout is unavailable on this host; refusing implicit restart"
            )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SharedRunStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class MirroredSqliteSaver(SqliteSaver):
    """Publish every committed checkpoint and pending write before proceeding."""

    def __init__(self, connection: sqlite3.Connection, store: SharedRunStore):
        super().__init__(connection)
        self.lock = threading.RLock()
        self.store = store

    def put(self, *args, **kwargs):
        with self.lock:
            result = super().put(*args, **kwargs)
            self.store.save()
            return result

    def put_writes(self, *args, **kwargs):
        with self.lock:
            result = super().put_writes(*args, **kwargs)
            self.store.save()
            return result
