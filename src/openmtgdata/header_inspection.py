"""Header-only physical schema evidence and corpus grouping (M2.5)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from threading import Lock

from openmtgdata.archive_registration import (
    ARCHIVE_REGISTRATION_CONTRACT_ID,
    SOURCE_ARCHIVE_RECORD_SCHEMA_ID,
    ArchiveRegistrationError,
    ArchiveRegistrationResult,
    SourceArchiveRecordV1,
    register_archive,
)
from openmtgdata.compression_validation import (
    CompressionDiagnosticCode,
    CompressionValidationEvidenceV1,
    CompressionValidationStatus,
)
from openmtgdata.inventory import InventoryItem
from openmtgdata.source_container import (
    SOURCE_CONTAINER_POLICY_ID,
    ByteReader,
    SourceContainerError,
    open_csv_source_payload,
)
from openmtgdata.source_filename import RecognizedSourceFilename, SourceKind
from openmtgdata.source_manifest import SOURCE_MANIFEST_SCHEMA_ID, SourceManifestV1

SOURCE_INSPECTION_SCHEMA_ID = "openmtgdata.source-inspection.v1"
SOURCE_INSPECTION_ID_CONTRACT_ID = "openmtgdata.source-inspection-id.v1"
HEADER_INSPECTION_METHOD_ID = "openmtgdata.header-inspection.v2"
RAW_SCHEMA_FINGERPRINT_CONTRACT_ID = "openmtgdata.raw-schema-fingerprint.v1"
HEADER_INVENTORY_CONTRACT_ID = "openmtgdata.header-inventory.v1"
SCHEMA_GROUPING_CONTRACT_ID = "openmtgdata.schema-grouping.v1"
HEADER_EVIDENCE_SCOPE_ID = "openmtgdata.header-evidence-scope.v1"
MAX_HEADER_BYTES = 4 * 1024 * 1024
MAX_HEADER_FIELDS = 4096
MAX_HEADER_FIELD_CHARS = 1024 * 1024
_LINE_READ_CHUNK_BYTES = 8192
_CSV_LIMIT_LOCK = Lock()


@contextmanager
def csv_field_limit(max_field_chars: int) -> Iterator[None]:
    """Temporarily set stdlib CSV's process-global field limit under a lock."""
    if max_field_chars <= 0:
        raise ValueError("CSV field limit must be positive")
    with _CSV_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(max_field_chars)
        try:
            yield
        finally:
            csv.field_size_limit(previous_limit)


class HeaderInspectionExecutionError(RuntimeError):
    """An archive identity or filesystem failure prevented trustworthy inspection."""


class HeaderInspectionStatus(StrEnum):
    SUCCESS = "success"
    UNSUPPORTED = "unsupported"


class HeaderDiagnosticCode(StrEnum):
    EMPTY_ARCHIVE = "EMPTY_ARCHIVE"
    MISSING_HEADER = "MISSING_HEADER"
    HEADER_TOO_LARGE = "HEADER_TOO_LARGE"
    TOO_MANY_HEADER_FIELDS = "TOO_MANY_HEADER_FIELDS"
    HEADER_FIELD_TOO_LARGE = "HEADER_FIELD_TOO_LARGE"
    INVALID_ENCODING = "INVALID_ENCODING"
    CSV_HEADER_PARSE_ERROR = "CSV_HEADER_PARSE_ERROR"
    DUPLICATE_HEADER_FIELD = "DUPLICATE_HEADER_FIELD"
    EMPTY_HEADER_FIELD = "EMPTY_HEADER_FIELD"
    INVALID_GZIP = "INVALID_GZIP"
    TRUNCATED_GZIP = "TRUNCATED_GZIP"
    READ_FAILURE = "READ_FAILURE"
    COMPRESSION_NOT_CHECKED = "COMPRESSION_NOT_CHECKED"
    GZIP_INTEGRITY_FAILURE = "GZIP_INTEGRITY_FAILURE"
    UNSUPPORTED_SOURCE_CONTAINER = "UNSUPPORTED_SOURCE_CONTAINER"


class TypeEvidenceStatus(StrEnum):
    NOT_INSPECTED = "not_inspected"


class NullabilityEvidenceStatus(StrEnum):
    NOT_INSPECTED = "not_inspected"


class HeaderInspectionError(Exception):
    """Source-level failure while obtaining one header, with stable diagnostic code."""

    def __init__(self, code: HeaderDiagnosticCode, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CsvParserObservationV1:
    """Explicit scanner policy and successful header-parse observation."""

    policy_id: str = "openmtgdata.csv-header-policy.comma-utf8.v1"
    configured_delimiter: str = ","
    configured_quotechar: str = '"'
    configured_doublequote: bool = True
    configured_escapechar: str | None = None
    strict: bool = True
    source_declaration: str = "not_observed"
    parse_result: str = "successful_first_record"
    quote_character_observed: bool = False
    doubled_quote_observed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "configured_delimiter": self.configured_delimiter,
            "configured_doublequote": self.configured_doublequote,
            "configured_escapechar": self.configured_escapechar,
            "configured_quotechar": self.configured_quotechar,
            "doubled_quote_observed": self.doubled_quote_observed,
            "parse_result": self.parse_result,
            "policy_id": self.policy_id,
            "quote_character_observed": self.quote_character_observed,
            "source_declaration": self.source_declaration,
            "strict": self.strict,
        }


