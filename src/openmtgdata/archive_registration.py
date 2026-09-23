"""Immutable identity registration for exact compressed archive bytes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from openmtgdata.inventory import (
    INVENTORY_CONTRACT_ID,
    InventoryItem,
    InventoryResult,
)
from openmtgdata.source_filename import (
    FILENAME_CONTRACT_ID,
    FilenameParseResult,
    RecognizedSourceFilename,
    SourceKind,
    UnrecognizedSourceFilename,
)

PROVIDER_NAMESPACE = "17lands.public-datasets"
PROVIDER = "17lands"
SOURCE_ARCHIVE_RECORD_SCHEMA_ID = "openmtgdata.source-archive-record.v1"
SOURCE_ARCHIVE_ID_CONTRACT_ID = "openmtgdata.source-archive-id.v1"
ARCHIVE_REGISTRATION_CONTRACT_ID = "openmtgdata.archive-registration.v1"
HASH_CHUNK_SIZE_BYTES = 4 * 1024 * 1024
_CANDIDATE_SUFFIX = ".csv.gz"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class ArchiveRegistrationError(RuntimeError):
    """Base class for failures registering a candidate's exact bytes."""


class RegistrationConfigurationError(ArchiveRegistrationError):
    """Raised for invalid registration options or inventory inputs."""


class ArchiveFileDisappearedError(ArchiveRegistrationError):
    """Raised when a candidate or required path component disappears."""


class ArchiveNotRegularFileError(ArchiveRegistrationError):
    """Raised when a candidate is no longer a regular file."""


class ArchiveSymlinkError(ArchiveRegistrationError):
    """Raised when a candidate path contains an observable symlink/reparse alias."""


class ArchivePathEscapeError(ArchiveRegistrationError):
    """Raised when a candidate path resolves outside its recorded raw root."""


class ArchiveReadError(ArchiveRegistrationError):
    """Raised when metadata or binary reads fail."""


class ArchiveChangedDuringRegistrationError(ArchiveRegistrationError):
    """Raised when filesystem identity/metadata changes during registration."""


class ArchiveByteCountMismatchError(ArchiveRegistrationError):
    """Raised when bytes read do not match the stable reported file size."""


class RegistrationExecutionError(ArchiveRegistrationError):
    """Raised when batch registration cannot produce a complete result."""


class RegistrationStatus(StrEnum):
    """Status of a successfully registered immutable source archive record."""

    REGISTERED = "registered"


class MetadataAvailability(StrEnum):
    """Whether optional registration/audit metadata was explicitly supplied."""

    UNKNOWN = "unknown"
    PROVIDED = "provided"


