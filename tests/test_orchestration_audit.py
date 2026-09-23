import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astra_orchestrator.audit import (
    AmbiguousCallError,
    AuditLog,
    canonical_json,
    read_json,
    replace_json,
    sha256_bytes,
    sha256_file,
    write_json_exclusive,
)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ExclusiveArtifactTests(unittest.TestCase):
    def test_write_json_exclusive_is_canonical_hashed_and_immutable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "nested" / "artifact.json"
            value = {"z": [2, 1], "a": "evidence"}

            digest = write_json_exclusive(path, value)

            expected = canonical_json(value)
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(digest, sha256_bytes(expected))
            self.assertEqual(digest, sha256_file(path))

            with self.assertRaises(FileExistsError):
                write_json_exclusive(path, {"replacement": True})
            self.assertEqual(path.read_bytes(), expected)


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.run_dir = Path(self.temporary_directory.name) / "run"
        self.audit = AuditLog.create(
            self.run_dir,
            {"schema_version": 1, "run_id": "run-1"},
        )

    def prepare_running_call(self, call_id):
        directory = self.audit.prepare_call(
            call_id,
            "SOL",
            {"task_id": "task-1"},
            {"type": "object", "additionalProperties": False},
        )
        self.audit.mark_call_running(call_id)
        return directory

    def finish_call(self, call_id):
        directory = self.prepare_running_call(call_id)
        result = {"task_id": "task-1", "status": "DONE"}
        receipt = self.audit.finish_call(
            call_id,
            result,
            {"session_id": "session-1", "exit_code": 0},
        )
        return directory, result, receipt

    def test_successful_call_is_recovered_from_hash_linked_artifacts(self):
        directory, result, receipt = self.finish_call("call-success")

        recovered = self.audit.recover_call("call-success")

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.result, result)
        self.assertEqual(recovered.receipt, receipt)
        self.assertEqual(
            recovered.receipt["result_sha256"],
            sha256_file(directory / "result.json"),
        )
        self.assertEqual(read_json(directory / "status.json")["status"], "SUCCEEDED")
        self.assertIsNone(self.audit.recover_call("call-not-created"))

    def test_running_and_failed_calls_are_ambiguous_to_recovery(self):
        self.prepare_running_call("call-running")
        with self.assertRaisesRegex(AmbiguousCallError, "RUNNING"):
            self.audit.recover_call("call-running")

        self.prepare_running_call("call-failed")
        self.audit.fail_call(
            "call-failed",
            {"error": "bounded subprocess failed"},
            ambiguous=False,
        )
        with self.assertRaisesRegex(AmbiguousCallError, "FAILED"):
            self.audit.recover_call("call-failed")

    def test_missing_status_or_final_receipt_is_ambiguous(self):
        self.audit.call_dir("call-no-status").mkdir()
        with self.assertRaisesRegex(AmbiguousCallError, "no status"):
            self.audit.recover_call("call-no-status")

        directory, _, _ = self.finish_call("call-no-receipt")
        (directory / "receipt.json").unlink()
        with self.assertRaisesRegex(AmbiguousCallError, "final artifacts are missing"):
            self.audit.recover_call("call-no-receipt")

    def test_mismatched_receipt_hash_is_ambiguous(self):
        directory, _, _ = self.finish_call("call-tampered")
        receipt_path = directory / "receipt.json"
        receipt = read_json(receipt_path)
        receipt["result_sha256"] = "0" * 64
        replace_json(receipt_path, receipt)

        with self.assertRaisesRegex(AmbiguousCallError, "does not match"):
            self.audit.recover_call("call-tampered")

    def test_recovery_rejects_every_tampered_hash_link(self):
        cases = (
            ("request", "request.json", lambda value: {**value, "task_id": "changed"}),
            (
                "output schema",
                "output-schema.json",
                lambda value: {**value, "additionalProperties": True},
            ),
            ("result", "result.json", lambda value: {**value, "status": "BLOCKED"}),
            ("receipt", "receipt.json", lambda value: {**value, "session_id": "changed"}),
        )
        for index, (label, filename, mutate) in enumerate(cases):
            with self.subTest(label=label):
                call_id = f"call-tampered-{index}"
                directory, _, _ = self.finish_call(call_id)
                path = directory / filename
                replace_json(path, mutate(read_json(path)))
                with self.assertRaisesRegex(AmbiguousCallError, label):
                    self.audit.recover_call(call_id)

        directory, _, _ = self.finish_call("call-tampered-result-status")
        status_path = directory / "status.json"
        status = read_json(status_path)
        status["result_sha256"] = "0" * 64
        replace_json(status_path, status)
        with self.assertRaisesRegex(AmbiguousCallError, "result"):
            self.audit.recover_call("call-tampered-result-status")

    def test_unreadable_hash_link_is_reported_as_ambiguous(self):
        self.finish_call("call-unreadable")

        with mock.patch(
            "astra_orchestrator.audit.sha256_file",
            side_effect=OSError("synthetic read failure"),
        ):
            with self.assertRaisesRegex(AmbiguousCallError, "unreadable"):
                self.audit.recover_call("call-unreadable")

    def test_transition_ids_are_append_only_and_deduplicated(self):
        self.audit.append_transition(
            {"transition_id": "transition-1", "from": "ASTRA", "to": "SOL"}
        )
        self.audit.append_transition(
            {"transition_id": "transition-1", "from": "ASTRA", "to": "REVIEW"}
        )
        self.audit.append_transition(
            {"transition_id": "transition-2", "from": "SOL", "to": "ASTRA"}
        )

        rows = read_jsonl(self.run_dir / "transitions.jsonl")

        self.assertEqual(
            [row["transition_id"] for row in rows],
            ["transition-1", "transition-2"],
        )
        self.assertEqual(rows[0]["to"], "SOL")

    def test_keyed_jsonl_is_checkpoint_replay_idempotent_and_conflict_safe(self):
        attempt = {
            "issue_id": "issue-1",
            "strategy_id": "strategy-1",
            "attempt": 1,
            "outcome": "The distinct strategy failed.",
        }
        self.audit.append_jsonl_once(
            "recovery-attempts.jsonl",
            attempt,
            identity_fields=("issue_id", "strategy_id"),
        )
        self.audit.append_jsonl_once(
            "recovery-attempts.jsonl",
            dict(attempt),
            identity_fields=("issue_id", "strategy_id"),
        )
        self.assertEqual(len(read_jsonl(self.run_dir / "recovery-attempts.jsonl")), 1)

        with self.assertRaisesRegex(AmbiguousCallError, "conflicting keyed evidence"):
            self.audit.append_jsonl_once(
                "recovery-attempts.jsonl",
                {**attempt, "outcome": "Conflicting replay."},
                identity_fields=("issue_id", "strategy_id"),
            )

    def test_review_packet_exact_reuse_is_idempotent_and_changes_are_rejected(self):
        packet = {
            "review_id": "review-1",
            "stage_id": "stage-1",
            "evidence_artifact_paths": [{"path": "evidence/check.txt"}],
        }

        path, digest = self.audit.write_review_packet("review-1", packet)
        original_packet = path.read_bytes()
        original_receipt = (path.parent / "packet-receipt.json").read_bytes()
        events_before_reuse = read_jsonl(self.run_dir / "events.jsonl")

        reused_path, reused_digest = self.audit.write_review_packet(
            "review-1",
            {
                "evidence_artifact_paths": [{"path": "evidence/check.txt"}],
                "stage_id": "stage-1",
                "review_id": "review-1",
            },
        )

        self.assertEqual(reused_path, path)
        self.assertEqual(reused_digest, digest)
        self.assertEqual(digest, sha256_file(path))
        self.assertEqual(read_json(path.parent / "packet-receipt.json")["packet_sha256"], digest)
        self.assertEqual(
            read_jsonl(self.run_dir / "events.jsonl"),
            events_before_reuse,
        )

        with self.assertRaisesRegex(AmbiguousCallError, "different or incomplete"):
            self.audit.write_review_packet(
                "review-1",
                {**packet, "stage_id": "changed-stage"},
            )

        self.assertEqual(path.read_bytes(), original_packet)
        self.assertEqual(
            (path.parent / "packet-receipt.json").read_bytes(),
            original_receipt,
        )

    def test_incomplete_review_packet_directory_is_ambiguous(self):
        (self.audit.reviews_dir / "review-incomplete").mkdir()

        with self.assertRaisesRegex(AmbiguousCallError, "different or incomplete"):
            self.audit.write_review_packet(
                "review-incomplete",
                {"review_id": "review-incomplete"},
            )

        packet = {"review_id": "review-missing-receipt", "stage_id": "stage-1"}
        path, _ = self.audit.write_review_packet("review-missing-receipt", packet)
        (path.parent / "packet-receipt.json").unlink()
        with self.assertRaisesRegex(AmbiguousCallError, "missing its hash receipt"):
            self.audit.write_review_packet("review-missing-receipt", packet)

    def test_tampered_review_packet_receipt_is_ambiguous(self):
        packet = {"review_id": "review-tampered-receipt", "stage_id": "stage-1"}
        path, _ = self.audit.write_review_packet("review-tampered-receipt", packet)
        replace_json(
            path.parent / "packet-receipt.json",
            {
                "schema_version": 1,
                "review_id": "review-tampered-receipt",
                "packet_sha256": "0" * 64,
            },
        )
        with self.assertRaisesRegex(AmbiguousCallError, "does not match"):
            self.audit.write_review_packet("review-tampered-receipt", packet)

    def test_review_id_cannot_escape_the_review_directory(self):
        outside = self.run_dir.parent / "outside"
        with self.assertRaisesRegex(ValueError, "review_id"):
            self.audit.write_review_packet("../../outside", {"review_id": "../../outside"})
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
