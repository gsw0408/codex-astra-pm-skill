"""Durable, append-only evidence helpers for orchestration runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .schema import SCHEMA_VERSION


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_exclusive(path: Path, value: Any) -> str:
    """Create an immutable JSON artifact and return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(value)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return sha256_bytes(data)


def replace_json(path: Path, value: Any) -> None:
    """Atomically replace mutable controller metadata, never evidence artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(value)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


class AmbiguousCallError(RuntimeError):
    """An external call may have produced side effects but lacks a final receipt."""


@dataclass(frozen=True)
class RecoveredCall:
    result: dict[str, Any]
    receipt: dict[str, Any]


class AuditLog:
    """Own the private evidence directory for one LangGraph thread."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir.resolve()
        self.calls_dir = self.run_dir / "calls"
        self.reviews_dir = self.run_dir / "reviews"

    @classmethod
    def create(cls, run_dir: Path, manifest: Mapping[str, Any]) -> "AuditLog":
        run_dir = run_dir.resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
        audit = cls(run_dir)
        audit.calls_dir.mkdir()
        audit.reviews_dir.mkdir()
        write_json_exclusive(run_dir / "manifest.json", dict(manifest))
        audit.append_event("RUN_CREATED", {"manifest_sha256": sha256_file(run_dir / "manifest.json")})
        return audit

    @classmethod
    def open(cls, run_dir: Path) -> "AuditLog":
        audit = cls(run_dir)
        if not (audit.run_dir / "manifest.json").is_file():
            raise FileNotFoundError(f"orchestration manifest not found: {audit.run_dir}")
        audit.calls_dir.mkdir(exist_ok=True)
        audit.reviews_dir.mkdir(exist_ok=True)
        return audit

    def append_jsonl(self, filename: str, value: Mapping[str, Any]) -> None:
        path = self.run_dir / filename
        row = {"recorded_at": utc_now(), **dict(value)}
        with path.open("ab") as handle:
            handle.write(canonical_json(row))
            handle.flush()
            os.fsync(handle.fileno())

    def append_jsonl_once(
        self,
        filename: str,
        value: Mapping[str, Any],
        *,
        identity_fields: tuple[str, ...],
    ) -> None:
        """Append one keyed record, tolerating an exact checkpoint replay.

        External role output and audit writes happen before LangGraph commits
        the node update.  If the process stops in that window, replay must not
        duplicate a plan revision or recovery attempt, and a reused identity
        with different content must fail closed.
        """
        if not identity_fields or any(field not in value for field in identity_fields):
            raise ValueError("keyed JSONL records require every identity field")
        path = self.run_dir / filename
        expected = dict(value)
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    existing = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(existing, dict) or not all(
                    existing.get(field) == expected[field] for field in identity_fields
                ):
                    continue
                comparable = {key: item for key, item in existing.items() if key != "recorded_at"}
                if comparable != expected:
                    identity = ", ".join(
                        f"{field}={expected[field]!r}" for field in identity_fields
                    )
                    raise AmbiguousCallError(
                        f"{filename} contains conflicting keyed evidence ({identity})"
                    )
                return
        self.append_jsonl(filename, expected)

    def append_event(self, event: str, details: Mapping[str, Any]) -> None:
        self.append_jsonl("events.jsonl", {"event": event, "details": dict(details)})

    def append_transition(self, transition: Mapping[str, Any]) -> None:
        transition_id = transition.get("transition_id")
        path = self.run_dir / "transitions.jsonl"
        if transition_id and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("transition_id") == transition_id:
                        return
                except ValueError:
                    continue
        self.append_jsonl("transitions.jsonl", dict(transition))

    def append_error(self, error: Mapping[str, Any]) -> None:
        self.append_jsonl("errors.jsonl", dict(error))

    def call_dir(self, call_id: str) -> Path:
        if not call_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in call_id):
            raise ValueError("call_id must contain only letters, digits, dash, and underscore")
        return self.calls_dir / call_id

    def review_dir(self, review_id: str) -> Path:
        if (
            not review_id
            or len(review_id) > 128
            or not review_id[0].isalnum()
            or not review_id[0].isascii()
            or any(
                not (character.isascii() and (character.isalnum() or character in "-_"))
                for character in review_id
            )
        ):
            raise ValueError(
                "review_id must start with an ASCII letter or digit and contain only "
                "ASCII letters, digits, dash, or underscore"
            )
        directory = (self.reviews_dir / review_id).resolve()
        if directory.parent != self.reviews_dir.resolve():
            raise ValueError("review_id resolves outside the reviews directory")
        return directory

    def observed_session_ids(self, *roles: str) -> set[str]:
        """Return sessions for the selected roles from durable process receipts."""
        observed: set[str] = set()
        if not self.calls_dir.is_dir():
            return observed
        selected = set(roles)
        for process_path in self.calls_dir.glob("*/process.json"):
            try:
                process = read_json(process_path)
            except (OSError, ValueError):
                continue
            session_id = process.get("session_id")
            if (
                (not selected or process.get("role") in selected)
                and isinstance(session_id, str)
                and session_id
            ):
                observed.add(session_id)
        return observed

    def observed_reviewer_session_ids(self) -> set[str]:
        """Return every Reviewer session observed in durable process receipts."""
        return self.observed_session_ids("reviewer")

    def observed_fresh_session_ids(self) -> set[str]:
        """Return every Reviewer or Luna session, including failed attempts."""
        return self.observed_session_ids("reviewer", "luna")

    def recover_call(self, call_id: str) -> RecoveredCall | None:
        directory = self.call_dir(call_id)
        if not directory.exists():
            return None
        status_path = directory / "status.json"
        if not status_path.is_file():
            raise AmbiguousCallError(f"call {call_id} has a directory but no status")
        try:
            status = read_json(status_path)
        except (OSError, ValueError) as error:
            raise AmbiguousCallError(f"call {call_id} has an unreadable status") from error
        if status.get("status") != "SUCCEEDED":
            raise AmbiguousCallError(
                f"call {call_id} stopped in {status.get('status', 'UNKNOWN')} state; refusing automatic re-execution"
            )
        request_path = directory / "request.json"
        schema_path = directory / "output-schema.json"
        result_path = directory / "result.json"
        receipt_path = directory / "receipt.json"
        if not result_path.is_file() or not receipt_path.is_file():
            raise AmbiguousCallError(f"call {call_id} claims success but final artifacts are missing")
        for field, path, label in (
            ("request_sha256", request_path, "request"),
            ("output_schema_sha256", schema_path, "output schema"),
            ("result_sha256", result_path, "result"),
            ("receipt_sha256", receipt_path, "receipt"),
        ):
            if not path.is_file():
                raise AmbiguousCallError(f"call {call_id} {label} artifact is missing")
            try:
                actual_hash = sha256_file(path)
            except OSError as error:
                raise AmbiguousCallError(
                    f"call {call_id} {label} artifact is unreadable"
                ) from error
            if status.get(field) != actual_hash:
                raise AmbiguousCallError(
                    f"call {call_id} {label} hash does not match its status"
                )
        try:
            result = read_json(result_path)
            receipt = read_json(receipt_path)
        except (OSError, ValueError) as error:
            raise AmbiguousCallError(f"call {call_id} has unreadable final artifacts") from error
        if receipt.get("result_sha256") != status["result_sha256"]:
            raise AmbiguousCallError(f"call {call_id} result hash does not match its receipt")
        return RecoveredCall(result, receipt)

    def prepare_call(
        self,
        call_id: str,
        role: str,
        request: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Path:
        directory = self.call_dir(call_id)
        directory.mkdir(parents=False, exist_ok=False)
        request_hash = write_json_exclusive(directory / "request.json", dict(request))
        schema_hash = write_json_exclusive(directory / "output-schema.json", dict(output_schema))
        replace_json(
            directory / "status.json",
            {
                "schema_version": SCHEMA_VERSION,
                "call_id": call_id,
                "role": role,
                "status": "PREPARED",
                "request_sha256": request_hash,
                "output_schema_sha256": schema_hash,
                "updated_at": utc_now(),
            },
        )
        self.append_event("CALL_PREPARED", {"call_id": call_id, "role": role})
        return directory

    def mark_call_running(self, call_id: str) -> None:
        directory = self.call_dir(call_id)
        status = read_json(directory / "status.json")
        if status.get("status") != "PREPARED":
            raise ValueError(f"call {call_id} cannot move from {status.get('status')} to RUNNING")
        status.update(status="RUNNING", updated_at=utc_now())
        replace_json(directory / "status.json", status)
        self.append_event("CALL_RUNNING", {"call_id": call_id, "role": status["role"]})

    def finish_call(
        self,
        call_id: str,
        result: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        directory = self.call_dir(call_id)
        status = read_json(directory / "status.json")
        if status.get("status") != "RUNNING":
            raise ValueError(f"call {call_id} cannot finish from {status.get('status')}")
        result_hash = write_json_exclusive(directory / "result.json", dict(result))
        final_receipt = {**dict(receipt), "result_sha256": result_hash}
        receipt_hash = write_json_exclusive(directory / "receipt.json", final_receipt)
        status.update(status="SUCCEEDED", result_sha256=result_hash, receipt_sha256=receipt_hash, updated_at=utc_now())
        replace_json(directory / "status.json", status)
        self.append_event("CALL_SUCCEEDED", {"call_id": call_id, "role": status["role"], "receipt_sha256": receipt_hash})
        return final_receipt

    def fail_call(self, call_id: str, error: Mapping[str, Any], *, ambiguous: bool) -> None:
        directory = self.call_dir(call_id)
        status = read_json(directory / "status.json")
        error_hash = write_json_exclusive(directory / "error.json", dict(error))
        status.update(
            status="AMBIGUOUS" if ambiguous else "FAILED",
            error_sha256=error_hash,
            updated_at=utc_now(),
        )
        replace_json(directory / "status.json", status)
        self.append_error({"call_id": call_id, "role": status.get("role"), "ambiguous": ambiguous, **dict(error)})

    def write_review_packet(self, review_id: str, packet: Mapping[str, Any]) -> tuple[Path, str]:
        directory = self.review_dir(review_id)
        path = directory / "packet.json"
        expected = sha256_bytes(canonical_json(dict(packet)))
        if directory.exists():
            if not path.is_file() or sha256_file(path) != expected:
                raise AmbiguousCallError(
                    f"review packet directory {review_id} exists with different or incomplete evidence"
                )
            receipt_path = directory / "packet-receipt.json"
            if not receipt_path.is_file():
                raise AmbiguousCallError(
                    f"review packet {review_id} is missing its hash receipt"
                )
            try:
                receipt = read_json(receipt_path)
            except (OSError, ValueError) as error:
                raise AmbiguousCallError(
                    f"review packet {review_id} has an unreadable hash receipt"
                ) from error
            if (
                receipt.get("review_id") != review_id
                or receipt.get("packet_sha256") != expected
            ):
                raise AmbiguousCallError(
                    f"review packet {review_id} hash receipt does not match the packet"
                )
            return path, expected
        directory.mkdir(parents=False, exist_ok=False)
        digest = write_json_exclusive(path, dict(packet))
        write_json_exclusive(
            directory / "packet-receipt.json",
            {
                "schema_version": SCHEMA_VERSION,
                "review_id": review_id,
                "packet_sha256": digest,
                "created_at": utc_now(),
            },
        )
        self.append_event("REVIEW_PACKET_WRITTEN", {"review_id": review_id, "packet_sha256": digest})
        return path, digest
