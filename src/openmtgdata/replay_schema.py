"""Evidence analysis and versioned M5.1 Replay mapping contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum

from openmtgdata.schema_evolution import (
    FieldSurfaceV1,
    RawSchemaGroupEvidenceV1,
    VerifiedM3EvidenceV1,
)
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import VerifiedSchemaRegistryV1

REPLAY_EVENT_SCHEMA_ID = "openmtgdata.replay-event.v1"
REPLAY_FIELD_MAPPING_CONTRACT_ID = "openmtgdata.replay-field-mapping.v1"
REPLAY_FIELD_MAPPING_ID_CONTRACT_ID = "openmtgdata.replay-field-mapping-id.v1"
REPLAY_FIELD_LINEAGE_CONTRACT_ID = "openmtgdata.replay-field-lineage.v1"
REPLAY_NORMALIZATION_CONTRACT_ID = "openmtgdata.replay-normalization.v1"
REPLAY_NORMALIZATION_QUALITY_CONTRACT_ID = "openmtgdata.replay-normalization-quality.v1"
REPLAY_EVENT_LOCATOR_CONTRACT_ID = "openmtgdata.replay-event-locator.v1"
REPLAY_MAPPING_REGISTRY_CONTRACT_ID = "openmtgdata.replay-mapping-registry.v1"
REPLAY_MAPPING_REGISTRY_DIGEST_CONTRACT_ID = "openmtgdata.replay-mapping-registry-digest.v1"
REPLAY_FIELD_ANALYSIS_CONTRACT_ID = "openmtgdata.replay-field-analysis.v1"
REPLAY_EVENT_BOUNDARY_RULE_ID = "openmtgdata.replay-boundary.per-side-turn-slot.v1"
REPLAY_EVENT_ORDINAL_RULE_ID = "openmtgdata.replay-event-ordinal.source-turn-order.v1"
REPLAY_TURN_SIDE_TRANSFORMATION_ID = "openmtgdata.replay-turn-side-from-on-play.v1"
REPLAY_TURN_INDEX_TRANSFORMATION_ID = "openmtgdata.replay-turn-slot-index-from-ordinal.v1"
STRICT_TURN_COUNT_PARSER_ID = "openmtgdata.strict-nonnegative-decimal-int.v1"
STRICT_ON_PLAY_PARSER_ID = "openmtgdata.strict-zero-one-bool.v1"
PRESERVE_SOURCE_STRING_TRANSFORM_ID = "openmtgdata.preserve-csv-string.v1"
REPLAY_MAPPING_POLICY_ID = "openmtgdata.replay-mapping-policy.v1"
_TURN_FIELD_RE = re.compile(r"(user|oppo)_turn_([1-9][0-9]*)_(.+)\Z")
_INT_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


class ReplaySchemaError(RuntimeError):
    """Invalid M3/M4 evidence or an inconsistent Replay mapping contract."""


class MappingDisposition(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED_EVENT_STRUCTURE = "unsupported_event_structure"
    UNSUPPORTED_SEMANTIC_MAPPING = "unsupported_semantic_mapping"
    NEEDS_REVIEW = "needs_review"


class FieldDisposition(StrEnum):
    MAPPED = "mapped"
    UNMAPPED_PRESERVED_AT_RAW_LAYER = "unmapped_preserved_at_raw_layer"
    UNSUPPORTED = "unsupported"
    IGNORED_BY_REVIEWED_POLICY = "ignored_by_reviewed_policy"


class FieldOrigin(StrEnum):
    SOURCE_DIRECT = "source_direct"
    DETERMINISTIC_NORMALIZATION = "deterministic_normalization"
    RECONSTRUCTED = "reconstructed"


class ReplayTurnSideV1(StrEnum):
    """Closed source-relative turn-family labels; these are not player identities."""

    USER = "user"
    OPPO = "oppo"


class ReplayQualityCode(StrEnum):
    UNMAPPED_SOURCE_FIELDS_PRESENT = "unmapped_source_fields_present"
    SOURCE_ZERO_TURN_COUNT = "source_zero_turn_count"
    INVALID_SOURCE_TURN_COUNT = "invalid_source_turn_count"
    INVALID_SOURCE_ON_PLAY = "invalid_source_on_play"
    TURN_COUNT_EXCEEDS_PHYSICAL_SLOTS = "turn_count_exceeds_physical_slots"
    PARTIAL_EVENT_SLOT = "partial_event_slot"
    M4_WIDTH_REJECTED_RECORDS_EXCLUDED = "m4_width_rejected_records_excluded"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def derive_replay_field_mapping_id(projection: dict[str, object]) -> str:
    """Derive an M5 mapping identity without changing physical/M3 schema identities."""
    if projection.get("mapping_schema_id") != REPLAY_FIELD_MAPPING_CONTRACT_ID:
        raise ReplaySchemaError("mapping projection uses an unsupported schema ID")
    if projection.get("replay_field_mapping_id_contract_id") != (
        REPLAY_FIELD_MAPPING_ID_CONTRACT_ID
    ):
        raise ReplaySchemaError("mapping projection uses an unsupported ID contract")
    return f"openmtgdata.replay-field-mapping.v1:{_digest(projection)}"


@dataclass(frozen=True, slots=True)
class SourceColumnSelectorV1:
    column_index: int
    exact_header_name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "column_index": self.column_index,
            "exact_header_name": self.exact_header_name,
        }


@dataclass(frozen=True, slots=True)
class ReplayIndexedFieldFamilyV1:
    source_side_prefix: str
    minimum_slot_index: int
    maximum_slot_index: int
    slot_count: int
    exact_suffixes: tuple[str, ...]
    contiguous_indices: bool
    identical_suffix_surface_per_slot: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "contiguous_indices": self.contiguous_indices,
            "exact_suffixes": list(self.exact_suffixes),
            "identical_suffix_surface_per_slot": self.identical_suffix_surface_per_slot,
            "maximum_slot_index": self.maximum_slot_index,
            "minimum_slot_index": self.minimum_slot_index,
            "slot_count": self.slot_count,
            "source_side_prefix": self.source_side_prefix,
        }


@dataclass(frozen=True, slots=True)
class ReplayFieldLineageV1:
    normalized_field_path: str
    origin: FieldOrigin
    source_selectors: tuple[SourceColumnSelectorV1, ...]
    event_side: str | None
    event_slot_index: int | None
    transformation_id: str | None
    input_cardinality: str
    type_parser_id: str
    empty_value_policy: str
    failure_behavior: str
    evidence_basis: tuple[str, ...]
    derived_inputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "empty_value_policy": self.empty_value_policy,
            "event_side": self.event_side,
            "event_slot_index": self.event_slot_index,
            "evidence_basis": list(self.evidence_basis),
            "failure_behavior": self.failure_behavior,
            "derived_inputs": list(self.derived_inputs),
            "input_cardinality": self.input_cardinality,
            "lineage_contract_id": REPLAY_FIELD_LINEAGE_CONTRACT_ID,
            "normalized_field_path": self.normalized_field_path,
            "origin": self.origin.value,
            "source_selectors": [selector.to_dict() for selector in self.source_selectors],
            "transformation_id": self.transformation_id,
            "type_parser_id": self.type_parser_id,
        }


@dataclass(frozen=True, slots=True)
class ReplaySourceFieldMappingV1:
    selector: SourceColumnSelectorV1
    event_side: str
    event_slot_index: int
    exact_suffix: str
    normalized_field_path: str
    disposition: FieldDisposition
    origin: FieldOrigin
    transformation_id: str | None
    type_parser_id: str
    empty_value_policy: str
    failure_behavior: str

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "empty_value_policy": self.empty_value_policy,
            "event_side": self.event_side,
            "event_slot_index": self.event_slot_index,
            "exact_suffix": self.exact_suffix,
            "failure_behavior": self.failure_behavior,
            "normalized_field_path": self.normalized_field_path,
            "origin": self.origin.value,
            "selector": self.selector.to_dict(),
            "transformation_id": self.transformation_id,
            "type_parser_id": self.type_parser_id,
        }


@dataclass(frozen=True, slots=True)
class ReplayFieldDispositionV1:
    selector: SourceColumnSelectorV1
    disposition: FieldDisposition
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "reason": self.reason,
            "selector": self.selector.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReplayFieldMappingV1:
    mapping_schema_id: str
    replay_field_mapping_id: str
    replay_field_mapping_id_contract_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    source_kind: SourceKind
    boundary_rule_id: str
    event_ordinal_rule_id: str
    turn_count_selector: SourceColumnSelectorV1
    on_play_selector: SourceColumnSelectorV1
    turn_slot_families: tuple[ReplayIndexedFieldFamilyV1, ...]
    mapped_turn_fields: tuple[ReplaySourceFieldMappingV1, ...]
    field_dispositions: tuple[ReplayFieldDispositionV1, ...]
    field_lineage: tuple[ReplayFieldLineageV1, ...]
    turn_count_parser_id: str
    on_play_parser_id: str
    maximum_supported_turn_count: int
    active_slot_empty_policy: str
    out_of_range_slot_policy: str
    record_rejection_policy: str
    normalization_quality_contract_id: str
    semantic_field_origin_policy: str
    reconstruction_policy: str
    evidence_bindings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mapping_schema_id != REPLAY_FIELD_MAPPING_CONTRACT_ID:
            raise ReplaySchemaError("unsupported Replay field mapping schema")
        if not _INT_SHA_RE.fullmatch(self.raw_schema_fingerprint):
            raise ReplaySchemaError("Replay mapping raw fingerprint is malformed")
        if not self.source_interpretation_contract_id.startswith(
            "openmtgdata.source-interpretation.v1:"
        ):
            raise ReplaySchemaError("Replay mapping M3.2 interpretation ID is malformed")
        if self.source_kind is not SourceKind.REPLAY:
            raise ReplaySchemaError("Replay mapping source kind must be Replay")
        if self.replay_field_mapping_id and self.replay_field_mapping_id != (
            derive_replay_field_mapping_id(self.identity_projection_dict())
        ):
            raise ReplaySchemaError("Replay mapping ID does not match its canonical projection")

    def identity_projection_dict(self) -> dict[str, object]:
        return {
            "active_slot_empty_policy": self.active_slot_empty_policy,
            "boundary_rule_id": self.boundary_rule_id,
            "event_ordinal_rule_id": self.event_ordinal_rule_id,
            "field_dispositions": [item.to_dict() for item in self.field_dispositions],
            "field_lineage_contract_id": REPLAY_FIELD_LINEAGE_CONTRACT_ID,
            "field_lineage": [item.to_dict() for item in self.field_lineage],
            "mapping_schema_id": self.mapping_schema_id,
            "maximum_supported_turn_count": self.maximum_supported_turn_count,
            "normalization_contract_id": REPLAY_NORMALIZATION_CONTRACT_ID,
            "normalization_quality_contract_id": self.normalization_quality_contract_id,
            "on_play_parser_id": self.on_play_parser_id,
            "on_play_selector": self.on_play_selector.to_dict(),
            "out_of_range_slot_policy": self.out_of_range_slot_policy,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "record_rejection_policy": self.record_rejection_policy,
            "reconstruction_policy": self.reconstruction_policy,
            "replay_event_schema_id": REPLAY_EVENT_SCHEMA_ID,
            "replay_field_mapping_contract_id": REPLAY_FIELD_MAPPING_CONTRACT_ID,
            "replay_field_mapping_id_contract_id": self.replay_field_mapping_id_contract_id,
            "semantic_field_origin_policy": self.semantic_field_origin_policy,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_kind": self.source_kind.value,
            "turn_count_parser_id": self.turn_count_parser_id,
            "turn_count_selector": self.turn_count_selector.to_dict(),
            "turn_slot_families": [item.to_dict() for item in self.turn_slot_families],
            "mapped_turn_fields": [item.to_dict() for item in self.mapped_turn_fields],
            "evidence_bindings": list(self.evidence_bindings),
        }

    def to_dict(self) -> dict[str, object]:
        disposition_counts = Counter(item.disposition.value for item in self.field_dispositions)
        lineage_origins = Counter(item.origin.value for item in self.field_lineage)
        return {
            **self.identity_projection_dict(),
            "mapping_summary": {
                "header_field_count": len(self.field_dispositions),
                "mapped_physical_source_column_count": disposition_counts.get(
                    FieldDisposition.MAPPED.value, 0
                ),
                "unmapped_preserved_at_raw_layer_count": disposition_counts.get(
                    FieldDisposition.UNMAPPED_PRESERVED_AT_RAW_LAYER.value, 0
                ),
                "lineage_entry_count": len(self.field_lineage),
                "source_direct_lineage_count": lineage_origins.get(
                    FieldOrigin.SOURCE_DIRECT.value, 0
                ),
                "deterministic_normalization_lineage_count": lineage_origins.get(
                    FieldOrigin.DETERMINISTIC_NORMALIZATION.value, 0
                ),
                "reconstructed_lineage_count": lineage_origins.get(
                    FieldOrigin.RECONSTRUCTED.value, 0
                ),
            },
            "replay_field_mapping_id": self.replay_field_mapping_id,
        }


@dataclass(frozen=True, slots=True)
class ReplayMappingReviewV1:
    raw_schema_fingerprint: str
    source_archive_id: str
    m4_reader_completion_status: str
    m4_records_seen: int
    m4_records_accepted: int
    m4_width_rejected_records: int
    m5_records_submitted: int
    m5_records_normalized: int
    m5_records_rejected: int
    event_count: int
    zero_event_records: int
    partial_event_records: int
    turn_count_mismatch_records: int
    turn_count_histogram: tuple[tuple[int, int], ...]
    event_side_counts: tuple[tuple[str, int], ...]
    evidence_scope: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event_count": self.event_count,
            "event_side_counts": dict(self.event_side_counts),
            "evidence_scope": self.evidence_scope,
            "m4_reader_completion_status": self.m4_reader_completion_status,
            "m4_records_accepted": self.m4_records_accepted,
            "m4_records_seen": self.m4_records_seen,
            "m4_width_rejected_records": self.m4_width_rejected_records,
            "m5_records_normalized": self.m5_records_normalized,
            "m5_records_rejected": self.m5_records_rejected,
            "m5_records_submitted": self.m5_records_submitted,
            "partial_event_records": self.partial_event_records,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "source_archive_id": self.source_archive_id,
            "turn_count_histogram": [
                {"turn_count": turns, "record_count": count}
                for turns, count in self.turn_count_histogram
            ],
            "turn_count_mismatch_records": self.turn_count_mismatch_records,
            "zero_event_records": self.zero_event_records,
        }


@dataclass(frozen=True, slots=True)
class ReplayFieldEvidenceSummaryV1:
    column_index: int
    exact_header_name: str
    rows_observed: int
    lexical_class_counts: tuple[tuple[str, int], ...]
    empty_field_count: int
    non_empty_field_count: int
    source_null_semantics: str

    def to_dict(self) -> dict[str, object]:
        return {
            "column_index": self.column_index,
            "empty_field_count": self.empty_field_count,
            "exact_header_name": self.exact_header_name,
            "lexical_class_counts": dict(self.lexical_class_counts),
            "non_empty_field_count": self.non_empty_field_count,
            "rows_observed": self.rows_observed,
            "source_null_semantics": self.source_null_semantics,
        }


@dataclass(frozen=True, slots=True)
class ReplaySchemaGroupAnalysisV1:
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    archive_count: int
    member_source_archive_ids: tuple[str, ...]
    representative_source_archive_id: str
    representative_filename: str
    field_count: int
    exact_ordered_header_surface: tuple[str, ...]
    field_evidence: tuple[ReplayFieldEvidenceSummaryV1, ...]
    expansion_format_counts: tuple[tuple[str, str, int], ...]
    candidate_scalar_fields: tuple[str, ...]
    indexed_field_families: tuple[ReplayIndexedFieldFamilyV1, ...]
    candidate_event_structure: str
    m3_sampled_rows: int
    m3_row_width_shorter: int
    m3_row_width_longer: int
    mapping_disposition: MappingDisposition
    mapping_id: str | None
    disposition_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "candidate_event_structure": self.candidate_event_structure,
            "candidate_scalar_fields": list(self.candidate_scalar_fields),
            "disposition_reason": self.disposition_reason,
            "expansion_format_counts": [
                {"expansion": expansion, "format": fmt, "archive_count": count}
                for expansion, fmt, count in self.expansion_format_counts
            ],
            "exact_ordered_header_surface": list(self.exact_ordered_header_surface),
            "field_count": self.field_count,
            "field_evidence": [item.to_dict() for item in self.field_evidence],
            "indexed_field_families": [item.to_dict() for item in self.indexed_field_families],
            "m3_row_width_longer": self.m3_row_width_longer,
            "m3_row_width_shorter": self.m3_row_width_shorter,
            "m3_sampled_rows": self.m3_sampled_rows,
            "mapping_disposition": self.mapping_disposition.value,
            "mapping_id": self.mapping_id,
            "member_source_archive_ids": list(self.member_source_archive_ids),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "representative_filename": self.representative_filename,
            "representative_source_archive_id": self.representative_source_archive_id,
            "source_kind": SourceKind.REPLAY.value,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
        }


@dataclass(frozen=True, slots=True)
class ReplayFieldAnalysisV1:
    analysis_contract_id: str
    source_catalog_digest: str
    m3_evidence_digest: str
    schema_registry_digest: str
    groups: tuple[ReplaySchemaGroupAnalysisV1, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "analysis_contract_id": self.analysis_contract_id,
            "groups": [item.to_dict() for item in self.groups],
            "m3_evidence_digest": self.m3_evidence_digest,
            "schema_registry_digest": self.schema_registry_digest,
            "source_catalog_digest": self.source_catalog_digest,
        }


@dataclass(frozen=True, slots=True)
class ReplayMappingGroupDispositionV1:
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    source_kind: SourceKind
    archive_count: int
    disposition: MappingDisposition
    replay_field_mapping_id: str | None
    reason: str
    review_evidence: ReplayMappingReviewV1 | None

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "disposition": self.disposition.value,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "reason": self.reason,
            "replay_field_mapping_id": self.replay_field_mapping_id,
            "review_evidence": self.review_evidence.to_dict() if self.review_evidence else None,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_kind": self.source_kind.value,
        }


@dataclass(frozen=True, slots=True)
class ReplayMappingRegistryV1:
    mapping_registry_contract_id: str
    mapping_registry_digest_contract_id: str
    mapping_policy_id: str
    source_catalog_digest: str
    m3_evidence_digest: str
    schema_registry_digest: str
    mapping_contracts: tuple[ReplayFieldMappingV1, ...]
    group_dispositions: tuple[ReplayMappingGroupDispositionV1, ...]
    mapping_registry_digest: str

    def __post_init__(self) -> None:
        if self.mapping_registry_contract_id != REPLAY_MAPPING_REGISTRY_CONTRACT_ID:
            raise ReplaySchemaError("unsupported Replay mapping registry contract")
        if self.mapping_registry_digest_contract_id != REPLAY_MAPPING_REGISTRY_DIGEST_CONTRACT_ID:
            raise ReplaySchemaError("unsupported Replay mapping registry digest contract")
        if self.mapping_policy_id != REPLAY_MAPPING_POLICY_ID:
            raise ReplaySchemaError("unsupported Replay mapping policy")
        if not _INT_SHA_RE.fullmatch(self.mapping_registry_digest):
            raise ReplaySchemaError("Replay mapping registry digest is malformed")
        for name, digest in (
            ("source catalog", self.source_catalog_digest),
            ("M3 evidence", self.m3_evidence_digest),
            ("M3.2 schema registry", self.schema_registry_digest),
        ):
            if not _INT_SHA_RE.fullmatch(digest):
                raise ReplaySchemaError(f"Replay mapping registry {name} digest is malformed")
        if _digest(self.semantic_projection_dict()) != self.mapping_registry_digest:
            raise ReplaySchemaError("Replay mapping registry digest mismatch")
        dispositions = {item.raw_schema_fingerprint: item for item in self.group_dispositions}
        if len(dispositions) != len(self.group_dispositions):
            raise ReplaySchemaError("Replay mapping registry has duplicate group dispositions")
        contracts = {item.replay_field_mapping_id: item for item in self.mapping_contracts}
        if len(contracts) != len(self.mapping_contracts):
            raise ReplaySchemaError("Replay mapping registry has duplicate mapping IDs")
        supported = {
            item.raw_schema_fingerprint: item
            for item in self.group_dispositions
            if item.disposition is MappingDisposition.SUPPORTED
        }
        if set(supported) != {item.raw_schema_fingerprint for item in self.mapping_contracts}:
            raise ReplaySchemaError("supported groups and mapping contracts do not reconcile")
        for fingerprint, disposition in supported.items():
            if disposition.source_kind is not SourceKind.REPLAY:
                raise ReplaySchemaError(
                    "non-Replay source kind cannot enter Replay mapping registry"
                )
            contract = contracts.get(disposition.replay_field_mapping_id or "")
            if contract is None or contract.raw_schema_fingerprint != fingerprint:
                raise ReplaySchemaError("supported Replay group references the wrong mapping")
        if any(
            item.source_kind is not SourceKind.REPLAY
            or not _INT_SHA_RE.fullmatch(item.raw_schema_fingerprint)
            or not item.source_interpretation_contract_id.startswith(
                "openmtgdata.source-interpretation.v1:"
            )
            or not item.reason
            or (
                item.disposition is not MappingDisposition.SUPPORTED
                and item.replay_field_mapping_id is not None
            )
            for item in self.group_dispositions
        ):
            raise ReplaySchemaError("mapping registry group identity is invalid")

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "group_dispositions": [item.to_dict() for item in self.group_dispositions],
            "mapping_contracts": [item.to_dict() for item in self.mapping_contracts],
            "mapping_policy_id": self.mapping_policy_id,
            "mapping_registry_contract_id": self.mapping_registry_contract_id,
            "mapping_registry_digest_contract_id": self.mapping_registry_digest_contract_id,
            "m3_evidence_digest": self.m3_evidence_digest,
            "schema_registry_digest": self.schema_registry_digest,
            "source_catalog_digest": self.source_catalog_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "mapping_registry": self.semantic_projection_dict(),
            "mapping_registry_digest": self.mapping_registry_digest,
        }

    @property
    def supported_group_count(self) -> int:
        return sum(
            item.disposition is MappingDisposition.SUPPORTED for item in self.group_dispositions
        )


@dataclass(frozen=True, slots=True)
class ReplayFieldValueV1:
    column_index: int
    exact_header_name: str
    value: str


@dataclass(frozen=True, slots=True)
class ReplayEventLocatorV1:
    source_archive_id: str
    data_record_ordinal: int
    event_ordinal_within_source_record: int

    def to_dict(self) -> dict[str, object]:
        return {
            "data_record_ordinal": self.data_record_ordinal,
            "event_ordinal_within_source_record": self.event_ordinal_within_source_record,
            "locator_contract_id": REPLAY_EVENT_LOCATOR_CONTRACT_ID,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class ReplayEventV1:
    replay_event_contract_id: str
    source_archive_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    replay_field_mapping_id: str
    locator: ReplayEventLocatorV1
    source_turn_count_raw: str
    source_on_play_raw: str
    source_turn_side: ReplayTurnSideV1
    source_turn_slot_index: int
    source_turn_count: int
    source_on_play: bool
    source_fields: tuple[ReplayFieldValueV1, ...]
    quality_codes: tuple[ReplayQualityCode, ...]

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "locator": self.locator.to_dict(),
            "quality_contract_id": REPLAY_NORMALIZATION_QUALITY_CONTRACT_ID,
            "quality_codes": [item.value for item in self.quality_codes],
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "replay_event_contract_id": self.replay_event_contract_id,
            "replay_field_mapping_id": self.replay_field_mapping_id,
            "source_archive_id": self.source_archive_id,
            "source_fields": [
                {
                    "column_index": item.column_index,
                    "exact_header_name": item.exact_header_name,
                    "value": item.value,
                }
                for item in self.source_fields
            ],
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_on_play": self.source_on_play,
            "source_turn_count": self.source_turn_count,
            "source_turn_side": self.source_turn_side.value,
            "source_turn_slot_index": self.source_turn_slot_index,
            "source_turn_count_raw": self.source_turn_count_raw,
            "source_on_play_raw": self.source_on_play_raw,
        }

    @property
    def semantic_digest(self) -> str:
        return _digest(self.semantic_projection_dict())


@dataclass(frozen=True, slots=True)
class ReplayNormalizationRejectionV1:
    data_record_ordinal: int
    quality_code: ReplayQualityCode
    reason: str
    source_selectors: tuple[SourceColumnSelectorV1, ...]


@dataclass(frozen=True, slots=True)
class ReplayNormalizationResultV1:
    replay_normalization_contract_id: str
    source_archive_id: str
    data_record_ordinal: int
    events: tuple[ReplayEventV1, ...]
    row_quality_codes: tuple[ReplayQualityCode, ...]
    rejection: ReplayNormalizationRejectionV1 | None


def _find_turn_families(
    fields: tuple[FieldSurfaceV1, ...],
) -> tuple[ReplayIndexedFieldFamilyV1, ...]:
    by_side: dict[str, dict[int, list[tuple[str, FieldSurfaceV1]]]] = {
        "user": {},
        "oppo": {},
    }
    for field in fields:
        match = _TURN_FIELD_RE.fullmatch(field.exact_header_name)
        if match is None:
            continue
        side, index_text, suffix = match.groups()
        index = int(index_text)
        by_side[side].setdefault(index, []).append((suffix, field))
    result: list[ReplayIndexedFieldFamilyV1] = []
    for side in ("user", "oppo"):
        slots = by_side[side]
        if not slots:
            continue
        indexes = sorted(slots)
        suffix_sets = [tuple(sorted(suffix for suffix, _ in slots[index])) for index in indexes]
        result.append(
            ReplayIndexedFieldFamilyV1(
                source_side_prefix=f"{side}_turn",
                minimum_slot_index=indexes[0],
                maximum_slot_index=indexes[-1],
                slot_count=len(indexes),
                exact_suffixes=suffix_sets[0],
                contiguous_indices=indexes == list(range(indexes[0], indexes[-1] + 1)),
                identical_suffix_surface_per_slot=len(set(suffix_sets)) == 1,
            )
        )
    return tuple(result)


def build_replay_field_analysis(
    evidence: VerifiedM3EvidenceV1,
    schema_registry: VerifiedSchemaRegistryV1,
) -> ReplayFieldAnalysisV1:
    """Analyze all M3 Replay groups; candidate patterns remain evidence, not approval."""
    m3_groups = {
        group.raw_schema_fingerprint: group
        for group in evidence.groups
        if group.source_kind == "replay"
    }
    registry_groups = {
        group.raw_schema_fingerprint: group
        for group in schema_registry.groups
        if group.source_kind is SourceKind.REPLAY
    }
    if set(m3_groups) != set(registry_groups):
        raise ReplaySchemaError("M3.1 and M3.2 Replay group sets do not reconcile")
    analyses: list[ReplaySchemaGroupAnalysisV1] = []
    for fingerprint, group in sorted(m3_groups.items()):
        registry_group = registry_groups[fingerprint]
        if group.source_kind != registry_group.source_kind.value:
            raise ReplaySchemaError("M3.1/M3.2 Replay source kinds disagree")
        families = _find_turn_families(group.fields)
        candidate = (
            "one source row contains candidate per-side indexed turn-summary slots; "
            "this is a structural candidate only, not yet an event interpretation"
            if len(families) == 2
            else "no complete pair of user/opponent indexed turn-slot families was established"
        )
        scalar = tuple(
            field.exact_header_name
            for field in group.fields
            if _TURN_FIELD_RE.fullmatch(field.exact_header_name) is None
        )
        sampled_row_counts = {
            sum(count for _lexical_class, count in field.lexical_class_counts)
            for field in group.fields
        }
        field_evidence = tuple(
            ReplayFieldEvidenceSummaryV1(
                field.column_index,
                field.exact_header_name,
                sum(count for _name, count in field.lexical_class_counts),
                field.lexical_class_counts,
                sum(count for name, count in field.lexical_class_counts if name == "empty"),
                sum(count for name, count in field.lexical_class_counts if name != "empty"),
                "not_established_from_bounded_lexical_evidence",
            )
            for field in group.fields
        )
        analyses.append(
            ReplaySchemaGroupAnalysisV1(
                fingerprint,
                registry_group.source_interpretation_contract_id,
                group.archive_count,
                group.source_archive_ids,
                group.representative_source_archive_id,
                group.representative_filename,
                len(group.fields),
                group.field_names,
                field_evidence,
                group.expansion_format_counts,
                scalar,
                families,
                candidate,
                max(sampled_row_counts, default=0),
                sum(item.rows_shorter_than_header for item in group.row_width_anomalies),
                sum(item.rows_longer_than_header for item in group.row_width_anomalies),
                (
                    MappingDisposition.NEEDS_REVIEW
                    if len(families) == 2
                    else MappingDisposition.UNSUPPORTED_EVENT_STRUCTURE
                ),
                None,
                (
                    "M3.1 header/lexical evidence is only a structural candidate; review actual "
                    "M4.1 accepted rows for strict turns/on_play lexemes, per-side slot occupancy, "
                    "active-slot completeness, and full-source integrity before mapping this "
                    "fingerprint"
                ),
            )
        )
    return ReplayFieldAnalysisV1(
        REPLAY_FIELD_ANALYSIS_CONTRACT_ID,
        evidence.semantic_source_catalog_digest,
        evidence.deep_inspection_evidence_digest,
        schema_registry.schema_registry_digest,
        tuple(analyses),
    )


def build_turn_slot_mapping(
    group: RawSchemaGroupEvidenceV1,
    *,
    source_interpretation_contract_id: str,
    m3_evidence_digest: str,
    source_archive_id_reviewed: str,
) -> ReplayFieldMappingV1:
    """Build the strict declarative per-side turn-slot mapping candidate for one header."""
    if group.source_kind != SourceKind.REPLAY.value:
        raise ReplaySchemaError("turn-slot Replay mapping cannot consume a non-Replay group")
    if source_archive_id_reviewed not in group.source_archive_ids:
        raise ReplaySchemaError("mapping evidence source archive is not a member of its group")
    by_name: dict[str, FieldSurfaceV1] = {}
    for field in group.fields:
        if field.exact_header_name in by_name:
            raise ReplaySchemaError("duplicate source header names are unsupported by this mapping")
        by_name[field.exact_header_name] = field
    turn_selector = by_name.get("turns")
    on_play_selector = by_name.get("on_play")
    if turn_selector is None or on_play_selector is None:
        raise ReplaySchemaError("reviewed mapping requires exact 'turns' and 'on_play' fields")
    families = _find_turn_families(group.fields)
    if len(families) != 2 or any(
        family.minimum_slot_index != 1
        or family.maximum_slot_index != 30
        or family.slot_count != 30
        or not family.contiguous_indices
        or not family.identical_suffix_surface_per_slot
        for family in families
    ):
        raise ReplaySchemaError("per-side turn slot surfaces are not complete 1..30 families")
    fields_by_side_index: dict[tuple[str, int], list[FieldSurfaceV1]] = {}
    for field in group.fields:
        match = _TURN_FIELD_RE.fullmatch(field.exact_header_name)
        if match is not None:
            side, index_text, _suffix = match.groups()
            fields_by_side_index.setdefault((side, int(index_text)), []).append(field)
    suffix_sets = {
        family.source_side_prefix.split("_", maxsplit=1)[0]: set(family.exact_suffixes)
        for family in families
    }
    if suffix_sets["user"] != suffix_sets["oppo"]:
        raise ReplaySchemaError("user/opponent slot suffix surfaces differ")

    family_selectors: dict[tuple[str, int], tuple[SourceColumnSelectorV1, ...]] = {}
    mapped: list[ReplaySourceFieldMappingV1] = []
    lineage: list[ReplayFieldLineageV1] = []
    for side in ("user", "oppo"):
        for slot_index in range(1, 31):
            selected = sorted(
                fields_by_side_index[(side, slot_index)], key=lambda f: f.column_index
            )
            selectors = tuple(
                SourceColumnSelectorV1(field.column_index, field.exact_header_name)
                for field in selected
            )
            family_selectors[(side, slot_index)] = selectors
            for field, selector in zip(selected, selectors, strict=True):
                suffix = _TURN_FIELD_RE.fullmatch(field.exact_header_name)
                if suffix is None:
                    raise ReplaySchemaError("internal turn selector grammar mismatch")
                exact_suffix = suffix.group(3)
                path = f"source_fields[{selector.column_index}:{selector.exact_header_name}]"
                mapped.append(
                    ReplaySourceFieldMappingV1(
                        selector,
                        side,
                        slot_index,
                        exact_suffix,
                        path,
                        FieldDisposition.MAPPED,
                        FieldOrigin.SOURCE_DIRECT,
                        PRESERVE_SOURCE_STRING_TRANSFORM_ID,
                        "openmtgdata.preserve-exact-csv-field-string.v1",
                        "preserve_empty_string",
                        "preserve_exact_parser_string",
                    )
                )
                lineage.append(
                    ReplayFieldLineageV1(
                        path,
                        FieldOrigin.SOURCE_DIRECT,
                        (selector,),
                        side,
                        slot_index,
                        PRESERVE_SOURCE_STRING_TRANSFORM_ID,
                        "one_source_field_value",
                        "openmtgdata.preserve-exact-csv-field-string.v1",
                        "preserve_empty_string",
                        "preserve_exact_parser_string",
                        ("M4.1 exact accepted raw field", "M3.1 ordered physical header"),
                    )
                )

    turn_count_col = SourceColumnSelectorV1(
        turn_selector.column_index, turn_selector.exact_header_name
    )
    on_play_col = SourceColumnSelectorV1(
        on_play_selector.column_index, on_play_selector.exact_header_name
    )
    lineage.extend(
        (
            ReplayFieldLineageV1(
                "source_turn_count_raw",
                FieldOrigin.SOURCE_DIRECT,
                (turn_count_col,),
                None,
                None,
                PRESERVE_SOURCE_STRING_TRANSFORM_ID,
                "one_source_field_value",
                "openmtgdata.preserve-exact-csv-field-string.v1",
                "preserve_empty_string_in_raw_value; parsed companion rejects empty",
                "preserve_exact_parser_string",
                ("exact source header selector", "M4.1 decoded field string"),
            ),
            ReplayFieldLineageV1(
                "source_on_play_raw",
                FieldOrigin.SOURCE_DIRECT,
                (on_play_col,),
                None,
                None,
                PRESERVE_SOURCE_STRING_TRANSFORM_ID,
                "one_source_field_value",
                "openmtgdata.preserve-exact-csv-field-string.v1",
                "preserve_empty_string_in_raw_value; parsed companion rejects empty",
                "preserve_exact_parser_string",
                ("exact source header selector", "M4.1 decoded field string"),
            ),
            ReplayFieldLineageV1(
                "source_turn_count",
                FieldOrigin.DETERMINISTIC_NORMALIZATION,
                (turn_count_col,),
                None,
                None,
                STRICT_TURN_COUNT_PARSER_ID,
                "one_source_field_value",
                STRICT_TURN_COUNT_PARSER_ID,
                "reject_empty",
                "reject_record_on_parse_failure",
                ("exact source header selector", "strict source lexical parser"),
            ),
            ReplayFieldLineageV1(
                "source_on_play",
                FieldOrigin.DETERMINISTIC_NORMALIZATION,
                (on_play_col,),
                None,
                None,
                STRICT_ON_PLAY_PARSER_ID,
                "one_source_field_value",
                STRICT_ON_PLAY_PARSER_ID,
                "reject_empty",
                "reject_record_on_parse_failure",
                ("exact source header selector", "observed 0/1 source values"),
            ),
            ReplayFieldLineageV1(
                "source_turn_side",
                FieldOrigin.DETERMINISTIC_NORMALIZATION,
                (on_play_col,),
                None,
                None,
                REPLAY_TURN_SIDE_TRANSFORMATION_ID,
                "one_source_flag_plus_event_ordinal",
                "preserve_source_side_prefix.v1",
                "not_applicable_by_event_structure",
                "reject_record_on_ambiguous_slot",
                ("exact on_play selector", "versioned alternating source-turn-order rule"),
                ("event_ordinal_within_source_record",),
            ),
            ReplayFieldLineageV1(
                "source_turn_slot_index",
                FieldOrigin.DETERMINISTIC_NORMALIZATION,
                (),
                None,
                None,
                REPLAY_TURN_INDEX_TRANSFORMATION_ID,
                "one_event_ordinal",
                "integer_slot_index_from_ordinal.v1",
                "not_applicable_by_event_structure",
                "reject_record_on_ambiguous_slot",
                ("zero-based event ordinal rule",),
                ("event_ordinal_within_source_record",),
            ),
        )
    )

    slot_columns = {
        selector.column_index for selectors in family_selectors.values() for selector in selectors
    }
    mapped_context_columns = {turn_count_col.column_index, on_play_col.column_index}
    dispositions = tuple(
        ReplayFieldDispositionV1(
            SourceColumnSelectorV1(field.column_index, field.exact_header_name),
            (
                FieldDisposition.MAPPED
                if field.column_index in slot_columns | mapped_context_columns
                else FieldDisposition.UNMAPPED_PRESERVED_AT_RAW_LAYER
            ),
            (
                "selected turn-summary source field or event-boundary control"
                if field.column_index in slot_columns | mapped_context_columns
                else "retained in authoritative M4 raw layer; no M5.1 semantic mapping"
            ),
        )
        for field in group.fields
    )
    mapping = ReplayFieldMappingV1(
        REPLAY_FIELD_MAPPING_CONTRACT_ID,
        "",
        REPLAY_FIELD_MAPPING_ID_CONTRACT_ID,
        group.raw_schema_fingerprint,
        source_interpretation_contract_id,
        SourceKind.REPLAY,
        REPLAY_EVENT_BOUNDARY_RULE_ID,
        REPLAY_EVENT_ORDINAL_RULE_ID,
        turn_count_col,
        on_play_col,
        families,
        tuple(mapped),
        dispositions,
        tuple(lineage),
        STRICT_TURN_COUNT_PARSER_ID,
        STRICT_ON_PLAY_PARSER_ID,
        60,
        "reject_source_record_if_every_field_in_active_slot_is_empty",
        "do_not_emit slots beyond source turn count; cells remain preserved in M4 raw layer",
        "reject only the source record on invalid turn count, on_play, or wholly empty active slot",
        REPLAY_NORMALIZATION_QUALITY_CONTRACT_ID,
        "source_direct or deterministic_normalization only",
        "no reconstructed gameplay facts",
        (
            f"M3.1:{m3_evidence_digest}",
            f"review_source_archive:{source_archive_id_reviewed}",
            "M4.1 raw row and exact header authority",
        ),
    )
    return replace(
        mapping,
        replay_field_mapping_id=derive_replay_field_mapping_id(mapping.identity_projection_dict()),
    )


def build_replay_mapping_registry(
    analysis: ReplayFieldAnalysisV1,
    *,
    mapping_candidates: tuple[ReplayFieldMappingV1, ...],
    reviews: tuple[ReplayMappingReviewV1, ...],
) -> ReplayMappingRegistryV1:
    """Create an all-groups-accounted registry; support requires complete M4 evidence."""
    candidates = {item.raw_schema_fingerprint: item for item in mapping_candidates}
    review_by_fp = {item.raw_schema_fingerprint: item for item in reviews}
    if len(candidates) != len(mapping_candidates) or len(review_by_fp) != len(reviews):
        raise ReplaySchemaError("duplicate mapping candidate or review fingerprint")
    group_dispositions: list[ReplayMappingGroupDispositionV1] = []
    supported_contracts: list[ReplayFieldMappingV1] = []
    for group in analysis.groups:
        candidate = candidates.get(group.raw_schema_fingerprint)
        review = review_by_fp.get(group.raw_schema_fingerprint)
        if candidate is None:
            if group.candidate_event_structure.startswith("no complete"):
                disposition = MappingDisposition.UNSUPPORTED_EVENT_STRUCTURE
                reason = "M3.1 ordered header has no complete user/opponent indexed slot pair"
            else:
                disposition = MappingDisposition.NEEDS_REVIEW
                reason = (
                    "candidate per-side turn slots exist, but the exact M4.1 row values, "
                    "turn-count/on_play reconciliation, active-slot completeness, and full-source "
                    "integrity were not reviewed for this raw fingerprint"
                )
            group_dispositions.append(
                ReplayMappingGroupDispositionV1(
                    group.raw_schema_fingerprint,
                    group.source_interpretation_contract_id,
                    SourceKind.REPLAY,
                    group.archive_count,
                    disposition,
                    None,
                    reason,
                    review,
                )
            )
            continue
        if (
            candidate.source_interpretation_contract_id != group.source_interpretation_contract_id
            or candidate.source_kind is not SourceKind.REPLAY
        ):
            raise ReplaySchemaError("mapping candidate disagrees with its M3.2 group")
        if review is None:
            raise ReplaySchemaError("mapping candidate cannot be supported without row review")
        if review.source_archive_id not in group.member_source_archive_ids:
            raise ReplaySchemaError("M4 full-stream review archive is outside its M3 Replay group")
        if f"review_source_archive:{review.source_archive_id}" not in candidate.evidence_bindings:
            raise ReplaySchemaError("mapping contract does not bind the reviewed source archive")
        if review.evidence_scope != "m4-full-stream-verified":
            raise ReplaySchemaError("Replay mapping support requires full-stream M4 evidence")
        if (
            sum(count for _turns, count in review.turn_count_histogram)
            != review.m5_records_normalized
            or sum(turns * count for turns, count in review.turn_count_histogram)
            != review.event_count
            or sum(count for _side, count in review.event_side_counts) != review.event_count
        ):
            raise ReplaySchemaError("Replay mapping review turn/event counts do not reconcile")
        if (
            not _INT_SHA_RE.fullmatch(review.source_archive_id)
            or review.m4_reader_completion_status != "complete"
            or review.raw_schema_fingerprint != group.raw_schema_fingerprint
            or review.m4_records_seen
            != review.m4_records_accepted + review.m4_width_rejected_records
            or review.m5_records_submitted != review.m4_records_accepted
            or review.m5_records_normalized + review.m5_records_rejected
            != review.m5_records_submitted
            or review.partial_event_records > 0
            or review.turn_count_mismatch_records > 0
        ):
            raise ReplaySchemaError("M4/M5 review evidence does not satisfy mapping support gate")
        supported_contracts.append(candidate)
        group_dispositions.append(
            ReplayMappingGroupDispositionV1(
                group.raw_schema_fingerprint,
                group.source_interpretation_contract_id,
                SourceKind.REPLAY,
                group.archive_count,
                MappingDisposition.SUPPORTED,
                candidate.replay_field_mapping_id,
                "per-side indexed turn summary slots validated against source turn count/on_play "
                "through M4.1 full-stream completion for the reviewed representative",
                review,
            )
        )
    if set(candidates) != {
        item.raw_schema_fingerprint
        for item in group_dispositions
        if item.disposition is MappingDisposition.SUPPORTED
    }:
        raise ReplaySchemaError(
            "mapping candidates do not reconcile with supported group decisions"
        )
    projection = {
        "mapping_registry_contract_id": REPLAY_MAPPING_REGISTRY_CONTRACT_ID,
        "mapping_registry_digest_contract_id": REPLAY_MAPPING_REGISTRY_DIGEST_CONTRACT_ID,
        "mapping_policy_id": REPLAY_MAPPING_POLICY_ID,
        "source_catalog_digest": analysis.source_catalog_digest,
        "m3_evidence_digest": analysis.m3_evidence_digest,
        "schema_registry_digest": analysis.schema_registry_digest,
        "mapping_contracts": [
            item.to_dict()
            for item in sorted(supported_contracts, key=lambda x: x.raw_schema_fingerprint)
        ],
        "group_dispositions": [
            item.to_dict()
            for item in sorted(group_dispositions, key=lambda x: x.raw_schema_fingerprint)
        ],
    }
    return ReplayMappingRegistryV1(
        REPLAY_MAPPING_REGISTRY_CONTRACT_ID,
        REPLAY_MAPPING_REGISTRY_DIGEST_CONTRACT_ID,
        REPLAY_MAPPING_POLICY_ID,
        analysis.source_catalog_digest,
        analysis.m3_evidence_digest,
        analysis.schema_registry_digest,
        tuple(sorted(supported_contracts, key=lambda x: x.raw_schema_fingerprint)),
        tuple(sorted(group_dispositions, key=lambda x: x.raw_schema_fingerprint)),
        _digest(projection),
    )


def apply_mapping_dispositions_to_analysis(
    analysis: ReplayFieldAnalysisV1,
    registry: ReplayMappingRegistryV1,
) -> ReplayFieldAnalysisV1:
    """Return an M5.1 analysis view annotated with the final disposition for each group."""
    if (
        analysis.source_catalog_digest != registry.source_catalog_digest
        or analysis.m3_evidence_digest != registry.m3_evidence_digest
        or analysis.schema_registry_digest != registry.schema_registry_digest
    ):
        raise ReplaySchemaError("analysis and mapping registry evidence bindings disagree")
    by_fp = {item.raw_schema_fingerprint: item for item in registry.group_dispositions}
    if set(by_fp) != {item.raw_schema_fingerprint for item in analysis.groups}:
        raise ReplaySchemaError("mapping registry does not account for every analysis group")
    return replace(
        analysis,
        groups=tuple(
            replace(
                group,
                mapping_disposition=by_fp[group.raw_schema_fingerprint].disposition,
                mapping_id=by_fp[group.raw_schema_fingerprint].replay_field_mapping_id,
                disposition_reason=by_fp[group.raw_schema_fingerprint].reason,
            )
            for group in analysis.groups
        ),
    )
