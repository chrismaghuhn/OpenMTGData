from __future__ import annotations

import gzip
import json
import tracemalloc
from pathlib import Path

import pytest

from openmtgdata.archive_registration import register_inventory_archives
from openmtgdata.compression_validation import validate_registered_compression
from openmtgdata.config import RuntimeConfig
from openmtgdata.header_inspection import inspect_registered_archive
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.schema_evolution import SchemaEvolutionError
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    CompletionStatus,
    ReaderDiagnosticCode,
    SourceReaderConfigV1,
    SourceReaderError,
    VerifiedRegistryGroupV1,
    VerifiedSchemaRegistryV1,
    load_verified_schema_registry,
    open_source_reader,
)


def _config(tmp_path: Path) -> RuntimeConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "raw"
    raw.mkdir()
    return RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )


def _reader_fixture(tmp_path: Path, csv_bytes: bytes, *, batch_size: int = 2):
    config = _config(tmp_path)
    source = config.raw_roots[0] / "game_data_public.TST.PremierDraft.csv.gz"
    source.write_bytes(gzip.compress(csv_bytes))
    record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    header = inspect_registered_archive(
        record, compression_evidence=validate_registered_compression(record)
    ).header_evidence
    assert header is not None
    contract_id = "openmtgdata.source-interpretation.v1:" + "a" * 64
    registry = VerifiedSchemaRegistryV1(
        "b" * 64,
        "openmtgdata.schema-registry.v1",
        "openmtgdata.schema-registry-digest.v1",
        "openmtgdata.schema-evolution-policy.v1",
        (
            VerifiedRegistryGroupV1(
                SourceKind.GAME,
                header.raw_schema_fingerprint,
                contract_id,
                (header.raw_schema_fingerprint,),
                "openmtgdata.csv-header-policy.comma-utf8.v1",
            ),
        ),
    )
    return (
        record,
        registry,
        SourceReaderConfigV1(
            max_logical_record_bytes=1024,
            max_fields_per_record=32,
            max_field_chars=128,
            max_records_per_batch=batch_size,
            max_batch_payload_bytes=1024,
        ),
    )


def test_reader_preserves_csv_values_and_multiline_fields(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(
        tmp_path,
        b'a,b,c,d\r\n001,"a,b","line1\nline2","say ""hello"""\r\n ,+1,1.0,true\r\n',
    )
    with open_source_reader(record, registry, config=config) as reader:
        batches = list(reader)
        summary = reader.summary
    rows = [row for batch in batches for row in batch.accepted_records]
    assert rows[0].fields == ("001", "a,b", "line1\nline2", 'say "hello"')
    assert rows[1].fields == (" ", "+1", "1.0", "true")
    assert [row.data_record_ordinal for row in rows] == [1, 2]
    assert summary.completion_status is CompletionStatus.COMPLETE
    assert summary.records_seen == summary.records_accepted == 2


def test_batch_size_does_not_change_record_locators_or_values(tmp_path: Path) -> None:
    body = b"a,b\n1,001\n2,+1\n3,1.0\n4,true\n"
    first, registry, config_one = _reader_fixture(tmp_path / "one", body, batch_size=1)
    second, registry2, config_many = _reader_fixture(tmp_path / "two", body, batch_size=3)
    with open_source_reader(first, registry, config=config_one) as reader:
        rows_one = [r for batch in reader for r in batch.accepted_records]
    with open_source_reader(second, registry2, config=config_many) as reader:
        rows_many = [r for batch in reader for r in batch.accepted_records]
    assert [(r.data_record_ordinal, r.fields) for r in rows_one] == [
        (r.data_record_ordinal, r.fields) for r in rows_many
    ]


def test_width_errors_are_rejected_without_renumbering(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(tmp_path, b"a,b\n1,2\n3,4,5\n6\n7,8\n", batch_size=2)
    with open_source_reader(record, registry, config=config) as reader:
        batches = list(reader)
        summary = reader.summary
    accepted = [row.data_record_ordinal for batch in batches for row in batch.accepted_records]
    diagnostics = [d for batch in batches for d in batch.record_diagnostics]
    assert accepted == [1, 4]
    assert [(d.data_record_ordinal, d.code) for d in diagnostics] == [
        (2, ReaderDiagnosticCode.ROW_WIDTH_LONGER),
        (3, ReaderDiagnosticCode.ROW_WIDTH_SHORTER),
    ]
    assert (summary.records_seen, summary.records_accepted, summary.records_rejected) == (4, 2, 2)


def test_duplicate_and_empty_header_positions_are_preserved(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(tmp_path, b"a,,a\n1,2,3\n")
    with open_source_reader(record, registry, config=config) as reader:
        batches = list(reader)
        assert reader.header_fields == ("a", "", "a")
    assert batches[0].accepted_records[0].fields == ("1", "2", "3")


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"a,b\n\xff,1\n", ReaderDiagnosticCode.INVALID_UTF8),
        (b'a,b\n"unterminated,1', ReaderDiagnosticCode.MALFORMED_CSV),
    ],
)
def test_fatal_parser_errors_do_not_complete(
    tmp_path: Path, content: bytes, code: ReaderDiagnosticCode
) -> None:
    record, registry, config = _reader_fixture(tmp_path, content)
    with open_source_reader(record, registry, config=config) as reader:
        with pytest.raises(SourceReaderError, match=code.value) as caught:
            list(reader)
        assert reader.summary.completion_status is CompletionStatus.FATAL_ERROR
        assert caught.value.diagnostic is not None
        assert caught.value.diagnostic.data_record_ordinal == 1
        assert b"\xff" not in caught.value.diagnostic.reason.encode()


