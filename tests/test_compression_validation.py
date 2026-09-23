"""Tests for gzip integrity evidence kept separate from byte registration."""

from __future__ import annotations

import csv
import gzip
from pathlib import Path

import pytest

import openmtgdata.compression_validation as compression_module
from openmtgdata.archive_registration import (
    ArchiveRegistrationResult,
    register_inventory_archives,
)
from openmtgdata.compression_validation import (
    COMPRESSION_VALIDATION_CONTRACT_ID,
    DECOMPRESSED_DISCARD_CHUNK_SIZE_BYTES,
    CompressionDiagnosticCode,
    CompressionValidationStatus,
    RegisteredSourceBytesMismatchError,
    not_checked_compression_evidence,
    validate_registered_compression,
    validate_registration_compression,
)
from openmtgdata.config import RuntimeConfig
from openmtgdata.inventory import inventory_raw_roots


def make_registration(
    tmp_path: Path, basename: str, compressed_bytes: bytes
) -> ArchiveRegistrationResult:
    base = tmp_path / "project"
    base.mkdir(parents=True, exist_ok=True)
    raw = base / "raw"
    raw.mkdir(exist_ok=True)
    (raw / basename).write_bytes(compressed_bytes)
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=base / "work" / "intermediate",
        quarantine_root=base / "work" / "quarantine",
        release_root=base / "work" / "release",
        base_dir=base,
    )
    return register_inventory_archives(inventory_raw_roots(config))


def test_valid_gzip_returns_evidence_bound_to_registered_source(tmp_path: Path) -> None:
    compressed = gzip.compress(b"synthetic gzip payload" * 100)
    registration = make_registration(
        tmp_path, "game_data_public.AFR.PremierDraft.csv.gz", compressed
    )

    evidence = validate_registered_compression(registration.records[0])

    assert evidence.contract_id == COMPRESSION_VALIDATION_CONTRACT_ID
    assert evidence.status is CompressionValidationStatus.VALID
    assert evidence.diagnostic_code is None
    assert evidence.reason is None
    assert evidence.source_archive_id == registration.records[0].source_archive_id
    assert evidence.compressed_sha256 == registration.records[0].compressed_sha256
    assert evidence.compressed_size_bytes == len(compressed)


@pytest.mark.parametrize(
    ("compressed", "expected_status", "expected_code"),
    [
        (
            b"not a gzip stream",
            CompressionValidationStatus.INVALID,
            CompressionDiagnosticCode.INVALID_GZIP_HEADER,
        ),
        (
            gzip.compress(b"truncated payload")[:-6],
            CompressionValidationStatus.INVALID,
            CompressionDiagnosticCode.TRUNCATED_GZIP,
        ),
        (
            bytes(
                bytearray(gzip.compress(b"crc payload"))[:-8]
                + bytes([gzip.compress(b"crc payload")[-8] ^ 1])
                + gzip.compress(b"crc payload")[-7:]
            ),
            CompressionValidationStatus.INVALID,
            CompressionDiagnosticCode.GZIP_INTEGRITY_FAILURE,
        ),
    ],
)
def test_invalid_or_truncated_gzip_is_diagnosed_without_changing_source_identity(
    tmp_path: Path,
    compressed: bytes,
    expected_status: CompressionValidationStatus,
    expected_code: CompressionDiagnosticCode,
) -> None:
    registration = make_registration(
        tmp_path, "game_data_public.AFR.PremierDraft.csv.gz", compressed
    )
    original_record = registration.records[0]
    original_record_bytes = original_record.canonical_bytes

    evidence = validate_registered_compression(original_record)

    assert evidence.status is expected_status
    assert evidence.diagnostic_code is expected_code
    assert evidence.source_archive_id == original_record.source_archive_id
    assert original_record.canonical_bytes == original_record_bytes


