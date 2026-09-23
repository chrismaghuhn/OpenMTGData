"""Bounded streaming gzip-integrity evidence for registered source archives."""

from __future__ import annotations

import gzip
import zlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import BinaryIO

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
from openmtgdata.inventory import InventoryItem

COMPRESSION_VALIDATION_CONTRACT_ID = "openmtgdata.compression-validation.v1"
DECOMPRESSED_DISCARD_CHUNK_SIZE_BYTES = 1024 * 1024


class CompressionValidationStatus(StrEnum):
    """Outcome of checking one registered compressed artifact as a gzip stream."""

    VALID = "valid"
    INVALID = "invalid"
    UNREADABLE = "unreadable"
    NOT_CHECKED = "not_checked"


class CompressionDiagnosticCode(StrEnum):
    """Stable machine-readable compression-validation findings."""

    INVALID_GZIP_HEADER = "INVALID_GZIP_HEADER"
    TRUNCATED_GZIP = "TRUNCATED_GZIP"
    GZIP_INTEGRITY_FAILURE = "GZIP_INTEGRITY_FAILURE"
    INVALID_GZIP_STREAM = "INVALID_GZIP_STREAM"
    READ_FAILURE = "READ_FAILURE"
    NOT_CHECKED = "NOT_CHECKED"


class CompressionValidationExecutionError(ArchiveRegistrationError):
    """Raised when evidence cannot be safely attached to a registered source."""


class RegisteredSourceBytesMismatchError(CompressionValidationExecutionError):
    """Raised when the file no longer matches its immutable M2.3 identity."""


@dataclass(frozen=True, slots=True)
class CompressionValidationEvidenceV1:
    """Compression result tied to one exact registered archive and local location."""

    contract_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    raw_root: str
    relative_path: PurePosixPath
    status: CompressionValidationStatus
    diagnostic_code: CompressionDiagnosticCode | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.contract_id != COMPRESSION_VALIDATION_CONTRACT_ID:
            raise ValueError("unsupported compression validation evidence contract")
        if self.compressed_size_bytes < 0:
            raise ValueError("compressed_size_bytes must be non-negative")
        if self.status is CompressionValidationStatus.VALID and (
            self.diagnostic_code is not None or self.reason is not None
        ):
            raise ValueError("valid compression evidence cannot carry a failure diagnostic")
        if self.status is not CompressionValidationStatus.VALID and (
            self.diagnostic_code is None or self.reason is None
        ):
            raise ValueError("non-valid compression evidence requires a diagnostic")

    def to_dict(self) -> dict[str, object]:
        return {
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "contract_id": self.contract_id,
            "diagnostic_code": self.diagnostic_code.value if self.diagnostic_code else None,
            "path_scope": "runtime_local",
            "raw_root": self.raw_root,
            "reason": self.reason,
            "relative_path": self.relative_path.as_posix(),
            "source_archive_id": self.source_archive_id,
            "status": self.status.value,
        }


def not_checked_compression_evidence(
    record: SourceArchiveRecordV1,
) -> CompressionValidationEvidenceV1:
    """Represent explicitly that this registered source has not been gzip-checked."""
    return CompressionValidationEvidenceV1(
        contract_id=COMPRESSION_VALIDATION_CONTRACT_ID,
        source_archive_id=record.source_archive_id,
        compressed_sha256=record.compressed_sha256,
        compressed_size_bytes=record.compressed_size_bytes,
        raw_root=str(record.raw_root),
        relative_path=record.relative_path,
        status=CompressionValidationStatus.NOT_CHECKED,
        diagnostic_code=CompressionDiagnosticCode.NOT_CHECKED,
        reason="gzip integrity validation was not requested",
    )


def _item_for_record(record: SourceArchiveRecordV1) -> InventoryItem:
    return InventoryItem(
        basename=record.original_filename,
        raw_root=record.raw_root,
        relative_path=record.relative_path,
        filename_result=record.filename_result,
    )


def _classify_zlib_error(message: str) -> tuple[CompressionDiagnosticCode, str]:
    lowered = message.casefold()
    if "data check" in lowered or "crc" in lowered or "length check" in lowered:
        return (
            CompressionDiagnosticCode.GZIP_INTEGRITY_FAILURE,
            "gzip member CRC/length integrity check failed",
        )
    if "header check" in lowered or "unknown compression method" in lowered:
        return (
            CompressionDiagnosticCode.INVALID_GZIP_HEADER,
            "gzip header or compression method is invalid",
        )
    return (CompressionDiagnosticCode.INVALID_GZIP_STREAM, "gzip stream is structurally invalid")