@dataclass(frozen=True, slots=True)
class EncodingObservationV1:
    """Encoding used by the scanner, distinct from an authoritative declaration."""

    encoding_used: str
    bom_present: bool
    decode_status: str = "success"
    authoritative_declaration: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "authoritative_declaration": self.authoritative_declaration,
            "bom_present": self.bom_present,
            "decode_status": self.decode_status,
            "encoding_used": self.encoding_used,
        }


@dataclass(frozen=True, slots=True)
class HeaderEvidenceV1:
    """Exact ordered first-record fields plus physical header observations."""

    ordered_fields: tuple[str, ...]
    field_count: int
    duplicate_field_names: tuple[str, ...]
    empty_field_positions: tuple[int, ...]
    encoding: EncodingObservationV1
    parser_observation: CsvParserObservationV1
    physical_line_endings: tuple[str, ...]
    header_byte_count: int
    header_character_count: int
    fingerprint_projection_bytes: bytes
    raw_schema_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_field_names": list(self.duplicate_field_names),
            "empty_field_positions": list(self.empty_field_positions),
            "encoding_observation": self.encoding.to_dict(),
            "field_count": self.field_count,
            "header_byte_count": self.header_byte_count,
            "header_character_count": self.header_character_count,
            "ordered_fields": list(self.ordered_fields),
            "parser_observation": self.parser_observation.to_dict(),
            "physical_line_endings": list(self.physical_line_endings),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SourceInspectionV1:
    """Header-only evidence tied to one immutable registered source archive."""

    source_inspection_schema_id: str
    inspection_method_contract_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    compression_validation_status: CompressionValidationStatus
    raw_schema_fingerprint: str | None
    status: HeaderInspectionStatus
    evidence_scope_contract_id: str
    evidence_scope: tuple[str, ...]
    header_evidence: HeaderEvidenceV1 | None
    type_evidence_status: TypeEvidenceStatus
    type_evidence: None
    nullability_evidence_status: NullabilityEvidenceStatus
    nullability_evidence: None
    source_interpretation_contract_id: None
    diagnostics: tuple[HeaderDiagnosticCode, ...]
    tool_identity: str | None
    inspection_timestamp_utc: str | None
    source_inspection_id: str
    source_inspection_id_contract_id: str
    raw_root: Path
    relative_path: PurePosixPath
    source_kind: SourceKind | None
    expansion_token: str | None
    format_token: str | None
    source_container_policy_id: str
    source_container_kind: str | None
    source_container_member_name: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "compression_validation_status": self.compression_validation_status.value,
            "diagnostics": [code.value for code in self.diagnostics],
            "evidence_scope": list(self.evidence_scope),
            "evidence_scope_contract_id": self.evidence_scope_contract_id,
            "expansion_token": self.expansion_token,
            "format_token": self.format_token,
            "header_evidence": (
                self.header_evidence.to_dict() if self.header_evidence is not None else None
            ),
            "inspection_method_contract_id": self.inspection_method_contract_id,
            "inspection_timestamp_utc": self.inspection_timestamp_utc,
            "nullability_evidence": self.nullability_evidence,
            "nullability_evidence_status": self.nullability_evidence_status.value,
            "path_scope": "runtime_local",
            "raw_root": str(self.raw_root),
            "relative_path": self.relative_path.as_posix(),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "source_archive_id": self.source_archive_id,
            "source_container_kind": self.source_container_kind,
            "source_container_member_name": self.source_container_member_name,
            "source_container_policy_id": self.source_container_policy_id,
            "source_inspection_id": self.source_inspection_id,
            "source_inspection_id_contract_id": self.source_inspection_id_contract_id,
            "source_inspection_schema_id": self.source_inspection_schema_id,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
            "status": self.status.value,
            "tool_identity": self.tool_identity,
            "type_evidence": self.type_evidence,
            "type_evidence_status": self.type_evidence_status.value,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class SchemaGroupV1:
    """Deterministic reporting group; identity excludes expansion and format."""

    group_id: str
    source_kind: SourceKind | None
    raw_schema_fingerprint: str
    archive_count: int
    field_count: int
    expansions: tuple[str, ...]
    formats: tuple[str, ...]
    expansion_format_counts: tuple[tuple[str, str, int], ...]
    representative_filenames: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "archive_count": self.archive_count,
            "expansion_format_counts": [
                {"expansion": expansion, "format": fmt, "archive_count": count}
                for expansion, fmt, count in self.expansion_format_counts
            ],
            "expansions": list(self.expansions),
            "field_count": self.field_count,
            "formats": list(self.formats),
            "group_id": self.group_id,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "representative_filenames": list(self.representative_filenames),
            "source_kind": self.source_kind.value if self.source_kind is not None else None,
        }


