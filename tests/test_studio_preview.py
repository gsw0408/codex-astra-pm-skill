"""The Studio preview must mirror routing without running the controller."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from astra_orchestrator.codex_cli import ScriptedBackend
from astra_orchestrator.dry_run import _create_run, _spec
from astra_orchestrator.graph import OrchestrationRuntime
from astra_orchestrator.studio import graph


class StudioPreviewTests(unittest.TestCase):
    def test_config_points_to_a_non_executable_preview_and_local_env(self) -> None:
        config = json.loads(
            (Path(__file__).resolve().parents[1] / "langgraph.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            config["graphs"]["astra_orchestrator"],
            "./astra_orchestrator/studio.py:graph",
        )
        self.assertEqual(config["env"], ".env")
        example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("LANGSMITH_TRACING=false", example)

    def test_preview_has_production_routes_and_cannot_dispatch_roles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="astra-studio-preview-") as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            spec = _spec(project)
            run_dir = root / "run"
            _create_run(run_dir, spec, "studio-route-check")
            backend = ScriptedBackend({})
            with OrchestrationRuntime(run_dir, backend) as runtime:
                production = runtime.graph.get_graph().to_json()

            preview = graph.get_graph().to_json()
            self.assertEqual(
                {node["id"] for node in preview["nodes"]},
                {node["id"] for node in production["nodes"]},
            )
            self.assertEqual(preview["edges"], production["edges"])
            with self.assertRaisesRegex(RuntimeError, "displays the route topology only"):
                graph.invoke({})
            self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
