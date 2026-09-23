"""Codex CLI session backends for the Astra orchestration graph.

This module deliberately talks only to the local ``codex`` executable.  It has
no provider SDK or direct model API dependency.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any, Collection, Iterable, Literal, Mapping, Protocol, Sequence, runtime_checkable
from uuid import uuid4

from .schema import SCHEMA_VERSION
from .transport_schema import codex_transport_schema, decode_codex_transport_output


Role = Literal["astra", "sol", "reviewer", "luna"]
Mode = Literal["new", "resume"]
SchemaInput = Mapping[str, Any] | str | os.PathLike[str]


FIXED_ROLE_SETTINGS: Mapping[Role, Mapping[str, str]] = MappingProxyType({
    "astra": MappingProxyType({
        "title": "Project Manager",
        "model": "gpt-6-astra",
        "reasoning_effort": "high",
    }),
    "sol": MappingProxyType({
        "title": "Implementation Worker",
        "model": "gpt-6-sol",
        "reasoning_effort": "high",
    }),
    "reviewer": MappingProxyType({
        "title": "Independent Reviewer",
        "model": "gpt-6-sol",
        "reasoning_effort": "high",
    }),
    "luna": MappingProxyType({
        "title": "Research and Information-Gathering Specialist",
        "model": "gpt-6-luna",
        "reasoning_effort": "xhigh",
    }),
})


# Older schema-v2 manifests retain their exact recorded role policy on resume.
# This is not a configurable fallback for new runs.
LEGACY_ROLE_SETTINGS: Mapping[Role, Mapping[str, str]] = MappingProxyType({
    "astra": MappingProxyType({
        "title": "Project Manager", "model": "gpt-6-astra", "reasoning_effort": "high",
    }),
    "sol": MappingProxyType({
        "title": "Implementation Worker", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh",
    }),
    "reviewer": MappingProxyType({
        "title": "Independent Reviewer", "model": "gpt-5.6-sol", "reasoning_effort": "ultra",
    }),
    "luna": MappingProxyType({
        "title": "Research and Information-Gathering Specialist",
        "model": "gpt-5.6-luna", "reasoning_effort": "ultra",
    }),
})


def validated_role_settings(
    settings: Mapping[str, Mapping[str, str]],
) -> dict[Role, dict[str, str]]:
    """Accept only an exact current or historical fixed policy."""
    if settings != FIXED_ROLE_SETTINGS and settings != LEGACY_ROLE_SETTINGS:
        raise ValueError("manifest role settings do not match a supported fixed orchestration policy")
    return {role: dict(values) for role, values in settings.items()}  # type: ignore[misc]


@dataclass(frozen=True)
class BackendResult:
    """A parsed, evidenced result from one backend turn."""

    role: Role
    session_id: str
    mode: Mode
    output: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)
    receipt_dir: Path | None = None
    returncode: int = 0
    timed_out: bool = False
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class BackendCall:
    role: Role
    mode: Mode
    prompt: str
    output_schema: dict[str, Any]
    receipt_dir: Path
    requested_session_id: str | None


@runtime_checkable
class Backend(Protocol):
    def run_astra(
        self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
        *, session_id: str | None = None,
    ) -> BackendResult: ...

    def run_sol(
        self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
        *, session_id: str | None = None,
    ) -> BackendResult: ...

    def run_reviewer(
        self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
        *, used_session_ids: Collection[str] = (),
    ) -> BackendResult: ...

    def run_luna(
        self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
        *, used_session_ids: Collection[str] = (),
    ) -> BackendResult: ...


class BackendError(RuntimeError):
    """A backend turn failed after its available evidence was persisted."""

    def __init__(
        self,
        message: str,
        receipt_dir: Path,
        *,
        failure_kind: str = "OTHER",
        codex_error_info: str | None = None,
    ):
        super().__init__(f"{message} (receipts: {receipt_dir})")
        self.receipt_dir = receipt_dir
        self.failure_kind = failure_kind
        self.codex_error_info = codex_error_info


USAGE_LIMIT_FAILURE_KIND = "USAGE_LIMIT_EXCEEDED"


class BackendUsageLimitExceeded(BackendError):
    """Codex reported that the account's available usage quota is exhausted."""

    def __init__(
        self,
        message: str,
        receipt_dir: Path,
        *,
        codex_error_info: str = "usage_limit_exceeded",
    ):
        super().__init__(
            message,
            receipt_dir,
            failure_kind=USAGE_LIMIT_FAILURE_KIND,
            codex_error_info=codex_error_info,
        )


