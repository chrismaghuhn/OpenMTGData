"""M3.2 physical-schema compatibility policy and deterministic registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from openmtgdata.config import RuntimeConfig

M3_REPORT_CONTRACT_ID = "openmtgdata.deep-inspection-report.v1"
M3_REPORT_DIGEST_CONTRACT_ID = "openmtgdata.deep-inspection-report-digest.v1"
SCHEMA_EVOLUTION_POLICY_ID = "openmtgdata.schema-evolution-policy.v1"
SCHEMA_COMPATIBILITY_ASSESSMENT_ID = "openmtgdata.schema-compatibility-assessment.v1"
SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID = "openmtgdata.source-interpretation-contract.v1"
SOURCE_INTERPRETATION_ID_CONTRACT_ID = "openmtgdata.source-interpretation-id.v1"
REFERENCE_SCHEMA_SELECTION_RULE = (
    "minimum field count, then lexicographically minimum raw_schema_fingerprint, "
    "within each directly verified interpretation family"
)
SCHEMA_REGISTRY_CONTRACT_ID = "openmtgdata.schema-registry.v1"
SCHEMA_REGISTRY_DIGEST_CONTRACT_ID = "openmtgdata.schema-registry-digest.v1"
EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID = "openmtgdata.header-inventory.v1"
EXPECTED_M3_DEEP_METHOD_ID = "openmtgdata.deep-source-inspection.v2"
LEXICAL_EVIDENCE_CONTRACT_ID = "openmtgdata.lexical-field-evidence.v1"
CSV_PARSER_POLICY_ID = "openmtgdata.csv-header-policy.comma-utf8.v1"
FINDING_COUNT_SCOPE = (
    "Counts findings on directed same-source-kind pair assessments; both A-to-B and B-to-A "
    "are counted separately."
)


class SchemaEvolutionError(RuntimeError):
    """Base error for invalid evidence inputs or internally inconsistent registry data."""


class DeepReportValidationError(SchemaEvolutionError):
    """A persisted M3.1 report failed contract or digest validation."""


class CompatibilityFinding(StrEnum):
    EXACT = "exact"
    ADDITIVE = "additive"
    REORDERED = "reordered"
    RENAMED_OR_ALIAS_CANDIDATE = "renamed_or_alias_candidate"
    REMOVED = "removed"
    LEXICAL_TYPE_EVIDENCE_DRIFT = "lexical_type_evidence_drift"
    EMPTY_FIELD_EVIDENCE_DRIFT = "empty_field_evidence_drift"
    ROW_WIDTH_DRIFT = "row_width_drift"
    SEMANTIC_DRIFT_UNKNOWN = "semantic_drift_unknown"
    INCOMPATIBLE = "incompatible"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DUPLICATE_NAME_AMBIGUITY = "duplicate_name_ambiguity"


class RegistryDisposition(StrEnum):
    SUPPORTED_INTERPRETATION = "supported_interpretation"
    BLOCKED_INCOMPATIBLE = "blocked_incompatible"
    BLOCKED_INSUFFICIENT_EVIDENCE = "blocked_insufficient_evidence"


class ReviewStatus(StrEnum):
    REVIEWED = "reviewed"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class FieldSurfaceV1:
    column_index: int
    exact_header_name: str
    observed_lexical_classes: tuple[str, ...]
    empty_field_observed: bool
    lexical_class_counts: tuple[tuple[str, int], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "column_index": self.column_index,
            "empty_field_observed": self.empty_field_observed,
            "exact_header_name": self.exact_header_name,
            "lexical_class_counts": dict(self.lexical_class_counts),
            "observed_lexical_classes": list(self.observed_lexical_classes),
        }


@dataclass(frozen=True, slots=True)
class RowWidthAnomalyV1:
    source_archive_id: str
    original_filename: str
    expected_field_count: int
    rows_matching_header_width: int
    rows_shorter_than_header: int
    rows_longer_than_header: int
    minimum_observed_row_width: int | None
    maximum_observed_row_width: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_field_count": self.expected_field_count,
            "maximum_observed_row_width": self.maximum_observed_row_width,
            "minimum_observed_row_width": self.minimum_observed_row_width,
            "original_filename": self.original_filename,
            "rows_longer_than_header": self.rows_longer_than_header,
            "rows_matching_header_width": self.rows_matching_header_width,
            "rows_shorter_than_header": self.rows_shorter_than_header,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class RawSchemaGroupEvidenceV1:
    group_id: str
    source_kind: str
    raw_schema_fingerprint: str
    archive_count: int
    source_archive_ids: tuple[str, ...]
    deep_inspection_ids: tuple[str, ...]
    representative_source_archive_id: str
    representative_filename: str
    expansion_format_counts: tuple[tuple[str, str, int], ...]
    fields: tuple[FieldSurfaceV1, ...]
    lexical_profile_digest: str
    duplicate_header_names: tuple[str, ...]
    empty_header_positions: tuple[int, ...]
    row_width_anomalies: tuple[RowWidthAnomalyV1, ...]

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(field.exact_header_name for field in self.fields)

    @property
    def has_row_width_drift(self) -> bool:
        return bool(self.row_width_anomalies)

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "deep_inspection_ids": list(self.deep_inspection_ids),
            "duplicate_header_names": list(self.duplicate_header_names),
            "empty_header_positions": list(self.empty_header_positions),
            "expansion_format_counts": [
                {"expansion": expansion, "format": fmt, "archive_count": count}
                for expansion, fmt, count in self.expansion_format_counts
            ],
            "field_count": len(self.fields),
            "group_id": self.group_id,
            "lexical_profile_digest": self.lexical_profile_digest,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "representative_filename": self.representative_filename,
            "representative_source_archive_id": self.representative_source_archive_id,
            "row_width_anomalies": [item.to_dict() for item in self.row_width_anomalies],
            "source_archive_ids": list(self.source_archive_ids),
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class VerifiedM3EvidenceV1:
    semantic_source_catalog_digest: str
    deep_inspection_evidence_digest: str
    deep_inspection_method_id: str
    header_inventory_contract_id: str
    header_inventory_evidence_digest: str
    configured_source_count: int
    source_url_status_counts: tuple[tuple[str, int], ...]
    license_status_counts: tuple[tuple[str, int], ...]
    groups: tuple[RawSchemaGroupEvidenceV1, ...]


@dataclass(frozen=True, slots=True)
class CompatibilityAssessmentV1:
    contract_id: str
    assessment_id: str
    source_kind: str
    left_group_id: str
    left_raw_schema_fingerprint: str
    right_group_id: str
    right_raw_schema_fingerprint: str
    left_field_count: int
    right_field_count: int
    exact_ordered_header_equality: bool
    shared_exact_field_count: int
    left_only_exact_field_count: int
    right_only_exact_field_count: int
    left_only_field_name_samples: tuple[str, ...]
    right_only_field_name_samples: tuple[str, ...]
    shared_fields_preserve_relative_order: bool
    duplicate_name_ambiguity: bool
    empty_header_name_present: bool
    lexical_type_evidence_drift_field_count: int
    lexical_type_evidence_drift_name_samples: tuple[str, ...]
    empty_field_evidence_drift_field_count: int
    empty_field_evidence_drift_name_samples: tuple[str, ...]
    left_row_width_anomaly_archive_count: int
    right_row_width_anomaly_archive_count: int
    additive_direction: str | None
    additive_rule_passed: bool
    exact_contract_candidate: bool
    findings: tuple[CompatibilityFinding, ...]
    assessment_status: str
    review_status: ReviewStatus

    def to_dict(self) -> dict[str, object]:
        return {
            "additive_direction": self.additive_direction,
            "additive_rule_passed": self.additive_rule_passed,
            "assessment_id": self.assessment_id,
            "assessment_status": self.assessment_status,
            "compatibility_assessment_contract_id": self.contract_id,
            "duplicate_name_ambiguity": self.duplicate_name_ambiguity,
            "exact_contract_candidate": self.exact_contract_candidate,
            "empty_field_evidence_drift_field_count": self.empty_field_evidence_drift_field_count,
            "empty_field_evidence_drift_name_samples": list(
                self.empty_field_evidence_drift_name_samples
            ),
            "empty_header_name_present": self.empty_header_name_present,
            "exact_ordered_header_equality": self.exact_ordered_header_equality,
            "findings": [item.value for item in self.findings],
            "left_field_count": self.left_field_count,
            "left_group_id": self.left_group_id,
            "left_only_exact_field_count": self.left_only_exact_field_count,
            "left_only_field_name_samples": list(self.left_only_field_name_samples),
            "left_raw_schema_fingerprint": self.left_raw_schema_fingerprint,
            "left_row_width_anomaly_archive_count": self.left_row_width_anomaly_archive_count,
            "lexical_type_evidence_drift_field_count": (
                self.lexical_type_evidence_drift_field_count
            ),
            "lexical_type_evidence_drift_name_samples": list(
                self.lexical_type_evidence_drift_name_samples
            ),
            "review_status": self.review_status.value,
            "right_field_count": self.right_field_count,
            "right_group_id": self.right_group_id,
            "right_only_exact_field_count": self.right_only_exact_field_count,
            "right_only_field_name_samples": list(self.right_only_field_name_samples),
            "right_raw_schema_fingerprint": self.right_raw_schema_fingerprint,
            "right_row_width_anomaly_archive_count": self.right_row_width_anomaly_archive_count,
            "shared_exact_field_count": self.shared_exact_field_count,
            "shared_fields_preserve_relative_order": self.shared_fields_preserve_relative_order,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class SourceInterpretationContractV1:
    contract_schema_id: str
    source_interpretation_contract_id: str
    contract_id_derivation_id: str
    deep_inspection_evidence_digest: str
    source_kind: str
    reference_raw_schema_fingerprint: str
    member_raw_schema_fingerprints: tuple[str, ...]
    member_schema_group_ids: tuple[str, ...]
    required_fields: tuple[tuple[int, str], ...]
    reference_schema_selection_rule: str
    required_field_order_rule: str
    csv_parser_policy_id: str
    encoding_policy: str
    unknown_additional_field_policy: str
    duplicate_field_policy: str
    empty_header_name_policy: str
    row_width_policy: str
    lexical_evidence_contract_id: str
    lexical_evidence_scope: str
    member_lexical_profile_digests: tuple[str, ...]
    lexical_profile_merge_rule: str
    null_semantics_status: str
    semantic_mapping_status: str
    semantic_drift_status: str
    known_anomaly_codes: tuple[str, ...]
    member_evidence_digests: tuple[str, ...]
    schema_evolution_policy_id: str
    review_status: ReviewStatus
    review_basis: tuple[str, ...]

    def projection_dict(self) -> dict[str, object]:
        return {
            "contract_schema_id": self.contract_schema_id,
            "contract_id_derivation_id": self.contract_id_derivation_id,
            "csv_parser_policy_id": self.csv_parser_policy_id,
            "deep_inspection_evidence_digest": self.deep_inspection_evidence_digest,
            "duplicate_field_policy": self.duplicate_field_policy,
            "encoding_policy": self.encoding_policy,
            "empty_header_name_policy": self.empty_header_name_policy,
            "known_anomaly_codes": list(self.known_anomaly_codes),
            "lexical_evidence_contract_id": self.lexical_evidence_contract_id,
            "lexical_evidence_scope": self.lexical_evidence_scope,
            "lexical_profile_merge_rule": self.lexical_profile_merge_rule,
            "member_lexical_profile_digests": list(self.member_lexical_profile_digests),
            "member_evidence_digests": list(self.member_evidence_digests),
            "member_raw_schema_fingerprints": list(self.member_raw_schema_fingerprints),
            "member_schema_group_ids": list(self.member_schema_group_ids),
            "null_semantics_status": self.null_semantics_status,
            "reference_raw_schema_fingerprint": self.reference_raw_schema_fingerprint,
            "reference_schema_selection_rule": self.reference_schema_selection_rule,
            "required_field_order_rule": self.required_field_order_rule,
            "required_fields": [
                {"column_index": index, "exact_header_name": name}
                for index, name in self.required_fields
            ],
            "review_basis": list(self.review_basis),
            "review_status": self.review_status.value,
            "row_width_policy": self.row_width_policy,
            "semantic_drift_status": self.semantic_drift_status,
            "semantic_mapping_status": self.semantic_mapping_status,
            "source_kind": self.source_kind,
            "schema_evolution_policy_id": self.schema_evolution_policy_id,
            "unknown_additional_field_policy": self.unknown_additional_field_policy,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.projection_dict(),
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
        }


@dataclass(frozen=True, slots=True)
class SchemaRegistryGroupV1:
    group_id: str
    source_kind: str
    raw_schema_fingerprint: str
    archive_count: int
    disposition: RegistryDisposition
    source_interpretation_contract_id: str | None
    reference_raw_schema_fingerprint: str | None
    required_field_count: int
    expansion_format_counts: tuple[tuple[str, str, int], ...]
    compatibility_findings: tuple[CompatibilityFinding, ...]
    compatibility_assessment_count: int
    known_row_width_drift: bool
    row_width_anomaly_archive_count: int
    row_width_anomalies: tuple[RowWidthAnomalyV1, ...]
    known_anomaly_codes: tuple[str, ...]
    review_status: ReviewStatus
    review_reason: str
    evidence_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "compatibility_assessment_count": self.compatibility_assessment_count,
            "compatibility_findings": [item.value for item in self.compatibility_findings],
            "disposition": self.disposition.value,
            "evidence_digest": self.evidence_digest,
            "expansion_format_counts": [
                {"expansion": expansion, "format": fmt, "archive_count": count}
                for expansion, fmt, count in self.expansion_format_counts
            ],
            "group_id": self.group_id,
            "known_anomaly_codes": list(self.known_anomaly_codes),
            "known_row_width_drift": self.known_row_width_drift,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "reference_raw_schema_fingerprint": self.reference_raw_schema_fingerprint,
            "required_field_count": self.required_field_count,
            "review_reason": self.review_reason,
            "review_status": self.review_status.value,
            "row_width_anomaly_archive_count": self.row_width_anomaly_archive_count,
            "row_width_anomalies": [item.to_dict() for item in self.row_width_anomalies],
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True, slots=True)
class SchemaEvolutionPolicyV1:
    policy_id: str = SCHEMA_EVOLUTION_POLICY_ID

    def to_dict(self) -> dict[str, object]:
        return {
            "additive_compatibility_conditions": [
                "same_source_kind",
                "base_required_fields_present_exactly_once_with_exact_spelling",
                "base_required_fields_preserve_relative_order; inserted additions are permitted",
                "no_duplicate_name_ambiguity_in_required_field_surface",
                "no_empty_name_in_required_field_surface_for_additive_mapping",
                "observed_lexical_class_support_matches_for_shared_required_fields",
                "empty_field_observation_matches_for_shared_required_fields",
                "no_known_row_width_anomaly_in_additive_members",
                "all_additional_fields_are_preserved_uninterpreted",
                "candidate_fingerprint_is_explicitly_registered_and_reviewed",
            ],
            "additive_directional": True,
            "additional_field_policy": (
                "preserved_uninterpreted; fields from unregistered raw fingerprints fail closed"
            ),
            "default_reorder_policy": "incompatible_until_explicit_policy_review",
            "duplicate_name_policy": (
                "preserve by column index; name-only mapping is prohibited when duplicates exist"
            ),
            "empty_header_name_policy": (
                "preserve the exact empty name and index; never synthesize a name; "
                "no semantic mapping"
            ),
            "lexical_evidence_policy": (
                "bounded-prefix lexical classes are descriptive only; drift is not a semantic "
                "type claim"
            ),
            "null_semantics_policy": "empty-field evidence is not proof of source nullability",
            "policy_id": self.policy_id,
            "reference_schema_selection_rule": REFERENCE_SCHEMA_SELECTION_RULE,
            "removal_policy": (
                "incompatible; do not synthesize defaults or equate missing with empty"
            ),
            "rename_policy": "candidate-only; no automatic aliases or field mappings",
            "row_width_policy": (
                "exact member-header width required; short/long rows are unsupported; no padding, "
                "truncation, or synthesized field names"
            ),
            "semantic_equivalence_policy": (
                "not established from physical and bounded lexical evidence alone"
            ),
            "temporal_coverage_policy": "bounded first 256 data records; not full-history proof",
            "transitive_compatibility_policy": (
                "never infer a family from connected components; each member is checked directly "
                "against the deterministic reference field surface"
            ),
        }


@dataclass(frozen=True, slots=True)
class SchemaRegistryV1:
    registry_contract_id: str
    registry_digest_contract_id: str
    policy: SchemaEvolutionPolicyV1
    semantic_source_catalog_digest: str
    deep_inspection_evidence_digest: str
    deep_inspection_report_contract_id: str
    deep_inspection_method_id: str
    header_inventory_contract_id: str
    header_inventory_evidence_digest: str
    interpretation_contracts: tuple[SourceInterpretationContractV1, ...]
    groups: tuple[SchemaRegistryGroupV1, ...]
    compatibility_assessments: tuple[CompatibilityAssessmentV1, ...]
    finding_counts: tuple[tuple[str, int], ...]
    source_url_status_counts: tuple[tuple[str, int], ...]
    license_status_counts: tuple[tuple[str, int], ...]
    schema_group_count: int
    registered_archive_count: int
    max_interpreted_data_rows: int
    chronological_coverage_status: str
    supported_group_count: int
    blocked_group_count: int
    needs_review_group_count: int
    interpretation_contract_count: int
    game_contract_count: int
    replay_contract_count: int
    needs_review_assessment_count: int
    known_row_width_drift_group_count: int
    policy_audit_note: str

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "compatibility_assessment_contract_id": SCHEMA_COMPATIBILITY_ASSESSMENT_ID,
            "compatibility_assessments": [
                item.to_dict() for item in self.compatibility_assessments
            ],
            "deep_inspection_evidence_digest": self.deep_inspection_evidence_digest,
            "deep_inspection_method_id": self.deep_inspection_method_id,
            "deep_inspection_report_contract_id": self.deep_inspection_report_contract_id,
            "finding_counts": dict(self.finding_counts),
            "finding_count_scope": FINDING_COUNT_SCOPE,
            "groups": [item.to_dict() for item in self.groups],
            "header_inventory_contract_id": self.header_inventory_contract_id,
            "header_inventory_evidence_digest": self.header_inventory_evidence_digest,
            "interpretation_contracts": [item.to_dict() for item in self.interpretation_contracts],
            "license_status_counts": dict(self.license_status_counts),
            "known_row_width_drift_group_count": self.known_row_width_drift_group_count,
            "needs_review_assessment_count": self.needs_review_assessment_count,
            "policy": self.policy.to_dict(),
            "registry_contract_id": self.registry_contract_id,
            "registry_digest_contract_id": self.registry_digest_contract_id,
            "registered_archive_count": self.registered_archive_count,
            "schema_group_count": self.schema_group_count,
            "supported_group_count": self.supported_group_count,
            "blocked_group_count": self.blocked_group_count,
            "needs_review_group_count": self.needs_review_group_count,
            "interpretation_contract_count": self.interpretation_contract_count,
            "game_contract_count": self.game_contract_count,
            "replay_contract_count": self.replay_contract_count,
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "source_interpretation_contract_schema_id": (SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID),
            "source_interpretation_id_contract_id": SOURCE_INTERPRETATION_ID_CONTRACT_ID,
            "source_url_status_counts": dict(self.source_url_status_counts),
            "max_interpreted_data_rows": self.max_interpreted_data_rows,
            "chronological_coverage_status": self.chronological_coverage_status,
            "scope_statement": (
                "Configured registered sources bound to the declared semantic source catalog "
                "and M3.1 evidence digest; no global 17Lands coverage claim."
            ),
        }

    @property
    def semantic_projection_bytes(self) -> bytes:
        return _canonical_bytes(self.semantic_projection_dict())

    @property
    def schema_registry_digest(self) -> str:
        return hashlib.sha256(self.semantic_projection_bytes).hexdigest()

    def summary_dict(self) -> dict[str, object]:
        return {
            "blocked_groups": self.blocked_group_count,
            "deep_inspection_evidence_digest": self.deep_inspection_evidence_digest,
            "finding_counts": dict(self.finding_counts),
            "finding_count_scope": FINDING_COUNT_SCOPE,
            "compatibility_assessment_count": len(self.compatibility_assessments),
            "additive_rule_passed_assessment_count": sum(
                item.additive_rule_passed for item in self.compatibility_assessments
            ),
            "rename_or_alias_candidate_assessment_count": dict(self.finding_counts).get(
                CompatibilityFinding.RENAMED_OR_ALIAS_CANDIDATE.value,
                0,
            ),
            "game_contract_count": self.game_contract_count,
            "interpretation_contract_count": self.interpretation_contract_count,
            "license_status_counts": dict(self.license_status_counts),
            "needs_review_groups": self.needs_review_group_count,
            "needs_review_assessments": self.needs_review_assessment_count,
            "known_row_width_drift_groups": self.known_row_width_drift_group_count,
            "raw_schema_groups": self.schema_group_count,
            "registered_archive_count": self.registered_archive_count,
            "registry_digest_contract_id": self.registry_digest_contract_id,
            "schema_registry_digest": self.schema_registry_digest,
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "source_url_status_counts": dict(self.source_url_status_counts),
            "supported_groups": self.supported_group_count,
            "replay_contract_count": self.replay_contract_count,
            "max_interpreted_data_rows": self.max_interpreted_data_rows,
            "chronological_coverage_status": self.chronological_coverage_status,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "registry_digest": self.schema_registry_digest,
            "registry_digest_contract_id": self.registry_digest_contract_id,
            "registry": self.semantic_projection_dict(),
            "summary": self.summary_dict(),
            "policy_audit_note": self.policy_audit_note,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class VerifiedDeepEvidenceV1:
    semantic_source_catalog_digest: str
    deep_inspection_evidence_digest: str
    deep_inspection_report_contract_id: str
    deep_inspection_method_id: str
    header_inventory_contract_id: str
    header_inventory_evidence_digest: str
    configured_source_count: int
    max_interpreted_data_rows: int
    chronological_coverage_status: str
    source_url_status_counts: tuple[tuple[str, int], ...]
    license_status_counts: tuple[tuple[str, int], ...]
    groups: tuple[RawSchemaGroupEvidenceV1, ...]


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_LEXICAL_CLASSES = (
    "empty",
    "integer_lexeme",
    "decimal_lexeme",
    "boolean_lexeme",
    "other_text",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _count_map(value: object, *, context: str) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict):
        raise DeepReportValidationError(f"{context} must be an object of integer counts")
    result: list[tuple[str, int]] = []
    for key, count in value.items():
        if not isinstance(key, str) or not isinstance(count, int) or count < 0:
            raise DeepReportValidationError(f"{context} has an invalid count entry")
        result.append((key, count))
    return tuple(sorted(result))


def _group_evidence_from_report(
    group_summary: dict[str, Any],
    member_inspections: list[dict[str, Any]],
) -> RawSchemaGroupEvidenceV1:
    try:
        group_id = group_summary["group_id"]
        raw_fingerprint = group_summary["raw_schema_fingerprint"]
        source_kind = group_summary["source_kind"]
        expected_archive_count = group_summary["archive_count"]
        designated = group_summary["designated_representative"]
        representative_id = designated["source_archive_id"]
        representative_filename = designated["original_filename"]
        expansion_format_counts = tuple(
            sorted(
                (
                    item["expansion"],
                    item["format"],
                    item["archive_count"],
                )
                for item in group_summary["expansion_format_counts"]
            )
        )
    except (KeyError, TypeError) as exc:
        raise DeepReportValidationError("M3.1 representative group has incomplete fields") from exc
    if (
        not isinstance(group_id, str)
        or not group_id
        or not isinstance(raw_fingerprint, str)
        or not _SHA256_RE.fullmatch(raw_fingerprint)
        or source_kind not in {"game", "replay", "draft"}
        or not isinstance(expected_archive_count, int)
        or expected_archive_count <= 0
        or not isinstance(representative_id, str)
        or not isinstance(representative_filename, str)
        or len(member_inspections) != expected_archive_count
    ):
        raise DeepReportValidationError("M3.1 group identity/count fields are invalid")
    if not any(
        item.get("source_archive_id") == representative_id
        and item.get("original_filename") == representative_filename
        for item in member_inspections
    ):
        raise DeepReportValidationError("designated M3.1 representative is not a group member")

    reference_names: tuple[str, ...] | None = None
    class_counts_by_index: dict[int, Counter[str]] = defaultdict(Counter)
    rows_observed_by_index: Counter[int] = Counter()
    empty_observed_by_index: Counter[int] = Counter()
    source_ids: list[str] = []
    inspection_ids: list[str] = []
    width_anomalies: list[RowWidthAnomalyV1] = []

    for inspection in member_inspections:
        try:
            columns = inspection["columns"]
            source_id = inspection["source_archive_id"]
            inspection_id = inspection["deep_inspection_id"]
            filename = inspection["original_filename"]
            expected_width = inspection["expected_field_count"]
            matching = inspection["rows_matching_header_width"]
            shorter = inspection["rows_shorter_than_header"]
            longer = inspection["rows_longer_than_header"]
            minimum_width = inspection["minimum_observed_row_width"]
            maximum_width = inspection["maximum_observed_row_width"]
        except (KeyError, TypeError) as exc:
            raise DeepReportValidationError(
                "M3.1 archive inspection is missing row evidence"
            ) from exc
        if (
            not isinstance(columns, list)
            or not isinstance(source_id, str)
            or not isinstance(inspection_id, str)
            or not isinstance(filename, str)
            or expected_width != len(columns)
        ):
            raise DeepReportValidationError("M3.1 archive column evidence is malformed")
        if inspection.get("status") != "success":
            raise DeepReportValidationError(
                "M3.2 requires successful M3.1 bounded row evidence for every input archive"
            )
        current_names: list[str] = []
        current_indexes: list[int] = []
        for column in columns:
            try:
                index = column["column_index"]
                name = column["exact_header_name"]
                lexical_counts = column["lexical_class_counts"]
                empty_count = column["empty_field_count"]
                rows_observed = column["rows_observed"]
            except (KeyError, TypeError) as exc:
                raise DeepReportValidationError("M3.1 column evidence is malformed") from exc
            if (
                not isinstance(index, int)
                or not isinstance(name, str)
                or not isinstance(lexical_counts, dict)
                or not isinstance(empty_count, int)
                or not isinstance(rows_observed, int)
                or empty_count < 0
                or rows_observed < 0
            ):
                raise DeepReportValidationError("M3.1 column fields have invalid types")
            if index != len(current_indexes):
                raise DeepReportValidationError("M3.1 column indices are not contiguous")
            current_indexes.append(index)
            current_names.append(name)
            if set(lexical_counts) != set(_LEXICAL_CLASSES) or not all(
                isinstance(value, int) and value >= 0 for value in lexical_counts.values()
            ):
                raise DeepReportValidationError("M3.1 lexical class counts are invalid")
            if (
                sum(lexical_counts.values()) != rows_observed
                or lexical_counts["empty"] != empty_count
                or empty_count > rows_observed
            ):
                raise DeepReportValidationError(
                    "M3.1 column row/empty/lexical counts do not reconcile"
                )
            for lexical_class, count in lexical_counts.items():
                class_counts_by_index[index][lexical_class] += count
            rows_observed_by_index[index] += rows_observed
            empty_observed_by_index[index] += empty_count > 0

        names_tuple = tuple(current_names)
        if reference_names is None:
            reference_names = names_tuple
        elif names_tuple != reference_names:
            raise DeepReportValidationError(
                "same M3.1 raw-schema group contains inconsistent ordered header fields"
            )
        source_ids.append(source_id)
        inspection_ids.append(inspection_id)
        if shorter > 0 or longer > 0:
            width_anomalies.append(
                RowWidthAnomalyV1(
                    source_archive_id=source_id,
                    original_filename=filename,
                    expected_field_count=expected_width,
                    rows_matching_header_width=matching,
                    rows_shorter_than_header=shorter,
                    rows_longer_than_header=longer,
                    minimum_observed_row_width=minimum_width,
                    maximum_observed_row_width=maximum_width,
                )
            )

    assert reference_names is not None
    field_surfaces = tuple(
        FieldSurfaceV1(
            column_index=index,
            exact_header_name=name,
            observed_lexical_classes=tuple(
                lexical_class
                for lexical_class in _LEXICAL_CLASSES
                if lexical_class != "empty" and class_counts_by_index[index][lexical_class] > 0
            ),
            empty_field_observed=empty_observed_by_index[index] > 0,
            lexical_class_counts=tuple(
                (lexical_class, class_counts_by_index[index][lexical_class])
                for lexical_class in _LEXICAL_CLASSES
            ),
        )
        for index, name in enumerate(reference_names)
    )
    duplicate_names = tuple(
        sorted(name for name, count in Counter(reference_names).items() if count > 1)
    )
    empty_positions = tuple(index for index, name in enumerate(reference_names) if name == "")
    lexical_digest = _digest_json(
        [
            {
                "column_index": field.column_index,
                "empty_field_observed": field.empty_field_observed,
                "exact_header_name": field.exact_header_name,
                "observed_lexical_classes": list(field.observed_lexical_classes),
            }
            for field in field_surfaces
        ]
    )
    return RawSchemaGroupEvidenceV1(
        group_id=group_id,
        source_kind=source_kind,
        raw_schema_fingerprint=raw_fingerprint,
        archive_count=expected_archive_count,
        source_archive_ids=tuple(sorted(source_ids)),
        deep_inspection_ids=tuple(sorted(inspection_ids)),
        representative_source_archive_id=representative_id,
        representative_filename=representative_filename,
        expansion_format_counts=expansion_format_counts,
        fields=field_surfaces,
        lexical_profile_digest=lexical_digest,
        duplicate_header_names=duplicate_names,
        empty_header_positions=empty_positions,
        row_width_anomalies=tuple(
            sorted(
                width_anomalies, key=lambda item: (item.source_archive_id, item.original_filename)
            )
        ),
    )


def load_verified_m3_evidence(
    report_path: str | Path,
    *,
    expected_evidence_digest: str | None = None,
    expected_source_catalog_digest: str | None = None,
) -> VerifiedDeepEvidenceV1:
    """Load and verify a persisted M3.1 report without touching raw source archives."""
    try:
        with Path(report_path).open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeepReportValidationError("could not load persisted M3.1 report JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("evidence"), dict):
        raise DeepReportValidationError("M3.1 report has no evidence projection")
    evidence = document["evidence"]
    claimed_digest = document.get("evidence_digest")
    if not isinstance(claimed_digest, str) or not _SHA256_RE.fullmatch(claimed_digest):
        raise DeepReportValidationError("M3.1 evidence digest is missing or malformed")
    actual_digest = _digest_json(evidence)
    if actual_digest != claimed_digest:
        raise DeepReportValidationError(
            "M3.1 evidence digest does not match its canonical projection"
        )
    if expected_evidence_digest is not None and claimed_digest != expected_evidence_digest:
        raise DeepReportValidationError("M3.1 evidence digest differs from the required input")
    try:
        report_contract = evidence["deep_inspection_report_contract_id"]
        digest_contract = evidence["deep_report_digest_contract_id"]
        source_digest = evidence["semantic_source_catalog_digest"]
        method_id = evidence["deep_inspection_method_id"]
        header_contract = evidence["header_inventory_contract_id"]
        header_digest = evidence["header_inventory_evidence_digest"]
        declared_group_count = evidence["raw_schema_group_count"]
        registered_count = evidence["registered_archive_count"]
        attempted_count = evidence["attempted_deep_inspections"]
        successful_count = evidence["successful_deep_inspections"]
        partial_count = evidence["partial_deep_inspections"]
        unsupported_count = evidence["unsupported_deep_inspections"]
        header_inspected_count = evidence["header_inspected_archive_count"]
        execution_blocker_count = evidence["execution_blocker_count"]
        designated_representative_count = evidence["designated_representative_count"]
        group_summaries = evidence["representatives"]
        archive_inspections = evidence["inspections"]
        source_url_counts = evidence["source_url_status_counts"]
        license_counts = evidence["license_status_counts"]
        covered_group_count = evidence["covered_raw_schema_group_count"]
        blocked_group_count = evidence["blocked_raw_schema_group_count"]
        max_data_rows = evidence["max_interpreted_data_rows"]
        # M3.1's released evidence projection may omit chronology because no source dates are known.
        chronology_status = evidence.get(
            "chronological_coverage_status", "unavailable_not_established"
        )
    except (KeyError, TypeError) as exc:
        raise DeepReportValidationError("M3.1 evidence projection lacks required fields") from exc
    if report_contract != M3_REPORT_CONTRACT_ID or digest_contract != M3_REPORT_DIGEST_CONTRACT_ID:
        raise DeepReportValidationError("unsupported M3.1 report/digest contract")
    if method_id != EXPECTED_M3_DEEP_METHOD_ID:
        raise DeepReportValidationError("unsupported M3.1 deep-inspection method")
    if header_contract != EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID:
        raise DeepReportValidationError("unsupported M2.5 header-inventory contract")
    if (
        not isinstance(source_digest, str)
        or not _SHA256_RE.fullmatch(source_digest)
        or not isinstance(header_digest, str)
        or not _SHA256_RE.fullmatch(header_digest)
    ):
        raise DeepReportValidationError("M3.1 source/header evidence digest is malformed")
    if (
        expected_source_catalog_digest is not None
        and source_digest != expected_source_catalog_digest
    ):
        raise DeepReportValidationError(
            "M3.1 report belongs to a different semantic source catalog"
        )
    if evidence.get("source_interpretation_contract_id") is not None:
        raise DeepReportValidationError("M3.1 must not set a source interpretation contract")
    if not isinstance(group_summaries, list) or not isinstance(archive_inspections, list):
        raise DeepReportValidationError("M3.1 group/archive records must be arrays")
    if (
        not all(
            isinstance(count, int) and count >= 0
            for count in (
                declared_group_count,
                registered_count,
                attempted_count,
                successful_count,
                partial_count,
                unsupported_count,
                header_inspected_count,
                execution_blocker_count,
                designated_representative_count,
                covered_group_count,
                blocked_group_count,
                max_data_rows,
            )
        )
        or attempted_count != registered_count
        or successful_count + partial_count + unsupported_count != attempted_count
        or partial_count != 0
        or unsupported_count != 0
        or successful_count != registered_count
        or header_inspected_count != registered_count
        or execution_blocker_count != 0
        or covered_group_count != declared_group_count
        or blocked_group_count != 0
        or designated_representative_count != declared_group_count
        or not isinstance(chronology_status, str)
        or len(archive_inspections) != registered_count
        or len(group_summaries) != declared_group_count
    ):
        raise DeepReportValidationError("M3.1 source/group outcome counts do not reconcile")

    members_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for inspection in archive_inspections:
        if not isinstance(inspection, dict) or inspection.get("status") != "success":
            raise DeepReportValidationError("M3.1 contains a non-successful archive inspection")
        source_kind = inspection.get("source_kind")
        fingerprint = inspection.get("raw_schema_fingerprint")
        if source_kind not in {"game", "replay"}:
            raise DeepReportValidationError("M3.1 archive source kind is unsupported or unknown")
        if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
            raise DeepReportValidationError("M3.1 archive raw schema fingerprint is malformed")
        if (
            not isinstance(inspection.get("source_archive_id"), str)
            or not _SHA256_RE.fullmatch(inspection["source_archive_id"])
            or not isinstance(inspection.get("deep_inspection_id"), str)
            or not _SHA256_RE.fullmatch(inspection["deep_inspection_id"])
        ):
            raise DeepReportValidationError("M3.1 source/deep inspection identity is malformed")
        members_by_key[(source_kind, fingerprint)].append(inspection)

    groups: list[RawSchemaGroupEvidenceV1] = []
    seen_keys: set[tuple[str, str]] = set()
    for summary in group_summaries:
        if not isinstance(summary, dict):
            raise DeepReportValidationError("M3.1 representative group is not an object")
        source_kind = summary.get("source_kind")
        raw_fingerprint = summary.get("raw_schema_fingerprint")
        if (
            source_kind not in {"game", "replay"}
            or not isinstance(raw_fingerprint, str)
            or not _SHA256_RE.fullmatch(raw_fingerprint)
        ):
            raise DeepReportValidationError("M3.1 raw schema group key is invalid")
        key = (source_kind, raw_fingerprint)
        if key in seen_keys:
            raise DeepReportValidationError("M3.1 raw schema group key is duplicated or invalid")
        seen_keys.add(key)
        members = members_by_key.get(key, [])
        if not members:
            raise DeepReportValidationError("M3.1 raw schema group has no member inspections")
        group = _group_evidence_from_report(summary, members)
        groups.append(group)
    if seen_keys != set(members_by_key):
        raise DeepReportValidationError(
            "M3.1 archives contain raw-schema groups absent from registry input"
        )
    if sum(group.archive_count for group in groups) != registered_count:
        raise DeepReportValidationError(
            "M3.1 raw-schema group membership does not cover all archives"
        )
    source_url_status_counts = _count_map(source_url_counts, context="source URL status counts")
    license_status_counts = _count_map(license_counts, context="license status counts")
    if (
        sum(count for _, count in source_url_status_counts) != registered_count
        or sum(count for _, count in license_status_counts) != registered_count
    ):
        raise DeepReportValidationError("M3.1 source URL/license status counts do not reconcile")
    return VerifiedDeepEvidenceV1(
        semantic_source_catalog_digest=source_digest,
        deep_inspection_evidence_digest=claimed_digest,
        deep_inspection_report_contract_id=report_contract,
        deep_inspection_method_id=method_id,
        header_inventory_contract_id=header_contract,
        header_inventory_evidence_digest=header_digest,
        configured_source_count=registered_count,
        max_interpreted_data_rows=max_data_rows,
        chronological_coverage_status=chronology_status,
        source_url_status_counts=source_url_status_counts,
        license_status_counts=license_status_counts,
        groups=tuple(
            sorted(groups, key=lambda item: (item.source_kind, item.raw_schema_fingerprint))
        ),
    )


def _field_name_counts(group: RawSchemaGroupEvidenceV1) -> Counter[str]:
    return Counter(group.field_names)


def _lexical_profile_by_unique_name(
    group: RawSchemaGroupEvidenceV1,
) -> dict[str, FieldSurfaceV1]:
    counts = _field_name_counts(group)
    return {
        field.exact_header_name: field
        for field in group.fields
        if counts[field.exact_header_name] == 1 and field.exact_header_name != ""
    }


def _compatible_addition_conditions(
    base: RawSchemaGroupEvidenceV1,
    candidate: RawSchemaGroupEvidenceV1,
    *,
    relative_order_preserved: bool,
    duplicate_ambiguity: bool,
    lexical_drift_names: tuple[str, ...],
    empty_drift_names: tuple[str, ...],
) -> tuple[str, ...]:
    failed: list[str] = []
    base_counts = _field_name_counts(base)
    candidate_counts = _field_name_counts(candidate)
    if base.source_kind != candidate.source_kind:
        failed.append("source_kind_differs")
    if duplicate_ambiguity:
        failed.append("duplicate_name_ambiguity")
    if "" in base_counts:
        failed.append("empty_required_header_name")
    if "" in candidate_counts:
        failed.append("empty_additional_header_name")
    if any(count != 1 for count in base_counts.values()):
        failed.append("base_required_field_name_not_unique")
    if any(candidate_counts[name] != count for name, count in base_counts.items()):
        failed.append("required_field_missing_or_ambiguous")
    if not relative_order_preserved:
        failed.append("required_field_relative_order_changed")
    if lexical_drift_names:
        failed.append("lexical_profile_drift_on_shared_required_fields")
    if empty_drift_names:
        failed.append("empty_field_evidence_drift_on_shared_required_fields")
    if base.has_row_width_drift or candidate.has_row_width_drift:
        failed.append("known_row_width_anomaly_in_member")
    return tuple(failed)


def assess_schema_pair(
    base: RawSchemaGroupEvidenceV1,
    candidate: RawSchemaGroupEvidenceV1,
    *,
    policy: SchemaEvolutionPolicyV1 | None = None,
) -> CompatibilityAssessmentV1:
    """Assess a directional same-source-kind schema pair using only exact evidence."""
    policy = policy or SchemaEvolutionPolicyV1()
    if base.source_kind != candidate.source_kind:
        raise SchemaEvolutionError(
            "Game and Replay schemas cannot be pairwise compatibility inputs"
        )
    left_names = base.field_names
    right_names = candidate.field_names
    left_counts = Counter(left_names)
    right_counts = Counter(right_names)
    duplicate_ambiguity = bool(base.duplicate_header_names or candidate.duplicate_header_names)
    shared_counts = left_counts & right_counts
    shared_exact_count = sum(shared_counts.values())
    left_only_counts = left_counts - right_counts
    right_only_counts = right_counts - left_counts
    left_only_names = tuple(name for name in left_names if left_only_counts[name] > 0)
    right_only_names = tuple(name for name in right_names if right_only_counts[name] > 0)
    common_unique = {
        name
        for name in set(left_names) & set(right_names)
        if name != "" and left_counts[name] == right_counts[name] == 1
    }
    left_shared_order = tuple(name for name in left_names if name in common_unique)
    right_shared_order = tuple(name for name in right_names if name in common_unique)
    relative_order_preserved = left_shared_order == right_shared_order
    left_profile = _lexical_profile_by_unique_name(base)
    right_profile = _lexical_profile_by_unique_name(candidate)
    profile_shared = common_unique & left_profile.keys() & right_profile.keys()
    lexical_drift_names = tuple(
        sorted(
            name
            for name in profile_shared
            if left_profile[name].observed_lexical_classes
            != right_profile[name].observed_lexical_classes
        )
    )
    empty_drift_names = tuple(
        sorted(
            name
            for name in profile_shared
            if left_profile[name].empty_field_observed != right_profile[name].empty_field_observed
        )
    )
    exact_header = left_names == right_names
    exact_contract_candidate = (
        exact_header
        and base.raw_schema_fingerprint == candidate.raw_schema_fingerprint
        and not duplicate_ambiguity
        and not base.has_row_width_drift
        and not candidate.has_row_width_drift
        and not lexical_drift_names
        and not empty_drift_names
    )
    left_is_strict_subset = (
        left_counts != right_counts
        and not (left_counts - right_counts)
        and relative_order_preserved
    )
    right_has_removed_base_fields = bool(left_counts - right_counts)
    row_width_drift = base.has_row_width_drift or candidate.has_row_width_drift
    both_unique = not duplicate_ambiguity
    rename_candidate_names: tuple[str, ...] = ()
    if (
        len(left_names) == len(right_names)
        and len(left_only_names) == 1
        and len(right_only_names) == 1
        and relative_order_preserved
        and both_unique
    ):
        left_name = left_only_names[0]
        right_name = right_only_names[0]
        left_observation = left_profile.get(left_name)
        right_observation = right_profile.get(right_name)
        if (
            left_observation is not None
            and right_observation is not None
            and left_observation.observed_lexical_classes
            == right_observation.observed_lexical_classes
            and left_observation.empty_field_observed == right_observation.empty_field_observed
        ):
            rename_candidate_names = (left_name, right_name)

    findings: set[CompatibilityFinding] = set()
    if exact_header:
        findings.add(CompatibilityFinding.EXACT)
    if left_is_strict_subset:
        findings.add(CompatibilityFinding.ADDITIVE)
    if right_has_removed_base_fields:
        findings.add(CompatibilityFinding.REMOVED)
    if not relative_order_preserved and common_unique:
        findings.add(CompatibilityFinding.REORDERED)
    if rename_candidate_names:
        findings.add(CompatibilityFinding.RENAMED_OR_ALIAS_CANDIDATE)
    if left_only_names and right_only_names and not rename_candidate_names:
        findings.add(CompatibilityFinding.INSUFFICIENT_EVIDENCE)
    if duplicate_ambiguity:
        findings.add(CompatibilityFinding.DUPLICATE_NAME_AMBIGUITY)
    if lexical_drift_names:
        findings.add(CompatibilityFinding.LEXICAL_TYPE_EVIDENCE_DRIFT)
    if empty_drift_names:
        findings.add(CompatibilityFinding.EMPTY_FIELD_EVIDENCE_DRIFT)
    if row_width_drift:
        findings.add(CompatibilityFinding.ROW_WIDTH_DRIFT)
    if base.raw_schema_fingerprint != candidate.raw_schema_fingerprint:
        findings.add(CompatibilityFinding.SEMANTIC_DRIFT_UNKNOWN)

    additive_rule_passed = False
    additive_direction: str | None = None
    if left_is_strict_subset:
        additive_direction = "left_to_right"
        additive_rule_passed = not _compatible_addition_conditions(
            base,
            candidate,
            relative_order_preserved=relative_order_preserved,
            duplicate_ambiguity=duplicate_ambiguity,
            lexical_drift_names=lexical_drift_names,
            empty_drift_names=empty_drift_names,
        )
    if exact_header:
        if exact_contract_candidate:
            status = "exact_same_physical_schema"
            review_status = ReviewStatus.REVIEWED
        else:
            # Equal parsed field surfaces do not explain differing physical fingerprints.
            status = "exact_fields_needs_physical_evidence_review"
            findings.add(CompatibilityFinding.INSUFFICIENT_EVIDENCE)
            review_status = ReviewStatus.NEEDS_REVIEW
    elif additive_rule_passed:
        status = "additive_compatible_under_policy"
        review_status = ReviewStatus.REVIEWED
    elif rename_candidate_names:
        status = "needs_review"
        findings.add(CompatibilityFinding.INSUFFICIENT_EVIDENCE)
        review_status = ReviewStatus.NEEDS_REVIEW
    elif CompatibilityFinding.REORDERED in findings or right_has_removed_base_fields:
        status = "incompatible_under_default_policy"
        findings.add(CompatibilityFinding.INCOMPATIBLE)
        review_status = ReviewStatus.BLOCKED
    elif left_is_strict_subset or additive_direction is not None or rename_candidate_names:
        status = "needs_review"
        findings.add(CompatibilityFinding.INSUFFICIENT_EVIDENCE)
        review_status = ReviewStatus.NEEDS_REVIEW
    else:
        status = "insufficient_evidence"
        findings.add(CompatibilityFinding.INSUFFICIENT_EVIDENCE)
        review_status = ReviewStatus.NEEDS_REVIEW

    body: dict[str, object] = {
        "additive_direction": additive_direction,
        "additive_rule_passed": additive_rule_passed,
        "assessment_contract_id": SCHEMA_COMPATIBILITY_ASSESSMENT_ID,
        "assessment_status": status,
        "base_raw_schema_fingerprint": base.raw_schema_fingerprint,
        "candidate_raw_schema_fingerprint": candidate.raw_schema_fingerprint,
        "duplicate_name_ambiguity": duplicate_ambiguity,
        "empty_field_evidence_drift_names": list(empty_drift_names),
        "exact_contract_candidate": exact_contract_candidate,
        "exact_header_order_equality": exact_header,
        "findings": sorted(item.value for item in findings),
        "left_group_id": base.group_id,
        "left_only_exact_field_count": sum(left_only_counts.values()),
        "left_only_field_names": list(left_only_names),
        "lexical_type_evidence_drift_names": list(lexical_drift_names),
        "right_group_id": candidate.group_id,
        "right_only_exact_field_count": sum(right_only_counts.values()),
        "right_only_field_names": list(right_only_names),
        "shared_exact_field_count": shared_exact_count,
        "shared_fields_preserve_relative_order": relative_order_preserved,
        "source_kind": base.source_kind,
    }
    assessment_id = _digest_json(body)
    return CompatibilityAssessmentV1(
        contract_id=SCHEMA_COMPATIBILITY_ASSESSMENT_ID,
        assessment_id=assessment_id,
        source_kind=base.source_kind,
        left_group_id=base.group_id,
        left_raw_schema_fingerprint=base.raw_schema_fingerprint,
        right_group_id=candidate.group_id,
        right_raw_schema_fingerprint=candidate.raw_schema_fingerprint,
        left_field_count=len(left_names),
        right_field_count=len(right_names),
        exact_ordered_header_equality=exact_header,
        shared_exact_field_count=shared_exact_count,
        left_only_exact_field_count=sum(left_only_counts.values()),
        right_only_exact_field_count=sum(right_only_counts.values()),
        left_only_field_name_samples=left_only_names[:8],
        right_only_field_name_samples=right_only_names[:8],
        shared_fields_preserve_relative_order=relative_order_preserved,
        duplicate_name_ambiguity=duplicate_ambiguity,
        empty_header_name_present="" in left_counts or "" in right_counts,
        lexical_type_evidence_drift_field_count=len(lexical_drift_names),
        lexical_type_evidence_drift_name_samples=lexical_drift_names[:8],
        empty_field_evidence_drift_field_count=len(empty_drift_names),
        empty_field_evidence_drift_name_samples=empty_drift_names[:8],
        left_row_width_anomaly_archive_count=len(base.row_width_anomalies),
        right_row_width_anomaly_archive_count=len(candidate.row_width_anomalies),
        additive_direction=additive_direction,
        additive_rule_passed=additive_rule_passed,
        exact_contract_candidate=exact_contract_candidate,
        findings=tuple(sorted(findings, key=lambda item: item.value)),
        assessment_status=status,
        review_status=review_status,
    )


def derive_source_interpretation_contract_id(projection: dict[str, object]) -> str:
    """Derive a versioned interpretation identity from its canonical policy projection."""
    if projection.get("contract_schema_id") != SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID:
        raise SchemaEvolutionError("interpretation ID projection has an unsupported schema ID")
    digest = _digest_json(
        {
            "contract_id_derivation_id": SOURCE_INTERPRETATION_ID_CONTRACT_ID,
            "contract_projection": projection,
        }
    )
    return f"openmtgdata.source-interpretation.v1:{digest}"


def _contract_for_family(
    members: tuple[RawSchemaGroupEvidenceV1, ...],
    *,
    evidence: VerifiedDeepEvidenceV1,
    policy: SchemaEvolutionPolicyV1,
) -> SourceInterpretationContractV1:
    if not members:
        raise SchemaEvolutionError("cannot create an interpretation contract for an empty family")
    source_kinds = {item.source_kind for item in members}
    if len(source_kinds) != 1:
        raise SchemaEvolutionError("Game and Replay groups cannot share an interpretation contract")
    reference = min(
        members,
        key=lambda item: (len(item.fields), item.raw_schema_fingerprint),
    )
    anomaly_codes: set[str] = set()
    for member in members:
        if member.row_width_anomalies:
            anomaly_codes.add(CompatibilityFinding.ROW_WIDTH_DRIFT.value)
        if member.duplicate_header_names:
            anomaly_codes.add(CompatibilityFinding.DUPLICATE_NAME_AMBIGUITY.value)
        if member.empty_header_positions:
            anomaly_codes.add("empty_header_name_preserved_by_index")
        if any(len(field.observed_lexical_classes) > 1 for field in member.fields):
            anomaly_codes.add("mixed_lexical_classes_observed")

    lexical_policy = (
        "bounded-prefix classes are descriptive only; no semantic type assignment; a shared "
        "required field must have equal observed non-empty class support and empty-observation "
        "status for automatic additive admission"
    )
    if len(members) > 1:
        additional_policy = (
            "additional member fields are preserved_uninterpreted; only explicitly listed member "
            "fingerprints are admitted; unseen additions fail closed"
        )
        order_policy = (
            "reference required fields retain exact spelling and relative order; admitted extra "
            "fields may be inserted at any position without changing required-field relative order"
        )
    else:
        additional_policy = (
            "preserved_uninterpreted in raw evidence; no unregistered extra field is admitted; "
            "unseen fingerprints fail closed until reviewed"
        )
        order_policy = "exact registered header order is required"
    projection = SourceInterpretationContractV1(
        contract_schema_id=SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID,
        source_interpretation_contract_id="",
        contract_id_derivation_id=SOURCE_INTERPRETATION_ID_CONTRACT_ID,
        deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
        source_kind=reference.source_kind,
        reference_raw_schema_fingerprint=reference.raw_schema_fingerprint,
        member_raw_schema_fingerprints=tuple(
            sorted(item.raw_schema_fingerprint for item in members)
        ),
        member_schema_group_ids=tuple(sorted(item.group_id for item in members)),
        required_fields=tuple(
            (field.column_index, field.exact_header_name) for field in reference.fields
        ),
        reference_schema_selection_rule=REFERENCE_SCHEMA_SELECTION_RULE,
        required_field_order_rule=order_policy,
        csv_parser_policy_id=CSV_PARSER_POLICY_ID,
        encoding_policy=(
            "strict UTF-8 decoding with optional initial UTF-8 BOM; "
            "source encoding declaration unknown"
        ),
        unknown_additional_field_policy=additional_policy,
        duplicate_field_policy=(
            "preserve duplicate names by column index; name-only mapping is prohibited"
        ),
        empty_header_name_policy=(
            "preserve exact empty name and index; never synthesize a name; "
            "semantic mapping is unset"
        ),
        row_width_policy=(
            "exact registered member-header width required; short and long rows are unsupported; "
            "no padding, truncation, or synthesized fields"
        ),
        lexical_evidence_contract_id=LEXICAL_EVIDENCE_CONTRACT_ID,
        lexical_evidence_scope=(
            "M3.1 deterministic first-"
            f"{evidence.max_interpreted_data_rows}-data-record prefix per archive; "
            "not full-history evidence"
        ),
        member_lexical_profile_digests=tuple(
            sorted(item.lexical_profile_digest for item in members)
        ),
        lexical_profile_merge_rule=lexical_policy,
        null_semantics_status="not_established; empty-field evidence is not nullable proof",
        semantic_mapping_status="source_schema_compatibility_only",
        semantic_drift_status="not_established_beyond_structural_and_bounded_lexical_evidence",
        known_anomaly_codes=tuple(sorted(anomaly_codes)),
        member_evidence_digests=tuple(
            sorted(
                _digest_json(
                    {
                        "deep_inspection_evidence_digest": evidence.deep_inspection_evidence_digest,
                        "group_id": item.group_id,
                        "lexical_profile_digest": item.lexical_profile_digest,
                        "raw_schema_fingerprint": item.raw_schema_fingerprint,
                        "row_width_anomalies": [
                            anomaly.to_dict() for anomaly in item.row_width_anomalies
                        ],
                    }
                )
                for item in members
            )
        ),
        schema_evolution_policy_id=policy.policy_id,
        review_status=ReviewStatus.REVIEWED,
        review_basis=(
            "exact_header_evidence",
            "bounded_lexical_evidence",
            "row_width_evidence",
            "explicit_m3_2_policy_application",
        ),
    )
    contract_id = derive_source_interpretation_contract_id(projection.projection_dict())
    return replace(projection, source_interpretation_contract_id=contract_id)


def _form_interpretation_families(
    groups: tuple[RawSchemaGroupEvidenceV1, ...],
    assessments: tuple[CompatibilityAssessmentV1, ...],
) -> tuple[tuple[RawSchemaGroupEvidenceV1, ...], ...]:
    assessment_by_pair = {
        (
            assessment.source_kind,
            assessment.left_raw_schema_fingerprint,
            assessment.right_raw_schema_fingerprint,
        ): assessment
        for assessment in assessments
    }
    families: list[tuple[RawSchemaGroupEvidenceV1, ...]] = []
    for source_kind in sorted({item.source_kind for item in groups}):
        pending = sorted(
            (item for item in groups if item.source_kind == source_kind),
            key=lambda item: (len(item.fields), item.raw_schema_fingerprint),
        )
        while pending:
            reference = pending.pop(0)
            members = [reference]
            accepted: list[RawSchemaGroupEvidenceV1] = []
            for candidate in pending:
                assessment = assessment_by_pair.get(
                    (
                        reference.source_kind,
                        reference.raw_schema_fingerprint,
                        candidate.raw_schema_fingerprint,
                    )
                )
                # Every member is checked directly against the contract reference. No connected-
                # component/transitive compatibility is inferred.
                if assessment is not None and assessment.additive_rule_passed:
                    accepted.append(candidate)
            members.extend(accepted)
            accepted_ids = {item.raw_schema_fingerprint for item in accepted}
            pending = [item for item in pending if item.raw_schema_fingerprint not in accepted_ids]
            families.append(tuple(members))
    return tuple(families)


def build_schema_registry(
    evidence: VerifiedDeepEvidenceV1,
    *,
    policy: SchemaEvolutionPolicyV1 | None = None,
) -> SchemaRegistryV1:
    """Create deterministic same-source-kind assessments, contracts, and registry entries."""
    policy = policy or SchemaEvolutionPolicyV1()
    groups = evidence.groups
    if len({group.group_id for group in groups}) != len(groups):
        raise SchemaEvolutionError("M3.1 raw schema group IDs are not unique")
    group_keys = {(group.source_kind, group.raw_schema_fingerprint) for group in groups}
    if len(group_keys) != len(groups):
        raise SchemaEvolutionError("M3.1 has duplicate source-kind/raw-fingerprint groups")

    assessments: list[CompatibilityAssessmentV1] = []
    by_kind: dict[str, list[RawSchemaGroupEvidenceV1]] = defaultdict(list)
    for group in groups:
        by_kind[group.source_kind].append(group)
    for _source_kind, same_kind_groups in sorted(by_kind.items()):
        ordered = sorted(same_kind_groups, key=lambda item: item.raw_schema_fingerprint)
        for base in ordered:
            for candidate in ordered:
                if base.group_id == candidate.group_id:
                    continue
                assessments.append(assess_schema_pair(base, candidate, policy=policy))
    ordered_assessments = tuple(
        sorted(
            assessments,
            key=lambda item: (
                item.source_kind,
                item.left_raw_schema_fingerprint,
                item.right_raw_schema_fingerprint,
            ),
        )
    )

    families = _form_interpretation_families(groups, ordered_assessments)
    contracts = tuple(
        sorted(
            (_contract_for_family(family, evidence=evidence, policy=policy) for family in families),
            key=lambda item: item.source_interpretation_contract_id,
        )
    )
    contract_by_group_id: dict[str, SourceInterpretationContractV1] = {}
    for contract in contracts:
        for group_id in contract.member_schema_group_ids:
            if group_id in contract_by_group_id:
                raise SchemaEvolutionError("a raw schema group was assigned to multiple contracts")
            contract_by_group_id[group_id] = contract
    if set(contract_by_group_id) != {group.group_id for group in groups}:
        raise SchemaEvolutionError("schema registry contract assignment omitted a raw group")

    findings_by_group: dict[str, set[CompatibilityFinding]] = {
        group.group_id: set() for group in groups
    }
    assessment_counts: Counter[str] = Counter()
    finding_counts: Counter[str] = Counter({item.value: 0 for item in CompatibilityFinding})
    for assessment in ordered_assessments:
        findings = set(assessment.findings)
        findings_by_group[assessment.left_group_id].update(findings)
        findings_by_group[assessment.right_group_id].update(findings)
        assessment_counts[assessment.left_group_id] += 1
        assessment_counts[assessment.right_group_id] += 1
        for finding in assessment.findings:
            finding_counts[finding.value] += 1

    registry_groups: list[SchemaRegistryGroupV1] = []
    for group in groups:
        contract = contract_by_group_id[group.group_id]
        disposition = (
            RegistryDisposition.SUPPORTED_INTERPRETATION
            if contract.review_status is ReviewStatus.REVIEWED
            else RegistryDisposition.BLOCKED_INSUFFICIENT_EVIDENCE
        )
        anomaly_codes = set(contract.known_anomaly_codes)
        registry_groups.append(
            SchemaRegistryGroupV1(
                group_id=group.group_id,
                source_kind=group.source_kind,
                raw_schema_fingerprint=group.raw_schema_fingerprint,
                archive_count=group.archive_count,
                disposition=disposition,
                source_interpretation_contract_id=contract.source_interpretation_contract_id,
                reference_raw_schema_fingerprint=contract.reference_raw_schema_fingerprint,
                required_field_count=len(contract.required_fields),
                expansion_format_counts=group.expansion_format_counts,
                compatibility_findings=tuple(
                    sorted(findings_by_group[group.group_id], key=lambda item: item.value)
                ),
                compatibility_assessment_count=assessment_counts[group.group_id],
                known_row_width_drift=bool(group.row_width_anomalies),
                row_width_anomaly_archive_count=len(group.row_width_anomalies),
                row_width_anomalies=group.row_width_anomalies,
                known_anomaly_codes=tuple(sorted(anomaly_codes)),
                review_status=contract.review_status,
                review_reason=(
                    "Reviewed under the exact, source-schema-compatibility-only M3.2 policy; "
                    "no semantic mapping or cross-group equivalence is asserted. "
                    + (
                        "Rows with non-exact width are unsupported by this contract."
                        if group.has_row_width_drift
                        else ""
                    )
                ),
                evidence_digest=_digest_json(group.to_dict()),
            )
        )
    ordered_registry_groups = tuple(
        sorted(registry_groups, key=lambda item: (item.source_kind, item.raw_schema_fingerprint))
    )
    supported = sum(
        item.disposition is RegistryDisposition.SUPPORTED_INTERPRETATION
        for item in ordered_registry_groups
    )
    blocked = sum(
        item.disposition is not RegistryDisposition.SUPPORTED_INTERPRETATION
        for item in ordered_registry_groups
    )
    needs_review_groups = sum(
        item.review_status is ReviewStatus.NEEDS_REVIEW for item in ordered_registry_groups
    )
    needs_review_assessments = sum(
        item.review_status is ReviewStatus.NEEDS_REVIEW for item in ordered_assessments
    )
    game_contract_count = sum(contract.source_kind == "game" for contract in contracts)
    replay_contract_count = sum(contract.source_kind == "replay" for contract in contracts)
    return SchemaRegistryV1(
        registry_contract_id=SCHEMA_REGISTRY_CONTRACT_ID,
        registry_digest_contract_id=SCHEMA_REGISTRY_DIGEST_CONTRACT_ID,
        policy=policy,
        semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
        deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
        deep_inspection_report_contract_id=evidence.deep_inspection_report_contract_id,
        deep_inspection_method_id=evidence.deep_inspection_method_id,
        header_inventory_contract_id=evidence.header_inventory_contract_id,
        header_inventory_evidence_digest=evidence.header_inventory_evidence_digest,
        interpretation_contracts=contracts,
        groups=ordered_registry_groups,
        compatibility_assessments=ordered_assessments,
        finding_counts=tuple(sorted(finding_counts.items())),
        source_url_status_counts=evidence.source_url_status_counts,
        license_status_counts=evidence.license_status_counts,
        schema_group_count=len(groups),
        registered_archive_count=evidence.configured_source_count,
        max_interpreted_data_rows=evidence.max_interpreted_data_rows,
        chronological_coverage_status=evidence.chronological_coverage_status,
        supported_group_count=supported,
        blocked_group_count=blocked,
        needs_review_group_count=needs_review_groups,
        interpretation_contract_count=len(contracts),
        game_contract_count=game_contract_count,
        replay_contract_count=replay_contract_count,
        needs_review_assessment_count=needs_review_assessments,
        known_row_width_drift_group_count=sum(group.has_row_width_drift for group in groups),
        policy_audit_note=(
            "reviewed means reviewed under the explicit M3.2 structural policy; it does not "
            "claim semantic equivalence or a human-approved normalized mapping"
        ),
    )


def write_schema_registry_no_overwrite(
    registry: SchemaRegistryV1,
    output_path: str | Path,
    config: RuntimeConfig,
    *,
    base_dir: str | Path,
) -> Path:
    """Atomically create a registry under intermediate_root without replacing an existing file."""
    candidate = Path(output_path)
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate
    target = candidate.resolve(strict=False)
    intermediate = config.intermediate_root.resolve(strict=False)
    if not target.is_relative_to(intermediate):
        raise SchemaEvolutionError("schema registry output must be under intermediate_root")
    if any(target == root or target.is_relative_to(root) for root in config.raw_roots):
        raise SchemaEvolutionError("schema registry output aliases a configured raw root")
    if target.exists() and target.is_dir():
        raise SchemaEvolutionError("schema registry output path is a directory")

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    if not resolved_parent.is_relative_to(intermediate):
        raise SchemaEvolutionError("schema registry parent escaped intermediate_root")
    target = resolved_parent / target.name
    if any(target == root or target.is_relative_to(root) for root in config.raw_roots):
        raise SchemaEvolutionError("resolved registry output aliases a configured raw root")
    if os.path.lexists(target):
        raise SchemaEvolutionError("schema registry output exists; refusing overwrite")

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".openmtgdata-registry-incomplete-",
            suffix=".tmp",
            dir=resolved_parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(registry.canonical_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, target)
    except FileExistsError as exc:
        raise SchemaEvolutionError(
            "schema registry output appeared concurrently; refusing overwrite"
        ) from exc
    except OSError as exc:
        raise SchemaEvolutionError("could not safely create schema registry report") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target