class LicenseReviewStatus(StrEnum):
    """Review state of per-source license metadata; no license is assumed."""

    UNKNOWN = "unknown"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class SourceArchiveMetadataV1:
    """Source URL/evidence and license metadata, explicitly unknown by default."""

    source_url_status: MetadataAvailability = MetadataAvailability.UNKNOWN
    source_url: str | None = None
    source_evidence_refs: tuple[str, ...] = ()
    license_status: LicenseReviewStatus = LicenseReviewStatus.UNKNOWN
    license_identifier: str | None = None
    license_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_url_status, MetadataAvailability):
            raise ValueError("source_url_status must be MetadataAvailability")
        if not isinstance(self.license_status, LicenseReviewStatus):
            raise ValueError("license_status must be LicenseReviewStatus")
        if not isinstance(self.source_evidence_refs, tuple) or not isinstance(
            self.license_evidence_refs, tuple
        ):
            raise ValueError("evidence references must be immutable tuples")
        if not all(isinstance(value, str) for value in self.source_evidence_refs):
            raise ValueError("source evidence references must be strings")
        if not all(isinstance(value, str) for value in self.license_evidence_refs):
            raise ValueError("license evidence references must be strings")
        if any(not value for value in self.source_evidence_refs + self.license_evidence_refs):
            raise ValueError("evidence references must be non-empty strings")
        source_values_present = self.source_url is not None or bool(self.source_evidence_refs)
        if self.source_url_status is MetadataAvailability.UNKNOWN and source_values_present:
            raise ValueError("unknown source URL status cannot carry source URL/evidence values")
        if self.source_url_status is MetadataAvailability.PROVIDED and not source_values_present:
            raise ValueError("provided source URL status requires a URL or evidence reference")
        if self.source_url == "":
            raise ValueError("source_url must be non-empty when provided")

        license_values_present = self.license_identifier is not None or bool(
            self.license_evidence_refs
        )
        if self.license_status is LicenseReviewStatus.UNKNOWN and license_values_present:
            raise ValueError("unknown license status cannot carry license identifier/evidence")
        if self.license_status in {
            LicenseReviewStatus.VERIFIED,
            LicenseReviewStatus.INCOMPATIBLE,
        } and (not self.license_identifier or not self.license_evidence_refs):
            raise ValueError(
                "verified/incompatible license status requires identifier and evidence"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "license_evidence_refs": list(self.license_evidence_refs),
            "license_identifier": self.license_identifier,
            "license_status": self.license_status.value,
            "source_evidence_refs": list(self.source_evidence_refs),
            "source_url": self.source_url,
            "source_url_status": self.source_url_status.value,
        }


