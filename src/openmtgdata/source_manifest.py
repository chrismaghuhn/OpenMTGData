"""Versioned source catalog with separate audit and semantic projections."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from openmtgdata.archive_registration import (
    ARCHIVE_REGISTRATION_CONTRACT_ID,
    SOURCE_ARCHIVE_ID_CONTRACT_ID,
    SOURCE_ARCHIVE_RECORD_SCHEMA_ID,
    ArchiveRegistrationResult,
    LicenseReviewStatus,
    MetadataAvailability,
    SourceArchiveRecordV1,
)
from openmtgdata.compression_validation import (
    COMPRESSION_VALIDATION_CONTRACT_ID,
    CompressionDiagnosticCode,
    CompressionValidationEvidenceV1,
    CompressionValidationStatus,
    not_checked_compression_evidence,
)
from openmtgdata.inventory import CandidateDisposition
from openmtgdata.source_filename import (
    FILENAME_CONTRACT_ID,
    RecognizedSourceFilename,
    SourceKind,
    UnrecognizedSourceFilename,
)

SOURCE_MANIFEST_SCHEMA_ID = "openmtgdata.source-manifest.v1"
AUDIT_CATALOG_SCHEMA_ID = "openmtgdata.source-audit-catalog.v1"
SEMANTIC_SOURCE_PROJECTION_SCHEMA_ID = "openmtgdata.semantic-source-set.v1"
SEMANTIC_SOURCE_CATALOG_DIGEST_CONTRACT_ID = "openmtgdata.semantic-source-catalog-digest.v1"
SOURCE_SELECTION_CONTRACT_ID = "openmtgdata.source-selection.v1"
SOURCE_PUBLICATION_ELIGIBILITY_CONTRACT_ID = "openmtgdata.source-publication-eligibility.v1"
SOURCE_MANIFEST_SCOPE_STATEMENT = (
    "Catalog of the supplied M2.3 registrations only; "
    "no claim is made about global 17Lands coverage."
)
SOURCE_SELECTION_RULE = (
    "Semantic v1 includes exact source_url only when supplied; generic source_evidence_refs "
    "remain audit-only until a typed evidence-identity contract exists. License status and "
    "identifier participate, license evidence refs do not; local acquisition/ingestion/tool "
    "metadata never participates."
)


class SourceManifestError(RuntimeError):
    """Base class for invalid SourceManifestV1 construction or projection."""


class SourceManifestInputError(SourceManifestError):
    """Raised when source registrations/evidence do not reconcile."""


class SourceMetadataConflictError(SourceManifestError):
    """Raised when same-byte registrations have conflicting semantic metadata."""


class ManifestDiagnosticCode(StrEnum):
    """Stable audit-catalog diagnostics for source evidence gaps/findings."""

    UNRECOGNIZED_FILENAME = "UNRECOGNIZED_FILENAME"
    SOURCE_URL_EVIDENCE_UNKNOWN = "SOURCE_URL_EVIDENCE_UNKNOWN"
    DATASET_FAMILY_UNKNOWN = "DATASET_FAMILY_UNKNOWN"
    LICENSE_UNKNOWN = "LICENSE_UNKNOWN"
    LICENSE_PENDING_REVIEW = "LICENSE_PENDING_REVIEW"
    LICENSE_INCOMPATIBLE = "LICENSE_INCOMPATIBLE"
    ACQUISITION_METADATA_UNKNOWN = "ACQUISITION_METADATA_UNKNOWN"
    INGESTION_AUDIT_METADATA_UNKNOWN = "INGESTION_AUDIT_METADATA_UNKNOWN"
    COMPRESSION_NOT_CHECKED = "COMPRESSION_NOT_CHECKED"
    COMPRESSION_INVALID = "COMPRESSION_INVALID"
    COMPRESSION_UNREADABLE = "COMPRESSION_UNREADABLE"
    COMPRESSION_EVIDENCE_CONFLICT = "COMPRESSION_EVIDENCE_CONFLICT"
    DUPLICATE_LOCAL_REGISTRATION = "DUPLICATE_LOCAL_REGISTRATION"


class PublicationEligibilityStatus(StrEnum):
    """Source-level eligibility only; not a final dataset release result."""

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


class PublicationBlockCode(StrEnum):
    """Stable source-level publication gate findings."""

    EMPTY_SOURCE_SET = "EMPTY_SOURCE_SET"
    UNRECOGNIZED_FILENAME = "UNRECOGNIZED_FILENAME"
    SOURCE_URL_EVIDENCE_UNKNOWN = "SOURCE_URL_EVIDENCE_UNKNOWN"
    LICENSE_UNKNOWN = "LICENSE_UNKNOWN"
    LICENSE_PENDING_REVIEW = "LICENSE_PENDING_REVIEW"
    LICENSE_INCOMPATIBLE = "LICENSE_INCOMPATIBLE"
    COMPRESSION_NOT_CHECKED = "COMPRESSION_NOT_CHECKED"
    COMPRESSION_INVALID = "COMPRESSION_INVALID"
    COMPRESSION_UNREADABLE = "COMPRESSION_UNREADABLE"
    COMPRESSION_EVIDENCE_CONFLICT = "COMPRESSION_EVIDENCE_CONFLICT"


@dataclass(frozen=True, slots=True)
class SourceManifestAuditMetadataV1:
    """Optional report/build audit values; no timestamp or tool is generated."""

    report_generated_at_utc: str | None = None
    builder_identity: str | None = None
    tool_identity: str | None = None

    def __post_init__(self) -> None:
        if any(
            value == ""
            for value in (
                self.report_generated_at_utc,
                self.builder_identity,
                self.tool_identity,
            )
        ):
            raise ValueError("manifest audit metadata values must be non-empty when supplied")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "builder_identity": self.builder_identity,
            "report_generated_at_utc": self.report_generated_at_utc,
            "tool_identity": self.tool_identity,
        }


@dataclass(frozen=True, slots=True)
class SemanticSourceArchiveV1:
    """One unique provider byte identity in the v1 semantic source set."""

    source_archive_id: str
    provider: str
    provider_namespace: str
    filename_disposition: CandidateDisposition
    source_kind: SourceKind | None
    expansion_token: str | None
    format_token: str | None
    dataset_family_status: MetadataAvailability
    dataset_family: str | None
    source_url_status: MetadataAvailability
    source_url: str | None
    license_status: LicenseReviewStatus
    license_identifier: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_family": self.dataset_family,
            "dataset_family_status": self.dataset_family_status.value,
            "expansion_token": self.expansion_token,
            "filename_disposition": self.filename_disposition.value,
            "format_token": self.format_token,
            "license_identifier": self.license_identifier,
            "license_status": self.license_status.value,
            "provider": self.provider,
            "provider_namespace": self.provider_namespace,
            "source_archive_id": self.source_archive_id,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
            "source_url": self.source_url,
            "source_url_status": self.source_url_status.value,
        }


@dataclass(frozen=True, slots=True)
class ManifestDiagnosticV1:
    """Deterministic audit finding, optionally anchored to a runtime-local record."""

    code: ManifestDiagnosticCode
    reason: str
    source_archive_id: str | None = None
    detail_code: str | None = None
    raw_root: Path | None = None
    relative_path: PurePosixPath | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "detail_code": self.detail_code,
            "path_scope": "runtime_local" if self.raw_root is not None else None,
            "raw_root": str(self.raw_root) if self.raw_root is not None else None,
            "reason": self.reason,
            "relative_path": self.relative_path.as_posix()
            if self.relative_path is not None
            else None,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class PublicationEligibilityFindingV1:
    """A source-level publication blocker; not a command-execution error."""

    code: PublicationBlockCode
    source_archive_id: str | None
    reason: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code.value,
            "reason": self.reason,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class SourcePublicationEligibilityV1:
    """Versioned source evidence gate result, distinct from final release validation."""

    contract_id: str
    status: PublicationEligibilityStatus
    semantic_source_count: int
    eligible_source_count: int
    blocked_source_count: int
    findings: tuple[PublicationEligibilityFindingV1, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "blocked_source_count": self.blocked_source_count,
            "contract_id": self.contract_id,
            "eligible_source_count": self.eligible_source_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "semantic_source_count": self.semantic_source_count,
            "status": self.status.value,
        }


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path))


def _record_sort_key(record: SourceArchiveRecordV1) -> tuple[str, str, str, str]:
    return (
        record.source_archive_id,
        _path_key(record.raw_root),
        record.relative_path.as_posix(),
        record.original_filename,
    )


def _evidence_sort_key(
    evidence: CompressionValidationEvidenceV1,
) -> tuple[str, str, str, str]:
    return (
        evidence.source_archive_id,
        os.path.normcase(evidence.raw_root),
        evidence.relative_path.as_posix(),
        evidence.status.value,
    )


def _metadata_conflict(source_archive_id: str, field_name: str) -> SourceMetadataConflictError:
    return SourceMetadataConflictError(
        f"same source_archive_id has conflicting semantic {field_name}: {source_archive_id}"
    )


def _merge_filename_metadata(
    records: tuple[SourceArchiveRecordV1, ...],
) -> tuple[CandidateDisposition, SourceKind | None, str | None, str | None]:
    recognized = {
        (
            record.filename_result.source_kind,
            record.filename_result.expansion_token,
            record.filename_result.format_token,
        )
        for record in records
        if isinstance(record.filename_result, RecognizedSourceFilename)
    }
    if len(recognized) > 1:
        raise _metadata_conflict(records[0].source_archive_id, "filename classification")
    if recognized:
        source_kind, expansion_token, format_token = next(iter(recognized))
        return CandidateDisposition.RECOGNIZED, source_kind, expansion_token, format_token
    return CandidateDisposition.UNRECOGNIZED, None, None, None


def _merge_source_url_metadata(
    records: tuple[SourceArchiveRecordV1, ...],
) -> tuple[MetadataAvailability, str | None]:
    supplied = {
        record.source_metadata.source_url
        for record in records
        if record.source_metadata.source_url is not None
    }
    if len(supplied) > 1:
        raise _metadata_conflict(records[0].source_archive_id, "source URL/evidence")
    if supplied:
        return MetadataAvailability.PROVIDED, next(iter(supplied))
    return MetadataAvailability.UNKNOWN, None


def _merge_license_metadata(
    records: tuple[SourceArchiveRecordV1, ...],
) -> tuple[LicenseReviewStatus, str | None]:
    known = {
        (record.source_metadata.license_status, record.source_metadata.license_identifier)
        for record in records
        if record.source_metadata.license_status is not LicenseReviewStatus.UNKNOWN
    }
    if len(known) > 1:
        raise _metadata_conflict(records[0].source_archive_id, "license status/identifier")
    if known:
        return next(iter(known))
    return LicenseReviewStatus.UNKNOWN, None


def _semantic_archives(
    records: tuple[SourceArchiveRecordV1, ...],
) -> tuple[SemanticSourceArchiveV1, ...]:
    grouped: dict[str, list[SourceArchiveRecordV1]] = defaultdict(list)
    for record in records:
        grouped[record.source_archive_id].append(record)

    semantic: list[SemanticSourceArchiveV1] = []
    for source_archive_id, local_records in grouped.items():
        providers = {(record.provider, record.provider_namespace) for record in local_records}
        byte_identities = {
            (record.compressed_sha256, record.compressed_size_bytes) for record in local_records
        }
        if len(providers) != 1 or len(byte_identities) != 1:
            raise _metadata_conflict(source_archive_id, "provider or compressed-byte identity")

        filename_disposition, source_kind, expansion_token, format_token = _merge_filename_metadata(
            tuple(local_records)
        )
        source_url_status, source_url = _merge_source_url_metadata(tuple(local_records))
        license_status, license_identifier = _merge_license_metadata(tuple(local_records))
        provider, provider_namespace = next(iter(providers))
        semantic.append(
            SemanticSourceArchiveV1(
                source_archive_id=source_archive_id,
                provider=provider,
                provider_namespace=provider_namespace,
                filename_disposition=filename_disposition,
                source_kind=source_kind,
                expansion_token=expansion_token,
                format_token=format_token,
                dataset_family_status=MetadataAvailability.UNKNOWN,
                dataset_family=None,
                source_url_status=source_url_status,
                source_url=source_url,
                license_status=license_status,
                license_identifier=license_identifier,
            )
        )
    return tuple(sorted(semantic, key=lambda entry: entry.source_archive_id))


def _registration_location_key(
    record: SourceArchiveRecordV1,
) -> tuple[str, str, int, str, str]:
    return (
        record.source_archive_id,
        record.compressed_sha256,
        record.compressed_size_bytes,
        _path_key(record.raw_root),
        record.relative_path.as_posix(),
    )


def _evidence_location_key(
    evidence: CompressionValidationEvidenceV1,
) -> tuple[str, str, int, str, str]:
    return (
        evidence.source_archive_id,
        evidence.compressed_sha256,
        evidence.compressed_size_bytes,
        os.path.normcase(evidence.raw_root),
        evidence.relative_path.as_posix(),
    )


def _record_diagnostic(
    record: SourceArchiveRecordV1,
    *,
    code: ManifestDiagnosticCode,
    reason: str,
    detail_code: str | None = None,
) -> ManifestDiagnosticV1:
    return ManifestDiagnosticV1(
        code=code,
        reason=reason,
        source_archive_id=record.source_archive_id,
        detail_code=detail_code,
        raw_root=record.raw_root,
        relative_path=record.relative_path,
    )


@dataclass(frozen=True, slots=True)
class SourceManifestV1:
    """Audit catalog and independent semantic source-set projection."""

    archive_records: tuple[SourceArchiveRecordV1, ...]
    compression_evidence: tuple[CompressionValidationEvidenceV1, ...]
    inspection_refs: tuple[str, ...] = ()
    audit_metadata: SourceManifestAuditMetadataV1 = SourceManifestAuditMetadataV1()
    source_manifest_schema_id: str = SOURCE_MANIFEST_SCHEMA_ID
    audit_catalog_schema_id: str = AUDIT_CATALOG_SCHEMA_ID
    semantic_projection_schema_id: str = SEMANTIC_SOURCE_PROJECTION_SCHEMA_ID
    semantic_source_catalog_digest_contract_id: str = SEMANTIC_SOURCE_CATALOG_DIGEST_CONTRACT_ID
    source_selection_contract_id: str = SOURCE_SELECTION_CONTRACT_ID
    source_archive_record_schema_id: str = SOURCE_ARCHIVE_RECORD_SCHEMA_ID
    source_archive_id_contract_id: str = SOURCE_ARCHIVE_ID_CONTRACT_ID
    filename_contract_id: str = FILENAME_CONTRACT_ID
    compression_validation_contract_id: str = COMPRESSION_VALIDATION_CONTRACT_ID
    source_publication_eligibility_contract_id: str = SOURCE_PUBLICATION_ELIGIBILITY_CONTRACT_ID

    def __post_init__(self) -> None:
        if not isinstance(self.archive_records, tuple) or not all(
            isinstance(record, SourceArchiveRecordV1) for record in self.archive_records
        ):
            raise ValueError("archive_records must be an immutable tuple of SourceArchiveRecordV1")
        if not isinstance(self.compression_evidence, tuple) or not all(
            isinstance(evidence, CompressionValidationEvidenceV1)
            for evidence in self.compression_evidence
        ):
            raise ValueError("compression_evidence must be an immutable typed tuple")
        if not isinstance(self.inspection_refs, tuple) or not all(
            isinstance(reference, str) and reference for reference in self.inspection_refs
        ):
            raise ValueError("inspection_refs must be an immutable tuple of non-empty references")
        if len(set(self.inspection_refs)) != len(self.inspection_refs):
            raise ValueError("inspection_refs must not contain duplicate references")
        if not isinstance(self.audit_metadata, SourceManifestAuditMetadataV1):
            raise ValueError("audit_metadata must be SourceManifestAuditMetadataV1")

        expected = Counter(_registration_location_key(record) for record in self.archive_records)
        actual = Counter(_evidence_location_key(item) for item in self.compression_evidence)
        if expected != actual:
            raise ValueError(
                "each registration requires exactly one matching compression evidence record"
            )
        # Validate per-source metadata consensus before exposing a complete manifest.
        _semantic_archives(self.archive_records)

    @property
    def ordered_archive_records(self) -> tuple[SourceArchiveRecordV1, ...]:
        return tuple(sorted(self.archive_records, key=_record_sort_key))

    @property
    def ordered_compression_evidence(self) -> tuple[CompressionValidationEvidenceV1, ...]:
        return tuple(sorted(self.compression_evidence, key=_evidence_sort_key))

    @property
    def semantic_archives(self) -> tuple[SemanticSourceArchiveV1, ...]:
        return _semantic_archives(self.ordered_archive_records)

    def semantic_projection_dict(self) -> dict[str, object]:
        """Return the explicit, local-path-free v1 semantic source-set projection."""
        return {
            "archives": [archive.to_dict() for archive in self.semantic_archives],
            "filename_contract_id": self.filename_contract_id,
            "semantic_projection_schema_id": self.semantic_projection_schema_id,
            "semantic_source_catalog_digest_contract_id": (
                self.semantic_source_catalog_digest_contract_id
            ),
            "source_archive_id_contract_id": self.source_archive_id_contract_id,
            "source_archive_record_schema_id": self.source_archive_record_schema_id,
            "source_manifest_schema_id": self.source_manifest_schema_id,
            "source_selection_contract_id": self.source_selection_contract_id,
        }

    @property
    def semantic_projection_bytes(self) -> bytes:
        """Canonical compact UTF-8 JSON for the semantic source-set projection."""
        return json.dumps(
            self.semantic_projection_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def semantic_source_catalog_digest(self) -> str:
        """SHA-256 of canonical semantic projection bytes, independent of audit data."""
        return hashlib.sha256(self.semantic_projection_bytes).hexdigest()

    @property
    def publication_eligibility(self) -> SourcePublicationEligibilityV1:
        return _source_publication_eligibility(self)

    @property
    def audit_diagnostics(self) -> tuple[ManifestDiagnosticV1, ...]:
        return _audit_diagnostics(self)

    def audit_catalog_dict(self) -> dict[str, object]:
        """Return the separate full-fidelity audit catalog projection."""
        records = self.ordered_archive_records
        semantic_archives = self.semantic_archives
        compression_status_counts = {
            status.value: sum(item.status is status for item in self.compression_evidence)
            for status in CompressionValidationStatus
        }
        license_status_counts = {
            status.value: sum(record.source_metadata.license_status is status for record in records)
            for status in LicenseReviewStatus
        }
        source_url_status_counts = {
            status.value: sum(
                record.source_metadata.source_url_status is status for record in records
            )
            for status in MetadataAvailability
        }
        filename_counts = {
            "recognized": sum(record.filename_disposition == "recognized" for record in records),
            "unrecognized": sum(
                record.filename_disposition == "unrecognized" for record in records
            ),
        }
        source_kind_counts = {kind.value: 0 for kind in SourceKind}
        source_kind_counts["unknown"] = 0
        for record in records:
            if record.source_kind is None:
                source_kind_counts["unknown"] += 1
            else:
                source_kind_counts[record.source_kind.value] += 1

        duplicate_counts = Counter(record.source_archive_id for record in records)
        duplicate_group_count = sum(count > 1 for count in duplicate_counts.values())
        return {
            "archive_registration_contract_id": ARCHIVE_REGISTRATION_CONTRACT_ID,
            "archive_records": [record.to_dict() for record in records],
            "audit_catalog_schema_id": self.audit_catalog_schema_id,
            "audit_metadata": self.audit_metadata.to_dict(),
            "audit_archive_registration_count": len(records),
            "byte_duplicate_group_count": duplicate_group_count,
            "duplicate_local_registration_count": len(records) - len(semantic_archives),
            "compression_validation_evidence": [
                evidence.to_dict() for evidence in self.ordered_compression_evidence
            ],
            "compression_evidence_count": len(self.compression_evidence),
            "compression_status_counts": compression_status_counts,
            "compression_validation_contract_id": self.compression_validation_contract_id,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.audit_diagnostics],
            "filename_disposition_counts": filename_counts,
            "inspection_refs": sorted(self.inspection_refs),
            "license_status_counts": license_status_counts,
            "path_scope": "runtime_local",
            "semantic_source_count": len(semantic_archives),
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "semantic_projection_schema_id": self.semantic_projection_schema_id,
            "semantic_source_catalog_digest_contract_id": (
                self.semantic_source_catalog_digest_contract_id
            ),
            "source_archive_id_contract_id": self.source_archive_id_contract_id,
            "source_archive_record_schema_id": self.source_archive_record_schema_id,
            "source_publication_eligibility_contract_id": (
                self.source_publication_eligibility_contract_id
            ),
            "source_manifest_schema_id": self.source_manifest_schema_id,
            "source_publication_eligibility": self.publication_eligibility.to_dict(),
            "source_selection_contract_id": self.source_selection_contract_id,
            "source_selection_rule": SOURCE_SELECTION_RULE,
            "source_url_status_counts": source_url_status_counts,
            "source_kind_counts": source_kind_counts,
            "scope_statement": SOURCE_MANIFEST_SCOPE_STATEMENT,
            "total_compressed_bytes": sum(record.compressed_size_bytes for record in records),
        }

    @property
    def audit_catalog_bytes(self) -> bytes:
        """Canonical compact UTF-8 JSON for the audit catalog only."""
        return json.dumps(
            self.audit_catalog_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def to_dict(self) -> dict[str, object]:
        """Serialize audit and semantic projections as explicitly separate objects."""
        return {
            "audit_catalog": self.audit_catalog_dict(),
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "semantic_source_set": self.semantic_projection_dict(),
            "source_publication_eligibility": self.publication_eligibility.to_dict(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Serialize the complete report; use the named projections for their identities."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def _compression_status_by_source(
    manifest: SourceManifestV1,
) -> dict[str, tuple[CompressionValidationStatus, CompressionDiagnosticCode | None, bool]]:
    evidence_by_source: dict[str, list[CompressionValidationEvidenceV1]] = defaultdict(list)
    for item in manifest.compression_evidence:
        evidence_by_source[item.source_archive_id].append(item)

    statuses: dict[
        str,
        tuple[CompressionValidationStatus, CompressionDiagnosticCode | None, bool],
    ] = {}
    for source_archive_id, evidence_items in evidence_by_source.items():
        conclusive = {
            item.status
            for item in evidence_items
            if item.status is not CompressionValidationStatus.NOT_CHECKED
        }
        if len(conclusive) > 1:
            statuses[source_archive_id] = (
                CompressionValidationStatus.UNREADABLE,
                CompressionDiagnosticCode.READ_FAILURE,
                True,
            )
        elif CompressionValidationStatus.VALID in conclusive:
            statuses[source_archive_id] = (CompressionValidationStatus.VALID, None, False)
        elif CompressionValidationStatus.INVALID in conclusive:
            first_invalid = next(
                item
                for item in evidence_items
                if item.status is CompressionValidationStatus.INVALID
            )
            statuses[source_archive_id] = (
                CompressionValidationStatus.INVALID,
                first_invalid.diagnostic_code,
                False,
            )
        elif CompressionValidationStatus.UNREADABLE in conclusive:
            statuses[source_archive_id] = (
                CompressionValidationStatus.UNREADABLE,
                CompressionDiagnosticCode.READ_FAILURE,
                False,
            )
        else:
            statuses[source_archive_id] = (
                CompressionValidationStatus.NOT_CHECKED,
                CompressionDiagnosticCode.NOT_CHECKED,
                False,
            )
    return statuses


def _source_publication_eligibility(
    manifest: SourceManifestV1,
) -> SourcePublicationEligibilityV1:
    semantic_archives = manifest.semantic_archives
    compression_by_source = _compression_status_by_source(manifest)
    findings: list[PublicationEligibilityFindingV1] = []
    eligible_count = 0

    if not semantic_archives:
        findings.append(
            PublicationEligibilityFindingV1(
                code=PublicationBlockCode.EMPTY_SOURCE_SET,
                source_archive_id=None,
                reason="source catalog contains no registered archives",
            )
        )

    for archive in semantic_archives:
        archive_findings: list[PublicationEligibilityFindingV1] = []
        if archive.filename_disposition is CandidateDisposition.UNRECOGNIZED:
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=PublicationBlockCode.UNRECOGNIZED_FILENAME,
                    source_archive_id=archive.source_archive_id,
                    reason="source filename does not establish a recognized public-dump family",
                )
            )
        if archive.source_url_status is MetadataAvailability.UNKNOWN:
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=PublicationBlockCode.SOURCE_URL_EVIDENCE_UNKNOWN,
                    source_archive_id=archive.source_archive_id,
                    reason="source URL/evidence has not been supplied for publication review",
                )
            )
        if archive.license_status is LicenseReviewStatus.UNKNOWN:
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=PublicationBlockCode.LICENSE_UNKNOWN,
                    source_archive_id=archive.source_archive_id,
                    reason="per-source license status is unknown",
                )
            )
        elif archive.license_status is LicenseReviewStatus.PENDING_REVIEW:
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=PublicationBlockCode.LICENSE_PENDING_REVIEW,
                    source_archive_id=archive.source_archive_id,
                    reason="per-source license review is pending",
                )
            )
        elif archive.license_status is LicenseReviewStatus.INCOMPATIBLE:
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=PublicationBlockCode.LICENSE_INCOMPATIBLE,
                    source_archive_id=archive.source_archive_id,
                    reason="per-source license status is incompatible with publication",
                )
            )

        compression_status, compression_code, conflicting_evidence = compression_by_source[
            archive.source_archive_id
        ]
        if conflicting_evidence:
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=PublicationBlockCode.COMPRESSION_EVIDENCE_CONFLICT,
                    source_archive_id=archive.source_archive_id,
                    reason=(
                        "local copies with the same byte identity have conflicting gzip evidence"
                    ),
                )
            )
        elif compression_status is not CompressionValidationStatus.VALID:
            code = {
                CompressionValidationStatus.NOT_CHECKED: (
                    PublicationBlockCode.COMPRESSION_NOT_CHECKED
                ),
                CompressionValidationStatus.INVALID: PublicationBlockCode.COMPRESSION_INVALID,
                CompressionValidationStatus.UNREADABLE: PublicationBlockCode.COMPRESSION_UNREADABLE,
                CompressionValidationStatus.VALID: PublicationBlockCode.COMPRESSION_NOT_CHECKED,
            }[compression_status]
            archive_findings.append(
                PublicationEligibilityFindingV1(
                    code=code,
                    source_archive_id=archive.source_archive_id,
                    reason=(
                        f"compression status is {compression_status.value}"
                        + (f" ({compression_code.value})" if compression_code else "")
                    ),
                )
            )

        if archive_findings:
            findings.extend(archive_findings)
        else:
            eligible_count += 1

    ordered_findings = tuple(
        sorted(findings, key=lambda item: (item.source_archive_id or "", item.code.value))
    )
    blocked_count = len(semantic_archives) - eligible_count
    if not semantic_archives:
        blocked_count = 0
    return SourcePublicationEligibilityV1(
        contract_id=SOURCE_PUBLICATION_ELIGIBILITY_CONTRACT_ID,
        status=(
            PublicationEligibilityStatus.ELIGIBLE
            if semantic_archives and not ordered_findings
            else PublicationEligibilityStatus.BLOCKED
        ),
        semantic_source_count=len(semantic_archives),
        eligible_source_count=eligible_count,
        blocked_source_count=blocked_count,
        findings=ordered_findings,
    )


def _audit_diagnostics(manifest: SourceManifestV1) -> tuple[ManifestDiagnosticV1, ...]:
    diagnostics: list[ManifestDiagnosticV1] = []
    for record in manifest.ordered_archive_records:
        if record.filename_disposition == "unrecognized":
            parser_result = record.filename_result
            detail = (
                parser_result.diagnostic_code.value
                if isinstance(parser_result, UnrecognizedSourceFilename)
                else None
            )
            diagnostics.append(
                _record_diagnostic(
                    record,
                    code=ManifestDiagnosticCode.UNRECOGNIZED_FILENAME,
                    detail_code=detail,
                    reason=(
                        "candidate basename is cataloged but is not a recognized canonical filename"
                    ),
                )
            )
        if record.source_metadata.source_url_status is MetadataAvailability.UNKNOWN:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    code=ManifestDiagnosticCode.SOURCE_URL_EVIDENCE_UNKNOWN,
                    reason="source URL/evidence is not present in the immutable registration",
                )
            )
        diagnostics.append(
            _record_diagnostic(
                record,
                code=ManifestDiagnosticCode.DATASET_FAMILY_UNKNOWN,
                reason="dataset-family identity has not been independently established",
            )
        )
        license_status = record.source_metadata.license_status
        if license_status is LicenseReviewStatus.UNKNOWN:
            license_code = ManifestDiagnosticCode.LICENSE_UNKNOWN
            license_reason = "per-source license status has not been reviewed"
        elif license_status is LicenseReviewStatus.PENDING_REVIEW:
            license_code = ManifestDiagnosticCode.LICENSE_PENDING_REVIEW
            license_reason = "per-source license review is pending"
        elif license_status is LicenseReviewStatus.INCOMPATIBLE:
            license_code = ManifestDiagnosticCode.LICENSE_INCOMPATIBLE
            license_reason = "per-source license status is marked incompatible"
        else:
            license_code = None
            license_reason = None
        if license_code is not None and license_reason is not None:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    code=license_code,
                    reason=license_reason,
                )
            )
        if record.acquisition_metadata.status is MetadataAvailability.UNKNOWN:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    code=ManifestDiagnosticCode.ACQUISITION_METADATA_UNKNOWN,
                    reason="acquisition audit metadata was not supplied",
                )
            )
        if record.ingestion_audit_metadata.status is MetadataAvailability.UNKNOWN:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    code=ManifestDiagnosticCode.INGESTION_AUDIT_METADATA_UNKNOWN,
                    reason="ingestion audit metadata was not supplied",
                )
            )

    for evidence in manifest.ordered_compression_evidence:
        if evidence.status is CompressionValidationStatus.VALID:
            continue
        code = {
            CompressionValidationStatus.NOT_CHECKED: ManifestDiagnosticCode.COMPRESSION_NOT_CHECKED,
            CompressionValidationStatus.INVALID: ManifestDiagnosticCode.COMPRESSION_INVALID,
            CompressionValidationStatus.UNREADABLE: ManifestDiagnosticCode.COMPRESSION_UNREADABLE,
            CompressionValidationStatus.VALID: ManifestDiagnosticCode.COMPRESSION_NOT_CHECKED,
        }[evidence.status]
        diagnostics.append(
            ManifestDiagnosticV1(
                code=code,
                source_archive_id=evidence.source_archive_id,
                detail_code=evidence.diagnostic_code.value if evidence.diagnostic_code else None,
                reason=evidence.reason or f"compression status is {evidence.status.value}",
                raw_root=Path(evidence.raw_root),
                relative_path=evidence.relative_path,
            )
        )

    for source_archive_id, (_status, _code, conflict) in _compression_status_by_source(
        manifest
    ).items():
        if conflict:
            diagnostics.append(
                ManifestDiagnosticV1(
                    code=ManifestDiagnosticCode.COMPRESSION_EVIDENCE_CONFLICT,
                    source_archive_id=source_archive_id,
                    reason=(
                        "local copies with one source_archive_id have conflicting "
                        "compression evidence"
                    ),
                )
            )

    record_groups: dict[str, list[SourceArchiveRecordV1]] = defaultdict(list)
    for record in manifest.ordered_archive_records:
        record_groups[record.source_archive_id].append(record)
    for source_archive_id, records in record_groups.items():
        if len(records) > 1:
            diagnostics.append(
                ManifestDiagnosticV1(
                    code=ManifestDiagnosticCode.DUPLICATE_LOCAL_REGISTRATION,
                    source_archive_id=source_archive_id,
                    detail_code=str(len(records)),
                    reason="multiple local registrations reference the same source_archive_id",
                )
            )

    return tuple(
        sorted(
            diagnostics,
            key=lambda item: (
                item.source_archive_id or "",
                item.code.value,
                _path_key(item.raw_root) if item.raw_root is not None else "",
                item.relative_path.as_posix() if item.relative_path is not None else "",
                item.detail_code or "",
            ),
        )
    )


def build_source_manifest(
    registration: ArchiveRegistrationResult,
    *,
    compression_evidence: tuple[CompressionValidationEvidenceV1, ...] | None = None,
    inspection_refs: tuple[str, ...] = (),
    audit_metadata: SourceManifestAuditMetadataV1 | None = None,
) -> SourceManifestV1:
    """Build a versioned source catalog from immutable M2.3 registrations."""
    if registration.registration_contract_id != ARCHIVE_REGISTRATION_CONTRACT_ID:
        raise SourceManifestInputError("unsupported archive registration contract")
    if registration.source_archive_record_schema_id != SOURCE_ARCHIVE_RECORD_SCHEMA_ID:
        raise SourceManifestInputError("unsupported SourceArchiveRecordV1 schema")

    records = tuple(sorted(registration.records, key=_record_sort_key))
    evidence = compression_evidence
    if evidence is None:
        evidence = tuple(not_checked_compression_evidence(record) for record in records)
    ordered_evidence = tuple(sorted(evidence, key=_evidence_sort_key))

    try:
        return SourceManifestV1(
            archive_records=records,
            compression_evidence=ordered_evidence,
            inspection_refs=tuple(sorted(set(inspection_refs))),
            audit_metadata=audit_metadata or SourceManifestAuditMetadataV1(),
        )
    except ValueError as exc:
        raise SourceManifestInputError(f"invalid SourceManifestV1 inputs: {exc}") from exc
