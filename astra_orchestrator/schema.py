"""Strict, JSON-compatible contracts for deterministic graph routing.

Only Astra decisions contain routing, planning, or stage-transition authority.
Sol, Luna, and Reviewer outputs contain observations about an assigned task or
review package; their validators reject both unknown fields and explicit
authority fields. Validators return defensive, normalized copies and never
mutate their inputs.

``ProjectSpec`` and ``WorkflowState`` document the persisted graph state used
by the orchestration layer.  They are deliberately ordinary ``TypedDict``
types so the package remains compatible with Python 3.10 and the standard
library.  Model-facing JSON Schemas live in ``astra_orchestrator/schemas``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypedDict, cast


Route = Literal["SOL", "LUNA", "REVIEW", "USER", "END"]
SolStatus = Literal[
    "DONE", "BLOCKED", "NEEDS_USER", "MILESTONE_COMPLETE", "FAILED"
]
LunaStatus = Literal["DONE", "INSUFFICIENT_EVIDENCE", "BLOCKED", "FAILED"]
ReviewerVerdict = Literal["PASS", "FAIL", "NEEDS_EVIDENCE"]
UserResponseStatus = Literal["PROVIDED", "DECLINED", "CANCELLED"]
WorkflowStatus = Literal["RUNNING", "PAUSED_USER", "COMPLETED", "ABORTED", "FAILED"]

ASTRA_ROUTES: tuple[Route, ...] = ("SOL", "LUNA", "REVIEW", "USER", "END")
SOL_STATUSES: tuple[SolStatus, ...] = (
    "DONE",
    "BLOCKED",
    "NEEDS_USER",
    "MILESTONE_COMPLETE",
    "FAILED",
)
REVIEWER_VERDICTS: tuple[ReviewerVerdict, ...] = (
    "PASS",
    "FAIL",
    "NEEDS_EVIDENCE",
)
LUNA_STATUSES: tuple[LunaStatus, ...] = (
    "DONE",
    "INSUFFICIENT_EVIDENCE",
    "BLOCKED",
    "FAILED",
)
USER_RESPONSE_STATUSES: tuple[UserResponseStatus, ...] = (
    "PROVIDED",
    "DECLINED",
    "CANCELLED",
)

DEFAULT_MAX_SOL_ATTEMPTS_PER_STAGE = 3
DEFAULT_MAX_REVIEW_ATTEMPTS_PER_STAGE = 3
DEFAULT_MAX_TRANSITIONS = 50
DEFAULT_MAX_MODEL_ERRORS = 3
SCHEMA_VERSION = 2


class EvidenceArtifact(TypedDict, total=False):
    """A path supplied as evidence, optionally pinned to exact bytes."""

    path: str
    sha256: str
    description: str


class RecoveryStrategy(TypedDict):
    """One bounded automatic-recovery strategy selected by Astra."""

    issue_id: str
    strategy_id: str
    attempt: int
    approach: str
    rationale: str
    difference_from_prior: str


class SolTask(TypedDict, total=False):
    """A bounded implementation assignment authored by Astra."""

    task_id: str
    stage_id: str
    plan_revision_id: str
    objective: str
    acceptance_criteria: list[str]
    constraints: list[str]
    targeted_checks: list[str]
    forbidden_areas: list[str]
    recovery_strategy: RecoveryStrategy


class SolResult(TypedDict, total=False):
    """Sol's factual report; it intentionally has no planning authority."""

    task_id: str
    stage_id: str
    plan_revision_id: str
    status: SolStatus
    summary: str
    evidence: list[str]
    evidence_artifact_paths: list[EvidenceArtifact]
    known_limitations: list[str]
    blockers: list[str]
    user_actions_requested: list[str]
    error: str


class LunaTask(TypedDict, total=False):
    """A bounded research assignment authored by Astra for a fresh Luna."""

    research_id: str
    stage_id: str
    plan_revision_id: str
    question: str
    scope: list[str]
    sources_to_consult: list[str]
    deliverables: list[str]
    constraints: list[str]
    recovery_strategy: RecoveryStrategy


class LunaResult(TypedDict, total=False):
    """Fresh Luna's factual research report with no management authority."""

    research_id: str
    stage_id: str
    plan_revision_id: str
    status: LunaStatus
    summary: str
    findings: list[str]
    sources: list[EvidenceArtifact]
    evidence_artifact_paths: list[EvidenceArtifact]
    known_limitations: list[str]
    blockers: list[str]
    error: str


class ReviewPackage(TypedDict, total=False):
    """Complete, self-contained context Astra gives a fresh Reviewer."""

    review_id: str
    package_id: str
    stage_id: str
    plan_revision_id: str
    ultimate_purpose: str
    project_root: str
    project_context: str
    milestone_goal: str
    acceptance_criteria: list[str]
    sol_instructions: SolTask
    actual_results: SolResult
    evidence_artifact_paths: list[EvidenceArtifact]
    known_limitations: list[str]
    current_state: dict[str, Any]
    proposed_next_plan: str


class ReviewerResult(TypedDict):
    """Read-only findings from one fresh Reviewer session."""

    review_id: str
    stage_id: str
    plan_revision_id: str
    ultimate_purpose: str
    verdict: ReviewerVerdict
    summary: str
    findings: list[str]
    evidence_inspected: list[str]
    missing_evidence: list[str]
    known_limitations: list[str]


class UserResponse(TypedDict, total=False):
    """A response to a graph pause; Astra decides how it affects routing."""

    request_id: str
    status: UserResponseStatus
    response: str
    evidence_artifact_paths: list[EvidenceArtifact]


class ExecutionLimits(TypedDict):
    max_sol_attempts_per_stage: int
    max_review_attempts_per_stage: int
    max_transitions: int
    max_model_errors: int


class StageSpec(TypedDict):
    id: str
    goal: str
    acceptance_criteria: list[str]
    requires_review: bool


class ActivePlan(TypedDict):
    """The mutable, versioned plan Astra currently manages."""

    revision_id: str
    current_stage: str
    project_context: str
    target_stage: str
    stages: list[StageSpec]
    experiment_plan: list[str]
    datasets: list[str]
    evaluation_criteria: list[str]
    approaches: list[str]


