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

## GitHub Codespace check

A new 2-core Codespace was created from `main` at commit
`d05b239c4abbe8df1c657d0040b47f844afbe1e9`. GitHub's automatic
post-create step exited with code 0 and reported Python 3.11.16, Node
22.23.2, and Codex CLI 0.156.1. Its checkout remained clean after the checks.

| Check | Observed result |
| --- | --- |
| Remote regression | The 79 tests committed to this repository ran OK in the Codespace. The larger 145-test local run also includes uncommitted project-specific tests that were intentionally not published. |
| Remote scripted dry-run | `PASS`, `codex_or_model_calls=0`, `real_training_or_experiments_run=false`; output stayed in the ignored `.astra-orchestrator/` directory. |
| Remote Agent Server | `langgraph dev --host 0.0.0.0 --port 2024 --no-browser` returned `{"ok":true}` at `/ok`; `/assistants/search` listed `astra_orchestrator`. |
| Port and Studio | GitHub CLI reported port 2024 forwarded with `private` visibility. An authenticated local tunnel from remote 2024 to notebook `127.0.0.1:2025` served `/ok`, and LangSmith Studio showed the connected Astra/Sol/Luna/Reviewer graph. Chrome blocked the direct `app.github.dev` URL on this notebook, so direct-URL browser access was not validated. |
| Codex login | `codex login status` reported `Not logged in` before user authentication. No real role/model call was made. |

Still requiring a human-only ChatGPT sign-in in this Codespace, verification
of actual GPT-6 Sol/Luna account access, and the Codespace commit/push followed
by notebook pull. A missing `LANGSMITH_API_KEY` banner appeared in Studio; the
graph preview works without it, but remote LangSmith trace uploads were not
verified. Follow [the Codespaces guide](codespaces.md) for the safe handoff
boundary. Stop the Codespace when not in use to conserve quota.


## Codespace-to-notebook handoff

The Codespace fast-forwarded from `d05b239` to `6e52dc3` with `git pull --ff-only`. This section is the small Codespace-originated Git artifact for the return-path check; the notebook pull is verified separately after this commit is pushed.
