"""M7.1 evidence contracts for decision boundaries, timing, and perspective safety.

This module records source evidence and fail-closed policy constraints. It does not
emit decision samples or infer actor/action semantics from field names.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from openmtgdata.archive_registration import SourceArchiveRecordV1
from openmtgdata.deep_inspection import classify_lexeme
from openmtgdata.replay_schema import ReplayFieldMappingV1
from openmtgdata.schema_evolution import FieldSurfaceV1, RawSchemaGroupEvidenceV1
from openmtgdata.source_reader import SourceReaderSummaryV1, VerifiedSchemaRegistryV1

DECISION_EVIDENCE_REPORT_CONTRACT_ID = "openmtgdata.decision-semantics-evidence.v1"
DECISION_FIELD_SAFETY_CONTRACT_ID = "openmtgdata.decision-field-safety.v1"
DECISION_TYPE_EVIDENCE_CONTRACT_ID = "openmtgdata.decision-type-evidence.v1"
PERSPECTIVE_EVIDENCE_CONTRACT_ID = "openmtgdata.perspective-evidence.v1"
TEMPORAL_SAFETY_CONTRACT_ID = "openmtgdata.temporal-safety.v1"
ANNOTATED_EXAMPLE_CONTRACT_ID = "openmtgdata.decision-annotated-example.v1"
DECISION_REPORT_DIGEST_CONTRACT_ID = "openmtgdata.decision-evidence-report-digest.v1"
DECISION_SEMANTICS_POLICY_ID = "openmtgdata.replay-decision-semantics-policy.v1"
_REPORT_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TURN_FIELD = re.compile(r"(user|oppo)_turn_([1-9][0-9]*)_(.+)\Z")
_NONSEMANTIC_KEY = re.compile(
    r"(?:^|_)(?:path|timestamp|created_at|updated_at|hostname|wall_clock|runtime|duration)(?:_|$)",
    re.IGNORECASE,
)
_ABSOLUTE_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:\\|/(?:home|Users|tmp)/)")


class DecisionEvidenceError(ValueError):
    """Invalid or internally inconsistent M7.1 evidence."""


class TimingStatus(StrEnum):
    PRE_TURN = "pre_turn"
    TURN_START = "turn_start"
    WITHIN_TURN_UNKNOWN = "within_turn_unknown"
    POST_ACTION = "post_action"
    END_OF_TURN = "end_of_turn"
    POST_TURN = "post_turn"
    GAME_LEVEL = "game_level"
    UNKNOWN_TIMING = "unknown_timing"


class VisibilityStatus(StrEnum):
    PUBLIC = "public"
    SOURCE_USER_PRIVATE = "source_user_private"
    SOURCE_OPPO_PRIVATE = "source_oppo_private"
    SOURCE_COMPLETE_INFORMATION = "source_complete_information"
    UNKNOWN_VISIBILITY = "unknown_visibility"
    NOT_APPLICABLE = "not_applicable"


class PolicyInputStatus(StrEnum):
    SAFE_IF_ACTOR_USER = "safe_if_actor_user"
    SAFE_IF_ACTOR_OPPO = "safe_if_actor_oppo"
    SAFE_PUBLIC = "safe_public"
    POST_DECISION_ONLY = "post_decision_only"
    HIDDEN_INFO_UNSAFE = "hidden_info_unsafe"
    TIMING_AMBIGUOUS = "timing_ambiguous"
    ACTOR_DEPENDENT = "actor_dependent"
    UNSUPPORTED = "unsupported"


class ActionStatus(StrEnum):
    NOT_ACTION = "not_action"
    ACTION_CANDIDATE = "action_candidate"
    ACTION_AGGREGATE = "action_aggregate"
    ACTION_SEQUENCE_CANDIDATE = "action_sequence_candidate"
    DECISION_BOUNDARY_SUPPORTED = "decision_boundary_supported"
    DECISION_BOUNDARY_UNRESOLVED = "decision_boundary_unresolved"


class PerspectiveStatus(StrEnum):
    ACTOR_SOURCE_USER = "actor_source_user"
    ACTOR_SOURCE_OPPO = "actor_source_oppo"
    ACTOR_DERIVABLE = "actor_derivable"
    ACTOR_AMBIGUOUS = "actor_ambiguous"
    ACTOR_UNKNOWN = "actor_unknown"


class BeforeStateStatus(StrEnum):
    EXACT_BEFORE_STATE = "exact_before_state"
    TURN_START_STATE = "turn_start_state"
    PREVIOUS_TURN_END_STATE = "previous_turn_end_state"
    PARTIAL_BEFORE_STATE = "partial_before_state"
    TURN_AGGREGATE_ONLY = "turn_aggregate_only"
    NO_BEFORE_STATE = "no_before_state"


class TargetStatus(StrEnum):
    ACTION_TARGET_SUPPORTED = "action_target_supported"
    ACTION_TARGET_UNSUPPORTED = "action_target_unsupported"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class LeakageStatus(StrEnum):
    PRE_DECISION_SAFE = "pre_decision_safe"
    POST_DECISION = "post_decision"
    FUTURE_TURN = "future_turn"
    GAME_OUTCOME = "game_outcome"
    TIMING_UNKNOWN = "timing_unknown"


class DecisionTypeDisposition(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED_ACTOR = "unsupported_actor"
    UNSUPPORTED_TIMING = "unsupported_timing"
    UNSUPPORTED_VISIBILITY = "unsupported_visibility"
    UNSUPPORTED_ACTION_STRUCTURE = "unsupported_action_structure"
    UNSUPPORTED_BEFORE_STATE = "unsupported_before_state"
    UNSUPPORTED_TARGET_SEMANTICS = "unsupported_target_semantics"


class OverallDecisionStatus(StrEnum):
    DECISION_EXTRACTION_SUPPORTED = "decision_extraction_supported"
    PARTIAL_DECISION_EXTRACTION_SUPPORTED = "partial_decision_extraction_supported"
    TURN_SUMMARY_ONLY = "turn_summary_only"
    ACTOR_UNRESOLVED = "actor_unresolved"
    TEMPORAL_BOUNDARY_UNRESOLVED = "temporal_boundary_unresolved"
    UNSAFE_FOR_BEHAVIOR_CLONING = "unsafe_for_behavior_cloning"
    ANALYSIS_INCOMPLETE = "analysis_incomplete"


class ReconstructionEvidenceLevel(StrEnum):
    SOURCE_DIRECT = "source_direct"
    DETERMINISTIC_WITHIN_SOURCE = "deterministic_within_source"
    PARTIAL_RECONSTRUCTION = "partial_reconstruction"
    ORDER_UNRESOLVED = "order_unresolved"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class DecisionFieldReferenceV1:
    column_index: int
    exact_header_name: str

    def __post_init__(self) -> None:
        if self.column_index < 0:
            raise DecisionEvidenceError("field selector index must be nonnegative")

    def to_dict(self) -> dict[str, object]:
        return {
            "column_index": self.column_index,
            "exact_header_name": self.exact_header_name,
            "field_reference_contract_id": "openmtgdata.decision-field-reference.v1",
        }


@dataclass(frozen=True, slots=True)
class DecisionFieldSafetyV1:
    selector: DecisionFieldReferenceV1
    structural_family: str
    source_side: str | None
    turn_slot_index: int | None
    exact_suffix: str | None
    m5_mapping_status: str
    m3_lexical_class_counts: tuple[tuple[str, int], ...]
    m3_empty_field_observed: bool
    m3_evidence_scope: str
    candidate_semantic_category: str
    timing_status: TimingStatus
    visibility_status: VisibilityStatus
    policy_input_status: PolicyInputStatus
    action_status: ActionStatus
    perspective_status: PerspectiveStatus
    before_state_status: BeforeStateStatus
    action_target_status: TargetStatus
    leakage_status: LeakageStatus
    source_side_private_candidate: bool
    end_of_turn_name_candidate_only: bool
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action_status": self.action_status.value,
            "action_target_status": self.action_target_status.value,
            "before_state_status": self.before_state_status.value,
            "candidate_semantic_category": self.candidate_semantic_category,
            "end_of_turn_name_candidate_only": self.end_of_turn_name_candidate_only,
            "exact_suffix": self.exact_suffix,
            "field_safety_contract_id": DECISION_FIELD_SAFETY_CONTRACT_ID,
            "leakage_status": self.leakage_status.value,
            "m3_evidence_scope": self.m3_evidence_scope,
            "m3_empty_field_observed": self.m3_empty_field_observed,
            "m3_lexical_class_counts": dict(self.m3_lexical_class_counts),
            "m5_mapping_status": self.m5_mapping_status,
            "perspective_status": self.perspective_status.value,
            "policy_input_status": self.policy_input_status.value,
            "rationale": self.rationale,
            "selector": self.selector.to_dict(),
            "source_side": self.source_side,
            "source_side_private_candidate": self.source_side_private_candidate,
            "structural_family": self.structural_family,
            "timing_status": self.timing_status.value,
            "turn_slot_index": self.turn_slot_index,
            "visibility_status": self.visibility_status.value,
        }


@dataclass(frozen=True, slots=True)
class TurnFamilyEvidenceV1:
    exact_suffix: str
    slot_count_per_side: int
    sides_present: tuple[str, ...]
    m3_cells_observed: int
    m3_empty_field_count: int
    m3_non_empty_field_count: int
    m3_empty_rate_percent: float
    m3_lexical_class_counts: tuple[tuple[str, int], ...]
    m4_bounded_cells_observed: int
    m4_bounded_empty_field_count: int
    m4_bounded_non_empty_field_count: int
    m4_bounded_empty_rate_percent: float
    m4_bounded_empty_slot_count: int
    m4_bounded_non_empty_slot_count: int
    m4_pipe_character_observed_count: int
    m4_comma_character_observed_count: int
    m4_semicolon_character_observed_count: int
    m4_newline_character_observed_count: int
    m4_edge_whitespace_observed_count: int
    minimum_non_empty_character_length: int | None
    maximum_non_empty_character_length: int | None
    bounded_nonempty_same_slot_cooccurrence: tuple[tuple[str, int], ...]
    evidence_scope: str
    timing_status: TimingStatus
    visibility_status: VisibilityStatus
    action_status: ActionStatus
    policy_input_status: PolicyInputStatus
    order_status: str
    semantic_limit: str

    def to_dict(self) -> dict[str, object]:
        return {
            "action_status": self.action_status.value,
            "evidence_scope": self.evidence_scope,
            "exact_suffix": self.exact_suffix,
            "m3_cells_observed": self.m3_cells_observed,
            "m3_empty_field_count": self.m3_empty_field_count,
            "m3_empty_rate_percent": self.m3_empty_rate_percent,
            "m3_lexical_class_counts": dict(self.m3_lexical_class_counts),
            "m3_non_empty_field_count": self.m3_non_empty_field_count,
            "m4_bounded_cells_observed": self.m4_bounded_cells_observed,
            "m4_bounded_empty_field_count": self.m4_bounded_empty_field_count,
            "m4_bounded_empty_rate_percent": self.m4_bounded_empty_rate_percent,
            "m4_bounded_empty_slot_count": self.m4_bounded_empty_slot_count,
            "m4_bounded_non_empty_field_count": self.m4_bounded_non_empty_field_count,
            "m4_bounded_non_empty_slot_count": self.m4_bounded_non_empty_slot_count,
            "m4_comma_character_observed_count": self.m4_comma_character_observed_count,
            "m4_edge_whitespace_observed_count": self.m4_edge_whitespace_observed_count,
            "m4_newline_character_observed_count": self.m4_newline_character_observed_count,
            "m4_pipe_character_observed_count": self.m4_pipe_character_observed_count,
            "m4_semicolon_character_observed_count": self.m4_semicolon_character_observed_count,
            "bounded_nonempty_same_slot_cooccurrence": [
                {"other_suffix": suffix, "cooccurring_slot_count": count}
                for suffix, count in self.bounded_nonempty_same_slot_cooccurrence
            ],
            "maximum_non_empty_character_length": self.maximum_non_empty_character_length,
            "minimum_non_empty_character_length": self.minimum_non_empty_character_length,
            "order_status": self.order_status,
            "policy_input_status": self.policy_input_status.value,
            "semantic_limit": self.semantic_limit,
            "sides_present": list(self.sides_present),
            "slot_count_per_side": self.slot_count_per_side,
            "timing_status": self.timing_status.value,
            "turn_family_evidence_contract_id": "openmtgdata.decision-turn-family-evidence.v1",
            "visibility_status": self.visibility_status.value,
        }


@dataclass(frozen=True, slots=True)
class DecisionTypeEvidenceV1:
    decision_type_id: str
    exact_source_families: tuple[str, ...]
    disposition: DecisionTypeDisposition
    value_structure: str
    observed_action_origin: ReconstructionEvidenceLevel
    action_structure_status: ActionStatus
    ordering_status: str
    actor_status: PerspectiveStatus
    perspective_status: str
    visibility_status: VisibilityStatus
    before_state_status: BeforeStateStatus
    target_status: TargetStatus
    leakage_status: LeakageStatus
    safe_predecision_fields: tuple[DecisionFieldReferenceV1, ...]
    evidence_scope: str
    reason: str

    def __post_init__(self) -> None:
        actor_is_supported = self.actor_status in {
            PerspectiveStatus.ACTOR_SOURCE_USER,
            PerspectiveStatus.ACTOR_SOURCE_OPPO,
            PerspectiveStatus.ACTOR_DERIVABLE,
        }
        action_label_is_supported = (
            self.observed_action_origin
            in {
                ReconstructionEvidenceLevel.SOURCE_DIRECT,
                ReconstructionEvidenceLevel.DETERMINISTIC_WITHIN_SOURCE,
            }
            and self.action_structure_status is ActionStatus.DECISION_BOUNDARY_SUPPORTED
            and actor_is_supported
        )
        if self.disposition is DecisionTypeDisposition.PARTIAL and not action_label_is_supported:
            raise DecisionEvidenceError(
                "partial evidence requires a source-backed action boundary and supported actor"
            )
        if (
            self.disposition is DecisionTypeDisposition.SUPPORTED
            and self.evidence_scope != "m4-full-stream-verified"
        ):
            raise DecisionEvidenceError("full decision support requires full-stream evidence")
        if self.disposition is DecisionTypeDisposition.SUPPORTED:
            visibility_is_supported = (
                self.visibility_status is VisibilityStatus.PUBLIC
                or (
                    self.visibility_status is VisibilityStatus.SOURCE_USER_PRIVATE
                    and self.actor_status is PerspectiveStatus.ACTOR_SOURCE_USER
                )
                or (
                    self.visibility_status is VisibilityStatus.SOURCE_OPPO_PRIVATE
                    and self.actor_status is PerspectiveStatus.ACTOR_SOURCE_OPPO
                )
            )
            if (
                self.observed_action_origin
                not in {
                    ReconstructionEvidenceLevel.SOURCE_DIRECT,
                    ReconstructionEvidenceLevel.DETERMINISTIC_WITHIN_SOURCE,
                }
                or self.action_structure_status is not ActionStatus.DECISION_BOUNDARY_SUPPORTED
                or not actor_is_supported
                or self.before_state_status
                not in {BeforeStateStatus.EXACT_BEFORE_STATE, BeforeStateStatus.TURN_START_STATE}
                or not visibility_is_supported
                or self.leakage_status is not LeakageStatus.PRE_DECISION_SAFE
                or not self.safe_predecision_fields
                or self.target_status
                in {TargetStatus.ACTION_TARGET_UNSUPPORTED, TargetStatus.UNKNOWN}
            ):
                raise DecisionEvidenceError(
                    "supported decision type fails actor/timing/state/visibility/action gates"
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "action_target_status": self.target_status.value,
            "actor_status": self.actor_status.value,
            "before_state_status": self.before_state_status.value,
            "decision_type_contract_id": DECISION_TYPE_EVIDENCE_CONTRACT_ID,
            "decision_type_id": self.decision_type_id,
            "disposition": self.disposition.value,
            "exact_source_families": list(self.exact_source_families),
            "action_structure_status": self.action_structure_status.value,
            "evidence_scope": self.evidence_scope,
            "leakage_status": self.leakage_status.value,
            "ordering_status": self.ordering_status,
            "observed_action_origin": self.observed_action_origin.value,
            "perspective_status": self.perspective_status,
            "reason": self.reason,
            "safe_predecision_fields": [item.to_dict() for item in self.safe_predecision_fields],
            "value_structure": self.value_structure,
            "visibility_status": self.visibility_status.value,
        }


@dataclass(frozen=True, slots=True)
class PerspectiveEvidenceV1:
    source_turn_side_order_status: str
    turn_owner_status: str
    decision_actor_status: PerspectiveStatus
    actor_can_differ_from_turn_owner: str
    self_opponent_transform_status: str
    source_user_private_is_unconditional_self: bool
    source_oppo_private_is_unconditional_opponent: bool
    future_equivariance_requirement: str
    perspective_swap_augmentation_status: str
    unresolved_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "actor_can_differ_from_turn_owner": self.actor_can_differ_from_turn_owner,
            "decision_actor_status": self.decision_actor_status.value,
            "future_equivariance_requirement": self.future_equivariance_requirement,
            "perspective_evidence_contract_id": PERSPECTIVE_EVIDENCE_CONTRACT_ID,
            "perspective_swap_augmentation_status": self.perspective_swap_augmentation_status,
            "self_opponent_transform_status": self.self_opponent_transform_status,
            "source_oppo_private_is_unconditional_opponent": (
                self.source_oppo_private_is_unconditional_opponent
            ),
            "source_turn_side_order_status": self.source_turn_side_order_status,
            "source_user_private_is_unconditional_self": (
                self.source_user_private_is_unconditional_self
            ),
            "turn_owner_status": self.turn_owner_status,
            "unresolved_evidence": list(self.unresolved_evidence),
        }


@dataclass(frozen=True, slots=True)
class AnnotatedExampleV1:
    source_archive_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    replay_field_mapping_id: str
    m4_reader_contract_id: str
    m5_event_contract_id: str
    data_record_ordinal: int
    source_turn_count: int | None
    source_on_play_value_shape: str | None
    sampled_event_slot_count: int | None
    source_turn_slots: tuple[tuple[int, str, int], ...]
    field_shapes: tuple[tuple[str, int, str], ...]
    annotations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "annotations": list(self.annotations),
            "data_record_ordinal": self.data_record_ordinal,
            "example_contract_id": ANNOTATED_EXAMPLE_CONTRACT_ID,
            "field_shapes": [
                {"exact_header_name": name, "column_index": index, "value_shape": shape}
                for name, index, shape in self.field_shapes
            ],
            "sampled_event_slot_count": self.sampled_event_slot_count,
            "source_turn_slots": [
                {
                    "event_ordinal_within_source_record": ordinal,
                    "source_turn_side": side,
                    "source_turn_slot_index": slot,
                }
                for ordinal, side, slot in self.source_turn_slots
            ],
            "source_archive_id": self.source_archive_id,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "replay_field_mapping_id": self.replay_field_mapping_id,
            "m4_reader_contract_id": self.m4_reader_contract_id,
            "m5_event_contract_id": self.m5_event_contract_id,
            "source_turn_count": self.source_turn_count,
            "source_on_play_value_shape": self.source_on_play_value_shape,
            "raw_values_retained": False,
        }


@dataclass(frozen=True, slots=True)
class BoundedReplayEvidenceV1:
    header_fields: tuple[str, ...]
    raw_schema_fingerprint: str
    m4_rows_seen: int
    m4_rows_accepted: int
    m4_rows_rejected: int
    m5_rows_submitted: int
    m5_rows_normalized: int
    m5_rows_rejected: int
    m5_rejection_counts: tuple[tuple[str, int], ...]
    family_cell_counts: dict[str, dict[str, int]]
    examples: tuple[AnnotatedExampleV1, ...]
    reader_completion_status: str


def collect_bounded_replay_evidence(
    source_record: SourceArchiveRecordV1,
    schema_registry: VerifiedSchemaRegistryV1,
    group: RawSchemaGroupEvidenceV1,
    mapping: ReplayFieldMappingV1,
    *,
    max_data_rows: int = 256,
) -> BoundedReplayEvidenceV1:
    """Read a deterministic bounded M4-v2 prefix, retaining aggregates only.

    This helper never completes or claims terminal verification of the source; the
    accepted M5 review artifact remains the full-stream integrity authority.
    """
    from collections import Counter

    from openmtgdata.replay_adapter import ReplayAdapterV1
    from openmtgdata.source_reader import SourceReaderConfigV1, open_source_reader

    if not 1 <= max_data_rows <= 256:
        raise DecisionEvidenceError("M7.1 bounded row limit must be in 1..256")
    if group.raw_schema_fingerprint != mapping.raw_schema_fingerprint:
        raise DecisionEvidenceError("bounded M4 source and M5 mapping fingerprints disagree")
    adapter = ReplayAdapterV1._for_review_candidate(mapping)
    summary: SourceReaderSummaryV1 | None = None
    accepted = seen = rejected = m5_normalized = m5_rejected = 0
    rejection_counts: Counter[str] = Counter()
    family_counters: dict[str, Counter[str]] = {}
    examples: list[AnnotatedExampleV1] = []
    sample_names = (
        "turns",
        "on_play",
        "won",
        "opening_hand",
        "user_turn_1_cards_drawn",
        "user_turn_1_lands_played",
        "user_turn_1_eot_user_cards_in_hand",
        "oppo_turn_1_cards_drawn",
        "oppo_turn_1_lands_played",
        "oppo_turn_1_eot_user_cards_in_hand",
    )
    pattern = _TURN_FIELD
    with open_source_reader(
        source_record,
        schema_registry,
        config=SourceReaderConfigV1(max_records_per_batch=max_data_rows),
    ) as reader:
        batch = next(reader)
        if reader.raw_schema_fingerprint != group.raw_schema_fingerprint:
            raise DecisionEvidenceError("M4 actual header differs from corrected M3 group")
        headers = reader.header_fields
        if headers != group.field_names:
            raise DecisionEvidenceError("M4 exact ordered header differs from M3 authority")
        seen = batch.records_seen
        accepted = batch.records_accepted
        rejected = batch.records_rejected
        for row in batch.accepted_records:
            nonempty_by_slot: dict[tuple[str, int], set[str]] = {}
            for index, header in enumerate(headers):
                match = pattern.fullmatch(header)
                if match is None:
                    continue
                suffix = match.group(3)
                counts = family_counters.setdefault(suffix, Counter())
                value = row.fields[index]
                counts["empty" if value == "" else "nonempty"] += 1
                if value == "":
                    continue
                nonempty_by_slot.setdefault((match.group(1), int(match.group(2))), set()).add(
                    suffix
                )
                counts[f"lexical:{classify_lexeme(value).value}"] += 1
                counts["pipe"] += "|" in value
                counts["comma"] += "," in value
                counts["semicolon"] += ";" in value
                counts["newline"] += "\n" in value
                counts["edge_whitespace"] += value != value.strip()
                counts["minimum_nonempty_chars"] = min(
                    counts.get("minimum_nonempty_chars", len(value)), len(value)
                )
                counts["maximum_nonempty_chars"] = max(
                    counts.get("maximum_nonempty_chars", 0), len(value)
                )
            for suffixes in nonempty_by_slot.values():
                for suffix in suffixes:
                    counts = family_counters[suffix]
                    for other_suffix in suffixes - {suffix}:
                        counts[f"cooccur:{other_suffix}"] += 1
            for side in ("user", "oppo"):
                for slot in range(1, 31):
                    occupied = nonempty_by_slot.get((side, slot), set())
                    for suffix in family_counters:
                        family_counters[suffix][
                            "nonempty_slots" if suffix in occupied else "empty_slots"
                        ] += 1
            normalized = adapter.normalize_record(row, header_fields=headers)
            if normalized.rejection is None:
                m5_normalized += 1
            else:
                m5_rejected += 1
                rejection_counts[normalized.rejection.quality_code.value] += 1
                continue
            if len(examples) < 3:
                selected = tuple(
                    (
                        name,
                        headers.index(name),
                        classify_value_shape(row.fields[headers.index(name)]),
                    )
                    for name in sample_names
                    if name in headers
                )
                turns = normalized.events[0].source_turn_count if normalized.events else 0
                on_play_shape = (
                    classify_value_shape(row.fields[headers.index("on_play")])
                    if "on_play" in headers
                    else None
                )
                examples.append(
                    AnnotatedExampleV1(
                        row.source_archive_id,
                        row.raw_schema_fingerprint,
                        row.source_interpretation_contract_id,
                        mapping.replay_field_mapping_id,
                        row.reader_contract_id,
                        "openmtgdata.replay-event.v1",
                        row.data_record_ordinal,
                        turns,
                        on_play_shape,
                        len(normalized.events),
                        tuple(
                            (
                                event.locator.event_ordinal_within_source_record,
                                event.source_turn_side.value,
                                event.source_turn_slot_index,
                            )
                            for event in normalized.events
                        ),
                        selected,
                        (
                            "M4 accepted exact-width raw row; field values redacted to shapes",
                            "M5 event ordinals denote source turn-summary slots only",
                            "no action ordering, decision actor, or pre-decision state inferred",
                        ),
                    )
                )
        summary = reader.summary
    assert summary is not None
    if seen != accepted + rejected or accepted != m5_normalized + m5_rejected:
        raise DecisionEvidenceError("bounded M4/M5 row counts do not reconcile")
    return BoundedReplayEvidenceV1(
        headers,
        group.raw_schema_fingerprint,
        seen,
        accepted,
        rejected,
        accepted,
        m5_normalized,
        m5_rejected,
        tuple(sorted(rejection_counts.items())),
        {name: dict(counts) for name, counts in sorted(family_counters.items())},
        tuple(examples),
        summary.completion_status.value,
    )


@dataclass(frozen=True, slots=True)
class DecisionEvidenceReportV1:
    source_catalog_digest: str
    m3_evidence_digest: str
    schema_registry_digest: str
    m5_mapping_id: str
    m5_mapping_registry_digest: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    m4_reader_contract_id: str
    m5_event_contract_id: str
    evidence_scope: str
    population: dict[str, int]
    field_inventory: tuple[DecisionFieldSafetyV1, ...]
    turn_families: tuple[TurnFamilyEvidenceV1, ...]
    decision_types: tuple[DecisionTypeEvidenceV1, ...]
    perspective: PerspectiveEvidenceV1
    annotated_examples: tuple[AnnotatedExampleV1, ...]
    findings: dict[str, object]
    overall_status: OverallDecisionStatus
    turn_summary_observed: bool
    observed_behavior_supported: bool
    can_build_safe_behavior_cloning_sample: bool
    limitations: tuple[str, ...]
    report_digest: str

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "annotated_examples": [
                item.to_dict()
                for item in sorted(
                    self.annotated_examples,
                    key=lambda item: (item.source_archive_id, item.data_record_ordinal),
                )
            ],
            "can_build_safe_behavior_cloning_sample": self.can_build_safe_behavior_cloning_sample,
            "observed_behavior_supported": self.observed_behavior_supported,
            "decision_types": [
                item.to_dict()
                for item in sorted(self.decision_types, key=lambda item: item.decision_type_id)
            ],
            "evidence_scope": self.evidence_scope,
            "field_inventory": [
                item.to_dict()
                for item in sorted(
                    self.field_inventory,
                    key=lambda item: (
                        item.selector.column_index,
                        item.selector.exact_header_name,
                    ),
                )
            ],
            "findings": self.findings,
            "limitations": list(self.limitations),
            "m3_evidence_digest": self.m3_evidence_digest,
            "m4_reader_contract_id": self.m4_reader_contract_id,
            "m5_event_contract_id": self.m5_event_contract_id,
            "m5_mapping_id": self.m5_mapping_id,
            "m5_mapping_registry_digest": self.m5_mapping_registry_digest,
            "overall_status": self.overall_status.value,
            "perspective": self.perspective.to_dict(),
            "population": self.population,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "report_contract_id": DECISION_EVIDENCE_REPORT_CONTRACT_ID,
            "schema_registry_digest": self.schema_registry_digest,
            "source_archive_id": self.source_archive_id,
            "source_catalog_digest": self.source_catalog_digest,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "turn_summary_observed": self.turn_summary_observed,
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "turn_families": [
                item.to_dict()
                for item in sorted(self.turn_families, key=lambda item: item.exact_suffix)
            ],
            "decision_semantics_policy_id": DECISION_SEMANTICS_POLICY_ID,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence": self.semantic_projection_dict(),
            "report_digest_contract_id": DECISION_REPORT_DIGEST_CONTRACT_ID,
            "report_digest": self.report_digest,
        }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8", errors="strict"
    )


def decision_report_digest(projection: dict[str, object]) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {
                "digest_contract_id": DECISION_REPORT_DIGEST_CONTRACT_ID,
                "report_projection": projection,
            }
        )
    ).hexdigest()


def _validate_semantic_projection_values(value: object) -> None:
    """Reject runtime audit keys and local absolute paths from semantic evidence."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _NONSEMANTIC_KEY.search(str(key)):
                raise DecisionEvidenceError(
                    "runtime/audit metadata cannot enter semantic report identity"
                )
            _validate_semantic_projection_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_semantic_projection_values(item)
    elif isinstance(value, str) and _ABSOLUTE_LOCAL_PATH.search(value):
        raise DecisionEvidenceError("absolute local paths cannot enter semantic report identity")


