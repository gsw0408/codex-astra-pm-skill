"""Two-host handoff proof with a private-store stand-in and no model calls."""

from __future__ import annotations

from copy import deepcopy
import io
from pathlib import Path
from unittest import mock
import sqlite3
import tempfile
import threading
import unittest
import zipfile

from astra_orchestrator.audit import read_json, write_json_exclusive
from astra_orchestrator.codex_cli import ScriptedBackend
from astra_orchestrator.dry_run import (
    _active_plan, _astra, _create_run, _end, _review, _review_package,
    _sol_result, _spec, _task,
)
from astra_orchestrator.graph import OrchestrationRuntime, initial_state
from astra_orchestrator.shared import SharedRunStore, SharedStateError, _archive, _extract


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakePostgres:
    def __init__(self):
        self.runs = {}
        self.sessions = {}

    def execute(self, query, parameters):
        if query.startswith("SELECT 1 FROM astra_shared_runs"):
            return FakeResult([(1,)] if parameters[0] in self.runs else [])
        if query.startswith("SELECT sha256 FROM astra_shared_runs"):
            run = self.runs.get(parameters[0])
            return FakeResult([(run[1],)] if run else [])
        if query.startswith("SELECT snapshot, sha256 FROM astra_shared_runs"):
            run = self.runs.get(parameters[0])
            return FakeResult([run] if run else [])
        if query.startswith("INSERT INTO astra_shared_runs"):
            self.runs[parameters[0]] = (parameters[1], parameters[2])
            return FakeResult([])
        if query.startswith("INSERT INTO astra_shared_sessions"):
            self.sessions[(parameters[0], parameters[1], parameters[2])] = (
                parameters[3], parameters[4]
            )
            return FakeResult([])
        if query.startswith("SELECT session_id, relative_path, content, sha256 "):
            return FakeResult([
                (session_id, relative, content, digest)
                for (run_id, session_id, relative), (content, digest) in self.sessions.items()
                if run_id == parameters[0]
            ])
        raise AssertionError("unexpected shared storage query")

    def close(self):
        pass


class MemoryStore:
    """The same snapshot boundary as PostgreSQL, held only inside a test."""

    def __init__(self, run_dir: Path, cell: dict[str, bytes]):
        self.run_dir = run_dir
        self.cell = cell
        self.sqlite_connection: sqlite3.Connection | None = None
        self.lock = threading.RLock()

    def save(self) -> None:
        with self.lock:
            self.cell["snapshot"] = _archive(self.run_dir, self.sqlite_connection)

    def fetch(self) -> None:
        _extract(self.cell["snapshot"], self.run_dir)

    def save_session(self, session_id: str) -> bool:
        return False


class SharedResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="shared-handoff-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.local = self.root / "local-project"
        self.remote = self.root / "codespace-project"
        self.local.mkdir()
        self.remote.mkdir()
        for project in (self.local, self.remote):
            (project / "evidence.txt").write_text("synthetic only\n", encoding="utf-8")
        self.cell: dict[str, bytes] = {}

    def _host(self, name: str, project: Path) -> tuple[Path, MemoryStore]:
        run_dir = self.root / name
        store = MemoryStore(run_dir, self.cell)
        if name != "first":
            store.fetch()
            roots = read_json(run_dir / "host-roots.json")["roots"]
            if str(project) not in roots:
                from astra_orchestrator.audit import replace_json
                replace_json(run_dir / "host-roots.json", {"roots": [*roots, str(project)]})
                store.save()
        return run_dir, store

    def _postgres_store(self, run_dir: Path, database: FakePostgres) -> SharedRunStore:
        store = SharedRunStore.__new__(SharedRunStore)
        store.connection = database
        store.run_id = "handoff-thread"
        store.run_dir = run_dir
        store.sqlite_connection = None
        store.lock = threading.RLock()
        store.expected_sha = None
        return store

    def test_local_codespace_local_preserves_thread_and_completed_calls(self) -> None:
        run_id = "handoff-thread"
        spec = _spec(self.local)
        plan = _active_plan(spec)
        task = _task("portable-task", "Produce only synthetic evidence.")
        remote_result = _sol_result(
            task, "MILESTONE_COMPLETE", "Synthetic evidence is ready.", self.remote / "evidence.txt"
        )
        local_result = deepcopy(remote_result)
        local_result["evidence_artifact_paths"][0]["path"] = str(self.local / "evidence.txt")
        proposal = "Pass only after independent inspection."
        local_package = _review_package(
            "portable-review", spec, plan, task, local_result, proposal,
            sol_attempts=1, task_attempts={task["task_id"]: 1},
        )
        remote_spec = {**spec, "project_root": str(self.remote)}
        remote_package = _review_package(
            "portable-review", remote_spec, plan, task, remote_result, proposal,
            sol_attempts=1, task_attempts={task["task_id"]: 1},
        )
        request_one = {
            "request_id": "human-one", "category": "AUTH_OR_HUMAN_ACTION_REQUIRED",
            "prompt": "Supply a synthetic manual confirmation.",
            "required_actions": ["Confirm the synthetic fixture."],
        }
        local_backend = ScriptedBackend({
            "astra": [
                _astra("USER", 1, user_request=request_one),
                _end(4, "portable-review"),
            ],
            "reviewer": [_review("portable-review", "PASS")],
        })
        remote_backend = ScriptedBackend({
            "astra": [
                _astra("SOL", 2, sol_task=task),
                _astra("REVIEW", 3, proposed_next_plan=proposal, review_package=remote_package),
            ],
            "sol": [remote_result],
        })

        class StopBeforeReview(OrchestrationRuntime):
            def _review_node(self, state):
                raise KeyboardInterrupt("simulated host stop before review")
        first, first_store = self._host("first", self.local)
        _create_run(first, spec, run_id)
        write_json_exclusive(first / "host-roots.json", {"roots": [str(self.local)]})
        first_store.save()
        with OrchestrationRuntime(first, local_backend, shared_store=first_store, project_root=self.local) as runtime:
            runtime.invoke(initial_state(spec, run_id))
            state_one = runtime.status()
            first_checkpoint = runtime.graph.get_state(runtime.config).config["configurable"]["checkpoint_id"]
        self.assertEqual(state_one["status"], "PAUSED_USER")
        self.assertEqual([call.role for call in local_backend.calls], ["astra"])

        second, second_store = self._host("second", self.remote)
        with StopBeforeReview(second, remote_backend, shared_store=second_store, project_root=self.remote) as runtime:
            self.assertEqual(runtime.run_id, run_id)
            self.assertEqual(runtime.status()["call_sequence"], 1)
            self.assertEqual(
                runtime.graph.get_state(runtime.config).config["configurable"]["checkpoint_id"],
                first_checkpoint,
            )
            with self.assertRaisesRegex(KeyboardInterrupt, "simulated host stop"):
                runtime.resume({
                    "request_id": "human-one", "status": "PROVIDED", "response": "confirmed",
                    "evidence_artifact_paths": [],
                })
            state_two = runtime.status()
            second_checkpoint = runtime.graph.get_state(runtime.config).config["configurable"]["checkpoint_id"]
        self.assertEqual([call.role for call in remote_backend.calls], ["astra", "sol", "astra"])
        self.assertEqual(state_two["call_sequence"], 4)

        third, third_store = self._host("third", self.local)
        with OrchestrationRuntime(third, local_backend, shared_store=third_store, project_root=self.local) as runtime:
            self.assertEqual(runtime.status()["call_sequence"], 4)
            self.assertEqual(
                runtime.graph.get_state(runtime.config).config["configurable"]["checkpoint_id"],
                second_checkpoint,
            )
            self.assertEqual(runtime._local_state(runtime.status())["sol_result"], local_result)
            self.assertEqual(runtime._local_state(runtime.status())["review_package"], local_package)
            runtime.continue_after_restart()
            final = runtime.status()
        self.assertEqual(final["run_id"], run_id)
        self.assertEqual(final["status"], "COMPLETED", final.get("last_error"))
        self.assertTrue(final["target_stage_completed"])
        self.assertEqual([call.role for call in local_backend.calls], ["astra", "reviewer", "astra"])
        self.assertEqual(len(final["review_session_ids"]), 1)
        self.assertEqual(final["transition_log"][-2]["reason"], "PASS")
        self.assertEqual(len(list((third / "calls").glob("*/receipt.json"))), 6)
        self.assertEqual(len(list((third / "reviews" / "portable-review").glob("execution-packet-*.json"))), 1)

    def test_snapshot_rejects_existing_cache_and_unsafe_paths(self) -> None:
        first = self.root / "first"
        first.mkdir()
        (first / "receipt.json").write_text("{}", encoding="utf-8")
        (first / ".env").write_text("synthetic password", encoding="utf-8")
        (first / "auth.json").write_text("synthetic credential", encoding="utf-8")
        snapshot = _archive(first)
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            self.assertEqual(archive.namelist(), ["receipt.json"])
        with self.assertRaisesRegex(SharedStateError, "new empty"):
            _extract(snapshot, first)

    def test_postgres_url_requires_direct_ssl_without_echoing_password(self) -> None:
        for url in (
            "postgresql://user:synthetic-password@host/db?sslmode=disable",
            "postgresql://user:synthetic-password@host-pooler/db?sslmode=require",
        ):
            with self.subTest(url=url):
                with self.assertRaises(SharedStateError) as raised:
                    SharedRunStore("thread", url, self.root / "cache")
                self.assertNotIn("synthetic-password", str(raised.exception))

    def test_windows_and_linux_project_paths_rebase_without_changing_goal(self) -> None:
        spec = _spec(self.local)
        run_dir = self.root / "path-run"
        _create_run(run_dir, spec, "path-run")
        write_json_exclusive(run_dir / "host-roots.json", {
            "roots": ["D:\\AI\\project", "/workspaces/project", str(self.local)]
        })
        store = MemoryStore(run_dir, {})
        with OrchestrationRuntime(run_dir, ScriptedBackend({}), shared_store=store, project_root=self.local) as runtime:
            transformed = runtime._local_state({
                "project_spec": {
                    "project_root": "/workspaces/project",
                    "ultimate_purpose": "Preserve this synthetic goal.",
                },
                "sol_result": {"evidence_artifact_paths": [
                    {"path": "D:\\AI\\project\\evidence.txt"},
                    {"path": "/workspaces/project/evidence.txt"},
                ]},
            })
        self.assertEqual(transformed["project_spec"]["project_root"], str(self.local))
        self.assertEqual(transformed["project_spec"]["ultimate_purpose"], "Preserve this synthetic goal.")
        self.assertEqual(
            [item["path"] for item in transformed["sol_result"]["evidence_artifact_paths"]],
            [str(self.local / "evidence.txt")] * 2,
        )

    def test_three_phase_shared_probe_uses_only_scripted_roles(self) -> None:
        from astra_orchestrator import shared_probe

        database = FakePostgres()

        class FakeStore(SharedRunStore):
            def __init__(self, run_id, url, run_dir):
                self.connection = database
                self.run_id = run_id
                self.run_dir = run_dir
                self.sqlite_connection = None
                self.lock = threading.RLock()
                self.expected_sha = None

        with mock.patch.object(shared_probe, "SharedRunStore", FakeStore):
            with mock.patch.object(shared_probe.tempfile, "gettempdir", return_value=str(self.root)):
                with mock.patch.dict("os.environ", {"ASTRA_STATE_DATABASE_URL": "synthetic-test-url"}):
                    first = shared_probe.run("start", "scripted-probe", self.root / "phase-one")
                    second = shared_probe.run("continue", "scripted-probe", self.root / "phase-two")
                    third = shared_probe.run("finish", "scripted-probe", self.root / "phase-three")
        self.assertEqual((first["status"], second["route"], third["status"]),
                         ("PAUSED_USER", "REVIEW", "COMPLETED"))
        self.assertEqual(third["completed_receipts"], 6)
        self.assertEqual([first["model_calls"], second["model_calls"], third["model_calls"]],
                         [0, 0, 0])
        self.assertNotEqual(first["checkpoint_id"], second["checkpoint_id"])
        self.assertNotEqual(second["checkpoint_id"], third["checkpoint_id"])

    def test_completed_sol_receipt_recovers_before_its_checkpoint(self) -> None:
        run_id = "receipt-before-checkpoint"
        spec = _spec(self.local)
        plan = _active_plan(spec)
        task = _task("receipt-task", "Produce synthetic evidence once.")
        result = _sol_result(task, "MILESTONE_COMPLETE", "Synthetic result.", self.local / "evidence.txt")
        proposal = "Request a fresh review."
        package = _review_package(
            "receipt-review", spec, plan, task, result, proposal,
            sol_attempts=1, task_attempts={task["task_id"]: 1},
        )

        class StopAfterReceipt(OrchestrationRuntime):
            def _invoke_role(self, state, role, prompt, schema_name, validator):
                answer = super()._invoke_role(state, role, prompt, schema_name, validator)
                if role == "sol":
                    raise KeyboardInterrupt("simulated stop after saved Sol receipt")
                return answer

        first_backend = ScriptedBackend({
            "astra": [_astra("SOL", 1, sol_task=task)],
            "sol": [result],
        })
        first, first_store = self._host("first", self.local)
        _create_run(first, spec, run_id)
        write_json_exclusive(first / "host-roots.json", {"roots": [str(self.local)]})
        first_store.save()
        with StopAfterReceipt(first, first_backend, shared_store=first_store, project_root=self.local) as runtime:
            with self.assertRaisesRegex(KeyboardInterrupt, "saved Sol receipt"):
                runtime.invoke(initial_state(spec, run_id))
        self.assertEqual(read_json(first / "calls" / "0002-sol" / "status.json")["status"], "SUCCEEDED")

        second_backend = ScriptedBackend({
            "astra": [
                _astra("REVIEW", 2, proposed_next_plan=proposal, review_package=package),
                _end(3, "receipt-review"),
            ],
            "reviewer": [_review("receipt-review", "PASS")],
        })
        second, second_store = self._host("second", self.local)
        with OrchestrationRuntime(second, second_backend, shared_store=second_store, project_root=self.local) as runtime:
            runtime.continue_after_restart()
            final = runtime.status()
        self.assertEqual(final["status"], "COMPLETED", final.get("last_error"))
        self.assertEqual([call.role for call in second_backend.calls], ["astra", "reviewer", "astra"])
        self.assertEqual(len(list((second / "calls").glob("*/receipt.json"))), 5)

    def test_stale_cache_cannot_overwrite_a_newer_shared_checkpoint(self) -> None:
        database = FakePostgres()
        first = self.root / "first"
        first.mkdir()
        (first / "receipt.json").write_text("{}", encoding="utf-8")
        old_store = self._postgres_store(first, database)
        old_store.save()
        second = self.root / "second"
        new_store = self._postgres_store(second, database)
        new_store.fetch()
        (second / "next.json").write_text("{}", encoding="utf-8")
        new_store.save()
        with self.assertRaisesRegex(SharedStateError, "changed on another host"):
            old_store.repair_from_local()

    def test_only_named_codex_rollout_moves_and_divergence_fails_closed(self) -> None:
        database = FakePostgres()
        store = self._postgres_store(self.root / "cache", database)
        session_id = "019e0ccb-c6ac-7e60-a6b8-97580fd7002f"
        relative = Path("2026") / "09" / "23" / f"rollout-2026-09-23T10-00-00-{session_id}.jsonl"
        source_home = self.root / "source-codex"
        source = source_home / "sessions" / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"first\nsecond\n")
        (source_home / "auth.json").write_text("synthetic secret", encoding="utf-8")
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(source_home)}):
            self.assertTrue(store.save_session(session_id))
        target_home = self.root / "target-codex"
        target = target_home / "sessions" / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(b"first\n")
        with mock.patch.dict("os.environ", {"CODEX_HOME": str(target_home)}):
            store.restore_sessions()
            store.ensure_session(session_id)
            self.assertEqual(target.read_bytes(), b"first\nsecond\n")
            self.assertFalse((target_home / "auth.json").exists())
            target.write_bytes(b"unrelated\n")
            with self.assertRaisesRegex(SharedStateError, "conflicts"):
                store.restore_sessions()


if __name__ == "__main__":
    unittest.main()
