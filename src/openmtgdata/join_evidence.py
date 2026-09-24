"""M6.1 evidence-only analysis of exact Replay/Game row-key candidates."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import StrEnum

from openmtgdata.source_filename import SourceKind

JOIN_EVIDENCE_REPORT_CONTRACT_ID = "openmtgdata.replay-game-join-evidence.v1"
JOIN_CANDIDATE_CONTRACT_ID = "openmtgdata.replay-game-join-candidate.v1"
JOIN_FIELD_REFERENCE_CONTRACT_ID = "openmtgdata.join-field-reference.v1"
JOIN_SIDE_STATS_CONTRACT_ID = "openmtgdata.join-candidate-side-stats.v1"
JOIN_CARDINALITY_CONTRACT_ID = "openmtgdata.join-cardinality-stats.v1"
JOIN_DECISION_CONTRACT_ID = "openmtgdata.join-evidence-decision.v1"
JOIN_REPORT_DECISION_CONTRACT_ID = "openmtgdata.join-evidence-report-decision.v1"
JOIN_REPORT_DIGEST_CONTRACT_ID = "openmtgdata.join-evidence-report-digest.v1"
EXACT_STRING_COMPARISON_CONTRACT_ID = "openmtgdata.exact-m4-csv-string-comparison.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPLAY_MAPPING_ID = re.compile(r"openmtgdata\.replay-field-mapping\.v1:[0-9a-f]{64}\Z")
_GAME_MAPPING_ID = re.compile(r"openmtgdata\.game-field-mapping\.v1:[0-9a-f]{64}\Z")
_INTERPRETATION_ID = re.compile(r"openmtgdata\.source-interpretation\.v1:[0-9a-f]{64}\Z")
_CANONICAL_INT = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class JoinEvidenceError(ValueError):
    """Invalid source binding, candidate declaration, or evidence projection."""


class JoinDecision(StrEnum):
    SUPPORTED_SCOPED_JOIN_KEY = "supported_scoped_join_key"
    PARTIAL_UNAMBIGUOUS_COVERAGE = "partial_unambiguous_coverage"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class JoinReportDecisionStatus(StrEnum):
    SUPPORTED_SCOPED_JOIN = "supported_scoped_join"
    JOIN_UNSUPPORTED = "join_unsupported"
    ANALYSIS_INCOMPLETE = "analysis_incomplete"


@dataclass(frozen=True, slots=True)
class JoinFieldReferenceV1:
    source_kind: SourceKind
    column_index: int
    exact_header_name: str
    source_value_status: str

    def __post_init__(self) -> None:
        if self.column_index < 0 or not self.exact_header_name:
            raise JoinEvidenceError("join field reference requires an exact non-negative selector")

    def to_dict(self) -> dict[str, object]:
        return {
            "column_index": self.column_index,
            "exact_header_name": self.exact_header_name,
            "field_reference_contract_id": JOIN_FIELD_REFERENCE_CONTRACT_ID,
            "source_kind": self.source_kind.value,
            "source_value_status": self.source_value_status,
        }


@dataclass(frozen=True, slots=True)
class JoinFieldEvidenceV1:
    reference: JoinFieldReferenceV1
    observed_lexical_classes: tuple[str, ...]
    empty_field_observed: bool
    m3_observation_scope: str
    candidate_rationale: str

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_rationale": self.candidate_rationale,
            "empty_field_observed": self.empty_field_observed,
            "field_evidence_contract_id": "openmtgdata.join-field-evidence.v1",
            "m3_observation_scope": self.m3_observation_scope,
            "observed_lexical_classes": list(self.observed_lexical_classes),
            "reference": self.reference.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class JoinCandidateV1:
    replay_fields: tuple[JoinFieldReferenceV1, ...]
    game_fields: tuple[JoinFieldReferenceV1, ...]
    candidate_scope: str
    comparison_contract_id: str
    candidate_id: str

    def __post_init__(self) -> None:
        if not self.replay_fields or len(self.replay_fields) != len(self.game_fields):
            raise JoinEvidenceError(
                "Replay/Game candidate must have equal non-empty component counts"
            )
        if any(item.source_kind is not SourceKind.REPLAY for item in self.replay_fields):
            raise JoinEvidenceError("Replay candidate selectors must reference Replay fields")
        if any(item.source_kind is not SourceKind.GAME for item in self.game_fields):
            raise JoinEvidenceError("Game candidate selectors must reference Game fields")
        if self.comparison_contract_id != EXACT_STRING_COMPARISON_CONTRACT_ID:
            raise JoinEvidenceError("unsupported candidate comparison contract")
        expected = derive_join_candidate_id(
            self.replay_fields, self.game_fields, self.candidate_scope
        )
        if self.candidate_id != expected:
            raise JoinEvidenceError("candidate ID does not match its exact selector projection")

    def identity_projection_dict(self) -> dict[str, object]:
        return {
            "candidate_contract_id": JOIN_CANDIDATE_CONTRACT_ID,
            "candidate_scope": self.candidate_scope,
            "comparison_contract_id": self.comparison_contract_id,
            "game_fields": [item.to_dict() for item in self.game_fields],
            "replay_fields": [item.to_dict() for item in self.replay_fields],
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.identity_projection_dict(), "candidate_id": self.candidate_id}


@dataclass(frozen=True, slots=True)
class JoinCandidateSideStatsV1:
    source_kind: SourceKind
    rows_examined: int
    m4_records_seen: int
    m4_records_accepted: int
    m4_records_rejected: int
    rows_with_all_fields_present: int
    rows_with_any_empty_component: int
    rows_with_all_empty_components: int
    rows_with_partial_key: int
    rows_with_complete_key: int
    distinct_complete_keys: int
    duplicate_complete_key_count: int
    duplicate_rows_in_complete_keys: int
    key_multiplicity_distribution: tuple[tuple[int, int], ...]
    empty_component_counts: tuple[tuple[int, int], ...]
    m5_rows_submitted: int
    m5_normalized_rows: int
    m5_rejected_rows: int
    normalized_rows_by_key: tuple[tuple[tuple[str, ...], int], ...] = field(repr=False)
    rejected_rows_by_key: tuple[tuple[tuple[str, ...], int], ...] = field(repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_complete_key_count": self.duplicate_complete_key_count,
            "duplicate_rows_in_complete_keys": self.duplicate_rows_in_complete_keys,
            "distinct_complete_keys": self.distinct_complete_keys,
            "empty_component_counts": [
                {"component_index": i, "empty_rows": count}
                for i, count in self.empty_component_counts
            ],
            "key_multiplicity_distribution": [
                {"distinct_key_count": number, "multiplicity": multiple}
                for multiple, number in self.key_multiplicity_distribution
            ],
            "m4_records_accepted": self.m4_records_accepted,
            "m4_records_rejected": self.m4_records_rejected,
            "m4_records_seen": self.m4_records_seen,
            "m5_normalized_rows": self.m5_normalized_rows,
            "m5_rows_submitted": self.m5_rows_submitted,
            "m5_rejected_rows": self.m5_rejected_rows,
            "rows_examined": self.rows_examined,
            "rows_with_all_empty_components": self.rows_with_all_empty_components,
            "rows_with_all_fields_present": self.rows_with_all_fields_present,
            "rows_with_any_empty_component": self.rows_with_any_empty_component,
            "rows_with_complete_key": self.rows_with_complete_key,
            "rows_with_partial_key": self.rows_with_partial_key,
            "side_stats_contract_id": JOIN_SIDE_STATS_CONTRACT_ID,
            "source_kind": self.source_kind.value,
        }

    @property
    def key_counts(self) -> tuple[tuple[tuple[str, ...], int], ...]:
        # Private source-key material is retained only by the in-memory analyzer,
        # never serialized into the portable report.
        return self._key_counts

    _key_counts: tuple[tuple[tuple[str, ...], int], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class JoinCardinalityStatsV1:
    key_counts: tuple[tuple[str, int], ...]
    replay_rows_by_cardinality: tuple[tuple[str, int], ...]
    game_rows_by_cardinality: tuple[tuple[str, int], ...]
    replay_matched_rows: int
    replay_unmatched_rows: int
    replay_ambiguous_rows: int
    game_matched_rows: int
    game_unmatched_rows: int
    game_ambiguous_rows: int
    replay_matched_m5_normalized: int
    replay_matched_m5_rejected: int
    replay_ambiguous_m5_normalized: int
    replay_ambiguous_m5_rejected: int
    replay_unmatched_m5_normalized: int
    replay_unmatched_m5_rejected: int
    replay_unambiguous_percent_of_complete_key_rows: float
    game_unambiguous_percent_of_complete_key_rows: float

    def to_dict(self) -> dict[str, object]:
        return {
            "cardinality_contract_id": JOIN_CARDINALITY_CONTRACT_ID,
            "game_ambiguous_rows": self.game_ambiguous_rows,
            "game_matched_rows": self.game_matched_rows,
            "game_unambiguous_percent_of_complete_key_rows": (
                self.game_unambiguous_percent_of_complete_key_rows
            ),
            "game_unmatched_rows": self.game_unmatched_rows,
            "game_rows_by_cardinality": dict(self.game_rows_by_cardinality),
            "key_counts": dict(self.key_counts),
            "replay_ambiguous_m5_normalized": self.replay_ambiguous_m5_normalized,
            "replay_ambiguous_m5_rejected": self.replay_ambiguous_m5_rejected,
            "replay_ambiguous_rows": self.replay_ambiguous_rows,
            "replay_matched_m5_normalized": self.replay_matched_m5_normalized,
            "replay_matched_m5_rejected": self.replay_matched_m5_rejected,
            "replay_matched_rows": self.replay_matched_rows,
            "replay_rows_by_cardinality": dict(self.replay_rows_by_cardinality),
            "replay_unambiguous_percent_of_complete_key_rows": (
                self.replay_unambiguous_percent_of_complete_key_rows
            ),
            "replay_unmatched_rows": self.replay_unmatched_rows,
            "replay_unmatched_m5_normalized": self.replay_unmatched_m5_normalized,
            "replay_unmatched_m5_rejected": self.replay_unmatched_m5_rejected,
        }


@dataclass(frozen=True, slots=True)
class JoinEvidenceDecisionV1:
    status: JoinDecision
    reason: str
    approved_for_scope: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "approved_for_scope": self.approved_for_scope,
            "decision_contract_id": JOIN_DECISION_CONTRACT_ID,
            "reason": self.reason,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class JoinEvidenceReportDecisionV1:
    status: JoinReportDecisionStatus
    approved_candidate_id: str | None
    reason: str
    scope: str

    def to_dict(self) -> dict[str, object]:
        return {
            "approved_candidate_id": self.approved_candidate_id,
            "decision_contract_id": JOIN_REPORT_DECISION_CONTRACT_ID,
            "reason": self.reason,
            "scope": self.scope,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class JoinCandidateEvidenceV1:
    candidate: JoinCandidateV1
    replay_stats: JoinCandidateSideStatsV1
    game_stats: JoinCandidateSideStatsV1
    cardinality: JoinCardinalityStatsV1
    decision: JoinEvidenceDecisionV1

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "cardinality": self.cardinality.to_dict(),
            "decision": self.decision.to_dict(),
            "game_stats": self.game_stats.to_dict(),
            "replay_stats": self.replay_stats.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class JoinEvidenceReportV1:
    semantic_source_catalog_digest: str
    m3_evidence_digest: str
    schema_registry_digest: str
    replay_mapping_id: str
    replay_mapping_registry_digest: str
    game_mapping_id: str
    game_mapping_registry_digest: str
    replay_source_interpretation_contract_id: str
    game_source_interpretation_contract_id: str
    replay_source_archive_id: str
    game_source_archive_id: str
    replay_raw_schema_fingerprint: str
    game_raw_schema_fingerprint: str
    m4_reader_contract_id: str
    evidence_scope: str
    replay_m4_completion_status: str
    game_m4_completion_status: str
    overall_decision: JoinEvidenceReportDecisionV1
    candidate_field_inventory: tuple[JoinFieldEvidenceV1, ...]
    candidate_results: tuple[JoinCandidateEvidenceV1, ...]
    replay_m5_rejection_counts: tuple[tuple[str, int], ...]
    reciprocal_game_row_evidence: dict[str, object]
    game_number_game_index_relation: dict[str, object]
    report_digest: str

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "candidate_field_inventory": [
                item.to_dict() for item in self.candidate_field_inventory
            ],
            "candidate_results": [item.to_dict() for item in self.candidate_results],
            "evidence_scope": self.evidence_scope,
            "game_m4_completion_status": self.game_m4_completion_status,
            "game_mapping_id": self.game_mapping_id,
            "game_mapping_registry_digest": self.game_mapping_registry_digest,
            "game_raw_schema_fingerprint": self.game_raw_schema_fingerprint,
            "game_source_archive_id": self.game_source_archive_id,
            "game_source_interpretation_contract_id": self.game_source_interpretation_contract_id,
            "m3_evidence_digest": self.m3_evidence_digest,
            "m4_reader_contract_id": self.m4_reader_contract_id,
            "game_number_game_index_relation": self.game_number_game_index_relation,
            "reciprocal_game_row_evidence": self.reciprocal_game_row_evidence,
            "replay_mapping_id": self.replay_mapping_id,
            "replay_mapping_registry_digest": self.replay_mapping_registry_digest,
            "replay_m4_completion_status": self.replay_m4_completion_status,
            "replay_raw_schema_fingerprint": self.replay_raw_schema_fingerprint,
            "replay_source_archive_id": self.replay_source_archive_id,
            "replay_source_interpretation_contract_id": (
                self.replay_source_interpretation_contract_id
            ),
            "report_contract_id": JOIN_EVIDENCE_REPORT_CONTRACT_ID,
            "schema_registry_digest": self.schema_registry_digest,
            "overall_decision": self.overall_decision.to_dict(),
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "replay_m5_rejection_counts": dict(self.replay_m5_rejection_counts),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.semantic_projection_dict(),
            "report_digest_contract_id": JOIN_REPORT_DIGEST_CONTRACT_ID,
            "report_digest": self.report_digest,
        }


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8", errors="strict"
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def derive_join_candidate_id(
    replay_fields: tuple[JoinFieldReferenceV1, ...],
    game_fields: tuple[JoinFieldReferenceV1, ...],
    scope: str,
) -> str:
    return "openmtgdata.replay-game-join-candidate.v1:" + _sha256(
        {
            "candidate_contract_id": JOIN_CANDIDATE_CONTRACT_ID,
            "candidate_scope": scope,
            "comparison_contract_id": EXACT_STRING_COMPARISON_CONTRACT_ID,
            "game_fields": [item.to_dict() for item in game_fields],
            "replay_fields": [item.to_dict() for item in replay_fields],
        }
    )


class CandidateSideAccumulatorV1:
    """Retain only exact complete key counts and bounded scalar counters."""

    def __init__(
        self,
        source_kind: SourceKind,
        selectors: tuple[JoinFieldReferenceV1, ...],
    ) -> None:
        self.source_kind = source_kind
        self.selectors = selectors
        self.rows_examined = 0
        self.empty_counts = [0] * len(selectors)
        self.all_empty = 0
        self.partial = 0
        self.complete = 0
        self.keys: Counter[tuple[str, ...]] = Counter()
        self.m5_normalized = 0
        self.m5_rejected = 0
        self.m5_submitted = 0
        self.m5_normalized_by_key: Counter[tuple[str, ...]] = Counter()
        self.m5_rejected_by_key: Counter[tuple[str, ...]] = Counter()

    def observe(
        self,
        fields: tuple[str, ...],
        *,
        m5_normalized: bool | None = None,
    ) -> tuple[str, ...] | None:
        self.rows_examined += 1
        if m5_normalized is True:
            self.m5_submitted += 1
            self.m5_normalized += 1
        elif m5_normalized is False:
            self.m5_submitted += 1
            self.m5_rejected += 1
        values: list[str] = []
        for index, selector in enumerate(self.selectors):
            if selector.column_index >= len(fields):
                raise JoinEvidenceError("accepted M4 row does not contain candidate field selector")
            value = fields[selector.column_index]
            values.append(value)
            if value == "":
                self.empty_counts[index] += 1
        empty_count = sum(value == "" for value in values)
        if empty_count == len(values):
            self.all_empty += 1
            return None
        if empty_count:
            self.partial += 1
            return None
        key = tuple(values)
        self.complete += 1
        self.keys[key] += 1
        if m5_normalized is True:
            self.m5_normalized_by_key[key] += 1
        elif m5_normalized is False:
            self.m5_rejected_by_key[key] += 1
        return key

    def freeze(
        self,
        *,
        m4_records_seen: int,
        m4_records_accepted: int,
        m4_records_rejected: int,
    ) -> JoinCandidateSideStatsV1:
        if m4_records_seen != m4_records_accepted + m4_records_rejected:
            raise JoinEvidenceError("M4 side record counts do not reconcile")
        if m4_records_accepted != self.rows_examined:
            raise JoinEvidenceError("candidate pass does not account for each accepted M4 row")
        if self.m5_submitted not in {0, self.rows_examined}:
            raise JoinEvidenceError("M5 status pass must account for every examined row")
        multiplicity = Counter(self.keys.values())
        duplicate_keys = sum(count for multiple, count in multiplicity.items() if multiple > 1)
        duplicate_rows = sum(
            count * multiple for multiple, count in multiplicity.items() if multiple > 1
        )
        return JoinCandidateSideStatsV1(
            self.source_kind,
            self.rows_examined,
            m4_records_seen,
            m4_records_accepted,
            m4_records_rejected,
            self.rows_examined,
            self.partial + self.all_empty,
            self.all_empty,
            self.partial,
            self.complete,
            len(self.keys),
            duplicate_keys,
            duplicate_rows,
            tuple(sorted(multiplicity.items())),
            tuple(enumerate(self.empty_counts)),
            self.m5_submitted,
            self.m5_normalized,
            self.m5_rejected,
            tuple(sorted(self.m5_normalized_by_key.items())),
            tuple(sorted(self.m5_rejected_by_key.items())),
            tuple(sorted(self.keys.items())),
        )


class ReciprocalGameRowEvidenceV1:
    """Aggregate source-field symmetry for duplicate Game (draft_id, game_number) keys."""

    def __init__(self, column_indices: tuple[int, ...]) -> None:
        if len(column_indices) != 8 or any(index < 0 for index in column_indices):
            raise JoinEvidenceError("reciprocal Game review requires eight exact field indexes")
        self._columns = column_indices
        self._first: dict[tuple[str, str], tuple[tuple[str, ...], int]] = {}
        self._profiles: dict[tuple[str, str], tuple[bool, bool, bool, bool]] = {}

    def observe(self, fields: tuple[str, ...]) -> None:
        draft_id = fields[self._columns[0]]
        game_number = fields[self._columns[1]]
        if not draft_id or not game_number:
            return
        signature = tuple(fields[index] for index in self._columns[2:])
        key = (draft_id, game_number)
        previous = self._first.get(key)
        if previous is None:
            self._first[key] = (signature, 1)
            return
        first, count = previous
        if count == 1:
            rank_reciprocal = first[0] == signature[1] and first[1] == signature[0]
            mulligan_reciprocal = first[2] == signature[3] and first[3] == signature[2]
            on_play_equal = first[4] == signature[4]
            won_equal = first[5] == signature[5]
            self._profiles[key] = (
                rank_reciprocal,
                mulligan_reciprocal,
                on_play_equal,
                won_equal,
            )
            self._first[key] = (first, 2)
        elif count == 2:
            self._profiles.pop(key, None)
            self._first[key] = (first, 3)
        else:
            self._first[key] = (first, count + 1)

    def summarize(self, key_counts: tuple[tuple[tuple[str, ...], int], ...]) -> dict[str, object]:
        duplicate = Counter(multiplicity for _key, multiplicity in key_counts)
        exactly_two = {(key[0], key[1]) for key, multiplicity in key_counts if multiplicity == 2}
        profiles = [self._profiles[key] for key in exactly_two if key in self._profiles]
        return {
            "candidate_key": "(Game.draft_id, Game.game_number)",
            "interpretation": (
                "source-field symmetry measurements only; no participant identity "
                "or row pairing is asserted"
            ),
            "key_multiplicity_distribution": {
                str(multiplicity): number for multiplicity, number in sorted(duplicate.items())
            },
            "exactly_two_row_keys_observed": len(exactly_two),
            "two_row_keys_with_both_rows_observed": len(profiles),
            "rank_opp_rank_exact_reciprocal_keys": sum(x[0] for x in profiles),
            "mulligan_count_exact_reciprocal_keys": sum(x[1] for x in profiles),
            "on_play_source_lexemes_equal_keys": sum(x[2] for x in profiles),
            "on_play_source_lexemes_differing_keys": sum(not x[2] for x in profiles),
            "won_source_lexemes_equal_keys": sum(x[3] for x in profiles),
            "won_source_lexemes_differing_keys": sum(not x[3] for x in profiles),
            "keys_over_two_rows": sum(
                number for multiplicity, number in duplicate.items() if multiplicity > 2
            ),
        }


def measure_game_number_index_offsets(
    replay_key_counts: tuple[tuple[tuple[str, ...], int], ...],
    game_key_counts: tuple[tuple[tuple[str, ...], int], ...],
) -> dict[str, object]:
    """Measure candidate numeric offsets within equal exact source draft_id groups only."""
    replay_values: dict[str, set[str]] = {}
    game_values: dict[str, set[str]] = {}
    for key, _count in replay_key_counts:
        replay_values.setdefault(key[0], set()).add(key[-1])
    for key, _count in game_key_counts:
        game_values.setdefault(key[0], set()).add(key[-1])
    common_drafts = replay_values.keys() & game_values.keys()
    offset_matches = {-1: 0, 0: 0, 1: 0}
    violation_values = {-1: 0, 0: 0, 1: 0}
    noncanonical_game_values = 0
    noncanonical_replay_values = 0
    comparison_count = 0
    relation_profiles: Counter[str] = Counter()
    for draft_id in common_drafts:
        replay_ints: set[int] = set()
        for value in replay_values[draft_id]:
            if _CANONICAL_INT.fullmatch(value):
                replay_ints.add(int(value))
            else:
                noncanonical_replay_values += 1
        for game_value in game_values[draft_id]:
            if not _CANONICAL_INT.fullmatch(game_value):
                noncanonical_game_values += 1
                continue
            game_number = int(game_value)
            matched_offsets = [
                offset for offset in (-1, 0, 1) if game_number - offset in replay_ints
            ]
            comparison_count += 1
            if len(matched_offsets) > 1:
                relation_profiles["multiple_offsets_present"] += 1
            elif not matched_offsets:
                relation_profiles["no_exact_numeric_relation"] += 1
            else:
                offset = matched_offsets[0]
                offset_matches[offset] += 1
                relation_profiles[f"game_number_equals_game_index_plus_{offset}"] += 1
        game_value_count = sum(
            1 for value in game_values[draft_id] if _CANONICAL_INT.fullmatch(value)
        )
        for offset in offset_matches:
            offset_hits = sum(
                1
                for value in game_values[draft_id]
                if _CANONICAL_INT.fullmatch(value) and int(value) - offset in replay_ints
            )
            violation_values[offset] += game_value_count - offset_hits
    return {
        "analysis_scope": (
            "distinct canonical numeric values within exact shared draft_id groups; "
            "not row pairing or join authority"
        ),
        "shared_exact_draft_id_groups": len(common_drafts),
        "distinct_game_number_values_compared": comparison_count,
        "noncanonical_game_number_values": noncanonical_game_values,
        "noncanonical_game_index_values": noncanonical_replay_values,
        "per_value_relation_counts": dict(sorted(relation_profiles.items())),
        "candidate_constant_offsets": {
            str(offset): {
                "game_values_with_relation": offset_matches[offset],
                "game_values_without_relation": violation_values[offset],
                "rule": f"Game.game_number = Replay.game_index + ({offset})",
            }
            for offset in (-1, 0, 1)
        },
        "offset_normalization_applied": False,
    }


def _percent(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 6) if denominator else 0.0


def compare_candidate_cardinality(
    replay: JoinCandidateSideStatsV1,
    game: JoinCandidateSideStatsV1,
) -> JoinCardinalityStatsV1:
    replay_counts = dict(replay.key_counts)
    game_counts = dict(game.key_counts)
    replay_norm = dict(replay.normalized_rows_by_key)
    replay_rej = dict(replay.rejected_rows_by_key)
    categories: Counter[str] = Counter()
    replay_rows: Counter[str] = Counter()
    game_rows: Counter[str] = Counter()
    matched_r = unmatched_r = ambiguous_r = 0
    matched_g = unmatched_g = ambiguous_g = 0
    matched_r_norm = matched_r_rej = ambiguous_r_norm = ambiguous_r_rej = 0
    unmatched_r_norm = unmatched_r_rej = 0
    for key in replay_counts.keys() | game_counts.keys():
        rn = replay_counts.get(key, 0)
        gn = game_counts.get(key, 0)
        if rn == 0:
            category = "1_game:0_replay" if gn == 1 else "N_game:0_replay"
        elif gn == 0:
            category = "0_game:1_replay" if rn == 1 else "0_game:N_replay"
        elif rn == 1 and gn == 1:
            category = "1_game:1_replay"
        elif rn == 1:
            category = "N_game:1_replay"
        elif gn == 1:
            category = "1_game:N_replay"
        else:
            category = "N_game:N_replay"
        categories[category] += 1
        replay_rows[category] += rn
        game_rows[category] += gn
        if rn and gn and category == "1_game:1_replay":
            matched_r += rn
            matched_g += gn
            matched_r_norm += replay_norm.get(key, 0)
            matched_r_rej += replay_rej.get(key, 0)
        elif rn and gn:
            ambiguous_r += rn
            ambiguous_g += gn
            ambiguous_r_norm += replay_norm.get(key, 0)
            ambiguous_r_rej += replay_rej.get(key, 0)
        elif rn:
            unmatched_r += rn
            unmatched_r_norm += replay_norm.get(key, 0)
            unmatched_r_rej += replay_rej.get(key, 0)
        elif gn:
            unmatched_g += gn
    return JoinCardinalityStatsV1(
        tuple(sorted(categories.items())),
        tuple(sorted(replay_rows.items())),
        tuple(sorted(game_rows.items())),
        matched_r,
        unmatched_r,
        ambiguous_r,
        matched_g,
        unmatched_g,
        ambiguous_g,
        matched_r_norm,
        matched_r_rej,
        ambiguous_r_norm,
        ambiguous_r_rej,
        unmatched_r_norm,
        unmatched_r_rej,
        _percent(matched_r, replay.rows_with_complete_key),
        _percent(matched_g, game.rows_with_complete_key),
    )


def decide_candidate(
    cardinality: JoinCardinalityStatsV1,
    replay: JoinCandidateSideStatsV1,
    game: JoinCandidateSideStatsV1,
    *,
    semantics_documented: bool = False,
) -> JoinEvidenceDecisionV1:
    if cardinality.replay_ambiguous_rows or cardinality.game_ambiguous_rows:
        return JoinEvidenceDecisionV1(
            JoinDecision.AMBIGUOUS,
            "Observed shared candidate values include non-1:1 cardinalities; "
            "this candidate is not an authoritative row key.",
            None,
        )
    if cardinality.replay_matched_rows == 0 or cardinality.game_matched_rows == 0:
        return JoinEvidenceDecisionV1(
            JoinDecision.UNSUPPORTED,
            "No exact unambiguous cross-source row matches were observed for this candidate.",
            None,
        )
    if cardinality.replay_unmatched_rows or cardinality.game_unmatched_rows:
        return JoinEvidenceDecisionV1(
            JoinDecision.PARTIAL_UNAMBIGUOUS_COVERAGE,
            "Observed shared keys are 1:1 but eligible unmatched rows remain; "
            "no key is approved without documented source semantics.",
            None,
        )
    if not semantics_documented:
        return JoinEvidenceDecisionV1(
            JoinDecision.UNSUPPORTED,
            "Observed exact 1:1 cardinality is not sufficient to establish source semantics "
            "or identity correspondence from the available M3/M5 evidence.",
            None,
        )
    return JoinEvidenceDecisionV1(
        JoinDecision.SUPPORTED_SCOPED_JOIN_KEY,
        "Exact 1:1 full eligible-row coverage and source semantics were established "
        "for this archive pair.",
        "AFR PremierDraft exact reviewed archive pair only",
    )


def candidate_evidence(
    candidate: JoinCandidateV1,
    replay: JoinCandidateSideStatsV1,
    game: JoinCandidateSideStatsV1,
    *,
    semantics_documented: bool = False,
) -> JoinCandidateEvidenceV1:
    cardinality = compare_candidate_cardinality(replay, game)
    decision = decide_candidate(
        cardinality, replay, game, semantics_documented=semantics_documented
    )
    return JoinCandidateEvidenceV1(candidate, replay, game, cardinality, decision)


def report_digest(report_projection: dict[str, object]) -> str:
    return _sha256(
        {
            "digest_contract_id": JOIN_REPORT_DIGEST_CONTRACT_ID,
            "report_projection": report_projection,
        }
    )


def build_join_evidence_report(
    *,
    semantic_source_catalog_digest: str,
    m3_evidence_digest: str,
    schema_registry_digest: str,
    replay_mapping_id: str,
    replay_mapping_registry_digest: str,
    game_mapping_id: str,
    game_mapping_registry_digest: str,
    replay_source_interpretation_contract_id: str,
    game_source_interpretation_contract_id: str,
    replay_source_archive_id: str,
    game_source_archive_id: str,
    replay_raw_schema_fingerprint: str,
    game_raw_schema_fingerprint: str,
    candidate_field_inventory: tuple[JoinFieldEvidenceV1, ...],
    candidate_results: tuple[JoinCandidateEvidenceV1, ...],
    replay_m5_rejection_counts: tuple[tuple[str, int], ...],
    reciprocal_game_row_evidence: dict[str, object],
    game_number_game_index_relation: dict[str, object],
    replay_m4_completion_status: str = "complete",
    game_m4_completion_status: str = "complete",
) -> JoinEvidenceReportV1:
    digest_bindings = (
        semantic_source_catalog_digest,
        m3_evidence_digest,
        schema_registry_digest,
        replay_mapping_registry_digest,
        game_mapping_registry_digest,
        replay_source_archive_id,
        game_source_archive_id,
        replay_raw_schema_fingerprint,
        game_raw_schema_fingerprint,
    )
    if any(_SHA256.fullmatch(value) is None for value in digest_bindings):
        raise JoinEvidenceError("M6.1 report contains malformed source or schema identities")
    if (
        _REPLAY_MAPPING_ID.fullmatch(replay_mapping_id) is None
        or _GAME_MAPPING_ID.fullmatch(game_mapping_id) is None
    ):
        raise JoinEvidenceError("M6.1 report has malformed M5 mapping identities")
    if (
        _INTERPRETATION_ID.fullmatch(replay_source_interpretation_contract_id) is None
        or _INTERPRETATION_ID.fullmatch(game_source_interpretation_contract_id) is None
    ):
        raise JoinEvidenceError("M6.1 report has malformed source interpretation bindings")
    candidate_ids = [item.candidate.candidate_id for item in candidate_results]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise JoinEvidenceError("M6.1 report contains duplicate candidate identities")
    for result in candidate_results:
        replay_stats, game_stats = result.replay_stats, result.game_stats
        for stats in (replay_stats, game_stats):
            if stats.m4_records_seen != stats.m4_records_accepted + stats.m4_records_rejected:
                raise JoinEvidenceError("M6.1 side M4 counts do not reconcile")
            if stats.rows_examined != stats.m4_records_accepted:
                raise JoinEvidenceError("M6.1 key evidence does not cover accepted M4 rows")
        if replay_stats.m5_rows_submitted not in {0, replay_stats.m4_records_accepted}:
            raise JoinEvidenceError("M6.1 Replay M5 submission count does not reconcile")
        if game_stats.m5_rows_submitted not in {0, game_stats.m4_records_accepted}:
            raise JoinEvidenceError("M6.1 Game M5 submission count does not reconcile")
        if replay_stats.m5_normalized_rows + replay_stats.m5_rejected_rows not in {
            0,
            replay_stats.m5_rows_submitted,
        }:
            raise JoinEvidenceError("M6.1 Replay M5 outcome counts do not reconcile")
        if game_stats.m5_normalized_rows + game_stats.m5_rejected_rows not in {
            0,
            game_stats.m5_rows_submitted,
        }:
            raise JoinEvidenceError("M6.1 Game M5 outcome counts do not reconcile")
    if candidate_results and sum(count for _code, count in replay_m5_rejection_counts) != (
        candidate_results[0].replay_stats.m5_rejected_rows
    ):
        raise JoinEvidenceError("M6.1 Replay M5 diagnostic counts do not reconcile")
    ordered_results = tuple(sorted(candidate_results, key=lambda item: item.candidate.candidate_id))
    scope = "AFR PremierDraft reviewed archive pair and declared candidate inventory"
    approved = [
        item
        for item in ordered_results
        if item.decision.status is JoinDecision.SUPPORTED_SCOPED_JOIN_KEY
    ]
    if replay_m4_completion_status != "complete" or game_m4_completion_status != "complete":
        overall = JoinEvidenceReportDecisionV1(
            JoinReportDecisionStatus.ANALYSIS_INCOMPLETE,
            None,
            (
                "Both M4-v2 streams must complete terminal verification before "
                "join authority can be decided."
            ),
            scope,
        )
    elif not ordered_results:
        overall = JoinEvidenceReportDecisionV1(
            JoinReportDecisionStatus.ANALYSIS_INCOMPLETE,
            None,
            "No candidate definitions were evaluated; join authority remains undetermined.",
            scope,
        )
    elif len(approved) == 1:
        overall = JoinEvidenceReportDecisionV1(
            JoinReportDecisionStatus.SUPPORTED_SCOPED_JOIN,
            approved[0].candidate.candidate_id,
            "Exactly one declared candidate has an approved scoped decision.",
            scope,
        )
    elif approved:
        raise JoinEvidenceError("multiple candidates cannot be approved as overall join authority")
    else:
        overall = JoinEvidenceReportDecisionV1(
            JoinReportDecisionStatus.JOIN_UNSUPPORTED,
            None,
            (
                "All declared candidates were evaluated; none establishes an "
                "authoritative row key in scope."
            ),
            scope,
        )
    provisional = JoinEvidenceReportV1(
        semantic_source_catalog_digest,
        m3_evidence_digest,
        schema_registry_digest,
        replay_mapping_id,
        replay_mapping_registry_digest,
        game_mapping_id,
        game_mapping_registry_digest,
        replay_source_interpretation_contract_id,
        game_source_interpretation_contract_id,
        replay_source_archive_id,
        game_source_archive_id,
        replay_raw_schema_fingerprint,
        game_raw_schema_fingerprint,
        "openmtgdata.raw-source-reader.v2",
        "AFR PremierDraft archive-pair M4-accepted source rows only; no joined rows emitted",
        replay_m4_completion_status,
        game_m4_completion_status,
        overall,
        tuple(
            sorted(
                candidate_field_inventory,
                key=lambda item: (
                    item.reference.source_kind.value,
                    item.reference.column_index,
                    item.reference.exact_header_name,
                ),
            )
        ),
        ordered_results,
        tuple(sorted(replay_m5_rejection_counts)),
        reciprocal_game_row_evidence,
        game_number_game_index_relation,
        "",
    )
    return replace(
        provisional,
        report_digest=report_digest(provisional.semantic_projection_dict()),
    )


def validate_join_evidence_report(report: JoinEvidenceReportV1) -> None:
    if report.m4_reader_contract_id != "openmtgdata.raw-source-reader.v2":
        raise JoinEvidenceError("M6.1 report does not bind M4 reader v2")
    if _SHA256.fullmatch(report.report_digest) is None:
        raise JoinEvidenceError("M6.1 report digest is malformed")
    if report.report_digest != report_digest(report.semantic_projection_dict()):
        raise JoinEvidenceError("M6.1 report semantic digest mismatch")