def validate_decision_report(report: DecisionEvidenceReportV1) -> None:
    _validate_semantic_projection_values(report.semantic_projection_dict())
    if len(report.field_inventory) != len(
        {
            (field.selector.column_index, field.selector.exact_header_name)
            for field in report.field_inventory
        }
    ):
        raise DecisionEvidenceError("field inventory contains duplicate physical selectors")
    if any(field.policy_input_status in _SAFE_POLICY_STATUS for field in report.field_inventory):
        for field in report.field_inventory:
            if field.policy_input_status in _SAFE_POLICY_STATUS:
                if field.timing_status not in {TimingStatus.PRE_TURN, TimingStatus.TURN_START}:
                    raise DecisionEvidenceError("unsafe timing cannot be approved as policy input")
                if field.leakage_status is not LeakageStatus.PRE_DECISION_SAFE:
                    raise DecisionEvidenceError("leaking field cannot be approved as policy input")
                if (
                    field.policy_input_status is PolicyInputStatus.SAFE_PUBLIC
                    and field.source_side_private_candidate
                ):
                    raise DecisionEvidenceError(
                        "private-information candidate cannot be declared public"
                    )
                if field.visibility_status not in {
                    VisibilityStatus.PUBLIC,
                    VisibilityStatus.NOT_APPLICABLE,
                } and field.policy_input_status not in {
                    PolicyInputStatus.SAFE_IF_ACTOR_USER,
                    PolicyInputStatus.SAFE_IF_ACTOR_OPPO,
                }:
                    raise DecisionEvidenceError("private/unknown visibility requires actor binding")
                if field.policy_input_status is PolicyInputStatus.SAFE_IF_ACTOR_USER and (
                    field.source_side != "user"
                    or field.perspective_status is not PerspectiveStatus.ACTOR_SOURCE_USER
                ):
                    raise DecisionEvidenceError(
                        "source-user private input lacks matching actor evidence"
                    )
                if field.policy_input_status is PolicyInputStatus.SAFE_IF_ACTOR_OPPO and (
                    field.source_side != "oppo"
                    or field.perspective_status is not PerspectiveStatus.ACTOR_SOURCE_OPPO
                ):
                    raise DecisionEvidenceError(
                        "source-oppo private input lacks matching actor evidence"
                    )
    fields_by_selector = {
        (item.selector.column_index, item.selector.exact_header_name): item
        for item in report.field_inventory
    }
    for decision_type in report.decision_types:
        if decision_type.disposition is not DecisionTypeDisposition.SUPPORTED:
            continue
        if not decision_type.safe_predecision_fields:
            raise DecisionEvidenceError("supported decision type has no exact safe input selectors")
        for selector in decision_type.safe_predecision_fields:
            selected_field = fields_by_selector.get(
                (selector.column_index, selector.exact_header_name)
            )
            if (
                selected_field is None
                or selected_field.policy_input_status not in _SAFE_POLICY_STATUS
            ):
                raise DecisionEvidenceError("decision type references an unapproved policy field")
            if selected_field.leakage_status is not LeakageStatus.PRE_DECISION_SAFE:
                raise DecisionEvidenceError("decision type references a temporally unsafe field")
    if report.observed_behavior_supported and not any(
        supports_observed_behavior(item) for item in report.decision_types
    ):
        raise DecisionEvidenceError(
            "report cannot support observed behavior without an approved type"
        )
    if report.observed_behavior_supported != any(
        supports_observed_behavior(item) for item in report.decision_types
    ):
        raise DecisionEvidenceError("observed behavior flag does not follow decision-type evidence")
    if report.can_build_safe_behavior_cloning_sample and not any(
        item.disposition is DecisionTypeDisposition.SUPPORTED for item in report.decision_types
    ):
        raise DecisionEvidenceError(
            "report cannot claim BC safety without a supported decision type"
        )
    if report.can_build_safe_behavior_cloning_sample and not report.observed_behavior_supported:
        raise DecisionEvidenceError("BC-safe report requires a supported observed behavior label")
    if report.can_build_safe_behavior_cloning_sample and report.overall_status not in {
        OverallDecisionStatus.DECISION_EXTRACTION_SUPPORTED,
        OverallDecisionStatus.PARTIAL_DECISION_EXTRACTION_SUPPORTED,
    }:
        raise DecisionEvidenceError("overall status contradicts BC-safe result")
    if report.overall_status is not derive_overall_status(
        report.decision_types, turn_summary_observed=report.turn_summary_observed
    ):
        raise DecisionEvidenceError("overall report status does not follow decision-type evidence")
    if not _REPORT_SHA256.fullmatch(report.report_digest):
        raise DecisionEvidenceError("decision evidence report digest is malformed")
    for value in (
        report.source_catalog_digest,
        report.m3_evidence_digest,
        report.schema_registry_digest,
        report.m5_mapping_registry_digest,
        report.source_archive_id,
        report.compressed_sha256,
        report.raw_schema_fingerprint,
    ):
        if not _REPORT_SHA256.fullmatch(value):
            raise DecisionEvidenceError("decision evidence source binding is malformed")
    if not re.fullmatch(
        r"openmtgdata\.replay-field-mapping\.v1:[0-9a-f]{64}", report.m5_mapping_id
    ):
        raise DecisionEvidenceError("decision evidence M5 mapping identity is malformed")
    if not re.fullmatch(
        r"openmtgdata\.source-interpretation\.v1:[0-9a-f]{64}",
        report.source_interpretation_contract_id,
    ):
        raise DecisionEvidenceError("decision evidence M3.2 interpretation identity is malformed")
    if report.m4_reader_contract_id != "openmtgdata.raw-source-reader.v2":
        raise DecisionEvidenceError("decision evidence must bind M4 reader v2")
    if report.m5_event_contract_id != "openmtgdata.replay-event.v1":
        raise DecisionEvidenceError("decision evidence has unsupported M5 event contract")
    if report.report_digest != decision_report_digest(report.semantic_projection_dict()):
        raise DecisionEvidenceError("decision evidence report digest mismatch")


