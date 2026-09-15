#!/usr/bin/env python3
"""Validate the small set of cross-file Astra PM contract invariants."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List


SKILL_PATH = Path(".agents/skills/astra-pm/SKILL.md")
REFERENCE_PATH = Path(
    ".agents/skills/astra-pm/references/execution-ownership.md"
)
AGENT_CONFIG_PATH = Path(".codex/config.toml")


def read_text(path: Path, errors: List[str]) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read {path}: {exc}")
        return ""


def extract_instructions(profile_text: str, path: Path, errors: List[str]) -> str:
    match = re.search(
        r'^developer_instructions\s*=\s*"""(.*?)"""\s*$',
        profile_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        errors.append(f"missing developer_instructions block in {path}")
        return ""
    return match.group(1)


def extract_schema_fields(instructions: str) -> List[str]:
    marker = "Return no more than"
    if marker not in instructions:
        return []
    schema = instructions.split(marker, 1)[1]
    return re.findall(r"(?m)^([a-z][a-z0-9_]*):", schema)


def extract_pipe_enum(instructions: str, field: str) -> List[str]:
    match = re.search(rf"(?m)^{re.escape(field)}:\s*([^\r\n]+)$", instructions)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split("|")]


def registered_profile_path(config_text: str, role: str) -> str:
    section = re.search(
        rf"(?ms)^\[agents\.{re.escape(role)}\]\s*(.*?)(?=^\[|\Z)",
        config_text,
    )
    if not section:
        return ""
    value = re.search(r'(?m)^config_file\s*=\s*"([^"]+)"\s*$', section.group(1))
    return value.group(1).replace("\\", "/") if value else ""


def validate_skill_report_fields(
    skill: str, profiles: Dict[str, object], errors: List[str]
) -> None:
    expected_sets = [set(profile["fields"]) for profile in profiles.values()]
    common_fields = set.intersection(*expected_sets)
    terra_only = set(profiles["terra_executor"]["fields"]) - common_fields

    match = re.search(
        r"Require seven common English report fields:\s*(.*?)\.", skill
    )
    actual_fields = set(re.findall(r"`([a-z][a-z0-9_]*)`", match.group(1))) if match else set()
    if actual_fields != common_fields:
        errors.append(
            f"SKILL.md common report fields mismatch: expected {sorted(common_fields)}, "
            f"found {sorted(actual_fields)}"
        )

    terra_wait_phrase = "Only Terra may return `waiting`; its profile adds `wait_contract`"
    if terra_only != {"wait_contract"} or terra_wait_phrase not in skill:
        errors.append(
            "SKILL.md must define wait_contract as the sole Terra-only report field"
        )


def validate(project_root: Path, contract: Dict[str, object]) -> List[str]:
    errors: List[str] = []
    skill_path = project_root / SKILL_PATH
    reference_path = project_root / REFERENCE_PATH
    config_path = project_root / AGENT_CONFIG_PATH
    skill = read_text(skill_path, errors)
    reference = read_text(reference_path, errors)
    config = read_text(config_path, errors)

    lifecycle = contract["lifecycle"]
    for phrase in lifecycle["skill_required"]:
        if phrase not in skill:
            errors.append(f"SKILL.md missing lifecycle phrase: {phrase}")
    for phrase in lifecycle["skill_forbidden"]:
        if phrase in skill:
            errors.append(f"SKILL.md contains forbidden lifecycle phrase: {phrase}")
    for phrase in lifecycle["reference_required"]:
        if phrase not in reference:
            errors.append(f"execution-ownership.md missing lifecycle phrase: {phrase}")

    verdicts = contract["enums"]["gate_verdict"]
    rendered_verdicts = f"Astra alone decides {verdicts[0]}, {verdicts[1]}, or {verdicts[2]}."
    if rendered_verdicts not in skill:
        errors.append(f"SKILL.md gate verdicts do not match: {verdicts}")
    if re.search(r"\bCONDITION\b", skill):
        errors.append("SKILL.md contains deprecated gate verdict: CONDITION")

    profiles = contract["profiles"]
    validate_skill_report_fields(skill, profiles, errors)
    mirrors = contract["mirrors"]
    statuses = contract["enums"]["status"]
    for role, profile_contract in profiles.items():
        expected_config_file = profile_contract["config_file"]
        actual_config_file = registered_profile_path(config, role)
        if actual_config_file != expected_config_file:
            errors.append(
                f"{role} config path mismatch: expected {expected_config_file}, "
                f"found {actual_config_file or '<missing>'}"
            )

        profile_path = project_root / ".codex" / expected_config_file
        if not profile_path.is_file():
            errors.append(f"missing profile: {profile_path}")
            continue

        profile_text = read_text(profile_path, errors)
        instructions = extract_instructions(profile_text, profile_path, errors)
        actual_fields = extract_schema_fields(instructions)
        expected_fields = profile_contract["fields"]
        if len(actual_fields) != len(set(actual_fields)):
            errors.append(f"{role} report fields contain duplicates: {actual_fields}")
        if set(actual_fields) != set(expected_fields):
            errors.append(
                f"{role} report fields mismatch: expected {expected_fields}, "
                f"found {actual_fields}"
            )

        actual_statuses = extract_pipe_enum(instructions, "status")
        if actual_statuses != statuses[role]:
            errors.append(
                f"{role} status enum mismatch: expected {statuses[role]}, "
                f"found {actual_statuses}"
            )

        for mirror_name, mirror_text in mirrors.items():
            if mirror_text not in instructions:
                errors.append(f"{role} missing shared {mirror_name} contract")

        if role == "sol_gate_reviewer":
            actual_verdicts = extract_pipe_enum(instructions, "recommendation")
            if actual_verdicts != verdicts:
                errors.append(
                    f"Sol gate verdict mismatch: expected {verdicts}, "
                    f"found {actual_verdicts}"
                )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing .agents and .codex",
    )
    args = parser.parse_args()

    contract_path = Path(__file__).with_name("contract.json")
    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read contract manifest: {exc}")
        return 1

    try:
        errors = validate(args.project_root.resolve(), contract)
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        print(f"ERROR: invalid contract manifest structure: {exc}")
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