@dataclass(frozen=True, slots=True)
class HeaderInventoryV1:
    """Complete header-only accounting for all registrations in one source catalog."""

    contract_id: str
    schema_grouping_contract_id: str
    semantic_source_catalog_digest: str
    configured_source_count: int
    configured_raw_roots: tuple[Path, ...]
    attempted_inspection_count: int
    successful_inspection_count: int
    failed_inspection_count: int
    inspections: tuple[SourceInspectionV1, ...]
    schema_groups: tuple[SchemaGroupV1, ...]
    source_kind_counts: tuple[tuple[str, int], ...]
    diagnostic_counts: tuple[tuple[str, int], ...]
    scope_statement: str
    maximum_header_field_count: int | None
    minimum_header_field_count: int | None
    maximum_header_byte_count: int | None
    max_header_bytes: int
    max_header_fields: int
    max_header_field_chars: int

    @property
    def unique_raw_schema_fingerprint_count(self) -> int:
        return len(
            {
                item.header_evidence.raw_schema_fingerprint
                for item in self.inspections
                if item.header_evidence is not None
            }
        )

    @property
    def schema_counts_by_source_kind(self) -> dict[str, int]:
        counts: dict[str, set[str]] = {kind.value: set() for kind in SourceKind}
        counts["unknown"] = set()
        for item in self.inspections:
            if item.header_evidence is not None:
                key = item.source_kind.value if item.source_kind is not None else "unknown"
                counts[key].add(item.header_evidence.raw_schema_fingerprint)
        return {key: len(value) for key, value in sorted(counts.items())}

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted_inspection_count": self.attempted_inspection_count,
            "configured_source_count": self.configured_source_count,
            "configured_raw_roots": [str(path) for path in self.configured_raw_roots],
            "configured_raw_roots_scope": "runtime_local",
            "contract_id": self.contract_id,
            "diagnostic_counts": dict(self.diagnostic_counts),
            "failed_inspection_count": self.failed_inspection_count,
            "inspection_method_contract_id": HEADER_INSPECTION_METHOD_ID,
            "inspections": [item.to_dict() for item in self.inspections],
            "maximum_header_byte_count": self.maximum_header_byte_count,
            "maximum_header_field_count": self.maximum_header_field_count,
            "minimum_header_field_count": self.minimum_header_field_count,
            "scanner_safety_limits": {
                "max_header_bytes": self.max_header_bytes,
                "max_header_fields": self.max_header_fields,
                "max_header_field_chars": self.max_header_field_chars,
            },
            "raw_schema_fingerprint_contract_id": RAW_SCHEMA_FINGERPRINT_CONTRACT_ID,
            "schema_grouping_contract_id": self.schema_grouping_contract_id,
            "schema_groups": [group.to_dict() for group in self.schema_groups],
            "schema_counts_by_source_kind": self.schema_counts_by_source_kind,
            "scope_statement": self.scope_statement,
            "semantic_source_catalog_digest": self.semantic_source_catalog_digest,
            "source_kind_counts": dict(self.source_kind_counts),
            "successful_inspection_count": self.successful_inspection_count,
            "unique_raw_schema_fingerprint_count": self.unique_raw_schema_fingerprint_count,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_dict())


