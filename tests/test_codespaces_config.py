"""Static safety and reproducibility checks for the Codespaces entry point."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CodespacesConfigTests(unittest.TestCase):
    def test_devcontainer_installs_the_existing_package_and_tools(self) -> None:
        config = json.loads(
            (ROOT / ".devcontainer/devcontainer.json").read_text(encoding="utf-8")
        )
        self.assertIn("3.11", config["image"])
        self.assertEqual(
            config["features"]["ghcr.io/devcontainers/features/node:2"]["version"],
            "22",
        )
        self.assertIn("ghcr.io/devcontainers/features/sshd:1", config["features"])
        self.assertEqual(config["postCreateCommand"], "bash .devcontainer/post-create.sh")
        self.assertIn(2024, config["forwardPorts"])
        self.assertNotIn("portsVisibility", config)

        setup = (ROOT / ".devcontainer/post-create.sh").read_text(encoding="utf-8")
        self.assertIn("python3.11 -m venv .venv", setup)
        self.assertIn("pip install -e '.[studio,shared]'", setup)
        self.assertIn("npm install -g @openai/codex@", setup)
        self.assertNotIn("codex login", setup)
        self.assertNotIn("LANGSMITH_API_KEY=", setup)

    def test_private_local_state_is_ignored(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for entry in (
            ".env", ".venv/", ".astra-orchestrator/",
            ".codex/auth.json", ".codex/sessions/",
        ):
            with self.subTest(entry=entry):
                self.assertIn(entry, ignore)


if __name__ == "__main__":
    unittest.main()
