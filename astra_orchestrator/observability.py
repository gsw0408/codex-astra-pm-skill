"""Privacy-limited LangSmith spans for the Codex-backed control plane.

Only allowlisted identifiers and scalar outcomes leave this process. The
checkpoint, prompts, model responses, receipts, and environment never do.
Tracing is best-effort and cannot decide or alter a graph transition.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import math
import os
import time
from typing import Any, Iterator, Mapping

try:
    from langsmith import Client, tracing_context
    from langsmith.run_trees import RunTree
except ImportError:  # The graph remains usable in a partially installed environment.
    Client = None  # type: ignore[assignment,misc]
    RunTree = None  # type: ignore[assignment,misc]

    @contextmanager
    def tracing_context(*, enabled: bool) -> Iterator[None]:
        yield

from .codex_cli import FIXED_ROLE_SETTINGS, validated_role_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _usage_metadata(usage: object) -> dict[str, int | float]:
    """Accept only explicit, nonnegative Codex usage with unambiguous units."""

    if not isinstance(usage, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    for key in ("total_cost_usd", "cost_usd"):
        value = usage.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            result["cost_usd"] = value
            break
    return result


class LangSmithObserver:
    """Explicit RunTree parent/children; no automatic LangGraph state logging."""

    def __init__(
        self, *, enabled: bool | None = None, client: Any = None,
        role_settings: Mapping[str, Mapping[str, str]] = FIXED_ROLE_SETTINGS,
    ):
        self.role_settings = validated_role_settings(role_settings)
        if enabled is None:
            enabled = os.getenv("LANGSMITH_TRACING", "").lower() in {"1", "true", "yes"}
            enabled = enabled and bool(os.getenv("LANGSMITH_API_KEY"))
        self.enabled = bool(enabled) and RunTree is not None and Client is not None
        self._client = client
        self._root: RunTree | None = None
        self._role: RunTree | None = None
        self._role_metadata: dict[str, Any] = {}
        self._root_started = 0.0
        self._role_started = 0.0
        self._invocation_outcome: dict[str, Any] = {}
        if self.enabled and self._client is None:
            try:
                self._client = Client()
            except Exception:
                self.enabled = False

    def _disable(self) -> None:
        # Telemetry failures are deliberately invisible to graph routing.
        self.enabled = False
        self._root = None
        self._role = None
        self._role_metadata = {}

    @contextmanager
    def invocation(self, run_id: str, stage: str | None, kind: str) -> Iterator[None]:
        root: RunTree | None = None
        if self.enabled:
            started_at = _now()
            self._root_started = time.monotonic()
            try:
                root = RunTree(
                    name="Astra orchestration",
                    run_type="chain",
                    inputs={"run_id": run_id, "invocation": kind},
                    extra={"metadata": {
                        "run_id": run_id,
                        "invocation": kind,
                        "stage": stage,
                        "started_at": _iso(started_at),
                    }},
                    start_time=started_at,
                    project_name=os.getenv("LANGSMITH_PROJECT") or "astra-orchestrator",
                    ls_client=self._client,
                )
                root.post()
                self._root = root
            except Exception:
                self._disable()
                root = None
        failure: BaseException | None = None
        try:
            yield
        except BaseException as error:
            failure = error
            raise
        finally:
            if root is not None and self.enabled:
                ended_at = _now()
                metadata: dict[str, Any] = {
                    "ended_at": _iso(ended_at),
                    "latency_ms": round((time.monotonic() - self._root_started) * 1000, 3),
                }
                if failure is not None:
                    metadata.update(status="ERROR", error_type=type(failure).__name__)
                else:
                    metadata["status"] = self._invocation_outcome.get("status", "FINISHED")
                    metadata.update(self._invocation_outcome)
                try:
                    root.end(
                        outputs={key: metadata[key] for key in ("status", "routing_decision", "stage") if key in metadata},
                        error=metadata.get("error_type"),
                        end_time=ended_at,
                        metadata=metadata,
                    )
                    root.patch()
                    if hasattr(self._client, "flush"):
                        self._client.flush(timeout=5)
                except Exception:
                    self._disable()
                finally:
                    self._root = None
                    self._invocation_outcome = {}

    def record_outcome(self, state: Mapping[str, Any]) -> None:
        if self._root is None or not self.enabled:
            return
        self._invocation_outcome = {
            "status": state.get("status"),
            "routing_decision": state.get("route"),
            "stage": state.get("current_stage"),
        }

    @contextmanager
    def role(self, role: str, state: Mapping[str, Any]) -> Iterator[None]:
        child: RunTree | None = None
        if self.enabled and self._root is not None:
            started_at = _now()
            self._role_started = time.monotonic()
            settings = self.role_settings[role]
            self._role_metadata = {
                "role": role,
                "model": settings["model"],
                "reasoning_effort": settings["reasoning_effort"],
                "stage": state.get("current_stage"),
                "started_at": _iso(started_at),
            }
            task = state.get(f"pending_{role}_task") if role in {"sol", "luna"} else None
            if isinstance(task, Mapping) and isinstance(task.get("recovery_strategy"), Mapping):
                attempt = task["recovery_strategy"].get("attempt")
                if isinstance(attempt, int) and attempt > 0:
                    self._role_metadata["recovery_attempt"] = attempt
            try:
                child = self._root.create_child(
                    name=role.capitalize(),
                    run_type="tool",
                    inputs={"run_id": state.get("run_id"), "stage": state.get("current_stage")},
                    start_time=started_at,
                    extra={"metadata": dict(self._role_metadata)},
                )
                child.post()
                self._role = child
            except Exception:
                self._disable()
                child = None
        try:
            yield
        except BaseException as error:
            if child is not None and self.enabled:
                self._finish_role(child, {"status": "ERROR", "error_type": type(error).__name__})
            raise

    def finish_role(self, role: str, updates: Mapping[str, Any]) -> None:
        child = self._role
        if child is None or not self.enabled:
            return
        metadata: dict[str, Any] = {}
        if role == "astra":
            route = updates.get("route")
            if route in {"SOL", "LUNA", "REVIEW", "USER", "END"}:
                metadata["routing_decision"] = route
                task = updates.get(f"pending_{route.lower()}_task")
                if isinstance(task, Mapping) and isinstance(task.get("recovery_strategy"), Mapping):
                    attempt = task["recovery_strategy"].get("attempt")
                    if isinstance(attempt, int) and attempt > 0:
                        metadata["recovery_attempt"] = attempt
        else:
            metadata["routing_decision"] = (
                "END" if updates.get("route") == "END"
                or updates.get("status") in {"COMPLETED", "ABORTED", "FAILED"}
                else "ASTRA"
            )
        result_key = {"sol": "sol_result", "luna": "luna_result", "reviewer": "reviewer_result"}.get(role)
        result = updates.get(result_key) if result_key else None
        if isinstance(result, Mapping):
            if role == "reviewer" and result.get("verdict") in {"PASS", "FAIL", "NEEDS_EVIDENCE"}:
                metadata["reviewer_verdict"] = result["verdict"]
            elif result.get("status") in {
                "DONE", "BLOCKED", "NEEDS_USER", "MILESTONE_COMPLETE",
                "INSUFFICIENT_EVIDENCE", "FAILED",
            }:
                metadata["result_status"] = result["status"]
        if updates.get("status") == "FAILED" or updates.get("last_error") or metadata.get("result_status") == "FAILED":
            metadata["status"] = "ERROR"
            metadata.setdefault("error_type", self._role_metadata.get("error_type", "ROLE_CALL_FAILED"))
        else:
            metadata["status"] = "OK"
        self._finish_role(child, metadata)

    def _finish_role(self, child: RunTree, metadata: Mapping[str, Any]) -> None:
        ended_at = _now()
        values = {
            **self._role_metadata,
            **metadata,
            "ended_at": _iso(ended_at),
            "latency_ms": round((time.monotonic() - self._role_started) * 1000, 3),
        }
        try:
            child.end(
                outputs={key: values[key] for key in ("status", "routing_decision", "reviewer_verdict") if key in values},
                error=values.get("error_type") if values.get("status") == "ERROR" else None,
                end_time=ended_at,
                metadata=values,
            )
            child.patch()
        except Exception:
            self._disable()
        finally:
            self._role = None
            self._role_metadata = {}

    def record_call(self, call_id: str, receipt: Mapping[str, Any]) -> None:
        if self._role is None or not self.enabled:
            return
        self._role_metadata["call_id"] = call_id
        session_id = receipt.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._role_metadata["session_id"] = session_id
        mode = receipt.get("mode")
        if mode in {"new", "resume"}:
            self._role_metadata["session_mode"] = mode
        self._role_metadata.update(_usage_metadata(receipt.get("usage")))

    def record_error(self, call_id: str, error_type: str, session_id: str | None) -> None:
        if self._role is None or not self.enabled:
            return
        self._role_metadata.update(call_id=call_id, error_type=error_type)
        if session_id:
            self._role_metadata["session_id"] = session_id