class _BoundedPhysicalLines(Iterator[str]):
    """Yield UTF-8 physical lines through the first CSV record, with small read-ahead."""

    def __init__(
        self,
        stream: ByteReader,
        max_header_bytes: int,
        max_header_fields: int,
    ) -> None:
        self._stream = stream
        self._max_header_bytes = max_header_bytes
        self._max_header_fields = max_header_fields
        self._buffer = bytearray()
        self._eof = False
        self._bom_checked = False
        self._header_bytes = 0
        self._header_chars = 0
        self._line_endings: list[str] = []
        self.quote_character_observed = False
        self.doubled_quote_observed = False
        self._inside_quotes = False
        self._at_field_start = True
        self._field_count = 1
        self.bom_present = False
        self.decompressed_bytes_read = 0

    @staticmethod
    def _terminator_index(data: bytearray, *, eof: bool = False) -> tuple[int, int, str] | None:
        for index, value in enumerate(data):
            if value == 10:
                if index > 0 and data[index - 1] == 13:
                    return index - 1, index + 1, "CRLF"
                return index, index + 1, "LF"
            if value == 13:
                if index + 1 < len(data):
                    if data[index + 1] == 10:
                        return index, index + 2, "CRLF"
                    return index, index + 1, "CR"
                if eof:
                    return index, index + 1, "CR"
        return None

    def __iter__(self) -> _BoundedPhysicalLines:
        return self

    def begin_next_record(self) -> None:
        """Reset per-record safety/evidence counters after csv.reader returned a record."""
        self._header_bytes = 0
        self._header_chars = 0
        self._line_endings.clear()
        self._inside_quotes = False
        self._at_field_start = True
        self._field_count = 1
        self.quote_character_observed = False
        self.doubled_quote_observed = False

    def __next__(self) -> str:
        while True:
            terminator = self._terminator_index(self._buffer, eof=self._eof)
            if terminator is not None:
                start, end, ending = terminator
                raw = bytes(self._buffer[:end])
                del self._buffer[:end]
                return self._decode_line(raw, ending)
            if self._eof:
                if not self._buffer:
                    raise StopIteration
                raw = bytes(self._buffer)
                self._buffer.clear()
                return self._decode_line(raw, "NONE")

            allowance = self._max_header_bytes - self._header_bytes
            # A one-byte sentinel distinguishes an over-limit unterminated header.
            read_size = min(_LINE_READ_CHUNK_BYTES, max(1, allowance + 1))
            try:
                chunk = self._stream.read(read_size)
            except gzip.BadGzipFile as exc:
                raise HeaderInspectionError(
                    HeaderDiagnosticCode.INVALID_GZIP,
                    "archive is not a readable gzip stream",
                ) from exc
            except OSError as exc:
                raise HeaderInspectionError(
                    HeaderDiagnosticCode.READ_FAILURE,
                    "compressed stream could not be read while obtaining the CSV header",
                ) from exc
            except EOFError as exc:
                raise HeaderInspectionError(
                    HeaderDiagnosticCode.TRUNCATED_GZIP,
                    "gzip ended or failed before the first CSV record completed",
                ) from exc
            except zlib.error as exc:
                message = str(exc).casefold()
                code = (
                    HeaderDiagnosticCode.TRUNCATED_GZIP
                    if "incomplete" in message or "truncated" in message
                    else HeaderDiagnosticCode.INVALID_GZIP
                )
                raise HeaderInspectionError(
                    code,
                    "gzip decompression failed before the first CSV record completed",
                ) from exc
            if not chunk:
                self._eof = True
                continue
            self.decompressed_bytes_read += len(chunk)
            self._buffer.extend(chunk)
            if self._header_bytes + len(self._buffer) > self._max_header_bytes:
                # A terminator within the allowed prefix may still end a valid header.
                found = self._terminator_index(self._buffer)
                if found is None or found[1] > self._max_header_bytes - self._header_bytes:
                    raise HeaderInspectionError(
                        HeaderDiagnosticCode.HEADER_TOO_LARGE,
                        f"first CSV record exceeds {self._max_header_bytes} decompressed bytes",
                    )

    def _decode_line(self, raw: bytes, ending: str) -> str:
        if self._header_bytes + len(raw) > self._max_header_bytes:
            raise HeaderInspectionError(
                HeaderDiagnosticCode.HEADER_TOO_LARGE,
                f"first CSV record exceeds {self._max_header_bytes} decompressed bytes",
            )
        self._header_bytes += len(raw)
        self._line_endings.append(ending)
        content = raw
        if ending == "CRLF":
            content = raw[:-2]
        elif ending in {"CR", "LF"}:
            content = raw[:-1]
        if not self._bom_checked and content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]
        self.quote_character_observed |= b'"' in content
        self.doubled_quote_observed |= b'""' in content
        self._track_field_count(content)
        try:
            if not self._bom_checked:
                self._bom_checked = True
                self.bom_present = raw.startswith(b"\xef\xbb\xbf")
                decoded = raw.decode("utf-8-sig" if self.bom_present else "utf-8", errors="strict")
            else:
                decoded = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise HeaderInspectionError(
                HeaderDiagnosticCode.INVALID_ENCODING,
                "header is not valid UTF-8 under the v1 scanner policy",
            ) from exc
        self._header_chars += len(decoded)
        return decoded

    def _track_field_count(self, content: bytes) -> None:
        """Stop before a hostile comma-only header can allocate an unbounded list."""
        index = 0
        while index < len(content):
            value = content[index]
            if self._inside_quotes:
                if value == 34:
                    if index + 1 < len(content) and content[index + 1] == 34:
                        index += 2
                        continue
                    self._inside_quotes = False
            elif self._at_field_start:
                if value == 34:
                    self._inside_quotes = True
                    self._at_field_start = False
                elif value == 44:
                    self._field_count += 1
                    self._at_field_start = True
                else:
                    self._at_field_start = False
            elif value == 44:
                self._field_count += 1
                self._at_field_start = True

            if self._field_count > self._max_header_fields:
                raise HeaderInspectionError(
                    HeaderDiagnosticCode.TOO_MANY_HEADER_FIELDS,
                    f"header exceeds {self._max_header_fields} fields",
                )
            index += 1


def _header_fingerprint_projection(
    ordered_fields: tuple[str, ...],
    encoding: EncodingObservationV1,
    parser_observation: CsvParserObservationV1,
    physical_line_endings: tuple[str, ...],
) -> dict[str, object]:
    """Explicit physical evidence fields included in raw-schema identity v1."""
    return {
        "encoding_observation": {
            "bom_present": encoding.bom_present,
            "encoding_used": encoding.encoding_used,
            "authoritative_declaration": None,
        },
        "header_fields_ordered": list(ordered_fields),
        "csv_delimiter_used": parser_observation.configured_delimiter,
        "doubled_quote_observed": parser_observation.doubled_quote_observed,
        "physical_line_endings": list(physical_line_endings),
        "quote_character_observed": parser_observation.quote_character_observed,
        "raw_schema_fingerprint_contract_id": RAW_SCHEMA_FINGERPRINT_CONTRACT_ID,
    }


def raw_schema_fingerprint_projection_bytes(
    ordered_fields: tuple[str, ...],
    encoding: EncodingObservationV1,
    parser_observation: CsvParserObservationV1,
    physical_line_endings: tuple[str, ...],
) -> bytes:
    """Canonical v1 physical-schema projection, exposed for golden contract tests."""
    return _canonical_bytes(
        _header_fingerprint_projection(
            ordered_fields,
            encoding,
            parser_observation,
            physical_line_endings,
        )
    )


