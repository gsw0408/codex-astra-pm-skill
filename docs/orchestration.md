# Astra/Sol/Luna LangGraph orchestration

`astra_orchestrator` is a restart-safe control plane for the following fixed
workflow:

```text
                           +----------------------+
                           |                      v
START -> ASTRA --SOL-----> SOL ----------------> ASTRA
           |                                       |
           +--LUNA----> fresh, read-only LUNA -----+
           |                                       |
           +--REVIEW-> fresh, read-only REVIEWER --+
           |
           +--USER---> interrupt --resume--------> ASTRA
           |
           +--END--------------------------------> END
```

The runtime coordinates Codex CLI sessions; it does not call a model API
directly. It is additive and does not rewrite the repository's existing
`$astra-pm` skill, whose legacy role names have different meanings.
For Linux/Codespaces installation and notebook handoff, see
[the Codespaces guide](codespaces.md).

A verified Codex usage-quota-exhaustion signal from any role bypasses the
normal return arrows in this diagram: the controller preserves the call
evidence and terminates immediately at `FAILED` / `END`.

## Fixed roles and settings

The settings are policy, not per-run options. They are written to the run
manifest and checked again when a run is reopened.

| Role | Fixed setting | Authority |
| --- | --- | --- |
| Astra | Project Manager — `gpt-6-astra`, High (`high`) | Holds complete state, evaluates all results, revises the plan, selects routes, prepares review packages, and alone approves transitions |
| Sol | Implementation Worker — `gpt-6-sol`, High (`high`) | Executes one bounded Astra task and reports facts; it cannot re-plan or approve advancement |
| Reviewer | Independent Reviewer — `gpt-6-sol`, High (`high`) | Independently inspects one frozen package and named evidence in read-only mode; it returns only `PASS`, `FAIL`, or `NEEDS_EVIDENCE` to Astra |
| Luna | Research and Information-Gathering Specialist — `gpt-6-luna`, Extra High (`xhigh`) | Performs one bounded research or evidence-gathering task and returns sourced findings only to Astra |

Sol must not instruct Reviewer or Luna, and Reviewer and Luna must not instruct
Sol. Luna cannot manage the project, modify project authority, approve a stage
transition, perform implementation, or act as Reviewer. Reviewer cannot edit
the project, change the plan, or approve a transition. A Sol
`MILESTONE_COMPLETE`, a Luna `DONE`, and a Reviewer `PASS` are evidence for
Astra; none changes the stage by itself.

Whenever literature review, web research, evidence gathering, or other
external information collection is needed, Astra routes a bounded `LunaTask`.
It must not use Sol as a substitute researcher or reuse a prior Luna session.

New runs use these settings. Older schema-v2 manifests with the original
GPT-5.6 Sol/Reviewer/Luna settings are the only supported legacy policy; on
resume, their recorded models and efforts remain unchanged, including in
LangSmith spans. An altered or incomplete role policy is rejected. The runtime
does not silently downgrade a requested model or effort. A locally installed
Codex CLI or account that does not accept a recorded combination produces a
preserved call failure rather than a different hidden setting. GPT-6 Sol and
Luna availability depends on account rollout and workspace settings.

## Deterministic state and contracts

The orchestration schema version is `2`. `WorkflowState` checkpoints the
immutable `ProjectSpec`, mutable `ActivePlan`, plan-revision history, current
and completed stages, route, retry counters, recovery history, step count,
session IDs, latest role outputs, transitions, pause information, errors, and
`target_stage_completed`.

Important enums are:

- Routes: `SOL`, `LUNA`, `REVIEW`, `USER`, `END`
- Sol statuses: `DONE`, `BLOCKED`, `NEEDS_USER`, `MILESTONE_COMPLETE`, `FAILED`
- Luna statuses: `DONE`, `INSUFFICIENT_EVIDENCE`, `BLOCKED`, `FAILED`
- Reviewer verdicts: `PASS`, `FAIL`, `NEEDS_EVIDENCE`
- User response statuses: `PROVIDED`, `DECLINED`, `CANCELLED`
- Workflow statuses: `RUNNING`, `PAUSED_USER`, `COMPLETED`, `ABORTED`, `FAILED`

The main model-facing contracts are `AstraDecision`, `SolTask`, `SolResult`,
`LunaTask`, `LunaResult`, `ReviewPackage`, `ReviewerResult`, and
`UserResponse`. Every Sol and Luna task is bound to the active
`plan_revision_id`. Sol, Luna, and Reviewer validators reject routing,
planning, transition, target-stage, and approval fields. Only an
`AstraDecision` may contain route or plan authority.

