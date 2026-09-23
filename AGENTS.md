# Open Source SW Project Guidelines

These instructions apply to this repository in addition to the global Codex
guidelines.

Keep this file focused on current project rules. Historical decisions,
experiment logs, and research notes belong under `research/`, not here.

## Project map

Use this map to identify the relevant area before reading files.

- `training/`: model training and fine-tuning
- `service/`: inference and service/API code
- `scripts/`: data preparation, experiment, and utility scripts
- `tests/`: automated tests
- `research/CURRENT_STATE.md`: compact startup snapshot for the active phase
- `research/`: detailed research notes, experiment records, and history

Do not read every area by default. Load only what is relevant to the task.

## Task-specific guidance

Load only the guidance and context needed for the current task.

| Scope | Guidance |
| --- | --- |
| Active phase, blockers, and immediate next actions | `research/CURRENT_STATE.md` |
| Detailed decisions and experiment history | matching section of `research/PROJECT_STATE.md` or a focused file under `research/` |
| Model training or fine-tuning | relevant files under `training/` |
| Inference or API/service work | relevant files under `service/` |
| Data processing or utilities | relevant files under `scripts/` |
| Tests and verification | relevant files under `tests/` |
| Research or experiment interpretation | relevant files under `research/` |

Do not treat historical research notes as current instructions.

Do not read `research/PROJECT_STATE.md` in full by default. Search for the
needed heading or keyword and read only that section. When an earlier decision
conflicts with `research/CURRENT_STATE.md`, the compact current state takes
precedence.

## Astra PM session persistence

After the user explicitly invokes `$astra-pm` in a Codex task, keep the Astra
PM workflow active for later turns in that same task until the user ends it or
switches to an unrelated goal. Do not require the user to repeat the skill tag.

Initialize project state once: use the already active `AGENTS.md` instructions
and read `research/CURRENT_STATE.md` only on the first PM turn. Maintain the
phase, acceptance criteria, risks, active child sessions, and verified results
in the session context. Do not reopen the skill or state files on every user
message.

Refresh the compact state only after an external change, an uncertain
resume/compaction, or a conflict between remembered state and current evidence.
At a phase boundary, Astra writes the verified new snapshot and continues from
that result without rereading it.

## Experiment and evidence rules

Clearly distinguish:

- **Planned**: intended but not executed.
- **Observed**: directly produced by an executed experiment or command.
- **Inferred**: interpretation derived from observed evidence.

Never present Planned or Inferred information as an Observed result.

For dataset work, preserve or record when relevant:

- source,
- license,
- version or hash,
- train/validation/test split information.

For training and evaluation, preserve or record when relevant:

- random seed,
- configuration,
- checkpoint,
- dataset/version used.

Do not fabricate missing metadata.

Do not rewrite historical experiment results merely because a newer
experiment produced a different result.

## Verification for this project

Prefer targeted verification during iteration.

Examples:

- changed function -> relevant unit test,
- changed module -> module-level tests,
- changed training script -> lightweight execution or configuration check,
- changed service code -> relevant service/API tests.

Broaden verification when the change affects shared behavior.

Do not start expensive full training or large evaluation jobs merely to
"be safe" unless they are necessary or explicitly requested.

If a full run is not performed, say so clearly and report only the checks that
were actually executed.

## Verified commands

Only document commands here after they have actually been verified in this
repository.

Do not guess install, training, testing, lint, evaluation, or service commands
based only on conventional project layouts.

| Task | Command |
| --- | --- |
| Environment setup | |
| Run service | |
| Run targeted tests | `& .\\.venv-eval\\Scripts\\python.exe -m unittest tests.test_audio_windows -v` |
| Run all tests | `& .\\.venv-eval\\Scripts\\python.exe -m unittest discover -s tests -v` |
| Training | |
| Evaluation | `& .\\.venv-eval\\Scripts\\python.exe scripts\\evaluate_ssl_aasist_onnx.py --smoke --threads 2` |

Empty entries mean that no canonical command has been verified yet.

## User-assisted shortcuts

If a brief action, answer, file, or choice from the user would finish the task
materially faster than continuing alone or building a workaround, pause the
current task and send the user one concrete request immediately. State exactly
what the user should do or provide and why it is the shortest path to
completion.

Do not pause for information that a quick, safe local inspection can resolve.
Do not continue a costly workaround while waiting for the user-requested
shortcut.

## External and expensive operations

Ask before:

- downloading large datasets or models,
- starting long-running GPU training or evaluation,
- using paid APIs or cloud resources,
- uploading datasets, checkpoints, or results externally,
- publishing or deploying,
- deleting or overwriting persistent data,
- using credentials or secrets,
- changing production or remote infrastructure.

No confirmation is normally required for:

- reading repository files,
- searching the repository,
- inspecting Git status or diffs,
- making reversible local edits,
- running lightweight local checks,
- running targeted tests.

## Notebook and Codespace storage handoff

Use Git for selected code, private PostgreSQL for LangGraph checkpoints and
Codex session evidence, and Google Drive only as temporary storage for the
data or artifacts needed by the current task. Before switching hosts, identify
what the next host actually needs and remove unneeded temporary files from the
departing host and Drive only after confirming they are not active evidence or
the sole copy. For Drive cleanup, verify the local archive's filename, byte
count, and SHA-256 against the Drive copy or an execution receipt before
deletion. Preserve files needed for a pending review or resume.
The user has authorized project-scoped Drive downloads, uploads, and cleanup
without a separate permission request once the exact files and backup safety
have been verified. This does not authorize deleting unrelated personal files.

If Drive lacks room for a needed transfer, split it into independently
verified parts, transfer and reassemble with a whole-file hash check, and
report to the user if space still cannot be made safely. Do not buy storage,
weaken verification, or delete uncertain files to make a transfer fit.

## Research state maintenance

Keep the active startup context in:

`research/CURRENT_STATE.md`

Keep it at most 120 lines and 8 KiB. Replace stale facts rather than appending
a chronological diary. It should contain only:

- the active phase and immediate objective,
- verified assets and results still needed for the phase,
- active blockers and risks,
- next ordered actions,
- pointers to detailed evidence.

Put experiment history, superseded decisions, and detailed observations in
`research/PROJECT_STATE.md` or an existing focused research file. Read those
files by exact section only when the current task needs them.

## Language policy

Write all new or revised prose in `research/CURRENT_STATE.md` and
`research/PROJECT_STATE.md` in English. Preserve literal paths, commands,
hashes, identifiers, and quoted source text in their original form. Do not
bulk-translate unchanged historical entries unless the user explicitly asks.

Use Korean for user-facing commentary, questions, progress updates, and final
answers in the root session unless the user requests another language. All
agent-to-agent instructions, delegation packets, follow-ups, status messages,
and internal evidence reports must use English.

Do not turn this `AGENTS.md` into a chronological research diary.

## Project learnings

If an agent repeatedly makes the same project-specific mistake, propose a
short concrete rule that would prevent it.

Examples:

- `Always record the dataset version before comparing metrics.`
- `Never treat an unexecuted training plan as an observed result.`

Prefer tightening or replacing an existing rule over adding duplicates.
Remove obsolete rules when the project changes.