def fingerprint_raw_schema(
    ordered_fields: tuple[str, ...],
    *,
    encoding: EncodingObservationV1,
    parser_observation: CsvParserObservationV1,
    physical_line_endings: tuple[str, ...],
) -> tuple[bytes, str]:
    projection = raw_schema_fingerprint_projection_bytes(
        ordered_fields,
        encoding,
        parser_observation,
        physical_line_endings,
    )
    return projection, hashlib.sha256(projection).hexdigest()


def _parse_header(
    stream: ByteReader,
    *,
    max_header_bytes: int,
    max_header_fields: int,
    max_header_field_chars: int,
) -> HeaderEvidenceV1:
    lines = _BoundedPhysicalLines(stream, max_header_bytes, max_header_fields)
    with csv_field_limit(max_header_field_chars):
        try:
            reader = csv.reader(
                lines,
                delimiter=",",
                quotechar='"',
                doublequote=True,
                escapechar=None,
                strict=True,
            )
            try:
                fields = next(reader)
            except StopIteration as exc:
                if lines.decompressed_bytes_read == 0:
                    raise HeaderInspectionError(
                        HeaderDiagnosticCode.EMPTY_ARCHIVE,
                        "gzip stream contains no decompressed bytes",
                    ) from exc
                raise HeaderInspectionError(
                    HeaderDiagnosticCode.MISSING_HEADER,
                    "gzip stream contains no first CSV record",
                ) from exc
            except csv.Error as exc:
                message = str(exc)
                code = (
                    HeaderDiagnosticCode.HEADER_FIELD_TOO_LARGE
                    if "field larger than field limit" in message
                    else HeaderDiagnosticCode.CSV_HEADER_PARSE_ERROR
                )
                raise HeaderInspectionError(
                    code, "first CSV record is not parseable under v1"
                ) from exc
        except HeaderInspectionError:
            raise

    if not fields or (len(fields) == 1 and fields[0] == ""):
        raise HeaderInspectionError(
            HeaderDiagnosticCode.MISSING_HEADER,
            "first CSV record is blank and does not establish header fields",
        )
    if len(fields) > max_header_fields:
        raise HeaderInspectionError(
            HeaderDiagnosticCode.TOO_MANY_HEADER_FIELDS,
            f"header has {len(fields)} fields; limit is {max_header_fields}",
        )
    if any(len(field) > max_header_field_chars for field in fields):
        raise HeaderInspectionError(
            HeaderDiagnosticCode.HEADER_FIELD_TOO_LARGE,
            f"a header field exceeds {max_header_field_chars} characters",
        )

    ordered_fields = tuple(fields)
    duplicate_names = tuple(name for name, count in Counter(ordered_fields).items() if count > 1)
    empty_positions = tuple(index for index, name in enumerate(ordered_fields) if name == "")
    encoding = EncodingObservationV1(
        encoding_used="utf-8-sig" if lines.bom_present else "utf-8",
        bom_present=lines.bom_present,
    )
    parser_observation = CsvParserObservationV1(
        quote_character_observed=lines.quote_character_observed,
        doubled_quote_observed=lines.doubled_quote_observed,
    )
    physical_line_endings = tuple(lines._line_endings)
    projection, fingerprint = fingerprint_raw_schema(
        ordered_fields,
        encoding=encoding,
        parser_observation=parser_observation,
        physical_line_endings=physical_line_endings,
    )
    return HeaderEvidenceV1(
        ordered_fields=ordered_fields,
        field_count=len(ordered_fields),
        duplicate_field_names=duplicate_names,
        empty_field_positions=empty_positions,
        encoding=encoding,
        parser_observation=parser_observation,
        physical_line_endings=physical_line_endings,
        header_byte_count=lines._header_bytes,
        header_character_count=lines._header_chars,
        fingerprint_projection_bytes=projection,
        raw_schema_fingerprint=fingerprint,
    )


def _item_for_record(record: SourceArchiveRecordV1) -> InventoryItem:
    return InventoryItem(
        basename=record.original_filename,
        raw_root=record.raw_root,
        relative_path=record.relative_path,
        filename_result=record.filename_result,
    )


def _inspection_id(
    record: SourceArchiveRecordV1,
    *,
    status: HeaderInspectionStatus,
    scope: tuple[str, ...],
    header_evidence: HeaderEvidenceV1 | None,
    diagnostics: tuple[HeaderDiagnosticCode, ...],
    tool_identity: str | None,
    compression_validation_status: CompressionValidationStatus,
    source_container_kind: str | None,
    source_container_member_name: str | None,
) -> str:
    return _sha256_json(
        {
            "inspection_method_contract_id": HEADER_INSPECTION_METHOD_ID,
            "report_content": {
                "diagnostics": [item.value for item in diagnostics],
                "header_evidence": header_evidence.to_dict() if header_evidence else None,
                "status": status.value,
                "compression_validation_status": compression_validation_status.value,
                "source_container_policy_id": SOURCE_CONTAINER_POLICY_ID,
                "source_container_kind": source_container_kind,
                "source_container_member_name": source_container_member_name,
            },
            "scope": list(scope),
            "source_archive_id": record.source_archive_id,
            "source_inspection_id_contract_id": SOURCE_INSPECTION_ID_CONTRACT_ID,
            "tool_identity": tool_identity,
        }
    )