_SAFE_POLICY_STATUS = {
    PolicyInputStatus.SAFE_PUBLIC,
    PolicyInputStatus.SAFE_IF_ACTOR_USER,
    PolicyInputStatus.SAFE_IF_ACTOR_OPPO,
}


def derive_overall_status(
    decision_types: tuple[DecisionTypeEvidenceV1, ...], *, turn_summary_observed: bool
) -> OverallDecisionStatus:
    """Derive the report conclusion mechanically from reviewed decision dispositions."""
    dispositions = {item.disposition for item in decision_types}
    if not decision_types:
        return (
            OverallDecisionStatus.TURN_SUMMARY_ONLY
            if turn_summary_observed
            else OverallDecisionStatus.ANALYSIS_INCOMPLETE
        )
    if dispositions == {DecisionTypeDisposition.SUPPORTED} and all(
        item.evidence_scope == "m4-full-stream-verified" for item in decision_types
    ):
        return OverallDecisionStatus.DECISION_EXTRACTION_SUPPORTED
    if dispositions & {DecisionTypeDisposition.SUPPORTED, DecisionTypeDisposition.PARTIAL}:
        return OverallDecisionStatus.PARTIAL_DECISION_EXTRACTION_SUPPORTED
    if DecisionTypeDisposition.NEEDS_REVIEW in dispositions:
        return OverallDecisionStatus.ANALYSIS_INCOMPLETE
    if DecisionTypeDisposition.UNSUPPORTED_ACTOR in dispositions:
        return OverallDecisionStatus.ACTOR_UNRESOLVED
    if DecisionTypeDisposition.UNSUPPORTED_TIMING in dispositions:
        return OverallDecisionStatus.TEMPORAL_BOUNDARY_UNRESOLVED
    if turn_summary_observed and dispositions <= {
        DecisionTypeDisposition.UNSUPPORTED_ACTION_STRUCTURE,
        DecisionTypeDisposition.UNSUPPORTED_BEFORE_STATE,
        DecisionTypeDisposition.UNSUPPORTED_TARGET_SEMANTICS,
        DecisionTypeDisposition.UNSUPPORTED_VISIBILITY,
    }:
        return OverallDecisionStatus.TURN_SUMMARY_ONLY
    return OverallDecisionStatus.UNSAFE_FOR_BEHAVIOR_CLONING


