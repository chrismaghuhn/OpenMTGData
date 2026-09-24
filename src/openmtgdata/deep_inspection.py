"""Bounded row-level evidence collection for M3.1, without semantic adapters."""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
import re
import tempfile
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from openmtgdata.archive_registration import (
    ARCHIVE_REGISTRATION_CONTRACT_ID,
    SOURCE_ARCHIVE_RECORD_SCHEMA_ID,
    ArchiveRegistrationError,
    ArchiveRegistrationResult,
    SourceArchiveRecordV1,
    _open_verified_archive,
    _verify_opened_archive_unchanged,
    register_archive,
)
from openmtgdata.compression_validation import (
    CompressionDiagnosticCode,
    CompressionValidationEvidenceV1,
    CompressionValidationStatus,
)
from openmtgdata.config import RuntimeConfig
from openmtgdata.header_inspection import (
    HEADER_INSPECTION_METHOD_ID,
    HEADER_INVENTORY_CONTRACT_ID,
    CsvParserObservationV1,
    EncodingObservationV1,
    HeaderDiagnosticCode,
    HeaderInspectionError,
    HeaderInspectionStatus,
    HeaderInventoryV1,
    SourceInspectionV1,
    _BoundedPhysicalLines,
    _canonical_bytes,
    csv_field_limit,
    fingerprint_raw_schema,
)
from openmtgdata.inventory import InventoryItem
from openmtgdata.source_container import open_csv_source_payload
from openmtgdata.source_filename import RecognizedSourceFilename, SourceKind
from openmtgdata.source_manifest import (
    SOURCE_MANIFEST_SCHEMA_ID,
    SourceManifestV1,
)

DEEP_INSPECTION_SCHEMA_ID = "openmtgdata.deep-source-inspection-record.v1"
DEEP_INSPECTION_METHOD_ID = "openmtgdata.deep-source-inspection.v2"
DEEP_INSPECTION_ID_CONTRACT_ID = "openmtgdata.deep-source-inspection-id.v1"
DEEP_INSPECTION_REPORT_CONTRACT_ID = "openmtgdata.deep-inspection-report.v1"
LEXICAL_EVIDENCE_CONTRACT_ID = "openmtgdata.lexical-field-evidence.v1"
REPRESENTATIVE_SELECTION_CONTRACT_ID = "openmtgdata.deep-representative-selection.v1"
DEEP_REPORT_DIGEST_CONTRACT_ID = "openmtgdata.deep-inspection-report-digest.v1"
DEEP_CSV_POLICY_ID = "openmtgdata.csv-header-policy.comma-utf8.v1"
DEEP_ENCODING_POLICY = (
    "strict UTF-8 decoding; optional initial UTF-8 BOM; no authoritative encoding declaration"
)
LEXICAL_CLASSIFICATION_RULE = (
    "Exact decoded field text, no trimming or locale parsing: empty is ''; boolean is exactly "
    "'true' or 'false'; integer matches ASCII [+-]?[0-9]+; decimal matches ASCII "
    "[+-]?(?:(?:[0-9]+\\.[0-9]*|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+); "
    "all other text is other_text."
)
ROW_WIDTH_EVIDENCE_RULE = (
    "Parsed row widths are counted exactly; records are never padded or truncated; missing "
    "positions in short rows are not counted as empty fields. Empty text is observed as empty, "
    "with semantic null behavior not established."
)
ROW_FAILURE_RULE = (
    "A malformed/over-limit sampled record records its 1-based CSV record ordinal, retains only "
    "aggregates from earlier complete records, and terminates that archive as partial."
)
MAX_INTERPRETED_DATA_ROWS = 256
MAX_LOGICAL_ROW_BYTES = 8 * 1024 * 1024
MAX_ROW_FIELDS = 4096
MAX_FIELD_CHARS = 1024 * 1024
TOOL_IDENTITY_DEFAULT = "openmtgdata-deep-inspection-v1"

_INTEGER_LEXEME = re.compile(r"[+-]?[0-9]+\Z", re.ASCII)
_DECIMAL_LEXEME = re.compile(
    r"[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+)\Z",
    re.ASCII,
)


class DeepInspectionError(RuntimeError):
    """A provenance, input-contract, or filesystem failure prevents trusted output."""


class DeepInspectionStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class SampleTermination(StrEnum):
    ROW_LIMIT_REACHED = "row_limit_reached"
    SOURCE_EOF_REACHED = "source_eof_reached"
    ROW_PARSE_ERROR = "row_parse_error"
    HEADER_UNAVAILABLE = "header_unavailable"
    COMPRESSION_UNAVAILABLE = "compression_unavailable"


class DeepDiagnosticCode(StrEnum):
    MALFORMED_DATA_ROW = "MALFORMED_DATA_ROW"
    INVALID_DATA_ENCODING = "INVALID_DATA_ENCODING"
    DATA_ROW_TOO_LARGE = "DATA_ROW_TOO_LARGE"
    DATA_FIELD_TOO_LARGE = "DATA_FIELD_TOO_LARGE"
    DATA_READ_FAILURE = "DATA_READ_FAILURE"
    TOO_MANY_ROW_FIELDS = "TOO_MANY_ROW_FIELDS"
    ROW_WIDTH_SHORTER = "ROW_WIDTH_SHORTER"
    ROW_WIDTH_LONGER = "ROW_WIDTH_LONGER"
    COMPRESSION_NOT_VALID = "COMPRESSION_NOT_VALID"
    COMPRESSION_NOT_CHECKED = "COMPRESSION_NOT_CHECKED"
    COMPRESSION_READ_FAILURE = "COMPRESSION_READ_FAILURE"
    INVALID_GZIP = "INVALID_GZIP"
    TRUNCATED_GZIP = "TRUNCATED_GZIP"
    GZIP_INTEGRITY_FAILURE = "GZIP_INTEGRITY_FAILURE"
    HEADER_EVIDENCE_UNAVAILABLE = "HEADER_EVIDENCE_UNAVAILABLE"


class LexicalClass(StrEnum):
    EMPTY = "empty"
    INTEGER = "integer_lexeme"
    DECIMAL = "decimal_lexeme"
    BOOLEAN = "boolean_lexeme"
    OTHER_TEXT = "other_text"


LEXICAL_CLASS_ORDER = tuple(item.value for item in LexicalClass)


def classify_lexeme(value: str) -> LexicalClass:
    """Classify exact decoded field text using the lexical-evidence v1 grammar."""
    if value == "":
        return LexicalClass.EMPTY
    if value in {"true", "false"}:
        return LexicalClass.BOOLEAN
    if _INTEGER_LEXEME.fullmatch(value):
        return LexicalClass.INTEGER
    if _DECIMAL_LEXEME.fullmatch(value):
        return LexicalClass.DECIMAL
    return LexicalClass.OTHER_TEXT


def _location_key(record: SourceArchiveRecordV1) -> tuple[str, str]:
    return str(record.raw_root), record.relative_path.as_posix()


def _inventory_location_key(inspection: SourceInspectionV1) -> tuple[str, str]:
    return str(inspection.raw_root), inspection.relative_path.as_posix()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class LexicalClassCountsV1:
    empty: int = 0
    integer_lexeme: int = 0
    decimal_lexeme: int = 0
    boolean_lexeme: int = 0
    other_text: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "boolean_lexeme": self.boolean_lexeme,
            "decimal_lexeme": self.decimal_lexeme,
            "empty": self.empty,
            "integer_lexeme": self.integer_lexeme,
            "other_text": self.other_text,
        }


@dataclass(frozen=True, slots=True)
class ColumnRowEvidenceV1:
    column_index: int
    exact_header_name: str
    rows_observed: int
    empty_field_count: int
    non_empty_field_count: int
    lexical_class_counts: LexicalClassCountsV1
    minimum_observed_character_length: int | None
    maximum_observed_character_length: int | None
    maximum_observed_utf8_byte_length: int | None
    mixed_lexical_class_observed: bool
    source_null_semantics: str = "not_established"
    type_authority: str = "lexical_evidence_only"

    def to_dict(self) -> dict[str, object]:
        return {
            "column_index": self.column_index,
            "empty_field_count": self.empty_field_count,
            "exact_header_name": self.exact_header_name,
            "lexical_class_counts": self.lexical_class_counts.to_dict(),
            "maximum_observed_character_length": self.maximum_observed_character_length,
            "maximum_observed_utf8_byte_length": self.maximum_observed_utf8_byte_length,
            "minimum_observed_character_length": self.minimum_observed_character_length,
            "mixed_lexical_class_observed": self.mixed_lexical_class_observed,
            "non_empty_field_count": self.non_empty_field_count,
            "rows_observed": self.rows_observed,
            "source_null_semantics": self.source_null_semantics,
            "type_authority": self.type_authority,
        }


@dataclass(frozen=True, slots=True)
class DeepFindingV1:
    code: DeepDiagnosticCode
    row_ordinal: int | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code.value, "reason": self.reason, "row_ordinal": self.row_ordinal}