def _compression_diagnostic(
    code: CompressionDiagnosticCode | None,
) -> HeaderDiagnosticCode:
    if code is CompressionDiagnosticCode.NOT_CHECKED:
        return HeaderDiagnosticCode.COMPRESSION_NOT_CHECKED
    if code is CompressionDiagnosticCode.TRUNCATED_GZIP:
        return HeaderDiagnosticCode.TRUNCATED_GZIP
    if code is CompressionDiagnosticCode.GZIP_INTEGRITY_FAILURE:
        return HeaderDiagnosticCode.GZIP_INTEGRITY_FAILURE
    if code is CompressionDiagnosticCode.READ_FAILURE:
        return HeaderDiagnosticCode.READ_FAILURE
    return HeaderDiagnosticCode.INVALID_GZIP


def inspect_registered_archive(
    record: SourceArchiveRecordV1,
    *,
    compression_evidence: CompressionValidationEvidenceV1 | None = None,
    max_header_bytes: int = MAX_HEADER_BYTES,
    max_header_fields: int = MAX_HEADER_FIELDS,
    max_header_field_chars: int = MAX_HEADER_FIELD_CHARS,
    tool_identity: str | None = None,
    inspection_timestamp_utc: str | None = None,
) -> SourceInspectionV1:
    """Inspect one first CSV record, checking exact registered bytes around the read."""
    if min(max_header_bytes, max_header_fields, max_header_field_chars) <= 0:
        raise ValueError("header safety limits must be positive")
    item = _item_for_record(record)
    if not isinstance(record.filename_result, RecognizedSourceFilename):
        raise HeaderInspectionExecutionError("header inspection requires recognized source kind")
    source_kind = record.filename_result.source_kind.value
    compression_status = (
        compression_evidence.status
        if compression_evidence is not None
        else CompressionValidationStatus.NOT_CHECKED
    )
    if compression_evidence is not None and (
        compression_evidence.source_archive_id != record.source_archive_id
        or compression_evidence.compressed_sha256 != record.compressed_sha256
        or compression_evidence.compressed_size_bytes != record.compressed_size_bytes
        or compression_evidence.raw_root != str(record.raw_root)
        or compression_evidence.relative_path != record.relative_path
    ):
        raise HeaderInspectionExecutionError(
            "compression evidence does not match the registered archive identity/location"
        )

    from openmtgdata.archive_registration import (
        _open_verified_archive,
        _verify_opened_archive_unchanged,
    )

    evidence: HeaderEvidenceV1 | None = None
    diagnostics: tuple[HeaderDiagnosticCode, ...] = ()
    status = HeaderInspectionStatus.SUCCESS
    container_kind: str | None = None
    container_member_name: str | None = None
    if compression_status is CompressionValidationStatus.NOT_CHECKED:
        status = HeaderInspectionStatus.UNSUPPORTED
        diagnostics = (HeaderDiagnosticCode.COMPRESSION_NOT_CHECKED,)
    elif compression_status is not CompressionValidationStatus.VALID:
        status = HeaderInspectionStatus.UNSUPPORTED
        compression_code = (
            compression_evidence.diagnostic_code if compression_evidence is not None else None
        )
        diagnostics = (_compression_diagnostic(compression_code),)
    else:
        try:
            with _open_verified_archive(item) as (compressed, initial, opened):
                with gzip.GzipFile(fileobj=compressed, mode="rb") as decompressed:
                    payload = open_csv_source_payload(
                        decompressed,
                        original_filename=record.original_filename,
                        source_kind=source_kind,
                    )
                    container_kind = payload.container_kind
                    container_member_name = payload.member_name
                    try:
                        evidence = _parse_header(
                            payload.stream,
                            max_header_bytes=max_header_bytes,
                            max_header_fields=max_header_fields,
                            max_header_field_chars=max_header_field_chars,
                        )
                    finally:
                        payload.close()
                _verify_opened_archive_unchanged(item, compressed, initial, opened)
        except HeaderInspectionError as exc:
            status = HeaderInspectionStatus.UNSUPPORTED
            diagnostics = (exc.code,)
        except SourceContainerError:
            status = HeaderInspectionStatus.UNSUPPORTED
            diagnostics = (HeaderDiagnosticCode.UNSUPPORTED_SOURCE_CONTAINER,)
        except ArchiveRegistrationError as exc:
            raise HeaderInspectionExecutionError(
                f"registered source changed or became unsafe during header inspection: {exc}"
            ) from exc
        except gzip.BadGzipFile:
            status = HeaderInspectionStatus.UNSUPPORTED
            diagnostics = (HeaderDiagnosticCode.INVALID_GZIP,)
        except (EOFError, zlib.error):
            status = HeaderInspectionStatus.UNSUPPORTED
            diagnostics = (HeaderDiagnosticCode.TRUNCATED_GZIP,)
        except OSError as exc:
            raise HeaderInspectionExecutionError(
                "cannot read registered source during header inspection: "
                f"{record.original_filename}"
            ) from exc

    if evidence is not None:
        warning_codes: list[HeaderDiagnosticCode] = []
        if evidence.duplicate_field_names:
            warning_codes.append(HeaderDiagnosticCode.DUPLICATE_HEADER_FIELD)
        if evidence.empty_field_positions:
            warning_codes.append(HeaderDiagnosticCode.EMPTY_HEADER_FIELD)
        diagnostics = tuple(warning_codes)

    try:
        after = register_archive(item, provider_namespace=record.provider_namespace)
    except ArchiveRegistrationError as exc:
        raise HeaderInspectionExecutionError(
            f"cannot verify registered source after header inspection: {exc}"
        ) from exc
    if (
        after.source_archive_id != record.source_archive_id
        or after.compressed_sha256 != record.compressed_sha256
        or after.compressed_size_bytes != record.compressed_size_bytes
    ):
        raise HeaderInspectionExecutionError(
            f"registered bytes mismatch after header inspection for {record.original_filename}"
        )

    scope = (
        "header_only",
        "first_csv_record",
        "no_data_rows_interpreted",
        f"gzip_integrity_{compression_status.value}",
    )
    filename = record.filename_result
    recognized = filename if isinstance(filename, RecognizedSourceFilename) else None
    inspection_id = _inspection_id(
        record,
        status=status,
        scope=scope,
        header_evidence=evidence,
        diagnostics=diagnostics,
        tool_identity=tool_identity,
        compression_validation_status=compression_status,
        source_container_kind=container_kind,
        source_container_member_name=container_member_name,
    )
    return SourceInspectionV1(
        source_inspection_schema_id=SOURCE_INSPECTION_SCHEMA_ID,
        inspection_method_contract_id=HEADER_INSPECTION_METHOD_ID,
        source_archive_id=record.source_archive_id,
        compressed_sha256=record.compressed_sha256,
        compressed_size_bytes=record.compressed_size_bytes,
        compression_validation_status=compression_status,
        raw_schema_fingerprint=(evidence.raw_schema_fingerprint if evidence is not None else None),
        status=status,
        evidence_scope_contract_id=HEADER_EVIDENCE_SCOPE_ID,
        evidence_scope=scope,
        header_evidence=evidence,
        type_evidence_status=TypeEvidenceStatus.NOT_INSPECTED,
        type_evidence=None,
        nullability_evidence_status=NullabilityEvidenceStatus.NOT_INSPECTED,
        nullability_evidence=None,
        source_interpretation_contract_id=None,
        diagnostics=diagnostics,
        tool_identity=tool_identity,
        inspection_timestamp_utc=inspection_timestamp_utc,
        source_inspection_id=inspection_id,
        source_inspection_id_contract_id=SOURCE_INSPECTION_ID_CONTRACT_ID,
        raw_root=record.raw_root,
        relative_path=record.relative_path,
        source_kind=recognized.source_kind if recognized else None,
        expansion_token=recognized.expansion_token if recognized else None,
        format_token=recognized.format_token if recognized else None,
        source_container_policy_id=SOURCE_CONTAINER_POLICY_ID,
        source_container_kind=container_kind,
        source_container_member_name=container_member_name,
    )