def supports_observed_behavior(decision_type: DecisionTypeEvidenceV1) -> bool:
    """Action identity/boundary and decision actor must be established for behavior labels."""
    return (
        decision_type.disposition
        in {DecisionTypeDisposition.SUPPORTED, DecisionTypeDisposition.PARTIAL}
        and decision_type.observed_action_origin
        in {
            ReconstructionEvidenceLevel.SOURCE_DIRECT,
            ReconstructionEvidenceLevel.DETERMINISTIC_WITHIN_SOURCE,
        }
        and decision_type.action_structure_status is ActionStatus.DECISION_BOUNDARY_SUPPORTED
        and decision_type.actor_status
        in {
            PerspectiveStatus.ACTOR_SOURCE_USER,
            PerspectiveStatus.ACTOR_SOURCE_OPPO,
            PerspectiveStatus.ACTOR_DERIVABLE,
        }
    )


def classify_field(
    column_index: int,
    exact_header_name: str,
    *,
    lexical_class_counts: tuple[tuple[str, int], ...],
    m5_mapped: bool,
    m3_evidence_scope: str,
) -> DecisionFieldSafetyV1:
    """Classify a physical field conservatively, retaining only exact selector identity."""
    match = _TURN_FIELD.fullmatch(exact_header_name)
    side = match.group(1) if match else None
    slot = int(match.group(2)) if match else None
    suffix = match.group(3) if match else None
    if match:
        family = "indexed_turn_slot"
        timing = TimingStatus.WITHIN_TURN_UNKNOWN
        category = "turn_slot_field_semantics_unestablished"
        action = (
            ActionStatus.ACTION_CANDIDATE
            if _ACTION_CANDIDATE.search(suffix or "")
            else ActionStatus.NOT_ACTION
        )
        private_candidate = any(token in (suffix or "") for token in ("hand", "drawn", "learned"))
        eot_candidate = (suffix or "").startswith("eot_")
        rationale = (
            "M5.1 establishes a source turn-summary slot, not this field's within-turn timing, "
            "action meaning, or visibility."
        )
        leakage = LeakageStatus.TIMING_UNKNOWN
        policy = PolicyInputStatus.TIMING_AMBIGUOUS
        before = BeforeStateStatus.TURN_AGGREGATE_ONLY
        target = (
            TargetStatus.ACTION_TARGET_UNSUPPORTED
            if action is not ActionStatus.NOT_ACTION
            else TargetStatus.NOT_APPLICABLE
        )
    else:
        family = "row_level"
        side = (
            "user"
            if exact_header_name.startswith("user_")
            else "oppo"
            if exact_header_name.startswith("oppo_")
            else None
        )
        slot = None
        suffix = None
        lower = exact_header_name.lower()
        outcome_candidate = any(
            word in lower for word in ("won", "winner", "result", "outcome", "final")
        )
        private_candidate = any(
            word in lower for word in ("hand", "candidate_hand", "opening_hand")
        )
        eot_candidate = False
        if exact_header_name in {"draft_id", "history_id", "expansion", "format", "game_index"}:
            timing = TimingStatus.GAME_LEVEL
            category = "source_identity_or_context_candidate"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.TIMING_UNKNOWN
            policy = PolicyInputStatus.UNSUPPORTED
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        elif exact_header_name == "turns":
            timing = TimingStatus.GAME_LEVEL
            category = "whole_record_turn_count"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.POST_DECISION
            policy = PolicyInputStatus.POST_DECISION_ONLY
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        elif outcome_candidate:
            timing = TimingStatus.UNKNOWN_TIMING
            category = "outcome_like_name_candidate_semantics_unconfirmed"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.GAME_OUTCOME
            policy = PolicyInputStatus.POST_DECISION_ONLY
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        elif private_candidate:
            timing = TimingStatus.UNKNOWN_TIMING
            category = "potential_private_or_candidate_hand_field_semantics_unconfirmed"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.TIMING_UNKNOWN
            policy = PolicyInputStatus.HIDDEN_INFO_UNSAFE
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        elif exact_header_name in {"on_play", "time"}:
            timing = TimingStatus.UNKNOWN_TIMING
            category = "game_setup_or_time_candidate_semantics_unconfirmed"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.TIMING_UNKNOWN
            policy = PolicyInputStatus.TIMING_AMBIGUOUS
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        elif exact_header_name.endswith("_total_cards_drawn") or "_total_" in lower:
            timing = TimingStatus.GAME_LEVEL
            category = "whole_record_aggregate_candidate"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.POST_DECISION
            policy = PolicyInputStatus.POST_DECISION_ONLY
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        elif exact_header_name == "missing_diffs":
            timing = TimingStatus.UNKNOWN_TIMING
            category = "source_quality_field_semantics_unestablished"
            action = ActionStatus.NOT_ACTION
            leakage = LeakageStatus.TIMING_UNKNOWN
            policy = PolicyInputStatus.UNSUPPORTED
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = TargetStatus.NOT_APPLICABLE
        else:
            timing = TimingStatus.UNKNOWN_TIMING
            category = "unmapped_source_field_semantics_unestablished"
            action = (
                ActionStatus.ACTION_CANDIDATE
                if _ACTION_CANDIDATE.search(lower)
                else ActionStatus.NOT_ACTION
            )
            leakage = LeakageStatus.TIMING_UNKNOWN
            policy = PolicyInputStatus.TIMING_AMBIGUOUS
            before = BeforeStateStatus.NO_BEFORE_STATE
            target = (
                TargetStatus.ACTION_TARGET_UNSUPPORTED
                if action is not ActionStatus.NOT_ACTION
                else TargetStatus.NOT_APPLICABLE
            )
        rationale = (
            "Row-level M4 source field; its temporal, actor, visibility, and semantic scope is "
            "not established by M3/M5 evidence."
        )
    if not m5_mapped and policy in _SAFE_POLICY_STATUS:
        raise DecisionEvidenceError("raw-only fields cannot be implicitly approved as policy input")
    return DecisionFieldSafetyV1(
        DecisionFieldReferenceV1(column_index, exact_header_name),
        family,
        side,
        slot,
        suffix,
        "mapped" if m5_mapped else "raw_only",
        tuple(sorted(lexical_class_counts)),
        dict(lexical_class_counts).get("empty", 0) > 0,
        m3_evidence_scope,
        category,
        timing,
        VisibilityStatus.UNKNOWN_VISIBILITY,
        policy,
        action,
        PerspectiveStatus.ACTOR_UNKNOWN,
        before,
        target,
        leakage,
        private_candidate,
        eot_candidate,
        rationale,
    )


