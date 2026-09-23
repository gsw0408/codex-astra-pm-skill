# Codex Astra PM Skill

A project-scoped Codex workflow in which Astra owns planning and integration,
Luna performs bounded research, Terra executes one acceptance gate, and Sol
reviews only stable phase gates.

Invoke it explicitly with `$astra-pm`. The included `AGENTS.md` is a
project-policy example; adapt its project map, verified commands, and state
paths before using this repository in another codebase.

## LangGraph orchestration

The separate `astra_orchestrator` package runs a restart-safe Astra/Sol/Luna/
Reviewer control plane through Codex CLI sessions. New runs use GPT-6 Astra
High, GPT-6 Sol High for implementation and independent review, and GPT-6
Luna Extra High for research. Older schema-v2 runs retain their recorded
model settings on resume. See [the operations guide](docs/orchestration.md)
and [Codespaces setup](docs/codespaces.md) for safe local and Linux use.

## Included configuration

```text
AGENTS.md
.agents/skills/astra-pm/
  SKILL.md
  references/execution-ownership.md
  scripts/contract.json
  scripts/validate_contract.py
.codex/config.toml
.codex/agents/
  luna_researcher.toml
  terra_executor.toml
  sol_gate_reviewer.toml
```

This repository intentionally excludes source code, datasets, experiment
records, run logs, credentials, and generated artifacts from the originating
project.

## Current operating model

- **Astra** plans, routes work, integrates evidence, maintains compact state,
  and makes the final gate decision.
- **Luna** answers one bounded research question without reconstructing the
  full project context.
- **Terra** implements and self-verifies one acceptance gate. Browser, cloud,
  long-running, build, test, and implementation work belong here.
- **Sol** independently challenges evidence only at a stable phase gate.

The skill defaults to delegation. Astra may directly perform only a closed
operation whose result requires no second execution call. A one-shot contract
validator is an explicit exception because it returns a bounded configuration
result before any child is spawned.

## Changes captured in this version

- Replaced ambiguous per-phase child reuse with Luna-per-decision,
  Terra-per-acceptance-gate, and Sol-per-stable-phase-gate lifecycles.
- Standardized gate verdicts as `PASS`, `PASS_WITH_CONDITIONS`, and `BLOCK`.
- Standardized seven common English report fields. Terra alone may return
  `waiting` and adds its `wait_contract`.
- Defined `send_message` as an interrupt channel in every child profile;
  routine progress stays out of Astra's context.
- Added a small static contract validator for lifecycle wording, verdict/status
  enums, report-field sets, profile paths, and mirrored child instructions.
- Blocks child spawning on a configuration error. A user may explicitly permit
  a session-only bypass, which must be recorded as `policy_exception`.

## Verify

From the repository root:

```powershell
python .agents/skills/astra-pm/scripts/validate_contract.py --project-root .
```

Expected output is `OK`. The validator is intentionally limited to cross-file
configuration invariants; it does not replace behavioral evaluation of child
agents.
