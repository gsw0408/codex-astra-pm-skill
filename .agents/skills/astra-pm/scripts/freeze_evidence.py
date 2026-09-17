#!/usr/bin/env python3
"""Freeze private audit evidence without claiming historical rule versions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def capture_bytes(source: Path) -> Tuple[bytes, Dict[str, Any]]:
    """Read only the length observed on opening; later appends are excluded."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"source is not a regular file: {source}")
    with source.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"source is not a regular file: {source}")
        data = stream.read(before.st_size)
        after = os.fstat(stream.fileno())
    return data, {
        "source_path": str(source),
        "captured_at_utc": utc_now(),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "ends_with_newline": data.endswith(b"\n"),
        "read_limit_bytes": before.st_size,
        "source_size_after_read": after.st_size,
        "complete_for_initial_size": len(data) == before.st_size,
        "source_changed_during_read": (
            before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
        ),
    }


def jsonl_records(data: bytes) -> Iterable[Tuple[int, Dict[str, Any]]]:
    """Ignore invalid records, including a writer's unfinished final record."""
    for line_number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict):
            yield line_number, record


def session_identity(data: bytes) -> Dict[str, Any]:
    for line_number, record in jsonl_records(data):
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            if payload["id"].strip():
                return {
                    "session_id": payload["id"],
                    "session_id_status": "observed_from_session_meta",
                    "session_meta_line": line_number,
                }
    return {"session_id": None, "session_id_status": "unknown", "session_meta_line": None}


def freeze(
    sessions: List[Path], files: List[Tuple[str, Path]], output: Path
) -> Dict[str, Any]:
    if not sessions and not files:
        raise ValueError("provide at least one --session or --file")
    if len({label for label, _ in files}) != len(files):
        raise ValueError("--file labels must be unique")
    for _, source in [("session", path) for path in sessions] + files:
        if not source.expanduser().is_file():
            raise ValueError(f"source is not a regular file: {source}")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    entries: List[Dict[str, Any]] = []
    sources = [("session", None, path) for path in sessions]
    sources.extend(("current_file", label, path) for label, path in files)
    for index, (kind, label, source) in enumerate(sources, 1):
        data, entry = capture_bytes(source)
        destination = f"{index:04d}.{'jsonl' if kind == 'session' else 'bin'}"
        with (output / destination).open("xb") as stream:
            stream.write(data)
        entry.update({"kind": kind, "label": label, "snapshot_path": destination})
        if kind == "session":
            entry.update(session_identity(data))
        else:
            entry["historical_version_status"] = "historical_version_unknown"
        entries.append(entry)
    manifest = {
        "schema_version": 1,
        "captured_at_utc": utc_now(),
        "capture_method": "one_size_bounded_binary_read_per_source",
        "limitations": [
            "Hashes describe the saved bytes, not future source contents.",
            "Current files do not prove which versions were read in a past session.",
            "An in-place source edit can race capture; this is not a transactional filesystem snapshot.",
        ],
        "entries": entries,
    }
    with (output / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return manifest


def labeled_file(value: str) -> Tuple[str, Path]:
    label, separator, path = value.partition("=")
    if not separator or not label.strip() or not path.strip():
        raise argparse.ArgumentTypeError("--file must be a nonempty label=path")
    return label, Path(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", type=Path, default=[])
    parser.add_argument("--file", action="append", type=labeled_file, default=[])
    parser.add_argument("--output", type=Path, required=True, help="new private evidence directory")
    args = parser.parse_args()
    try:
        manifest = freeze(args.session, args.file, args.output)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"manifest": str(args.output.expanduser().resolve() / "manifest.json"),
                      "entries": len(manifest["entries"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
