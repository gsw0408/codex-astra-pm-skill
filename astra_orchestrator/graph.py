"""LangGraph control plane for Astra, Sol, fresh Luna, and fresh Reviewer."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .audit import (
    AmbiguousCallError,
    AuditLog,
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    write_json_exclusive,
)
from .codex_cli import (
    Backend,
    BackendResult,
    BackendUsageLimitExceeded,
    CodexCliBackend,
    FIXED_ROLE_SETTINGS,
    USAGE_LIMIT_FAILURE_KIND,
)
from .observability import LangSmithObserver, tracing_context
from .prompts import (
    build_astra_prompt,
    build_luna_prompt,
    build_reviewer_prompt,
    build_sol_prompt,
)
from .schema import (
    SCHEMA_VERSION,
    ValidationError,
    WorkflowState,
    schema_path,
    validate_astra_decision,
    validate_luna_result,
    validate_project_spec,
    validate_reviewer_result,
    validate_sol_result,
    validate_user_response,
)
from .shared import MirroredSqliteSaver, SharedRunStore, SharedStateError


RuntimeState = WorkflowState
RECOVERY_LIMIT = 5
USAGE_LIMIT_STOP_CODE = "CODEX_USAGE_LIMIT_EXHAUSTED"


class RoleCallFailure(RuntimeError):
    def __init__(
        self,
        role: str,
        call_id: str,
        message: str,
        *,
        ambiguous: bool,
        session_id: str | None = None,
    ):
        super().__init__(message)
        self.role = role
        self.call_id = call_id
        self.ambiguous = ambiguous
        self.session_id = session_id


class UsageLimitStop(RuntimeError):
    """A verified Codex account-usage limit requires terminal graph shutdown."""

    def __init__(self, role: str, call_id: str, message: str, session_id: str | None):
        super().__init__(message)
        self.role = role
        self.call_id = call_id
        self.session_id = session_id


def _stage(plan: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in plan["stages"]:
        if stage["id"] == stage_id:
            return dict(stage)
    raise ValidationError(f"unknown active-plan stage: {stage_id}")


def _next_stage(plan: Mapping[str, Any], stage_id: str) -> str | None:
    identifiers = [stage["id"] for stage in plan["stages"]]
    try:
        index = identifiers.index(stage_id)
    except ValueError as error:
        raise ValidationError(f"unknown active-plan stage: {stage_id}") from error
    return identifiers[index + 1] if index + 1 < len(identifiers) else None


def _initial_active_plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": "initial",
        "current_stage": spec["stages"][0]["id"],
        "project_context": spec["project_context"],
        "target_stage": spec["target_stage"],
        "stages": deepcopy(spec["stages"]),
        "experiment_plan": list(spec.get("experiment_plan", [])),
        "datasets": list(spec.get("datasets", [])),
        "evaluation_criteria": list(spec.get("evaluation_criteria", [])),
        "approaches": list(spec.get("approaches", [])),
    }


def initial_state(spec: Mapping[str, Any], run_id: str) -> RuntimeState:
    normalized = validate_project_spec(spec)
    active_plan = _initial_active_plan(normalized)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project_spec": normalized,
        "active_plan": active_plan,
        "plan_change_history": [],
        "execution_limits": dict(normalized["limits"]),
        "route": "SOL",
        "status": "RUNNING",
        "current_stage": active_plan["current_stage"],
        "completed_stages": [],
        "target_stage_completed": False,
        "step_count": 0,
        "call_sequence": 0,
        "model_error_count": 0,
        "retry_counts": {
            "sol_attempts_by_stage": {},
            "sol_attempts_by_task": {},
            "review_attempts_by_stage": {},
        },
        "review_session_ids": [],
        "luna_session_ids": [],
        "recovery_issue_id": None,
        "recovery_attempts": [],
        "transition_log": [],
        "correction_required": False,
        "correction_ready": False,
        "must_pause_user": False,
        "paused": False,
    }


def _schema(name: str) -> dict[str, Any]:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def _call_receipt(result: BackendResult, role: str, mode: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "session_id": result.session_id,
        "mode": mode,
        "usage": dict(result.usage),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "command": list(result.command),
        "backend_receipt_dir": str(result.receipt_dir) if result.receipt_dir else None,
        "finished_at": utc_now(),
    }


def _transition(
    audit: AuditLog,
    state: Mapping[str, Any],
    source: str,
    destination: str,
    reason: str,
) -> dict[str, Any]:
    number = int(state.get("step_count", 0)) + 1
    record = {
        "transition_id": f"{state['run_id']}-{number:04d}-{source.lower()}-{destination.lower()}",
        "sequence": number,
        "source": source,
        "destination": destination,
        "reason": reason,
        "stage": state.get("current_stage"),
        "plan_revision_id": state.get("active_plan", {}).get("revision_id"),
    }
    audit.append_transition(record)
    return record


def _safe_issue_id(prefix: str, value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{prefix}-{value}").strip("-")
    return (normalized or "automatic-recovery")[:128]


def _active_attempts(state: Mapping[str, Any], issue_id: str | None = None) -> list[dict[str, Any]]:
    selected = issue_id if issue_id is not None else state.get("recovery_issue_id")
    if not selected:
        return []
    return [
        dict(attempt)
        for attempt in state.get("recovery_attempts", [])
        if attempt.get("issue_id") == selected
    ]


def _review_state_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    task = state.get("last_sol_task") or {}
    result = state.get("sol_result") or {}
    return {
        "active_plan": deepcopy(state["active_plan"]),
        "current_stage": state["current_stage"],
        "completed_stages": list(state.get("completed_stages", [])),
        "target_stage": state["active_plan"]["target_stage"],
        "target_stage_completed": bool(state.get("target_stage_completed")),
        "execution_limits": deepcopy(state["execution_limits"]),
        "retry_counts": deepcopy(state["retry_counts"]),
        "correction_required": bool(state.get("correction_required")),
        "correction_ready": bool(state.get("correction_ready")),
        "last_sol_task_id": task.get("task_id", ""),
        "last_sol_status": result.get("status", ""),
    }


def _write_review_result(
    audit: AuditLog,
    review_id: str,
    result: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    directory = audit.review_dir(review_id)
    directory.mkdir(parents=True, exist_ok=True)
    result_path = directory / "result.json"
    expected = sha256_bytes(canonical_json(dict(result)))
    if result_path.exists():
        if sha256_file(result_path) != expected:
            raise AmbiguousCallError(f"review {review_id} has conflicting result evidence")
    else:
        write_json_exclusive(result_path, dict(result))
    review_receipt = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "result_sha256": expected,
        **dict(receipt),
    }
    receipt_path = directory / "receipt.json"
    receipt_expected = sha256_bytes(canonical_json(review_receipt))
    if receipt_path.exists():
        if sha256_file(receipt_path) != receipt_expected:
            raise AmbiguousCallError(f"review {review_id} has conflicting receipt evidence")
    else:
        write_json_exclusive(receipt_path, review_receipt)


def _compile_graph(
    astra_node: Callable[[RuntimeState], dict[str, Any]],
    sol_node: Callable[[RuntimeState], dict[str, Any]],
    luna_node: Callable[[RuntimeState], dict[str, Any]],
    review_node: Callable[[RuntimeState], dict[str, Any]],
    user_node: Callable[[RuntimeState], dict[str, Any]],
    *,
    checkpointer: Any = None,
):
    """Compile the one authoritative route topology with supplied role nodes."""

    builder = StateGraph(RuntimeState)
    builder.add_node("astra", astra_node)
    builder.add_node("sol", sol_node)
    builder.add_node("luna", luna_node)
    builder.add_node("review", review_node)
    builder.add_node("user", user_node)
    builder.add_edge(START, "astra")
    builder.add_conditional_edges(
        "astra",
        lambda state: state["route"],
        {"SOL": "sol", "LUNA": "luna", "REVIEW": "review", "USER": "user", "END": END},
    )
    post_role_route = lambda state: (
        "END"
        if state.get("route") == "END"
        or state.get("status") in {"COMPLETED", "ABORTED", "FAILED"}
        else "ASTRA"
    )
    for node in ("sol", "luna", "review"):
        builder.add_conditional_edges(
            node,
            post_role_route,
            {"ASTRA": "astra", "END": END},
        )
    builder.add_conditional_edges(
        "user",
        lambda state: "END" if state.get("status") == "ABORTED" else "ASTRA",
        {"ASTRA": "astra", "END": END},
    )
    return builder.compile(checkpointer=checkpointer)


class OrchestrationRuntime:
    """One single-controller, restart-safe orchestration thread."""

    def __init__(
        self, run_dir: Path, backend: Backend, *, observer: LangSmithObserver | None = None,
        shared_store: SharedRunStore | None = None,
        project_root: Path | None = None,
    ):
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        self.audit = AuditLog.open(run_dir)
        self.backend = backend
        self.shared_store = shared_store
        self.observer = observer if observer is not None else LangSmithObserver(
            role_settings=getattr(backend, "role_settings", FIXED_ROLE_SETTINGS)
        )
        manifest = read_json(self.audit.run_dir / "manifest.json")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported orchestration manifest schema_version; expected {SCHEMA_VERSION}"
            )
        effective_root = (project_root or Path(str(manifest["project_root"]))).resolve()
        self.project_root = effective_root
        roots_path = self.audit.run_dir / "host-roots.json"
        self.known_roots = read_json(roots_path)["roots"] if roots_path.is_file() else [str(effective_root)]
        if self.audit.run_dir == effective_root or self.audit.run_dir.is_relative_to(effective_root):
            raise ValueError(
                "run_dir must be outside project_root so Sol cannot modify controller evidence"
            )
        self.checkpoint_path = self.audit.run_dir / "checkpoint.sqlite"
        self.connection = sqlite3.connect(self.checkpoint_path, check_same_thread=False)
        if shared_store is not None:
            shared_store.sqlite_connection = self.connection
            self.checkpointer = MirroredSqliteSaver(self.connection, shared_store)
        else:
            self.checkpointer = SqliteSaver(self.connection)
        self.graph = self._build_graph()
        self.run_id = str(manifest["run_id"])
        self.config = {"configurable": {"thread_id": self.run_id}}

    def _build_graph(self):
        return _compile_graph(
            lambda state: self._traced_node("astra", state, self._astra_node),
            lambda state: self._traced_node("sol", state, self._sol_node),
            lambda state: self._traced_node("luna", state, self._luna_node),
            lambda state: self._traced_node("reviewer", state, self._review_node),
            self._user_node,
            checkpointer=self.checkpointer,
        )

    def _traced_node(
        self, role: str, state: RuntimeState,
        node: Callable[[RuntimeState], dict[str, Any]],
    ) -> dict[str, Any]:
        with self.observer.role(role, state):
            updates = node(self._local_state(state))
            self.observer.finish_role(role, updates)
            return updates

    def _local_state(self, state: RuntimeState) -> RuntimeState:
        """Project paths are host-local; scientific goals and checkpoint IDs are not."""
        if self.shared_store is None:
            return state

        def rebase(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: rebase(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rebase(item) for item in value]
            if not isinstance(value, str):
                return value
            for root in sorted(self.known_roots, key=len, reverse=True):
                old = root.rstrip("/\\") or root
                comparison = value.lower() if re.match(r"^[A-Za-z]:[\\/]", old) else value
                prefix = old.lower() if re.match(r"^[A-Za-z]:[\\/]", old) else old
                if comparison == prefix:
                    return str(self.project_root)
                if comparison.startswith(prefix + "/") or comparison.startswith(prefix + "\\"):
                    suffix = value[len(old):].lstrip("/\\")
                    return str(self.project_root.joinpath(*re.split(r"[/\\]+", suffix)))
            return value

        return rebase(dict(state))

    def _observed_call_session_id(self, call_id: str, role: str) -> str | None:
        path = self.audit.call_dir(call_id) / "process.json"
        if not path.is_file():
            return None
        try:
            process = read_json(path)
        except (OSError, ValueError):
            return None
        session_id = process.get("session_id")
        if process.get("role") != role or not isinstance(session_id, str) or not session_id:
            return None
        return session_id

    def _persisted_usage_limit_failure(self, call_id: str, role: str) -> bool:
        """Recognize a pre-checkpoint quota stop without re-executing its role call."""

        directory = self.audit.call_dir(call_id)
        status_path = directory / "status.json"
        if not status_path.is_file():
            return False
        try:
            status = read_json(status_path)
        except (OSError, ValueError):
            return False
        if status.get("role") != role or status.get("status") == "SUCCEEDED":
            return False
        error_path = directory / "error.json"
        if error_path.is_file():
            try:
                error = read_json(error_path)
            except (OSError, ValueError):
                error = {}
            if (
                error.get("failure_kind") == USAGE_LIMIT_FAILURE_KIND
                and status.get("error_sha256") == sha256_file(error_path)
            ):
                return True
        process_path = directory / "process.json"
        if process_path.is_file():
            try:
                process = read_json(process_path)
            except (OSError, ValueError):
                process = {}
            if (
                process.get("role") == role
                and process.get("backend_failure_kind") == USAGE_LIMIT_FAILURE_KIND
            ):
                if status.get("status") in {"PREPARED", "RUNNING"} and not error_path.exists():
                    self.audit.fail_call(
                        call_id,
                        {
                            "error_type": "RecoveredUsageLimitStop",
                            "failure_kind": USAGE_LIMIT_FAILURE_KIND,
                            "codex_error_info": process.get("codex_error_info"),
                            "message": "Recovered a persisted Codex usage-limit failure.",
                            "terminal_stop_code": USAGE_LIMIT_STOP_CODE,
                        },
                        ambiguous=role == "sol",
                    )
                return True
        return False

    def _used_fresh_session_ids(self, state: Mapping[str, Any]) -> set[str]:
        used = set(state.get("review_session_ids", []))
        used.update(state.get("luna_session_ids", []))
        # Include every durable role receipt, not just successful manager/
        # worker state.  A failed or superseded Astra/Sol session must never
        # later be recycled as a supposedly fresh specialist session.
        used.update(self.audit.observed_session_ids())
        used.update(
            value
            for value in (state.get("astra_session_id"), state.get("sol_session_id"))
            if isinstance(value, str) and value
        )
        return used

    def _fresh_session_ids(self, state: Mapping[str, Any]) -> set[str]:
        """Every specialist session known in state or durable receipts."""
        used = set(state.get("review_session_ids", []))
        used.update(state.get("luna_session_ids", []))
        used.update(self.audit.observed_fresh_session_ids())
        return used

    @staticmethod
    def _state_session_ids(state: Mapping[str, Any]) -> set[str]:
        used = set(state.get("review_session_ids", []))
        used.update(state.get("luna_session_ids", []))
        used.update(
            value
            for value in (state.get("astra_session_id"), state.get("sol_session_id"))
            if isinstance(value, str) and value
        )
        return used

    def _invoke_role(
        self,
        state: Mapping[str, Any],
        role: str,
        prompt: str,
        schema_name: str,
        validator: Callable[[object], dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        sequence = int(state.get("call_sequence", 0)) + 1
        call_id = f"{sequence:04d}-{role}"
        if self._persisted_usage_limit_failure(call_id, role):
            self.observer.record_error(
                call_id, "BackendUsageLimitExceeded",
                self._observed_call_session_id(call_id, role),
            )
            raise UsageLimitStop(
                role,
                call_id,
                f"{role} Codex usage limit was exhausted in the persisted call evidence",
                self._observed_call_session_id(call_id, role),
            )
        try:
            recovered = self.audit.recover_call(call_id)
        except AmbiguousCallError as error:
            self.observer.record_error(
                call_id, type(error).__name__, self._observed_call_session_id(call_id, role)
            )
            raise RoleCallFailure(
                role,
                call_id,
                str(error),
                ambiguous=role == "sol",
                session_id=self._observed_call_session_id(call_id, role),
            ) from error
        if recovered is not None:
            self.observer.record_call(call_id, recovered.receipt)
            return validator(recovered.result), recovered.receipt, call_id
        output_schema = _schema(schema_name)
        requested_session = state.get(f"{role}_session_id") if role in {"astra", "sol"} else None
        request = {
            "schema_version": SCHEMA_VERSION,
            "role": role,
            "call_id": call_id,
            "prompt": prompt,
            "requested_session_id": requested_session,
            "state_step": state.get("step_count", 0),
        }
        if self.shared_store is not None and requested_session and isinstance(self.backend, CodexCliBackend):
            self.shared_store.ensure_session(requested_session)
        directory = self.audit.prepare_call(call_id, role, request, output_schema)
        self.audit.mark_call_running(call_id)
        if self.shared_store is not None:
            self.shared_store.save()
        try:
            if role == "astra":
                backend_result = self.backend.run_astra(
                    prompt, output_schema, directory, session_id=state.get("astra_session_id")
                )
            elif role == "sol":
                backend_result = self.backend.run_sol(
                    prompt, output_schema, directory, session_id=state.get("sol_session_id")
                )
            elif role == "reviewer":
                backend_result = self.backend.run_reviewer(
                    prompt, output_schema, directory,
                    used_session_ids=self._used_fresh_session_ids(state),
                )
            elif role == "luna":
                backend_result = self.backend.run_luna(
                    prompt, output_schema, directory,
                    used_session_ids=self._used_fresh_session_ids(state),
                )
            else:
                raise ValueError(f"unsupported orchestration role: {role}")
            if (
                not isinstance(backend_result.session_id, str)
                or not backend_result.session_id.strip()
                or backend_result.session_id != backend_result.session_id.strip()
            ):
                raise ValueError(f"{role} backend returned an invalid session id")
            validated = validator(backend_result.output)
            if self.shared_store is not None and role in {"astra", "sol"}:
                exported = self.shared_store.save_session(backend_result.session_id)
                if not exported and isinstance(self.backend, CodexCliBackend):
                    self.audit.append_event(
                        "CODEX_SESSION_ROLLOUT_UNAVAILABLE",
                        {"role": role, "session_id": backend_result.session_id},
                    )
            receipt = self.audit.finish_call(
                call_id, validated, _call_receipt(backend_result, role, backend_result.mode)
            )
            if self.shared_store is not None:
                self.shared_store.save()
            self.observer.record_call(call_id, receipt)
            return validated, receipt, call_id
        except SharedStateError:
            raise
        except BackendUsageLimitExceeded as error:
            failure = {
                "error_type": type(error).__name__,
                "failure_kind": error.failure_kind,
                "codex_error_info": error.codex_error_info,
                "message": str(error),
                "terminal_stop_code": USAGE_LIMIT_STOP_CODE,
            }
            self.audit.fail_call(call_id, failure, ambiguous=role == "sol")
            if self.shared_store is not None:
                self.shared_store.save()
            self.observer.record_error(
                call_id, type(error).__name__, self._observed_call_session_id(call_id, role)
            )
            raise UsageLimitStop(
                role,
                call_id,
                str(error),
                self._observed_call_session_id(call_id, role),
            ) from error
        except Exception as error:
            self.audit.fail_call(
                call_id,
                {"error_type": type(error).__name__, "message": str(error)},
                ambiguous=role == "sol",
            )
            if self.shared_store is not None:
                self.shared_store.save()
            self.observer.record_error(
                call_id, type(error).__name__, self._observed_call_session_id(call_id, role)
            )
            raise RoleCallFailure(
                role, call_id, str(error), ambiguous=role == "sol",
                session_id=self._observed_call_session_id(call_id, role),
            ) from error

    def _terminal_failure(
        self,
        state: Mapping[str, Any],
        source: str,
        reason: str,
        *,
        call_id: str = "",
        count_call: bool = False,
        count_model_error: bool = True,
        error_code: str | None = None,
        error_role: str | None = None,
    ) -> dict[str, Any]:
        error_record: dict[str, Any] = {
            "source": source,
            "message": reason,
            "call_id": call_id,
        }
        if error_code:
            error_record["code"] = error_code
        if error_role:
            error_record["role"] = error_role
        self.audit.append_error(error_record)
        updates: dict[str, Any] = {
            "route": "END",
            "status": "FAILED",
            "paused": False,
            "pending_user_request": None,
            "must_pause_user": False,
            "last_error": error_record,
            "last_call_id": call_id,
            "call_sequence": int(state.get("call_sequence", 0)) + (1 if count_call and call_id else 0),
            "model_error_count": int(state.get("model_error_count", 0)) + (
                1 if count_model_error else 0
            ),
            "step_count": int(state.get("step_count", 0)),
            "transition_log": list(state.get("transition_log", [])),
        }
        if int(state.get("step_count", 0)) < int(state["execution_limits"]["max_transitions"]):
            transition = _transition(self.audit, state, source, "END", reason)
            updates["step_count"] = int(state.get("step_count", 0)) + 1
            updates["transition_log"] = [*state.get("transition_log", []), transition]
        return updates

    def _usage_limit_stop(
        self,
        state: Mapping[str, Any],
        error: UsageLimitStop,
    ) -> dict[str, Any]:
        reason = (
            "Codex usage is exhausted. The workflow stopped permanently without "
            "retry, automatic recovery, USER routing, reset-credit redemption, "
            "credit purchase, or scheduled continuation."
        )
        updates = self._terminal_failure(
            state,
            error.role.upper(),
            reason,
            call_id=error.call_id,
            count_call=True,
            count_model_error=False,
            error_code=USAGE_LIMIT_STOP_CODE,
            error_role=error.role,
        )
        if error.session_id:
            if error.role in {"astra", "sol"}:
                updates[f"{error.role}_session_id"] = error.session_id
            elif error.role == "luna":
                sessions = list(state.get("luna_session_ids", []))
                if error.session_id not in sessions:
                    sessions.append(error.session_id)
                updates["luna_session_ids"] = sessions
            elif error.role == "reviewer":
                sessions = list(state.get("review_session_ids", []))
                if error.session_id not in sessions:
                    sessions.append(error.session_id)
                updates["review_session_ids"] = sessions
        return updates

    def _limit_guard(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        if int(state.get("step_count", 0)) < int(state["execution_limits"]["max_transitions"]):
            return None
        return self._terminal_failure(
            state,
            "TRANSITION_GUARD",
            "The maximum transition count was reached; no USER interruption was fabricated.",
        )

    def _model_error_guard(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        # A persisted recovery episode is bounded independently at five
        # attempts. Do not let a smaller model-error bound pre-empt that
        # deterministic policy before Astra can record the next strategy or
        # route the exhausted episode to USER.
        if state.get("recovery_issue_id"):
            return None
        if int(state.get("model_error_count", 0)) < int(state["execution_limits"]["max_model_errors"]):
            return None
        return self._terminal_failure(
            state,
            "MODEL_ERROR_GUARD",
            "The model/structured-output error limit was reached; no USER interruption was fabricated.",
        )

    def _plan_updates(self, state: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
        revision = decision.get("plan_revision")
        if revision is None:
            return {}
        if decision["route"] not in {"SOL", "LUNA"}:
            raise ValidationError("a plan_revision must immediately route to bounded SOL or LUNA work")
        if decision.get("stage_transition") is not None:
            raise ValidationError("plan_revision and stage_transition cannot occur in one decision")
        used_ids = {state["active_plan"]["revision_id"]}
        used_ids.update(item["revision_id"] for item in state.get("plan_change_history", []))
        if revision["revision_id"] in used_ids:
            raise ValidationError("plan_revision revision_id must be new")
        active = {
            key: deepcopy(revision[key])
            for key in (
                "revision_id", "current_stage", "project_context", "target_stage",
                "stages", "experiment_plan", "datasets", "evaluation_criteria", "approaches",
            )
        }
        return {
            "active_plan": active,
            "current_stage": active["current_stage"],
            "plan_change_history": [*state.get("plan_change_history", []), deepcopy(revision)],
            "target_stage_completed": False,
            "sol_session_id": None,
            "pending_sol_task": None,
            "last_sol_task": None,
            "sol_result": None,
            "pending_luna_task": None,
            "last_luna_task": None,
            "luna_result": None,
            "review_package": None,
            "reviewer_result": None,
            "correction_required": False,
            "correction_ready": False,
        }

    def _apply_stage_transition(
        self, state: Mapping[str, Any], decision: Mapping[str, Any]
    ) -> dict[str, Any]:
        transition = decision.get("stage_transition")
        if transition is None:
            return {}
        current = state["current_stage"]
        if transition["from_stage"] != current:
            raise ValidationError("Astra stage transition does not start at the current stage")
        stage = _stage(state["active_plan"], current)
        reviewer = state.get("reviewer_result")
        if stage["requires_review"]:
            if not reviewer or reviewer.get("verdict") != "PASS":
                raise ValidationError("a required-review stage may advance only after Reviewer PASS")
            if reviewer.get("plan_revision_id") != state["active_plan"]["revision_id"]:
                raise ValidationError("Reviewer PASS belongs to a stale plan revision")
            if reviewer.get("ultimate_purpose") != state["project_spec"]["ultimate_purpose"]:
                raise ValidationError("Reviewer PASS does not preserve the immutable ultimate purpose")
            if transition["based_on_review_id"] != reviewer.get("review_id"):
                raise ValidationError("stage transition must cite the passing review_id")
            package = state.get("review_package")
            if not package or package.get("review_id") != reviewer.get("review_id"):
                raise ValidationError("the passing review must remain bound to its frozen package")
            if package.get("actual_results") != state.get("sol_result"):
                raise ValidationError("the passing review does not cover the current Sol result")
            if package.get("sol_instructions") != state.get("last_sol_task"):
                raise ValidationError("the passing review does not cover the current Sol task")
        else:
            result = state.get("sol_result")
            if not result or result.get("status") != "MILESTONE_COMPLETE":
                raise ValidationError("an unreviewed stage still requires MILESTONE_COMPLETE evidence")
            if result.get("plan_revision_id") != state["active_plan"]["revision_id"]:
                raise ValidationError("milestone evidence belongs to a stale plan revision")
            if transition["based_on_review_id"] != "NOT_REQUIRED":
                raise ValidationError("unreviewed transition basis must be NOT_REQUIRED")
        expected_next = _next_stage(state["active_plan"], current)
        target = state["active_plan"]["target_stage"]
        completing_target = current == target
        if completing_target:
            if transition["to_stage"] is not None or decision["route"] != "END":
                raise ValidationError("target completion must transition to null and route END")
        else:
            if expected_next is None:
                raise ValidationError(
                    "a non-target final stage cannot advance; the active plan is inconsistent"
                )
            if transition["to_stage"] != expected_next:
                raise ValidationError("Astra may advance only to the next active-plan stage")
        completed = list(state.get("completed_stages", []))
        if current not in completed:
            completed.append(current)
        next_current = current if completing_target else expected_next
        active_plan = deepcopy(state["active_plan"])
        active_plan["current_stage"] = next_current
        return {
            "active_plan": active_plan,
            "current_stage": next_current,
            "completed_stages": completed,
            "target_stage_completed": completing_target,
            "correction_required": False,
            "correction_ready": False,
            "sol_session_id": None,
            "reviewer_result": None,
            "review_package": None,
            "sol_result": None,
            "last_sol_task": None,
            "luna_result": None,
        }

    def _validate_recovery_strategy(
        self, state: Mapping[str, Any], route: str, task: Mapping[str, Any]
    ) -> str | None:
        strategy = task.get("recovery_strategy")
        active_issue = state.get("recovery_issue_id")
        attempts = _active_attempts(state, active_issue)
        if active_issue and len(attempts) >= RECOVERY_LIMIT:
            raise ValidationError("five automatic recovery strategies have already failed")
        if active_issue and strategy is None:
            raise ValidationError("an active recovery issue requires a structured recovery_strategy")
        if strategy is None:
            return None
        issue_id = strategy["issue_id"]
        if active_issue and issue_id != active_issue:
            raise ValidationError("recovery_strategy issue_id must match the active recovery issue")
        issue_attempts = _active_attempts(state, issue_id)
        expected = len(issue_attempts) + 1
        if strategy["attempt"] != expected:
            raise ValidationError(f"recovery_strategy attempt must be {expected}")
        if expected > RECOVERY_LIMIT:
            raise ValidationError("automatic recovery is limited to five strategies")
        if any(item["strategy_id"] == strategy["strategy_id"] for item in issue_attempts):
            raise ValidationError("recovery strategy_id must be unique for the issue")
        normalized_approach = " ".join(strategy["approach"].casefold().split())
        if any(
            " ".join(item["approach"].casefold().split()) == normalized_approach
            for item in issue_attempts
        ):
            raise ValidationError("recovery strategy approach must differ from prior attempts")
        if route not in {"SOL", "LUNA"}:
            raise ValidationError("recovery strategies may route only to SOL or LUNA")
        return issue_id

    def _validate_user_route(self, state: Mapping[str, Any], request: Mapping[str, Any]) -> None:
        category = request["category"]
        if category in {"AUTH_OR_HUMAN_ACTION_REQUIRED", "PAID_RESOURCE_APPROVAL"}:
            return
        issue_id = state.get("recovery_issue_id")
        attempts = _active_attempts(state, issue_id)
        if not issue_id or len(attempts) != RECOVERY_LIMIT:
            raise ValidationError(
                "AUTOMATIC_RECOVERY_EXHAUSTED requires exactly five recorded attempts"
            )
        if any(attempt["status"] != "FAILED" for attempt in attempts):
            raise ValidationError(
                "AUTOMATIC_RECOVERY_EXHAUSTED requires five failed recovery attempts"
            )

    def _validate_astra_business_rules(
        self, state: Mapping[str, Any], decision: Mapping[str, Any]
    ) -> dict[str, Any]:
        route = decision["route"]
        plan_updates = self._plan_updates(state, decision)
        effective = {**state, **plan_updates}
        transition_updates = self._apply_stage_transition(effective, decision)
        effective = {**effective, **transition_updates}
        active_issue = effective.get("recovery_issue_id")
        attempts = _active_attempts(effective, active_issue)
        if active_issue and len(attempts) >= RECOVERY_LIMIT:
            exhausted_route = (
                route == "USER"
                and decision["user_request"]["category"]
                == "AUTOMATIC_RECOVERY_EXHAUSTED"
            )
            if not exhausted_route:
                raise ValidationError(
                    "five failed recovery strategies require "
                    "AUTOMATIC_RECOVERY_EXHAUSTED USER routing"
                )
        reviewer = effective.get("reviewer_result")
        if reviewer and reviewer.get("verdict") in {"FAIL", "NEEDS_EVIDENCE"}:
            if effective.get("correction_required"):
                allowed = {"SOL", "LUNA", "USER"}
                if effective.get("correction_ready"):
                    allowed.add("REVIEW")
                if route not in allowed:
                    raise ValidationError(
                        "FAIL/NEEDS_EVIDENCE must return through Astra to corrective work"
                    )
        if route == "SOL":
            task = decision["sol_task"]
            if task["stage_id"] != effective["current_stage"]:
                raise ValidationError("Sol task stage_id must equal the active current stage")
            if task["plan_revision_id"] != effective["active_plan"]["revision_id"]:
                raise ValidationError("Sol task must bind to the active plan revision")
            attempts_for_stage = int(
                effective["retry_counts"]["sol_attempts_by_stage"].get(effective["current_stage"], 0)
            )
            if (
                not task.get("recovery_strategy")
                and attempts_for_stage >= int(effective["execution_limits"]["max_sol_attempts_per_stage"])
            ):
                raise ValidationError("maximum Sol attempts for this stage have been reached")
            self._validate_recovery_strategy(effective, route, task)
        elif route == "LUNA":
            task = decision["luna_task"]
            if task["stage_id"] != effective["current_stage"]:
                raise ValidationError("Luna task stage_id must equal the active current stage")
            if task["plan_revision_id"] != effective["active_plan"]["revision_id"]:
                raise ValidationError("Luna task must bind to the active plan revision")
            self._validate_recovery_strategy(effective, route, task)
        elif route == "REVIEW":
            if active_issue:
                raise ValidationError("an unresolved recovery issue cannot route to REVIEW")
            package = decision["review_package"]
            if package["stage_id"] != effective["current_stage"]:
                raise ValidationError("review package stage_id must equal current stage")
            if package["plan_revision_id"] != effective["active_plan"]["revision_id"]:
                raise ValidationError("review package must bind to the active plan revision")
            if package["ultimate_purpose"] != effective["project_spec"]["ultimate_purpose"]:
                raise ValidationError("review package must preserve the immutable ultimate purpose")
            if package["sol_instructions"] != effective.get("last_sol_task"):
                raise ValidationError("review package must contain the exact Sol instructions")
            if package["actual_results"] != effective.get("sol_result"):
                raise ValidationError("review package must contain Sol's actual result")
            if package["evidence_artifact_paths"] != effective["sol_result"]["evidence_artifact_paths"]:
                raise ValidationError("review package evidence paths must exactly match Sol's result")
            if package["known_limitations"] != effective["sol_result"]["known_limitations"]:
                raise ValidationError("review package limitations must exactly match Sol's result")
            declared = _stage(effective["active_plan"], effective["current_stage"])
            if (
                package["milestone_goal"] != declared["goal"]
                or package["acceptance_criteria"] != declared["acceptance_criteria"]
            ):
                raise ValidationError("review package must preserve the active milestone goal and criteria")
            if package["project_context"] != effective["active_plan"]["project_context"]:
                raise ValidationError("review package must contain the active project context")
            if package["project_root"] != effective["project_spec"]["project_root"]:
                raise ValidationError("review package must preserve the project root")
            if package["current_state"] != _review_state_snapshot(effective):
                raise ValidationError("review package must contain the exact controller state snapshot")
            if decision.get("proposed_next_plan") != package["proposed_next_plan"]:
                raise ValidationError("review package must contain Astra's proposed next plan")
            review_attempts = int(
                effective["retry_counts"]["review_attempts_by_stage"].get(effective["current_stage"], 0)
            )
            if review_attempts >= int(effective["execution_limits"]["max_review_attempts_per_stage"]):
                raise ValidationError("maximum Reviewer attempts for this stage have been reached")
            if effective.get("correction_required") and not effective.get("correction_ready"):
                raise ValidationError("corrective Sol work must complete before another review")
        elif route == "USER":
            self._validate_user_route(effective, decision["user_request"])
        elif route == "END":
            if not transition_updates.get("target_stage_completed"):
                raise ValidationError("END requires Astra's valid target-stage completion transition")
            if decision.get("target_stage_completed") is not True:
                raise ValidationError("END must explicitly confirm target completion")
        return {**plan_updates, **transition_updates}

    def _astra_node(self, state: RuntimeState) -> dict[str, Any]:
        # A recovered legacy checkpoint can already be terminal at this node.
        # Never dispatch another manager call from such a state.
        if state.get("status") in {"COMPLETED", "ABORTED", "FAILED"}:
            return {"route": "END"}
        guarded = self._limit_guard(state)
        if guarded is not None:
            return guarded
        guarded = self._model_error_guard(state)
        if guarded is not None:
            return guarded
        prompt = build_astra_prompt(
            state,
            latest_sol_result=state.get("sol_result"),
            latest_reviewer_result=state.get("reviewer_result"),
            latest_luna_result=state.get("luna_result"),
            user_response=state.get("user_response"),
        )
        try:
            decision, receipt, call_id = self._invoke_role(
                state, "astra", prompt, "astra_decision", validate_astra_decision
            )
            session_id = str(receipt["session_id"])
            if state.get("astra_session_id") and session_id != state["astra_session_id"]:
                raise ValidationError("Astra resume returned a different session id")
            if session_id in self._fresh_session_ids(state):
                raise ValidationError("Astra session id reuses a Reviewer or Luna session")
            state_updates = self._validate_astra_business_rules(state, decision)
        except UsageLimitStop as error:
            return self._usage_limit_stop(state, error)
        except RoleCallFailure as error:
            return self._terminal_failure(
                state, "ASTRA", str(error), call_id=error.call_id, count_call=True
            )
        except (ValidationError, AmbiguousCallError, OSError, ValueError) as error:
            return self._terminal_failure(
                state, "ASTRA_DECISION", str(error),
                call_id=locals().get("call_id", ""),
                count_call=bool(locals().get("call_id")),
            )
        route = decision["route"]
        if (
            route in {"SOL", "LUNA", "REVIEW"}
            and int(state.get("step_count", 0)) + 2 > int(state["execution_limits"]["max_transitions"])
        ):
            return self._terminal_failure(
                state, "TRANSITION_GUARD", f"No transition budget remains for {route}.",
                call_id=call_id, count_call=True,
            )
        updates: dict[str, Any] = {
            "astra_decision": decision,
            "astra_session_id": session_id,
            "route": route,
            "status": "COMPLETED" if route == "END" else ("PAUSED_USER" if route == "USER" else "RUNNING"),
            "paused": route == "USER",
            "call_sequence": int(state.get("call_sequence", 0)) + 1,
            "last_call_id": call_id,
            "model_error_count": 0,
            "must_pause_user": False,
            "step_count": int(state.get("step_count", 0)) + 1,
            "user_response": None,
            **state_updates,
        }
        if decision.get("plan_revision") is not None:
            try:
                self.audit.append_jsonl_once(
                    "plan-revisions.jsonl",
                    decision["plan_revision"],
                    identity_fields=("revision_id",),
                )
            except (AmbiguousCallError, OSError, ValueError) as error:
                return self._terminal_failure(
                    state, "PLAN_REVISION_AUDIT", str(error),
                    call_id=call_id, count_call=True,
                )
        effective = {**state, **updates}
        if route == "SOL":
            task = decision["sol_task"]
            updates["pending_sol_task"] = task
            if task.get("recovery_strategy"):
                updates["recovery_issue_id"] = task["recovery_strategy"]["issue_id"]
            updates["reviewer_result"] = None
            updates["review_package"] = None
        elif route == "LUNA":
            task = decision["luna_task"]
            updates["pending_luna_task"] = task
            if task.get("recovery_strategy"):
                updates["recovery_issue_id"] = task["recovery_strategy"]["issue_id"]
            updates["reviewer_result"] = None
            updates["review_package"] = None
        elif route == "REVIEW":
            package = decision["review_package"]
            try:
                self.audit.write_review_packet(package["review_id"], package)
            except (AmbiguousCallError, OSError, ValueError) as error:
                return self._terminal_failure(
                    state, "REVIEW_PACKET", str(error), call_id=call_id, count_call=True
                )
            updates["review_package"] = package
            updates["reviewer_result"] = None
        elif route == "USER":
            updates["pending_user_request"] = decision["user_request"]
        transition_state = {
            **effective,
            # The transition being recorded is this Astra node's one newly
            # consumed step.  Keep the effective plan/stage metadata, but use
            # the pre-node counter so sequences remain strictly monotonic.
            "step_count": int(state.get("step_count", 0)),
        }
        transition = _transition(
            self.audit, transition_state, "ASTRA", route, decision["reason"]
        )
        updates["transition_log"] = [*state.get("transition_log", []), transition]
        return updates

    def _record_recovery(
        self,
        state: Mapping[str, Any],
        route: str,
        task: Mapping[str, Any],
        *,
        success: bool,
        outcome: str,
    ) -> dict[str, Any]:
        strategy = task.get("recovery_strategy")
        if not strategy:
            return {}
        attempt = {
            **dict(strategy),
            "route": route,
            "outcome": outcome,
            "status": "SUCCEEDED" if success else "FAILED",
        }
        self.audit.append_jsonl_once(
            "recovery-attempts.jsonl",
            attempt,
            identity_fields=("issue_id", "strategy_id"),
        )
        return {
            "recovery_attempts": [*state.get("recovery_attempts", []), attempt],
            "recovery_issue_id": None if success else strategy["issue_id"],
        }

    def _sol_node(self, state: RuntimeState) -> dict[str, Any]:
        task = state.get("pending_sol_task")
        if not task:
            return self._terminal_failure(state, "SOL", "SOL route has no pending bounded task")
        call_id = ""
        result: dict[str, Any]
        call_failed = False
        session_id = state.get("sol_session_id")
        try:
            result, receipt, call_id = self._invoke_role(
                state, "sol", build_sol_prompt(state, task), "sol_result", validate_sol_result
            )
            session_id = str(receipt["session_id"])
            if (
                result["task_id"] != task["task_id"]
                or result["stage_id"] != task["stage_id"]
                or result["plan_revision_id"] != task["plan_revision_id"]
            ):
                raise ValidationError("Sol result identifiers do not match its assigned task")
            if state.get("sol_session_id") and session_id != state["sol_session_id"]:
                raise ValidationError("Sol resume returned a different session id")
            if session_id in self._fresh_session_ids(state):
                raise ValidationError("Sol session id reuses a Reviewer or Luna session")
        except UsageLimitStop as error:
            return self._usage_limit_stop(state, error)
        except (RoleCallFailure, ValidationError) as error:
            call_failed = True
            if isinstance(error, RoleCallFailure):
                call_id = error.call_id
                session_id = error.session_id or session_id
            if session_id in self._fresh_session_ids(state):
                session_id = state.get("sol_session_id")
            self.audit.append_error({"source": "SOL", "message": str(error), "call_id": call_id})
            result = {
                "task_id": task["task_id"], "stage_id": task["stage_id"],
                "plan_revision_id": task["plan_revision_id"], "status": "FAILED",
                "summary": "Sol did not produce a safe validated result.",
                "evidence": [], "evidence_artifact_paths": [],
                "known_limitations": ["Inspect the durable call receipts before further work."],
                "blockers": [], "user_actions_requested": [], "error": str(error),
            }
        retries = deepcopy(state["retry_counts"])
        stage_id = task["stage_id"]
        # Automatic recovery has its own persisted five-attempt budget.  Do not
        # also consume the ordinary Sol work budget or a successfully resumed
        # workflow could be unable to continue after the user resolves an
        # exhausted recovery episode.
        if not task.get("recovery_strategy"):
            retries["sol_attempts_by_stage"][stage_id] = (
                retries["sol_attempts_by_stage"].get(stage_id, 0) + 1
            )
            retries["sol_attempts_by_task"][task["task_id"]] = (
                retries["sol_attempts_by_task"].get(task["task_id"], 0) + 1
            )
        success = result["status"] in {"DONE", "MILESTONE_COMPLETE"}
        try:
            recovery_updates = self._record_recovery(
                state, "SOL", task, success=success, outcome=result["summary"]
            )
        except (AmbiguousCallError, OSError, ValueError) as error:
            return self._terminal_failure(
                state, "RECOVERY_AUDIT", str(error),
                call_id=call_id, count_call=bool(call_id),
            )
        if not task.get("recovery_strategy") and result["status"] in {"BLOCKED", "NEEDS_USER", "FAILED"}:
            recovery_updates["recovery_issue_id"] = state.get("recovery_issue_id") or _safe_issue_id(
                "sol", f"{call_id or state.get('step_count', 0)}-{task['task_id']}"
            )
        correction_ready = bool(state.get("correction_required")) and result["status"] == "MILESTONE_COMPLETE"
        transition = _transition(self.audit, state, "SOL", "ASTRA", result["status"])
        return {
            "sol_result": result,
            "last_sol_task": task,
            "pending_sol_task": None,
            "sol_session_id": session_id,
            "retry_counts": retries,
            "correction_ready": correction_ready,
            "correction_required": bool(state.get("correction_required")) and not correction_ready,
            "call_sequence": int(state.get("call_sequence", 0)) + (1 if call_id else 0),
            "last_call_id": call_id,
            "model_error_count": int(state.get("model_error_count", 0)) + (1 if call_failed else 0),
            "must_pause_user": False,
            "step_count": int(state.get("step_count", 0)) + 1,
            "transition_log": [*state.get("transition_log", []), transition],
            **recovery_updates,
        }

    def _luna_node(self, state: RuntimeState) -> dict[str, Any]:
        task = state.get("pending_luna_task")
        if not task:
            return self._terminal_failure(state, "LUNA", "LUNA route has no pending bounded task")
        call_id = ""
        result: dict[str, Any]
        observed_session: str | None = None
        call_failed = False
        try:
            result, receipt, call_id = self._invoke_role(
                state, "luna", build_luna_prompt(state, task), "luna_result", validate_luna_result
            )
            if (
                result["research_id"] != task["research_id"]
                or result["stage_id"] != task["stage_id"]
                or result["plan_revision_id"] != task["plan_revision_id"]
            ):
                raise ValidationError("Luna result identifiers do not match its assigned task")
            observed_session = str(receipt["session_id"])
            if receipt.get("mode") != "new":
                raise ValidationError("Luna must always use a new session")
            if observed_session in self._state_session_ids(state):
                raise ValidationError("Luna session id is not fresh")
        except UsageLimitStop as error:
            return self._usage_limit_stop(state, error)
        except (RoleCallFailure, ValidationError) as error:
            call_failed = True
            if isinstance(error, RoleCallFailure):
                call_id = error.call_id
                observed_session = error.session_id
            if not observed_session and call_id:
                observed_session = self._observed_call_session_id(call_id, "luna")
            self.audit.append_error({"source": "LUNA", "message": str(error), "call_id": call_id})
            result = {
                "research_id": task["research_id"], "stage_id": task["stage_id"],
                "plan_revision_id": task["plan_revision_id"], "status": "FAILED",
                "summary": "Luna did not produce a safe validated research result.",
                "findings": [], "sources": [], "evidence_artifact_paths": [],
                "known_limitations": ["Inspect the durable call receipts."],
                "blockers": [], "error": str(error),
            }
        sessions = list(state.get("luna_session_ids", []))
        if observed_session and observed_session not in sessions:
            sessions.append(observed_session)
        success = result["status"] == "DONE"
        try:
            recovery_updates = self._record_recovery(
                state, "LUNA", task, success=success, outcome=result["summary"]
            )
        except (AmbiguousCallError, OSError, ValueError) as error:
            return self._terminal_failure(
                state, "RECOVERY_AUDIT", str(error),
                call_id=call_id, count_call=bool(call_id),
            )
        if not task.get("recovery_strategy") and not success:
            recovery_updates["recovery_issue_id"] = state.get("recovery_issue_id") or _safe_issue_id(
                "luna", f"{call_id or state.get('step_count', 0)}-{task['research_id']}"
            )
        transition = _transition(self.audit, state, "LUNA", "ASTRA", result["status"])
        return {
            "luna_result": result,
            "last_luna_task": task,
            "pending_luna_task": None,
            "luna_session_ids": sessions,
            "call_sequence": int(state.get("call_sequence", 0)) + (1 if call_id else 0),
            "last_call_id": call_id,
            "model_error_count": int(state.get("model_error_count", 0)) + (1 if call_failed else 0),
            "must_pause_user": False,
            "step_count": int(state.get("step_count", 0)) + 1,
            "transition_log": [*state.get("transition_log", []), transition],
            **recovery_updates,
        }

    def _review_node(self, state: RuntimeState) -> dict[str, Any]:
        package = state.get("review_package")
        if not package:
            return self._terminal_failure(state, "REVIEW", "REVIEW route has no frozen package")
        if self.shared_store is not None:
            original_path = self.audit.review_dir(package["review_id"]) / "packet.json"
            if original_path.is_file() and read_json(original_path) != package:
                host_view = {
                    "original_packet_sha256": sha256_file(original_path),
                    "host_project_root": str(self.project_root),
                    "execution_package": package,
                }
                digest = sha256_bytes(canonical_json(host_view))
                view_path = original_path.parent / f"execution-packet-{digest[:16]}.json"
                if not view_path.exists():
                    write_json_exclusive(view_path, host_view)
                elif sha256_file(view_path) != digest:
                    raise AmbiguousCallError("review host-view packet conflicts with saved evidence")
        call_id = ""
        observed_session: str | None = None
        try:
            result, receipt, call_id = self._invoke_role(
                state, "reviewer", build_reviewer_prompt(package),
                "reviewer_result", validate_reviewer_result,
            )
            if (
                result["review_id"] != package["review_id"]
                or result["stage_id"] != package["stage_id"]
                or result["plan_revision_id"] != package["plan_revision_id"]
                or result["ultimate_purpose"] != package["ultimate_purpose"]
            ):
                raise ValidationError("Reviewer result identifiers do not match the review package")
            observed_session = str(receipt["session_id"])
            if receipt.get("mode") != "new":
                raise ValidationError("Reviewer must always use a new session")
            if observed_session in self._state_session_ids(state):
                raise ValidationError("Reviewer session id is not fresh")
            _write_review_result(self.audit, package["review_id"], result, receipt)
        except UsageLimitStop as error:
            return self._usage_limit_stop(state, error)
        except (RoleCallFailure, ValidationError, AmbiguousCallError, OSError) as error:
            if isinstance(error, RoleCallFailure):
                call_id = error.call_id
                observed_session = error.session_id
            if not observed_session and call_id:
                observed_session = self._observed_call_session_id(call_id, "reviewer")
            self.audit.append_error({"source": "REVIEW", "message": str(error), "call_id": call_id})
            retries = deepcopy(state["retry_counts"])
            stage_id = package["stage_id"]
            retries["review_attempts_by_stage"][stage_id] = retries["review_attempts_by_stage"].get(stage_id, 0) + 1
            sessions = list(state.get("review_session_ids", []))
            if observed_session and observed_session not in sessions:
                sessions.append(observed_session)
            transition = _transition(self.audit, state, "REVIEW", "ASTRA", "review call failed")
            return {
                "reviewer_result": None,
                "review_session_ids": sessions,
                "retry_counts": retries,
                "recovery_issue_id": state.get("recovery_issue_id") or _safe_issue_id(
                    "review", f"{call_id or state.get('step_count', 0)}-{package['review_id']}"
                ),
                "last_error": {"source": "REVIEW", "message": str(error), "call_id": call_id},
                "model_error_count": int(state.get("model_error_count", 0)) + 1,
                "call_sequence": int(state.get("call_sequence", 0)) + (1 if call_id else 0),
                "last_call_id": call_id,
                "must_pause_user": False,
                "step_count": int(state.get("step_count", 0)) + 1,
                "transition_log": [*state.get("transition_log", []), transition],
            }
        retries = deepcopy(state["retry_counts"])
        stage_id = package["stage_id"]
        retries["review_attempts_by_stage"][stage_id] = retries["review_attempts_by_stage"].get(stage_id, 0) + 1
        sessions = [*state.get("review_session_ids", []), observed_session]
        failed = result["verdict"] in {"FAIL", "NEEDS_EVIDENCE"}
        transition = _transition(self.audit, state, "REVIEW", "ASTRA", result["verdict"])
        return {
            "reviewer_result": result,
            "review_session_ids": sessions,
            "retry_counts": retries,
            "correction_required": failed,
            "correction_ready": False,
            "recovery_issue_id": (
                _safe_issue_id("review", f"{call_id}-{package['review_id']}")
                if failed
                else None
            ),
            "model_error_count": 0,
            "must_pause_user": False,
            "call_sequence": int(state.get("call_sequence", 0)) + 1,
            "last_call_id": call_id,
            "step_count": int(state.get("step_count", 0)) + 1,
            "transition_log": [*state.get("transition_log", []), transition],
        }

    def _user_node(self, state: RuntimeState) -> dict[str, Any]:
        response_value = interrupt(state["pending_user_request"])
        response = validate_user_response(response_value)
        if response["request_id"] != state["pending_user_request"]["request_id"]:
            raise ValidationError("user response request_id does not match the pending interrupt")
        aborted = response["status"] in {"DECLINED", "CANCELLED"}
        destination = "END" if aborted else "ASTRA"
        transition = _transition(self.audit, state, "USER", destination, response["status"])
        updates: dict[str, Any] = {
            "user_response": response,
            "pending_user_request": None,
            "route": "END" if aborted else "SOL",
            "status": "ABORTED" if aborted else "RUNNING",
            "paused": False,
            "must_pause_user": False,
            "model_error_count": state.get("model_error_count", 0) if aborted else 0,
            "step_count": int(state.get("step_count", 0)) + 1,
            "transition_log": [*state.get("transition_log", []), transition],
        }
        if not aborted:
            updates.update(
                recovery_issue_id=None,
                sol_result=None,
                luna_result=None,
                reviewer_result=None,
                review_package=None,
                correction_required=False,
                correction_ready=False,
            )
        return updates

    def invoke(self, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = state or self.status()
        if current.get("status") in {"COMPLETED", "ABORTED", "FAILED"}:
            return dict(current)
        maximum = int(current["execution_limits"]["max_transitions"])
        with self.observer.invocation(
            self.run_id, current.get("current_stage"), "start" if state else "continue"
        ):
            # LangGraph's automatic tracing would include full state and prompts.
            # Explicit allowlisted RunTree spans remain active in this context.
            with tracing_context(enabled=False):
                outcome = self.graph.invoke(
                    state, {**self.config, "recursion_limit": maximum * 3 + 20}
                )
            self.observer.record_outcome(outcome)
            return outcome

    def resume(self, response: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_user_response(response)
        current = self.status()
        maximum = int(current["execution_limits"]["max_transitions"])
        with self.observer.invocation(self.run_id, current.get("current_stage"), "resume"):
            with tracing_context(enabled=False):
                outcome = self.graph.invoke(
                    Command(resume=validated),
                    {**self.config, "recursion_limit": maximum * 3 + 20},
                )
            self.observer.record_outcome(outcome)
            return outcome

    def continue_after_restart(self) -> dict[str, Any]:
        return self.invoke(None)

    def status(self) -> dict[str, Any]:
        snapshot = self.graph.get_state(self.config)
        return dict(snapshot.values)

    def close(self) -> None:
        self.connection.close()
        if self.shared_store is not None and self.shared_store.sqlite_connection is self.connection:
            self.shared_store.sqlite_connection = None

    def __enter__(self) -> "OrchestrationRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