@dataclass(frozen=True, slots=True)
class DeepSourceInspectionV1:
    deep_inspection_schema_id: str
    deep_inspection_method_id: str
    deep_inspection_id_contract_id: str
    deep_inspection_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    compression_validation_status: CompressionValidationStatus
    provider: str
    original_filename: str
    source_kind: SourceKind | None
    expansion_token: str | None
    format_token: str | None
    source_url_status: str
    source_url: str | None
    source_evidence_refs: tuple[str, ...]
    license_status: str
    license_identifier: str | None
    license_evidence_refs: tuple[str, ...]
    m2_header_inspection_id: str | None
    header_inspection_method_id: str
    raw_schema_fingerprint: str | None
    status: DeepInspectionStatus
    sample_termination: SampleTermination
    max_interpreted_data_rows: int
    max_logical_row_bytes: int
    max_row_fields: int
    max_field_chars: int
    rows_interpreted: int
    rows_matching_header_width: int
    rows_shorter_than_header: int
    rows_longer_than_header: int
    expected_field_count: int | None
    minimum_observed_row_width: int | None
    maximum_observed_row_width: int | None
    maximum_observed_logical_row_bytes: int | None
    failed_row_ordinal: int | None
    columns: tuple[ColumnRowEvidenceV1, ...]
    diagnostics: tuple[DeepFindingV1, ...]
    source_interpretation_contract_id: None
    tool_identity: str | None
    inspection_timestamp_utc: str | None

    def to_evidence_dict(self) -> dict[str, object]:
        """Serialize inspection evidence without its audit-only timestamp."""
        return {
            "columns": [column.to_dict() for column in self.columns],
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "compression_validation_status": self.compression_validation_status.value,
            "deep_inspection_id": self.deep_inspection_id,
            "deep_inspection_id_contract_id": self.deep_inspection_id_contract_id,
            "deep_inspection_method_id": self.deep_inspection_method_id,
            "deep_inspection_schema_id": self.deep_inspection_schema_id,
            "diagnostics": [finding.to_dict() for finding in self.diagnostics],
            "expected_field_count": self.expected_field_count,
            "expansion_token": self.expansion_token,
            "failed_row_ordinal": self.failed_row_ordinal,
            "format_token": self.format_token,
            "header_inspection_method_id": self.header_inspection_method_id,
            "license_evidence_refs": list(self.license_evidence_refs),
            "license_identifier": self.license_identifier,
            "license_status": self.license_status,
            "max_field_chars": self.max_field_chars,
            "max_interpreted_data_rows": self.max_interpreted_data_rows,
            "max_logical_row_bytes": self.max_logical_row_bytes,
            "max_row_fields": self.max_row_fields,
            "maximum_observed_logical_row_bytes": self.maximum_observed_logical_row_bytes,
            "maximum_observed_row_width": self.maximum_observed_row_width,
            "minimum_observed_row_width": self.minimum_observed_row_width,
            "m2_header_inspection_id": self.m2_header_inspection_id,
            "original_filename": self.original_filename,
            "provider": self.provider,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "rows_interpreted": self.rows_interpreted,
            "rows_longer_than_header": self.rows_longer_than_header,
            "rows_matching_header_width": self.rows_matching_header_width,
            "rows_shorter_than_header": self.rows_shorter_than_header,
            "sample_termination": self.sample_termination.value,
            "source_archive_id": self.source_archive_id,
            "source_evidence_refs": list(self.source_evidence_refs),
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
            "source_url": self.source_url,
            "source_url_status": self.source_url_status,
            "status": self.status.value,
            "tool_identity": self.tool_identity,
        }

    def to_dict(self) -> dict[str, object]:
        """Serialize full inspection content, including its separate audit timestamp."""
        document = self.to_evidence_dict()
        document["inspection_timestamp_utc"] = self.inspection_timestamp_utc
        return document