_ACTION_CANDIDATE = re.compile(
    r"(?:played|cast|attack|block|discard|draw|foretold|ability|abilities|damage|kill|learned)",
    re.IGNORECASE,
)


def classify_value_shape(value: str) -> str:
    """Return a redacted lexical/shape descriptor; never return the source value."""
    if value == "":
        return "empty_string"
    lexical = classify_lexeme(value).value
    delimiters = tuple(char for char in ("|", ",", ";", "\n") if char in value)
    if delimiters:
        return (
            f"{lexical}; delimiter_characters_observed={''.join(delimiters)}; order_unestablished"
        )
    return lexical


def validate_policy_field_for_decision(
    field: DecisionFieldSafetyV1,
    *,
    candidate_event_ordinal: int,
    source_on_play: bool | None = None,
    decision_actor_source_side: str | None = None,
) -> None:
    """Reject future-slot, post-action, unknown-time, or non-actor-safe inputs.

    Candidate and source slot positions use M5.1's zero-based chronological event
    ordinal. Per-side slot indexes are converted using the source ``on_play`` flag.
    """
    if candidate_event_ordinal < 0:
        raise DecisionEvidenceError("candidate event ordinal must be nonnegative")
    if field.turn_slot_index is not None:
        if type(source_on_play) is not bool:
            raise DecisionEvidenceError("indexed turn fields require exact parsed source on_play")
        field_event_ordinal = source_turn_event_ordinal(field, source_on_play=source_on_play)
        if field_event_ordinal > candidate_event_ordinal:
            raise DecisionEvidenceError("later chronological turn slot cannot be a policy input")
    if field.timing_status not in {TimingStatus.PRE_TURN, TimingStatus.TURN_START}:
        raise DecisionEvidenceError("field timing is not established before the candidate decision")
    if field.leakage_status is not LeakageStatus.PRE_DECISION_SAFE:
        raise DecisionEvidenceError("field is not proven free of post-decision/future leakage")
    if field.policy_input_status not in _SAFE_POLICY_STATUS:
        raise DecisionEvidenceError("field policy-input status is not safe")
    if field.policy_input_status is PolicyInputStatus.SAFE_IF_ACTOR_USER and (
        decision_actor_source_side != "user"
        or field.source_side != "user"
        or field.perspective_status is not PerspectiveStatus.ACTOR_SOURCE_USER
    ):
        raise DecisionEvidenceError("source-user input requires an evidenced source-user actor")
    if field.policy_input_status is PolicyInputStatus.SAFE_IF_ACTOR_OPPO and (
        decision_actor_source_side != "oppo"
        or field.source_side != "oppo"
        or field.perspective_status is not PerspectiveStatus.ACTOR_SOURCE_OPPO
    ):
        raise DecisionEvidenceError("source-oppo input requires an evidenced source-oppo actor")