def test_early_consumer_stop_is_not_complete(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(tmp_path, b"a,b\n1,2\n3,4\n")
    with open_source_reader(record, registry, config=config) as reader:
        next(reader)
        assert reader.summary.completion_status is CompletionStatus.INCOMPLETE_CONSUMER_STOP


def test_registry_loader_verifies_persisted_registry(tmp_path: Path) -> None:
    registry_path = Path("data/intermediate/m3.2/schema-registry.json")
    if not registry_path.exists():
        pytest.skip("local ignored M3.2 registry is unavailable")
    registry = load_verified_schema_registry(
        registry_path,
        expected_registry_digest=(
            "7d3ef3af4304edd9a3aff88aee49f76e5e4c50b2908608b8f0b9e44a3f9fa1fb"
        ),
        expected_source_catalog_digest=(
            "dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602"
        ),
        expected_m3_evidence_digest=(
            "2572d8825d5eff17ad779695102f97d1ebb04b607545b02d1001d48461744add"
        ),
    )
    assert (
        registry.schema_registry_digest
        == "7d3ef3af4304edd9a3aff88aee49f76e5e4c50b2908608b8f0b9e44a3f9fa1fb"
    )
    assert len(registry.groups) == 77

    parsed = json.loads(registry_path.read_text(encoding="utf-8"))
    parsed["registry"]["policy"]["policy_id"] = "tampered"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(parsed), encoding="utf-8")
    with pytest.raises(SchemaEvolutionError):
        load_verified_schema_registry(bad)

    with pytest.raises(SchemaEvolutionError, match="expected registry digest"):
        load_verified_schema_registry(registry_path, expected_registry_digest="0" * 64)


def test_configuration_limits_are_validated() -> None:
    with pytest.raises(ValueError):
        SourceReaderConfigV1(max_batch_payload_bytes=10, max_logical_record_bytes=11)


def test_batch_payload_limit_flushes_before_overflow(tmp_path: Path) -> None:
    record, registry, _ = _reader_fixture(tmp_path, b"a,b\n1234,5678\nabcd,efgh\nlast,row!\n")
    config = SourceReaderConfigV1(
        max_logical_record_bytes=10,
        max_fields_per_record=8,
        max_field_chars=32,
        max_records_per_batch=20,
        max_batch_payload_bytes=10,
    )
    with open_source_reader(record, registry, config=config) as reader:
        batches = list(reader)
    assert [batch.payload_utf8_bytes for batch in batches] == [8, 8, 8]
    assert all(batch.payload_utf8_bytes <= config.max_batch_payload_bytes for batch in batches)