def test_not_checked_status_is_explicit() -> None:
    registration = make_registration_for_empty_fixture()
    record = registration.records[0]

    evidence = not_checked_compression_evidence(record)

    assert evidence.status is CompressionValidationStatus.NOT_CHECKED
    assert evidence.diagnostic_code is CompressionDiagnosticCode.NOT_CHECKED
    assert evidence.source_archive_id == record.source_archive_id


def make_registration_for_empty_fixture() -> ArchiveRegistrationResult:
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        base = Path(temp_dir) / "project"
        base.mkdir()
        raw = base / "raw"
        raw.mkdir()
        (raw / "x.csv.gz").write_bytes(b"not gzip")
        config = RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=base / "work" / "intermediate",
            quarantine_root=base / "work" / "quarantine",
            release_root=base / "work" / "release",
            base_dir=base,
        )
        return register_inventory_archives(inventory_raw_roots(config))


def test_gzip_validation_uses_bounded_discard_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = b"x" * (DECOMPRESSED_DISCARD_CHUNK_SIZE_BYTES * 3 + 17)
    registration = make_registration(
        tmp_path,
        "game_data_public.AFR.PremierDraft.csv.gz",
        gzip.compress(plain),
    )
    original_gzip_file = compression_module.gzip.GzipFile
    requested: list[int] = []
    returned: list[int] = []

    class GzipReadSpy:
        def __init__(self, **kwargs: object) -> None:
            self.inner = original_gzip_file(**kwargs)

        def __enter__(self) -> GzipReadSpy:
            self.inner.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.inner.__exit__(*args)

        def read(self, size: int) -> bytes:
            requested.append(size)
            data = self.inner.read(size)
            returned.append(len(data))
            return data

    monkeypatch.setattr(compression_module.gzip, "GzipFile", GzipReadSpy)

    evidence = validate_registered_compression(registration.records[0])

    assert evidence.status is CompressionValidationStatus.VALID
    assert len(returned) >= 4
    assert all(size == DECOMPRESSED_DISCARD_CHUNK_SIZE_BYTES for size in requested)
    assert max(returned) <= DECOMPRESSED_DISCARD_CHUNK_SIZE_BYTES
    assert sum(returned) == len(plain)


def test_gzip_validation_does_not_call_csv_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registration = make_registration(
        tmp_path,
        "game_data_public.AFR.PremierDraft.csv.gz",
        gzip.compress(b"column_a,column_b\nvalue_a,value_b\n"),
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("compression validation attempted CSV parsing")

    monkeypatch.setattr(csv, "reader", forbidden)
    monkeypatch.setattr(csv, "DictReader", forbidden)

    evidence = validate_registered_compression(registration.records[0])

    assert evidence.status is CompressionValidationStatus.VALID


def test_compression_validation_rejects_bytes_changed_since_registration(
    tmp_path: Path,
) -> None:
    registration = make_registration(
        tmp_path,
        "game_data_public.AFR.PremierDraft.csv.gz",
        gzip.compress(b"registered bytes"),
    )
    record = registration.records[0]
    path = record.raw_root.joinpath(*record.relative_path.parts)
    path.write_bytes(gzip.compress(b"different bytes"))

    with pytest.raises(RegisteredSourceBytesMismatchError):
        validate_registered_compression(record)


def test_registration_compression_batch_preserves_record_order(tmp_path: Path) -> None:
    config_base = tmp_path / "project"
    config_base.mkdir()
    raw = config_base / "raw"
    raw.mkdir()
    (raw / "b.csv.gz").write_bytes(gzip.compress(b"b"))
    (raw / "a.csv.gz").write_bytes(gzip.compress(b"a"))
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=config_base / "work" / "intermediate",
        quarantine_root=config_base / "work" / "quarantine",
        release_root=config_base / "work" / "release",
        base_dir=config_base,
    )
    registration = register_inventory_archives(inventory_raw_roots(config))

    evidence = validate_registration_compression(registration)

    assert tuple(item.source_archive_id for item in evidence) == tuple(
        record.source_archive_id for record in registration.records
    )
    assert all(item.status is CompressionValidationStatus.VALID for item in evidence)