class PlanRevision(ActivePlan):
    """A full plan replacement that cannot replace the ultimate purpose."""

    summary: str
    rationale: str
    purpose_alignment: str
    preserves_ultimate_purpose: Literal[True]


class ProjectSpec(TypedDict):
    """Immutable user-approved orchestration boundary.

    ``ultimate_purpose`` is the immutable project-authority boundary. The
    remaining plan fields seed revision 1 and may later be replaced only by an
    Astra-authored ``PlanRevision`` that preserves that purpose.
    """

    project_id: str
    project_root: str
    project_context: str
    ultimate_purpose: str
    target_stage: str
    stages: list[StageSpec]
    limits: ExecutionLimits
    experiment_plan: list[str]
    datasets: list[str]
    evaluation_criteria: list[str]
    approaches: list[str]


class RetryCounts(TypedDict):
    sol_attempts_by_stage: dict[str, int]
    sol_attempts_by_task: dict[str, int]
    review_attempts_by_stage: dict[str, int]


class RecoveryAttempt(TypedDict):
    issue_id: str
    strategy_id: str
    attempt: int
    route: Literal["SOL", "LUNA"]
    approach: str
    rationale: str
    difference_from_prior: str
    outcome: str
    status: Literal["FAILED", "SUCCEEDED"]


class ReviewStateSnapshot(TypedDict):
    """Controller-defined state facts supplied to an isolated Reviewer."""

    active_plan: ActivePlan
    current_stage: str
    completed_stages: list[str]
    target_stage: str
    target_stage_completed: bool
    execution_limits: ExecutionLimits
    retry_counts: RetryCounts
    correction_required: bool
    correction_ready: bool
    last_sol_task_id: str
    last_sol_status: SolStatus


class StageTransition(TypedDict):
    transition_id: str
    from_stage: str
    to_stage: str | None
    based_on_review_id: str
    reason: str
    approved: Literal[True]


class ExhaustionAttempt(TypedDict):
    attempt: int
    strategy_id: str
    route: Literal["SOL", "LUNA"]
    approach: str
    outcome: str


class ExhaustionState(TypedDict):
    stage_id: str
    target_stage: str
    plan_revision_id: str
    summary: str


class ExhaustionContext(TypedDict):
    problem: str
    why_blocked: str
    attempts: list[ExhaustionAttempt]
    current_state: ExhaustionState
    risks_and_impact: str
    resume_after_user: str


class _UserRequestRequired(TypedDict):
    request_id: str
    category: Literal[
        "AUTH_OR_HUMAN_ACTION_REQUIRED",
        "PAID_RESOURCE_APPROVAL",
        "AUTOMATIC_RECOVERY_EXHAUSTED",
    ]
    prompt: str
    required_actions: list[str]


class UserRequest(_UserRequestRequired, total=False):
    exhaustion_context: ExhaustionContext


class AstraDecision(TypedDict, total=False):
    """Astra's sole routing and stage-control output."""

    decision_id: str
    route: Route
    reason: str
    proposed_next_plan: str
    stage_transition: StageTransition
    sol_task: SolTask
    luna_task: LunaTask
    review_package: ReviewPackage
    user_request: UserRequest
    plan_revision: PlanRevision
    end_reason: str
    target_stage_completed: Literal[True]


class _RequiredWorkflowState(TypedDict):
    schema_version: int
    run_id: str
    project_spec: ProjectSpec
    active_plan: ActivePlan
    plan_change_history: list[PlanRevision]
    execution_limits: ExecutionLimits
    route: Route
    status: WorkflowStatus
    current_stage: str
    completed_stages: list[str]
    step_count: int
    call_sequence: int
    model_error_count: int
    retry_counts: RetryCounts
    review_session_ids: list[str]
    luna_session_ids: list[str]
    recovery_attempts: list[RecoveryAttempt]
    transition_log: list[dict[str, Any]]
    paused: bool
    target_stage_completed: bool
    correction_required: bool
    correction_ready: bool
    must_pause_user: bool


class WorkflowState(_RequiredWorkflowState, total=False):
    """Checkpointed state sufficient to resume the same workflow safely.

    ``review_session_ids`` is append-only evidence that each review used a new
    session. ``transition_log`` records routed decisions and checkpoint event
    references. Optional fields hold the latest role outputs and resumable
    pause/error context.
    """

    astra_session_id: str | None
    sol_session_id: str | None
    astra_decision: AstraDecision | None
    pending_sol_task: SolTask | None
    last_sol_task: SolTask | None
    sol_result: SolResult | None
    pending_luna_task: LunaTask | None
    last_luna_task: LunaTask | None
    luna_result: LunaResult | None
    review_package: ReviewPackage | None
    reviewer_result: ReviewerResult | None
    pending_user_request: UserRequest | None
    user_response: UserResponse | None
    last_error: dict[str, Any] | None
    last_call_id: str
    recovery_issue_id: str | None


class ValidationError(ValueError):
    """Raised when a model-facing object violates its deterministic contract."""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_USER_REQUEST_CATEGORIES = {
    "AUTH_OR_HUMAN_ACTION_REQUIRED",
    "PAID_RESOURCE_APPROVAL",
    "AUTOMATIC_RECOVERY_EXHAUSTED",
}
_LIMIT_FIELDS = {
    "max_sol_attempts_per_stage",
    "max_review_attempts_per_stage",
    "max_transitions",
    "max_model_errors",
}
_AUTHORITY_FIELDS = {
    "advance",
    "approval",
    "approved",
    "completed_stages",
    "current_stage",
    "decision",
    "end_reason",
    "next_plan",
    "next_route",
    "next_stage",
    "plan",
    "plan_revision",
    "project_plan",
    "proposed_next_plan",
    "route",
    "stage_transition",
    "target_stage",
    "target_stage_completed",
}


def schema_path(name: str) -> Path:
    """Return the absolute path of a bundled model-output JSON Schema."""

    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValidationError("schema name must contain lowercase letters, digits, or underscores")
    path = Path(__file__).with_name("schemas") / f"{name}.schema.json"
    if not path.is_file():
        raise ValidationError(f"unknown schema: {name}")
    return path


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValidationError(f"{label} keys must be strings")
    return dict(value)