_FILES = (
    "launch.json", "events.jsonl", "stderr.txt", "final.txt", "final.json",
    "process.json", "prompt.txt", "codex-output-schema.json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _prepare_receipts(value: str | os.PathLike[str]) -> Path:
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    collisions = [name for name in _FILES if (path / name).exists()]
    if collisions:
        raise BackendError(f"refusing to overwrite backend receipts: {', '.join(collisions)}", path)
    return path


def _load_schema(value: SchemaInput) -> dict[str, Any]:
    if isinstance(value, Mapping):
        schema = dict(value)
    else:
        try:
            schema = json.loads(Path(value).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"invalid output schema: {value}") from exc
    if not isinstance(schema, dict):
        raise ValueError("output schema must be a JSON object")
    return schema


def _session_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("session_id must be a nonempty explicit identifier")
    if value.startswith("-") or any(character.isspace() for character in value):
        raise ValueError("session_id may not be an option or contain whitespace")
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_events(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            errors.append(f"line {number} is not JSON")
            continue
        if not isinstance(event, dict):
            errors.append(f"line {number} is not an object")
            continue
        events.append(event)
    return events, errors


def _normalized_error_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def _usage_limit_code(value: Any) -> str | None:
    normalized = _normalized_error_code(value)
    if normalized == "usage_limit_exceeded":
        return normalized
    return None


def _usage_limit_from_mapping(value: Mapping[str, Any]) -> str | None:
    """Find an exact usage-limit discriminator inside one terminal error payload."""

    for key, item in value.items():
        normalized_key = _normalized_error_code(key)
        if normalized_key in {
            "codex_error_info", "code", "error_code", "kind", "reason", "type", "message",
        }:
            direct = _usage_limit_code(item)
            if direct:
                return direct
        if normalized_key == "message" and isinstance(item, str):
            try:
                embedded = json.loads(item)
            except ValueError:
                embedded = None
            if isinstance(embedded, Mapping):
                nested = _usage_limit_from_mapping(embedded)
                if nested:
                    return nested
        if isinstance(item, Mapping):
            nested = _usage_limit_from_mapping(item)
            if nested:
                return nested
        elif isinstance(item, list):
            for child in item:
                if isinstance(child, Mapping):
                    nested = _usage_limit_from_mapping(child)
                    if nested:
                        return nested
    return None


_CANONICAL_USAGE_LIMIT_SENTENCE = re.compile(
    r"(?:^|[\s{\[\"'])you(?:'|\u2019)?ve hit your usage limit(?:[.!\s}\]\"']|$)",
    re.IGNORECASE,
)


def _terminal_error_event(event: Mapping[str, Any]) -> bool:
    return _normalized_error_code(event.get("type")) in {"error", "turn_failed"}


def _usage_limit_error(
    events: Iterable[Mapping[str, Any]], stderr: str,
) -> str | None:
    """Classify only authoritative terminal usage-limit signals.

    Ordinary per-turn token usage, model prose, generic HTTP 429/rate limits,
    context-window errors, and session budgets deliberately do not match.
    """

    terminal_events = [event for event in events if _terminal_error_event(event)]
    for event in terminal_events:
        code = _usage_limit_from_mapping(event)
        if code:
            return code
        if _CANONICAL_USAGE_LIMIT_SENTENCE.search(json.dumps(event, ensure_ascii=False)):
            return "usage_limit_exceeded"

    for line in stderr.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except ValueError:
            if _usage_limit_code(stripped):
                return "usage_limit_exceeded"
            if _CANONICAL_USAGE_LIMIT_SENTENCE.search(stripped):
                return "usage_limit_exceeded"
            continue
        if isinstance(value, Mapping) and _terminal_error_event(value):
            code = _usage_limit_from_mapping(value)
            if code:
                return code
            if _CANONICAL_USAGE_LIMIT_SENTENCE.search(
                json.dumps(value, ensure_ascii=False)
            ):
                return "usage_limit_exceeded"
    return None


def _thread_id(events: Iterable[Mapping[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started":
            value = event.get("thread_id", event.get("threadId"))
            if isinstance(value, str) and value:
                return value
    return None


def _usage(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            result = dict(event["usage"])
    return result


def _agent_message(events: Iterable[Mapping[str, Any]]) -> str:
    result = ""
    for event in events:
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
            result = item["text"]
    return result


def _parse_final(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("final Codex message is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("final Codex message must be a JSON object")
    return value


class CodexCliBackend:
    """Run persistent manager/worker sessions and always-fresh specialist sessions."""

    def __init__(
        self, workspace: str | os.PathLike[str], *, codex_executable: str | os.PathLike[str] = "codex",
        timeout_seconds: float = 900.0, cleanup_timeout_seconds: float = 10.0,
        role_settings: Mapping[str, Mapping[str, str]] = FIXED_ROLE_SETTINGS,
    ):
        if timeout_seconds <= 0 or cleanup_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self.workspace = Path(workspace).resolve()
        self.codex_executable = str(codex_executable)
        self.timeout_seconds = float(timeout_seconds)
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)
        self.role_settings = validated_role_settings(role_settings)
        self.models = {
            role: _session_id(settings["model"])
            for role, settings in self.role_settings.items()
        }
        self.efforts = {
            role: _session_id(settings["reasoning_effort"])
            for role, settings in self.role_settings.items()
        }
        self._fresh_session_ids: set[str] = set()
        self._fresh_session_lock = threading.Lock()

    def run_astra(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                  *, session_id: str | None = None) -> BackendResult:
        return self._run("astra", prompt, output_schema, receipt_dir, session_id=session_id)

    def run_sol(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                *, session_id: str | None = None) -> BackendResult:
        return self._run("sol", prompt, output_schema, receipt_dir, session_id=session_id)

    def run_reviewer(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                     *, used_session_ids: Collection[str] = ()) -> BackendResult:
        return self._run_fresh(
            "reviewer", prompt, output_schema, receipt_dir, used_session_ids=used_session_ids,
        )

    def run_luna(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                 *, used_session_ids: Collection[str] = ()) -> BackendResult:
        return self._run_fresh(
            "luna", prompt, output_schema, receipt_dir, used_session_ids=used_session_ids,
        )

    def _run_fresh(
        self, role: Literal["reviewer", "luna"], prompt: str, output_schema: SchemaInput,
        receipt_dir: str | os.PathLike[str], *, used_session_ids: Collection[str],
    ) -> BackendResult:
        result = self._run(role, prompt, output_schema, receipt_dir, fresh=True)
        forbidden = set(used_session_ids)
        with self._fresh_session_lock:
            if result.session_id in forbidden or result.session_id in self._fresh_session_ids:
                raise BackendError(
                    f"{role} session was reused: {result.session_id}", Path(receipt_dir).resolve()
                )
            self._fresh_session_ids.add(result.session_id)
        return result

    def _command(self, role: Role, schema: Path, final: Path, session_id: str | None) -> tuple[list[str], Mode]:
        common = ["--json", "--model", self.models[role], "-c",
                  f'model_reasoning_effort="{self.efforts[role]}"',
                  "--output-schema", str(schema), "--output-last-message", str(final)]
        if role in {"reviewer", "luna"}:
            executable = [self.codex_executable]
            if role == "luna":
                executable.append("--search")
            return ([*executable, "exec", "--ephemeral", "--ignore-user-config",
                     "--ignore-rules", "--skip-git-repo-check", "--sandbox", "read-only",
                     "--cd", str(self.workspace),
                     *common, "-"], "new")
        sandbox = "read-only" if role == "astra" else "workspace-write"
        if session_id is None:
            return ([self.codex_executable, "exec", "--sandbox", sandbox,
                     "--skip-git-repo-check", *common, "-"], "new")
        explicit = _session_id(session_id)
        # `exec resume` has no --sandbox flag; force the equivalent config value.
        return ([self.codex_executable, "exec", "resume", "-c", f'sandbox_mode="{sandbox}"',
                 "--skip-git-repo-check", *common, explicit, "-"], "resume")

    def _cleanup(self, process: subprocess.Popen[str]) -> dict[str, Any]:
        evidence: dict[str, Any] = {"attempted": True}
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                    text=True, timeout=self.cleanup_timeout_seconds, check=False,
                )
                evidence["taskkill_returncode"] = completed.returncode
                evidence["taskkill_stdout"] = completed.stdout
                evidence["taskkill_stderr"] = completed.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                evidence["taskkill_error"] = repr(exc)
        if process.poll() is None:
            try:
                process.kill()
                evidence["direct_kill"] = True
            except OSError as exc:
                evidence["direct_kill_error"] = repr(exc)
        try:
            process.wait(timeout=self.cleanup_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            evidence["wait_error"] = repr(exc)
        return evidence

    def _run(self, role: Role, prompt: str, output_schema: SchemaInput,
             receipt_value: str | os.PathLike[str], *, session_id: str | None = None,
             fresh: bool = False) -> BackendResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be nonempty")
        receipt = _prepare_receipts(receipt_value)
        source_schema = _load_schema(output_schema)
        schema_value = codex_transport_schema(source_schema)
        schema_path = receipt / "codex-output-schema.json"
        final_path = receipt / "final.txt"
        _write_json(schema_path, schema_value)
        _atomic_write(receipt / "prompt.txt", prompt.encode("utf-8"))
        command, mode = self._command(role, schema_path, final_path, session_id)
        role_requires_fresh_session = role in {"reviewer", "luna"}
        if fresh != role_requires_fresh_session:
            raise AssertionError(f"{role} fresh-session policy mismatch")
        if fresh and mode != "new":
            raise AssertionError(f"{role} must always use a new session")
        if "--last" in command or "fork" in command or (fresh and "resume" in command):
            raise AssertionError("implicit resume and fork are forbidden")
        isolated: tempfile.TemporaryDirectory[str] | None = None
        cwd = self.workspace
        if fresh:
            isolated = tempfile.TemporaryDirectory(prefix=f"astra-fresh-{role}-")
            cwd = Path(isolated.name).resolve()
        started = _now()
        launch = {
            "schema_version": SCHEMA_VERSION, "role": role, "mode": mode, "started_at": started,
            "command": command, "cwd": str(cwd), "workspace": str(self.workspace),
            "requested_session_id": session_id, "prompt_transport": "stdin",
            "timeout_seconds": self.timeout_seconds,
            "requested_model": self.models[role],
            "requested_reasoning_effort": self.efforts[role],
            "schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            "fresh_isolated_cwd": fresh,
            "codex_working_root": str(self.workspace),
            "project_access": "read-only" if fresh else (
                "read-only" if role == "astra" else "workspace-write"
            ),
            "reviewer_isolated_cwd": role == "reviewer",
            "luna_isolated_cwd": role == "luna",
            "live_web_search": role == "luna",
        }
        _write_json(receipt / "launch.json", launch)
        process: subprocess.Popen[str] | None = None
        stdout = stderr = ""
        timed_out = False
        cleanup: dict[str, Any] = {}
        began = time.monotonic()
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                creationflags=flags,
            )
            try:
                stdout, stderr = process.communicate(input=prompt, timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                stdout, stderr = _text(exc.output), _text(exc.stderr)
                cleanup = self._cleanup(process)
                try:
                    trailing_out, trailing_err = process.communicate(timeout=self.cleanup_timeout_seconds)
                    stdout, stderr = _text(trailing_out) or stdout, _text(trailing_err) or stderr
                except (OSError, subprocess.TimeoutExpired) as cleanup_exc:
                    cleanup["communicate_error"] = repr(cleanup_exc)
        except OSError as exc:
            stderr = repr(exc)
            cleanup["launch_error"] = repr(exc)
        finally:
            if isolated is not None:
                isolated.cleanup()
        stdout, stderr = _text(stdout), _text(stderr)
        _atomic_write(receipt / "events.jsonl", stdout.encode("utf-8"))
        _atomic_write(receipt / "stderr.txt", stderr.encode("utf-8"))
        events, parse_errors = _parse_events(stdout)
        observed_id = _thread_id(events)
        codex_error_info = _usage_limit_error(events, stderr)
        returncode = process.returncode if process is not None and process.returncode is not None else -1
        process_receipt = {
            "schema_version": SCHEMA_VERSION, "role": role, "mode": mode, "started_at": started,
            "finished_at": _now(), "elapsed_seconds": time.monotonic() - began,
            "pid": process.pid if process is not None else None, "returncode": returncode,
            "timed_out": timed_out, "session_id": observed_id, "usage": _usage(events),
            "event_parse_errors": parse_errors, "cleanup": cleanup,
            "backend_failure_kind": (
                USAGE_LIMIT_FAILURE_KIND if codex_error_info else None
            ),
            "codex_error_info": codex_error_info,
        }
        _write_json(receipt / "process.json", process_receipt)
        if codex_error_info:
            raise BackendUsageLimitExceeded(
                f"{role} Codex usage limit is exhausted",
                receipt,
                codex_error_info=codex_error_info,
            )
        if timed_out:
            raise BackendError(f"{role} Codex turn timed out", receipt)
        if process is None:
            raise BackendError(f"could not launch Codex for {role}", receipt)
        if returncode != 0:
            raise BackendError(f"{role} Codex turn exited with {returncode}", receipt)
        if parse_errors:
            raise BackendError("Codex JSONL contained malformed events", receipt)
        if observed_id is None:
            raise BackendError("Codex did not emit thread.started", receipt)
        if mode == "resume" and observed_id != session_id:
            raise BackendError("resumed Codex session id did not match the requested id", receipt)
        raw_final = ""
        if final_path.exists():
            raw_final = final_path.read_text(encoding="utf-8")
        if not raw_final.strip():
            raw_final = _agent_message(events)
            _atomic_write(final_path, raw_final.encode("utf-8"))
        try:
            transport_output = _parse_final(raw_final)
            output = decode_codex_transport_output(transport_output, source_schema)
        except ValueError as exc:
            raise BackendError(str(exc), receipt) from exc
        _write_json(receipt / "final.json", output)
        return BackendResult(
            role=role, session_id=observed_id, mode=mode, output=output,
            usage=_usage(events), receipt_dir=receipt, returncode=returncode,
            timed_out=False, command=tuple(command),
        )


ScriptValue = BackendResult | Mapping[str, Any] | BaseException


class ScriptedBackend:
    """Injectable deterministic backend for dry runs and unit tests."""

    def __init__(self, script: Mapping[str, Sequence[ScriptValue]]):
        self._script = {role: deque(values) for role, values in script.items()}
        self.calls: list[BackendCall] = []
        self._fresh_session_ids: set[str] = set()

    def run_astra(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                  *, session_id: str | None = None) -> BackendResult:
        return self._run("astra", prompt, output_schema, receipt_dir, session_id)

    def run_sol(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                *, session_id: str | None = None) -> BackendResult:
        return self._run("sol", prompt, output_schema, receipt_dir, session_id)

    def run_reviewer(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                     *, used_session_ids: Collection[str] = ()) -> BackendResult:
        return self._run_fresh(
            "reviewer", prompt, output_schema, receipt_dir, used_session_ids=used_session_ids,
        )

    def run_luna(self, prompt: str, output_schema: SchemaInput, receipt_dir: str | os.PathLike[str],
                 *, used_session_ids: Collection[str] = ()) -> BackendResult:
        return self._run_fresh(
            "luna", prompt, output_schema, receipt_dir, used_session_ids=used_session_ids,
        )

    def _run_fresh(
        self, role: Literal["reviewer", "luna"], prompt: str, output_schema: SchemaInput,
        receipt_dir: str | os.PathLike[str], *, used_session_ids: Collection[str],
    ) -> BackendResult:
        result = self._run(role, prompt, output_schema, receipt_dir, None)
        if result.session_id in set(used_session_ids) or result.session_id in self._fresh_session_ids:
            raise BackendError(f"{role} session was reused: {result.session_id}", Path(receipt_dir).resolve())
        self._fresh_session_ids.add(result.session_id)
        return result

    def _run(self, role: Role, prompt: str, output_schema: SchemaInput,
             receipt_value: str | os.PathLike[str], session_id: str | None) -> BackendResult:
        receipt = _prepare_receipts(receipt_value)
        schema = _load_schema(output_schema)
        mode: Mode = "resume" if session_id is not None else "new"
        if session_id is not None:
            _session_id(session_id)
        self.calls.append(BackendCall(role, mode, prompt, schema, receipt, session_id))
        queue = self._script.get(role)
        if not queue:
            raise BackendError(f"no scripted {role} result remains", receipt)
        value = queue.popleft()
        if isinstance(value, BaseException):
            raise value
        generated_id = session_id or f"scripted-{role}-{uuid4()}"
        if isinstance(value, BackendResult):
            result = replace(value, role=role, mode=mode, receipt_dir=receipt)
        else:
            result = BackendResult(role, generated_id, mode, dict(value), receipt_dir=receipt)
        _write_json(receipt / "codex-output-schema.json", schema)
        _atomic_write(receipt / "prompt.txt", prompt.encode("utf-8"))
        event_rows = [
            {"type": "thread.started", "thread_id": result.session_id},
            {"type": "turn.completed", "usage": result.usage},
        ]
        _atomic_write(receipt / "events.jsonl", "".join(json.dumps(row) + "\n" for row in event_rows).encode("utf-8"))
        _atomic_write(receipt / "stderr.txt", b"")
        _atomic_write(receipt / "final.txt", json.dumps(result.output).encode("utf-8"))
        _write_json(receipt / "final.json", result.output)
        _write_json(receipt / "launch.json", {"schema_version": SCHEMA_VERSION, "role": role, "mode": mode,
                    "scripted": True, "requested_session_id": session_id})
        _write_json(receipt / "process.json", {"schema_version": SCHEMA_VERSION, "role": role, "mode": mode,
                    "scripted": True, "session_id": result.session_id, "returncode": result.returncode,
                    "timed_out": result.timed_out, "usage": result.usage})
        return result


# Compatibility-friendly descriptive alias.
SessionBackend = Backend
