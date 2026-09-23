"""Structured Outputs transport-schema checks; no Codex process is started."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from astra_orchestrator import codex_cli
from astra_orchestrator.codex_cli import BackendError, CodexCliBackend
from astra_orchestrator.schema import (
    ValidationError,
    schema_path,
    validate_astra_decision,
    validate_sol_result,
)
from astra_orchestrator.transport_schema import (
    codex_transport_schema,
    decode_codex_transport_output,
)
from tests.test_codex_sessions import FakeProcess, events
from tests.test_orchestration_schema import review_package


FORBIDDEN = {
    "oneOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "minLength",
    "maxLength",
    "const",
    "title",
}


def walk_schema(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_schema(child)


class TransportSchemaTests(unittest.TestCase):
    def bundled(self, name: str):
        return json.loads(schema_path(name).read_text(encoding="utf-8"))

    def test_all_live_role_schemas_fit_structured_outputs_subset(self):
        for name in ("astra_decision", "sol_result", "luna_result", "reviewer_result"):
            with self.subTest(name=name):
                source = self.bundled(name)
                before = deepcopy(source)
                transport = codex_transport_schema(source)
                self.assertEqual(source, before)
                self.assertEqual(transport["type"], "object")
                for node in walk_schema(transport):
                    self.assertFalse(FORBIDDEN.intersection(node))
                    if "enum" in node:
                        self.assertIn("type", node)
                    if node.get("type") == "object":
                        self.assertIs(node.get("additionalProperties"), False)
                        self.assertEqual(
                            node.get("required"), list(node.get("properties", {}))
                        )

    def test_optionals_are_nullable_and_required_without_weakening_source(self):
        source = self.bundled("astra_decision")
        transport = codex_transport_schema(source)
        self.assertNotIn("sol_task", source["required"])
        self.assertIn("sol_task", transport["required"])
        self.assertTrue(
            any(
                branch.get("type") == "null"
                for branch in transport["properties"]["sol_task"]["anyOf"]
            )
        )

        sol_source = self.bundled("sol_result")
        sol_transport = codex_transport_schema(sol_source)
        self.assertNotIn("error", sol_source["required"])
        self.assertIn("error", sol_transport["required"])
        self.assertTrue(
            any(
                branch.get("type") == "null"
                for branch in sol_transport["properties"]["error"]["anyOf"]
            )
        )

        target = transport["properties"]["target_stage_completed"]
        self.assertTrue(
            any(branch.get("type") == "null" for branch in target["anyOf"])
        )
        self.assertTrue(
            any(branch.get("enum") == [True] for branch in target["anyOf"])
        )

    def test_dynamic_retry_maps_use_reversible_entry_lists(self):
        source = self.bundled("astra_decision")
        transport = codex_transport_schema(source)
        retry = transport["$defs"]["retryCounts"]
        encoded_map = retry["properties"]["sol_attempts_by_stage"]
        self.assertEqual(encoded_map["type"], "array")
        self.assertEqual(encoded_map["items"]["required"], ["key", "value"])
        self.assertIs(encoded_map["items"]["additionalProperties"], False)

        value = {
            "sol_attempts_by_stage": [
                {"key": "stage-1", "value": 2},
                {"key": "stage-2", "value": 0},
            ],
            "sol_attempts_by_task": [{"key": "task-1", "value": 1}],
            "review_attempts_by_stage": [],
        }
        decoded = decode_codex_transport_output(
            value, source["$defs"]["retryCounts"]
        )
        self.assertEqual(decoded["sol_attempts_by_stage"], {"stage-1": 2, "stage-2": 0})
        self.assertEqual(decoded["sol_attempts_by_task"], {"task-1": 1})
        self.assertEqual(decoded["review_attempts_by_stage"], {})

        duplicate = deepcopy(value)
        duplicate["sol_attempts_by_stage"].append({"key": "stage-1", "value": 3})
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            decode_codex_transport_output(duplicate, source["$defs"]["retryCounts"])

    def test_complete_review_decision_round_trips_transport_representation(self):
        source = self.bundled("astra_decision")
        package = review_package()
        package["current_state"]["retry_counts"]["review_attempts_by_stage"] = {
            "stage-1": 2
        }
        canonical = {
            "decision_id": "decision-review-1",
            "route": "REVIEW",
            "reason": "The milestone is ready for independent review.",
            "proposed_next_plan": "Advance only after an independent PASS.",
            "review_package": package,
        }

        wire = deepcopy(canonical)
        wire.update(
            {
                "stage_transition": None,
                "sol_task": None,
                "luna_task": None,
                "user_request": None,
                "plan_revision": None,
                "end_reason": None,
                "target_stage_completed": None,
            }
        )
        wire_package = wire["review_package"]
        wire_package["package_id"] = None
        wire_package["sol_instructions"]["recovery_strategy"] = None
        wire_package["actual_results"]["error"] = None
        wire_package["evidence_artifact_paths"][0]["description"] = None
        retry_counts = wire_package["current_state"]["retry_counts"]
        for field, values in retry_counts.items():
            retry_counts[field] = [
                {"key": key, "value": value} for key, value in values.items()
            ]

        self.assertTrue(all(retry_counts.values()))
        self.assertIsNone(wire["sol_task"])
        self.assertIsNone(wire_package["package_id"])
        decoded = decode_codex_transport_output(wire, source)
        self.assertEqual(decoded, canonical)
        validated = validate_astra_decision(decoded)
        self.assertEqual(validated["route"], "REVIEW")
        self.assertEqual(validated["review_package"]["review_id"], "review-1")

    def test_decoder_omits_only_optional_nulls_and_preserves_required_null(self):
        source = self.bundled("astra_decision")
        raw = {
            "decision_id": "decision-end",
            "route": "END",
            "reason": "Target passed review.",
            "proposed_next_plan": None,
            "stage_transition": {
                "transition_id": "transition-1",
                "from_stage": "stage-1",
                "to_stage": None,
                "based_on_review_id": "review-1",
                "reason": "The target stage passed.",
                "approved": True,
            },
            "sol_task": None,
            "luna_task": None,
            "review_package": None,
            "user_request": None,
            "plan_revision": None,
            "end_reason": "Requested target completed.",
            "target_stage_completed": True,
        }
        decoded = decode_codex_transport_output(raw, source)
        self.assertNotIn("sol_task", decoded)
        self.assertNotIn("proposed_next_plan", decoded)
        self.assertIn("to_stage", decoded["stage_transition"])
        self.assertIsNone(decoded["stage_transition"]["to_stage"])
        self.assertEqual(validate_astra_decision(decoded)["route"], "END")

        sol_raw = {
            "decision_id": "decision-sol",
            "route": "SOL",
            "reason": "Assign bounded work.",
            "proposed_next_plan": None,
            "stage_transition": None,
            "sol_task": {
                "task_id": "task-1",
                "stage_id": "stage-1",
                "plan_revision_id": "plan-1",
                "objective": "Inspect the frozen evidence.",
                "acceptance_criteria": ["Report the evidence status."],
                "constraints": ["Do not run experiments."],
                "targeted_checks": [],
                "forbidden_areas": ["Training"],
                "recovery_strategy": None,
            },
            "luna_task": None,
            "review_package": None,
            "user_request": None,
            "plan_revision": None,
            "end_reason": None,
            "target_stage_completed": None,
        }
        decoded_sol = decode_codex_transport_output(sol_raw, source)
        self.assertNotIn("target_stage_completed", decoded_sol)
        self.assertNotIn("recovery_strategy", decoded_sol["sol_task"])
        self.assertEqual(validate_astra_decision(decoded_sol)["route"], "SOL")

    def test_decoder_preserves_unknowns_for_authoritative_validator(self):
        source = self.bundled("sol_result")
        raw = {
            "task_id": "task-1",
            "stage_id": "stage-1",
            "plan_revision_id": "plan-1",
            "status": "DONE",
            "summary": "done",
            "evidence": [],
            "evidence_artifact_paths": [],
            "known_limitations": [],
            "blockers": [],
            "user_actions_requested": [],
            "error": None,
            "route": "END",
        }
        decoded = decode_codex_transport_output(raw, source)
        self.assertNotIn("error", decoded)
        self.assertEqual(decoded["route"], "END")
        with self.assertRaisesRegex(ValidationError, "Astra authority"):
            validate_sol_result(decoded)

    def test_backend_writes_transport_schema_but_keeps_graph_schema_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="transport-backend-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            receipt = root / "receipt"
            receipt.mkdir()
            original = self.bundled("sol_result")
            graph_schema_path = receipt / "output-schema.json"
            graph_schema_path.write_text(
                json.dumps(original, indent=2) + "\n", encoding="utf-8"
            )
            (receipt / "request.json").write_text("{}\n", encoding="utf-8")
            (receipt / "status.json").write_text("{}\n", encoding="utf-8")
            transport_result = {
                "task_id": "task-1",
                "stage_id": "stage-1",
                "plan_revision_id": "plan-1",
                "status": "DONE",
                "summary": "done",
                "evidence": [],
                "evidence_artifact_paths": [],
                "known_limitations": [],
                "blockers": [],
                "user_actions_requested": [],
                "error": None,
            }
            backend = CodexCliBackend(workspace, codex_executable="not-run")
            process = FakeProcess(events("sol-session", transport_result))
            with mock.patch.object(codex_cli.subprocess, "Popen", return_value=process):
                result = backend.run_sol("bounded work", original, receipt)

            self.assertNotIn("error", result.output)
            self.assertEqual(
                json.loads(graph_schema_path.read_text(encoding="utf-8")), original
            )
            wire = json.loads(
                (receipt / "codex-output-schema.json").read_text(encoding="utf-8")
            )
            self.assertFalse(any(FORBIDDEN.intersection(node) for node in walk_schema(wire)))

    def test_non_object_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "root must have type object"):
            codex_transport_schema({"type": "array", "items": {"type": "string"}})


if __name__ == "__main__":
    unittest.main()