def source_turn_event_ordinal(field: DecisionFieldSafetyV1, *, source_on_play: bool) -> int:
    """Convert a M5.1 per-side 1-based slot index to its 0-based source turn order."""
    if field.turn_slot_index is None or field.source_side not in {"user", "oppo"}:
        raise DecisionEvidenceError("chronological turn conversion requires an indexed side field")
    first_side = "user" if source_on_play else "oppo"
    within_pair_offset = 0 if field.source_side == first_side else 1
    return 2 * (field.turn_slot_index - 1) + within_pair_offset


def validate_perspective_swap_requirement(
    *,
    source_user_actor: str,
    source_oppo_actor: str,
    swapped_source_user_actor: str,
    swapped_source_oppo_actor: str,
) -> tuple[str, str]:
    """Pure role mapping for future tests; source labels never bind permanently to SELF."""
    if source_user_actor not in {"actor", "other"} or source_oppo_actor not in {"actor", "other"}:
        raise DecisionEvidenceError("actor relation must be established before perspective mapping")
    if {source_user_actor, source_oppo_actor} != {"actor", "other"}:
        raise DecisionEvidenceError("source sides must map one-to-one to actor and other")
    if (
        swapped_source_user_actor != source_oppo_actor
        or swapped_source_oppo_actor != source_user_actor
    ):
        raise DecisionEvidenceError("perspective swap must exchange source-side actor relations")
    original = tuple(
        "SELF" if role == "actor" else "OPPONENT" for role in (source_user_actor, source_oppo_actor)
    )
    swapped = tuple(
        "SELF" if role == "actor" else "OPPONENT"
        for role in (swapped_source_user_actor, swapped_source_oppo_actor)
    )
    if original != tuple(reversed(swapped)):
        raise DecisionEvidenceError("actor-relative perspective equivariance failed")
    return original[0], original[1]