def _group_id(source_kind: SourceKind | None, fingerprint: str) -> str:
    return _sha256_json(
        {
            "grouping_contract_id": SCHEMA_GROUPING_CONTRACT_ID,
            "raw_schema_fingerprint": fingerprint,
            "source_kind": source_kind.value if source_kind is not None else None,
        }
    )


def _build_schema_groups(inspections: tuple[SourceInspectionV1, ...]) -> tuple[SchemaGroupV1, ...]:
    grouped: dict[tuple[SourceKind | None, str], list[SourceInspectionV1]] = defaultdict(list)
    for inspection in inspections:
        if inspection.header_evidence is not None:
            grouped[
                (inspection.source_kind, inspection.header_evidence.raw_schema_fingerprint)
            ].append(inspection)
    result: list[SchemaGroupV1] = []
    for (source_kind, fingerprint), members in grouped.items():
        expansions = tuple(
            sorted({item.expansion_token for item in members if item.expansion_token})
        )
        formats = tuple(sorted({item.format_token for item in members if item.format_token}))
        pair_counts = Counter(
            (item.expansion_token or "", item.format_token or "") for item in members
        )
        first_evidence = members[0].header_evidence
        if first_evidence is None:
            raise HeaderInspectionExecutionError(
                "schema grouping received an inspection without header evidence"
            )
        result.append(
            SchemaGroupV1(
                group_id=_group_id(source_kind, fingerprint),
                source_kind=source_kind,
                raw_schema_fingerprint=fingerprint,
                archive_count=len(members),
                field_count=first_evidence.field_count,
                expansions=expansions,
                formats=formats,
                expansion_format_counts=tuple(
                    (expansion, fmt, count)
                    for (expansion, fmt), count in sorted(pair_counts.items())
                ),
                representative_filenames=tuple(
                    sorted({Path(item.relative_path.as_posix()).name for item in members})[:3]
                ),
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda group: (
                group.source_kind.value if group.source_kind else "",
                group.raw_schema_fingerprint,
            ),
        )
    )


