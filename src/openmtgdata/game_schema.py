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
    "2572d8825d5eff17ad779695102f97d1ebb04b607545b02d1001d48461744add",
    "dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602",
    "openmtgdata.game-full-stream-review.v1:0443561f474493f7b623bce7c7bbc138ea8ccb0f5c45c67129da37c007ab7669",
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
    field_count: int
    reason: str
    row_width_policy: str | None
    known_anomalies: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "disposition": self.disposition.value,
            "field_count": self.field_count,
            "game_field_mapping_id": self.game_field_mapping_id,
            "known_anomalies": list(self.known_anomalies),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "reason": self.reason,
            "row_width_policy": self.row_width_policy,
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
            if (
                candidate_mapping.raw_schema_fingerprint != group.raw_schema_fingerprint
                or candidate_mapping.source_interpretation_contract_id
                != group.source_interpretation_contract_id
            ):
                raise GameSchemaError("Game group and mapping authority disagree")
        elif group.game_field_mapping_id is not None:
            raise GameSchemaError("unsupported Game group must not carry a mapping ID")
    if {item.raw_schema_fingerprint for item in registry.mappings} != {
        item.raw_schema_fingerprint
        for item in registry.groups
        if item.disposition is GameDisposition.SUPPORTED
    }:
        raise GameSchemaError("Game mappings and supported group dispositions differ")