@dataclass(frozen=True, slots=True)
class AcquisitionMetadataV1:
    """Optional acquisition facts; local registration invents none."""

    status: MetadataAvailability = MetadataAvailability.UNKNOWN
    acquired_from: str | None = None
    acquired_at_utc: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MetadataAvailability):
            raise ValueError("acquisition status must be MetadataAvailability")
        if self.status is MetadataAvailability.UNKNOWN and (
            self.acquired_from is not None or self.acquired_at_utc is not None
        ):
            raise ValueError("unknown acquisition status cannot carry acquisition values")
        if self.status is MetadataAvailability.PROVIDED and (
            self.acquired_from is None and self.acquired_at_utc is None
        ):
            raise ValueError("provided acquisition status requires an explicit value")
        if self.acquired_from == "" or self.acquired_at_utc == "":
            raise ValueError("acquisition metadata values must be non-empty when provided")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "acquired_at_utc": self.acquired_at_utc,
            "acquired_from": self.acquired_from,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class IngestionAuditMetadataV1:
    """Optional ingestion/tool audit facts, with no generated timestamp by default."""

    status: MetadataAvailability = MetadataAvailability.UNKNOWN
    ingested_at_utc: str | None = None
    registration_tool_identity: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MetadataAvailability):
            raise ValueError("ingestion status must be MetadataAvailability")
        if self.status is MetadataAvailability.UNKNOWN and (
            self.ingested_at_utc is not None or self.registration_tool_identity is not None
        ):
            raise ValueError("unknown ingestion status cannot carry ingestion audit values")
        if self.status is MetadataAvailability.PROVIDED and (
            self.ingested_at_utc is None and self.registration_tool_identity is None
        ):
            raise ValueError("provided ingestion status requires an explicit value")
        if self.ingested_at_utc == "" or self.registration_tool_identity == "":
            raise ValueError("ingestion audit values must be non-empty when provided")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "ingested_at_utc": self.ingested_at_utc,
            "registration_tool_identity": self.registration_tool_identity,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class SourceArchiveRecordV1:
    """Portable compressed-byte identity plus separate local path provenance."""

    source_archive_record_schema_id: str
    provider: str
    provider_namespace: str
    source_archive_id_contract_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    original_filename: str
    filename_contract_id: str
    inventory_contract_id: str
    filename_result: FilenameParseResult
    source_metadata: SourceArchiveMetadataV1
    acquisition_metadata: AcquisitionMetadataV1
    ingestion_audit_metadata: IngestionAuditMetadataV1
    raw_root: Path
    relative_path: PurePosixPath
    registration_status: RegistrationStatus

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.compressed_sha256):
            raise ValueError("compressed_sha256 must be 64 lowercase hexadecimal characters")
        if not _SHA256_RE.fullmatch(self.source_archive_id):
            raise ValueError("source_archive_id must be 64 lowercase hexadecimal characters")
        if self.compressed_size_bytes < 0:
            raise ValueError("compressed_size_bytes must be non-negative")
        if self.filename_result.original_basename != self.original_filename:
            raise ValueError("filename result basename must match original_filename")
        if self.provider != PROVIDER:
            raise ValueError("unsupported source provider")
        if self.provider_namespace == "":
            raise ValueError("provider_namespace must be non-empty")
        if not isinstance(self.source_metadata, SourceArchiveMetadataV1):
            raise ValueError("source_metadata must be SourceArchiveMetadataV1")
        if not isinstance(self.acquisition_metadata, AcquisitionMetadataV1):
            raise ValueError("acquisition_metadata must be AcquisitionMetadataV1")
        if not isinstance(self.ingestion_audit_metadata, IngestionAuditMetadataV1):
            raise ValueError("ingestion_audit_metadata must be IngestionAuditMetadataV1")
        if self.filename_contract_id != self.filename_result.contract_id:
            raise ValueError("filename_contract_id disagrees with filename result")
        if self.source_archive_id != derive_source_archive_id(
            self.compressed_sha256,
            provider_namespace=self.provider_namespace,
        ):
            raise ValueError("source_archive_id disagrees with its derivation contract")
        if self.source_archive_record_schema_id != SOURCE_ARCHIVE_RECORD_SCHEMA_ID:
            raise ValueError("unsupported source archive record schema")
        if self.source_archive_id_contract_id != SOURCE_ARCHIVE_ID_CONTRACT_ID:
            raise ValueError("unsupported source archive ID contract")
        if self.inventory_contract_id != INVENTORY_CONTRACT_ID:
            raise ValueError("unsupported inventory contract")

    @property
    def filename_disposition(self) -> str:
        if isinstance(self.filename_result, RecognizedSourceFilename):
            return "recognized"
        return "unrecognized"

    @property
    def source_kind(self) -> SourceKind | None:
        if isinstance(self.filename_result, RecognizedSourceFilename):
            return self.filename_result.source_kind
        return None

    @property
    def expansion_token(self) -> str | None:
        if isinstance(self.filename_result, RecognizedSourceFilename):
            return self.filename_result.expansion_token
        return None

    @property
    def format_token(self) -> str | None:
        if isinstance(self.filename_result, RecognizedSourceFilename):
            return self.filename_result.format_token
        return None

    @property
    def portable_projection(self) -> dict[str, object]:
        """Return portable record semantics without runtime-local paths."""
        return {
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "filename_classification": _filename_result_to_dict(self.filename_result),
            "filename_contract_id": self.filename_contract_id,
            "inventory_contract_id": self.inventory_contract_id,
            "license_evidence_refs": list(self.source_metadata.license_evidence_refs),
            "license_identifier": self.source_metadata.license_identifier,
            "license_status": self.source_metadata.license_status.value,
            "original_filename": self.original_filename,
            "provider": self.provider,
            "provider_namespace": self.provider_namespace,
            "registration_status": self.registration_status.value,
            "source_archive_record_schema_id": self.source_archive_record_schema_id,
            "source_archive_id": self.source_archive_id,
            "source_archive_id_contract_id": self.source_archive_id_contract_id,
            "source_evidence_refs": list(self.source_metadata.source_evidence_refs),
            "source_url": self.source_metadata.source_url,
            "source_url_status": self.source_metadata.source_url_status.value,
        }

    @property
    def license_status(self) -> LicenseReviewStatus:
        return self.source_metadata.license_status

    @property
    def source_url(self) -> str | None:
        return self.source_metadata.source_url

    @property
    def source_url_status(self) -> MetadataAvailability:
        return self.source_metadata.source_url_status

    @property
    def source_evidence_refs(self) -> tuple[str, ...]:
        return self.source_metadata.source_evidence_refs

    @property
    def license_identifier(self) -> str | None:
        return self.source_metadata.license_identifier

    @property
    def license_evidence_refs(self) -> tuple[str, ...]:
        return self.source_metadata.license_evidence_refs

    @property
    def audit_projection(self) -> dict[str, dict[str, str | None]]:
        """Return optional acquisition/ingestion audit fields, without inventing values."""
        return {
            "acquisition_metadata": self.acquisition_metadata.to_dict(),
            "ingestion_audit_metadata": self.ingestion_audit_metadata.to_dict(),
        }

    @property
    def runtime_local_projection(self) -> dict[str, str]:
        """Return local path provenance, explicitly scoped to this runtime."""
        return {
            "path_scope": "runtime_local",
            "raw_root": str(self.raw_root),
            "relative_path": self.relative_path.as_posix(),
        }

    def to_dict(self) -> dict[str, object]:
        """Serialize portable record data separately from local path provenance."""
        return {
            "audit_metadata": self.audit_projection,
            "portable_semantic": self.portable_projection,
            "runtime_local": self.runtime_local_projection,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return compact, sorted-key UTF-8 JSON for the full record serialization."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class SourceArchiveLocationV1:
    """Runtime-local inventory location kept for duplicate-content diagnostics."""

    raw_root: Path
    relative_path: PurePosixPath

    def to_dict(self) -> dict[str, str]:
        return {
            "path_scope": "runtime_local",
            "raw_root": str(self.raw_root),
            "relative_path": self.relative_path.as_posix(),
        }


@dataclass(frozen=True, slots=True)
class DuplicateSourceArchiveGroup:
    """Multiple local registrations that share one provider byte identity."""

    source_archive_id: str
    record_count: int
    locations: tuple[SourceArchiveLocationV1, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "locations": [location.to_dict() for location in self.locations],
            "record_count": self.record_count,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class ArchiveRegistrationResult:
    """Complete deterministic result for registering one inventory."""

    registration_contract_id: str
    source_archive_record_schema_id: str
    source_archive_id_contract_id: str
    provider_namespace: str
    inventory_contract_id: str
    records: tuple[SourceArchiveRecordV1, ...]
    total_compressed_bytes: int
    duplicate_content_groups: tuple[DuplicateSourceArchiveGroup, ...]

    @property
    def registered_archive_count(self) -> int:
        return len(self.records)

    @property
    def unique_source_archive_id_count(self) -> int:
        return len({record.source_archive_id for record in self.records})

    @property
    def duplicate_content_group_count(self) -> int:
        return len(self.duplicate_content_groups)

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_content_group_count": self.duplicate_content_group_count,
            "duplicate_content_groups": [
                group.to_dict() for group in self.duplicate_content_groups
            ],
            "inventory_contract_id": self.inventory_contract_id,
            "provider_namespace": self.provider_namespace,
            "records": [record.to_dict() for record in self.records],
            "registered_archive_count": self.registered_archive_count,
            "registration_contract_id": self.registration_contract_id,
            "source_archive_record_schema_id": self.source_archive_record_schema_id,
            "source_archive_id_contract_id": self.source_archive_id_contract_id,
            "total_compressed_bytes": self.total_compressed_bytes,
            "unique_source_archive_id_count": self.unique_source_archive_id_count,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return compact, sorted-key UTF-8 JSON for the complete batch result."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class _StatSnapshot:
    device: int
    inode: int
    file_type: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> _StatSnapshot:
        return cls(
            device=info.st_dev,
            inode=info.st_ino,
            file_type=stat.S_IFMT(info.st_mode),
            size=info.st_size,
            mtime_ns=getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
            ctime_ns=getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)),
        )