def _consume_gzip_stream(
    compressed_stream: BinaryIO,
) -> tuple[CompressionValidationStatus, CompressionDiagnosticCode | None, str | None]:
    try:
        with gzip.GzipFile(fileobj=compressed_stream, mode="rb") as decompressed:
            while decompressed.read(DECOMPRESSED_DISCARD_CHUNK_SIZE_BYTES):
                pass
    except gzip.BadGzipFile as exc:
        message = str(exc).casefold()
        if "crc" in message or "length check" in message:
            return (
                CompressionValidationStatus.INVALID,
                CompressionDiagnosticCode.GZIP_INTEGRITY_FAILURE,
                "gzip member CRC/length integrity check failed",
            )
        if "not a gzipped file" in message or "header" in message:
            return (
                CompressionValidationStatus.INVALID,
                CompressionDiagnosticCode.INVALID_GZIP_HEADER,
                "gzip header is invalid",
            )
        return (
            CompressionValidationStatus.INVALID,
            CompressionDiagnosticCode.INVALID_GZIP_STREAM,
            "gzip stream is structurally invalid",
        )
    except EOFError:
        return (
            CompressionValidationStatus.INVALID,
            CompressionDiagnosticCode.TRUNCATED_GZIP,
            "gzip stream ended before its trailer/member completed",
        )
    except zlib.error as exc:
        code, reason = _classify_zlib_error(str(exc))
        return (CompressionValidationStatus.INVALID, code, reason)
    except OSError:
        return (
            CompressionValidationStatus.UNREADABLE,
            CompressionDiagnosticCode.READ_FAILURE,
            "underlying compressed file could not be read during gzip validation",
        )
    return (CompressionValidationStatus.VALID, None, None)


def validate_registered_compression(
    record: SourceArchiveRecordV1,
) -> CompressionValidationEvidenceV1:
    """Stream and discard decompressed output, tying evidence back to M2.3 bytes.

    M2.3's no-follow open and mutation checks are reused. A final M2.3 registration
    re-hashes the path and must reproduce the record identity before evidence is
    returned. No decompressed bytes are retained or interpreted.
    """
    item = _item_for_record(record)
    try:
        with _open_verified_archive(item) as (compressed_stream, before, opened):
            status, diagnostic_code, reason = _consume_gzip_stream(compressed_stream)
            _verify_opened_archive_unchanged(item, compressed_stream, before, opened)

        current = register_archive(item, provider_namespace=record.provider_namespace)
    except ArchiveRegistrationError as exc:
        raise CompressionValidationExecutionError(
            "compression validation could not safely verify registered source "
            f"{record.source_archive_id}: {exc}"
        ) from exc

    if (
        current.source_archive_id != record.source_archive_id
        or current.compressed_sha256 != record.compressed_sha256
        or current.compressed_size_bytes != record.compressed_size_bytes
    ):
        raise RegisteredSourceBytesMismatchError(
            "current file bytes no longer match the immutable registered source archive"
        )

    return CompressionValidationEvidenceV1(
        contract_id=COMPRESSION_VALIDATION_CONTRACT_ID,
        source_archive_id=record.source_archive_id,
        compressed_sha256=record.compressed_sha256,
        compressed_size_bytes=record.compressed_size_bytes,
        raw_root=str(record.raw_root),
        relative_path=record.relative_path,
        status=status,
        diagnostic_code=diagnostic_code,
        reason=reason,
    )


def validate_registration_compression(
    registration: ArchiveRegistrationResult,
) -> tuple[CompressionValidationEvidenceV1, ...]:
    """Validate every record in an M2.3 registration, returning all or raising."""
    if registration.registration_contract_id != ARCHIVE_REGISTRATION_CONTRACT_ID:
        raise CompressionValidationExecutionError(
            "compression validation requires the supported archive registration contract"
        )
    if registration.source_archive_record_schema_id != SOURCE_ARCHIVE_RECORD_SCHEMA_ID:
        raise CompressionValidationExecutionError(
            "compression validation requires SourceArchiveRecordV1 registrations"
        )
    return tuple(validate_registered_compression(record) for record in registration.records)