def decision_report_with_digest(report: DecisionEvidenceReportV1) -> DecisionEvidenceReportV1:
    """Return an otherwise complete immutable report with its semantic digest populated."""
    from dataclasses import replace

    digest = decision_report_digest(report.semantic_projection_dict())
    result = replace(report, report_digest=digest)
    validate_decision_report(result)
    return result


def build_field_inventory(
    group: RawSchemaGroupEvidenceV1,
    mapping: ReplayFieldMappingV1,
) -> tuple[DecisionFieldSafetyV1, ...]:
    """Build exact index/name coverage for every physical field in one reviewed schema."""
    if group.raw_schema_fingerprint != mapping.raw_schema_fingerprint:
        raise DecisionEvidenceError("M3 raw fingerprint does not match M5 mapping")
    mapped_indexes = {
        item.selector.column_index
        for item in mapping.field_dispositions
        if item.disposition.value == "mapped"
    }
    result = tuple(
        classify_field(
            field.column_index,
            field.exact_header_name,
            lexical_class_counts=field.lexical_class_counts,
            m5_mapped=field.column_index in mapped_indexes,
            m3_evidence_scope=(
                "M3.1 lexical prefix aggregate across the corrected AFR PremierDraft+TradDraft "
                "physical group; not archive-specific. M7 M4 sample is separately "
                "PremierDraft-only."
            ),
        )
        for field in group.fields
    )
    expected = {(field.column_index, field.exact_header_name) for field in group.fields}
    actual = {(field.selector.column_index, field.selector.exact_header_name) for field in result}
    if expected != actual:
        raise DecisionEvidenceError("field safety inventory does not cover exact M3 field surface")
    return result