def _shape(
    value: object,
    *,
    label: str,
    required: set[str],
    optional: set[str] = frozenset(),
    reject_authority: bool = False,
) -> dict[str, object]:
    data = _object(value, label)
    missing = sorted(required - data.keys())
    if missing:
        raise ValidationError(f"{label} is missing required field(s): {', '.join(missing)}")
    unknown = set(data) - required - optional
    if reject_authority:
        authority = sorted(set(data) & _AUTHORITY_FIELDS)
        if authority:
            raise ValidationError(
                f"{label} cannot carry Astra authority field(s): {', '.join(authority)}"
            )
    if unknown:
        raise ValidationError(f"{label} has unknown field(s): {', '.join(sorted(unknown))}")
    return data


def _text(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValidationError(f"{path} must be {qualifier}")
    return value


def _identifier(value: object, path: str) -> str:
    identifier = _text(value, path)
    if not _SAFE_IDENTIFIER.fullmatch(identifier):
        raise ValidationError(
            f"{path} must start with an ASCII letter or digit and contain only "
            "ASCII letters, digits, dash, or underscore (maximum 128 characters)"
        )
    return identifier


def _strings(
    value: object, path: str, *, non_empty: bool = False
) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array of strings")
    result = [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if non_empty and not result:
        raise ValidationError(f"{path} must contain at least one item")
    return result


def _artifacts(value: object, path: str, *, non_empty: bool = False) -> list[EvidenceArtifact]:
    if not isinstance(value, list):
        raise ValidationError(f"{path} must be an array")
    result: list[EvidenceArtifact] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if isinstance(item, str):
            result.append({"path": _text(item, item_path)})
            continue
        artifact = _shape(
            item,
            label=item_path,
            required={"path"},
            optional={"sha256", "description"},
        )
        normalized: EvidenceArtifact = {"path": _text(artifact["path"], f"{item_path}.path")}
        if "sha256" in artifact:
            digest = _text(artifact["sha256"], f"{item_path}.sha256")
            if not _SHA256.fullmatch(digest):
                raise ValidationError(f"{item_path}.sha256 must be 64 lowercase hexadecimal characters")
            normalized["sha256"] = digest
        if "description" in artifact:
            normalized["description"] = _text(
                artifact["description"], f"{item_path}.description"
            )
        result.append(normalized)
    if non_empty and not result:
        raise ValidationError(f"{path} must contain at least one item")
    return result


def _positive_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(f"{path} must be a positive integer")
    return value


def _bounded_integer(value: object, path: str, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ValidationError(f"{path} must be an integer from {minimum} through {maximum}")
    return value


def _limits(value: object, path: str, *, partial: bool) -> dict[str, int]:
    data = _object(value, path)
    unknown = set(data) - _LIMIT_FIELDS
    if unknown:
        raise ValidationError(f"{path} has unknown field(s): {', '.join(sorted(unknown))}")
    if partial:
        if not data:
            raise ValidationError(f"{path} must contain at least one limit")
    else:
        missing = _LIMIT_FIELDS - data.keys()
        if missing:
            raise ValidationError(
                f"{path} is missing required field(s): {', '.join(sorted(missing))}"
            )
    return {
        key: _positive_integer(item, f"{path}.{key}") for key, item in data.items()
    }


def validate_recovery_strategy(value: object) -> RecoveryStrategy:
    data = _shape(
        value,
        label="RecoveryStrategy",
        required={
            "issue_id",
            "strategy_id",
            "attempt",
            "approach",
            "rationale",
            "difference_from_prior",
        },
    )
    return cast(
        RecoveryStrategy,
        {
            "issue_id": _identifier(data["issue_id"], "RecoveryStrategy.issue_id"),
            "strategy_id": _identifier(
                data["strategy_id"], "RecoveryStrategy.strategy_id"
            ),
            "attempt": _bounded_integer(
                data["attempt"], "RecoveryStrategy.attempt", minimum=1, maximum=5
            ),
            "approach": _text(data["approach"], "RecoveryStrategy.approach"),
            "rationale": _text(data["rationale"], "RecoveryStrategy.rationale"),
            "difference_from_prior": _text(
                data["difference_from_prior"],
                "RecoveryStrategy.difference_from_prior",
            ),
        },
    )


def validate_recovery_attempt(value: object) -> RecoveryAttempt:
    data = _shape(
        value,
        label="RecoveryAttempt",
        required={
            "issue_id",
            "strategy_id",
            "attempt",
            "route",
            "approach",
            "rationale",
            "difference_from_prior",
            "outcome",
            "status",
        },
    )
    route = _text(data["route"], "RecoveryAttempt.route")
    if route not in {"SOL", "LUNA"}:
        raise ValidationError("RecoveryAttempt.route must be SOL or LUNA")
    status = _text(data["status"], "RecoveryAttempt.status")
    if status not in {"FAILED", "SUCCEEDED"}:
        raise ValidationError("RecoveryAttempt.status must be FAILED or SUCCEEDED")
    return cast(
        RecoveryAttempt,
        {
            "issue_id": _identifier(data["issue_id"], "RecoveryAttempt.issue_id"),
            "strategy_id": _identifier(
                data["strategy_id"], "RecoveryAttempt.strategy_id"
            ),
            "attempt": _bounded_integer(
                data["attempt"], "RecoveryAttempt.attempt", minimum=1, maximum=5
            ),
            "route": route,
            "approach": _text(data["approach"], "RecoveryAttempt.approach"),
            "rationale": _text(data["rationale"], "RecoveryAttempt.rationale"),
            "difference_from_prior": _text(
                data["difference_from_prior"], "RecoveryAttempt.difference_from_prior"
            ),
            "outcome": _text(data["outcome"], "RecoveryAttempt.outcome"),
            "status": status,
        },
    )


def _stages(value: object, path: str) -> list[StageSpec]:
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{path} must be a non-empty array")
    stages: list[StageSpec] = []
    stage_ids: set[str] = set()
    for index, raw_stage in enumerate(value):
        label = f"{path}[{index}]"
        stage = _shape(
            raw_stage,
            label=label,
            required={"id", "goal", "acceptance_criteria", "requires_review"},
        )
        stage_id = _text(stage["id"], f"{label}.id")
        if stage_id in stage_ids:
            raise ValidationError(f"{path} contains duplicate id: {stage_id}")
        if not isinstance(stage["requires_review"], bool):
            raise ValidationError(f"{label}.requires_review must be a boolean")
        stage_ids.add(stage_id)
        stages.append(
            {
                "id": stage_id,
                "goal": _text(stage["goal"], f"{label}.goal"),
                "acceptance_criteria": _strings(
                    stage["acceptance_criteria"],
                    f"{label}.acceptance_criteria",
                    non_empty=True,
                ),
                "requires_review": stage["requires_review"],
            }
        )
    return stages


def _active_plan_fields(
    data: Mapping[str, object], path: str
) -> dict[str, object]:
    stages = _stages(data["stages"], f"{path}.stages")
    stage_ids = {stage["id"] for stage in stages}
    current_stage = _text(data["current_stage"], f"{path}.current_stage")
    target_stage = _text(data["target_stage"], f"{path}.target_stage")
    if current_stage not in stage_ids:
        raise ValidationError(f"{path}.current_stage must name a stage in {path}.stages")
    if target_stage not in stage_ids:
        raise ValidationError(f"{path}.target_stage must name a stage in {path}.stages")
    ordered_ids = [stage["id"] for stage in stages]
    if ordered_ids.index(target_stage) < ordered_ids.index(current_stage):
        raise ValidationError(
            f"{path}.target_stage must not precede {path}.current_stage"
        )
    return {
        "revision_id": _identifier(data["revision_id"], f"{path}.revision_id"),
        "current_stage": current_stage,
        "project_context": _text(data["project_context"], f"{path}.project_context"),
        "target_stage": target_stage,
        "stages": stages,
        "experiment_plan": _strings(data["experiment_plan"], f"{path}.experiment_plan"),
        "datasets": _strings(data["datasets"], f"{path}.datasets"),
        "evaluation_criteria": _strings(
            data["evaluation_criteria"], f"{path}.evaluation_criteria"
        ),
        "approaches": _strings(data["approaches"], f"{path}.approaches"),
    }


def validate_active_plan(value: object) -> ActivePlan:
    required = {
        "revision_id",
        "current_stage",
        "project_context",
        "target_stage",
        "stages",
        "experiment_plan",
        "datasets",
        "evaluation_criteria",
        "approaches",
    }
    data = _shape(value, label="ActivePlan", required=required)
    return cast(ActivePlan, _active_plan_fields(data, "ActivePlan"))


def validate_plan_revision(value: object) -> PlanRevision:
    required = {
        "revision_id",
        "current_stage",
        "project_context",
        "target_stage",
        "stages",
        "experiment_plan",
        "datasets",
        "evaluation_criteria",
        "approaches",
        "summary",
        "rationale",
        "purpose_alignment",
        "preserves_ultimate_purpose",
    }
    data = _shape(value, label="PlanRevision", required=required)
    if data["preserves_ultimate_purpose"] is not True:
        raise ValidationError("PlanRevision.preserves_ultimate_purpose must be true")
    return cast(
        PlanRevision,
        {
            **_active_plan_fields(data, "PlanRevision"),
            "summary": _text(data["summary"], "PlanRevision.summary"),
            "rationale": _text(data["rationale"], "PlanRevision.rationale"),
            "purpose_alignment": _text(
                data["purpose_alignment"], "PlanRevision.purpose_alignment"
            ),
            "preserves_ultimate_purpose": True,
        },
    )


def validate_project_spec(value: object) -> ProjectSpec:
    """Validate the immutable purpose and the initial mutable project plan."""

    data = _shape(
        value,
        label="ProjectSpec",
        required={
            "project_id",
            "project_root",
            "project_context",
            "ultimate_purpose",
            "target_stage",
            "stages",
            "limits",
        },
        optional={
            "experiment_plan",
            "datasets",
            "evaluation_criteria",
            "approaches",
        },
    )
    stages = _stages(data["stages"], "ProjectSpec.stages")
    stage_ids = {stage["id"] for stage in stages}
    target_stage = _text(data["target_stage"], "ProjectSpec.target_stage")
    if target_stage not in stage_ids:
        raise ValidationError("ProjectSpec.target_stage must name a stage in ProjectSpec.stages")
    project_root = str(
        Path(_text(data["project_root"], "ProjectSpec.project_root")).expanduser().resolve()
    )
    return cast(
        ProjectSpec,
        {
            "project_id": _text(data["project_id"], "ProjectSpec.project_id"),
            "project_root": project_root,
            "project_context": _text(
                data["project_context"], "ProjectSpec.project_context"
            ),
            "ultimate_purpose": _text(
                data["ultimate_purpose"], "ProjectSpec.ultimate_purpose"
            ),
            "target_stage": target_stage,
            "stages": stages,
            "limits": cast(ExecutionLimits, _limits(data["limits"], "ProjectSpec.limits", partial=False)),
            "experiment_plan": _strings(
                data.get("experiment_plan", []), "ProjectSpec.experiment_plan"
            ),
            "datasets": _strings(data.get("datasets", []), "ProjectSpec.datasets"),
            "evaluation_criteria": _strings(
                data.get("evaluation_criteria", []), "ProjectSpec.evaluation_criteria"
            ),
            "approaches": _strings(data.get("approaches", []), "ProjectSpec.approaches"),
        },
    )


def validate_sol_task(value: object) -> SolTask:
    data = _shape(
        value,
        label="SolTask",
        required={
            "task_id",
            "stage_id",
            "plan_revision_id",
            "objective",
            "acceptance_criteria",
            "constraints",
            "targeted_checks",
            "forbidden_areas",
        },
        optional={"recovery_strategy"},
    )
    return cast(
        SolTask,
        {
            "task_id": _text(data["task_id"], "SolTask.task_id"),
            "stage_id": _text(data["stage_id"], "SolTask.stage_id"),
            "plan_revision_id": _identifier(
                data["plan_revision_id"], "SolTask.plan_revision_id"
            ),
            "objective": _text(data["objective"], "SolTask.objective"),
            "acceptance_criteria": _strings(
                data["acceptance_criteria"],
                "SolTask.acceptance_criteria",
                non_empty=True,
            ),
            "constraints": _strings(data["constraints"], "SolTask.constraints"),
            "targeted_checks": _strings(
                data["targeted_checks"], "SolTask.targeted_checks"
            ),
            "forbidden_areas": _strings(
                data["forbidden_areas"], "SolTask.forbidden_areas"
            ),
            **(
                {"recovery_strategy": validate_recovery_strategy(data["recovery_strategy"])}
                if "recovery_strategy" in data
                else {}
            ),
        },
    )


def validate_sol_result(value: object) -> SolResult:
    data = _shape(
        value,
        label="SolResult",
        required={
            "task_id",
            "stage_id",
            "plan_revision_id",
            "status",
            "summary",
            "evidence",
            "evidence_artifact_paths",
            "known_limitations",
            "blockers",
            "user_actions_requested",
        },
        optional={"error"},
        reject_authority=True,
    )
    status = _text(data["status"], "SolResult.status")
    if status not in SOL_STATUSES:
        raise ValidationError(f"SolResult.status must be one of: {', '.join(SOL_STATUSES)}")
    blockers = _strings(data["blockers"], "SolResult.blockers")
    user_actions = _strings(
        data["user_actions_requested"], "SolResult.user_actions_requested"
    )
    artifacts = _artifacts(
        data["evidence_artifact_paths"], "SolResult.evidence_artifact_paths"
    )
    error = _text(data["error"], "SolResult.error") if "error" in data else None
    if status in {"DONE", "MILESTONE_COMPLETE"} and (blockers or user_actions or error):
        raise ValidationError(f"SolResult.status {status} cannot include blockers, user actions, or error")
    if status == "MILESTONE_COMPLETE" and not artifacts:
        raise ValidationError("MILESTONE_COMPLETE requires at least one evidence artifact path")
    if status == "BLOCKED" and (not blockers or user_actions or error):
        raise ValidationError("BLOCKED requires blockers and cannot include user actions or error")
    if status == "NEEDS_USER" and (not user_actions or error):
        raise ValidationError("NEEDS_USER requires user_actions_requested and cannot include error")
    if status == "FAILED" and (not error or user_actions):
        raise ValidationError("FAILED requires error and cannot include user_actions_requested")
    normalized: dict[str, object] = {
        "task_id": _text(data["task_id"], "SolResult.task_id"),
        "stage_id": _text(data["stage_id"], "SolResult.stage_id"),
        "plan_revision_id": _identifier(
            data["plan_revision_id"], "SolResult.plan_revision_id"
        ),
        "status": status,
        "summary": _text(data["summary"], "SolResult.summary"),
        "evidence": _strings(data["evidence"], "SolResult.evidence"),
        "evidence_artifact_paths": artifacts,
        "known_limitations": _strings(
            data["known_limitations"], "SolResult.known_limitations"
        ),
        "blockers": blockers,
        "user_actions_requested": user_actions,
    }
    if error is not None:
        normalized["error"] = error
    return cast(SolResult, normalized)


def validate_luna_task(value: object) -> LunaTask:
    data = _shape(
        value,
        label="LunaTask",
        required={
            "research_id",
            "stage_id",
            "plan_revision_id",
            "question",
            "scope",
            "sources_to_consult",
            "deliverables",
            "constraints",
        },
        optional={"recovery_strategy"},
    )
    normalized: dict[str, object] = {
        "research_id": _identifier(data["research_id"], "LunaTask.research_id"),
        "stage_id": _text(data["stage_id"], "LunaTask.stage_id"),
        "plan_revision_id": _identifier(
            data["plan_revision_id"], "LunaTask.plan_revision_id"
        ),
        "question": _text(data["question"], "LunaTask.question"),
        "scope": _strings(data["scope"], "LunaTask.scope", non_empty=True),
        "sources_to_consult": _strings(
            data["sources_to_consult"], "LunaTask.sources_to_consult"
        ),
        "deliverables": _strings(
            data["deliverables"], "LunaTask.deliverables", non_empty=True
        ),
        "constraints": _strings(data["constraints"], "LunaTask.constraints"),
    }
    if "recovery_strategy" in data:
        normalized["recovery_strategy"] = validate_recovery_strategy(
            data["recovery_strategy"]
        )
    return cast(LunaTask, normalized)


def validate_luna_result(value: object) -> LunaResult:
    data = _shape(
        value,
        label="LunaResult",
        required={
            "research_id",
            "stage_id",
            "plan_revision_id",
            "status",
            "summary",
            "findings",
            "sources",
            "evidence_artifact_paths",
            "known_limitations",
            "blockers",
        },
        optional={"error"},
        reject_authority=True,
    )
    status = _text(data["status"], "LunaResult.status")
    if status not in LUNA_STATUSES:
        raise ValidationError(
            f"LunaResult.status must be one of: {', '.join(LUNA_STATUSES)}"
        )
    findings = _strings(data["findings"], "LunaResult.findings")
    sources = _artifacts(data["sources"], "LunaResult.sources")
    limitations = _strings(
        data["known_limitations"], "LunaResult.known_limitations"
    )
    blockers = _strings(data["blockers"], "LunaResult.blockers")
    error = _text(data["error"], "LunaResult.error") if "error" in data else None
    if status == "DONE" and (not findings or not sources or blockers or error):
        raise ValidationError(
            "LunaResult.status DONE requires findings and sources and cannot include blockers or error"
        )
    if status == "INSUFFICIENT_EVIDENCE" and (not limitations and not blockers):
        raise ValidationError(
            "INSUFFICIENT_EVIDENCE requires known limitations or blockers"
        )
    if status == "INSUFFICIENT_EVIDENCE" and error:
        raise ValidationError("INSUFFICIENT_EVIDENCE cannot include error")
    if status == "BLOCKED" and (not blockers or error):
        raise ValidationError("BLOCKED requires blockers and cannot include error")
    if status == "FAILED" and not error:
        raise ValidationError("FAILED requires error")
    normalized: dict[str, object] = {
        "research_id": _identifier(data["research_id"], "LunaResult.research_id"),
        "stage_id": _text(data["stage_id"], "LunaResult.stage_id"),
        "plan_revision_id": _identifier(
            data["plan_revision_id"], "LunaResult.plan_revision_id"
        ),
        "status": status,
        "summary": _text(data["summary"], "LunaResult.summary"),
        "findings": findings,
        "sources": sources,
        "evidence_artifact_paths": _artifacts(
            data["evidence_artifact_paths"], "LunaResult.evidence_artifact_paths"
        ),
        "known_limitations": limitations,
        "blockers": blockers,
    }
    if error is not None:
        normalized["error"] = error
    return cast(LunaResult, normalized)


def _nonnegative_counts(value: object, path: str) -> dict[str, int]:
    data = _object(value, path)
    normalized: dict[str, int] = {}
    for key, item in data.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValidationError(f"{path}.{key} must be a non-negative integer")
        normalized[_text(key, f"{path} key")] = item
    return normalized


def _review_state(value: object) -> ReviewStateSnapshot:
    data = _shape(
        value,
        label="ReviewStateSnapshot",
        required={
            "active_plan",
            "current_stage",
            "completed_stages",
            "target_stage",
            "target_stage_completed",
            "execution_limits",
            "retry_counts",
            "correction_required",
            "correction_ready",
            "last_sol_task_id",
            "last_sol_status",
        },
    )
    retry_data = _shape(
        data["retry_counts"],
        label="ReviewStateSnapshot.retry_counts",
        required={
            "sol_attempts_by_stage",
            "sol_attempts_by_task",
            "review_attempts_by_stage",
        },
    )
    for field in ("target_stage_completed", "correction_required", "correction_ready"):
        if not isinstance(data[field], bool):
            raise ValidationError(f"ReviewStateSnapshot.{field} must be a boolean")
    status = _text(data["last_sol_status"], "ReviewStateSnapshot.last_sol_status")
    if status not in SOL_STATUSES:
        raise ValidationError(
            f"ReviewStateSnapshot.last_sol_status must be one of: {', '.join(SOL_STATUSES)}"
        )
    return cast(
        ReviewStateSnapshot,
        {
            "active_plan": validate_active_plan(data["active_plan"]),
            "current_stage": _text(data["current_stage"], "ReviewStateSnapshot.current_stage"),
            "completed_stages": _strings(
                data["completed_stages"], "ReviewStateSnapshot.completed_stages"
            ),
            "target_stage": _text(data["target_stage"], "ReviewStateSnapshot.target_stage"),
            "target_stage_completed": data["target_stage_completed"],
            "execution_limits": cast(
                ExecutionLimits,
                _limits(data["execution_limits"], "ReviewStateSnapshot.execution_limits", partial=False),
            ),
            "retry_counts": cast(
                RetryCounts,
                {
                    "sol_attempts_by_stage": _nonnegative_counts(
                        retry_data["sol_attempts_by_stage"],
                        "ReviewStateSnapshot.retry_counts.sol_attempts_by_stage",
                    ),
                    "sol_attempts_by_task": _nonnegative_counts(
                        retry_data["sol_attempts_by_task"],
                        "ReviewStateSnapshot.retry_counts.sol_attempts_by_task",
                    ),
                    "review_attempts_by_stage": _nonnegative_counts(
                        retry_data["review_attempts_by_stage"],
                        "ReviewStateSnapshot.retry_counts.review_attempts_by_stage",
                    ),
                },
            ),
            "correction_required": data["correction_required"],
            "correction_ready": data["correction_ready"],
            "last_sol_task_id": _text(
                data["last_sol_task_id"], "ReviewStateSnapshot.last_sol_task_id"
            ),
            "last_sol_status": status,
        },
    )


def validate_review_package(value: object) -> ReviewPackage:
    data = _shape(
        value,
        label="ReviewPackage",
        required={
            "review_id",
            "stage_id",
            "plan_revision_id",
            "ultimate_purpose",
            "project_root",
            "project_context",
            "milestone_goal",
            "acceptance_criteria",
            "sol_instructions",
            "actual_results",
            "evidence_artifact_paths",
            "known_limitations",
            "current_state",
            "proposed_next_plan",
        },
        optional={"package_id"},
    )
    review_id = _identifier(data["review_id"], "ReviewPackage.review_id")
    package_id = (
        _identifier(data["package_id"], "ReviewPackage.package_id")
        if "package_id" in data
        else None
    )
    if package_id is not None and package_id != review_id:
        raise ValidationError("ReviewPackage.package_id alias must equal review_id")
    stage_id = _text(data["stage_id"], "ReviewPackage.stage_id")
    plan_revision_id = _identifier(
        data["plan_revision_id"], "ReviewPackage.plan_revision_id"
    )
    task = validate_sol_task(data["sol_instructions"])
    result = validate_sol_result(data["actual_results"])
    if task["stage_id"] != stage_id or result["stage_id"] != stage_id:
        raise ValidationError("ReviewPackage stage_id must match SolTask and SolResult stage_id")
    if task["task_id"] != result["task_id"]:
        raise ValidationError("ReviewPackage SolTask and SolResult task_id must match")
    if (
        task["plan_revision_id"] != plan_revision_id
        or result["plan_revision_id"] != plan_revision_id
    ):
        raise ValidationError(
            "ReviewPackage plan_revision_id must match SolTask and SolResult plan_revision_id"
        )
    if result["status"] != "MILESTONE_COMPLETE":
        raise ValidationError("ReviewPackage actual_results must have MILESTONE_COMPLETE status")
    normalized: dict[str, object] = {
        "review_id": review_id,
        "stage_id": stage_id,
        "plan_revision_id": plan_revision_id,
        "ultimate_purpose": _text(
            data["ultimate_purpose"], "ReviewPackage.ultimate_purpose"
        ),
        "project_root": _text(data["project_root"], "ReviewPackage.project_root"),
        "project_context": _text(
            data["project_context"], "ReviewPackage.project_context"
        ),
        "milestone_goal": _text(
            data["milestone_goal"], "ReviewPackage.milestone_goal"
        ),
        "acceptance_criteria": _strings(
            data["acceptance_criteria"],
            "ReviewPackage.acceptance_criteria",
            non_empty=True,
        ),
        "sol_instructions": task,
        "actual_results": result,
        "evidence_artifact_paths": _artifacts(
            data["evidence_artifact_paths"],
            "ReviewPackage.evidence_artifact_paths",
            non_empty=True,
        ),
        "known_limitations": _strings(
            data["known_limitations"], "ReviewPackage.known_limitations"
        ),
        "current_state": _review_state(data["current_state"]),
        "proposed_next_plan": _text(
            data["proposed_next_plan"], "ReviewPackage.proposed_next_plan"
        ),
    }
    if package_id is not None:
        normalized["package_id"] = package_id
    return cast(ReviewPackage, normalized)


def validate_reviewer_result(value: object) -> ReviewerResult:
    data = _shape(
        value,
        label="ReviewerResult",
        required={
            "review_id",
            "stage_id",
            "plan_revision_id",
            "ultimate_purpose",
            "verdict",
            "summary",
            "findings",
            "evidence_inspected",
            "missing_evidence",
            "known_limitations",
        },
        reject_authority=True,
    )
    verdict = _text(data["verdict"], "ReviewerResult.verdict")
    if verdict not in REVIEWER_VERDICTS:
        raise ValidationError(
            f"ReviewerResult.verdict must be one of: {', '.join(REVIEWER_VERDICTS)}"
        )
    findings = _strings(data["findings"], "ReviewerResult.findings")
    inspected = _strings(
        data["evidence_inspected"], "ReviewerResult.evidence_inspected"
    )
    missing = _strings(data["missing_evidence"], "ReviewerResult.missing_evidence")
    if verdict == "PASS" and (not inspected or missing):
        raise ValidationError("PASS requires inspected evidence and no missing evidence")
    if verdict == "FAIL" and (not findings or not inspected):
        raise ValidationError("FAIL requires findings and inspected evidence")
    if verdict == "NEEDS_EVIDENCE" and not missing:
        raise ValidationError("NEEDS_EVIDENCE requires missing_evidence")
    return cast(
        ReviewerResult,
        {
            "review_id": _identifier(data["review_id"], "ReviewerResult.review_id"),
            "stage_id": _text(data["stage_id"], "ReviewerResult.stage_id"),
            "plan_revision_id": _identifier(
                data["plan_revision_id"], "ReviewerResult.plan_revision_id"
            ),
            "ultimate_purpose": _text(
                data["ultimate_purpose"], "ReviewerResult.ultimate_purpose"
            ),
            "verdict": verdict,
            "summary": _text(data["summary"], "ReviewerResult.summary"),
            "findings": findings,
            "evidence_inspected": inspected,
            "missing_evidence": missing,
            "known_limitations": _strings(
                data["known_limitations"], "ReviewerResult.known_limitations"
            ),
        },
    )


def _validate_user_request(value: object) -> UserRequest:
    data = _shape(
        value,
        label="UserRequest",
        required={"request_id", "category", "prompt", "required_actions"},
        optional={"exhaustion_context"},
    )
    category = _text(data["category"], "UserRequest.category")
    if category not in _USER_REQUEST_CATEGORIES:
        raise ValidationError(
            "UserRequest.category must identify a supported human-intervention reason"
        )
    prompt = _text(data["prompt"], "UserRequest.prompt")
    actions = _strings(data["required_actions"], "UserRequest.required_actions", non_empty=True)
    normalized: UserRequest = {
        "request_id": _text(data["request_id"], "UserRequest.request_id"),
        "category": cast(Any, category),
        "prompt": prompt,
        "required_actions": actions,
    }
    if category != "AUTOMATIC_RECOVERY_EXHAUSTED":
        if "exhaustion_context" in data:
            raise ValidationError("exhaustion_context is only valid for automatic recovery exhaustion")
        return normalized
    if "exhaustion_context" not in data:
        raise ValidationError("AUTOMATIC_RECOVERY_EXHAUSTED requires exhaustion_context")
    context_data = _shape(
        data["exhaustion_context"],
        label="UserRequest.exhaustion_context",
        required={
            "problem", "why_blocked", "attempts", "current_state",
            "risks_and_impact", "resume_after_user",
        },
    )
    attempts_value = context_data["attempts"]
    if not isinstance(attempts_value, list) or len(attempts_value) != 5:
        raise ValidationError("exhaustion_context.attempts must contain exactly five attempts")
    attempts: list[ExhaustionAttempt] = []
    for index, value in enumerate(attempts_value, 1):
        item = _shape(
            value,
            label=f"exhaustion_context.attempts[{index - 1}]",
            required={"attempt", "strategy_id", "route", "approach", "outcome"},
        )
        if type(item["attempt"]) is not int or item["attempt"] != index:
            raise ValidationError("exhaustion_context.attempts must be numbered 1 through 5")
        route = _text(item["route"], f"exhaustion_context.attempts[{index - 1}].route")
        if route not in {"SOL", "LUNA"}:
            raise ValidationError("exhaustion_context attempt route must be SOL or LUNA")
        attempts.append({
            "attempt": index,
            "strategy_id": _identifier(item["strategy_id"], "exhaustion_context.strategy_id"),
            "route": cast(Any, route),
            "approach": _text(item["approach"], "exhaustion_context.approach"),
            "outcome": _text(item["outcome"], "exhaustion_context.outcome"),
        })
    state_data = _shape(
        context_data["current_state"],
        label="exhaustion_context.current_state",
        required={"stage_id", "target_stage", "plan_revision_id", "summary"},
    )
    current_state: ExhaustionState = {
        "stage_id": _text(state_data["stage_id"], "exhaustion_context.current_state.stage_id"),
        "target_stage": _text(state_data["target_stage"], "exhaustion_context.current_state.target_stage"),
        "plan_revision_id": _text(state_data["plan_revision_id"], "exhaustion_context.current_state.plan_revision_id"),
        "summary": _text(state_data["summary"], "exhaustion_context.current_state.summary"),
    }
    context: ExhaustionContext = {
        "problem": _text(context_data["problem"], "exhaustion_context.problem"),
        "why_blocked": _text(context_data["why_blocked"], "exhaustion_context.why_blocked"),
        "attempts": attempts,
        "current_state": current_state,
        "risks_and_impact": _text(context_data["risks_and_impact"], "exhaustion_context.risks_and_impact"),
        "resume_after_user": _text(context_data["resume_after_user"], "exhaustion_context.resume_after_user"),
    }
    attempt_lines = [
        f"{item['attempt']}. {item['route']} — {item['approach']}: {item['outcome']}"
        for item in attempts
    ]
    normalized["exhaustion_context"] = context
    normalized["prompt"] = "\n".join([
        "Automatic recovery is exhausted; user action is required.",
        "", "Problem: " + context["problem"],
        "Why blocked: " + context["why_blocked"],
        "What Astra tried (all failed):", *attempt_lines,
        "Current state: stage " + current_state["stage_id"]
        + ", target " + current_state["target_stage"]
        + ", plan " + current_state["plan_revision_id"]
        + ". " + current_state["summary"],
        "USER must do or provide:", *["- " + action for action in actions],
        "Risks/impact: " + context["risks_and_impact"],
        "After your response: the same checkpoint resumes at Astra. "
        + context["resume_after_user"],
    ])
    return normalized


def _validate_stage_transition(value: object) -> StageTransition:
    data = _shape(
        value,
        label="StageTransition",
        required={
            "transition_id",
            "from_stage",
            "to_stage",
            "based_on_review_id",
            "reason",
            "approved",
        },
    )
    from_stage = _text(data["from_stage"], "StageTransition.from_stage")
    to_stage = (
        None
        if data["to_stage"] is None
        else _text(data["to_stage"], "StageTransition.to_stage")
    )
    if to_stage is not None and from_stage == to_stage:
        raise ValidationError("StageTransition must change the stage")
    if data["approved"] is not True:
        raise ValidationError("StageTransition.approved must be true")
    return cast(
        StageTransition,
        {
            "transition_id": _text(
                data["transition_id"], "StageTransition.transition_id"
            ),
            "from_stage": from_stage,
            "to_stage": to_stage,
            "based_on_review_id": _identifier(
                data["based_on_review_id"], "StageTransition.based_on_review_id"
            ),
            "reason": _text(data["reason"], "StageTransition.reason"),
            "approved": True,
        },
    )


def validate_astra_decision(value: object) -> AstraDecision:
    data = _shape(
        value,
        label="AstraDecision",
        required={"decision_id", "route", "reason"},
        optional={
            "proposed_next_plan",
            "stage_transition",
            "sol_task",
            "luna_task",
            "review_package",
            "user_request",
            "plan_revision",
            "end_reason",
            "target_stage_completed",
        },
    )
    route = _text(data["route"], "AstraDecision.route")
    if route not in ASTRA_ROUTES:
        raise ValidationError(f"AstraDecision.route must be one of: {', '.join(ASTRA_ROUTES)}")
    route_fields = {
        "SOL": {"sol_task"},
        "LUNA": {"luna_task"},
        "REVIEW": {"review_package"},
        "USER": {"user_request"},
        "END": {"end_reason", "target_stage_completed"},
    }
    selected = route_fields[route]
    absent = selected - data.keys()
    if absent:
        raise ValidationError(
            f"AstraDecision route {route} requires: {', '.join(sorted(absent))}"
        )
    all_route_fields = set().union(*route_fields.values())
    conflicting = (set(data) & all_route_fields) - selected
    if conflicting:
        raise ValidationError(
            f"AstraDecision route {route} forbids: {', '.join(sorted(conflicting))}"
        )
    normalized: dict[str, object] = {
        "decision_id": _text(data["decision_id"], "AstraDecision.decision_id"),
        "route": route,
        "reason": _text(data["reason"], "AstraDecision.reason"),
    }
    if "proposed_next_plan" in data:
        normalized["proposed_next_plan"] = _text(
            data["proposed_next_plan"], "AstraDecision.proposed_next_plan"
        )
    if "stage_transition" in data:
        normalized["stage_transition"] = _validate_stage_transition(data["stage_transition"])
    if "plan_revision" in data:
        normalized["plan_revision"] = validate_plan_revision(data["plan_revision"])
    if route == "SOL":
        normalized["sol_task"] = validate_sol_task(data["sol_task"])
    elif route == "LUNA":
        normalized["luna_task"] = validate_luna_task(data["luna_task"])
    elif route == "REVIEW":
        normalized["review_package"] = validate_review_package(data["review_package"])
    elif route == "USER":
        normalized["user_request"] = _validate_user_request(data["user_request"])
    else:
        normalized["end_reason"] = _text(data["end_reason"], "AstraDecision.end_reason")
        if data["target_stage_completed"] is not True:
            raise ValidationError("AstraDecision END requires target_stage_completed=true")
        normalized["target_stage_completed"] = True
    return cast(AstraDecision, normalized)


def validate_user_response(value: object) -> UserResponse:
    data = _shape(
        value,
        label="UserResponse",
        required={"request_id", "status", "response", "evidence_artifact_paths"},
        reject_authority=True,
    )
    status = _text(data["status"], "UserResponse.status")
    if status not in USER_RESPONSE_STATUSES:
        raise ValidationError(
            f"UserResponse.status must be one of: {', '.join(USER_RESPONSE_STATUSES)}"
        )
    response = _text(data["response"], "UserResponse.response", allow_empty=True)
    artifacts = _artifacts(
        data["evidence_artifact_paths"], "UserResponse.evidence_artifact_paths"
    )
    if status == "PROVIDED" and not response.strip() and not artifacts:
        raise ValidationError("PROVIDED requires a response or evidence artifact path")
    if status in {"DECLINED", "CANCELLED"} and not response.strip():
        raise ValidationError(f"{status} requires a non-empty response")
    normalized: dict[str, object] = {
        "request_id": _text(data["request_id"], "UserResponse.request_id"),
        "status": status,
        "response": response,
        "evidence_artifact_paths": artifacts,
    }
    return cast(UserResponse, normalized)
