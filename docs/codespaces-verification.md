# Codespaces compatibility verification receipt

Recorded: 2026-09-23 UTC. Scope: the orchestration package and its Linux
development environment. No scientific training, evaluation, dataset work,
or approved-evidence change was performed. Later account-access checks used
minimal harmless Codex model probes; the orchestration dry-runs used none.

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
| Codex login before authentication | `codex login status` reported `Not logged in`; no role/model call had yet been made. |

After the user's ChatGPT device authentication, `codex login status` reported
`Logged in using ChatGPT`. Three minimal read-only probes returned `OK` for
Astra `gpt-6-astra`/`high`, Sol/Reviewer `gpt-6-sol`/`high`, and Luna
`gpt-6-luna`/`xhigh`. These checked model access only; they were not project
experiments. Sol and Reviewer share the same configured model/effort pair, so
no duplicate Reviewer probe was needed.

## Authenticated LangSmith check

The user's Codespaces secrets were initially absent from the running shell
even after a VS Code reload. After stopping and restarting the Codespace,
a boolean-only check reported `key_present=True` and
`tracing_enabled=True`. No key value or credential was printed or recorded.
`codex login status` still reported `Logged in using ChatGPT` after restart.

| Check | Observed result |
| --- | --- |
| Committed remote tests | `.venv/bin/python -m unittest discover -s tests -q`: 79 tests in 14.537 s, `OK`. |
| No-model dry-run | `PASS`, 16/16 checks, `codex_or_model_calls=0`, `network_calls=0`, `real_training_or_experiments_run=false`. Receipt: ignored `.astra-orchestrator/codespaces-auth-verification-20260923/final-receipt.json`. |
| LangSmith upload and readback | A scripted `_research_plan_pass` orchestration completed with `LangSmithObserver` enabled. The LangSmith API returned parent run `01a0ccd7-43b1-7c11-bcb0-5ae133b3197d` as `success`, with `status=COMPLETED` and `routing_decision=END`. |
| Nested role spans | Seven direct children appeared in order: Astra, Luna, Astra, Sol, Astra, Reviewer, Astra. Every child's `parent_run_id` matched the parent. |
| Span metadata | Every child had the configured model/effort, `stage=verified`, status, session ID, start/end timestamps, and latency. Routes were `LUNA`, `ASTRA`, `SOL`, `ASTRA`, `REVIEW`, `ASTRA`, `END`; Reviewer verdict was `PASS`. Luna and Reviewer session modes were `new`. |
| Usage and cost | The scripted backend supplied no reliable token or cost data. No child span contained input/output/total token or cost metadata; no values were estimated. |

The LangSmith API readback and assertions passed for this
[synthetic trace](https://smith.langchain.com/o/370b99a4-bc52-43f3-b63c-0529327ecf4a/projects/p/7fc337a5-75b8-4b76-9c6d-2af7c3f61087/r/01a0ccd7-43b1-7c11-bcb0-5ae133b3197d?poll=true).
The trace proves the upload, nesting, and available metadata path; it does
not claim that live scientific work or live Codex role execution was traced.
Follow [the Codespaces guide](codespaces.md) for the safe handoff boundary.
Stop the Codespace when not in use to conserve quota.

## Codespace-to-notebook handoff

The Codespace fast-forwarded from `d05b239` to `6e52dc3` with
`git pull --ff-only`. It committed and pushed this section as `4051ed6`.
The notebook then fast-forwarded from `6e52dc3` to `4051ed6` with
`git pull --ff-only origin main`; unrelated local changes remained untouched.
