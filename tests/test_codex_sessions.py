"""Mocked Codex CLI session checks; this suite never invokes a model."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from astra_orchestrator.audit import AuditLog
from astra_orchestrator import cli as orchestration_cli
from astra_orchestrator import codex_cli
from astra_orchestrator.codex_cli import (
    BackendError,
    BackendResult,
    BackendUsageLimitExceeded,
    CodexCliBackend,
    FIXED_ROLE_SETTINGS,
    LEGACY_ROLE_SETTINGS,
    ScriptedBackend,
)
from astra_orchestrator.graph import OrchestrationRuntime
from astra_orchestrator.schema import SCHEMA_VERSION


SCHEMA = {"type": "object", "additionalProperties": True}


def events(session_id: str, output: dict | None = None, *, malformed: bool = False,
           final_text: str | None = None) -> str:
    rows = [
        {"type": "thread.started", "thread_id": session_id},
        {"type": "item.completed", "item": {
            "type": "agent_message",
            "text": final_text if final_text is not None else json.dumps(output or {"ok": True}),
        }},
        {"type": "turn.completed", "usage": {
            "input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 5,
        }},
    ]
    text = "".join(json.dumps(row) + "\n" for row in rows)
    return text + ("not-json\n" if malformed else "")


class FakeProcess:
    next_pid = 42000

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0,
                 *, times_out: bool = False):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.stdout_value = stdout
        self.stderr_value = stderr
        self.final_returncode = returncode
        self.returncode = None
        self.times_out = times_out
        self.communicate_count = 0
        self.kill = mock.Mock(side_effect=self._kill)
        self.wait = mock.Mock(side_effect=self._wait)
        self.poll = mock.Mock(side_effect=lambda: self.returncode)

    def communicate(self, input=None, timeout=None):
        self.communicate_count += 1
        if self.times_out and self.communicate_count == 1:
            raise subprocess.TimeoutExpired("mock-codex", timeout, output=self.stdout_value,
                                            stderr=self.stderr_value)
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.stdout_value, self.stderr_value

    def _kill(self):
        self.returncode = -9

    def _wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self.final_returncode
        return self.returncode


class CodexSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-session-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.backend = CodexCliBackend(
            self.workspace, codex_executable="codex-not-executed",
            timeout_seconds=0.1, cleanup_timeout_seconds=0.1,
        )

    def receipt(self, name: str) -> Path:
        path = self.root / "receipts" / name
        path.mkdir(parents=True)
        # These are graph-owned ledger files; the backend must preserve them.
        (path / "request.json").write_text("{}\n", encoding="utf-8")
        (path / "output-schema.json").write_text("{}\n", encoding="utf-8")
        (path / "status.json").write_text("{}\n", encoding="utf-8")
        return path

    def assert_role_settings(self, command: list[str], role: str) -> None:
        settings = FIXED_ROLE_SETTINGS[role]
        self.assertEqual(command[command.index("--model") + 1], settings["model"])
        self.assertIn(
            f'model_reasoning_effort="{settings["reasoning_effort"]}"', command,
        )

    def test_astra_and_sol_new_and_exact_resume_commands(self):
        processes = [
            FakeProcess(events("astra-new")),
            FakeProcess(events("astra-session")),
            FakeProcess(events("sol-new")),
            FakeProcess(events("sol-session")),
        ]
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=processes) as popen:
            astra_new = self.backend.run_astra("manage", SCHEMA, self.receipt("astra-new"))
            astra_resume = self.backend.run_astra(
                "continue", SCHEMA, self.receipt("astra-resume"), session_id="astra-session",
            )
            sol_new = self.backend.run_sol("implement", SCHEMA, self.receipt("sol-new"))
            sol_resume = self.backend.run_sol(
                "continue", SCHEMA, self.receipt("sol-resume"), session_id="sol-session",
            )

        calls = [call.args[0] for call in popen.call_args_list]
        self.assertEqual([astra_new.mode, astra_resume.mode, sol_new.mode, sol_resume.mode],
                         ["new", "resume", "new", "resume"])
        self.assertNotIn("resume", calls[0])
        self.assertEqual(calls[0][calls[0].index("--sandbox") + 1], "read-only")
        self.assertIn("resume", calls[1])
        self.assertIn("astra-session", calls[1])
        self.assertIn('sandbox_mode="read-only"', calls[1])
        self.assertNotIn("resume", calls[2])
        self.assertEqual(calls[2][calls[2].index("--sandbox") + 1], "workspace-write")
        self.assertIn("resume", calls[3])
        self.assertIn("sol-session", calls[3])
        self.assertIn('sandbox_mode="workspace-write"', calls[3])
        for command, role in zip(calls, ("astra", "astra", "sol", "sol")):
            self.assertNotIn("--last", command)
            self.assertNotIn("fork", command)
            self.assert_role_settings(command, role)
        for call in popen.call_args_list:
            self.assertEqual(Path(call.kwargs["cwd"]), self.workspace)

    def test_reviewer_is_always_fresh_ephemeral_isolated_and_unique(self):
        processes = [
            FakeProcess(events("review-1", {"verdict": "PASS"})),
            FakeProcess(events("review-2", {"verdict": "PASS"})),
            FakeProcess(events("review-1", {"verdict": "PASS"})),
        ]
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=processes) as popen:
            first = self.backend.run_reviewer("packet one", SCHEMA, self.receipt("review-1"))
            second = self.backend.run_reviewer(
                "packet two", SCHEMA, self.receipt("review-2"),
                used_session_ids={first.session_id},
            )
            with self.assertRaisesRegex(BackendError, "reused"):
                self.backend.run_reviewer(
                    "packet three", SCHEMA, self.receipt("review-duplicate"),
                    used_session_ids={first.session_id, second.session_id},
                )

        self.assertNotEqual(first.session_id, second.session_id)
        reviewer_prefix = [
            "codex-not-executed", "exec", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--skip-git-repo-check", "--sandbox", "read-only",
            "--cd", str(self.workspace),
        ]
        working_directories = []
        for call in popen.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:len(reviewer_prefix)], reviewer_prefix)
            self.assertNotIn("resume", command)
            self.assertNotIn("fork", command)
            self.assertNotIn("--last", command)
            self.assertNotIn("--search", command)
            self.assert_role_settings(command, "reviewer")
            working_directories.append(Path(call.kwargs["cwd"]))
        self.assertEqual(len(set(working_directories)), 3)
        self.assertTrue(all(path != self.workspace for path in working_directories))
        self.assertTrue(all(not path.exists() for path in working_directories))

    def test_luna_is_fresh_search_enabled_isolated_and_shares_uniqueness_guard(self):
        processes = [
            FakeProcess(events("luna-1", {"summary": "one"})),
            FakeProcess(events("luna-2", {"summary": "two"})),
            FakeProcess(events("external-session", {"summary": "duplicate"})),
            FakeProcess(events("luna-1", {"verdict": "PASS"})),
        ]
        first_receipt = self.receipt("luna-1")
        with mock.patch.object(codex_cli.subprocess, "Popen", side_effect=processes) as popen:
            first = self.backend.run_luna("research one", SCHEMA, first_receipt)
            second = self.backend.run_luna(
                "research two", SCHEMA, self.receipt("luna-2"),
                used_session_ids={first.session_id},
            )
            with self.assertRaisesRegex(BackendError, "luna session was reused"):
                self.backend.run_luna(
                    "research duplicate", SCHEMA, self.receipt("luna-supplied-duplicate"),
                    used_session_ids={"external-session"},
                )
            with self.assertRaisesRegex(BackendError, "reviewer session was reused"):
                self.backend.run_reviewer(
                    "review duplicate", SCHEMA, self.receipt("review-cross-role-duplicate"),
                )

        self.assertNotEqual(first.session_id, second.session_id)
        luna_prefix = [
            "codex-not-executed", "--search", "exec", "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--sandbox", "read-only", "--cd", str(self.workspace),
        ]
        working_directories = []
        for call in popen.call_args_list[:3]:
            command = call.args[0]
            self.assertEqual(command[:len(luna_prefix)], luna_prefix)
            self.assertNotIn("resume", command)
            self.assertNotIn("fork", command)
            self.assertNotIn("--last", command)
            self.assert_role_settings(command, "luna")
            working_directories.append(Path(call.kwargs["cwd"]))
        reviewer_command = popen.call_args_list[3].args[0]
        self.assertNotIn("--search", reviewer_command)
        self.assert_role_settings(reviewer_command, "reviewer")
        working_directories.append(Path(popen.call_args_list[3].kwargs["cwd"]))
        self.assertEqual(len(set(working_directories)), 4)
        self.assertTrue(all(path != self.workspace for path in working_directories))
        self.assertTrue(all(not path.exists() for path in working_directories))
        launch = json.loads((first_receipt / "launch.json").read_text())
        self.assertTrue(launch["fresh_isolated_cwd"])
        self.assertTrue(launch["luna_isolated_cwd"])
        self.assertTrue(launch["live_web_search"])
        self.assertEqual(launch["codex_working_root"], str(self.workspace))
        self.assertEqual(launch["project_access"], "read-only")

    def test_parses_output_usage_and_writes_nonconflicting_receipts(self):
        receipt = self.receipt("parsed")
        process = FakeProcess(events("session-1", {"route": "END"}), "warning")
        with mock.patch.object(codex_cli.subprocess, "Popen", return_value=process):
            result = self.backend.run_astra("manage", SCHEMA, receipt)

        self.assertEqual(result.output, {"route": "END"})
        self.assertEqual(result.usage["input_tokens"], 11)
        self.assertEqual(json.loads((receipt / "final.json").read_text()), result.output)
        self.assertEqual((receipt / "stderr.txt").read_text(), "warning")
        launch = json.loads((receipt / "launch.json").read_text())
        outcome = json.loads((receipt / "process.json").read_text())
        self.assertEqual(launch["prompt_transport"], "stdin")
        self.assertNotIn("manage", " ".join(launch["command"]))
        self.assertEqual(outcome["session_id"], "session-1")
        for graph_file in ("request.json", "output-schema.json", "status.json"):
            self.assertEqual((receipt / graph_file).read_text(), "{}\n")

    def test_nonzero_malformed_jsonl_missing_session_and_bad_final_fail_closed(self):
        cases = (
            ("nonzero", FakeProcess(events("s"), returncode=7), "exited with 7"),
            ("malformed", FakeProcess(events("s", malformed=True)), "malformed events"),
            ("missing-session", FakeProcess(json.dumps({"type": "turn.completed"}) + "\n"),
             "thread.started"),
            ("bad-final", FakeProcess(events("s", final_text="not-json")),
             "final Codex message is not JSON"),
        )
        for name, process, message in cases:
            with self.subTest(name=name), \
                    mock.patch.object(codex_cli.subprocess, "Popen", return_value=process):
                receipt = self.receipt(name)
                with self.assertRaisesRegex(BackendError, message):
                    self.backend.run_astra("manage", SCHEMA, receipt)
                self.assertTrue((receipt / "events.jsonl").exists())
                self.assertTrue((receipt / "process.json").exists())

    def test_terminal_stdout_usage_limit_signals_are_classified_and_stop_once(self):
        cases = {
            "snake-case": {
                "type": "error",
                "error": {"codex_error_info": "usage_limit_exceeded"},
            },
            "nested-camel-case": {
                "type": "turn.failed",
                "error": {
                    "details": {"codexErrorInfo": "UsageLimitExceeded"},
                },
            },
        }
        for name, event in cases.items():
            with self.subTest(name=name):
                process = FakeProcess(json.dumps(event) + "\n", returncode=1)
                receipt = self.receipt(f"usage-limit-stdout-{name}")
                with mock.patch.object(
                    codex_cli.subprocess, "Popen", return_value=process,
                ) as popen:
                    with self.assertRaises(BackendUsageLimitExceeded) as caught:
                        self.backend.run_astra("manage", SCHEMA, receipt)

                self.assertEqual(popen.call_count, 1)
                self.assertEqual(caught.exception.failure_kind, "USAGE_LIMIT_EXCEEDED")
                self.assertEqual(
                    caught.exception.codex_error_info, "usage_limit_exceeded",
                )
                outcome = json.loads((receipt / "process.json").read_text())
                self.assertEqual(
                    outcome["backend_failure_kind"], "USAGE_LIMIT_EXCEEDED",
                )
                self.assertEqual(
                    outcome["codex_error_info"], "usage_limit_exceeded",
                )

    def test_json_and_plain_stderr_usage_limit_signals_stop_once(self):
        cases = {
            "json": json.dumps({
                "type": "turn.failed",
                "error": {"message": "You've hit your usage limit."},
            }) + "\n",
            "plain": "You've hit your usage limit.\n",
        }
        for name, stderr in cases.items():
            with self.subTest(name=name):
                process = FakeProcess("", stderr, returncode=1)
                receipt = self.receipt(f"usage-limit-stderr-{name}")
                with mock.patch.object(
                    codex_cli.subprocess, "Popen", return_value=process,
                ) as popen:
                    with self.assertRaises(BackendUsageLimitExceeded):
                        self.backend.run_sol("work", SCHEMA, receipt)

                self.assertEqual(popen.call_count, 1)
                outcome = json.loads((receipt / "process.json").read_text())
                self.assertEqual(
                    outcome["backend_failure_kind"], "USAGE_LIMIT_EXCEEDED",
                )
                self.assertEqual(
                    outcome["codex_error_info"], "usage_limit_exceeded",
                )

    def test_non_usage_quota_signals_remain_ordinary_backend_errors(self):
        def rows(*values):
            return "".join(json.dumps(value) + "\n" for value in values)

        cases = {
            "agent-message-mention": events(
                "agent-message-session", {"note": "usage_limit_exceeded"},
            ),
            "completed-turn-usage": rows(
                {"type": "thread.started", "thread_id": "usage-session"},
                {
                    "type": "turn.completed",
                    "usage": {"codex_error_info": "usage_limit_exceeded"},
                },
            ),
            "rate-limit": rows({
                "type": "error",
                "code": "rate_limit_exceeded",
                "message": "HTTP 429; retry later",
            }),
            "generic-429": rows({
                "type": "turn.failed",
                "error": {"message": "HTTP 429 Too Many Requests; retry later"},
            }),
            "context-window": rows({
                "type": "turn.failed",
                "error": {"codexErrorInfo": "ContextWindowExceeded"},
            }),
            "session-budget": rows({
                "type": "error",
                "code": "session_budget_exceeded",
            }),
        }
        for name, stdout in cases.items():
            with self.subTest(name=name):
                process = FakeProcess(stdout, returncode=9)
                receipt = self.receipt(f"not-usage-limit-{name}")
                with mock.patch.object(
                    codex_cli.subprocess, "Popen", return_value=process,
                ) as popen:
                    with self.assertRaises(BackendError) as caught:
                        self.backend.run_astra("manage", SCHEMA, receipt)

                self.assertEqual(type(caught.exception), BackendError)
                self.assertEqual(popen.call_count, 1)
                outcome = json.loads((receipt / "process.json").read_text())
                self.assertIsNone(outcome["backend_failure_kind"])
                self.assertIsNone(outcome["codex_error_info"])

    def test_timeout_kills_child_and_records_failure(self):
        process = FakeProcess(events("slow"), times_out=True)
        with mock.patch.object(codex_cli.subprocess, "Popen", return_value=process), \
                mock.patch.object(codex_cli.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="terminated", stderr="",
                )):
            receipt = self.receipt("timeout")
            with self.assertRaisesRegex(BackendError, "timed out"):
                self.backend.run_sol("work", SCHEMA, receipt)
        process.kill.assert_called()
        process.wait.assert_called()
        outcome = json.loads((receipt / "process.json").read_text())
        self.assertTrue(outcome["timed_out"])
        self.assertTrue(outcome["cleanup"]["attempted"])

    def test_windows_cleanup_uses_taskkill_for_the_process_tree(self):
        process = FakeProcess()
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(codex_cli.os, "name", "nt"), \
                mock.patch.object(codex_cli.subprocess, "run", return_value=completed) as run:
            evidence = self.backend._cleanup(process)
        run.assert_called_once_with(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, text=True, timeout=0.1, check=False,
        )
        self.assertEqual(evidence["taskkill_returncode"], 0)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=0.1)

    def test_cli_uses_new_fixed_settings_and_preserves_legacy_resume(self):
        args = SimpleNamespace(codex="fixed-codex", timeout=123.0)
        config = orchestration_cli._backend_config(args)
        expected = {
            "astra": {
                "title": "Project Manager", "model": "gpt-6-astra",
                "reasoning_effort": "high",
            },
            "sol": {
                "title": "Implementation Worker", "model": "gpt-6-sol",
                "reasoning_effort": "high",
            },
            "reviewer": {
                "title": "Independent Reviewer", "model": "gpt-6-sol",
                "reasoning_effort": "high",
            },
            "luna": {
                "title": "Research and Information-Gathering Specialist",
                "model": "gpt-6-luna", "reasoning_effort": "xhigh",
            },
        }
        self.assertEqual(config["role_settings"], expected)
        manifest = {"project_root": str(self.workspace), "backend": config}
        resumed = orchestration_cli._backend_from_manifest(manifest)
        self.assertEqual(resumed.models, {
            role: settings["model"] for role, settings in expected.items()
        })
        self.assertEqual(resumed.efforts, {
            role: settings["reasoning_effort"] for role, settings in expected.items()
        })

        legacy = deepcopy(manifest)
        legacy["backend"]["role_settings"] = {
            role: dict(settings) for role, settings in LEGACY_ROLE_SETTINGS.items()
        }
        old_run = orchestration_cli._backend_from_manifest(legacy)
        for role, settings in LEGACY_ROLE_SETTINGS.items():
            command, mode = old_run._command(
                role, self.workspace / "schema.json", self.workspace / "final.json",
                "existing-session" if role in {"astra", "sol"} else None,
            )
            self.assertEqual(command[command.index("--model") + 1], settings["model"])
            self.assertIn(
                f'model_reasoning_effort="{settings["reasoning_effort"]}"', command,
            )
            self.assertEqual(mode, "resume" if role in {"astra", "sol"} else "new")
        legacy_run_dir = self.root / "legacy-run"
        AuditLog.create(legacy_run_dir, {
            "schema_version": SCHEMA_VERSION,
            "run_id": "legacy-run",
            "project_root": str(self.workspace),
            "backend": legacy["backend"],
        })
        with OrchestrationRuntime(legacy_run_dir, old_run) as runtime:
            self.assertEqual(runtime.observer.role_settings["sol"], LEGACY_ROLE_SETTINGS["sol"])
            self.assertEqual(runtime.observer.role_settings["luna"], LEGACY_ROLE_SETTINGS["luna"])

        changed = deepcopy(manifest)
        changed["backend"]["role_settings"]["reviewer"]["model"] = "gpt-6-astra"
        with self.assertRaisesRegex(ValueError, "supported fixed orchestration policy"):
            orchestration_cli._backend_from_manifest(changed)
        missing = deepcopy(manifest)
        del missing["backend"]["role_settings"]["luna"]
        with self.assertRaisesRegex(ValueError, "supported fixed orchestration policy"):
            orchestration_cli._backend_from_manifest(missing)
        with self.assertRaisesRegex(ValueError, "supported fixed orchestration policy"):
            CodexCliBackend(self.workspace, role_settings=changed["backend"]["role_settings"])

        parsed = orchestration_cli.build_parser().parse_args([
            "start", "--spec", "spec.json", "--project-root", ".",
            "--run-dir", "run",
        ])
        for removed in (
            "astra_model", "sol_model", "reviewer_model", "luna_model",
            "astra_effort", "sol_effort", "reviewer_effort", "luna_effort",
        ):
            self.assertFalse(hasattr(parsed, removed))
        with self.assertRaises(TypeError):
            FIXED_ROLE_SETTINGS["sol"]["model"] = "mutable"  # type: ignore[index]
        self.assertEqual(orchestration_cli._workflow_exit_code({"status": "COMPLETED"}), 0)
        self.assertEqual(orchestration_cli._workflow_exit_code({"status": "PAUSED_USER"}), 0)
        self.assertEqual(orchestration_cli._workflow_exit_code({"status": "FAILED"}), 1)
        self.assertEqual(orchestration_cli._workflow_exit_code({"status": "ABORTED"}), 2)

    def test_scripted_backend_records_new_resume_modes_and_rejects_reviewer_reuse(self):
        duplicate = BackendResult(
            role="reviewer", session_id="scripted-reviewer-fixed", mode="new",
            output={"verdict": "PASS"},
        )
        luna = BackendResult(
            role="luna", session_id="scripted-luna-fixed", mode="new",
            output={"summary": "research"},
        )
        cross_role_duplicate = BackendResult(
            role="luna", session_id="scripted-reviewer-fixed", mode="new",
            output={"summary": "duplicate"},
        )
        backend = ScriptedBackend({
            "astra": [{"route": "SOL"}, {"route": "END"}],
            "sol": [{"status": "DONE"}],
            "reviewer": [duplicate, duplicate],
            "luna": [luna, cross_role_duplicate],
        })
        backend.run_astra("a1", SCHEMA, self.receipt("script-a1"))
        resumed = backend.run_astra(
            "a2", SCHEMA, self.receipt("script-a2"), session_id="astra-script-session",
        )
        sol = backend.run_sol("s1", SCHEMA, self.receipt("script-sol"))
        reviewer = backend.run_reviewer("r1", SCHEMA, self.receipt("script-r1"))
        with self.assertRaisesRegex(BackendError, "reused"):
            backend.run_reviewer(
                "r2", SCHEMA, self.receipt("script-r2"),
                used_session_ids={reviewer.session_id},
            )
        luna_result = backend.run_luna("l1", SCHEMA, self.receipt("script-l1"))
        with self.assertRaisesRegex(BackendError, "reused"):
            backend.run_luna("l2", SCHEMA, self.receipt("script-l2"))

        self.assertEqual(resumed.session_id, "astra-script-session")
        self.assertEqual(luna_result.session_id, "scripted-luna-fixed")
        self.assertEqual([call.mode for call in backend.calls],
                         ["new", "resume", "new", "new", "new", "new", "new"])
        self.assertEqual(sol.mode, "new")
        self.assertEqual(len({call.receipt_dir for call in backend.calls}), 7)


if __name__ == "__main__":
    unittest.main()
