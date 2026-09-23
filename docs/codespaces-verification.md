# Codespaces compatibility verification receipt

Recorded: 2026-09-23 UTC. Scope: the orchestration package and its Linux
development environment. No scientific training, evaluation, dataset work,
real Codex role call, or approved-evidence change was performed.

| Check | Observed result |
| --- | --- |
| New role policy | Astra `gpt-6-astra`/`high`; Sol and Reviewer `gpt-6-sol`/`high`; Luna `gpt-6-luna`/`xhigh` in the scripted receipt. |
| Prior run compatibility | A preserved schema-v2 GPT-5.6 manifest selects its recorded model/effort in mocked CLI commands. The existing paused S2 run opened read-only with `status=PAUSED_USER`, `route=USER`; it was not resumed. |
| Windows regression | `python -m unittest discover -s tests -q`: 145 run, OK, one skipped. Scripted dry-run: PASS. |
| Linux devcontainer | Built from `.devcontainer/devcontainer.json` in an isolated local workspace. The post-create script completed as `vscode` after test-folder ownership was corrected: Python 3.11.16, Node 22.23.2, Codex CLI 0.156.1, LangGraph CLI 0.4.31, Git 2.55.0. |
| Linux regression | Same 145-test suite ran OK in the local devcontainer, with one skip. Scripted dry-run: PASS; zero Codex/model calls and no project experiment. |
| Studio server | `langgraph dev --host 0.0.0.0 --port 2024 --no-browser` served `/ok` as `{"ok":true}` and listed the `astra_orchestrator` assistant. The preview graph remains non-executable by test. |
| Secret boundary | The test container's generated `.env` was empty with mode `600`. The staged Git set excludes populated `.env`, Codex auth/session cache, run directories, scientific project specs/receipts, and large artifacts. |

The first isolated local devcontainer launch could not read its root-owned
`mktemp` workspace as user `vscode`; this was a test-fixture permission issue.
After giving `vscode` access, the exact post-create script ran successfully.
This local check does not prove GitHub's automatic post-create lifecycle.

Still requiring a newly created, authenticated GitHub Codespace:

- automatic post-create execution from the pushed branch;
- ChatGPT account device/browser sign-in and account access to GPT-6 Sol/Luna;
- private port-2024 forwarding and Studio access from the notebook browser;
- the real Codespace commit/push followed by notebook pull.

Those steps are not marked PASS by the local container check. Follow
[the Codespaces guide](codespaces.md) for setup and the safe handoff boundary.