def build_header_inventory(
    registration: ArchiveRegistrationResult,
    *,
    source_manifest: SourceManifestV1,
    configured_raw_roots: tuple[Path, ...],
    tool_identity: str | None = None,
    inspection_timestamp_utc: str | None = None,
    max_header_bytes: int = MAX_HEADER_BYTES,
    max_header_fields: int = MAX_HEADER_FIELDS,
    max_header_field_chars: int = MAX_HEADER_FIELD_CHARS,
) -> HeaderInventoryV1:
    """Inspect every M2.3 registration and account for each exactly once."""
    if registration.registration_contract_id != ARCHIVE_REGISTRATION_CONTRACT_ID:
        raise HeaderInspectionExecutionError("unsupported archive registration contract")
    if registration.source_archive_record_schema_id != SOURCE_ARCHIVE_RECORD_SCHEMA_ID:
        raise HeaderInspectionExecutionError("unsupported SourceArchiveRecordV1 schema")
    if source_manifest.source_manifest_schema_id != SOURCE_MANIFEST_SCHEMA_ID:
        raise HeaderInspectionExecutionError("unsupported SourceManifestV1 schema")
    if len(source_manifest.archive_records) != registration.registered_archive_count:
        raise HeaderInspectionExecutionError(
            "source manifest record count does not match archive registration"
        )
    registration_locations = tuple(
        sorted(
            (
                record.source_archive_id,
                str(record.raw_root),
                record.relative_path.as_posix(),
            )
            for record in registration.records
        )
    )
    manifest_locations = tuple(
        sorted(
            (
                record.source_archive_id,
                str(record.raw_root),
                record.relative_path.as_posix(),
            )
            for record in source_manifest.archive_records
        )
    )
    if registration_locations != manifest_locations:
        raise HeaderInspectionExecutionError(
            "source manifest archive registrations do not match header inventory input"
        )
    semantic_source_catalog_digest = source_manifest.semantic_source_catalog_digest
    compression_by_location = {
        (str(item.raw_root), item.relative_path.as_posix()): item
        for item in source_manifest.compression_evidence
    }
    if len(compression_by_location) != len(registration.records):
        raise HeaderInspectionExecutionError(
            "source manifest compression evidence does not account for each registration"
        )
    records = tuple(
        sorted(
            registration.records,
            key=lambda item: (
                item.source_archive_id,
                str(item.raw_root),
                item.relative_path.as_posix(),
            ),
        )
    )
    configured_root_set = set(configured_raw_roots)
    if any(record.raw_root not in configured_root_set for record in records):
        raise HeaderInspectionExecutionError(
            "archive registration contains a source outside the configured raw roots"
        )
    if len(records) != registration.registered_archive_count:
        raise HeaderInspectionExecutionError("registration count does not reconcile with records")
    inspections = tuple(
        inspect_registered_archive(
            record,
            compression_evidence=compression_by_location[
                (str(record.raw_root), record.relative_path.as_posix())
            ],
            max_header_bytes=max_header_bytes,
            max_header_fields=max_header_fields,
            max_header_field_chars=max_header_field_chars,
            tool_identity=tool_identity,
            inspection_timestamp_utc=inspection_timestamp_utc,
        )
        for record in records
    )
    inspection_locations = {
        (str(item.raw_root), item.relative_path.as_posix()) for item in inspections
    }
    if len(inspections) != len(records) or len(inspection_locations) != len(records):
        raise HeaderInspectionExecutionError(
            "header inspection accounting is incomplete or ambiguous"
        )

    diagnostic_counts = Counter(
        diagnostic.value for inspection in inspections for diagnostic in inspection.diagnostics
    )
    kind_counts: Counter[str] = Counter()
    for inspection in inspections:
        kind_counts[inspection.source_kind.value if inspection.source_kind else "unknown"] += 1
    field_counts = [
        inspection.header_evidence.field_count
        for inspection in inspections
        if inspection.header_evidence is not None
    ]
    byte_counts = [
        inspection.header_evidence.header_byte_count
        for inspection in inspections
        if inspection.header_evidence is not None
    ]
    return HeaderInventoryV1(
        contract_id=HEADER_INVENTORY_CONTRACT_ID,
        schema_grouping_contract_id=SCHEMA_GROUPING_CONTRACT_ID,
        semantic_source_catalog_digest=semantic_source_catalog_digest,
        configured_source_count=len(records),
        configured_raw_roots=tuple(sorted(set(configured_raw_roots), key=str)),
        attempted_inspection_count=len(inspections),
        successful_inspection_count=sum(
            inspection.status is HeaderInspectionStatus.SUCCESS for inspection in inspections
        ),
        failed_inspection_count=sum(
            inspection.status is HeaderInspectionStatus.UNSUPPORTED for inspection in inspections
        ),
        inspections=inspections,
        schema_groups=_build_schema_groups(inspections),
        source_kind_counts=tuple(sorted(kind_counts.items())),
        diagnostic_counts=tuple(sorted(diagnostic_counts.items())),
        scope_statement=(
            "Complete for the registered sources in semantic source catalog "
            f"{semantic_source_catalog_digest} at the observed execution state; no claim is "
            "made about unconfigured or globally available 17Lands archives."
        ),
        maximum_header_field_count=max(field_counts) if field_counts else None,
        minimum_header_field_count=min(field_counts) if field_counts else None,
        maximum_header_byte_count=max(byte_counts) if byte_counts else None,
        max_header_bytes=max_header_bytes,
        max_header_fields=max_header_fields,
        max_header_field_chars=max_header_field_chars,
    )