def build_turn_family_summaries(
    group: RawSchemaGroupEvidenceV1,
    *,
    bounded_counts: dict[str, dict[str, int]],
    bounded_rows_seen: int,
    bounded_rows_accepted: int,
) -> tuple[TurnFamilyEvidenceV1, ...]:
    """Combine M3 lexical evidence with redacted bounded M4 shape aggregates."""
    family_fields: dict[str, list[FieldSurfaceV1]] = {}
    sides: dict[str, set[str]] = {}
    slots: dict[str, dict[str, set[int]]] = {}
    for field in group.fields:
        match = _TURN_FIELD.fullmatch(field.exact_header_name)
        if match is None:
            continue
        side, slot_text, suffix = match.groups()
        slot = int(slot_text)
        family_fields.setdefault(suffix, []).append(field)
        sides.setdefault(suffix, set()).add(side)
        slots.setdefault(suffix, {}).setdefault(side, set()).add(slot)
    summaries: list[TurnFamilyEvidenceV1] = []
    for suffix, fields in sorted(family_fields.items()):
        lexical: dict[str, int] = {}
        cells = empty = 0
        for field in fields:
            field_counts = dict(field.lexical_class_counts)
            for name, count in field_counts.items():
                lexical[name] = lexical.get(name, 0) + count
                cells += count
                if name == "empty":
                    empty += count
        sample = bounded_counts.get(suffix, {})
        action_candidate = bool(_ACTION_CANDIDATE.search(suffix))
        slot_counts = {len(value) for value in slots[suffix].values()}
        if len(slot_counts) != 1 or set(slots[suffix]) != {"user", "oppo"}:
            raise DecisionEvidenceError("turn suffix does not have paired user/oppo slot coverage")
        summaries.append(
            TurnFamilyEvidenceV1(
                suffix,
                next(iter(slot_counts)),
                tuple(sorted(sides[suffix])),
                cells,
                empty,
                cells - empty,
                round(100 * empty / cells, 6) if cells else 0.0,
                tuple(sorted(lexical.items())),
                bounded_rows_accepted * len(fields),
                sample.get("empty", 0),
                sample.get("nonempty", 0),
                round(100 * sample.get("empty", 0) / (bounded_rows_accepted * len(fields)), 6)
                if bounded_rows_accepted and fields
                else 0.0,
                sample.get("empty_slots", 0),
                sample.get("nonempty_slots", 0),
                sample.get("pipe", 0),
                sample.get("comma", 0),
                sample.get("semicolon", 0),
                sample.get("newline", 0),
                sample.get("edge_whitespace", 0),
                sample.get("minimum_nonempty_chars", 0) or None,
                sample.get("maximum_nonempty_chars", 0) or None,
                tuple(
                    sorted(
                        (key.removeprefix("cooccur:"), count)
                        for key, count in sample.items()
                        if key.startswith("cooccur:")
                    )
                ),
                (
                    f"M3.1 bounded prefix; M4-v2 first {bounded_rows_seen} logical rows "
                    f"({bounded_rows_accepted} accepted); no full-stream action semantics"
                ),
                TimingStatus.WITHIN_TURN_UNKNOWN,
                VisibilityStatus.UNKNOWN_VISIBILITY,
                ActionStatus.ACTION_CANDIDATE if action_candidate else ActionStatus.NOT_ACTION,
                PolicyInputStatus.TIMING_AMBIGUOUS,
                "unestablished; delimiter characters do not establish an ordered sequence grammar",
                (
                    "slot membership is established by M5.1; suffix semantics, timing, target "
                    "and actor remain unproven"
                ),
            )
        )
    return tuple(summaries)


def validate_report_digest_text(value: str) -> None:
    if _REPORT_SHA256.fullmatch(value) is None:
        raise DecisionEvidenceError("report digest must be lowercase 64-character SHA-256")