Unknown fields, missing fields, bad enum values, stale plan IDs, mismatched
task/result identities, and invalid transition evidence are rejected before
they can enter graph state. A validation or infrastructure failure never
fabricates a `USER` reason.

### Canonical contracts and Codex transport schemas

The JSON schemas under `astra_orchestrator/schemas` are the canonical
contracts. Codex Structured Outputs accepts a smaller JSON Schema subset, so
the CLI adapter derives a transport-only schema for each call rather than
weakening those contracts. The derivation:

- changes `oneOf` to `anyOf` and removes unsupported conditional/composition
  refinements (`allOf`, `not`, `if`/`then`/`else`, `dependentRequired`, and
  `dependentSchemas`), while retaining a structural `$ref` found in the
  bundled `allOf` shapes;
- removes unsupported `minLength`/`maxLength` bounds and schema-only metadata;
- changes each `const` to a single-value, explicitly typed `enum`;
- marks every object property required on the wire and represents a
  canonically optional property as a union with `null`; and
- represents arbitrary-key retry-count maps as deterministic
  `[{"key": ..., "value": ...}]` entry arrays, because every transport object
  is closed with `additionalProperties: false`.

The adapter reverses the nullable-option and entry-array encodings after the
call. `calls/<call-id>/final.txt` preserves the raw transport JSON written by
Codex, while `final.json` contains the decoded canonical shape. The original
Python validators then enforce all canonical requirements, including every
constraint intentionally omitted from the transport schema. They remain the
authoritative gate before any model output can enter graph state.

