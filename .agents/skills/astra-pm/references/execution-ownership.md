# Execution Ownership Details

Read this reference only when Astra is considering direct execution beyond normal initialization, applying a delegation exception, or sending a follow-up action packet.

## Acceptance gate boundary

Default to one acceptance gate per user request. Preparation, execution, monitoring, persistence, and verification of one deliverable remain one workstream. A split must be declared before execution and have independently reportable acceptance criteria. Never split or rename a gate retroactively to reset ownership or packet accounting.

## Total-cost objective and baseline

The primary efficiency measure is `total_session_tokens_per_delivered_gate`: total recorded tokens for Astra and every descendant, including work on blocked or failed gates, divided by the number of acceptance gates declared before execution and delivered to their acceptance criteria. Always retain the raw session total and gate outcomes beside the quotient. Root share, root maximum input, and cached-input percentage are diagnostic only and must not be used to claim efficiency.

The 2026-09-14 RVC session recorded 44,444,547 total tokens. Two retrospectively identifiable user-visible outcomes were delivered: the first-VC human-quality decision and the target194 calibration-20/Version3 observed-output outcome. The provisional historical quotient is therefore 22,222,274 tokens per delivered gate. The second outcome retained a saved-byte verification risk, and gate IDs were not declared before execution, so this quotient is a comparison baseline rather than a pass/fail target. The blocked target187 Version4 persistence work remains in the numerator. Never improve this baseline by retroactively splitting work into more gates.

For future runs, record cumulative root and descendant token totals at each predeclared gate's start and terminal outcome. Prefer the actual per-gate token delta over evenly dividing the whole session when those deltas are available.

## Inter-agent event protocol

`send_message` is an interrupt, not a progress stream. Before sending, the child must ask whether Astra must decide or act before safe execution can continue. If not, write the observation to the compact status artifact and continue without messaging.

Do not send operation-start notices, unchanged states, elapsed-time updates, estimates, intermediate counts, or ordinary checkpoints. Do not send a terminal summary and then return the same summary as the final report. A final report is delivered automatically.

Astra must not ask a child for routine status. Follow-ups are limited to new user input, new external evidence, a consolidated correction, or authority required to clear a precise blocker. A child must not call `wait_agent` merely to wait for Astra. If parent action is required, return `blocked` with one exact request.

`done` is valid only when all acceptance criteria are complete. If an external supervisor is still running, return `waiting` with `status_artifact`, `terminal_condition`, `earliest_check_at`, and a durable `recovery_anchor` when one exists. An ephemeral path or an unfinished archive is not a recovery anchor.

## Browser observation compression

Use a full accessibility tree or DOM snapshot once for initial orientation within a gate. A second full read is allowed only after an unexpected UI change makes recovery necessary. After orientation, query the exact button, status label, output row, URL, or named value needed for the next decision and emit a compact structured result. Do not combine every click or wait with `getAXState()`, a full DOM snapshot, or an unbounded tail of page state.

For structured artifacts, query named keys, matching records, counts, hashes, and terminal fields. Never concatenate whole manifests, inventories, JSONL files, source plans, or logs into one tool result. Store bulky evidence at a precise path and return only the result and pointer.

## Gate-scoped executor lifecycle

Declare gate IDs and acceptance criteria before execution. Reuse one Terra within a gate so corrections retain local context, then retire that context at the terminal gate outcome. Recovery, first-VC review, calibration persistence, separate-target training and backup, bulk conversion, and final assembly are separate gates only when declared up front with independently reportable acceptance criteria.

At a gate transition, write a small English handoff file containing only the completed gate result, exact durable artifact paths and hashes, unresolved risks, and next acceptance criteria. The next Terra receives the file path and its purpose, not the handoff body or prior transcript in an agent message.

## Conservative waiting until scheduled resumption is verified

Do not assume that ending a root turn will automatically resume the task. Until a thread-heartbeat or equivalent scheduled-resumption path has been verified in this environment, keep the authorized workflow in the active root turn and use event-driven waiting for the longest permitted interval. A timeout with no new decision-relevant evidence must not trigger a status query to the child or another child progress message. This is a conservative continuity fallback, not a claim that the waiting-cost problem is solved.

## Closure and expectation tests

A direct execution call is closed only when every possible result lets Astra stop executing and do exactly one of the following:

- make and report a decision;
- send one complete agent packet; or
- request one precise user action that is the shortest path through a blocker.

If any result could make Astra inspect again, retry, modify, test, wait, poll, or explore another path, delegate before the first call.

A direct lookup is allowed only when Astra states the expected result before the call. Checking that a reported artifact exists at its declared path, comparing a hash with a supplied value, or confirming an expected scoped Git state is verification. Listing directories, searching broadly, opening files to discover their contents, or checking an unspecified state is exploration and belongs to Luna.

Normal direct operations are limited to the one-time startup snapshot, one-shot Astra PM contract validation, a bounded check of submitted child evidence or a scoped diff, an expectation-backed deterministic lookup, a gate decision, and PM-owned compact-state maintenance. Tests, builds, implementation edits, browser or computer use, cloud operations, training, installs, uploads, downloads, waiting, and polling belong to Terra. Research and exploration belong to Luna.

## Delegation exception

Delegation is genuinely unavailable only when the required collaboration tool is unavailable, an actual child-spawn attempt fails, or the user explicitly directs Astra itself to execute without delegation. Do not infer an exception from convenience, urgency, child slowness, a child blocker, or the task appearing small. Phrases such as "hurry," "continue," or "handle it" are not explicit directions for Astra to execute personally.

When the exception applies, tell the user in one line and record `policy_exception`, its reason, and its scope in the in-session ledger. Do not create a separate progress log merely for this record.

The exception changes execution ownership only. It does not expand the user's requested outcome, granted permissions, destructive or remote authority, retry limits, observation-compression rules, fast-fail rules, or any other safety boundary.

Under an exception, do not replace an unavailable child with an unbounded direct loop. For long-running work, use a compact status artifact with an explicit terminal state and return only state transitions upstream. Do not reread logs that repeat that state. Check at most twice before the expected completion time and once after it. After two failures of the same UI action or operation, stop that path and request user help when a brief user action is the shortest route; otherwise report the precise blocker.

## Action-packet accounting

Track `astra_action_packets_per_gate` against the stable acceptance-gate ID, not a child session, role instance, filename, or phase label. Replacing a child or renaming work does not reset it.

Count the initial work packet and every later packet that requests correction, additional implementation, or re-execution. Do not count passive status transitions, cancellation, or a relayed user message that requests no agent action.

Classify each packet reason as `initial`, `packet_gap`, `implementation_failure`, `reviewer_finding`, `new_external_evidence`, or `user_scope_change`. A third packet caused by `packet_gap` or `implementation_failure` indicates that the initial packet or executor discretion was too narrow. Before sending it, consolidate all known corrections and expand Terra's implementation discretion only within the existing acceptance criteria, forbidden areas, and user-granted authority. Never broaden external permissions or the requested outcome.

## Post-run signals

Evaluate cost only after the workstream rather than polling usage during execution. The primary measure is total recorded tokens across root and descendants per delivered predeclared gate, with the raw total and each gate outcome retained.

For the next long browser verification, inspect these three causal signals first:

- Terra maximum input as a percentage of its recorded context window;
- total serialized browser-return characters;
- Terra compaction count.

Child upstream interrupts, root and child wait calls, action packets, and root share may explain a regression, but they are secondary diagnostics. Do not use a lower Astra share to claim improvement when total cost per delivered gate rises. Do not poll session logs during work merely to calculate these values.
