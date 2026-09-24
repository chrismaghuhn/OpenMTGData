"""Evidence-gated, source-preserving Game row contracts (M5.2)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from openmtgdata.source_filename import SourceKind

GAME_RECORD_SCHEMA_ID = "openmtgdata.game-record.v1"
GAME_MAPPING_SCHEMA_ID = "openmtgdata.game-field-mapping.v1"
GAME_LINEAGE_SCHEMA_ID = "openmtgdata.game-field-lineage.v1"
GAME_NORMALIZATION_SCHEMA_ID = "openmtgdata.game-normalization.v1"
GAME_QUALITY_SCHEMA_ID = "openmtgdata.game-normalization-quality.v1"
GAME_LOCATOR_SCHEMA_ID = "openmtgdata.game-record-locator.v1"
GAME_REGISTRY_SCHEMA_ID = "openmtgdata.game-mapping-registry.v1"
GAME_REGISTRY_DIGEST_SCHEMA_ID = "openmtgdata.game-mapping-registry-digest.v1"
GAME_EVIDENCE_BINDINGS = (
    "c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29",
    "dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602",
)
_SHA = re.compile(r"^[0-9a-f]{64}$")


class GameSchemaError(ValueError):
    """Invalid or unsupported M5.2 contract/input."""


class GameDisposition(StrEnum):
    SUPPORTED = "supported"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED_RECORD_STRUCTURE = "unsupported_record_structure"
    UNSUPPORTED_SEMANTIC_MAPPING = "unsupported_semantic_mapping"


class GameQualityCode(StrEnum):
    COMPLETE_SOURCE_MAPPING = "complete_source_mapping"
    PARTIAL_SOURCE_MAPPING = "partial_source_mapping"
    UNMAPPED_SOURCE_FIELDS_PRESENT = "unmapped_source_fields_present"


@dataclass(frozen=True, slots=True)
class GameSourceSelectorV1:
    column_index: int
    exact_header_name: str

    def to_dict(self) -> dict[str, object]:
        return {"column_index": self.column_index, "exact_header_name": self.exact_header_name}


@dataclass(frozen=True, slots=True)
class GameMappingReviewV1:
    review_contract_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    schema_registry_digest: str
    game_field_mapping_id: str
    m4_reader_contract_id: str
    m4_completion_status: str
    m4_records_seen: int
    m4_records_accepted: int
    m4_records_rejected: int
    m4_diagnostic_counts: tuple[tuple[str, int], ...]
    m5_records_submitted: int
    m5_records_normalized: int
    m5_records_rejected: int
    semantic_validation_digest: str
    evidence_scope: str

    def identity_projection_dict(self) -> dict[str, object]:
        return {
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "evidence_scope": self.evidence_scope,
            "game_field_mapping_id": self.game_field_mapping_id,
            "m4_completion_status": self.m4_completion_status,
            "m4_diagnostic_counts": dict(self.m4_diagnostic_counts),
            "m4_reader_contract_id": self.m4_reader_contract_id,
            "m4_records_accepted": self.m4_records_accepted,
            "m4_records_rejected": self.m4_records_rejected,
            "m4_records_seen": self.m4_records_seen,
            "m5_records_normalized": self.m5_records_normalized,
            "m5_records_rejected": self.m5_records_rejected,
            "m5_records_submitted": self.m5_records_submitted,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "review_contract_id": self.review_contract_id,
            "schema_registry_digest": self.schema_registry_digest,
            "semantic_validation_digest": self.semantic_validation_digest,
            "source_archive_id": self.source_archive_id,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
        }

    def to_dict(self) -> dict[str, object]:
        return self.identity_projection_dict()


@dataclass(frozen=True, slots=True)
class GameFieldLineageV1:
    normalized_field_path: str
    origin: str
    source_selectors: tuple[GameSourceSelectorV1, ...]
    derived_inputs: tuple[str, ...]
    transformation_id: str
    parser_id: str
    input_cardinality: str
    empty_value_policy: str
    failure_behavior: str
    evidence_basis: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "derived_inputs": list(self.derived_inputs),
            "empty_value_policy": self.empty_value_policy,
            "evidence_basis": list(self.evidence_basis),
            "failure_behavior": self.failure_behavior,
            "input_cardinality": self.input_cardinality,
            "lineage_schema_id": GAME_LINEAGE_SCHEMA_ID,
            "normalized_field_path": self.normalized_field_path,
            "origin": self.origin,
            "parser_id": self.parser_id,
            "source_selectors": [item.to_dict() for item in self.source_selectors],
            "transformation_id": self.transformation_id,
        }


@dataclass(frozen=True, slots=True)
class GameFieldMappingV1:
    game_field_mapping_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    source_kind: SourceKind
    row_granularity: str
    mappings: tuple[tuple[GameSourceSelectorV1, str], ...]
    field_dispositions: tuple[tuple[GameSourceSelectorV1, str], ...]
    lineages: tuple[GameFieldLineageV1, ...]
    row_width_policy: str
    empty_value_policy: str
    semantic_boundary: str
    evidence_bindings: tuple[str, ...]

    def identity_projection_dict(self) -> dict[str, object]:
        return {
            "empty_value_policy": self.empty_value_policy,
            "evidence_bindings": list(self.evidence_bindings),
            "field_dispositions": [
                {"disposition": disposition, "selector": selector.to_dict()}
                for selector, disposition in self.field_dispositions
            ],
            "game_field_mapping_schema_id": GAME_MAPPING_SCHEMA_ID,
            "game_lineage_schema_id": GAME_LINEAGE_SCHEMA_ID,
            "game_normalization_schema_id": GAME_NORMALIZATION_SCHEMA_ID,
            "lineages": [lineage.to_dict() for lineage in self.lineages],
            "mappings": [
                {"normalized_field_path": path, "selector": selector.to_dict()}
                for selector, path in self.mappings
            ],
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "row_granularity": self.row_granularity,
            "row_width_policy": self.row_width_policy,
            "semantic_boundary": self.semantic_boundary,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_kind": self.source_kind.value,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity_projection_dict(),
            "game_field_mapping_id": self.game_field_mapping_id,
        }


@dataclass(frozen=True, slots=True)
class GameMappingGroupV1:
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    disposition: GameDisposition
    game_field_mapping_id: str | None
    archive_count: int
    source_archive_ids: tuple[str, ...]
    field_count: int
    reason: str
    row_width_policy: str | None
    known_anomalies: tuple[str, ...]
    review: GameMappingReviewV1 | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "disposition": self.disposition.value,
            "field_count": self.field_count,
            "game_field_mapping_id": self.game_field_mapping_id,
            "known_anomalies": list(self.known_anomalies),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "reason": self.reason,
            "review": self.review.to_dict() if self.review is not None else None,
            "row_width_policy": self.row_width_policy,
            "source_archive_ids": list(self.source_archive_ids),
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
        }


@dataclass(frozen=True, slots=True)
class GameMappingRegistryV1:
    schema_registry_digest: str
    semantic_source_catalog_digest: str
    deep_inspection_evidence_digest: str
    game_mapping_policy_id: str
    mappings: tuple[GameFieldMappingV1, ...]
    groups: tuple[GameMappingGroupV1, ...]
    game_mapping_registry_digest: str

    def identity_projection_dict(self) -> dict[str, object]:
        return {
            "deep_inspection_evidence_digest": self.deep_inspection_evidence_digest,
            "game_field_mapping_schema_id": GAME_MAPPING_SCHEMA_ID,
            "game_mapping_policy_id": self.game_mapping_policy_id,
            "game_mapping_registry_schema_id": GAME_REGISTRY_SCHEMA_ID,
            "groups": [
                item.to_dict()
                for item in sorted(self.groups, key=lambda x: x.raw_schema_fingerprint)
            ],
            "mappings": [
                item.to_dict()
                for item in sorted(self.mappings, key=lambda x: x.game_field_mapping_id)
            ],
            "schema_registry_digest": self.schema_registry_digest,
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.identity_projection_dict(),
            "game_mapping_registry_digest_contract_id": GAME_REGISTRY_DIGEST_SCHEMA_ID,
            "game_mapping_registry_digest": self.game_mapping_registry_digest,
        }


@dataclass(frozen=True, slots=True)
class GameRecordLocatorV1:
    source_archive_id: str
    data_record_ordinal: int

    def to_dict(self) -> dict[str, object]:
        return {
            "data_record_ordinal": self.data_record_ordinal,
            "locator_schema_id": GAME_LOCATOR_SCHEMA_ID,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class GameSourceFactV1:
    normalized_field_path: str
    source_value: str
    lineage: GameFieldLineageV1

    def to_dict(self) -> dict[str, object]:
        return {
            "lineage": self.lineage.to_dict(),
            "normalized_field_path": self.normalized_field_path,
            "source_value": self.source_value,
        }


@dataclass(frozen=True, slots=True)
class GameRecordV1:
    game_record_contract_id: str
    source_archive_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    game_field_mapping_id: str
    locator: GameRecordLocatorV1
    source_facts: tuple[GameSourceFactV1, ...]
    quality_codes: tuple[GameQualityCode, ...]

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "game_field_mapping_id": self.game_field_mapping_id,
            "game_record_contract_id": self.game_record_contract_id,
            "locator": self.locator.to_dict(),
            "quality_codes": [item.value for item in self.quality_codes],
            "quality_schema_id": GAME_QUALITY_SCHEMA_ID,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "source_archive_id": self.source_archive_id,
            "source_facts": [item.to_dict() for item in self.source_facts],
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
        }

    def to_dict(self) -> dict[str, object]:
        return self.semantic_projection_dict()


def _canonical_digest(projection: object) -> str:
    payload = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()


def derive_game_mapping_id(mapping: GameFieldMappingV1) -> str:
    return f"{GAME_MAPPING_SCHEMA_ID}:{_canonical_digest(mapping.identity_projection_dict())}"


def derive_game_registry_digest(registry: GameMappingRegistryV1) -> str:
    return _canonical_digest(registry.identity_projection_dict())


def validate_game_mapping(mapping: GameFieldMappingV1) -> None:
    if mapping.source_kind is not SourceKind.GAME:
        raise GameSchemaError("Game mapping requires source_kind=game")
    if not _SHA.fullmatch(mapping.raw_schema_fingerprint):
        raise GameSchemaError("malformed Game raw fingerprint")
    if mapping.game_field_mapping_id != derive_game_mapping_id(mapping):
        raise GameSchemaError("Game mapping ID does not match canonical contract")
    if len({path for _, path in mapping.mappings}) != len(mapping.mappings):
        raise GameSchemaError("duplicate normalized Game field path")
    if {lineage.normalized_field_path for lineage in mapping.lineages} != {
        path for _, path in mapping.mappings
    }:
        raise GameSchemaError("Game mapping fields and lineage paths differ")


def validate_game_registry(registry: GameMappingRegistryV1) -> None:
    if not _SHA.fullmatch(registry.schema_registry_digest):
        raise GameSchemaError("malformed bound M3.2 schema registry digest")
    if not _SHA.fullmatch(registry.semantic_source_catalog_digest):
        raise GameSchemaError("malformed bound semantic source catalog digest")
    if not _SHA.fullmatch(registry.deep_inspection_evidence_digest):
        raise GameSchemaError("malformed bound M3.1 evidence digest")
    if registry.game_mapping_registry_digest != derive_game_registry_digest(registry):
        raise GameSchemaError("Game mapping registry digest mismatch")
    mapping_by_id: dict[str, GameFieldMappingV1] = {}
    for mapping in registry.mappings:
        validate_game_mapping(mapping)
        if mapping.game_field_mapping_id in mapping_by_id:
            raise GameSchemaError("duplicate Game mapping ID")
        mapping_by_id[mapping.game_field_mapping_id] = mapping
    fingerprints = [item.raw_schema_fingerprint for item in registry.groups]
    if len(set(fingerprints)) != len(fingerprints):
        raise GameSchemaError("duplicate Game schema registry group")
    for group in registry.groups:
        if not _SHA.fullmatch(group.raw_schema_fingerprint):
            raise GameSchemaError("malformed Game group fingerprint")
        if not group.source_interpretation_contract_id.startswith(
            "openmtgdata.source-interpretation.v1:"
        ):
            raise GameSchemaError("malformed group M3.2 interpretation ID")
        if group.disposition is GameDisposition.SUPPORTED:
            candidate_mapping = mapping_by_id.get(group.game_field_mapping_id or "")
            if candidate_mapping is None:
                raise GameSchemaError("supported Game group has no mapping")
            if group.review is None:
                raise GameSchemaError("supported Game group has no machine-readable M5.2 review")
            if (
                candidate_mapping.raw_schema_fingerprint != group.raw_schema_fingerprint
                or candidate_mapping.source_interpretation_contract_id
                != group.source_interpretation_contract_id
            ):
                raise GameSchemaError("Game group and mapping authority disagree")
            validate_game_mapping_review(
                group.review,
                group=group,
                mapping=candidate_mapping,
                schema_registry_digest=registry.schema_registry_digest,
            )
            if derive_game_m4_review_evidence_id(group.review) not in (
                candidate_mapping.evidence_bindings
            ):
                raise GameSchemaError("Game mapping ID does not bind its M4 review evidence")
        elif group.game_field_mapping_id is not None:
            raise GameSchemaError("unsupported Game group must not carry a mapping ID")
        elif group.review is not None:
            raise GameSchemaError("unsupported Game group cannot claim a successful review")
    if {item.raw_schema_fingerprint for item in registry.mappings} != {
        item.raw_schema_fingerprint
        for item in registry.groups
        if item.disposition is GameDisposition.SUPPORTED
    }:
        raise GameSchemaError("Game mappings and supported group dispositions differ")


def validate_game_mapping_review(
    review: GameMappingReviewV1,
    *,
    group: GameMappingGroupV1,
    mapping: GameFieldMappingV1,
    schema_registry_digest: str,
) -> None:
    """Fail closed unless a complete M4 and reconciled M5 review supports the group."""
    if review.review_contract_id != "openmtgdata.game-mapping-review.v1":
        raise GameSchemaError("unsupported Game mapping review contract")
    if (
        not _SHA.fullmatch(review.source_archive_id)
        or review.source_archive_id not in group.source_archive_ids
        or review.raw_schema_fingerprint != group.raw_schema_fingerprint
        or review.raw_schema_fingerprint != mapping.raw_schema_fingerprint
        or review.source_interpretation_contract_id != group.source_interpretation_contract_id
        or review.source_interpretation_contract_id != mapping.source_interpretation_contract_id
        or review.schema_registry_digest != schema_registry_digest
        or review.game_field_mapping_id != mapping.game_field_mapping_id
    ):
        raise GameSchemaError("Game mapping review identity does not match reviewed group")
    if not _SHA.fullmatch(review.compressed_sha256) or not _SHA.fullmatch(
        review.semantic_validation_digest
    ):
        raise GameSchemaError("Game mapping review contains malformed SHA-256 identity")
    if review.compressed_size_bytes <= 0 or review.m4_completion_status != "complete":
        raise GameSchemaError("Game mapping review lacks complete M4 source verification")
    if review.m4_reader_contract_id != "openmtgdata.raw-source-reader.v2":
        raise GameSchemaError("Game mapping review uses an unsupported M4 reader contract")
    counts = (
        review.m4_records_seen,
        review.m4_records_accepted,
        review.m4_records_rejected,
        review.m5_records_submitted,
        review.m5_records_normalized,
        review.m5_records_rejected,
    )
    if any(value < 0 for value in counts):
        raise GameSchemaError("Game mapping review counters cannot be negative")
    if review.m4_records_seen != review.m4_records_accepted + review.m4_records_rejected:
        raise GameSchemaError("M4 Game review counts do not reconcile")
    if sum(count for _, count in review.m4_diagnostic_counts) != review.m4_records_rejected:
        raise GameSchemaError("M4 Game diagnostics do not reconcile with rejected records")
    if review.m5_records_submitted != review.m4_records_accepted:
        raise GameSchemaError("M5 Game submissions do not reconcile with M4 accepted records")
    if review.m5_records_normalized + review.m5_records_rejected != review.m5_records_submitted:
        raise GameSchemaError("M5 Game normalization counts do not reconcile")
    if review.evidence_scope != "m4-full-stream-verified":
        raise GameSchemaError("Game review is not scoped to a complete verified M4 stream")


def derive_game_m4_review_evidence_id(review: GameMappingReviewV1) -> str:
    """Derive non-circular mapping evidence identity from M4 source and terminal proof."""
    projection = {
        "compressed_sha256": review.compressed_sha256,
        "compressed_size_bytes": review.compressed_size_bytes,
        "evidence_scope": review.evidence_scope,
        "m4_reader_contract_id": review.m4_reader_contract_id,
        "raw_schema_fingerprint": review.raw_schema_fingerprint,
        "review_contract_id": review.review_contract_id,
        "schema_registry_digest": review.schema_registry_digest,
        "source_archive_id": review.source_archive_id,
        "source_interpretation_contract_id": review.source_interpretation_contract_id,
    }
    return "openmtgdata.game-m4-review-evidence.v1:" + _canonical_digest(projection)