This compatibility layer follows the supported subset documented in the
[official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

## Immutable purpose and autonomous plan revisions

`ProjectSpec.ultimate_purpose` is the immutable project-authority boundary.
The initial mutable plan is seeded from these other specification fields:

- project context;
- ordered stages, goals, acceptance criteria, and review requirements;
- target stage;
- experiment plan;
- datasets;
- evaluation criteria; and
- approaches.

Astra may autonomously replace the mutable plan. It does not request USER
approval merely because it changes an experiment plan, dataset, evaluation
criterion, stage structure or order, or implementation/research approach.
Such a change is a complete `PlanRevision`, not a partial patch. A revision
contains a new `revision_id`, complete replacement plan, summary, rationale,
`purpose_alignment`, and `preserves_ultimate_purpose=true`; it must route
immediately to bounded Sol or Luna work. A plan revision cannot share the same
decision with a stage transition.

Astra is prohibited from weakening, replacing, removing, or conflicting with
the immutable ultimate purpose. The schema does not offer an
`ultimate_purpose` field in a plan revision, the controller keeps the original
purpose outside `ActivePlan`, review packages repeat that immutable value, and
Reviewer is instructed to flag a purpose conflict. Because purpose alignment
is semantic, Astra's explicit rationale and the subsequent independent review
remain part of the durable evidence.

Applying a revision invalidates pending/stale Sol, Luna, package, and Reviewer
state and starts a new Sol working scope. A PASS tied to an older plan revision
cannot authorize advancement. Plan revisions are appended to
`plan-revisions.jsonl`.

## USER policy and automatic recovery

`USER` is allowed for exactly three categories:

| Category | When it is valid | Recovery attempts before pausing |
| --- | --- | --- |
| `AUTH_OR_HUMAN_ACTION_REQUIRED` | Authentication, MFA, CAPTCHA, a secret supplied out of band, a permission grant, a physical/manual step, or another genuinely human-only action | Zero; pause immediately when clearly unavoidable |
| `PAID_RESOURCE_APPROVAL` | Approval for a new paid resource | Zero; pause before incurring the cost |
| `AUTOMATIC_RECOVERY_EXHAUSTED` | One identified issue has exactly five recorded, failed automatic recovery strategies | Exactly five failed strategies |

Scope or plan changes are not USER reasons. Nor are ordinary implementation
difficulty, missing public information, a generic model error, retry-limit
exhaustion, or an ambiguous call automatically USER reasons.

When Sol reports `NEEDS_USER`, Astra first classifies the requested action. If
it is clearly human-only or a new paid-resource approval, Astra pauses without
consuming recovery attempts. Otherwise Astra creates a recovery issue and
must try reasonable automatic strategies before asking the user. The same
rule applies to recoverable Sol, Luna, or review failures.

Each recovery strategy is a numbered record with:

- the same `issue_id` for the episode;
- a unique `strategy_id`;
- `attempt` from 1 through 5;
- an `approach`, rationale, and explanation of how it differs from prior
  attempts; and
- a `SOL` or `LUNA` route plus the recorded outcome.

An approach that normalizes to the same text as a prior approach is rejected.
Strategies should be meaningfully different wherever reasonable, not merely
renamed retries. A successful strategy closes the recovery issue immediately;
unused attempts are not consumed. If all five fail, the next valid Astra
decision is `USER/AUTOMATIC_RECOVERY_EXHAUSTED`. A sixth automatic strategy,
an exhaustion interrupt before five failures, or a non-USER route after the
fifth failure is rejected. Attempts are checkpointed and appended to
`recovery-attempts.jsonl`, so restarting cannot reset the count.

## Codex usage-quota hard stop

Codex usage quota exhaustion is a terminal controller condition, not a USER
category or an automatic-recovery issue. On the first verified
`UsageLimitExceeded` signal (or an equally unambiguous structured Codex quota
exhaustion signal) from Astra, Sol, Reviewer, or Luna, the controller:

1. persists the failed call's raw events, stderr, process metadata, session ID
   when present, and terminal error evidence;
2. sets workflow status to `FAILED` and route to `END`;
3. clears any pending USER request and writes terminal receipts with
   `resumable: false`; and
4. dispatches no later Astra, Sol, Reviewer, or Luna call.

This stop consumes neither a role retry nor one of the five automatic recovery
strategies. The controller must not retry or resume the failed role call,
route the condition through USER, redeem a usage-reset credit, purchase
credits, schedule a later continuation, or automatically restart the run.
Re-invoking a terminal run is idempotent and cannot resume work.

Generic transient rate limiting and context-window exhaustion are not by
themselves proof that the usage quota is exhausted. The hard stop requires the
service-classified quota signal or unmistakable quota-exhaustion evidence.
Codex documents `UsageLimitExceeded` separately from
`ContextWindowExceeded` in the
[App Server error contract](https://learn.chatgpt.com/docs/app-server).

## Milestone review and transition authority

For a stage with `requires_review=true`, the path is:

1. Sol reports `MILESTONE_COMPLETE` with evidence.
2. Astra freezes a complete `ReviewPackage`.
3. A completely fresh Reviewer independently inspects the named evidence.
4. The verdict returns to Astra.
5. Only Astra may emit a valid `StageTransition`.

The package contains:

- the active project context and immutable ultimate purpose;
- the absolute project root, active `plan_revision_id`, and complete active
  plan (stage order, experiment plan, datasets, evaluation criteria, and
  approaches);
- the unaltered active milestone goal and acceptance criteria;
- the exact Sol instructions and actual Sol result;
- the exact evidence/artifact paths and known limitations from that result;
- a controller-defined snapshot of stage, target, retry, limit, correction,
  and latest-Sol state; and
- Astra's proposed next plan.

It is written to `reviews/<review-id>/packet.json` before Reviewer starts, and
its SHA-256 is recorded. Reviewer independently reports evidence inspected,
findings, missing evidence, limitations, and one verdict. `PASS` returns to
Astra and permits—but never performs—advancement. `FAIL` or
`NEEDS_EVIDENCE` creates a recovery issue and returns to Astra for bounded
corrective Sol work or Luna research. A new review is possible only when the
issue is resolved and corrective milestone work is ready. Every such review
uses another fresh Reviewer.

For a stage with `requires_review=false`, Sol still must provide
`MILESTONE_COMPLETE`, and Astra remains the only transition authority. The
transition basis is `NOT_REQUIRED` rather than a fabricated review.

## Fresh Reviewer and Luna guarantees

Astra and Sol may resume their exact recorded session IDs when continuity is
useful and the workflow remains nonterminal. Reviewer and Luna always use a
new session. For each specialist call, the adapter:

- runs `codex exec`, never `codex exec resume`, `resume --last`, or a fork;
- passes `--ephemeral`, `--ignore-user-config`, and `--ignore-rules`;
- launches the process from a newly created isolated process CWD, while
  explicitly setting the Codex working root to `project_root` with `--cd` and
  enforcing `--skip-git-repo-check --sandbox read-only` there;
- gives only the self-contained bounded prompt and output schema;
- additionally enables Codex search for Luna;
- captures the `thread.started` session ID; and
- rejects an ID already observed for Astra, Sol, Reviewer, or Luna in the run.

Session IDs observed even in failed specialist attempts are recovered from
durable process receipts. Reviewer IDs are appended to
`review_session_ids`; Luna IDs are appended to `luna_session_ids`. The fresh
session proof therefore survives a process restart.

"Fresh" means a new session with no prior role conversation or saved rollout.
Ignored user configuration and rules prevent user or project instruction files
from supplying hidden context. The isolated process CWD does not remove the
explicit project access: `--cd project_root` gives Reviewer and Luna a named,
read-only Codex working root for inspecting the evidence in their bounded
prompt. Platform safety instructions and the explicitly supplied task/package
still apply.

Codex CLI mechanics are described in
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
The requested model names are listed in the
[OpenAI model catalog](https://developers.openai.com/api/docs/models).

## Persistence, restart, and evidence

Each run has one durable directory. `checkpoint.sqlite` is the LangGraph
SQLite checkpointer, and the run ID is the stable LangGraph `thread_id`.
Reopening the same run directory continues the same checkpoint, counters,
recovery history, and role session policy. Real runs never use an in-memory
checkpointer.

The run directory must be outside `project_root`. Sol receives a
workspace-write sandbox rooted at the project, so placing controller evidence
inside that tree would let implementation work alter the manifest,
checkpoints, or receipts. The CLI and runtime both reject that unsafe layout.

Calls are prepared before process launch and finalized with immutable results
and receipts. A successfully receipted call is recovered rather than executed
again. A call with incomplete/conflicting durable evidence is not silently
replayed. Its failure or ambiguity is returned to Astra as an issue that must
follow the same automatic-recovery/USER policy; it does not create a fourth
USER category. The sole exception is a durably recorded usage-quota hard stop:
reopening the checkpoint reconstructs the same terminal `FAILED` / `END`
state instead of replaying or recovering the call.

LangGraph documents thread-scoped checkpoints in its
[persistence guide](https://docs.langchain.com/oss/python/langgraph/persistence)
and same-thread `Command(resume=...)` behavior in its
[interrupt guide](https://docs.langchain.com/oss/python/langgraph/interrupts).

The evidence layout is:

```text
<run-dir>/
  manifest.json
  spec.json
  checkpoint.sqlite
  events.jsonl
  transitions.jsonl
  plan-revisions.jsonl          # present after a plan revision
  recovery-attempts.jsonl       # present after an automatic recovery attempt
  errors.jsonl                  # present after an error
  resume-info.json
  calls/
    <call-id>/
      request.json
      output-schema.json
      status.json
      prompt.txt
      codex-output-schema.json
      launch.json               # requested role/model/effort and command
      events.jsonl
      stderr.txt
      final.txt
      final.json
      process.json              # includes the observed session ID
      result.json               # successful validated call
      error.json                # failed or ambiguous call
      receipt.json              # successful hash-bearing call receipt
  reviews/
    <review-id>/
      packet.json
      packet-receipt.json
      result.json
      receipt.json
  final-receipt.json            # terminal run
```

JSONL records are append-only. Immutable JSON artifacts are created
exclusively, small status files are replaced atomically, and artifacts are
hash-linked where applicable. Preserve the complete directory when
reproducible evidence or later resume is required.

For a usage-quota stop, the call's `process.json` identifies the detected
quota condition, while the call error, error log, transition, checkpoint,
`final-receipt.json`, and `resume-info.json` preserve the terminal decision.
The checkpoint remains inspectable evidence, but it is not resumable.

Schema-v1 manifests/checkpoints are intentionally not resumed by this
schema-v2 runtime; the runtime fails closed on a version mismatch. Preserve a
v1 directory as historical evidence and start a new v2 run. No in-place
checkpoint migration is implied.

## Pause and resume

The USER node uses LangGraph `interrupt()`. Its durable request identifies one
of the three allowed categories, the prompt, and exact required actions.

Never put a password, token, MFA code, or secret value in `--response` or a
response file because responses are retained. Configure credentials or grant
permissions out of band, then provide only a safe confirmation or artifact
reference.

Inspect without advancing:

```powershell
python -m astra_orchestrator status `
  --run-dir ..\astra-orchestrator-runs\demo-v2
```

Resume the same checkpoint after the requested action:

```powershell
python -m astra_orchestrator resume `
  --run-dir ..\astra-orchestrator-runs\demo-v2 `
  --response "Authentication completed out of band."
```

For a structured response, copy the pending `request_id` exactly:

```json
{
  "request_id": "auth-request-1",
  "status": "PROVIDED",
  "response": "Authentication completed out of band.",
  "evidence_artifact_paths": []
}
```

Then use `--response-file response.json`. `DECLINED` and `CANCELLED` require
a non-empty explanation and terminate the run as `ABORTED`; they never mark
the target complete. USER responses cannot rewrite execution limits or project
authority.

These resume commands apply only to a run that is still `PAUSED_USER`. A run
terminated by verified usage-quota exhaustion is `FAILED` / `END`, has no
pending USER request, and cannot be resumed; restoring usage later does not
automatically restart that terminal run.

## Loop limits and termination

The input specification fixes positive limits for Sol attempts per stage,
Reviewer attempts per stage, total graph transitions, and model/structured
output errors. Those counters are checkpointed and do not reset after
restart. The five-strategy recovery bound is separate and fixed by policy.
Recovery work is still subject to the hard total-transition budget.

Ordinary limit exhaustion or an invalid controller/model output ends safely as
`FAILED`; it is not mislabeled as human-required. Set realistic limits before
starting a run. A user response does not raise them.

Verified Codex usage-quota exhaustion is also terminal, but it bypasses all
configured retry and recovery budgets immediately. It never falls through to
the USER node, reset-credit handling, or a deferred continuation.

Astra may route `END` only together with a valid transition completing the
active target stage and `target_stage_completed=true`. After that, later
stages are not started. Terminal `COMPLETED`, `ABORTED`, and `FAILED` states
are idempotent on subsequent invocations.

## Setup and commands

Prerequisites are Python 3.10 or newer and an installed, authenticated Codex
CLI. From the repository root:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
codex --version
```

No direct OpenAI model API or API SDK is required. Codex CLI authentication,
model availability, permissions, and paid-resource approval remain operator
responsibilities.

Copy and edit `examples/orchestration-spec.json`. Preserve the immutable
ultimate purpose and choose a target stage. `start --project-root` replaces
the JSON file's `project_root` with the normalized absolute directory.

```powershell
& .\.venv\Scripts\python.exe -m astra_orchestrator start `
  --spec examples\orchestration-spec.json `
  --project-root . `
  --run-dir ..\astra-orchestrator-runs\demo-v2
```

`status` is read-only. Use `resume` with the existing run directory; never use
`start` to continue an interrupted run. The `astra-orchestrator` console
command is equivalent to `python -m astra_orchestrator`. `start` and `resume`
exit `0` for normal completion or a safe USER pause, `1` for `FAILED`, and `2`
for a user-declined or cancelled `ABORTED` run; `status` itself remains a
read-only query with exit code `0` when the run can be inspected.

## Safe end-to-end dry run

Run the deterministic simulation in a new output directory:

```powershell
& .\.venv\Scripts\python.exe -m astra_orchestrator dry-run `
  --output .astra-orchestrator\dry-run-v2
```

The dry run uses scripted role results. It is designed to cover Astra/Sol
loops, autonomous plan revision, a fresh Luna returning to Astra, fresh
Reviewer verdicts, FAIL/NEEDS_EVIDENCE correction, PASS-controlled
advancement, immediate human-only USER pause, five-strategy recovery
exhaustion, checkpoint reopen/resume, usage-quota hard-stop behavior without
retry or USER routing, loop bounds, and target termination.

It must not launch a live Codex/model session, run training or project
experiments, process real datasets, consume a paid resource, or alter approved
project evidence. The output directory must not already exist, preventing a
verification run from overwriting earlier receipts.

Run the orchestration tests separately:

```powershell
& .\.venv\Scripts\python.exe -m unittest `
  tests.test_codex_sessions `
  tests.test_orchestration_audit `
  tests.test_orchestration_graph `
  tests.test_orchestration_prompts `
  tests.test_orchestration_schema `
  tests.test_transport_schema -v
```

The generated dry-run receipt and test output are the local evidence map. A
successful synthetic run does not authorize real experiments, external side
effects, or paid-resource use.

## LangSmith tracing

The production CLI uses [LangSmith `RunTree` custom instrumentation](https://docs.langchain.com/langsmith/annotate-code#use-the-runtree-api):
one parent `Astra orchestration` trace per `start`, `resume`, or automatic
continuation invocation, with an explicitly nested child span for each Astra,
Sol, Luna, or Reviewer node. A resumed checkpoint keeps its workflow `run_id`
but creates a new parent trace segment; filter by `run_id` to see all segments.
The USER interrupt remains a checkpoint operation, not a model span.

Copy `.env.example` to a local `.env` and set your own LangSmith key. The CLI
loads `.env` without overriding existing process environment variables;
`langgraph.json` also uses it for Studio. `.env` is Git-ignored. Never put a
real key in `.env.example`, a command argument, a project spec, or committed
configuration.

```powershell
Copy-Item .env.example .env
# Edit .env locally: LANGSMITH_TRACING=true and LANGSMITH_API_KEY=<your key>
& .\.venv\Scripts\python.exe -m unittest tests.test_orchestration_tracing -v
```

Supported `.env` variables:

| Variable | Purpose |
| --- | --- |
| `LANGSMITH_TRACING` | `true` enables the explicit production traces; `false` disables them. |
| `LANGSMITH_API_KEY` | Required for trace delivery; keep it private. Without it the controller runs without tracing. |
| `LANGSMITH_PROJECT` | LangSmith destination project; defaults to `astra-orchestrator`. |
| `LANGSMITH_ENDPOINT` | Optional nondefault/self-hosted LangSmith API endpoint. |

Run the normal documented `start` or `resume` command, then open your project
in [LangSmith Tracing](https://smith.langchain.com/) and filter by the
controller `run_id`. This is the *real* run trace; the Studio graph below is
only a topology preview. Local SQLite checkpoints and audit receipts remain
the authoritative evidence and resume mechanism even if LangSmith is offline.
An unavailable SDK, missing key, or trace transport failure cannot change
routing, retries, role session policy, or a USER pause. Remote delivery may be
incomplete during an outage; rerunning a role merely to fill a trace is unsafe.

Each role span records the fixed model and reasoning effort, UTC start/end
timestamps, measured latency, stage, session ID/mode when observed, status or
sanitized error type, and actual routing decision. Reviewer spans record the
validated verdict; recovery spans record the assigned attempt number. The
parent records invocation kind, final status, stage, and route. Inputs and
outputs contain identifiers/outcomes only. Full graph state, prompts, model
responses, secrets, credentials, and environment values are not sent. Native
LangGraph auto-tracing is suppressed during production graph invocation for
that reason; `RunTree` spans continue to post explicit metadata.

Token counts are present only when the Codex `turn.completed.usage` receipt
contains explicit valid `input_tokens`, `output_tokens`, or `total_tokens`.
Missing totals are not inferred. USD cost is recorded only if that receipt
explicitly supplies a nonnegative numeric `total_cost_usd` or `cost_usd`;
the current Codex CLI commonly provides token counts but no cost. No price
table or token/cost estimate is used. These values are span metadata on tool
runs, not a promise of LangSmith's native LLM cost aggregation. The scripted
test's token/cost numbers are fixtures, not observed model consumption.

The no-model proof is:

```powershell
& .\.venv\Scripts\python.exe -m unittest tests.test_orchestration_tracing -v
```

It captures native `RunTree` create/update calls in memory, asserts one parent
with all four roles, exact available metrics, fresh sessions, recovery
attempts, USER pause, and unchanged routes when trace delivery fails. It does
not contact LangSmith or run project experiments. A live LangSmith trace
requires a configured key and an authorized real CLI workflow; it is not
created by this test.

## LangSmith Studio preview

The repository's `langgraph.json` exposes `astra_orchestrator.studio:graph`
to LangSmith Studio. It uses the same route builder as the controller, so the
visible Astra, Sol, Luna, Reviewer, USER, and END nodes and edges match the
production graph. The preview nodes reject execution. Studio does not open the
controller's SQLite checkpoint or show earlier CLI run history. Use the CLI
`status` command and the run receipts to inspect a particular workflow.

LangGraph's local development server requires Python 3.11 or newer. From the
repository root on Windows, create a local environment and start it:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[studio]'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
& .\.venv\Scripts\langgraph.exe dev
```

Open the Studio URL printed by the server, normally
[LangSmith Studio](https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024),
and select `astra_orchestrator` in Graph mode. Sign in to LangSmith when
prompted. Keep any required LangSmith API key in your local `.env` or process
environment, never in `langgraph.json` or a committed file. Stop the local
server with Ctrl+C.

This entry point is for inspecting the topology. To start or resume a real
workflow, use the documented `astra_orchestrator` CLI commands, which own the
checkpoint, audit receipts, and role sessions.
