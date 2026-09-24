"""Crash-safe restart-and-restream orchestration over the M4.1 source reader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import cast

from openmtgdata.archive_registration import SourceArchiveRecordV1
from openmtgdata.config import RuntimeConfig
from openmtgdata.source_filename import RecognizedSourceFilename
from openmtgdata.source_reader import (
    BATCH_CONTRACT_ID,
    READER_CONFIG_CONTRACT_ID,
    READER_CONTRACT_ID,
    CompletionStatus,
    RawCsvBatchV1,
    SourceReaderConfigV1,
    SourceReaderError,
    SourceReaderSummaryV1,
    VerifiedSchemaRegistryV1,
    open_source_reader,
)

RUN_CONTRACT_ID = "openmtgdata.resumable-source-run.v1"
CHECKPOINT_CONTRACT_ID = "openmtgdata.source-reader-checkpoint.v1"
CHECKPOINT_DIGEST_CONTRACT_ID = "openmtgdata.source-reader-checkpoint-digest.v1"
RESUME_STRATEGY_ID = "openmtgdata.source-reader-resume-strategy.restream-v1"
UNIT_CONTRACT_ID = "openmtgdata.source-reader-unit.v1"
BATCH_SEMANTIC_DIGEST_CONTRACT_ID = "openmtgdata.source-reader-batch-semantic-digest.v1"
PREFIX_DIGEST_CONTRACT_ID = "openmtgdata.source-reader-prefix-digest.v1"
RESUMABLE_SUMMARY_CONTRACT_ID = "openmtgdata.resumable-reader-summary.v1"
M42_STAGE_CONTRACT_ID = "openmtgdata.resumable-source-stage.v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ROW_DIAGNOSTICS = {"ROW_WIDTH_SHORTER", "ROW_WIDTH_LONGER"}
_ACTIVE_PATHS: set[Path] = set()
_ACTIVE_LOCK = Lock()


class ResumableReaderError(RuntimeError):
    """Checkpoint, acknowledgement, or resume-prefix integrity failure."""


class SessionStatus(StrEnum):
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    ALREADY_COMPLETE = "already_complete"


@dataclass(frozen=True, slots=True)
class SourceReaderConfigIdentityV1:
    source_reader_config_contract_id: str
    canonical_config_bytes: bytes
    source_reader_config_digest: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_reader_config_identity(
    config: SourceReaderConfigV1,
) -> SourceReaderConfigIdentityV1:
    body = config.to_dict()
    encoded = _canonical_bytes(body)
    return SourceReaderConfigIdentityV1(
        READER_CONFIG_CONTRACT_ID,
        encoded,
        _sha256(
            _canonical_bytes(
                {
                    "contract_id": READER_CONFIG_CONTRACT_ID,
                    "config": body,
                }
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class ResumableRunIdentityV1:
    resumable_source_run_id: str
    resume_strategy_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    source_kind: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    schema_registry_digest: str
    source_reader_config_digest: str
    reader_contract_id: str
    stage_contract_id: str
    projection_bytes: bytes

    def projection_dict(self) -> dict[str, object]:
        value = json.loads(self.projection_bytes)
        if not isinstance(value, dict):
            raise ResumableReaderError("run identity projection is malformed")
        return value


def _build_run_identity(
    record: SourceArchiveRecordV1,
    registry: VerifiedSchemaRegistryV1,
    reader_summary: SourceReaderSummaryV1,
    config_identity: SourceReaderConfigIdentityV1,
) -> ResumableRunIdentityV1:
    if not isinstance(record.filename_result, RecognizedSourceFilename):
        raise ResumableReaderError("resumable reading requires a recognized source kind")
    projection = {
        "compressed_sha256": record.compressed_sha256,
        "compressed_size_bytes": record.compressed_size_bytes,
        "reader_contract_id": READER_CONTRACT_ID,
        "raw_schema_fingerprint": reader_summary.raw_schema_fingerprint,
        "resumable_source_run_contract_id": RUN_CONTRACT_ID,
        "resume_strategy_id": RESUME_STRATEGY_ID,
        "schema_registry_digest": registry.schema_registry_digest,
        "source_archive_id": record.source_archive_id,
        "source_archive_record_schema_id": record.source_archive_record_schema_id,
        "source_archive_id_contract_id": record.source_archive_id_contract_id,
        "source_interpretation_contract_id": reader_summary.source_interpretation_contract_id,
        "source_kind": record.filename_result.source_kind.value,
        "source_reader_config_digest": config_identity.source_reader_config_digest,
        "stage_contract_id": M42_STAGE_CONTRACT_ID,
    }
    digest = _sha256(_canonical_bytes(projection))
    run_id = f"{RUN_CONTRACT_ID}:{digest}"
    return ResumableRunIdentityV1(
        run_id,
        RESUME_STRATEGY_ID,
        record.source_archive_id,
        record.compressed_sha256,
        record.compressed_size_bytes,
        record.filename_result.source_kind.value,
        reader_summary.raw_schema_fingerprint,
        reader_summary.source_interpretation_contract_id,
        registry.schema_registry_digest,
        config_identity.source_reader_config_digest,
        READER_CONTRACT_ID,
        M42_STAGE_CONTRACT_ID,
        _canonical_bytes(projection),
    )


def _batch_semantic_digest(batch: RawCsvBatchV1) -> str:
    projection = {
        "accepted_records": [
            {
                "data_record_ordinal": item.data_record_ordinal,
                "fields": list(item.fields),
                "raw_schema_fingerprint": item.raw_schema_fingerprint,
                "raw_csv_record_contract_id": item.raw_csv_record_contract_id,
                "reader_contract_id": item.reader_contract_id,
                "source_archive_id": item.source_archive_id,
                "source_interpretation_contract_id": item.source_interpretation_contract_id,
            }
            for item in batch.accepted_records
        ],
        "batch_contract_id": batch.batch_contract_id,
        "batch_ordinal": batch.batch_ordinal,
        "first_data_record_ordinal": batch.first_data_record_ordinal,
        "last_data_record_ordinal": batch.last_data_record_ordinal,
        "raw_schema_fingerprint": batch.raw_schema_fingerprint,
        "records_accepted": batch.records_accepted,
        "records_rejected": batch.records_rejected,
        "records_seen": batch.records_seen,
        "record_diagnostics": [
            {
                "code": diagnostic.code.value,
                "data_record_ordinal": diagnostic.data_record_ordinal,
                "diagnostic_contract_id": diagnostic.diagnostic_contract_id,
                "expected_field_count": diagnostic.expected_field_count,
                "observed_field_count": diagnostic.observed_field_count,
                "source_archive_id": diagnostic.source_archive_id,
            }
            for diagnostic in batch.record_diagnostics
        ],
        "source_archive_id": batch.source_archive_id,
        "source_interpretation_contract_id": batch.source_interpretation_contract_id,
        "batch_semantic_digest_contract_id": BATCH_SEMANTIC_DIGEST_CONTRACT_ID,
    }
    return _sha256(_canonical_bytes(projection))


def _prefix_genesis() -> str:
    return _sha256(
        _canonical_bytes(
            {
                "domain": PREFIX_DIGEST_CONTRACT_ID,
                "state": "genesis",
            }
        )
    )


def _advance_prefix(previous: str, batch_digest: str) -> str:
    if not _SHA256_RE.fullmatch(previous) or not _SHA256_RE.fullmatch(batch_digest):
        raise ResumableReaderError("prefix chain input is malformed")
    return _sha256(
        _canonical_bytes(
            {
                "batch_semantic_digest": batch_digest,
                "previous_prefix_digest": previous,
                "prefix_digest_contract_id": PREFIX_DIGEST_CONTRACT_ID,
            }
        )
    )


def _unit_id(run_id: str, batch: RawCsvBatchV1, batch_digest: str) -> str:
    body = {
        "batch_ordinal": batch.batch_ordinal,
        "batch_semantic_digest": batch_digest,
        "first_data_record_ordinal": batch.first_data_record_ordinal,
        "last_data_record_ordinal": batch.last_data_record_ordinal,
        "records_accepted": batch.records_accepted,
        "records_rejected": batch.records_rejected,
        "records_seen": batch.records_seen,
        "resumable_source_run_id": run_id,
        "unit_contract_id": UNIT_CONTRACT_ID,
    }
    return f"{UNIT_CONTRACT_ID}:{_sha256(_canonical_bytes(body))}"


@dataclass(frozen=True, slots=True)
class ResumableReaderUnitV1:
    unit_contract_id: str
    unit_id: str
    batch_semantic_digest: str
    batch: RawCsvBatchV1


@dataclass(frozen=True, slots=True)
class CheckpointV1:
    checkpoint_contract_id: str
    checkpoint_digest_contract_id: str
    resumable_source_run_id: str
    resume_strategy_id: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    schema_registry_digest: str
    source_reader_config_digest: str
    reader_contract_id: str
    stage_contract_id: str
    committed_batch_ordinal: int
    last_committed_data_record_ordinal: int
    cumulative_records_seen: int
    cumulative_records_accepted: int
    cumulative_records_rejected: int
    cumulative_diagnostic_counts: tuple[tuple[str, int], ...]
    committed_prefix_digest: str
    completion_status: str
    last_committed_unit_id: str | None
    last_committed_batch_semantic_digest: str | None

    def semantic_dict(self) -> dict[str, object]:
        return {
            "checkpoint_contract_id": self.checkpoint_contract_id,
            "checkpoint_digest_contract_id": self.checkpoint_digest_contract_id,
            "committed_batch_ordinal": self.committed_batch_ordinal,
            "committed_prefix_digest": self.committed_prefix_digest,
            "completion_status": self.completion_status,
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "cumulative_diagnostic_counts": dict(self.cumulative_diagnostic_counts),
            "cumulative_records_accepted": self.cumulative_records_accepted,
            "cumulative_records_rejected": self.cumulative_records_rejected,
            "cumulative_records_seen": self.cumulative_records_seen,
            "last_committed_batch_semantic_digest": self.last_committed_batch_semantic_digest,
            "last_committed_data_record_ordinal": self.last_committed_data_record_ordinal,
            "last_committed_unit_id": self.last_committed_unit_id,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "reader_contract_id": self.reader_contract_id,
            "resumable_source_run_id": self.resumable_source_run_id,
            "resume_strategy_id": self.resume_strategy_id,
            "schema_registry_digest": self.schema_registry_digest,
            "source_archive_id": self.source_archive_id,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_reader_config_digest": self.source_reader_config_digest,
            "stage_contract_id": self.stage_contract_id,
        }

    @property
    def checkpoint_digest(self) -> str:
        return _sha256(_canonical_bytes(self.semantic_dict()))

    def document(self, write_sequence: int) -> bytes:
        return _canonical_bytes(
            {
                "audit": {"write_sequence": write_sequence},
                "checkpoint": self.semantic_dict(),
                "checkpoint_digest": self.checkpoint_digest,
            }
        )


def _zero_checkpoint(identity: ResumableRunIdentityV1) -> CheckpointV1:
    return CheckpointV1(
        CHECKPOINT_CONTRACT_ID,
        CHECKPOINT_DIGEST_CONTRACT_ID,
        identity.resumable_source_run_id,
        identity.resume_strategy_id,
        identity.source_archive_id,
        identity.compressed_sha256,
        identity.compressed_size_bytes,
        identity.raw_schema_fingerprint,
        identity.source_interpretation_contract_id,
        identity.schema_registry_digest,
        identity.source_reader_config_digest,
        identity.reader_contract_id,
        identity.stage_contract_id,
        0,
        0,
        0,
        0,
        0,
        (),
        _prefix_genesis(),
        "incomplete",
        None,
        None,
    )


_CHECKPOINT_FIELDS = frozenset(CheckpointV1.__dataclass_fields__)


def _checkpoint_from_document(
    path: Path,
    identity: ResumableRunIdentityV1,
) -> tuple[CheckpointV1, int]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict) or set(envelope) != {
            "audit",
            "checkpoint",
            "checkpoint_digest",
        }:
            raise ResumableReaderError("checkpoint envelope fields are invalid")
        body = envelope["checkpoint"]
        audit = envelope["audit"]
        if not isinstance(body, dict) or set(body) != _CHECKPOINT_FIELDS:
            raise ResumableReaderError("checkpoint fields are incomplete or unknown")
        if not isinstance(audit, dict) or set(audit) != {"write_sequence"}:
            raise ResumableReaderError("checkpoint audit fields are invalid")
        if (
            not isinstance(audit["write_sequence"], int)
            or isinstance(audit["write_sequence"], bool)
            or audit["write_sequence"] < 1
        ):
            raise ResumableReaderError("checkpoint write sequence is invalid")
        if envelope["checkpoint_digest"] != _sha256(_canonical_bytes(body)):
            raise ResumableReaderError("checkpoint digest mismatch")
        counts = body["cumulative_diagnostic_counts"]
        if not isinstance(counts, dict) or any(
            key not in _ROW_DIAGNOSTICS
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            for key, value in counts.items()
        ):
            raise ResumableReaderError("checkpoint diagnostic counts are invalid")
        checkpoint = CheckpointV1(
            checkpoint_contract_id=body["checkpoint_contract_id"],
            checkpoint_digest_contract_id=body["checkpoint_digest_contract_id"],
            resumable_source_run_id=body["resumable_source_run_id"],
            resume_strategy_id=body["resume_strategy_id"],
            source_archive_id=body["source_archive_id"],
            compressed_sha256=body["compressed_sha256"],
            compressed_size_bytes=body["compressed_size_bytes"],
            raw_schema_fingerprint=body["raw_schema_fingerprint"],
            source_interpretation_contract_id=body["source_interpretation_contract_id"],
            schema_registry_digest=body["schema_registry_digest"],
            source_reader_config_digest=body["source_reader_config_digest"],
            reader_contract_id=body["reader_contract_id"],
            stage_contract_id=body["stage_contract_id"],
            committed_batch_ordinal=body["committed_batch_ordinal"],
            last_committed_data_record_ordinal=body["last_committed_data_record_ordinal"],
            cumulative_records_seen=body["cumulative_records_seen"],
            cumulative_records_accepted=body["cumulative_records_accepted"],
            cumulative_records_rejected=body["cumulative_records_rejected"],
            cumulative_diagnostic_counts=tuple(sorted(counts.items())),
            committed_prefix_digest=body["committed_prefix_digest"],
            completion_status=body["completion_status"],
            last_committed_unit_id=body["last_committed_unit_id"],
            last_committed_batch_semantic_digest=body["last_committed_batch_semantic_digest"],
        )
    except ResumableReaderError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ResumableReaderError("checkpoint is malformed") from exc
    _validate_checkpoint(checkpoint, identity)
    if envelope["checkpoint_digest"] != checkpoint.checkpoint_digest:
        raise ResumableReaderError("checkpoint canonical digest does not match model")
    return checkpoint, audit["write_sequence"]


def _validate_checkpoint(checkpoint: CheckpointV1, identity: ResumableRunIdentityV1) -> None:
    if checkpoint.checkpoint_contract_id != CHECKPOINT_CONTRACT_ID:
        raise ResumableReaderError("unsupported checkpoint contract")
    if checkpoint.checkpoint_digest_contract_id != CHECKPOINT_DIGEST_CONTRACT_ID:
        raise ResumableReaderError("unsupported checkpoint digest contract")
    if checkpoint.completion_status != "incomplete":
        raise ResumableReaderError("checkpoint completion state is invalid")
    for field_name in (
        "resumable_source_run_id",
        "resume_strategy_id",
        "source_archive_id",
        "compressed_sha256",
        "raw_schema_fingerprint",
        "source_interpretation_contract_id",
        "schema_registry_digest",
        "source_reader_config_digest",
        "reader_contract_id",
        "stage_contract_id",
    ):
        if not isinstance(getattr(checkpoint, field_name), str):
            raise ResumableReaderError(f"checkpoint identity field is malformed: {field_name}")
    if checkpoint.resumable_source_run_id != identity.resumable_source_run_id:
        raise ResumableReaderError("checkpoint belongs to a different run identity")
    expected = identity.projection_dict()
    actual_fields = {
        "resume_strategy_id": checkpoint.resume_strategy_id,
        "source_archive_id": checkpoint.source_archive_id,
        "compressed_sha256": checkpoint.compressed_sha256,
        "compressed_size_bytes": checkpoint.compressed_size_bytes,
        "source_kind": expected["source_kind"],
        "raw_schema_fingerprint": checkpoint.raw_schema_fingerprint,
        "source_interpretation_contract_id": checkpoint.source_interpretation_contract_id,
        "schema_registry_digest": checkpoint.schema_registry_digest,
        "source_reader_config_digest": checkpoint.source_reader_config_digest,
        "reader_contract_id": checkpoint.reader_contract_id,
        "stage_contract_id": checkpoint.stage_contract_id,
    }
    for name, value in actual_fields.items():
        if expected.get(name) != value:
            raise ResumableReaderError(f"checkpoint immutable identity field mismatch: {name}")
    integer_fields = (
        checkpoint.compressed_size_bytes,
        checkpoint.committed_batch_ordinal,
        checkpoint.last_committed_data_record_ordinal,
        checkpoint.cumulative_records_seen,
        checkpoint.cumulative_records_accepted,
        checkpoint.cumulative_records_rejected,
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in integer_fields
    ):
        raise ResumableReaderError("checkpoint counters must be non-negative integers")
    if checkpoint.cumulative_records_seen != (
        checkpoint.cumulative_records_accepted + checkpoint.cumulative_records_rejected
    ):
        raise ResumableReaderError("checkpoint record counters do not reconcile")
    if checkpoint.cumulative_records_seen != checkpoint.last_committed_data_record_ordinal:
        raise ResumableReaderError("checkpoint ordinal and seen count disagree")
    counts = dict(checkpoint.cumulative_diagnostic_counts)
    if sum(counts.values()) != checkpoint.cumulative_records_rejected:
        raise ResumableReaderError("checkpoint diagnostics do not reconcile with rejections")
    if not _SHA256_RE.fullmatch(checkpoint.committed_prefix_digest):
        raise ResumableReaderError("checkpoint prefix digest is malformed")
    if checkpoint.committed_batch_ordinal == 0:
        if any(
            (
                checkpoint.last_committed_data_record_ordinal,
                checkpoint.cumulative_records_seen,
                checkpoint.cumulative_records_accepted,
                checkpoint.cumulative_records_rejected,
            )
        ):
            raise ResumableReaderError("zero checkpoint has nonzero progress")
        if checkpoint.committed_prefix_digest != _prefix_genesis():
            raise ResumableReaderError("zero checkpoint prefix is not the genesis digest")
        if checkpoint.last_committed_unit_id is not None or (
            checkpoint.last_committed_batch_semantic_digest is not None
        ):
            raise ResumableReaderError("zero checkpoint unexpectedly names a committed unit")
    else:
        if checkpoint.last_committed_data_record_ordinal == 0:
            raise ResumableReaderError("committed batch checkpoint has zero record ordinal")
        if (
            not isinstance(checkpoint.last_committed_unit_id, str)
            or not _SHA256_RE.fullmatch(
                checkpoint.last_committed_unit_id.rsplit(":", maxsplit=1)[-1]
            )
            or not checkpoint.last_committed_unit_id.startswith(f"{UNIT_CONTRACT_ID}:")
        ):
            raise ResumableReaderError("checkpoint last unit ID is malformed")
        if not isinstance(checkpoint.last_committed_batch_semantic_digest, str) or not (
            _SHA256_RE.fullmatch(checkpoint.last_committed_batch_semantic_digest)
        ):
            raise ResumableReaderError("checkpoint last batch digest is malformed")


@dataclass(frozen=True, slots=True)
class ResumableReaderSummaryV1:
    summary_contract_id: str
    resumable_source_run_id: str
    resume_strategy_id: str
    source_archive_id: str
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    schema_registry_digest: str
    source_reader_config_digest: str
    committed_batch_ordinal: int
    last_committed_data_record_ordinal: int
    records_seen: int
    records_accepted: int
    records_rejected: int
    diagnostic_counts: tuple[tuple[str, int], ...]
    committed_prefix_digest: str
    completion_status: SessionStatus
    terminal_reader_completion_status: str
    resume_count: int
    completion_digest: str | None


def _atomic_replace(
    path: Path,
    data: bytes,
    *,
    fault_hook: Callable[[str], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if fault_hook is not None:
            fault_hook("before_replace")
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Directory fsync is not available or supported on every platform.
            pass
        if fault_hook is not None:
            fault_hook("after_replace")
    except Exception:
        # Deliberately leave a crash-window temp file; it is never authoritative.
        raise


class ResumableSourceSessionV1:
    """One single-writer source session with explicit batch acknowledgement."""

    def __init__(
        self,
        record: SourceArchiveRecordV1,
        registry: VerifiedSchemaRegistryV1,
        runtime_config: RuntimeConfig,
        checkpoint_root: Path,
        reader_config: SourceReaderConfigV1,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.record = record
        self.registry = registry
        self.runtime_config = runtime_config
        self.reader_config = reader_config
        self.config_identity = source_reader_config_identity(reader_config)
        self.checkpoint_root = _validate_checkpoint_root(checkpoint_root, runtime_config, record)
        self.fault_hook = fault_hook
        self.reader = open_source_reader(record, registry, config=reader_config)
        self.identity: ResumableRunIdentityV1 | None = None
        self.run_directory: Path | None = None
        self.checkpoint_path: Path | None = None
        self.completion_path: Path | None = None
        self._lock_path: Path | None = None
        self._checkpoint: CheckpointV1 | None = None
        self._write_sequence = 0
        self._prefix = _prefix_genesis()
        self._diagnostic_counts: Counter[str] = Counter()
        self._seen = self._accepted = self._rejected = 0
        self._committed_batch_ordinal = 0
        self._last_ordinal = 0
        self._last_unit_id: str | None = None
        self._last_batch_digest: str | None = None
        self._outstanding: ResumableReaderUnitV1 | None = None
        self._complete_marker: dict[str, object] | None = None
        self._already_complete = False
        self._entered = False
        self._closed = False
        self._resume_count = 0
        self._reader_terminal = "not_verified"

    def __enter__(self) -> ResumableSourceSessionV1:
        if self._entered:
            raise ResumableReaderError("resumable session is single-use")
        self._entered = True
        self.reader.__enter__()
        try:
            summary = self.reader.summary
            self.identity = _build_run_identity(
                self.record, self.registry, summary, self.config_identity
            )
            self.run_directory = (
                self.checkpoint_root
                / (self.identity.resumable_source_run_id.split(":", maxsplit=1)[1])
            )
            self.checkpoint_path = self.run_directory / "checkpoint.json"
            self.completion_path = self.run_directory / "complete.json"
            self.checkpoint_root.mkdir(parents=True, exist_ok=True)
            _validate_resolved_under(self.checkpoint_root, self.runtime_config.intermediate_root)
            self._acquire_single_writer()
            self._bind_checkpoint_namespace()
            self.run_directory.mkdir(parents=True, exist_ok=True)
            _validate_resolved_under(self.run_directory, self.checkpoint_root)
            if self.completion_path.exists():
                self._complete_marker = _load_complete_marker(
                    self.completion_path, self.identity, self.checkpoint_path
                )
                self._already_complete = True
                self._reader_terminal = CompletionStatus.COMPLETE.value
                return self
            if self.checkpoint_path.exists():
                _validate_local_artifact(self.checkpoint_path, self.run_directory)
                checkpoint, sequence = _checkpoint_from_document(
                    self.checkpoint_path, self.identity
                )
                self._resume_count = 1
                self._write_sequence = sequence
                self._verify_committed_prefix(checkpoint)
                self._checkpoint = checkpoint
            else:
                self._checkpoint = _zero_checkpoint(self.identity)
            return self
        except Exception:
            self.reader.close()
            if self._lock_path is not None:
                with _ACTIVE_LOCK:
                    _ACTIVE_PATHS.discard(self._lock_path)
            self._closed = True
            raise

    def _acquire_single_writer(self) -> None:
        with _ACTIVE_LOCK:
            if self.checkpoint_root in _ACTIVE_PATHS:
                raise ResumableReaderError("another session is active for this checkpoint")
            _ACTIVE_PATHS.add(self.checkpoint_root)
            self._lock_path = self.checkpoint_root

    def _bind_checkpoint_namespace(self) -> None:
        assert self.identity is not None
        namespace_path = self.checkpoint_root / "run-identity.json"
        if namespace_path.exists():
            _validate_local_artifact(namespace_path, self.checkpoint_root)
            try:
                document = json.loads(namespace_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ResumableReaderError("checkpoint namespace identity is malformed") from exc
            if not isinstance(document, dict) or set(document) != {
                "identity",
                "identity_digest",
            }:
                raise ResumableReaderError("checkpoint namespace identity fields are invalid")
            projection = self.identity.projection_dict()
            if (
                document.get("identity") != projection
                or document.get("identity_digest") != self.identity.resumable_source_run_id
            ):
                raise ResumableReaderError(
                    "checkpoint namespace is bound to different source/config/schema inputs"
                )
            return
        projection = self.identity.projection_dict()
        _atomic_replace(
            namespace_path,
            _canonical_bytes(
                {
                    "identity": projection,
                    "identity_digest": self.identity.resumable_source_run_id,
                }
            ),
        )

    def _verify_committed_prefix(self, checkpoint: CheckpointV1) -> None:
        assert self.identity is not None
        if checkpoint.committed_batch_ordinal == 0:
            self._checkpoint = checkpoint
            return
        for expected_ordinal in range(1, checkpoint.committed_batch_ordinal + 1):
            try:
                batch = next(self.reader)
            except StopIteration as exc:
                raise ResumableReaderError(
                    "source ended before the checkpoint's committed batch boundary"
                ) from exc
            if batch.batch_ordinal != expected_ordinal:
                raise ResumableReaderError("re-streamed batch ordinal differs from checkpoint")
            unit = self._make_unit(batch)
            self._accumulate_committed(unit)
        if (
            self._committed_batch_ordinal != checkpoint.committed_batch_ordinal
            or self._last_ordinal != checkpoint.last_committed_data_record_ordinal
            or self._seen != checkpoint.cumulative_records_seen
            or self._accepted != checkpoint.cumulative_records_accepted
            or self._rejected != checkpoint.cumulative_records_rejected
            or tuple(sorted(self._diagnostic_counts.items()))
            != checkpoint.cumulative_diagnostic_counts
            or self._prefix != checkpoint.committed_prefix_digest
            or self._last_unit_id != checkpoint.last_committed_unit_id
            or self._last_batch_digest != checkpoint.last_committed_batch_semantic_digest
        ):
            raise ResumableReaderError("re-streamed committed prefix does not match checkpoint")

    def _make_unit(self, batch: RawCsvBatchV1) -> ResumableReaderUnitV1:
        assert self.identity is not None
        if batch.batch_contract_id != BATCH_CONTRACT_ID:
            raise ResumableReaderError("reader emitted an unsupported batch contract")
        semantic_digest = _batch_semantic_digest(batch)
        return ResumableReaderUnitV1(
            UNIT_CONTRACT_ID,
            _unit_id(self.identity.resumable_source_run_id, batch, semantic_digest),
            semantic_digest,
            batch,
        )

    def next_batch(self) -> ResumableReaderUnitV1 | None:
        self._require_open()
        if self._already_complete or self._complete_marker is not None:
            return None
        if self._outstanding is not None:
            raise ResumableReaderError("previous batch must be committed before requesting next")
        try:
            batch = next(self.reader)
        except StopIteration:
            reader_summary = self.reader.summary
            self._reader_terminal = reader_summary.completion_status.value
            if reader_summary.completion_status is not CompletionStatus.COMPLETE:
                raise ResumableReaderError(
                    "M4.1 reader did not verify complete source EOF"
                ) from None
            if self._checkpoint is None:
                assert self.identity is not None
                self._checkpoint = _zero_checkpoint(self.identity)
            if self.checkpoint_path is not None and not self.checkpoint_path.exists():
                self._persist_checkpoint(self._checkpoint, prior_ordinal=0)
            self._validate_terminal_counts(reader_summary)
            self._write_complete_marker(reader_summary)
            return None
        except SourceReaderError:
            # Fatal M4.1 errors never change the committed checkpoint or prefix.
            self._reader_terminal = CompletionStatus.FATAL_ERROR.value
            raise
        unit = self._make_unit(batch)
        self._outstanding = unit
        return unit

    def commit_batch(self, unit_id: str) -> CheckpointV1:
        self._require_open()
        unit = self._outstanding
        if unit is None:
            raise ResumableReaderError("there is no outstanding batch to commit")
        if unit.unit_id != unit_id:
            raise ResumableReaderError("unit ID does not match the outstanding batch")
        batch = unit.batch
        if batch.records_seen <= 0 or batch.last_data_record_ordinal is None:
            raise ResumableReaderError("empty semantic batches cannot be committed")
        prior = self._checkpoint
        if prior is None:
            raise ResumableReaderError("checkpoint state is unavailable")
        if batch.batch_ordinal != prior.committed_batch_ordinal + 1:
            raise ResumableReaderError("batch is not the next monotonic checkpoint unit")
        if batch.first_data_record_ordinal != prior.last_committed_data_record_ordinal + 1:
            raise ResumableReaderError("batch ordinal range does not follow committed prefix")
        new_prefix = _advance_prefix(self._prefix, unit.batch_semantic_digest)
        new_counts = Counter(self._diagnostic_counts)
        for diagnostic in batch.record_diagnostics:
            if diagnostic.code.value not in _ROW_DIAGNOSTICS:
                raise ResumableReaderError("unsupported diagnostic in commit-eligible batch")
            new_counts[diagnostic.code.value] += 1
        checkpoint = self._make_checkpoint(
            batch=batch,
            unit=unit,
            prefix=new_prefix,
            diagnostics=new_counts,
        )
        self._persist_checkpoint(checkpoint, prior_ordinal=prior.committed_batch_ordinal)
        self._checkpoint = checkpoint
        self._prefix = new_prefix
        self._diagnostic_counts = new_counts
        self._seen += batch.records_seen
        self._accepted += batch.records_accepted
        self._rejected += batch.records_rejected
        self._committed_batch_ordinal = batch.batch_ordinal
        self._last_ordinal = batch.last_data_record_ordinal
        self._last_unit_id = unit.unit_id
        self._last_batch_digest = unit.batch_semantic_digest
        self._outstanding = None
        return checkpoint

    def _make_checkpoint(
        self,
        *,
        batch: RawCsvBatchV1,
        unit: ResumableReaderUnitV1,
        prefix: str,
        diagnostics: Counter[str],
    ) -> CheckpointV1:
        assert self.identity is not None
        prior = self._checkpoint
        assert prior is not None
        seen = prior.cumulative_records_seen + batch.records_seen
        accepted = prior.cumulative_records_accepted + batch.records_accepted
        rejected = prior.cumulative_records_rejected + batch.records_rejected
        if seen != accepted + rejected or seen != batch.last_data_record_ordinal:
            raise ResumableReaderError("batch counters/ordinals do not reconcile")
        return CheckpointV1(
            CHECKPOINT_CONTRACT_ID,
            CHECKPOINT_DIGEST_CONTRACT_ID,
            self.identity.resumable_source_run_id,
            self.identity.resume_strategy_id,
            self.identity.source_archive_id,
            self.identity.compressed_sha256,
            self.identity.compressed_size_bytes,
            self.identity.raw_schema_fingerprint,
            self.identity.source_interpretation_contract_id,
            self.identity.schema_registry_digest,
            self.identity.source_reader_config_digest,
            self.identity.reader_contract_id,
            self.identity.stage_contract_id,
            batch.batch_ordinal,
            batch.last_data_record_ordinal,
            seen,
            accepted,
            rejected,
            tuple(sorted(diagnostics.items())),
            prefix,
            "incomplete",
            unit.unit_id,
            unit.batch_semantic_digest,
        )

    def _persist_checkpoint(self, checkpoint: CheckpointV1, *, prior_ordinal: int) -> None:
        assert self.checkpoint_path is not None
        initial_zero = checkpoint.committed_batch_ordinal == 0 and prior_ordinal == 0
        if not initial_zero and checkpoint.committed_batch_ordinal != prior_ordinal + 1:
            raise ResumableReaderError("checkpoint advancement must be exactly one batch")
        current_sequence = 0
        if self.checkpoint_path.exists():
            current, current_sequence = _checkpoint_from_document(
                self.checkpoint_path, self.identity_or_error()
            )
            if current.committed_batch_ordinal != prior_ordinal:
                raise ResumableReaderError("checkpoint changed or advanced concurrently")
            if self._checkpoint is not None and current.checkpoint_digest != (
                self._checkpoint.checkpoint_digest
            ):
                raise ResumableReaderError("checkpoint no longer matches this session")
        elif prior_ordinal != 0:
            raise ResumableReaderError("prior checkpoint disappeared")
        elif not initial_zero and self._checkpoint is None:
            raise ResumableReaderError("first checkpoint must follow a committed batch")
        _validate_checkpoint(checkpoint, self.identity_or_error())
        _atomic_replace(
            self.checkpoint_path,
            checkpoint.document(current_sequence + 1),
            fault_hook=self.fault_hook,
        )
        self._write_sequence = current_sequence + 1

    def _accumulate_committed(self, unit: ResumableReaderUnitV1) -> None:
        batch = unit.batch
        if batch.batch_ordinal != self._committed_batch_ordinal + 1:
            raise ResumableReaderError("prefix replay has a batch ordinal gap")
        if batch.records_seen != batch.records_accepted + batch.records_rejected:
            raise ResumableReaderError("replayed batch counters do not reconcile")
        if batch.first_data_record_ordinal != self._last_ordinal + 1:
            raise ResumableReaderError("replayed batch record ordinal has a gap")
        for diagnostic in batch.record_diagnostics:
            if diagnostic.code.value not in _ROW_DIAGNOSTICS:
                raise ResumableReaderError("unexpected diagnostic in committed prefix")
            self._diagnostic_counts[diagnostic.code.value] += 1
        self._seen += batch.records_seen
        self._accepted += batch.records_accepted
        self._rejected += batch.records_rejected
        self._last_ordinal = batch.last_data_record_ordinal or 0
        self._committed_batch_ordinal = batch.batch_ordinal
        self._prefix = _advance_prefix(self._prefix, unit.batch_semantic_digest)
        self._last_unit_id = unit.unit_id
        self._last_batch_digest = unit.batch_semantic_digest

    def _validate_terminal_counts(self, summary: SourceReaderSummaryV1) -> None:
        if (
            summary.records_seen != self._seen
            or summary.records_accepted != self._accepted
            or summary.records_rejected != self._rejected
            or tuple(sorted(self._diagnostic_counts.items())) != summary.diagnostic_counts
            or summary.records_seen != self._last_ordinal
        ):
            raise ResumableReaderError("terminal M4.1 counts differ from committed checkpoint")

    def _write_complete_marker(self, summary: SourceReaderSummaryV1) -> None:
        assert self.identity is not None
        assert self.checkpoint_path is not None
        assert self.completion_path is not None
        checkpoint = self._checkpoint
        if checkpoint is None:
            raise ResumableReaderError("completion requires a valid terminal checkpoint")
        if self._outstanding is not None:
            raise ResumableReaderError("completion cannot precede outstanding batch commit")
        semantic = {
            "completion_contract_id": RESUMABLE_SUMMARY_CONTRACT_ID,
            "committed_batch_ordinal": checkpoint.committed_batch_ordinal,
            "committed_prefix_digest": checkpoint.committed_prefix_digest,
            "compressed_bytes_verified": summary.compressed_bytes_verified,
            "diagnostic_counts": dict(checkpoint.cumulative_diagnostic_counts),
            "final_checkpoint_digest": checkpoint.checkpoint_digest,
            "final_data_record_ordinal": checkpoint.last_committed_data_record_ordinal,
            "gzip_integrity_status": summary.gzip_integrity_status,
            "records_accepted": checkpoint.cumulative_records_accepted,
            "records_rejected": checkpoint.cumulative_records_rejected,
            "records_seen": checkpoint.cumulative_records_seen,
            "resumable_source_run_id": self.identity.resumable_source_run_id,
            "source_archive_id": self.identity.source_archive_id,
            "terminal_m4_reader_completion_status": summary.completion_status.value,
            "terminal_status": SessionStatus.COMPLETE.value,
        }
        if summary.completion_status is not CompletionStatus.COMPLETE:
            raise ResumableReaderError("terminal source reader verification is incomplete")
        marker: dict[str, object] = {
            "completion": semantic,
            "completion_digest": _sha256(_canonical_bytes(semantic)),
        }
        _atomic_replace(
            self.completion_path,
            _canonical_bytes(marker),
            fault_hook=self.fault_hook,
        )
        self._complete_marker = marker
        self._reader_terminal = summary.completion_status.value

    def identity_or_error(self) -> ResumableRunIdentityV1:
        if self.identity is None:
            raise ResumableReaderError("run identity is not initialized")
        return self.identity

    def _require_open(self) -> None:
        if not self._entered or self._closed:
            raise ResumableReaderError("resumable session is not open")

    @property
    def status(self) -> SessionStatus:
        if self._already_complete:
            return SessionStatus.ALREADY_COMPLETE
        if self._complete_marker is not None:
            return SessionStatus.COMPLETE
        return SessionStatus.INCOMPLETE

    @property
    def summary(self) -> ResumableReaderSummaryV1:
        identity = self.identity_or_error()
        checkpoint = self._checkpoint or _zero_checkpoint(identity)
        return ResumableReaderSummaryV1(
            RESUMABLE_SUMMARY_CONTRACT_ID,
            identity.resumable_source_run_id,
            identity.resume_strategy_id,
            identity.source_archive_id,
            identity.raw_schema_fingerprint,
            identity.source_interpretation_contract_id,
            identity.schema_registry_digest,
            identity.source_reader_config_digest,
            checkpoint.committed_batch_ordinal,
            checkpoint.last_committed_data_record_ordinal,
            checkpoint.cumulative_records_seen,
            checkpoint.cumulative_records_accepted,
            checkpoint.cumulative_records_rejected,
            checkpoint.cumulative_diagnostic_counts,
            checkpoint.committed_prefix_digest,
            self.status,
            self._reader_terminal,
            self._resume_count,
            (
                cast(str, self._complete_marker["completion_digest"])
                if self._complete_marker is not None
                else None
            ),
        )

    @property
    def checkpoint_file(self) -> Path | None:
        return self.checkpoint_path

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.reader.close()
        finally:
            if self._lock_path is not None:
                with _ACTIVE_LOCK:
                    _ACTIVE_PATHS.discard(self._lock_path)
            self._closed = True

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _validate_resolved_under(path: Path, parent: Path) -> None:
    resolved = path.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    if not resolved.is_relative_to(resolved_parent):
        raise ResumableReaderError("checkpoint path resolves outside intermediate_root")


def _validate_checkpoint_root(
    checkpoint_root: Path,
    runtime_config: RuntimeConfig,
    record: SourceArchiveRecordV1,
) -> Path:
    if not checkpoint_root.is_absolute():
        raise ResumableReaderError("checkpoint root must be an explicit absolute path")
    if record.raw_root not in runtime_config.raw_roots:
        raise ResumableReaderError("source record raw root is outside RuntimeConfig")
    root = checkpoint_root.resolve(strict=False)
    intermediate = runtime_config.intermediate_root.resolve(strict=False)
    if not root.is_relative_to(intermediate):
        raise ResumableReaderError("checkpoint root must be beneath intermediate_root")
    for protected in (
        *runtime_config.raw_roots,
        runtime_config.quarantine_root,
        runtime_config.release_root,
    ):
        protected_resolved = protected.resolve(strict=False)
        if root == protected_resolved or root.is_relative_to(protected_resolved):
            raise ResumableReaderError("checkpoint root overlaps a protected filesystem root")
    return root


def _validate_local_artifact(path: Path, run_directory: Path) -> None:
    try:
        path.lstat()
    except OSError as exc:
        raise ResumableReaderError("checkpoint artifact cannot be inspected") from exc
    if path.is_symlink() or not path.is_file():
        raise ResumableReaderError("checkpoint artifact must be a regular non-symlink file")
    _validate_resolved_under(path, run_directory)


def _load_complete_marker(
    path: Path,
    identity: ResumableRunIdentityV1,
    checkpoint_path: Path,
) -> dict[str, object]:
    _validate_local_artifact(path, path.parent)
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
        semantic = marker["completion"]
        digest = marker["completion_digest"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ResumableReaderError("completion marker is malformed") from exc
    if not isinstance(semantic, dict) or set(marker) != {"completion", "completion_digest"}:
        raise ResumableReaderError("completion marker fields are invalid")
    expected_fields = {
        "completion_contract_id",
        "committed_batch_ordinal",
        "committed_prefix_digest",
        "compressed_bytes_verified",
        "diagnostic_counts",
        "final_checkpoint_digest",
        "final_data_record_ordinal",
        "gzip_integrity_status",
        "records_accepted",
        "records_rejected",
        "records_seen",
        "resumable_source_run_id",
        "source_archive_id",
        "terminal_m4_reader_completion_status",
        "terminal_status",
    }
    if set(semantic) != expected_fields:
        raise ResumableReaderError("completion marker semantic fields are invalid")
    if digest != _sha256(_canonical_bytes(semantic)):
        raise ResumableReaderError("completion marker digest mismatch")
    if semantic.get("completion_contract_id") != RESUMABLE_SUMMARY_CONTRACT_ID:
        raise ResumableReaderError("completion marker contract is unsupported")
    if semantic.get("resumable_source_run_id") != identity.resumable_source_run_id:
        raise ResumableReaderError("completion marker belongs to a different run")
    if semantic.get("terminal_status") != SessionStatus.COMPLETE.value:
        raise ResumableReaderError("completion marker is not complete")
    if semantic.get("terminal_m4_reader_completion_status") != CompletionStatus.COMPLETE.value:
        raise ResumableReaderError("completion marker lacks terminal M4.1 verification")
    if semantic.get("source_archive_id") != identity.source_archive_id:
        raise ResumableReaderError("completion marker source identity differs from run")
    if semantic.get("gzip_integrity_status") != "valid":
        raise ResumableReaderError("completion marker lacks gzip integrity verification")
    if semantic.get("compressed_bytes_verified") != identity.compressed_size_bytes:
        raise ResumableReaderError("completion marker compressed byte count differs from run")
    _validate_local_artifact(checkpoint_path, path.parent)
    checkpoint, _ = _checkpoint_from_document(checkpoint_path, identity)
    if semantic.get("final_checkpoint_digest") != checkpoint.checkpoint_digest:
        raise ResumableReaderError("completion marker does not bind the final checkpoint")
    if (
        semantic.get("committed_prefix_digest") != checkpoint.committed_prefix_digest
        or semantic.get("records_seen") != checkpoint.cumulative_records_seen
        or semantic.get("records_accepted") != checkpoint.cumulative_records_accepted
        or semantic.get("records_rejected") != checkpoint.cumulative_records_rejected
        or semantic.get("committed_batch_ordinal") != checkpoint.committed_batch_ordinal
        or semantic.get("final_data_record_ordinal")
        != checkpoint.last_committed_data_record_ordinal
        or semantic.get("diagnostic_counts") != dict(checkpoint.cumulative_diagnostic_counts)
    ):
        raise ResumableReaderError("completion marker progress differs from checkpoint")
    return dict(marker)


def open_resumable_source_session(
    record: SourceArchiveRecordV1,
    schema_registry: VerifiedSchemaRegistryV1,
    runtime_config: RuntimeConfig,
    *,
    checkpoint_root: Path,
    reader_config: SourceReaderConfigV1 | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> ResumableSourceSessionV1:
    """Create a one-source restart/restream session; only explicit commits persist progress."""
    return ResumableSourceSessionV1(
        record,
        schema_registry,
        runtime_config,
        checkpoint_root,
        reader_config or SourceReaderConfigV1(),
        fault_hook,
    )
