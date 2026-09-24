"""Bounded, forward-only raw gzip CSV reader (M4.1)."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, cast

from openmtgdata.archive_registration import (
    SOURCE_ARCHIVE_RECORD_SCHEMA_ID,
    ArchiveReadError,
    ArchiveRegistrationError,
    SourceArchiveRecordV1,
    _open_verified_archive,
    _verify_opened_archive_unchanged,
)
from openmtgdata.header_inspection import (
    CsvParserObservationV1,
    EncodingObservationV1,
    HeaderDiagnosticCode,
    HeaderInspectionError,
    _BoundedPhysicalLines,
    _canonical_bytes,
    csv_field_limit,
    fingerprint_raw_schema,
)
from openmtgdata.inventory import InventoryItem
from openmtgdata.schema_evolution import (
    CSV_PARSER_POLICY_ID,
    EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID,
    EXPECTED_M3_DEEP_METHOD_ID,
    SCHEMA_EVOLUTION_POLICY_ID,
    SCHEMA_REGISTRY_CONTRACT_ID,
    SCHEMA_REGISTRY_DIGEST_CONTRACT_ID,
    SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID,
    SchemaEvolutionError,
    SchemaEvolutionPolicyV1,
    derive_source_interpretation_contract_id,
)
from openmtgdata.source_container import (
    CsvSourcePayloadV1,
    SourceContainerError,
    open_csv_source_payload,
)
from openmtgdata.source_filename import RecognizedSourceFilename, SourceKind

READER_CONTRACT_ID = "openmtgdata.raw-source-reader.v2"
LOCATOR_CONTRACT_ID = "openmtgdata.source-record-locator.v1"
RAW_RECORD_CONTRACT_ID = "openmtgdata.raw-csv-record.v1"
BATCH_CONTRACT_ID = "openmtgdata.raw-csv-batch.v1"
DIAGNOSTIC_CONTRACT_ID = "openmtgdata.source-reader-diagnostic.v1"
SUMMARY_CONTRACT_ID = "openmtgdata.source-reader-summary.v1"
READER_CONFIG_CONTRACT_ID = "openmtgdata.source-reader-config.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SourceReaderError(RuntimeError):
    """Fatal source, CSV, registry, or integrity error."""

    def __init__(
        self,
        message: str,
        diagnostic: SourceReaderDiagnosticV1 | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class ReaderDiagnosticCode(StrEnum):
    EMPTY_ARCHIVE = "EMPTY_ARCHIVE"
    MISSING_HEADER = "MISSING_HEADER"
    HEADER_TOO_LARGE = "HEADER_TOO_LARGE"
    TOO_MANY_FIELDS = "TOO_MANY_FIELDS"
    FIELD_TOO_LARGE = "FIELD_TOO_LARGE"
    INVALID_UTF8 = "INVALID_UTF8"
    MALFORMED_CSV = "MALFORMED_CSV"
    ROW_WIDTH_SHORTER = "ROW_WIDTH_SHORTER"
    ROW_WIDTH_LONGER = "ROW_WIDTH_LONGER"
    INVALID_GZIP = "INVALID_GZIP"
    TRUNCATED_GZIP = "TRUNCATED_GZIP"
    GZIP_INTEGRITY_FAILURE = "GZIP_INTEGRITY_FAILURE"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    SOURCE_DIGEST_MISMATCH = "SOURCE_DIGEST_MISMATCH"
    READ_FAILURE = "READ_FAILURE"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    REGISTRY_MISMATCH = "REGISTRY_MISMATCH"
    LOGICAL_RECORD_TOO_LARGE = "LOGICAL_RECORD_TOO_LARGE"


class CompletionStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE_CONSUMER_STOP = "incomplete_consumer_stop"
    FATAL_ERROR = "fatal_error"


@dataclass(frozen=True, slots=True)
class SourceReaderConfigV1:
    max_logical_record_bytes: int = 8 * 1024 * 1024
    max_fields_per_record: int = 4096
    max_field_chars: int = 1024 * 1024
    max_records_per_batch: int = 256
    max_batch_payload_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.max_logical_record_bytes,
            self.max_fields_per_record,
            self.max_field_chars,
            self.max_records_per_batch,
            self.max_batch_payload_bytes,
        )
        if any(value <= 0 for value in values):
            raise ValueError("reader safety limits must be positive")
        if self.max_batch_payload_bytes < self.max_logical_record_bytes:
            raise ValueError("batch payload limit must fit one maximum-sized logical record")

    def to_dict(self) -> dict[str, int | str]:
        return {
            "config_contract_id": READER_CONFIG_CONTRACT_ID,
            "max_batch_payload_bytes": self.max_batch_payload_bytes,
            "max_field_chars": self.max_field_chars,
            "max_fields_per_record": self.max_fields_per_record,
            "max_logical_record_bytes": self.max_logical_record_bytes,
            "max_records_per_batch": self.max_records_per_batch,
        }


@dataclass(frozen=True, slots=True)
class VerifiedRegistryGroupV1:
    source_kind: SourceKind
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    member_raw_schema_fingerprints: tuple[str, ...]
    parser_policy_id: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.raw_schema_fingerprint):
            raise SchemaEvolutionError("verified group has malformed raw fingerprint")
        if not re.fullmatch(
            r"openmtgdata\.source-interpretation\.v1:[0-9a-f]{64}",
            self.source_interpretation_contract_id,
        ):
            raise SchemaEvolutionError("verified group has malformed interpretation ID")
        if self.raw_schema_fingerprint not in self.member_raw_schema_fingerprints:
            raise SchemaEvolutionError("verified group fingerprint is not a contract member")
        if self.parser_policy_id != CSV_PARSER_POLICY_ID:
            raise SchemaEvolutionError("verified group parser policy is unsupported")


@dataclass(frozen=True, slots=True)
class VerifiedSchemaRegistryV1:
    schema_registry_digest: str
    registry_contract_id: str
    registry_digest_contract_id: str
    policy_id: str
    groups: tuple[VerifiedRegistryGroupV1, ...]

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.schema_registry_digest):
            raise SchemaEvolutionError("verified registry digest is malformed")
        if self.registry_contract_id != SCHEMA_REGISTRY_CONTRACT_ID:
            raise SchemaEvolutionError("verified registry contract is unsupported")
        if self.registry_digest_contract_id != SCHEMA_REGISTRY_DIGEST_CONTRACT_ID:
            raise SchemaEvolutionError("verified registry digest contract is unsupported")
        if self.policy_id != SCHEMA_EVOLUTION_POLICY_ID:
            raise SchemaEvolutionError("verified registry policy is unsupported")
        keys = [(group.source_kind, group.raw_schema_fingerprint) for group in self.groups]
        if len(keys) != len(set(keys)):
            raise SchemaEvolutionError("verified registry contains duplicate groups")

    def lookup(self, source_kind: SourceKind, fingerprint: str) -> VerifiedRegistryGroupV1 | None:
        return next(
            (
                g
                for g in self.groups
                if g.source_kind is source_kind and g.raw_schema_fingerprint == fingerprint
            ),
            None,
        )


def load_verified_schema_registry(
    path: Path,
    *,
    expected_registry_digest: str | None = None,
    expected_source_catalog_digest: str | None = None,
    expected_m3_evidence_digest: str | None = None,
) -> VerifiedSchemaRegistryV1:
    """Validate registry contracts; optional caller pins bind a specific snapshot."""
    try:
        outer = json.loads(path.read_text(encoding="utf-8"))
        registry = outer["registry"]
        digest = outer["registry_digest"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SchemaEvolutionError("schema registry file is unreadable or malformed") from exc
    if not isinstance(registry, dict) or not isinstance(digest, str):
        raise SchemaEvolutionError("schema registry envelope has invalid types")
    if outer.get("registry_digest_contract_id") != SCHEMA_REGISTRY_DIGEST_CONTRACT_ID:
        raise SchemaEvolutionError("unsupported registry digest contract")
    if registry.get("registry_contract_id") != SCHEMA_REGISTRY_CONTRACT_ID:
        raise SchemaEvolutionError("unsupported schema registry contract")
    if registry.get("registry_digest_contract_id") != SCHEMA_REGISTRY_DIGEST_CONTRACT_ID:
        raise SchemaEvolutionError("registry projection has the wrong digest contract")
    computed = hashlib.sha256(_canonical_bytes(registry)).hexdigest()
    if not _SHA256_RE.fullmatch(digest) or computed != digest:
        raise SchemaEvolutionError("schema registry digest mismatch")
    source_catalog_digest = registry.get("semantic_source_catalog_digest")
    m3_evidence_digest = registry.get("deep_inspection_evidence_digest")
    if not isinstance(source_catalog_digest, str) or not _SHA256_RE.fullmatch(
        source_catalog_digest
    ):
        raise SchemaEvolutionError("registry source catalog digest is malformed")
    if not isinstance(m3_evidence_digest, str) or not _SHA256_RE.fullmatch(m3_evidence_digest):
        raise SchemaEvolutionError("registry M3.1 evidence digest is malformed")
    for label, expected, actual in (
        ("registry", expected_registry_digest, digest),
        ("source catalog", expected_source_catalog_digest, source_catalog_digest),
        ("M3 evidence", expected_m3_evidence_digest, m3_evidence_digest),
    ):
        if expected is not None:
            if not _SHA256_RE.fullmatch(expected):
                raise ValueError(f"expected {label} digest must be lowercase SHA-256 hex")
            if expected != actual:
                raise SchemaEvolutionError(f"registry does not match expected {label} digest")
    if registry.get("header_inventory_contract_id") != EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID:
        raise SchemaEvolutionError("registry has an unsupported header inventory contract")
    if registry.get("deep_inspection_method_id") != EXPECTED_M3_DEEP_METHOD_ID:
        raise SchemaEvolutionError("registry has an unsupported deep inspection method")
    policy = registry.get("policy")
    if not isinstance(policy, dict) or policy != SchemaEvolutionPolicyV1().to_dict():
        raise SchemaEvolutionError("unsupported schema evolution policy")
    if registry.get("source_interpretation_contract_schema_id") != (
        SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID
    ):
        raise SchemaEvolutionError("unsupported interpretation contract schema")
    raw_contracts = registry.get("interpretation_contracts")
    raw_groups = registry.get("groups")
    if not isinstance(raw_contracts, list) or not isinstance(raw_groups, list):
        raise SchemaEvolutionError("registry groups/contracts must be arrays")
    contracts: dict[str, dict[str, object]] = {}
    for contract in raw_contracts:
        if not isinstance(contract, dict):
            raise SchemaEvolutionError("interpretation contract must be an object")
        contract_id = contract.get("source_interpretation_contract_id")
        if contract.get("contract_schema_id") != SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID:
            raise SchemaEvolutionError("interpretation contract schema ID mismatch")
        projection = {
            key: value
            for key, value in contract.items()
            if key != "source_interpretation_contract_id"
        }
        try:
            expected_id = derive_source_interpretation_contract_id(projection)
        except (SchemaEvolutionError, TypeError) as exc:
            raise SchemaEvolutionError("interpretation contract projection is invalid") from exc
        if contract_id != expected_id or contract_id in contracts:
            raise SchemaEvolutionError("interpretation contract identity mismatch or collision")
        if contract.get("csv_parser_policy_id") != CSV_PARSER_POLICY_ID:
            raise SchemaEvolutionError("interpretation contract uses unsupported CSV policy")
        if contract.get("review_status") != "reviewed":
            raise SchemaEvolutionError("interpretation contract is not reviewed")
        if contract.get("source_kind") not in {kind.value for kind in SourceKind}:
            raise SchemaEvolutionError("interpretation contract has unknown source kind")
        members = contract.get("member_raw_schema_fingerprints")
        if (
            not isinstance(members, list)
            or not members
            or any(not isinstance(fp, str) or not _SHA256_RE.fullmatch(fp) for fp in members)
        ):
            raise SchemaEvolutionError("interpretation contract has invalid member fingerprints")
        if not isinstance(contract_id, str):
            raise SchemaEvolutionError("interpretation contract ID is missing")
        contracts[contract_id] = contract
    groups: list[VerifiedRegistryGroupV1] = []
    seen: set[tuple[str, str]] = set()
    for group in raw_groups:
        if not isinstance(group, dict):
            raise SchemaEvolutionError("registry group must be an object")
        kind_text = group.get("source_kind")
        fingerprint = group.get("raw_schema_fingerprint")
        if (
            kind_text not in {kind.value for kind in SourceKind}
            or not isinstance(fingerprint, str)
            or not _SHA256_RE.fullmatch(fingerprint)
        ):
            raise SchemaEvolutionError("registry group has invalid kind/fingerprint")
        key = (kind_text, fingerprint)
        if key in seen:
            raise SchemaEvolutionError("registry contains duplicate kind/fingerprint group")
        seen.add(key)
        disposition = group.get("disposition")
        if disposition not in {
            "supported_interpretation",
            "blocked_incompatible",
            "blocked_insufficient_evidence",
        }:
            raise SchemaEvolutionError("registry group has unknown disposition")
        if disposition != "supported_interpretation":
            if group.get("source_interpretation_contract_id") is not None:
                raise SchemaEvolutionError("blocked group unexpectedly references a contract")
            continue
        interpretation_id = group.get("source_interpretation_contract_id")
        contract = contracts.get(interpretation_id) if isinstance(interpretation_id, str) else None
        if contract is None:
            raise SchemaEvolutionError("supported group references missing interpretation contract")
        if contract.get("source_kind") != kind_text:
            raise SchemaEvolutionError("group and interpretation source kinds disagree")
        members = cast(list[str], contract["member_raw_schema_fingerprints"])
        if fingerprint not in members:
            raise SchemaEvolutionError("group fingerprint is absent from contract membership")
        if group.get("reference_raw_schema_fingerprint") != contract.get(
            "reference_raw_schema_fingerprint"
        ):
            raise SchemaEvolutionError("group and interpretation reference fingerprints disagree")
        groups.append(
            VerifiedRegistryGroupV1(
                source_kind=SourceKind(kind_text),
                raw_schema_fingerprint=fingerprint,
                source_interpretation_contract_id=cast(str, interpretation_id),
                member_raw_schema_fingerprints=tuple(members),
                parser_policy_id=str(contract["csv_parser_policy_id"]),
            )
        )
    if not groups:
        raise SchemaEvolutionError("registry has no supported groups")
    for contract_id, contract in contracts.items():
        kind = contract["source_kind"]
        members = cast(list[str], contract["member_raw_schema_fingerprints"])
        if any((kind, fp) not in seen for fp in members):
            raise SchemaEvolutionError("interpretation contract contains an unregistered member")
        if not any(
            group.get("source_interpretation_contract_id") == contract_id
            and group.get("disposition") == "supported_interpretation"
            for group in raw_groups
            if isinstance(group, dict)
        ):
            raise SchemaEvolutionError("unreferenced interpretation contract")
    return VerifiedSchemaRegistryV1(
        schema_registry_digest=digest,
        registry_contract_id=SCHEMA_REGISTRY_CONTRACT_ID,
        registry_digest_contract_id=SCHEMA_REGISTRY_DIGEST_CONTRACT_ID,
        policy_id=SCHEMA_EVOLUTION_POLICY_ID,
        groups=tuple(
            sorted(groups, key=lambda item: (item.source_kind.value, item.raw_schema_fingerprint))
        ),
    )


@dataclass(frozen=True, slots=True)
class SourceRecordLocatorV1:
    source_record_locator_contract_id: str
    source_archive_id: str
    data_record_ordinal: int


@dataclass(frozen=True, slots=True)
class RawCsvRecordV1:
    raw_csv_record_contract_id: str
    reader_contract_id: str
    source_archive_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    data_record_ordinal: int
    fields: tuple[str, ...]

    @property
    def locator(self) -> SourceRecordLocatorV1:
        return SourceRecordLocatorV1(
            LOCATOR_CONTRACT_ID, self.source_archive_id, self.data_record_ordinal
        )


@dataclass(frozen=True, slots=True)
class SourceReaderDiagnosticV1:
    diagnostic_contract_id: str
    code: ReaderDiagnosticCode
    source_archive_id: str
    original_filename: str
    data_record_ordinal: int | None
    disposition: str
    reason: str
    expected_field_count: int | None = None
    observed_field_count: int | None = None
    configured_limit: int | None = None


@dataclass(frozen=True, slots=True)
class RawCsvBatchV1:
    batch_contract_id: str
    reader_contract_id: str
    source_archive_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    schema_registry_digest: str
    batch_ordinal: int
    first_data_record_ordinal: int | None
    last_data_record_ordinal: int | None
    accepted_records: tuple[RawCsvRecordV1, ...]
    record_diagnostics: tuple[SourceReaderDiagnosticV1, ...]
    records_seen: int
    records_accepted: int
    records_rejected: int
    payload_utf8_bytes: int


@dataclass(frozen=True, slots=True)
class SourceReaderSummaryV1:
    summary_contract_id: str
    reader_contract_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    source_kind: SourceKind
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    schema_registry_digest: str
    header_fields: tuple[str, ...]
    records_seen: int
    records_accepted: int
    records_rejected: int
    diagnostic_counts: tuple[tuple[str, int], ...]
    batches_emitted: int
    completion_status: CompletionStatus
    compressed_bytes_verified: int
    gzip_integrity_status: str


class _HashingReader(io.RawIOBase):
    """Non-seekable compressed stream wrapper with bounded incremental identity."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise SourceReaderError("unbounded compressed reads are forbidden")
        data = self.stream.read(size)
        self.digest.update(data)
        self.bytes_read += len(data)
        return data

    def seekable(self) -> bool:
        return False

    def readable(self) -> bool:
        return True


