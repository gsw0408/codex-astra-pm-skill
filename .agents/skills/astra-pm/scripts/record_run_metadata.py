#!/usr/bin/env python3
"""Append declared PM state and provenance-backed model observations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from freeze_evidence import capture_bytes, jsonl_records, session_identity, utc_now


def current_file_hashes(project_root: Path) -> List[Dict[str, Any]]:
    paths = [
        project_root / ".agents/skills/astra-pm/SKILL.md",
        project_root / ".codex/config.toml",
        project_root / "AGENTS.md",
        project_root / ".agents/skills/astra-pm/references/execution-ownership.md",
    ]
    paths.extend(sorted((project_root / ".codex/agents").glob("*.toml")))
    results: List[Dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            results.append({"source_path": str(path), "capture_status": "missing",
                            "historical_version_status": "historical_version_unknown"})
            continue
        _, captured = capture_bytes(path)
        captured.update({"capture_status": "captured_current_file",
                         "historical_version_status": "historical_version_unknown"})
        results.append(captured)
    return results


def build_metadata(
    project_root: Path,
    session_id: str,
    pm_status: str = "unknown",
    activation_evidence: Optional[Path] = None,
    rollout: Optional[Path] = None,
    declared_model: Optional[str] = None,
    declared_role: Optional[str] = None,
) -> Dict[str, Any]:
    if pm_status not in {"active", "inactive", "unknown"}:
        raise ValueError("pm_status must be active, inactive, or unknown")
    if not session_id.strip():
        raise ValueError("session_id must not be empty")
    if pm_status == "active" and activation_evidence is None:
        raise ValueError("active PM status requires an existing --activation-evidence file")
    activation = None
    if activation_evidence is not None:
        _, activation = capture_bytes(activation_evidence)
        activation["validation"] = "file_exists_and_hash_captured_only"
    project_root = project_root.expanduser().resolve(strict=True)
    if not project_root.is_dir():
        raise ValueError("project_root must be a directory")
    observed: List[Dict[str, Any]] = []
    rollout_capture = None
    if rollout is not None:
        data, rollout_capture = capture_bytes(rollout)
        observed_ids = set()
        for line_number, record in jsonl_records(data):
            payload = record.get("payload")
            if record.get("type") == "session_meta":
                observed_id = payload.get("id") if isinstance(payload, dict) else None
                if not isinstance(observed_id, str) or not observed_id.strip():
                    raise ValueError("rollout session_meta id is missing or invalid")
                observed_ids.add(observed_id)
                continue
            if record.get("type") != "turn_context" or not isinstance(payload, dict):
                continue
            model = payload.get("model")
            if not isinstance(model, str) or not model.strip():
                continue
            timestamp = record.get("timestamp")
            turn_id = payload.get("turn_id")
            effort = payload.get("effort")
            observed.append({
                "model": model,
                "turn_id": turn_id if isinstance(turn_id, str) and turn_id.strip() else None,
                "effort": effort if isinstance(effort, str) and effort.strip() else None,
                "provenance": {
                    "source_path": rollout_capture["source_path"],
                    "source_sha256": rollout_capture["sha256"],
                    "line_number": line_number,
                    "record_type": "turn_context",
                    "field": "payload.model",
                    "record_timestamp": timestamp if isinstance(timestamp, str) else None,
                },
            })
        if not observed_ids:
            raise ValueError("rollout is missing an observed session_meta id")
        if len(observed_ids) != 1:
            raise ValueError("rollout has conflicting session_meta ids")
        if observed_ids != {session_id}:
            raise ValueError("rollout session_meta id does not match --session-id")
        rollout_capture.update(session_identity(data))
    hashes = current_file_hashes(project_root)
    return {
        "schema_version": 1,
        "captured_at_utc": utc_now(),
        "project_root": str(project_root),
        "session_id": session_id,
        "pm_status": pm_status,
        "pm_status_basis": "caller_declaration_not_semantically_verified",
        "activation_evidence": activation,
        "declared_model": declared_model,
        "declared_role": declared_role,
        "observed_models": observed,
        "observed_model_status": "observed_from_turn_context" if observed else "unknown",
        "observed_role": None,
        "rollout_capture": rollout_capture,
        "current_files": hashes,
        "profile_discovery_scope": ".codex/agents/*.toml; not an assertion of runtime-loaded profiles",
        "limitations": [
            "An activation evidence file is checked for existence, not whether it proves PM activation.",
            "Configured or declared models do not prove executed models; observations use only turn_context.payload.model.",
            "Current file hashes do not prove versions read or applied during this session.",
            "Invalid JSONL records are ignored, including an unfinished final record.",
        ],
    }


def append_metadata(output: Path, metadata: Dict[str, Any]) -> None:
    output = output.expanduser().resolve()
    source_entries = [metadata.get("rollout_capture"), metadata.get("activation_evidence")]
    source_entries.extend(metadata.get("current_files", []))
    for entry in source_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_path"), str):
            continue
        source = Path(entry["source_path"]).expanduser().resolve()
        if output == source or (output.exists() and source.exists() and output.samefile(source)):
            raise ValueError("metadata output must not alias a captured source input")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.stat().st_size:
        with output.open("rb") as stream:
            stream.seek(-1, 2)
            if stream.read(1) != b"\n":
                raise ValueError("existing metadata file has an incomplete final line; refusing to append")
    encoded = (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with output.open("ab") as stream:
        stream.write(encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--pm-status", choices=("active", "inactive", "unknown"), default="unknown")
    parser.add_argument("--activation-evidence", type=Path, help="existing evidence file; meaning is not verified")
    parser.add_argument("--rollout", type=Path)
    parser.add_argument("--declared-model")
    parser.add_argument("--declared-role")
    args = parser.parse_args()
    try:
        metadata = build_metadata(args.project_root, args.session_id, args.pm_status,
                                  args.activation_evidence, args.rollout,
                                  args.declared_model, args.declared_role)
        append_metadata(args.output, metadata)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output.expanduser().resolve()),
                      "session_id": args.session_id, "pm_status": metadata["pm_status"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
