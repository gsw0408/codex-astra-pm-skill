"""Command-line entry point for the persistent orchestration control plane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from uuid import uuid4

from dotenv import load_dotenv

from .audit import (
    AuditLog,
    canonical_json,
    read_json,
    replace_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    write_json_exclusive,
)
from .codex_cli import FIXED_ROLE_SETTINGS, CodexCliBackend, validated_role_settings
from .graph import OrchestrationRuntime, USAGE_LIMIT_STOP_CODE, initial_state
from .schema import SCHEMA_VERSION, validate_project_spec, validate_user_response


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _backend_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "type": "codex_cli",
        "codex_executable": args.codex,
        "timeout_seconds": args.timeout,
        "role_settings": {
            role: dict(settings) for role, settings in FIXED_ROLE_SETTINGS.items()
        },
    }


def _backend_from_manifest(manifest: Mapping[str, Any]) -> CodexCliBackend:
    config = manifest["backend"]
    if config.get("type") != "codex_cli":
        raise ValueError("this command can resume only a codex_cli run")
    role_settings = validated_role_settings(config.get("role_settings", {}))
    return CodexCliBackend(
        manifest["project_root"],
        codex_executable=config["codex_executable"],
        timeout_seconds=float(config["timeout_seconds"]),
        role_settings=role_settings,
    )


def _public_state(state: Mapping[str, Any]) -> dict[str, Any]:
    active_plan = state.get("active_plan")
    active_plan_revision_id = (
        active_plan.get("revision_id") if isinstance(active_plan, Mapping) else None
    )
    plan_change_history = state.get("plan_change_history", [])
    recovery_attempts = state.get("recovery_attempts", [])
    last_error = state.get("last_error")
    usage_limit_exhausted = (
        isinstance(last_error, Mapping)
        and last_error.get("code") == USAGE_LIMIT_STOP_CODE
    )
    return {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "route": state.get("route"),
        "current_stage": state.get("current_stage"),
        "active_plan_revision_id": active_plan_revision_id,
        "plan_revision_count": len(plan_change_history),
        "completed_stages": state.get("completed_stages", []),
        "target_stage_completed": state.get("target_stage_completed", False),
        "step_count": state.get("step_count", 0),
        "review_session_ids": state.get("review_session_ids", []),
        "luna_session_ids": state.get("luna_session_ids", []),
        "recovery_issue_id": state.get("recovery_issue_id"),
        "recovery_attempt_count": len(recovery_attempts),
        "recovery_attempts": recovery_attempts,
        "pending_user_request": state.get("pending_user_request"),
        "last_error": last_error,
        "usage_limit_exhausted": usage_limit_exhausted,
    }


def _workflow_exit_code(state: Mapping[str, Any]) -> int:
    """Map terminal workflow outcomes to automation-safe process codes."""
    if state.get("status") == "FAILED":
        return 1
    if state.get("status") == "ABORTED":
        return 2
    return 0


def _record_outcome(run_dir: Path, state: Mapping[str, Any]) -> None:
    public = _public_state(state)
    if state.get("status") in {"COMPLETED", "ABORTED", "FAILED"}:
        path = run_dir / "final-receipt.json"
        if path.exists():
            receipt = read_json(path)
            if receipt.get("schema_version") != SCHEMA_VERSION:
                raise RuntimeError(
                    "final receipt schema version does not match this orchestrator"
                )
            stable_keys = (
                "run_id", "status", "current_stage", "completed_stages",
                "target_stage_completed", "active_plan_revision_id",
                "plan_revision_count", "review_session_ids",
                "luna_session_ids", "recovery_issue_id",
                "recovery_attempt_count", "recovery_attempts",
            )
            # Older schema-v2 final receipts predate the explicit quota-stop
            # fields. Keep them readable while comparing the new evidence
            # whenever the receipt was created by this policy revision.
            optional_keys = tuple(
                key for key in ("last_error", "usage_limit_exhausted") if key in receipt
            )
            stable_fields = {
                key: receipt.get(key) for key in (*stable_keys, *optional_keys)
            }
            if stable_fields != {key: public.get(key) for key in stable_fields}:
                raise RuntimeError("final receipt already exists with different contents")
        else:
            receipt = {
                "schema_version": SCHEMA_VERSION,
                "finished_at": utc_now(),
                **public,
                "checkpoint_sha256": sha256_file(run_dir / "checkpoint.sqlite"),
                "events_sha256": sha256_file(run_dir / "events.jsonl"),
                "transitions_sha256": sha256_file(run_dir / "transitions.jsonl"),
            }
            write_json_exclusive(path, receipt)
        replace_json(
            run_dir / "resume-info.json",
            {
                "schema_version": SCHEMA_VERSION,
                "resumable": False,
                "run_id": state.get("run_id"),
                "status": state.get("status"),
                "active_plan_revision_id": public["active_plan_revision_id"],
                "plan_revision_count": public["plan_revision_count"],
                "luna_session_ids": public["luna_session_ids"],
                "recovery_issue_id": public["recovery_issue_id"],
                "recovery_attempt_count": public["recovery_attempt_count"],
                "last_error": public["last_error"],
                "usage_limit_exhausted": public["usage_limit_exhausted"],
                "updated_at": utc_now(),
            },
        )
    else:
        replace_json(
            run_dir / "resume-info.json",
            {
                "schema_version": SCHEMA_VERSION,
                "resumable": True,
                "run_id": state.get("run_id"),
                "status": state.get("status"),
                "route": state.get("route"),
                "active_plan_revision_id": public["active_plan_revision_id"],
                "plan_revision_count": public["plan_revision_count"],
                "luna_session_ids": public["luna_session_ids"],
                "recovery_issue_id": public["recovery_issue_id"],
                "recovery_attempt_count": public["recovery_attempt_count"],
                "pending_user_request": state.get("pending_user_request"),
                "checkpoint_path": str(run_dir / "checkpoint.sqlite"),
                "updated_at": utc_now(),
                "warning": "Do not place passwords, tokens, or credential values in a durable response.",
            },
        )


def _verify_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            "unsupported orchestration manifest schema_version; "
            f"expected {SCHEMA_VERSION} (schema v1 runs are not resumable as v2)"
        )
    spec_path = run_dir / manifest["spec_path"]
    if sha256_file(spec_path) != manifest["spec_sha256"]:
        raise RuntimeError("the persisted project specification hash does not match the manifest")
    spec = validate_project_spec(_read_object(spec_path))
    return manifest, spec


def _start(args: argparse.Namespace) -> int:
    spec_source = args.spec.resolve()
    project_root = args.project_root.resolve()
    run_dir = args.run_dir.resolve()
    if not project_root.is_dir():
        raise FileNotFoundError(f"project root is not a directory: {project_root}")
    if run_dir == project_root or run_dir.is_relative_to(project_root):
        raise ValueError(
            "--run-dir must be outside --project-root so Sol cannot modify controller evidence"
        )
    raw_spec = _read_object(spec_source)
    raw_spec["project_root"] = str(project_root)
    spec = validate_project_spec(raw_spec)
    run_id = args.run_id or str(uuid4())
    spec_hash = sha256_bytes(canonical_json(spec))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": utc_now(),
        "project_root": str(project_root),
        "source_spec_path": str(spec_source),
        "spec_path": "spec.json",
        "spec_sha256": spec_hash,
        "checkpoint_path": "checkpoint.sqlite",
        "backend": _backend_config(args),
    }
    audit = AuditLog.create(run_dir, manifest)
    write_json_exclusive(audit.run_dir / "spec.json", spec)
    backend = _backend_from_manifest(manifest)
    with OrchestrationRuntime(audit.run_dir, backend) as runtime:
        runtime.invoke(initial_state(spec, run_id))
        state = runtime.status()
    _record_outcome(audit.run_dir, state)
    print(json.dumps(_public_state(state), ensure_ascii=False, indent=2))
    return _workflow_exit_code(state)


def _response_value(args: argparse.Namespace, state: Mapping[str, Any]) -> dict[str, Any] | None:
    if args.response is None and args.response_file is None:
        return None
    if args.response is not None and args.response_file is not None:
        raise ValueError("use only one of --response or --response-file")
    if args.response_file is not None:
        value = _read_object(args.response_file.resolve())
    else:
        try:
            parsed = json.loads(args.response)
        except ValueError:
            parsed = args.response
        if isinstance(parsed, dict):
            value = parsed
        elif isinstance(parsed, str):
            request = state.get("pending_user_request")
            if not request:
                raise ValueError("plain-text response requires a pending USER request")
            value = {
                "request_id": request["request_id"],
                "status": "PROVIDED",
                "response": parsed,
                "evidence_artifact_paths": [],
            }
        else:
            raise ValueError("--response must be a JSON object or a plain string")
    return validate_user_response(value)


def _resume(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest, _ = _verify_run(run_dir)
    backend = _backend_from_manifest(manifest)
    with OrchestrationRuntime(run_dir, backend) as runtime:
        before = runtime.status()
        response = _response_value(args, before)
        if response is None:
            runtime.continue_after_restart()
        else:
            runtime.resume(response)
        state = runtime.status()
    _record_outcome(run_dir, state)
    print(json.dumps(_public_state(state), ensure_ascii=False, indent=2))
    return _workflow_exit_code(state)


def _status(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest, _ = _verify_run(run_dir)
    backend = _backend_from_manifest(manifest)
    with OrchestrationRuntime(run_dir, backend) as runtime:
        state = runtime.status()
    print(json.dumps(_public_state(state), ensure_ascii=False, indent=2))
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    from .dry_run import run_dry_run

    receipt = run_dry_run(args.output)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["status"] == "PASS" else 1


def _add_backend_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--timeout", type=float, default=900.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="Start a new persistent workflow")
    start.add_argument("--spec", type=Path, required=True)
    start.add_argument("--project-root", type=Path, required=True)
    start.add_argument("--run-dir", type=Path, required=True)
    start.add_argument("--run-id")
    _add_backend_options(start)
    start.set_defaults(handler=_start)
    resume = subparsers.add_parser("resume", help="Resume the existing checkpoint/thread")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--response")
    resume.add_argument("--response-file", type=Path)
    resume.set_defaults(handler=_resume)
    status = subparsers.add_parser("status", help="Inspect persisted state without advancing it")
    status.add_argument("--run-dir", type=Path, required=True)
    status.set_defaults(handler=_status)
    dry = subparsers.add_parser("dry-run", help="Run the local scripted no-experiment proof")
    dry.add_argument("--output", type=Path, required=True)
    dry.set_defaults(handler=_dry_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(override=False)
    args = build_parser().parse_args(argv)
    try:
        if hasattr(args, "timeout") and args.timeout <= 0:
            raise ValueError("--timeout must be positive")
        return int(args.handler(args))
    except Exception as error:
        print(f"orchestration error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