def _stream_peak(tmp_path: Path, row_count: int) -> int:
    body = b"a,b\n" + b"x,y\n" * row_count
    record, registry, _ = _reader_fixture(tmp_path, body, batch_size=32)
    config = SourceReaderConfigV1(
        max_logical_record_bytes=128,
        max_fields_per_record=8,
        max_field_chars=32,
        max_records_per_batch=32,
        max_batch_payload_bytes=128,
    )
    tracemalloc.start()
    try:
        with open_source_reader(record, registry, config=config) as reader:
            for batch in reader:
                assert len(batch.accepted_records) <= config.max_records_per_batch
                assert batch.payload_utf8_bytes <= config.max_batch_payload_bytes
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_streaming_memory_is_bounded_by_batches_not_total_rows(tmp_path: Path) -> None:
    small_peak = _stream_peak(tmp_path / "small", 500)
    large_peak = _stream_peak(tmp_path / "large", 10_000)
    assert large_peak < small_peak * 4 + 512_000


def test_registered_bytes_mismatch_fails_after_streaming(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(tmp_path, b"a,b\n1,2\n")
    source = record.raw_root / Path(record.relative_path)
    source.write_bytes(gzip.compress(b"a,b\n9,8\n"))
    with open_source_reader(record, registry, config=config) as reader:
        with pytest.raises(SourceReaderError, match="SOURCE_DIGEST_MISMATCH") as caught:
            list(reader)
        assert caught.value.diagnostic is not None
        assert caught.value.diagnostic.data_record_ordinal is None
        assert reader.summary.completion_status is CompletionStatus.FATAL_ERROR


def test_unknown_actual_header_fingerprint_fails_closed(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(tmp_path, b"a,b\n1,2\n")
    source = record.raw_root / Path(record.relative_path)
    source.write_bytes(gzip.compress(b"different,header\n1,2\n"))
    with pytest.raises(SourceReaderError, match="actual header fingerprint"):
        with open_source_reader(record, registry, config=config):
            pass


def test_invalid_gzip_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = config.raw_roots[0] / "game_data_public.TST.PremierDraft.csv.gz"
    source.write_bytes(b"not a gzip stream")
    record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    registry = VerifiedSchemaRegistryV1(
        "b" * 64,
        "openmtgdata.schema-registry.v1",
        "openmtgdata.schema-registry-digest.v1",
        "openmtgdata.schema-evolution-policy.v1",
        (),
    )
    with pytest.raises(SourceReaderError, match="INVALID_GZIP") as caught:
        with open_source_reader(record, registry):
            pass
    assert caught.value.diagnostic is not None
    assert caught.value.diagnostic.data_record_ordinal is None


def test_reader_does_not_mutate_registered_source(tmp_path: Path) -> None:
    record, registry, config = _reader_fixture(tmp_path, b"a,b\n1,2\n")
    source = record.raw_root / Path(record.relative_path)
    before = source.read_bytes()
    with open_source_reader(record, registry, config=config) as reader:
        list(reader)
    assert source.read_bytes() == before


@pytest.mark.parametrize("damage", ["truncated", "crc"])
def test_gzip_tail_damage_cannot_complete_reader(tmp_path: Path, damage: str) -> None:
    body = b"a,b\n" + b"x,y\n" * 20_000
    record, registry, config = _reader_fixture(tmp_path, body, batch_size=8)
    source = record.raw_root / Path(record.relative_path)
    compressed = source.read_bytes()
    assert len(compressed) > 8
    damaged = (
        compressed[:-8]
        if damage == "truncated"
        else (compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:])
    )
    source.write_bytes(damaged)

    reader = open_source_reader(record, registry, config=config)
    accepted_before_failure = 0
    with reader:
        try:
            for batch in reader:
                accepted_before_failure += batch.records_accepted
        except SourceReaderError:
            pass
        assert accepted_before_failure > 0
        assert reader.summary.completion_status is CompletionStatus.FATAL_ERROR
        assert reader.summary.gzip_integrity_status != "valid"


def test_source_mutation_while_reader_is_active_fails_closed(tmp_path: Path) -> None:
    body = b"a,b\n" + b"x,y\n" * 1_000
    record, registry, config = _reader_fixture(tmp_path, body, batch_size=2)
    source = record.raw_root / Path(record.relative_path)
    reader = open_source_reader(record, registry, config=config)
    with reader:
        first = next(reader)
        assert first.records_accepted == 2
        source.write_bytes(gzip.compress(b"a,b\n" + b"z,q\n" * 1_000))
        with pytest.raises(SourceReaderError):
            list(reader)
        assert reader.summary.completion_status is CompletionStatus.FATAL_ERROR