class SourceReaderV1:
    """Context-managed one-pass reader. Exhaustion is required for verified completion."""

    def __init__(
        self,
        record: SourceArchiveRecordV1,
        registry: VerifiedSchemaRegistryV1,
        config: SourceReaderConfigV1 | None = None,
    ) -> None:
        self.record = record
        self.registry = registry
        self.config = config or SourceReaderConfigV1()
        self._stack = ExitStack()
        self._entered = False
        self._closed = False
        self._eof = False
        self._fatal = False
        self._compressed: BinaryIO | None = None
        self._hashing: _HashingReader | None = None
        self._gzip: gzip.GzipFile | None = None
        self._source_payload: CsvSourcePayloadV1 | None = None
        self._lines: _BoundedPhysicalLines | None = None
        self._csv: Iterator[list[str]] | None = None
        self._fields: tuple[str, ...] = ()
        self._raw_fingerprint = ""
        self._group: VerifiedRegistryGroupV1 | None = None
        self._next_ordinal = 1
        self._batch_ordinal = 1
        self._seen = 0
        self._accepted = 0
        self._rejected = 0
        self._batches = 0
        self._diagnostics: Counter[str] = Counter()
        self._completion = CompletionStatus.INCOMPLETE_CONSUMER_STOP
        self._verified_bytes = 0
        self._pending: tuple[tuple[str, ...], int, int] | None = None

    def __enter__(self) -> SourceReaderV1:
        if self._entered:
            raise SourceReaderError("reader is single-use")
        self._entered = True
        if self.record.source_archive_record_schema_id != SOURCE_ARCHIVE_RECORD_SCHEMA_ID:
            raise SourceReaderError("unsupported source archive record schema")
        if not isinstance(self.record.filename_result, RecognizedSourceFilename):
            raise SourceReaderError("source filename is unrecognized; source kind is required")
        self._group = None
        self._stack.enter_context(csv_field_limit(self.config.max_field_chars))
        item = InventoryItem(
            self.record.original_filename,
            self.record.raw_root,
            self.record.relative_path,
            self.record.filename_result,
        )
        try:
            self._compressed, before, opened = self._stack.enter_context(
                _open_verified_archive(item)
            )
            self._initial_snapshot = (item, before, opened)
            self._hashing = _HashingReader(self._compressed)
            self._gzip = self._stack.enter_context(
                gzip.GzipFile(fileobj=cast(BinaryIO, self._hashing), mode="rb")
            )
            try:
                self._source_payload = open_csv_source_payload(
                    self._gzip,
                    original_filename=self.record.original_filename,
                    source_kind=self.record.filename_result.source_kind.value,
                )
            except gzip.BadGzipFile as exc:
                raise SourceReaderError(ReaderDiagnosticCode.INVALID_GZIP.value) from exc
            except (EOFError, zlib.error) as exc:
                raise SourceReaderError(ReaderDiagnosticCode.TRUNCATED_GZIP.value) from exc
            self._stack.callback(self._source_payload.close)
            self._lines = _BoundedPhysicalLines(
                self._source_payload.stream,
                self.config.max_logical_record_bytes,
                self.config.max_fields_per_record,
            )
            self._csv = csv.reader(
                self._lines,
                delimiter=",",
                quotechar='"',
                doublequote=True,
                escapechar=None,
                strict=True,
            )
            self._fields = tuple(self._next_csv_record(header=True))
            encoding = EncodingObservationV1(
                encoding_used="utf-8-sig" if self._lines.bom_present else "utf-8",
                bom_present=self._lines.bom_present,
            )
            parser = CsvParserObservationV1(
                quote_character_observed=self._lines.quote_character_observed,
                doubled_quote_observed=self._lines.doubled_quote_observed,
            )
            _, self._raw_fingerprint = fingerprint_raw_schema(
                self._fields,
                encoding=encoding,
                parser_observation=parser,
                physical_line_endings=tuple(self._lines._line_endings),
            )
            if not _SHA256_RE.fullmatch(self._raw_fingerprint):
                raise SourceReaderError("computed raw schema fingerprint is malformed")
            source_kind = self.record.filename_result.source_kind
            group = self.registry.lookup(source_kind, self._raw_fingerprint)
            if group is None:
                raise SourceReaderError("actual header fingerprint is not supported by registry")
            if group.source_kind is not source_kind:
                raise SourceReaderError("registered filename source kind disagrees with registry")
            if group.parser_policy_id != CSV_PARSER_POLICY_ID:
                raise SourceReaderError("registry parser policy differs from reader policy")
            self._group = group
            return self
        except ArchiveRegistrationError as exc:
            self._fatal = True
            self._completion = CompletionStatus.FATAL_ERROR
            code = (
                ReaderDiagnosticCode.READ_FAILURE
                if isinstance(exc, ArchiveReadError)
                else ReaderDiagnosticCode.SOURCE_CHANGED
            )
            error = SourceReaderError(code.value)
            self._attach_fatal_diagnostic(None, error)
            self._stack.close()
            raise error from exc
        except Exception as exc:
            self._fatal = True
            self._completion = CompletionStatus.FATAL_ERROR
            self._attach_fatal_diagnostic(None, exc)
            self._stack.close()
            raise

    def _next_csv_record(self, *, header: bool = False) -> list[str]:
        if self._csv is None or self._lines is None:
            raise SourceReaderError("reader is not open")
        try:
            row = next(self._csv)
        except StopIteration:
            if header:
                code = (
                    ReaderDiagnosticCode.EMPTY_ARCHIVE
                    if self._lines.decompressed_bytes_read == 0
                    else ReaderDiagnosticCode.MISSING_HEADER
                )
                raise SourceReaderError(code.value) from None
            raise
        except HeaderInspectionError as exc:
            code_map = {
                HeaderDiagnosticCode.EMPTY_ARCHIVE: ReaderDiagnosticCode.EMPTY_ARCHIVE,
                HeaderDiagnosticCode.MISSING_HEADER: ReaderDiagnosticCode.MISSING_HEADER,
                HeaderDiagnosticCode.INVALID_GZIP: ReaderDiagnosticCode.INVALID_GZIP,
                HeaderDiagnosticCode.TRUNCATED_GZIP: ReaderDiagnosticCode.TRUNCATED_GZIP,
                HeaderDiagnosticCode.READ_FAILURE: ReaderDiagnosticCode.READ_FAILURE,
                HeaderDiagnosticCode.INVALID_ENCODING: ReaderDiagnosticCode.INVALID_UTF8,
                HeaderDiagnosticCode.HEADER_TOO_LARGE: (
                    ReaderDiagnosticCode.LOGICAL_RECORD_TOO_LARGE
                ),
                HeaderDiagnosticCode.TOO_MANY_HEADER_FIELDS: ReaderDiagnosticCode.TOO_MANY_FIELDS,
                HeaderDiagnosticCode.HEADER_FIELD_TOO_LARGE: ReaderDiagnosticCode.FIELD_TOO_LARGE,
                HeaderDiagnosticCode.CSV_HEADER_PARSE_ERROR: ReaderDiagnosticCode.MALFORMED_CSV,
            }
            raise SourceReaderError(
                code_map.get(exc.code, ReaderDiagnosticCode.READ_FAILURE).value
            ) from exc
        except csv.Error as exc:
            if "field larger than field limit" in str(exc):
                raise SourceReaderError(ReaderDiagnosticCode.FIELD_TOO_LARGE.value) from exc
            raise SourceReaderError(ReaderDiagnosticCode.MALFORMED_CSV.value) from exc
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise SourceReaderError(ReaderDiagnosticCode.GZIP_INTEGRITY_FAILURE.value) from exc
        except OSError as exc:
            raise SourceReaderError(ReaderDiagnosticCode.READ_FAILURE.value) from exc
        if len(row) > self.config.max_fields_per_record:
            raise SourceReaderError(ReaderDiagnosticCode.TOO_MANY_FIELDS.value)
        if any(len(value) > self.config.max_field_chars for value in row):
            raise SourceReaderError(ReaderDiagnosticCode.FIELD_TOO_LARGE.value)
        if self._lines._header_bytes > self.config.max_logical_record_bytes:
            raise SourceReaderError(ReaderDiagnosticCode.LOGICAL_RECORD_TOO_LARGE.value)
        return row

    @property
    def header_fields(self) -> tuple[str, ...]:
        return self._fields

    @property
    def raw_schema_fingerprint(self) -> str:
        return self._raw_fingerprint

    def __iter__(self) -> SourceReaderV1:
        return self

    def __next__(self) -> RawCsvBatchV1:
        if not self._entered or self._closed or self._eof:
            raise StopIteration
        if self._group is None or self._lines is None or self._csv is None:
            raise SourceReaderError("reader is not initialized")
        records: list[RawCsvRecordV1] = []
        diagnostics: list[SourceReaderDiagnosticV1] = []
        payload = 0
        seen = accepted = rejected = 0
        first: int | None = None
        last: int | None = None
        try:
            while seen < self.config.max_records_per_batch:
                if self._pending is not None:
                    fields_tuple, ordinal, record_payload = self._pending
                    fields = list(fields_tuple)
                    self._pending = None
                else:
                    self._lines.begin_next_record()
                    try:
                        fields = self._next_csv_record()
                    except StopIteration:
                        self._complete()
                        if not records and not diagnostics:
                            raise
                        break
                    ordinal = self._next_ordinal
                    record_payload = -1
                if len(fields) != len(self._fields):
                    short = len(fields) < len(self._fields)
                    code = (
                        ReaderDiagnosticCode.ROW_WIDTH_SHORTER
                        if short
                        else ReaderDiagnosticCode.ROW_WIDTH_LONGER
                    )
                    diagnostics.append(
                        SourceReaderDiagnosticV1(
                            DIAGNOSTIC_CONTRACT_ID,
                            code,
                            self.record.source_archive_id,
                            self.record.original_filename,
                            ordinal,
                            "rejected",
                            "CSV data record width differs from the registered header",
                            len(self._fields),
                            len(fields),
                            None,
                        )
                    )
                    rejected += 1
                    self._rejected += 1
                    self._diagnostics[code.value] += 1
                    self._next_ordinal += 1
                    seen += 1
                    self._seen += 1
                    first = ordinal if first is None else first
                    last = ordinal
                    continue
                if record_payload < 0:
                    record_payload = sum(len(value.encode("utf-8")) for value in fields)
                if record_payload > self.config.max_batch_payload_bytes:
                    raise SourceReaderError(ReaderDiagnosticCode.LOGICAL_RECORD_TOO_LARGE.value)
                if records and payload + record_payload > self.config.max_batch_payload_bytes:
                    # This record cannot be put back into csv.reader; retain it in a one-record
                    # pending slot so batch boundaries remain deterministic.
                    self._pending = (tuple(fields), ordinal, record_payload)
                    break
                records.append(
                    RawCsvRecordV1(
                        RAW_RECORD_CONTRACT_ID,
                        READER_CONTRACT_ID,
                        self.record.source_archive_id,
                        self._raw_fingerprint,
                        self._group.source_interpretation_contract_id,
                        ordinal,
                        tuple(fields),
                    )
                )
                payload += record_payload
                accepted += 1
                self._accepted += 1
                self._next_ordinal += 1
                seen += 1
                self._seen += 1
                first = ordinal if first is None else first
                last = ordinal
            if not records and not diagnostics:
                raise StopIteration
            return self._make_batch(
                tuple(records), tuple(diagnostics), seen, accepted, rejected, payload, first, last
            )
        except StopIteration:
            raise
        except Exception as exc:
            self._fatal = True
            self._completion = CompletionStatus.FATAL_ERROR
            self._attach_fatal_diagnostic(self._next_ordinal, exc)
            if isinstance(exc, SourceReaderError):
                raise
            raise SourceReaderError("source read failed") from exc

    def _attach_fatal_diagnostic(self, ordinal: int | None, exc: Exception | None = None) -> None:
        code: ReaderDiagnosticCode
        message = str(exc) if exc is not None else "source reader initialization failed"
        try:
            code = ReaderDiagnosticCode(message.split(":", maxsplit=1)[0])
        except ValueError:
            code = ReaderDiagnosticCode.READ_FAILURE
        if code in {
            ReaderDiagnosticCode.SOURCE_CHANGED,
            ReaderDiagnosticCode.SOURCE_DIGEST_MISMATCH,
            ReaderDiagnosticCode.GZIP_INTEGRITY_FAILURE,
            ReaderDiagnosticCode.INVALID_GZIP,
            ReaderDiagnosticCode.TRUNCATED_GZIP,
        }:
            ordinal = None
        if isinstance(exc, SourceReaderError) and exc.diagnostic is not None:
            return
        diagnostic = SourceReaderDiagnosticV1(
            DIAGNOSTIC_CONTRACT_ID,
            code,
            self.record.source_archive_id,
            self.record.original_filename,
            ordinal,
            "fatal",
            message,
        )
        self._diagnostics[code.value] += 1
        if isinstance(exc, SourceReaderError):
            exc.diagnostic = diagnostic

    def _make_batch(
        self,
        records: tuple[RawCsvRecordV1, ...],
        diagnostics: tuple[SourceReaderDiagnosticV1, ...],
        seen: int,
        accepted: int,
        rejected: int,
        payload: int,
        first: int | None,
        last: int | None,
    ) -> RawCsvBatchV1:
        assert self._group is not None
        batch = RawCsvBatchV1(
            BATCH_CONTRACT_ID,
            READER_CONTRACT_ID,
            self.record.source_archive_id,
            self._raw_fingerprint,
            self._group.source_interpretation_contract_id,
            self.registry.schema_registry_digest,
            self._batch_ordinal,
            first,
            last,
            records,
            diagnostics,
            seen,
            accepted,
            rejected,
            payload,
        )
        self._batch_ordinal += 1
        self._batches += 1
        return batch

    def _complete(self) -> None:
        if self._eof:
            return
        if self._source_payload is None:
            raise SourceReaderError("source container is unavailable at completion")
        try:
            self._source_payload.finish()
        except SourceContainerError as exc:
            raise SourceReaderError(
                "READ_FAILURE: source container failed integrity check"
            ) from exc
        if self._gzip is not None:
            self._gzip.close()
        if self._hashing is None or self._compressed is None:
            raise SourceReaderError("source stream is unavailable at completion")
        item, before, opened = self._initial_snapshot
        try:
            _verify_opened_archive_unchanged(
                item, self._compressed, before, opened, bytes_read=self._hashing.bytes_read
            )
        except ArchiveRegistrationError as exc:
            raise SourceReaderError(ReaderDiagnosticCode.SOURCE_CHANGED.value) from exc
        self._verified_bytes = self._hashing.bytes_read
        if (
            self._hashing.bytes_read != self.record.compressed_size_bytes
            or self._hashing.digest.hexdigest() != self.record.compressed_sha256
        ):
            raise SourceReaderError(ReaderDiagnosticCode.SOURCE_DIGEST_MISMATCH.value)
        self._eof = True
        self._completion = CompletionStatus.COMPLETE

    @property
    def summary(self) -> SourceReaderSummaryV1:
        if not self._entered or not isinstance(
            self.record.filename_result, RecognizedSourceFilename
        ):
            raise SourceReaderError("summary is unavailable before reader initialization")
        return SourceReaderSummaryV1(
            SUMMARY_CONTRACT_ID,
            READER_CONTRACT_ID,
            self.record.source_archive_id,
            self.record.compressed_sha256,
            self.record.compressed_size_bytes,
            self.record.filename_result.source_kind,
            self._raw_fingerprint,
            self._group.source_interpretation_contract_id if self._group else "",
            self.registry.schema_registry_digest,
            self._fields,
            self._seen,
            self._accepted,
            self._rejected,
            tuple(sorted(self._diagnostics.items())),
            self._batches,
            self._completion,
            self._verified_bytes,
            "valid" if self._completion is CompletionStatus.COMPLETE else "not_verified",
        )

    def close(self) -> None:
        if self._closed:
            return
        failure: SourceReaderError | None = None
        if not self._eof and not self._fatal and self._compressed is not None:
            try:
                item, before, opened = self._initial_snapshot
                _verify_opened_archive_unchanged(item, self._compressed, before, opened)
            except ArchiveRegistrationError:
                self._fatal = True
                self._completion = CompletionStatus.FATAL_ERROR
                failure = SourceReaderError(ReaderDiagnosticCode.SOURCE_CHANGED.value)
                self._attach_fatal_diagnostic(None, failure)
        if not self._eof and not self._fatal:
            self._completion = CompletionStatus.INCOMPLETE_CONSUMER_STOP
        self._stack.close()
        self._closed = True
        if failure is not None:
            raise failure

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc is not None:
            self._fatal = True
            self._completion = CompletionStatus.FATAL_ERROR
        self.close()


def open_source_reader(
    record: SourceArchiveRecordV1,
    schema_registry: VerifiedSchemaRegistryV1,
    *,
    config: SourceReaderConfigV1 | None = None,
) -> SourceReaderV1:
    """Create a lazy single-pass reader for one immutable registered archive."""
    return SourceReaderV1(record, schema_registry, config)