@dataclass(frozen=True, slots=True)
class CounterpartContextV1:
    status: str
    source_archive_id: str | None
    original_filename: str | None
    source_kind: SourceKind | None
    expansion_token: str | None
    format_token: str | None
    deep_inspection_id: str | None
    inspection_status: DeepInspectionStatus | None
    interpretation: str = "archive-level context only; no row correspondence or join semantics"

    def to_dict(self) -> dict[str, object]:
        return {
            "deep_inspection_id": self.deep_inspection_id,
            "expansion_token": self.expansion_token,
            "format_token": self.format_token,
            "inspection_status": self.inspection_status.value
            if self.inspection_status is not None
            else None,
            "interpretation": self.interpretation,
            "original_filename": self.original_filename,
            "source_archive_id": self.source_archive_id,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class RepresentativeEvidenceV1:
    group_id: str
    source_kind: SourceKind | None
    raw_schema_fingerprint: str
    archive_count: int
    expansion_format_counts: tuple[tuple[str, str, int], ...]
    representative: DeepSourceInspectionV1
    counterpart: CounterpartContextV1
    empty_column_indices: tuple[int, ...]
    mixed_lexical_column_indices: tuple[int, ...]

    def summary_dict(self) -> dict[str, object]:
        inspection = self.representative
        return {
            "archive_count": self.archive_count,
            "counterpart": self.counterpart.to_dict(),
            "designated_representative": {
                "deep_inspection_id": inspection.deep_inspection_id,
                "expansion_token": inspection.expansion_token,
                "expected_field_count": inspection.expected_field_count,
                "format_token": inspection.format_token,
                "license_status": inspection.license_status,
                "maximum_observed_row_width": inspection.maximum_observed_row_width,
                "minimum_observed_row_width": inspection.minimum_observed_row_width,
                "original_filename": inspection.original_filename,
                "raw_schema_fingerprint": inspection.raw_schema_fingerprint,
                "rows_interpreted": inspection.rows_interpreted,
                "rows_longer_than_header": inspection.rows_longer_than_header,
                "rows_matching_header_width": inspection.rows_matching_header_width,
                "rows_shorter_than_header": inspection.rows_shorter_than_header,
                "sample_termination": inspection.sample_termination.value,
                "source_archive_id": inspection.source_archive_id,
                "source_url_status": inspection.source_url_status,
                "status": inspection.status.value,
            },
            "empty_column_count": len(self.empty_column_indices),
            "expansion_format_counts": [
                {"expansion": expansion, "format": fmt, "archive_count": count}
                for expansion, fmt, count in self.expansion_format_counts
            ],
            "group_id": self.group_id,
            "mixed_lexical_column_count": len(self.mixed_lexical_column_indices),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
        }

    def to_dict(self) -> dict[str, object]:
        inspection = self.representative
        return {
            "archive_count": self.archive_count,
            "counterpart": self.counterpart.to_dict(),
            "expansion_format_counts": [
                {"expansion": expansion, "format": fmt, "archive_count": count}
                for expansion, fmt, count in self.expansion_format_counts
            ],
            "designated_representative": {
                "deep_inspection_id": inspection.deep_inspection_id,
                "expansion_token": inspection.expansion_token,
                "expected_field_count": inspection.expected_field_count,
                "format_token": inspection.format_token,
                "license_status": inspection.license_status,
                "maximum_observed_row_width": inspection.maximum_observed_row_width,
                "minimum_observed_row_width": inspection.minimum_observed_row_width,
                "original_filename": inspection.original_filename,
                "raw_schema_fingerprint": inspection.raw_schema_fingerprint,
                "rows_interpreted": inspection.rows_interpreted,
                "rows_longer_than_header": inspection.rows_longer_than_header,
                "rows_matching_header_width": inspection.rows_matching_header_width,
                "rows_shorter_than_header": inspection.rows_shorter_than_header,
                "sample_termination": inspection.sample_termination.value,
                "source_archive_id": inspection.source_archive_id,
                "source_url_status": inspection.source_url_status,
                "status": inspection.status.value,
            },
            "empty_column_indices": list(self.empty_column_indices),
            "group_id": self.group_id,
            "mixed_lexical_column_indices": list(self.mixed_lexical_column_indices),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FormatCoverageV1:
    source_kind: SourceKind | None
    raw_schema_fingerprint: str | None
    expansion_token: str | None
    format_token: str | None
    archive_count: int
    inspections_successful: int
    inspections_partial: int
    inspections_unsupported: int
    rows_interpreted: int

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "expansion_token": self.expansion_token,
            "format_token": self.format_token,
            "inspections_partial": self.inspections_partial,
            "inspections_successful": self.inspections_successful,
            "inspections_unsupported": self.inspections_unsupported,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "rows_interpreted": self.rows_interpreted,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
        }


@dataclass(frozen=True, slots=True)
class OperationalMetricsV1:
    wall_clock_seconds: float
    archives_per_second: float
    interpreted_rows_per_second: float

    def to_dict(self) -> dict[str, float]:
        return {
            "archives_per_second": self.archives_per_second,
            "interpreted_rows_per_second": self.interpreted_rows_per_second,
            "wall_clock_seconds": self.wall_clock_seconds,
        }


@dataclass(frozen=True, slots=True)
class DeepInspectionReportV1:
    contract_id: str
    semantic_source_catalog_digest: str
    header_inventory_contract_id: str
    header_inventory_evidence_digest: str
    deep_inspection_method_id: str
    representative_selection_contract_id: str
    lexical_evidence_contract_id: str
    deep_report_digest_contract_id: str
    registered_archive_count: int
    header_inspected_archive_count: int
    attempted_deep_inspections: int
    successful_deep_inspections: int
    partial_deep_inspections: int
    unsupported_deep_inspections: int
    execution_blocker_count: int
    raw_schema_group_count: int
    covered_raw_schema_group_count: int
    blocked_raw_schema_group_count: int
    designated_representative_count: int
    archive_kind_counts: tuple[tuple[str, int], ...]
    sample_rows_interpreted_total: int
    archives_reaching_row_limit: int
    archives_reaching_source_eof: int
    archives_with_parse_error: int
    row_width_matching_total: int
    row_width_shorter_total: int
    row_width_longer_total: int
    lexical_class_totals: LexicalClassCountsV1
    diagnostic_counts: tuple[tuple[str, int], ...]
    source_url_status_counts: tuple[tuple[str, int], ...]
    license_status_counts: tuple[tuple[str, int], ...]
    format_coverage: tuple[FormatCoverageV1, ...]
    inspections: tuple[DeepSourceInspectionV1, ...]
    representatives: tuple[RepresentativeEvidenceV1, ...]
    chronological_coverage_status: str
    source_interpretation_contract_id: None
    scope_statement: str
    max_interpreted_data_rows: int
    max_logical_row_bytes: int
    max_row_fields: int
    max_field_chars: int
    operational_metrics: OperationalMetricsV1

    def evidence_projection_dict(self) -> dict[str, object]:
        """Canonical evidence projection excludes runtime paths and operational timing."""
        return {
            "archive_kind_counts": dict(self.archive_kind_counts),
            "archives_reaching_row_limit": self.archives_reaching_row_limit,
            "archives_reaching_source_eof": self.archives_reaching_source_eof,
            "archives_with_parse_error": self.archives_with_parse_error,
            "attempted_deep_inspections": self.attempted_deep_inspections,
            "blocked_raw_schema_group_count": self.blocked_raw_schema_group_count,
            "covered_raw_schema_group_count": self.covered_raw_schema_group_count,
            "deep_inspection_method_id": self.deep_inspection_method_id,
            "method_definition": {
                "csv_parser_policy": {
                    "delimiter": ",",
                    "doublequote": True,
                    "encoding_policy": DEEP_ENCODING_POLICY,
                    "escapechar": None,
                    "policy_id": DEEP_CSV_POLICY_ID,
                    "quotechar": '"',
                    "sniffer_used": False,
                    "strict": True,
                },
                "lexical_classification_rule": LEXICAL_CLASSIFICATION_RULE,
                "row_failure_rule": ROW_FAILURE_RULE,
                "row_width_evidence_rule": ROW_WIDTH_EVIDENCE_RULE,
                "sample_interpretation": (
                    "deterministic prefix evidence, not a statistical sample of the full archive"
                ),
                "sample_scope": "header plus at most configured data-record prefix",
                "raw_row_values_retained": False,
            },
            "designated_representative_count": self.designated_representative_count,
            "diagnostic_counts": dict(self.diagnostic_counts),
            "format_coverage": [item.to_dict() for item in self.format_coverage],
            "header_inspected_archive_count": self.header_inspected_archive_count,
            "header_inventory_contract_id": self.header_inventory_contract_id,
            "header_inventory_evidence_digest": self.header_inventory_evidence_digest,
            "inspections": [item.to_evidence_dict() for item in self.inspections],
            "lexical_class_totals": self.lexical_class_totals.to_dict(),
            "lexical_evidence_contract_id": self.lexical_evidence_contract_id,
            "license_status_counts": dict(self.license_status_counts),
            "max_field_chars": self.max_field_chars,
            "max_interpreted_data_rows": self.max_interpreted_data_rows,
            "max_logical_row_bytes": self.max_logical_row_bytes,
            "max_row_fields": self.max_row_fields,
            "raw_schema_group_count": self.raw_schema_group_count,
            "registered_archive_count": self.registered_archive_count,
            "representative_selection_contract_id": self.representative_selection_contract_id,
            "representatives": [item.to_dict() for item in self.representatives],
            "row_width_longer_total": self.row_width_longer_total,
            "row_width_matching_total": self.row_width_matching_total,
            "row_width_shorter_total": self.row_width_shorter_total,
            "sample_rows_interpreted_total": self.sample_rows_interpreted_total,
            "scope_statement": self.scope_statement,
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_url_status_counts": dict(self.source_url_status_counts),
            "successful_deep_inspections": self.successful_deep_inspections,
            "unsupported_deep_inspections": self.unsupported_deep_inspections,
            "partial_deep_inspections": self.partial_deep_inspections,
            "execution_blocker_count": self.execution_blocker_count,
            "deep_inspection_report_contract_id": self.contract_id,
            "deep_report_digest_contract_id": self.deep_report_digest_contract_id,
        }

    @property
    def evidence_bytes(self) -> bytes:
        return _canonical_bytes(self.evidence_projection_dict())

    @property
    def evidence_digest(self) -> str:
        return hashlib.sha256(self.evidence_bytes).hexdigest()

    def summary_dict(self) -> dict[str, object]:
        return {
            "archives_reaching_row_limit": self.archives_reaching_row_limit,
            "archives_reaching_source_eof": self.archives_reaching_source_eof,
            "archives_with_parse_error": self.archives_with_parse_error,
            "attempted_deep_inspections": self.attempted_deep_inspections,
            "blocked_raw_schema_group_count": self.blocked_raw_schema_group_count,
            "covered_raw_schema_group_count": self.covered_raw_schema_group_count,
            "deep_inspection_evidence_digest": self.evidence_digest,
            "deep_inspection_method_id": self.deep_inspection_method_id,
            "deep_inspection_report_contract_id": self.contract_id,
            "designated_representative_count": self.designated_representative_count,
            "diagnostic_counts": dict(self.diagnostic_counts),
            "execution_blocker_count": self.execution_blocker_count,
            "header_inspected_archive_count": self.header_inspected_archive_count,
            "header_inventory_contract_id": self.header_inventory_contract_id,
            "header_inventory_evidence_digest": self.header_inventory_evidence_digest,
            "license_status_counts": dict(self.license_status_counts),
            "lexical_evidence_contract_id": self.lexical_evidence_contract_id,
            "raw_schema_group_count": self.raw_schema_group_count,
            "registered_archive_count": self.registered_archive_count,
            "row_width_longer_total": self.row_width_longer_total,
            "row_width_matching_total": self.row_width_matching_total,
            "row_width_shorter_total": self.row_width_shorter_total,
            "sample_rows_interpreted_total": self.sample_rows_interpreted_total,
            "sampling_interpretation": (
                "deterministic bounded prefix; not statistically representative of full archives"
            ),
            "schema_groups": [item.summary_dict() for item in self.representatives],
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_url_status_counts": dict(self.source_url_status_counts),
            "source_kind_counts": dict(self.archive_kind_counts),
            "status_counts": {
                "success": self.successful_deep_inspections,
                "partial": self.partial_deep_inspections,
                "unsupported": self.unsupported_deep_inspections,
            },
            "total_raw_schema_groups": self.raw_schema_group_count,
            "chronological_coverage_status": self.chronological_coverage_status,
            "representative_selection_contract_id": self.representative_selection_contract_id,
            "row_safety_limits": {
                "max_data_rows": self.max_interpreted_data_rows,
                "max_field_chars": self.max_field_chars,
                "max_logical_row_bytes": self.max_logical_row_bytes,
                "max_row_fields": self.max_row_fields,
            },
            "csv_policy_id": DEEP_CSV_POLICY_ID,
            "encoding_policy": DEEP_ENCODING_POLICY,
            "operational_metrics": self.operational_metrics.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "audit": {
                "inspection_metadata": [
                    {
                        "deep_inspection_id": item.deep_inspection_id,
                        "inspection_timestamp_utc": item.inspection_timestamp_utc,
                        "source_archive_id": item.source_archive_id,
                    }
                    for item in self.inspections
                ]
            },
            "evidence": self.evidence_projection_dict(),
            "evidence_digest": self.evidence_digest,
            "operational_metrics": self.operational_metrics.to_dict(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


class _ColumnAccumulator:
    __slots__ = (
        "rows_observed",
        "empty_count",
        "non_empty_count",
        "class_counts",
        "min_chars",
        "max_chars",
        "max_utf8_bytes",
    )

    def __init__(self) -> None:
        self.rows_observed = 0
        self.empty_count = 0
        self.non_empty_count = 0
        self.class_counts = {kind: 0 for kind in LexicalClass}
        self.min_chars: int | None = None
        self.max_chars: int | None = None
        self.max_utf8_bytes: int | None = None

    def observe(self, value: str) -> None:
        self.rows_observed += 1
        lexical_class = classify_lexeme(value)
        self.class_counts[lexical_class] += 1
        if value == "":
            self.empty_count += 1
        else:
            self.non_empty_count += 1
        character_length = len(value)
        utf8_length = len(value.encode("utf-8"))
        if self.min_chars is None or character_length < self.min_chars:
            self.min_chars = character_length
        if self.max_chars is None or character_length > self.max_chars:
            self.max_chars = character_length
        if self.max_utf8_bytes is None or utf8_length > self.max_utf8_bytes:
            self.max_utf8_bytes = utf8_length

    def freeze(self, column_index: int, header_name: str) -> ColumnRowEvidenceV1:
        non_empty_classes = sum(
            self.class_counts[kind] > 0 for kind in LexicalClass if kind is not LexicalClass.EMPTY
        )
        counts = LexicalClassCountsV1(
            empty=self.class_counts[LexicalClass.EMPTY],
            integer_lexeme=self.class_counts[LexicalClass.INTEGER],
            decimal_lexeme=self.class_counts[LexicalClass.DECIMAL],
            boolean_lexeme=self.class_counts[LexicalClass.BOOLEAN],
            other_text=self.class_counts[LexicalClass.OTHER_TEXT],
        )
        return ColumnRowEvidenceV1(
            column_index=column_index,
            exact_header_name=header_name,
            rows_observed=self.rows_observed,
            empty_field_count=self.empty_count,
            non_empty_field_count=self.non_empty_count,
            lexical_class_counts=counts,
            minimum_observed_character_length=self.min_chars,
            maximum_observed_character_length=self.max_chars,
            maximum_observed_utf8_byte_length=self.max_utf8_bytes,
            mixed_lexical_class_observed=non_empty_classes > 1,
        )


def _header_diagnostic_to_row_finding(code: HeaderDiagnosticCode) -> DeepDiagnosticCode:
    if code is HeaderDiagnosticCode.HEADER_TOO_LARGE:
        return DeepDiagnosticCode.DATA_ROW_TOO_LARGE
    if code is HeaderDiagnosticCode.TOO_MANY_HEADER_FIELDS:
        return DeepDiagnosticCode.TOO_MANY_ROW_FIELDS
    if code is HeaderDiagnosticCode.HEADER_FIELD_TOO_LARGE:
        return DeepDiagnosticCode.DATA_FIELD_TOO_LARGE
    if code is HeaderDiagnosticCode.INVALID_ENCODING:
        return DeepDiagnosticCode.INVALID_DATA_ENCODING
    if code is HeaderDiagnosticCode.READ_FAILURE:
        return DeepDiagnosticCode.DATA_READ_FAILURE
    return DeepDiagnosticCode.MALFORMED_DATA_ROW


def _compression_finding(evidence: CompressionValidationEvidenceV1) -> DeepDiagnosticCode:
    if evidence.diagnostic_code is CompressionDiagnosticCode.NOT_CHECKED:
        return DeepDiagnosticCode.COMPRESSION_NOT_CHECKED
    if evidence.diagnostic_code is CompressionDiagnosticCode.READ_FAILURE:
        return DeepDiagnosticCode.COMPRESSION_READ_FAILURE
    if evidence.diagnostic_code is CompressionDiagnosticCode.TRUNCATED_GZIP:
        return DeepDiagnosticCode.TRUNCATED_GZIP
    if evidence.diagnostic_code is CompressionDiagnosticCode.GZIP_INTEGRITY_FAILURE:
        return DeepDiagnosticCode.GZIP_INTEGRITY_FAILURE
    if evidence.diagnostic_code is CompressionDiagnosticCode.INVALID_GZIP_HEADER:
        return DeepDiagnosticCode.INVALID_GZIP
    return DeepDiagnosticCode.COMPRESSION_NOT_VALID


def _inspection_identity(
    record: SourceArchiveRecordV1,
    *,
    header_inspection: SourceInspectionV1 | None,
    status: DeepInspectionStatus,
    termination: SampleTermination,
    max_data_rows: int,
    max_row_bytes: int,
    max_row_fields: int,
    max_field_chars: int,
    rows_interpreted: int,
    rows_matching: int,
    rows_shorter: int,
    rows_longer: int,
    min_width: int | None,
    max_width: int | None,
    max_observed_row_bytes: int | None,
    failed_row_ordinal: int | None,
    columns: tuple[ColumnRowEvidenceV1, ...],
    diagnostics: tuple[DeepFindingV1, ...],
    tool_identity: str | None,
    compression_status: CompressionValidationStatus,
) -> str:
    return _sha256_json(
        {
            "columns": [item.to_dict() for item in columns],
            "compressed_sha256": record.compressed_sha256,
            "compressed_size_bytes": record.compressed_size_bytes,
            "compression_validation_status": compression_status.value,
            "deep_inspection_id_contract_id": DEEP_INSPECTION_ID_CONTRACT_ID,
            "deep_inspection_method_id": DEEP_INSPECTION_METHOD_ID,
            "diagnostics": [item.to_dict() for item in diagnostics],
            "failed_row_ordinal": failed_row_ordinal,
            "header_inspection_id": (
                header_inspection.source_inspection_id if header_inspection is not None else None
            ),
            "limits": {
                "max_data_rows": max_data_rows,
                "max_field_chars": max_field_chars,
                "max_logical_row_bytes": max_row_bytes,
                "max_row_fields": max_row_fields,
            },
            "raw_schema_fingerprint": (
                header_inspection.raw_schema_fingerprint if header_inspection is not None else None
            ),
            "row_evidence": {
                "maximum_observed_row_width": max_width,
                "maximum_observed_row_bytes": max_observed_row_bytes,
                "minimum_observed_row_width": min_width,
                "rows_interpreted": rows_interpreted,
                "rows_longer_than_header": rows_longer,
                "rows_matching_header_width": rows_matching,
                "rows_shorter_than_header": rows_shorter,
                "sample_termination": termination.value,
            },
            "sample_status": status.value,
            "source_archive_id": record.source_archive_id,
            "tool_identity": tool_identity,
        }
    )


def _deep_record(
    record: SourceArchiveRecordV1,
    header_inspection: SourceInspectionV1 | None,
    *,
    compression_status: CompressionValidationStatus,
    status: DeepInspectionStatus,
    termination: SampleTermination,
    max_data_rows: int,
    max_row_bytes: int,
    max_row_fields: int,
    max_field_chars: int,
    rows_interpreted: int = 0,
    rows_matching: int = 0,
    rows_shorter: int = 0,
    rows_longer: int = 0,
    min_width: int | None = None,
    max_width: int | None = None,
    max_observed_row_bytes: int | None = None,
    failed_row_ordinal: int | None = None,
    columns: tuple[ColumnRowEvidenceV1, ...] = (),
    diagnostics: tuple[DeepFindingV1, ...] = (),
    tool_identity: str | None,
    inspection_timestamp_utc: str | None,
) -> DeepSourceInspectionV1:
    filename = record.filename_result
    recognized = filename if isinstance(filename, RecognizedSourceFilename) else None
    metadata = record.source_metadata
    inspection_id = _inspection_identity(
        record,
        header_inspection=header_inspection,
        status=status,
        termination=termination,
        max_data_rows=max_data_rows,
        max_row_bytes=max_row_bytes,
        max_row_fields=max_row_fields,
        max_field_chars=max_field_chars,
        rows_interpreted=rows_interpreted,
        rows_matching=rows_matching,
        rows_shorter=rows_shorter,
        rows_longer=rows_longer,
        min_width=min_width,
        max_width=max_width,
        max_observed_row_bytes=max_observed_row_bytes,
        failed_row_ordinal=failed_row_ordinal,
        columns=columns,
        diagnostics=diagnostics,
        tool_identity=tool_identity,
        compression_status=compression_status,
    )
    return DeepSourceInspectionV1(
        deep_inspection_schema_id=DEEP_INSPECTION_SCHEMA_ID,
        deep_inspection_method_id=DEEP_INSPECTION_METHOD_ID,
        deep_inspection_id_contract_id=DEEP_INSPECTION_ID_CONTRACT_ID,
        deep_inspection_id=inspection_id,
        source_archive_id=record.source_archive_id,
        compressed_sha256=record.compressed_sha256,
        compressed_size_bytes=record.compressed_size_bytes,
        compression_validation_status=compression_status,
        provider=record.provider,
        original_filename=record.original_filename,
        source_kind=recognized.source_kind if recognized is not None else None,
        expansion_token=recognized.expansion_token if recognized is not None else None,
        format_token=recognized.format_token if recognized is not None else None,
        source_url_status=metadata.source_url_status.value,
        source_url=metadata.source_url,
        source_evidence_refs=metadata.source_evidence_refs,
        license_status=metadata.license_status.value,
        license_identifier=metadata.license_identifier,
        license_evidence_refs=metadata.license_evidence_refs,
        m2_header_inspection_id=(
            header_inspection.source_inspection_id if header_inspection is not None else None
        ),
        header_inspection_method_id=HEADER_INSPECTION_METHOD_ID,
        raw_schema_fingerprint=(
            header_inspection.raw_schema_fingerprint if header_inspection is not None else None
        ),
        status=status,
        sample_termination=termination,
        max_interpreted_data_rows=max_data_rows,
        max_logical_row_bytes=max_row_bytes,
        max_row_fields=max_row_fields,
        max_field_chars=max_field_chars,
        rows_interpreted=rows_interpreted,
        rows_matching_header_width=rows_matching,
        rows_shorter_than_header=rows_shorter,
        rows_longer_than_header=rows_longer,
        expected_field_count=(
            header_inspection.header_evidence.field_count
            if header_inspection is not None and header_inspection.header_evidence is not None
            else None
        ),
        minimum_observed_row_width=min_width,
        maximum_observed_row_width=max_width,
        maximum_observed_logical_row_bytes=max_observed_row_bytes,
        failed_row_ordinal=failed_row_ordinal,
        columns=columns,
        diagnostics=diagnostics,
        source_interpretation_contract_id=None,
        tool_identity=tool_identity,
        inspection_timestamp_utc=inspection_timestamp_utc,
    )


def inspect_registered_archive_rows(
    record: SourceArchiveRecordV1,
    header_inspection: SourceInspectionV1,
    compression_evidence: CompressionValidationEvidenceV1,
    *,
    max_data_rows: int = MAX_INTERPRETED_DATA_ROWS,
    max_logical_row_bytes: int = MAX_LOGICAL_ROW_BYTES,
    max_row_fields: int = MAX_ROW_FIELDS,
    max_field_chars: int = MAX_FIELD_CHARS,
    tool_identity: str | None = TOOL_IDENTITY_DEFAULT,
    inspection_timestamp_utc: str | None = None,
) -> DeepSourceInspectionV1:
    """Collect aggregate lexical/width evidence from one bounded data-row prefix."""
    if max_data_rows < 0 or min(max_logical_row_bytes, max_row_fields, max_field_chars) <= 0:
        raise ValueError("deep inspection row limits must be non-negative/positive")
    if (
        header_inspection.source_archive_id != record.source_archive_id
        or header_inspection.compressed_sha256 != record.compressed_sha256
        or header_inspection.compressed_size_bytes != record.compressed_size_bytes
        or _inventory_location_key(header_inspection) != _location_key(record)
    ):
        raise DeepInspectionError("M2.5 header evidence does not belong to this source archive")
    if (
        compression_evidence.source_archive_id != record.source_archive_id
        or compression_evidence.compressed_sha256 != record.compressed_sha256
        or compression_evidence.compressed_size_bytes != record.compressed_size_bytes
        or compression_evidence.raw_root != str(record.raw_root)
        or compression_evidence.relative_path != record.relative_path
    ):
        raise DeepInspectionError("M2.4 compression evidence does not match source registration")

    if compression_evidence.status is not CompressionValidationStatus.VALID:
        return _deep_record(
            record,
            header_inspection,
            compression_status=compression_evidence.status,
            status=DeepInspectionStatus.UNSUPPORTED,
            termination=SampleTermination.COMPRESSION_UNAVAILABLE,
            max_data_rows=max_data_rows,
            max_row_bytes=max_logical_row_bytes,
            max_row_fields=max_row_fields,
            max_field_chars=max_field_chars,
            diagnostics=(
                DeepFindingV1(
                    code=_compression_finding(compression_evidence),
                    row_ordinal=None,
                    reason="M2.4 did not establish a valid complete gzip stream",
                ),
            ),
            tool_identity=tool_identity,
            inspection_timestamp_utc=inspection_timestamp_utc,
        )
    if (
        header_inspection.status is not HeaderInspectionStatus.SUCCESS
        or header_inspection.header_evidence is None
        or header_inspection.raw_schema_fingerprint is None
    ):
        return _deep_record(
            record,
            header_inspection,
            compression_status=compression_evidence.status,
            status=DeepInspectionStatus.UNSUPPORTED,
            termination=SampleTermination.HEADER_UNAVAILABLE,
            max_data_rows=max_data_rows,
            max_row_bytes=max_logical_row_bytes,
            max_row_fields=max_row_fields,
            max_field_chars=max_field_chars,
            diagnostics=(
                DeepFindingV1(
                    code=DeepDiagnosticCode.HEADER_EVIDENCE_UNAVAILABLE,
                    row_ordinal=None,
                    reason="M2.5 did not produce successful header evidence for this source",
                ),
            ),
            tool_identity=tool_identity,
            inspection_timestamp_utc=inspection_timestamp_utc,
        )

    item = InventoryItem(
        basename=record.original_filename,
        raw_root=record.raw_root,
        relative_path=record.relative_path,
        filename_result=record.filename_result,
    )
    expected_header = header_inspection.header_evidence
    diagnostics: list[DeepFindingV1] = []
    accumulators: list[_ColumnAccumulator] = []
    rows_interpreted = 0
    rows_matching = 0
    rows_shorter = 0
    rows_longer = 0
    min_width: int | None = None
    max_width: int | None = None
    max_row_bytes_seen: int | None = None
    failed_row_ordinal: int | None = None
    termination = SampleTermination.ROW_LIMIT_REACHED if max_data_rows == 0 else None
    status = DeepInspectionStatus.SUCCESS

    try:
        with _open_verified_archive(item) as (compressed, initial, opened):
            try:
                with gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed:
                    if not isinstance(record.filename_result, RecognizedSourceFilename):
                        raise DeepInspectionError("deep scan requires recognized source kind")
                    payload = open_csv_source_payload(
                        decompressed,
                        original_filename=record.original_filename,
                        source_kind=record.filename_result.source_kind.value,
                    )
                    lines = _BoundedPhysicalLines(
                        payload.stream,
                        max_logical_row_bytes,
                        max_row_fields,
                    )
                    with csv_field_limit(max_field_chars):
                        reader = csv.reader(
                            lines,
                            delimiter=",",
                            quotechar='"',
                            doublequote=True,
                            escapechar=None,
                            strict=True,
                        )
                        try:
                            parsed_header = next(reader)
                        except (StopIteration, csv.Error, HeaderInspectionError) as exc:
                            raise DeepInspectionError(
                                "registered source no longer parses its M2.5 first CSV record"
                            ) from exc

                        encoding = EncodingObservationV1(
                            encoding_used="utf-8-sig" if lines.bom_present else "utf-8",
                            bom_present=lines.bom_present,
                        )
                        parser_observation = CsvParserObservationV1(
                            quote_character_observed=lines.quote_character_observed,
                            doubled_quote_observed=lines.doubled_quote_observed,
                        )
                        _, actual_fingerprint = fingerprint_raw_schema(
                            tuple(parsed_header),
                            encoding=encoding,
                            parser_observation=parser_observation,
                            physical_line_endings=tuple(lines._line_endings),
                        )
                        if (
                            tuple(parsed_header) != expected_header.ordered_fields
                            or actual_fingerprint != header_inspection.raw_schema_fingerprint
                        ):
                            raise DeepInspectionError(
                                "deep scanner header does not match accepted M2.5 "
                                "raw schema evidence"
                            )

                        expected_width = len(parsed_header)
                        accumulators = [_ColumnAccumulator() for _ in parsed_header]
                        lines.begin_next_record()

                        while rows_interpreted < max_data_rows:
                            row_ordinal = rows_interpreted + 1
                            try:
                                row = next(reader)
                            except StopIteration:
                                termination = SampleTermination.SOURCE_EOF_REACHED
                                break
                            except HeaderInspectionError as exc:
                                code = _header_diagnostic_to_row_finding(exc.code)
                                diagnostics.append(
                                    DeepFindingV1(
                                        code=code,
                                        row_ordinal=row_ordinal,
                                        reason=(
                                            "data record exceeded a configured parser safety limit"
                                        ),
                                    )
                                )
                                failed_row_ordinal = row_ordinal
                                status = DeepInspectionStatus.PARTIAL
                                termination = SampleTermination.ROW_PARSE_ERROR
                                break
                            except csv.Error as exc:
                                code = (
                                    DeepDiagnosticCode.DATA_FIELD_TOO_LARGE
                                    if "field larger than field limit" in str(exc)
                                    else DeepDiagnosticCode.MALFORMED_DATA_ROW
                                )
                                diagnostics.append(
                                    DeepFindingV1(
                                        code=code,
                                        row_ordinal=row_ordinal,
                                        reason=(
                                            "data record is not parseable under the declared "
                                            "strict CSV policy"
                                        ),
                                    )
                                )
                                failed_row_ordinal = row_ordinal
                                status = DeepInspectionStatus.PARTIAL
                                termination = SampleTermination.ROW_PARSE_ERROR
                                break

                            row_bytes = lines._header_bytes
                            if max_row_bytes_seen is None or row_bytes > max_row_bytes_seen:
                                max_row_bytes_seen = row_bytes
                            if len(row) > max_row_fields:
                                diagnostics.append(
                                    DeepFindingV1(
                                        code=DeepDiagnosticCode.TOO_MANY_ROW_FIELDS,
                                        row_ordinal=row_ordinal,
                                        reason=(
                                            "data record exceeds the configured field-count limit"
                                        ),
                                    )
                                )
                                failed_row_ordinal = row_ordinal
                                status = DeepInspectionStatus.PARTIAL
                                termination = SampleTermination.ROW_PARSE_ERROR
                                row.clear()
                                break
                            if len(row) == expected_width:
                                rows_matching += 1
                            elif len(row) < expected_width:
                                rows_shorter += 1
                                diagnostics.append(
                                    DeepFindingV1(
                                        code=DeepDiagnosticCode.ROW_WIDTH_SHORTER,
                                        row_ordinal=row_ordinal,
                                        reason=(
                                            "parsed record has fewer fields than its exact "
                                            "header width"
                                        ),
                                    )
                                )
                            else:
                                rows_longer += 1
                                diagnostics.append(
                                    DeepFindingV1(
                                        code=DeepDiagnosticCode.ROW_WIDTH_LONGER,
                                        row_ordinal=row_ordinal,
                                        reason=(
                                            "parsed record has more fields than its exact "
                                            "header width"
                                        ),
                                    )
                                )
                            min_width = len(row) if min_width is None else min(min_width, len(row))
                            max_width = len(row) if max_width is None else max(max_width, len(row))

                            for column_index, value in enumerate(row):
                                if column_index < expected_width:
                                    accumulators[column_index].observe(value)
                            if row:
                                del value
                            row.clear()
                            rows_interpreted += 1
                            lines.begin_next_record()

                        if termination is None:
                            termination = SampleTermination.ROW_LIMIT_REACHED
                del reader, lines, decompressed
                _verify_opened_archive_unchanged(item, compressed, initial, opened)
            except DeepInspectionError:
                raise
            except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
                raise DeepInspectionError(
                    "M2.4 marked this registered source gzip-valid, but deep reading failed"
                ) from exc
            except OSError as exc:
                raise DeepInspectionError(
                    "cannot read registered source during bounded row inspection: "
                    f"{record.original_filename}"
                ) from exc
    except ArchiveRegistrationError as exc:
        raise DeepInspectionError(
            f"source filesystem identity changed or became unsafe during deep inspection: {exc}"
        ) from exc

    try:
        after = register_archive(item, provider_namespace=record.provider_namespace)
    except ArchiveRegistrationError as exc:
        raise DeepInspectionError(
            f"cannot reverify archive bytes after row inspection: {exc}"
        ) from exc
    if (
        after.source_archive_id != record.source_archive_id
        or after.compressed_sha256 != record.compressed_sha256
        or after.compressed_size_bytes != record.compressed_size_bytes
    ):
        raise DeepInspectionError(
            f"registered bytes changed during deep inspection for {record.original_filename}"
        )

    frozen_columns = tuple(
        accumulator.freeze(index, header_name)
        for index, (accumulator, header_name) in enumerate(
            zip(accumulators, expected_header.ordered_fields, strict=True)
        )
    )
    return _deep_record(
        record,
        header_inspection,
        compression_status=compression_evidence.status,
        status=status,
        termination=termination or SampleTermination.SOURCE_EOF_REACHED,
        max_data_rows=max_data_rows,
        max_row_bytes=max_logical_row_bytes,
        max_row_fields=max_row_fields,
        max_field_chars=max_field_chars,
        rows_interpreted=rows_interpreted,
        rows_matching=rows_matching,
        rows_shorter=rows_shorter,
        rows_longer=rows_longer,
        min_width=min_width,
        max_width=max_width,
        max_observed_row_bytes=max_row_bytes_seen,
        failed_row_ordinal=failed_row_ordinal,
        columns=frozen_columns,
        diagnostics=tuple(diagnostics),
        tool_identity=tool_identity,
        inspection_timestamp_utc=inspection_timestamp_utc,
    )


def _header_inventory_projection_digest(inventory: HeaderInventoryV1) -> str:
    inspections = sorted(
        (
            {
                "compressed_sha256": item.compressed_sha256,
                "compressed_size_bytes": item.compressed_size_bytes,
                "raw_schema_fingerprint": item.raw_schema_fingerprint,
                "source_archive_id": item.source_archive_id,
                "source_inspection_id": item.source_inspection_id,
                "status": item.status.value,
            }
            for item in inventory.inspections
        ),
        key=lambda item: (
            item["source_archive_id"],
            item["raw_schema_fingerprint"] or "",
            item["source_inspection_id"],
        ),
    )
    groups = [
        {
            "archive_count": item.archive_count,
            "expansion_format_counts": [list(entry) for entry in item.expansion_format_counts],
            "expansions": list(item.expansions),
            "field_count": item.field_count,
            "formats": list(item.formats),
            "group_id": item.group_id,
            "raw_schema_fingerprint": item.raw_schema_fingerprint,
            "source_kind": item.source_kind.value if item.source_kind is not None else None,
        }
        for item in inventory.schema_groups
    ]
    projection = {
        "contract_id": inventory.contract_id,
        "groups": groups,
        "inspections": inspections,
        "semantic_source_catalog_digest": inventory.semantic_source_catalog_digest,
        "schema_grouping_contract_id": inventory.schema_grouping_contract_id,
    }
    return _sha256_json(projection)


def _validate_m3_inputs(
    registration: ArchiveRegistrationResult,
    manifest: SourceManifestV1,
    header_inventory: HeaderInventoryV1,
) -> dict[tuple[str, str], tuple[SourceInspectionV1, ...]]:
    if registration.registration_contract_id != ARCHIVE_REGISTRATION_CONTRACT_ID:
        raise DeepInspectionError("unsupported M2.3 archive-registration contract")
    if registration.source_archive_record_schema_id != SOURCE_ARCHIVE_RECORD_SCHEMA_ID:
        raise DeepInspectionError("unsupported SourceArchiveRecordV1 contract")
    if manifest.source_manifest_schema_id != SOURCE_MANIFEST_SCHEMA_ID:
        raise DeepInspectionError("unsupported SourceManifestV1 contract")
    if header_inventory.contract_id != HEADER_INVENTORY_CONTRACT_ID:
        raise DeepInspectionError("unsupported M2.5 HeaderInventoryV1 contract")
    if header_inventory.semantic_source_catalog_digest != manifest.semantic_source_catalog_digest:
        raise DeepInspectionError("M2.5 header inventory belongs to a different source catalog")
    if registration.registered_archive_count != len(manifest.archive_records):
        raise DeepInspectionError("source manifest and registration counts do not reconcile")
    if registration.registered_archive_count != header_inventory.configured_source_count:
        raise DeepInspectionError("header inventory and registration counts do not reconcile")
    if header_inventory.attempted_inspection_count != registration.registered_archive_count:
        raise DeepInspectionError("M2.5 attempted inspection count does not match registration")
    if (
        header_inventory.successful_inspection_count + header_inventory.failed_inspection_count
        != header_inventory.attempted_inspection_count
    ):
        raise DeepInspectionError("M2.5 inspection outcome counts do not reconcile")
    registration_identity = tuple(
        sorted(
            (
                record.source_archive_id,
                record.compressed_sha256,
                record.compressed_size_bytes,
                str(record.raw_root),
                record.relative_path.as_posix(),
            )
            for record in registration.records
        )
    )
    manifest_identity = tuple(
        sorted(
            (
                record.source_archive_id,
                record.compressed_sha256,
                record.compressed_size_bytes,
                str(record.raw_root),
                record.relative_path.as_posix(),
            )
            for record in manifest.archive_records
        )
    )
    if registration_identity != manifest_identity:
        raise DeepInspectionError("source manifest archive records differ from registration")

    inspections_by_location: dict[tuple[str, str], list[SourceInspectionV1]] = {}
    for item in header_inventory.inspections:
        inspections_by_location.setdefault(_inventory_location_key(item), []).append(item)
    if any(len(items) != 1 for items in inspections_by_location.values()):
        raise DeepInspectionError("M2.5 header inventory has duplicate local inspection locators")
    if len(inspections_by_location) != registration.registered_archive_count:
        raise DeepInspectionError("M2.5 header inventory does not account for every registration")
    record_by_location = {_location_key(record): record for record in registration.records}
    if len(record_by_location) != registration.registered_archive_count:
        raise DeepInspectionError("M2.3 registration contains duplicate local archive locators")
    for location, inspections in inspections_by_location.items():
        record = record_by_location.get(location)
        inspection = inspections[0]
        if record is None:
            raise DeepInspectionError("M2.5 contains an inspection outside the registration set")
        if (
            inspection.source_archive_id != record.source_archive_id
            or inspection.compressed_sha256 != record.compressed_sha256
            or inspection.compressed_size_bytes != record.compressed_size_bytes
        ):
            raise DeepInspectionError("M2.5 inspection identity differs from registered bytes")

    # Every group must match the actual M2.5 inspection membership, including count.
    grouped_inspections: dict[tuple[SourceKind | None, str], list[SourceInspectionV1]] = {}
    for inspections in inspections_by_location.values():
        inspection = inspections[0]
        if inspection.header_evidence is None:
            continue
        key = (inspection.source_kind, inspection.header_evidence.raw_schema_fingerprint)
        grouped_inspections.setdefault(key, []).append(inspection)
    seen_group_keys: set[tuple[SourceKind | None, str]] = set()
    for group in header_inventory.schema_groups:
        key = (group.source_kind, group.raw_schema_fingerprint)
        if key in seen_group_keys:
            raise DeepInspectionError("M2.5 schema group key is duplicated")
        seen_group_keys.add(key)
        members = grouped_inspections.get(key, [])
        if len(members) != group.archive_count:
            raise DeepInspectionError("M2.5 schema group count does not match member evidence")
    if seen_group_keys != set(grouped_inspections):
        raise DeepInspectionError("M2.5 contains successful schema evidence absent from its groups")
    return {key: tuple(items) for key, items in inspections_by_location.items()}


def _make_representatives(
    registration: ArchiveRegistrationResult,
    header_inventory: HeaderInventoryV1,
    inspections: tuple[DeepSourceInspectionV1, ...],
) -> tuple[RepresentativeEvidenceV1, ...]:
    records_by_location = {_location_key(record): record for record in registration.records}
    headers_by_location = {
        _inventory_location_key(item): item for item in header_inventory.inspections
    }
    sorted_records_for_evidence = sorted(
        registration.records,
        key=lambda item: (
            item.source_archive_id,
            item.original_filename,
            str(item.raw_root),
            item.relative_path.as_posix(),
        ),
    )
    sorted_inspections_for_evidence = sorted(
        inspections,
        key=lambda item: (
            item.source_archive_id,
            item.original_filename,
            item.deep_inspection_id,
        ),
    )
    deep_by_location = {
        _location_key(record): deep
        for record, deep in zip(
            sorted_records_for_evidence,
            sorted_inspections_for_evidence,
            strict=True,
        )
    }
    records_by_kind_format: dict[tuple[SourceKind, str, str], list[SourceArchiveRecordV1]] = {}
    for record in registration.records:
        parsed = record.filename_result
        if isinstance(parsed, RecognizedSourceFilename):
            records_by_kind_format.setdefault(
                (parsed.source_kind, parsed.expansion_token, parsed.format_token), []
            ).append(record)

    representatives: list[RepresentativeEvidenceV1] = []
    for group in header_inventory.schema_groups:
        members: list[tuple[SourceArchiveRecordV1, SourceInspectionV1]] = []
        for location, inspection in headers_by_location.items():
            if (
                inspection.header_evidence is not None
                and inspection.source_kind is group.source_kind
                and inspection.header_evidence.raw_schema_fingerprint
                == group.raw_schema_fingerprint
            ):
                member_record = records_by_location.get(location)
                if member_record is None:
                    raise DeepInspectionError("header group member disappeared from registration")
                members.append((member_record, inspection))
        if len(members) != group.archive_count or not members:
            raise DeepInspectionError("cannot select representative from incomplete M2.5 group")
        representative_record, representative_header = min(
            members,
            key=lambda pair: (pair[0].source_archive_id, pair[0].original_filename),
        )
        representative_deep = deep_by_location[_location_key(representative_record)]

        counterpart = CounterpartContextV1(
            status="unavailable",
            source_archive_id=None,
            original_filename=None,
            source_kind=None,
            expansion_token=representative_deep.expansion_token,
            format_token=representative_deep.format_token,
            deep_inspection_id=None,
            inspection_status=None,
        )
        if representative_deep.source_kind in {SourceKind.GAME, SourceKind.REPLAY} and (
            representative_deep.expansion_token is not None
            and representative_deep.format_token is not None
        ):
            opposite = (
                SourceKind.REPLAY
                if representative_deep.source_kind is SourceKind.GAME
                else SourceKind.GAME
            )
            counterpart_candidates = records_by_kind_format.get(
                (
                    opposite,
                    representative_deep.expansion_token,
                    representative_deep.format_token,
                ),
                [],
            )
            if counterpart_candidates:
                counterpart_record = min(
                    counterpart_candidates,
                    key=lambda item: (item.source_archive_id, item.original_filename),
                )
                counterpart_deep = deep_by_location[_location_key(counterpart_record)]
                counterpart = CounterpartContextV1(
                    status="available",
                    source_archive_id=counterpart_record.source_archive_id,
                    original_filename=counterpart_record.original_filename,
                    source_kind=counterpart_deep.source_kind,
                    expansion_token=counterpart_deep.expansion_token,
                    format_token=counterpart_deep.format_token,
                    deep_inspection_id=counterpart_deep.deep_inspection_id,
                    inspection_status=counterpart_deep.status,
                )

        evidence = representative_deep.columns
        representatives.append(
            RepresentativeEvidenceV1(
                group_id=group.group_id,
                source_kind=group.source_kind,
                raw_schema_fingerprint=group.raw_schema_fingerprint,
                archive_count=group.archive_count,
                expansion_format_counts=tuple(
                    (entry[0], entry[1], entry[2]) for entry in group.expansion_format_counts
                ),
                representative=representative_deep,
                counterpart=counterpart,
                empty_column_indices=tuple(
                    column.column_index for column in evidence if column.empty_field_count > 0
                ),
                mixed_lexical_column_indices=tuple(
                    column.column_index
                    for column in evidence
                    if column.mixed_lexical_class_observed
                ),
            )
        )
    return tuple(
        sorted(
            representatives,
            key=lambda item: (
                item.source_kind.value if item.source_kind is not None else "",
                item.raw_schema_fingerprint,
            ),
        )
    )


def _format_coverage(
    registration: ArchiveRegistrationResult,
    inspections: tuple[DeepSourceInspectionV1, ...],
) -> tuple[FormatCoverageV1, ...]:
    sorted_records = sorted(
        registration.records,
        key=lambda item: (
            item.source_archive_id,
            item.original_filename,
            str(item.raw_root),
            item.relative_path.as_posix(),
        ),
    )
    sorted_inspections = sorted(
        inspections,
        key=lambda item: (
            item.source_archive_id,
            item.original_filename,
            item.deep_inspection_id,
        ),
    )
    inspections_by_location = {
        _location_key(record): deep
        for record, deep in zip(sorted_records, sorted_inspections, strict=True)
    }
    aggregates: dict[tuple[SourceKind | None, str | None, str | None, str | None], list[Any]] = {}
    for record in registration.records:
        deep = inspections_by_location[_location_key(record)]
        key = (
            deep.source_kind,
            deep.raw_schema_fingerprint,
            deep.expansion_token,
            deep.format_token,
        )
        values = aggregates.setdefault(key, [0, 0, 0, 0, 0])
        values[0] += 1
        values[1] += deep.status is DeepInspectionStatus.SUCCESS
        values[2] += deep.status is DeepInspectionStatus.PARTIAL
        values[3] += deep.status is DeepInspectionStatus.UNSUPPORTED
        values[4] += deep.rows_interpreted
    return tuple(
        FormatCoverageV1(
            source_kind=key[0],
            raw_schema_fingerprint=key[1],
            expansion_token=key[2],
            format_token=key[3],
            archive_count=values[0],
            inspections_successful=values[1],
            inspections_partial=values[2],
            inspections_unsupported=values[3],
            rows_interpreted=values[4],
        )
        for key, values in sorted(
            aggregates.items(),
            key=lambda pair: tuple(value or "" for value in pair[0]),
        )
    )


def build_deep_inspection_report(
    registration: ArchiveRegistrationResult,
    source_manifest: SourceManifestV1,
    header_inventory: HeaderInventoryV1,
    *,
    max_data_rows: int = MAX_INTERPRETED_DATA_ROWS,
    max_logical_row_bytes: int = MAX_LOGICAL_ROW_BYTES,
    max_row_fields: int = MAX_ROW_FIELDS,
    max_field_chars: int = MAX_FIELD_CHARS,
    tool_identity: str | None = TOOL_IDENTITY_DEFAULT,
    inspection_timestamp_utc: str | None = None,
) -> DeepInspectionReportV1:
    """Run bounded row-evidence collection for every registered local archive."""
    if max_data_rows < 0 or min(max_logical_row_bytes, max_row_fields, max_field_chars) <= 0:
        raise ValueError("deep inspection row limits must be non-negative/positive")
    header_by_location = _validate_m3_inputs(registration, source_manifest, header_inventory)
    compression_by_location = {
        (str(item.raw_root), item.relative_path.as_posix()): item
        for item in source_manifest.compression_evidence
    }
    if len(compression_by_location) != registration.registered_archive_count:
        raise DeepInspectionError("M2.4 compression evidence does not account for every source")

    ordered_records = tuple(
        sorted(
            registration.records,
            key=lambda item: (
                item.source_archive_id,
                str(item.raw_root),
                item.relative_path.as_posix(),
            ),
        )
    )
    started = time.perf_counter()
    inspections: list[DeepSourceInspectionV1] = []
    for record in ordered_records:
        location = _location_key(record)
        header = header_by_location[location][0]
        compression = compression_by_location.get(location)
        if compression is None:
            raise DeepInspectionError("missing M2.4 compression evidence for registered source")
        inspections.append(
            inspect_registered_archive_rows(
                record,
                header,
                compression,
                max_data_rows=max_data_rows,
                max_logical_row_bytes=max_logical_row_bytes,
                max_row_fields=max_row_fields,
                max_field_chars=max_field_chars,
                tool_identity=tool_identity,
                inspection_timestamp_utc=inspection_timestamp_utc,
            )
        )
    ordered_inspections = tuple(
        sorted(
            inspections,
            key=lambda item: (
                item.source_archive_id,
                item.original_filename,
                item.deep_inspection_id,
            ),
        )
    )
    elapsed = max(time.perf_counter() - started, 1e-9)

    if len(ordered_inspections) != registration.registered_archive_count:
        raise DeepInspectionError("deep inspection archive accounting failed")
    if len({(_location_key(record)) for record in ordered_records}) != len(ordered_inspections):
        raise DeepInspectionError("deep inspection local-source accounting is ambiguous")

    deep_by_location = {
        _location_key(record): deep
        for record, deep in zip(ordered_records, inspections, strict=True)
    }
    successful = sum(item.status is DeepInspectionStatus.SUCCESS for item in inspections)
    partial = sum(item.status is DeepInspectionStatus.PARTIAL for item in inspections)
    unsupported = sum(item.status is DeepInspectionStatus.UNSUPPORTED for item in inspections)
    group_success_count = 0
    for group in header_inventory.schema_groups:
        group_members: list[SourceInspectionV1] = []
        for record in ordered_records:
            header = header_by_location[_location_key(record)][0]
            if (
                header.header_evidence is not None
                and header.source_kind is group.source_kind
                and header.header_evidence.raw_schema_fingerprint == group.raw_schema_fingerprint
            ):
                group_members.append(header)
        if len(group_members) != group.archive_count:
            raise DeepInspectionError("raw schema group membership no longer reconciles")
        if any(
            deep_by_location[_inventory_location_key(header)].status is DeepInspectionStatus.SUCCESS
            for header in group_members
        ):
            group_success_count += 1

    representative_evidence = _make_representatives(
        registration,
        header_inventory,
        ordered_inspections,
    )
    if len(representative_evidence) != len(header_inventory.schema_groups):
        raise DeepInspectionError("not every M2.5 schema group has one designated representative")

    archive_kind_counts: Counter[str] = Counter()
    source_url_counts: Counter[str] = Counter()
    license_counts: Counter[str] = Counter()
    for record in registration.records:
        kind = record.source_kind.value if record.source_kind is not None else "unknown"
        archive_kind_counts[kind] += 1
        source_url_counts[record.source_metadata.source_url_status.value] += 1
        license_counts[record.source_metadata.license_status.value] += 1
    lexical_totals = {kind: 0 for kind in LexicalClass}
    diagnostic_counts: Counter[str] = Counter()
    rows_interpreted_total = 0
    rows_matching_total = 0
    rows_shorter_total = 0
    rows_longer_total = 0
    row_limit_count = 0
    eof_count = 0
    parse_error_count = 0
    for inspection in inspections:
        rows_interpreted_total += inspection.rows_interpreted
        rows_matching_total += inspection.rows_matching_header_width
        rows_shorter_total += inspection.rows_shorter_than_header
        rows_longer_total += inspection.rows_longer_than_header
        row_limit_count += inspection.sample_termination is SampleTermination.ROW_LIMIT_REACHED
        eof_count += inspection.sample_termination is SampleTermination.SOURCE_EOF_REACHED
        parse_error_count += inspection.sample_termination is SampleTermination.ROW_PARSE_ERROR
        for finding in inspection.diagnostics:
            diagnostic_counts[finding.code.value] += 1
        for column in inspection.columns:
            counts = column.lexical_class_counts
            lexical_totals[LexicalClass.EMPTY] += counts.empty
            lexical_totals[LexicalClass.INTEGER] += counts.integer_lexeme
            lexical_totals[LexicalClass.DECIMAL] += counts.decimal_lexeme
            lexical_totals[LexicalClass.BOOLEAN] += counts.boolean_lexeme
            lexical_totals[LexicalClass.OTHER_TEXT] += counts.other_text

    metrics = OperationalMetricsV1(
        wall_clock_seconds=elapsed,
        archives_per_second=len(inspections) / elapsed,
        interpreted_rows_per_second=rows_interpreted_total / elapsed,
    )
    covered_groups = group_success_count
    report = DeepInspectionReportV1(
        contract_id=DEEP_INSPECTION_REPORT_CONTRACT_ID,
        semantic_source_catalog_digest=source_manifest.semantic_source_catalog_digest,
        header_inventory_contract_id=header_inventory.contract_id,
        header_inventory_evidence_digest=_header_inventory_projection_digest(header_inventory),
        deep_inspection_method_id=DEEP_INSPECTION_METHOD_ID,
        representative_selection_contract_id=REPRESENTATIVE_SELECTION_CONTRACT_ID,
        lexical_evidence_contract_id=LEXICAL_EVIDENCE_CONTRACT_ID,
        deep_report_digest_contract_id=DEEP_REPORT_DIGEST_CONTRACT_ID,
        registered_archive_count=registration.registered_archive_count,
        header_inspected_archive_count=header_inventory.successful_inspection_count,
        attempted_deep_inspections=len(inspections),
        successful_deep_inspections=successful,
        partial_deep_inspections=partial,
        unsupported_deep_inspections=unsupported,
        execution_blocker_count=0,
        raw_schema_group_count=len(header_inventory.schema_groups),
        covered_raw_schema_group_count=covered_groups,
        blocked_raw_schema_group_count=len(header_inventory.schema_groups) - covered_groups,
        designated_representative_count=len(representative_evidence),
        archive_kind_counts=tuple(
            sorted(
                (
                    {kind.value: archive_kind_counts[kind.value] for kind in SourceKind}
                    | {"unknown": archive_kind_counts["unknown"]}
                ).items()
            )
        ),
        sample_rows_interpreted_total=rows_interpreted_total,
        archives_reaching_row_limit=row_limit_count,
        archives_reaching_source_eof=eof_count,
        archives_with_parse_error=parse_error_count,
        row_width_matching_total=rows_matching_total,
        row_width_shorter_total=rows_shorter_total,
        row_width_longer_total=rows_longer_total,
        lexical_class_totals=LexicalClassCountsV1(
            empty=lexical_totals[LexicalClass.EMPTY],
            integer_lexeme=lexical_totals[LexicalClass.INTEGER],
            decimal_lexeme=lexical_totals[LexicalClass.DECIMAL],
            boolean_lexeme=lexical_totals[LexicalClass.BOOLEAN],
            other_text=lexical_totals[LexicalClass.OTHER_TEXT],
        ),
        diagnostic_counts=tuple(sorted(diagnostic_counts.items())),
        source_url_status_counts=tuple(sorted(source_url_counts.items())),
        license_status_counts=tuple(sorted(license_counts.items())),
        format_coverage=_format_coverage(registration, ordered_inspections),
        inspections=ordered_inspections,
        representatives=representative_evidence,
        chronological_coverage_status="unavailable_not_established",
        source_interpretation_contract_id=None,
        scope_statement=(
            "Complete for the registered sources in semantic source catalog "
            f"{source_manifest.semantic_source_catalog_digest}; configured-root scope only, "
            "with deterministic bounded prefix row evidence and no claim about global "
            "17Lands coverage."
        ),
        max_interpreted_data_rows=max_data_rows,
        max_logical_row_bytes=max_logical_row_bytes,
        max_row_fields=max_row_fields,
        max_field_chars=max_field_chars,
        operational_metrics=metrics,
    )
    return report


def write_report_no_overwrite(
    report: DeepInspectionReportV1,
    output_path: str | Path,
    config: RuntimeConfig,
    *,
    base_dir: str | Path,
) -> Path:
    """Atomically create a detailed JSON report under intermediate_root only."""
    candidate = Path(output_path)
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate
    target = candidate.resolve(strict=False)
    intermediate = config.intermediate_root.resolve(strict=False)
    if not target.is_relative_to(intermediate):
        raise DeepInspectionError("detailed report output must be under intermediate_root")
    if any(target == root or target.is_relative_to(root) for root in config.raw_roots):
        raise DeepInspectionError("detailed report output aliases a configured raw root")
    if target.exists() and target.is_dir():
        raise DeepInspectionError("detailed report output path is a directory")

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    if not resolved_parent.is_relative_to(intermediate):
        raise DeepInspectionError("report parent escaped intermediate_root during creation")
    target = resolved_parent / target.name
    if any(target == root or target.is_relative_to(root) for root in config.raw_roots):
        raise DeepInspectionError("resolved report output aliases a configured raw root")
    if os.path.lexists(target):
        raise DeepInspectionError("detailed report output already exists; refusing overwrite")

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".openmtgdata-incomplete-",
            suffix=".tmp",
            dir=resolved_parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(report.canonical_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link creation is atomic and fails if target already exists on supported filesystems.
        os.link(temporary_path, target)
    except FileExistsError as exc:
        raise DeepInspectionError(
            "detailed report output appeared concurrently; refusing overwrite"
        ) from exc
    except OSError as exc:
        raise DeepInspectionError("could not safely create detailed inspection report") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
    return target