def derive_source_archive_id(
    compressed_sha256: str,
    *,
    provider_namespace: str = PROVIDER_NAMESPACE,
) -> str:
    """Derive a portable v1 ID from only provider namespace and byte SHA-256."""
    if not _SHA256_RE.fullmatch(compressed_sha256):
        raise RegistrationConfigurationError(
            "compressed_sha256 must be 64 lowercase hexadecimal characters"
        )
    if not provider_namespace:
        raise RegistrationConfigurationError("provider_namespace must be non-empty")
    identity_projection = {
        "compressed_sha256": compressed_sha256,
        "contract_id": SOURCE_ARCHIVE_ID_CONTRACT_ID,
        "provider_namespace": provider_namespace,
    }
    canonical_bytes = json.dumps(
        identity_projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def _path_is_within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return os.name == "nt" and bool(attributes & _WINDOWS_REPARSE_POINT)


def _lstat(path: Path, *, role: str) -> os.stat_result:
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ArchiveFileDisappearedError(f"{role} disappeared: {path}") from exc
    except OSError as exc:
        raise ArchiveReadError(f"cannot inspect {role}: {path}") from exc


def _resolve(path: Path, *, role: str) -> Path:
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ArchiveFileDisappearedError(f"{role} disappeared: {path}") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArchiveReadError(f"cannot resolve {role}: {path}") from exc


def _validate_raw_root(raw_root: Path) -> Path:
    if not raw_root.is_absolute():
        raise RegistrationConfigurationError("inventory raw root must be canonical and absolute")
    root_info = _lstat(raw_root, role="recorded raw root")
    if stat.S_ISLNK(root_info.st_mode) or _is_reparse_point(root_info):
        raise ArchiveSymlinkError(f"recorded raw root became a symlink/reparse point: {raw_root}")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ArchiveNotRegularFileError(f"recorded raw root is no longer a directory: {raw_root}")
    resolved_root = _resolve(raw_root, role="recorded raw root")
    if resolved_root != raw_root:
        raise ArchivePathEscapeError(
            f"recorded raw root no longer resolves to its canonical path: {raw_root}"
        )
    return resolved_root


def _validate_relative_path(item: InventoryItem) -> tuple[str, ...]:
    relative = item.relative_path
    if relative.is_absolute() or not relative.parts:
        raise RegistrationConfigurationError("inventory item must have a non-empty relative path")
    if any(part in ("", ".", "..") for part in relative.parts):
        raise RegistrationConfigurationError("inventory relative path contains unsafe components")
    if relative.parts[-1] != item.basename:
        raise RegistrationConfigurationError("inventory basename disagrees with relative path")
    if item.filename_result.original_basename != item.basename:
        raise RegistrationConfigurationError("filename classification disagrees with basename")
    if item.filename_result.contract_id != FILENAME_CONTRACT_ID:
        raise RegistrationConfigurationError("unsupported filename-contract identity")
    if not item.basename.endswith(_CANDIDATE_SUFFIX):
        raise RegistrationConfigurationError("inventory item is not a `.csv.gz` candidate")
    return relative.parts


def _validate_path(item: InventoryItem) -> tuple[Path, os.stat_result]:
    root = _validate_raw_root(item.raw_root)
    parts = _validate_relative_path(item)
    current = root

    for component in parts[:-1]:
        current = current / component
        component_info = _lstat(current, role="raw-root path component")
        if stat.S_ISLNK(component_info.st_mode) or _is_reparse_point(component_info):
            raise ArchiveSymlinkError(f"raw-root path contains a symlink/reparse alias: {current}")
        if not stat.S_ISDIR(component_info.st_mode):
            raise ArchiveNotRegularFileError(
                f"raw-root path component is no longer a directory: {current}"
            )
        resolved_component = _resolve(current, role="raw-root path component")
        if not _path_is_within(root, resolved_component):
            raise ArchivePathEscapeError(f"path component escaped the recorded raw root: {current}")
        if resolved_component != current:
            raise ArchiveSymlinkError(
                f"raw-root path component resolves through an alias: {current}"
            )

    candidate_path = current / parts[-1]
    candidate_info = _lstat(candidate_path, role="candidate archive")
    if stat.S_ISLNK(candidate_info.st_mode) or _is_reparse_point(candidate_info):
        raise ArchiveSymlinkError(f"candidate became a symlink/reparse point: {candidate_path}")
    if not stat.S_ISREG(candidate_info.st_mode):
        raise ArchiveNotRegularFileError(f"candidate is no longer a regular file: {candidate_path}")

    resolved_candidate = _resolve(candidate_path, role="candidate archive")
    if not _path_is_within(root, resolved_candidate):
        raise ArchivePathEscapeError(
            f"candidate resolves outside its recorded raw root: {candidate_path}"
        )
    if resolved_candidate != candidate_path:
        raise ArchiveSymlinkError(f"candidate path resolves through an alias: {candidate_path}")
    return candidate_path, candidate_info


def _snapshot(info: os.stat_result) -> _StatSnapshot:
    snapshot = _StatSnapshot.from_stat(info)
    if snapshot.size < 0:
        raise ArchiveReadError("filesystem reported a negative candidate file size")
    return snapshot


def _ensure_unchanged(before: _StatSnapshot, after_info: os.stat_result, *, context: str) -> None:
    after = _snapshot(after_info)
    if after != before:
        raise ArchiveChangedDuringRegistrationError(
            f"candidate filesystem identity/metadata changed {context}"
        )


def _ensure_same_entry(before: _StatSnapshot, after_info: os.stat_result, *, context: str) -> None:
    after = _snapshot(after_info)
    stable_identity_before = (before.device, before.inode, before.file_type, before.size)
    stable_identity_after = (after.device, after.inode, after.file_type, after.size)
    if stable_identity_after != stable_identity_before:
        raise ArchiveChangedDuringRegistrationError(
            f"candidate file identity/size changed {context}"
        )


@contextmanager
def _open_verified_archive(
    item: InventoryItem,
) -> Iterator[tuple[BinaryIO, _StatSnapshot, _StatSnapshot]]:
    path, before_info = _validate_path(item)
    before = _snapshot(before_info)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ArchiveFileDisappearedError(f"candidate disappeared before open: {path}") from exc
    except OSError as exc:
        raise ArchiveReadError(f"cannot open candidate for binary reading: {path}") from exc

    stream: BinaryIO | None = None
    try:
        try:
            opened_info = os.fstat(descriptor)
        except OSError as exc:
            raise ArchiveReadError(f"cannot inspect opened candidate descriptor: {path}") from exc
        _ensure_same_entry(before, opened_info, context="between path validation and open")

        path_after_open, path_info_after_open = _validate_path(item)
        if path_after_open != path:
            raise ArchiveChangedDuringRegistrationError(
                f"candidate path changed between validation and open: {path}"
            )
        _ensure_unchanged(before, path_info_after_open, context="before reading")

        try:
            stream = os.fdopen(descriptor, "rb", buffering=0)
            descriptor = -1
        except OSError as exc:
            raise ArchiveReadError(f"cannot create binary stream for candidate: {path}") from exc

        with stream:
            yield stream, before, _snapshot(opened_info)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _iter_chunks(stream: BinaryIO, chunk_size_bytes: int) -> Iterator[bytes]:
    while True:
        try:
            chunk = stream.read(chunk_size_bytes)
        except OSError as exc:
            raise ArchiveReadError("binary candidate read failed") from exc
        if not chunk:
            return
        if len(chunk) > chunk_size_bytes:
            raise ArchiveReadError("binary stream exceeded requested bounded read size")
        yield chunk


def _filename_result_to_dict(result: FilenameParseResult) -> dict[str, object]:
    if isinstance(result, RecognizedSourceFilename):
        return {
            "contract_id": result.contract_id,
            "disposition": "recognized",
            "expansion_token": result.expansion_token,
            "format_token": result.format_token,
            "original_filename": result.original_basename,
            "source_kind": result.source_kind.value,
        }
    if isinstance(result, UnrecognizedSourceFilename):
        return {
            "contract_id": result.contract_id,
            "diagnostic_code": result.diagnostic_code.value,
            "disposition": "unrecognized",
            "original_filename": result.original_basename,
            "reason": result.reason,
        }
    raise RegistrationConfigurationError("inventory item has an unsupported filename result type")


def _make_record(
    item: InventoryItem,
    *,
    provider_namespace: str,
    compressed_sha256: str,
    compressed_size_bytes: int,
    source_metadata: SourceArchiveMetadataV1 | None,
    acquisition_metadata: AcquisitionMetadataV1 | None,
    ingestion_audit_metadata: IngestionAuditMetadataV1 | None,
) -> SourceArchiveRecordV1:
    return SourceArchiveRecordV1(
        source_archive_record_schema_id=SOURCE_ARCHIVE_RECORD_SCHEMA_ID,
        provider=PROVIDER,
        provider_namespace=provider_namespace,
        source_archive_id_contract_id=SOURCE_ARCHIVE_ID_CONTRACT_ID,
        source_archive_id=derive_source_archive_id(
            compressed_sha256,
            provider_namespace=provider_namespace,
        ),
        compressed_sha256=compressed_sha256,
        compressed_size_bytes=compressed_size_bytes,
        original_filename=item.basename,
        filename_contract_id=item.filename_result.contract_id,
        inventory_contract_id=INVENTORY_CONTRACT_ID,
        filename_result=item.filename_result,
        source_metadata=source_metadata or SourceArchiveMetadataV1(),
        acquisition_metadata=acquisition_metadata or AcquisitionMetadataV1(),
        ingestion_audit_metadata=ingestion_audit_metadata or IngestionAuditMetadataV1(),
        raw_root=item.raw_root,
        relative_path=item.relative_path,
        registration_status=RegistrationStatus.REGISTERED,
    )


def register_archive(
    item: InventoryItem,
    *,
    provider_namespace: str = PROVIDER_NAMESPACE,
    chunk_size_bytes: int = HASH_CHUNK_SIZE_BYTES,
    source_metadata: SourceArchiveMetadataV1 | None = None,
    acquisition_metadata: AcquisitionMetadataV1 | None = None,
    ingestion_audit_metadata: IngestionAuditMetadataV1 | None = None,
) -> SourceArchiveRecordV1:
    """Register one M2.2 candidate by streaming its exact compressed bytes."""
    if chunk_size_bytes <= 0:
        raise RegistrationConfigurationError("chunk_size_bytes must be positive")
    if not provider_namespace:
        raise RegistrationConfigurationError("provider_namespace must be non-empty")
    try:
        digest = hashlib.sha256()
        byte_count = 0
        with _open_verified_archive(item) as (stream, before, opened_snapshot):
            for chunk in _iter_chunks(stream, chunk_size_bytes):
                digest.update(chunk)
                byte_count += len(chunk)

            try:
                descriptor_after = os.fstat(stream.fileno())
            except OSError as exc:
                raise ArchiveReadError("cannot verify candidate descriptor after hashing") from exc
            if byte_count != before.size:
                raise ArchiveByteCountMismatchError(
                    f"read {byte_count} bytes but initial stable size was {before.size}"
                )
            _ensure_unchanged(opened_snapshot, descriptor_after, context="during binary hashing")

            _, final_path_info = _validate_path(item)
            _ensure_unchanged(before, final_path_info, context="during binary hashing")
            _ensure_same_entry(
                opened_snapshot,
                final_path_info,
                context="between open descriptor and final path check",
            )
            if byte_count != final_path_info.st_size:
                raise ArchiveByteCountMismatchError(
                    f"read {byte_count} bytes but final stable size was {final_path_info.st_size}"
                )
    except ArchiveRegistrationError:
        raise
    except OSError as exc:
        raise ArchiveReadError(f"binary registration failed for {item.basename}") from exc

    return _make_record(
        item,
        provider_namespace=provider_namespace,
        compressed_sha256=digest.hexdigest(),
        compressed_size_bytes=byte_count,
        source_metadata=source_metadata,
        acquisition_metadata=acquisition_metadata,
        ingestion_audit_metadata=ingestion_audit_metadata,
    )


def _duplicate_groups(
    records: tuple[SourceArchiveRecordV1, ...],
) -> tuple[DuplicateSourceArchiveGroup, ...]:
    records_by_id: dict[str, list[SourceArchiveRecordV1]] = defaultdict(list)
    for record in records:
        records_by_id[record.source_archive_id].append(record)

    groups = []
    for source_archive_id, matching in records_by_id.items():
        if len(matching) > 1:
            groups.append(
                DuplicateSourceArchiveGroup(
                    source_archive_id=source_archive_id,
                    record_count=len(matching),
                    locations=tuple(
                        SourceArchiveLocationV1(
                            raw_root=record.raw_root,
                            relative_path=record.relative_path,
                        )
                        for record in matching
                    ),
                )
            )
    return tuple(sorted(groups, key=lambda group: group.source_archive_id))


def register_inventory_archives(
    inventory: InventoryResult,
    *,
    provider_namespace: str = PROVIDER_NAMESPACE,
    chunk_size_bytes: int = HASH_CHUNK_SIZE_BYTES,
) -> ArchiveRegistrationResult:
    """Register every candidate in an inventory, returning all records or none."""
    if inventory.contract_id != INVENTORY_CONTRACT_ID:
        raise RegistrationConfigurationError(
            f"unsupported inventory contract: {inventory.contract_id}"
        )
    if chunk_size_bytes <= 0:
        raise RegistrationConfigurationError("chunk_size_bytes must be positive")
    if not provider_namespace:
        raise RegistrationConfigurationError("provider_namespace must be non-empty")

    canonical_roots = set(inventory.configured_roots)
    if any(item.raw_root not in canonical_roots for item in inventory.candidates):
        raise RegistrationConfigurationError(
            "inventory candidate references a root outside the inventory scope"
        )

    records: list[SourceArchiveRecordV1] = []
    for item in inventory.candidates:
        try:
            records.append(
                register_archive(
                    item,
                    provider_namespace=provider_namespace,
                    chunk_size_bytes=chunk_size_bytes,
                )
            )
        except ArchiveRegistrationError as exc:
            raise RegistrationExecutionError(
                "batch registration aborted for "
                f"{item.raw_root} / {item.relative_path.as_posix()}: {exc}"
            ) from exc

    ordered_records = tuple(records)
    result = ArchiveRegistrationResult(
        registration_contract_id=ARCHIVE_REGISTRATION_CONTRACT_ID,
        source_archive_record_schema_id=SOURCE_ARCHIVE_RECORD_SCHEMA_ID,
        source_archive_id_contract_id=SOURCE_ARCHIVE_ID_CONTRACT_ID,
        provider_namespace=provider_namespace,
        inventory_contract_id=inventory.contract_id,
        records=ordered_records,
        total_compressed_bytes=sum(record.compressed_size_bytes for record in ordered_records),
        duplicate_content_groups=_duplicate_groups(ordered_records),
    )
    if result.registered_archive_count != inventory.total_candidate_count:
        raise RegistrationExecutionError("registered archive count does not match inventory")
    if result.total_compressed_bytes != sum(
        record.compressed_size_bytes for record in result.records
    ):
        raise RegistrationExecutionError("compressed byte total reconciliation failed")
    return result
