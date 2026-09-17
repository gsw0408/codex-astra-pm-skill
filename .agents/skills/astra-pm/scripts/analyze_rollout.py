#!/usr/bin/env python3
"""Measure supplied Codex JSONL sessions without exporting conversation content."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import sys


TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "output_tokens",
    "reasoning_output_tokens", "total_tokens",
)
POST_COMPACTION_REQUESTS = 3


class AuditError(ValueError):
    """An input cannot support reliable accounting."""


def timestamp(value):
    if not isinstance(value, str):
        raise AuditError("missing or invalid timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("invalid ISO timestamp") from exc
    if result.tzinfo is None:
        raise AuditError("timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def usage_values(value):
    value = value if isinstance(value, dict) else {}
    result = {}
    for key in TOKEN_FIELDS:
        number = value.get(key)
        if number is not None and (type(number) is not int or number < 0):
            raise AuditError("token counts must be nonnegative integers or null")
        result[key] = number
    inp, cache = result["input_tokens"], result["cached_input_tokens"]
    out, reasoning = result["output_tokens"], result["reasoning_output_tokens"]
    if inp is not None and cache is not None and cache > inp:
        raise AuditError("cached input exceeds input")
    if out is not None and reasoning is not None and reasoning > out:
        raise AuditError("reasoning output exceeds output")
    total = result["total_tokens"]
    if None not in (inp, out, total) and inp + out != total:
        raise AuditError("total tokens differ from input plus output")
    result["noncached_input_tokens"] = inp - cache if None not in (inp, cache) else None
    return result


def aggregate(records):
    result = {}
    for key in (*TOKEN_FIELDS, "noncached_input_tokens"):
        numbers = [record["usage"][key] for record in records]
        result[key] = sum(numbers) if numbers and None not in numbers else None
    inp, cache = result["input_tokens"], result["cached_input_tokens"]
    result["input_cache_ratio"] = cache / inp if inp and cache is not None else None
    return result


def difference(left, right):
    return {
        key: left[key] - right[key] if left.get(key) is not None and right.get(key) is not None else None
        for key in (*TOKEN_FIELDS, "noncached_input_tokens")
    }


def distribution(values):
    if not values:
        return {"count": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "count": len(values), "min": min(values), "median": statistics.median(values),
        "mean": statistics.mean(values), "max": max(values),
    }


def load_session(path, cutoff):
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    events, warnings = [], []
    previous = None
    nonmonotonic = 0
    metadata = None
    metadata_at = None
    excluded = 0
    for index, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except (ValueError, UnicodeError):
            if index == len(lines) and not raw.endswith((b"\n", b"\r")):
                warnings.append({"kind": "truncated_trailing_line", "line": index})
                continue
            raise AuditError(f"invalid JSONL at {path.name}:{index}") from None
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            raise AuditError(f"invalid event object at {path.name}:{index}")
        try:
            when = timestamp(item.get("timestamp"))
        except AuditError as exc:
            raise AuditError(f"{exc} at {path.name}:{index}") from None
        if previous is not None and when < previous:
            nonmonotonic += 1
        previous = when
        if item.get("type") == "session_meta":
            if metadata is not None:
                raise AuditError(f"multiple session metadata records in {path.name}")
            metadata = item["payload"]
            metadata_at = when
        if when > cutoff:
            excluded += 1
            continue
        events.append((index, when, item))
    if not metadata or not metadata.get("id"):
        raise AuditError(f"missing session metadata id in {path.name}")
    if nonmonotonic:
        warnings.append({"kind": "nonmonotonic_event_timestamps", "count": nonmonotonic})
    parent = metadata.get("parent_thread_id")
    source = metadata.get("source")
    if parent is None and isinstance(source, dict):
        parent = source.get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
    return {
        "path": str(path.resolve()), "id": metadata["id"], "parent_id": parent,
        "metadata_at": metadata_at,
        "events": events, "warnings": warnings, "excluded_after_cutoff": excluded,
    }


def new_turn(turn_id):
    return {"turn_id": turn_id, "start": None, "end": None, "records": []}


def tool_input(payload):
    value = payload.get("input", payload.get("arguments", ""))
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def sleep_requested(payload):
    try:
        value = json.loads(tool_input(payload)).get("duration_ms")
        if type(value) in (int, float) and value >= 0:
            return value / 1000
    except (ValueError, AttributeError):
        pass
    return None


def sleep_elapsed(output):
    if not isinstance(output, str):
        return None
    match = re.fullmatch(
        r"Wall time: ([0-9]+(?:\.[0-9]+)?) seconds\s+"
        r"Sleep (?:completed|interrupted by new input)\.\s*",
        output,
    )
    return float(match.group(1)) if match else None


def summarize_session(session, poll_pattern, monitor_turn_id, seen_responses):
    turns, records, contexts = {}, [], {}
    current_turn = None
    last_count = None
    window = None
    compactions = []
    outer, nested = Counter(), Counter()
    calls, sleeps, polls = {}, [], []
    duplicate_count = inherited_count = 0
    malformed_tool_calls = 0
    for line, when, event in session["events"]:
        payload = event["payload"]
        kind, subtype = event.get("type"), payload.get("type")
        if kind == "event_msg" and subtype == "task_started":
            current_turn = payload.get("turn_id")
            turns.setdefault(current_turn, new_turn(current_turn))["start"] = when
            window = payload.get("model_context_window", window)
        elif kind == "turn_context":
            current_turn = payload.get("turn_id", current_turn)
            contexts[current_turn] = (payload.get("model"), payload.get("effort"))
            turns.setdefault(current_turn, new_turn(current_turn))
        elif kind == "event_msg" and subtype == "task_complete":
            turn_id = payload.get("turn_id", current_turn)
            turns.setdefault(turn_id, new_turn(turn_id))["end"] = when
        elif kind == "token_usage_record":
            owner = payload.get("thread_id")
            if owner is not None and owner != session["id"]:
                inherited_count += 1
                continue
            response_id = payload.get("response_id")
            if not isinstance(response_id, str) or not response_id:
                raise AuditError(f"missing response id at {Path(session['path']).name}:{line}")
            turn_id = payload.get("turn_id", current_turn)
            usage = usage_values(payload.get("usage"))
            identity = (session["id"], turn_id, usage)
            if response_id in seen_responses:
                if seen_responses[response_id] != identity:
                    raise AuditError(f"conflicting duplicate response id at {Path(session['path']).name}:{line}")
                duplicate_count += 1
                continue
            seen_responses[response_id] = identity
            model, effort = contexts.get(turn_id, (None, None))
            record = {
                "response_id": response_id, "turn_id": turn_id, "timestamp": iso(when),
                "line": line, "model": model, "effort": effort, "usage": usage,
            }
            records.append(record)
            turns.setdefault(turn_id, new_turn(turn_id))["records"].append(record)
        elif kind == "event_msg" and subtype == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                window = info.get("model_context_window", window)
                cumulative = info.get("total_token_usage")
                if isinstance(cumulative, dict):
                    last_count = {
                        "timestamp": iso(when), "line": line,
                        "usage": usage_values(cumulative),
                        "canonical_at_event": aggregate(records),
                    }
        elif kind == "compacted":
            history = payload.get("replacement_history")
            compactions.append({
                "timestamp": iso(when), "line": line,
                "response_id": payload.get("compaction_response_id"),
                "reported_context_window": window, "trigger": None,
                "replacement_item_count": len(history) if isinstance(history, list) else None,
                "replacement_item_types": dict(Counter(x.get("type", "unknown") for x in history if isinstance(x, dict))) if isinstance(history, list) else None,
                "duration_seconds": None,
            })
        elif kind == "event_msg" and subtype == "item_completed":
            item = payload.get("item", {})
            if isinstance(item, dict) and item.get("type") == "ContextCompaction" and compactions:
                start, end = payload.get("started_at_ms"), payload.get("completed_at_ms")
                if type(start) in (int, float) and type(end) in (int, float) and end >= start:
                    compactions[-1]["duration_seconds"] = (end - start) / 1000
        elif kind == "response_item" and subtype in ("function_call", "custom_tool_call"):
            name, call_id = payload.get("name"), payload.get("call_id")
            if not isinstance(name, str):
                malformed_tool_calls += 1
                continue
            if call_id and call_id in calls:
                continue
            turn_id = payload.get("internal_chat_message_metadata_passthrough", {}).get("turn_id", current_turn)
            call = {"name": name, "timestamp": iso(when), "turn_id": turn_id}
            if call_id:
                calls[call_id] = call
            outer[name] += 1
            text = tool_input(payload)
            if name in ("exec", "functions.exec"):
                nested.update(re.findall(r"\btools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(", text))
            if name in ("sleep", "clock.sleep", "clock__sleep"):
                call.update({"requested_seconds": sleep_requested(payload), "observed_elapsed_seconds": None})
                sleeps.append(call)
            if poll_pattern and (monitor_turn_id is None or monitor_turn_id == turn_id) and poll_pattern.search(text):
                polls.append({"timestamp": iso(when), "line": line, "turn_id": turn_id, "tool_name": name})
        elif kind == "response_item" and subtype in ("function_call_output", "custom_tool_call_output"):
            call = calls.get(payload.get("call_id"))
            if call and "requested_seconds" in call:
                call["observed_elapsed_seconds"] = sleep_elapsed(payload.get("output"))

    records_by_id = {record["response_id"]: record for record in records}
    for compact in compactions:
        record = records_by_id.get(compact["response_id"])
        compact["usage"] = record["usage"] if record else usage_values(None)
        inp, limit = compact["usage"]["input_tokens"], compact["reported_context_window"]
        compact["input_to_reported_window_ratio"] = inp / limit if inp is not None and limit else None
        # Source order identifies subsequent requests even when clocks move backwards.
        later = [record for record in records if record["line"] > compact["line"]][:POST_COMPACTION_REQUESTS]
        compact["post_request_limit"] = POST_COMPACTION_REQUESTS
        compact["post_requests"] = later
        compact["post_usage"] = aggregate(later)
        compact["causal_excess_noncached_tokens"] = None
        compact["observed_information_loss"] = None

    turn_reports = []
    previous_end = previous_response = None
    for turn in turns.values():
        first = turn["records"][0] if turn["records"] else None
        models = Counter((record["model"], record["effort"]) for record in turn["records"])
        first_usage = first["usage"] if first else usage_values(None)
        inp, cache = first_usage["input_tokens"], first_usage["cached_input_tokens"]
        gap = (turn["start"] - previous_end).total_seconds() if turn["start"] and previous_end else None
        response_gap = (timestamp(first["timestamp"]) - previous_response).total_seconds() if first and previous_response else None
        turn_reports.append({
            "turn_id": turn["turn_id"], "started_at": iso(turn["start"]) if turn["start"] else None,
            "completed_at": iso(turn["end"]) if turn["end"] else None,
            "completion_observed": turn["end"] is not None,
            "observed_response_count": len(turn["records"]), "usage": aggregate(turn["records"]),
            "models": [{"model": m, "effort": e, "response_count": n} for (m, e), n in models.items()],
            "resume": {
                "gap_since_previous_completion_seconds": gap if gap is None or gap >= 0 else None,
                "gap_since_previous_response_seconds": response_gap if response_gap is None or response_gap >= 0 else None,
                "first_request": first, "first_input_cache_ratio": cache / inp if inp and cache is not None else None,
            },
        })
        previous_end = turn["end"]
        if turn["records"]:
            previous_response = timestamp(turn["records"][-1]["timestamp"])

    polls.sort(key=lambda entry: entry["timestamp"])
    intervals = [(timestamp(b["timestamp"]) - timestamp(a["timestamp"])).total_seconds() for a, b in zip(polls, polls[1:])]
    requested = [entry["requested_seconds"] for entry in sleeps]
    elapsed = [entry["observed_elapsed_seconds"] for entry in sleeps]
    corroboration = None
    if last_count:
        corroboration = {
            **last_count,
            "canonical_minus_token_count_at_event": difference(last_count["canonical_at_event"], last_count["usage"]),
            "canonical_minus_token_count_at_cutoff": difference(aggregate(records), last_count["usage"]),
        }
    session_models = Counter((record["model"], record["effort"]) for record in records)
    return {
        "session_id": session["id"], "parent_thread_id": session["parent_id"], "source_path": session["path"],
        "last_included_event_at": max((iso(when) for _, when, _ in session["events"]), default=None),
        "excluded_events_after_cutoff": session["excluded_after_cutoff"], "warnings": session["warnings"],
        "observed_response_count": len(records), "duplicate_response_records": duplicate_count,
        "inherited_response_records_ignored": inherited_count,
        "unknown_model_response_count": sum(record["model"] is None for record in records),
        "models": [{"model": model, "effort": effort, "response_count": count} for (model, effort), count in session_models.items()],
        "usage_status": "unavailable" if not records else "partial" if any(None in record["usage"].values() for record in records) else "recorded",
        "usage": aggregate(records), "turns": turn_reports, "token_count_corroboration": corroboration,
        "tools": {
            "outer_call_counts": dict(outer), "nested_static_mentions": dict(nested),
            "nested_counts_are_executed_calls": False, "unnamed_calls": malformed_tool_calls,
            "sleep": {
                "call_count": len(sleeps),
                "requested_seconds": sum(requested) if requested and None not in requested else None,
                "observed_elapsed_seconds": sum(elapsed) if elapsed and None not in elapsed else None,
                "elapsed_available_count": sum(value is not None for value in elapsed), "calls": sleeps,
            },
            "polling": {
                "selector_available": poll_pattern is not None, "monitor_turn_id": monitor_turn_id,
                "selector_pattern": poll_pattern.pattern if poll_pattern else None,
                "matched_call_count": len(polls) if poll_pattern else None,
                "interval_seconds": intervals if poll_pattern else None,
                "interval_distribution_seconds": distribution(intervals) if poll_pattern else None,
                "calls": polls if poll_pattern else None, "causal_token_cost": None,
            },
        },
        "compactions": compactions,
    }, records


def analyze(paths, root_thread_id, cutoff, poll_pattern=None, monitor_turn_id=None):
    cutoff = timestamp(cutoff)
    try:
        pattern = re.compile(poll_pattern) if poll_pattern is not None else None
    except re.error as exc:
        raise AuditError("invalid polling regular expression") from exc
    sessions = [load_session(Path(path), cutoff) for path in paths]
    ids = [session["id"] for session in sessions]
    if len(ids) != len(set(ids)):
        raise AuditError("each session id must be supplied once")
    available = [session for session in sessions if session["metadata_at"] <= cutoff]
    if root_thread_id not in {session["id"] for session in available}:
        raise AuditError("root thread is absent from supplied sessions at cutoff")
    scope = {root_thread_id}
    while True:
        expanded = scope | {session["id"] for session in available if session["parent_id"] in scope}
        if expanded == scope:
            break
        scope = expanded
    reports, records, seen = [], [], {}
    for session in sessions:
        if session["id"] in scope:
            report, session_records = summarize_session(session, pattern, monitor_turn_id, seen)
            reports.append(report)
            records.extend(session_records)
    model_groups = {}
    for record in records:
        model_groups.setdefault((record["model"], record["effort"]), []).append(record)
    return {
        "schema_version": 1, "root_thread_id": root_thread_id, "cutoff": iso(cutoff),
        "last_included_event_at": max((report["last_included_event_at"] for report in reports if report["last_included_event_at"]), default=None),
        "scope": "root and descendants identified only among supplied session metadata",
        "excluded_unrelated_session_ids": [session["id"] for session in available if session["id"] not in scope],
        "excluded_future_session_ids": [session["id"] for session in sessions if session["metadata_at"] > cutoff],
        "session_count": len(reports), "observed_response_count": len(records), "usage": aggregate(records),
        "sessions_without_usage_records": [report["session_id"] for report in reports if report["usage_status"] == "unavailable"],
        "models": [{"model": model, "effort": effort, "observed_response_count": len(group), "usage": aggregate(group)} for (model, effort), group in model_groups.items()],
        "sessions": reports,
        "limitations": [
            "Usage is recorded response-id-deduplicated token_usage_record usage; cumulative values are not summed.",
            "Cached input is part of input; reasoning output is part of output. No billing conversion is performed.",
            "Absent metrics are null. Recorded usage does not establish coverage of unlogged requests or unsupplied descendants.",
            "Nonmonotonic events retain source order for model/turn attribution; polling intervals use timestamps.",
            "Nested tool mentions are static regex matches and can include strings, comments, or unexecuted branches.",
            "Polling matches may be false positives. Intervals are between matching call timestamps, not measured waiting time.",
            "Compaction and post-request measurements do not establish context overflow, information loss, or causal cost.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", required=True, type=Path)
    parser.add_argument("--root-thread-id", required=True)
    parser.add_argument("--cutoff", required=True, help="Inclusive ISO timestamp with timezone")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--poll-pattern", help="Regex over tool input; selects polling candidates, not proven polls")
    parser.add_argument("--monitor-turn-id", help="Restrict polling selection to one turn")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {path.resolve() for path in args.session}:
            raise AuditError("output must not overwrite an input session")
        report = analyze(args.session, args.root_thread_id, args.cutoff, args.poll_pattern, args.monitor_turn_id)
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered)
    except (AuditError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"OK: {report['session_count']} sessions; {report['observed_response_count']} recorded responses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
