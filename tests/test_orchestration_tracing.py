"""LangSmith trace evidence from scripted, zero-model orchestration runs."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from astra_orchestrator.codex_cli import FIXED_ROLE_SETTINGS, LEGACY_ROLE_SETTINGS
from astra_orchestrator.dry_run import (
    _automatic_recovery_exhaustion,
    _research_plan_pass,
    _usage_limit_stop,
)
from astra_orchestrator.observability import LangSmithObserver, _usage_metadata


class MemoryLangSmithClient:
    def __init__(self, fail_on: str | None = None):
        self.rows: list[tuple[str, dict]] = []
        self.fail_on = fail_on

    def create_run(self, **kwargs):
        if self.fail_on == "create":
            raise OSError("simulated tracing outage")
        self.rows.append(("create", kwargs))

    def update_run(self, **kwargs):
        if self.fail_on == "update":
            raise OSError("simulated tracing outage")
        self.rows.append(("update", kwargs))

    def flush(self, **kwargs):
        self.rows.append(("flush", kwargs))


class OrchestrationTracingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="astra-tracing-scripted-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "synthetic-project"
        self.project.mkdir()
        self.artifact = self.project / "synthetic-evidence.txt"
        self.artifact.write_text("scripted fixture only\n", encoding="utf-8")

    def research(self, name: str, observer: LangSmithObserver, *, usage=None):
        output = self.root / name
        output.mkdir()
        return _research_plan_pass(
            output, self.project, self.artifact,
            observer=observer, fixture_usage=usage,
        )

    def test_one_parent_contains_all_four_role_spans_with_exact_available_metrics(self):
        client = MemoryLangSmithClient()
        usage = {
            "astra": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15,
                      "total_cost_usd": 0.02},
            "sol": {"input_tokens": 7, "output_tokens": 3},
            "luna": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            "reviewer": {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
        }
        receipt = self.research(
            "traced", LangSmithObserver(enabled=True, client=client), usage=usage
        )
        self.assertEqual(receipt["status"], "COMPLETED")
        self.assertEqual(
            receipt["role_call_order"],
            ["astra", "luna", "astra", "sol", "astra", "reviewer", "astra"],
        )
        created = [row for action, row in client.rows if action == "create"]
        updated = [row for action, row in client.rows if action == "update"]
        self.assertEqual(len(created), 8)
        self.assertEqual(len(updated), 8)
        self.assertEqual(created[0]["name"], "Astra orchestration")
        root_id = created[0]["id"]
        self.assertTrue(all(row["parent_run_id"] == root_id for row in created[1:]))
        self.assertEqual(
            [row["name"] for row in created[1:]],
            ["Astra", "Luna", "Astra", "Sol", "Astra", "Reviewer", "Astra"],
        )
        for row in updated[:-1]:
            metadata = row["extra"]["metadata"]
            role = metadata["role"]
            self.assertEqual(metadata["model"], FIXED_ROLE_SETTINGS[role]["model"])
            self.assertEqual(
                metadata["reasoning_effort"], FIXED_ROLE_SETTINGS[role]["reasoning_effort"]
            )
            self.assertEqual(metadata["stage"], "verified")
            self.assertEqual(metadata["status"], "OK")
            self.assertIn(metadata["routing_decision"], {"SOL", "LUNA", "REVIEW", "END", "ASTRA"})
            self.assertGreaterEqual(metadata["latency_ms"], 0)
            started = datetime.fromisoformat(metadata["started_at"].replace("Z", "+00:00"))
            ended = datetime.fromisoformat(metadata["ended_at"].replace("Z", "+00:00"))
            self.assertLessEqual(started, ended)
            self.assertLess(
                abs((ended - started).total_seconds() * 1000 - metadata["latency_ms"]),
                250,
            )
            self.assertTrue(metadata["session_id"].startswith("dry-research-plan-pass-"))
            if role in {"luna", "reviewer"}:
                self.assertEqual(metadata["session_mode"], "new")
        reviewer = next(row for row in updated if row["name"] == "Reviewer")
        self.assertEqual(reviewer["extra"]["metadata"]["reviewer_verdict"], "PASS")
        self.assertEqual(reviewer["extra"]["metadata"]["routing_decision"], "ASTRA")
        self.assertEqual(updated[-1]["name"], "Astra orchestration")
        self.assertEqual(updated[-1]["extra"]["metadata"]["status"], "COMPLETED")
        sol = next(row for row in updated if row["name"] == "Sol")
        self.assertEqual(sol["extra"]["metadata"]["input_tokens"], 7)
        self.assertEqual(sol["extra"]["metadata"]["output_tokens"], 3)
        self.assertNotIn("total_tokens", sol["extra"]["metadata"])
        self.assertNotIn("cost_usd", sol["extra"]["metadata"])
        astra = next(row for row in updated if row["name"] == "Astra")
        self.assertEqual(astra["extra"]["metadata"]["total_tokens"], 15)
        self.assertEqual(astra["extra"]["metadata"]["cost_usd"], 0.02)
        payload = json.dumps(client.rows, default=str)
        for forbidden in ("prompt", "project_context", "ultimate_purpose", "LANGSMITH_API_KEY"):
            self.assertNotIn(forbidden, payload)

    def test_recovery_attempts_are_traced_without_changing_user_pause(self):
        client = MemoryLangSmithClient()
        output = self.root / "recovery"
        output.mkdir()
        receipt = _automatic_recovery_exhaustion(
            output, self.project, self.artifact,
            observer=LangSmithObserver(enabled=True, client=client),
        )
        self.assertEqual(receipt["status"], "PAUSED_USER")
        self.assertEqual(receipt["recovery_attempt_count"], 5)
        spans = [row["extra"]["metadata"] for action, row in client.rows
                 if action == "update" and row["name"] in {"Sol", "Luna"}]
        self.assertEqual(sorted(span["recovery_attempt"] for span in spans
                                if "recovery_attempt" in span), [1, 2, 3, 4, 5])
        self.assertEqual(len(receipt["luna_session_ids"]), len(set(receipt["luna_session_ids"])))

    def test_tracing_outages_and_disabled_mode_leave_routes_unchanged(self):
        baseline = self.research("baseline", LangSmithObserver(enabled=False))
        for failure in ("create", "update"):
            with self.subTest(failure=failure):
                client = MemoryLangSmithClient(fail_on=failure)
                result = self.research(
                    f"failure-{failure}", LangSmithObserver(enabled=True, client=client)
                )
                for key in ("status", "target_stage_completed", "role_call_order",
                            "role_call_modes", "transition_trace"):
                    self.assertEqual(result[key], baseline[key])
        with patch.dict("os.environ", {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": ""}):
            self.assertFalse(LangSmithObserver().enabled)
        with patch("astra_orchestrator.observability.RunTree", None):
            self.assertFalse(LangSmithObserver(enabled=True, client=MemoryLangSmithClient()).enabled)

    def test_legacy_resume_trace_reports_the_recorded_model(self):
        client = MemoryLangSmithClient()
        observer = LangSmithObserver(
            enabled=True, client=client, role_settings=LEGACY_ROLE_SETTINGS,
        )
        with observer.invocation("legacy-run", "verified", "resume"):
            with observer.role("sol", {"run_id": "legacy-run", "current_stage": "verified"}):
                observer.finish_role("sol", {"status": "RUNNING"})
        sol = next(row for action, row in client.rows if action == "update" and row["name"] == "Sol")
        self.assertEqual(sol["extra"]["metadata"]["model"], "gpt-5.6-sol")
        self.assertEqual(sol["extra"]["metadata"]["reasoning_effort"], "xhigh")

    def test_terminal_backend_error_is_sanitized_and_never_retries(self):
        client = MemoryLangSmithClient()
        output = self.root / "quota"
        output.mkdir()
        receipt = _usage_limit_stop(
            output, self.project,
            observer=LangSmithObserver(enabled=True, client=client),
        )
        self.assertEqual(receipt["status"], "FAILED")
        self.assertEqual(receipt["route"], "END")
        self.assertEqual(receipt["recovery_attempts"], [])
        sol = next(row for action, row in client.rows if action == "update" and row["name"] == "Sol")
        metadata = sol["extra"]["metadata"]
        self.assertEqual(metadata["status"], "ERROR")
        self.assertEqual(metadata["error_type"], "BackendUsageLimitExceeded")
        self.assertEqual(sol["error"], "BackendUsageLimitExceeded")
        self.assertNotIn("scripted Codex usage limit", json.dumps(client.rows, default=str))

    def test_usage_requires_explicit_valid_codex_fields(self):
        self.assertEqual(_usage_metadata({"scripted": True, "model_calls": 0}), {})
        self.assertEqual(_usage_metadata({
            "input_tokens": 4, "output_tokens": 6, "total_tokens": "10",
            "cost_usd": float("nan"),
        }), {"input_tokens": 4, "output_tokens": 6})
        self.assertEqual(_usage_metadata({"input_tokens": True, "total_cost_usd": -1}), {})


if __name__ == "__main__":
    unittest.main()
