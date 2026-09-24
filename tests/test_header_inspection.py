from __future__ import annotations

import csv
import gzip
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import openmtgdata.header_inspection as header_module
from openmtgdata.archive_registration import (
    ArchiveChangedDuringRegistrationError,
    register_inventory_archives,
)
from openmtgdata.compression_validation import (
    validate_registered_compression,
    validate_registration_compression,
)
from openmtgdata.config import RuntimeConfig
from openmtgdata.header_inspection import (
    HEADER_INSPECTION_METHOD_ID,
    HEADER_INVENTORY_CONTRACT_ID,
    MAX_HEADER_BYTES,
    MAX_HEADER_FIELD_CHARS,
    MAX_HEADER_FIELDS,
    RAW_SCHEMA_FINGERPRINT_CONTRACT_ID,
    SOURCE_INSPECTION_SCHEMA_ID,
    HeaderDiagnosticCode,
    HeaderInspectionExecutionError,
    HeaderInspectionStatus,
    NullabilityEvidenceStatus,
    TypeEvidenceStatus,
    build_header_inventory,
    fingerprint_raw_schema,
    inspect_registered_archive,
    raw_schema_fingerprint_projection_bytes,
)
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.source_manifest import build_source_manifest


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


def _registered(
    tmp_path: Path,
    name: str,
    uncompressed: bytes,
) -> tuple[RuntimeConfig, object]:
    config = _config(tmp_path)
    (config.raw_roots[0] / name).write_bytes(gzip.compress(uncompressed))
    registration = register_inventory_archives(inventory_raw_roots(config))
    return config, registration.records[0]


def _inspect(
    tmp_path: Path,
    content: bytes,
    *,
    name: str = "game_data_public.AFR.PremierDraft.csv.gz",
    **kwargs,
):
    _, record = _registered(tmp_path, name, content)
    return inspect_registered_archive(
        record,
        compression_evidence=validate_registered_compression(record),
        **kwargs,
    )


def _manifest(registration):
    return build_source_manifest(
        registration,
        compression_evidence=validate_registration_compression(registration),
    )


def test_contract_ids_and_basic_header_observation(tmp_path: Path) -> None:
    inspection = _inspect(tmp_path, b"game_id,turn,action\r\n1,2,play\r\n")

    assert SOURCE_INSPECTION_SCHEMA_ID == "openmtgdata.source-inspection.v1"
    assert HEADER_INSPECTION_METHOD_ID == "openmtgdata.header-inspection.v1"
    assert HEADER_INVENTORY_CONTRACT_ID == "openmtgdata.header-inventory.v1"
    assert RAW_SCHEMA_FINGERPRINT_CONTRACT_ID == "openmtgdata.raw-schema-fingerprint.v1"
    assert inspection.status is HeaderInspectionStatus.SUCCESS
    assert inspection.header_evidence is not None
    assert inspection.header_evidence.ordered_fields == ("game_id", "turn", "action")
    assert inspection.header_evidence.field_count == 3
    assert inspection.header_evidence.physical_line_endings == ("CRLF",)
    assert inspection.evidence_scope == (
        "header_only",
        "first_csv_record",
        "no_data_rows_interpreted",
        "gzip_integrity_valid",
    )
    assert inspection.type_evidence_status is TypeEvidenceStatus.NOT_INSPECTED
    assert inspection.type_evidence is None
    assert inspection.nullability_evidence_status is NullabilityEvidenceStatus.NOT_INSPECTED
    assert inspection.nullability_evidence is None
    assert inspection.source_interpretation_contract_id is None


@pytest.mark.parametrize(
    ("payload", "fields", "line_endings"),
    [
        (b"a,b,c\n", ("a", "b", "c"), ("LF",)),
        (b'a,"b,c",d\n', ("a", "b,c", "d"), ("LF",)),
        (b'"a""b",c\n', ('a"b', "c"), ("LF",)),
        (b'"a\nb",c\n', ("a\nb", "c"), ("LF", "LF")),
        (b"a,b\r", ("a", "b"), ("CR",)),
    ],
)
def test_csv_header_grammar_cases(
    tmp_path: Path,
    payload: bytes,
    fields: tuple[str, ...],
    line_endings: tuple[str, ...],
) -> None:
    inspection = _inspect(tmp_path, payload)
    assert inspection.header_evidence is not None
    assert inspection.header_evidence.ordered_fields == fields
    assert inspection.header_evidence.physical_line_endings == line_endings


def test_utf8_bom_is_observed_and_fields_are_not_normalized(tmp_path: Path) -> None:
    inspection = _inspect(tmp_path, b"\xef\xbb\xbf Game_ID ,Turn\n")

    assert inspection.header_evidence is not None
    assert inspection.header_evidence.ordered_fields == (" Game_ID ", "Turn")
    assert inspection.header_evidence.encoding.encoding_used == "utf-8-sig"
    assert inspection.header_evidence.encoding.bom_present is True
    assert inspection.header_evidence.encoding.authoritative_declaration is None


def test_observed_header_quote_syntax_is_physical_evidence(tmp_path: Path) -> None:
    unquoted = _inspect(tmp_path / "unquoted", b"a,b\n")
    quoted = _inspect(tmp_path / "quoted", b'"a",b\n')

    assert unquoted.header_evidence is not None and quoted.header_evidence is not None
    assert unquoted.header_evidence.ordered_fields == quoted.header_evidence.ordered_fields
    assert (
        unquoted.header_evidence.raw_schema_fingerprint
        != quoted.header_evidence.raw_schema_fingerprint
    )
    assert quoted.header_evidence.parser_observation.quote_character_observed is True


def test_duplicate_and_empty_fields_are_preserved_and_diagnosed_as_warnings(
    tmp_path: Path,
) -> None:
    duplicate = _inspect(tmp_path / "duplicate", b"a,b,a\n")
    empty = _inspect(tmp_path / "empty", b"a,,c\n")

    assert duplicate.status is HeaderInspectionStatus.SUCCESS
    assert duplicate.header_evidence is not None
    assert duplicate.header_evidence.ordered_fields == ("a", "b", "a")
    assert duplicate.header_evidence.duplicate_field_names == ("a",)
    assert empty.header_evidence is not None
    assert empty.header_evidence.ordered_fields == ("a", "", "c")
    assert empty.header_evidence.empty_field_positions == (1,)
    assert duplicate.diagnostics == (HeaderDiagnosticCode.DUPLICATE_HEADER_FIELD,)
    assert empty.diagnostics == (HeaderDiagnosticCode.EMPTY_HEADER_FIELD,)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"", HeaderDiagnosticCode.EMPTY_ARCHIVE),
        (b"\n", HeaderDiagnosticCode.MISSING_HEADER),
        (b'""\n', HeaderDiagnosticCode.MISSING_HEADER),
        (b"\xff,x\n", HeaderDiagnosticCode.INVALID_ENCODING),
        (b'"unterminated,x\n', HeaderDiagnosticCode.CSV_HEADER_PARSE_ERROR),
    ],
)
def test_unsupported_header_forms_have_stable_diagnostics(
    tmp_path: Path,
    payload: bytes,
    expected: HeaderDiagnosticCode,
) -> None:
    inspection = _inspect(tmp_path, payload)
    assert inspection.status is HeaderInspectionStatus.UNSUPPORTED
    assert inspection.diagnostics == (expected,)
    assert inspection.header_evidence is None


def test_header_safety_limits_are_explicit_and_enforced(tmp_path: Path) -> None:
    field = _inspect(tmp_path / "field", b"a" * 40 + b",b\n", max_header_field_chars=16)
    fields = _inspect(
        tmp_path / "fields",
        b",".join(f"f{i}".encode() for i in range(8)) + b"\n",
        max_header_fields=4,
    )
    header_bytes = _inspect(
        tmp_path / "bytes",
        b"a,b,c,d,e,f,g,h,i,j\n",
        max_header_bytes=8,
    )

    assert field.diagnostics == (HeaderDiagnosticCode.HEADER_FIELD_TOO_LARGE,)
    assert fields.diagnostics == (HeaderDiagnosticCode.TOO_MANY_HEADER_FIELDS,)
    assert header_bytes.diagnostics == (HeaderDiagnosticCode.HEADER_TOO_LARGE,)
    assert MAX_HEADER_BYTES > 0 and MAX_HEADER_FIELDS > 0 and MAX_HEADER_FIELD_CHARS > 0


def test_field_count_limit_stops_comma_only_header_before_materializing_all_fields(
    tmp_path: Path,
) -> None:
    inspection = _inspect(
        tmp_path,
        b",".join(b"" for _ in range(100_000)) + b"\n",
        max_header_fields=4,
    )

    assert inspection.status is HeaderInspectionStatus.UNSUPPORTED
    assert inspection.diagnostics == (HeaderDiagnosticCode.TOO_MANY_HEADER_FIELDS,)


def test_truncated_gzip_before_header_is_a_source_finding(tmp_path: Path) -> None:
    config = _config(tmp_path)
    compressed = gzip.compress(b"alpha,beta\n")
    (config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(compressed[:8])
    record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    inspection = inspect_registered_archive(
        record,
        compression_evidence=validate_registered_compression(record),
    )

    assert inspection.status is HeaderInspectionStatus.UNSUPPORTED
    assert inspection.diagnostics == (HeaderDiagnosticCode.TRUNCATED_GZIP,)


def test_invalid_gzip_is_reported_without_mutating_registration(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(b"bad")
    registration = register_inventory_archives(inventory_raw_roots(config))
    original = registration.records[0]

    inspection = inspect_registered_archive(
        original,
        compression_evidence=validate_registered_compression(original),
    )

    assert inspection.status is HeaderInspectionStatus.UNSUPPORTED
    assert inspection.diagnostics == (HeaderDiagnosticCode.INVALID_GZIP,)
    assert original.compressed_sha256 == registration.records[0].compressed_sha256


def test_missing_full_gzip_evidence_is_explicitly_not_checked(tmp_path: Path) -> None:
    _, record = _registered(tmp_path, "game_data_public.AFR.PremierDraft.csv.gz", b"a,b\n")

    inspection = inspect_registered_archive(record)

    assert inspection.status is HeaderInspectionStatus.UNSUPPORTED
    assert inspection.header_evidence is None
    assert inspection.compression_validation_status.value == "not_checked"
    assert inspection.diagnostics == (HeaderDiagnosticCode.COMPRESSION_NOT_CHECKED,)


def test_scanner_does_not_interpret_data_rows_or_infer_types(tmp_path: Path) -> None:
    payload = (
        b"a,b\n"
        b"FIRST_DATA_ROW_SHOULD_NOT_BE_INTERPRETED,123\n"
        b"\xff,SECOND_DATA_ROW_SHOULD_NOT_BE_INTERPRETED\n"
    )
    inspection = _inspect(tmp_path, payload)

    assert inspection.header_evidence is not None
    serialized = json.dumps(inspection.to_dict(), sort_keys=True)
    assert inspection.header_evidence.ordered_fields == ("a", "b")
    assert "FIRST_DATA_ROW" not in serialized
    assert "SECOND_DATA_ROW" not in serialized
    # Search for a serialized value, not this digit substring inside a SHA-256 digest.
    assert '"123"' not in serialized
    assert inspection.type_evidence is None
    assert inspection.nullability_evidence is None


def test_csv_reader_is_called_for_exactly_one_record(tmp_path: Path, monkeypatch) -> None:
    calls = 0
    real_reader = csv.reader

    def counted_reader(*args, **kwargs):
        nonlocal calls
        inner = real_reader(*args, **kwargs)

        class OneRecord:
            def __iter__(self):
                return self

            def __next__(self):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise AssertionError("scanner requested a second CSV record")
                return next(inner)

        return OneRecord()

    monkeypatch.setattr(header_module.csv, "reader", counted_reader)
    inspection = _inspect(tmp_path, b"a,b\nrow1,row2\n")

    assert inspection.header_evidence is not None
    assert calls == 1


def test_raw_schema_fingerprint_projection_golden_vector_and_physical_differences() -> None:
    from openmtgdata.header_inspection import (
        CsvParserObservationV1,
        EncodingObservationV1,
    )

    encoding = EncodingObservationV1(encoding_used="utf-8", bom_present=False)
    parser = CsvParserObservationV1()
    projection = raw_schema_fingerprint_projection_bytes(
        ("game_id", "turn", "action"),
        encoding,
        parser,
        ("LF",),
    )
    expected_bytes = (
        b'{"csv_delimiter_used":",","doubled_quote_observed":false,"encoding_observation":'
        b'{"authoritative_declaration":null,"bom_present":false,"encoding_used":"utf-8"},'
        b'"header_fields_ordered":["game_id","turn","action"],'
        b'"physical_line_endings":["LF"],"quote_character_observed":false,'
        b'"raw_schema_fingerprint_contract_id":'
        b'"openmtgdata.raw-schema-fingerprint.v1"}'
    )
    assert projection == expected_bytes
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()
    _, digest = fingerprint_raw_schema(
        ("game_id", "turn", "action"),
        encoding=encoding,
        parser_observation=parser,
        physical_line_endings=("LF",),
    )
    assert digest == expected_digest
    assert digest == "f8567d098d80f612d2f31c86dce581f720885f7620efe227dc57b636f55f117f"

    base = ("game_id", "turn", "action")
    _, reordered = fingerprint_raw_schema(
        ("turn", "game_id", "action"),
        encoding=encoding,
        parser_observation=parser,
        physical_line_endings=("LF",),
    )
    _, extended = fingerprint_raw_schema(
        (*base, "new_field"),
        encoding=encoding,
        parser_observation=parser,
        physical_line_endings=("LF",),
    )
    _, renamed = fingerprint_raw_schema(
        ("gameId", "turn", "action"),
        encoding=encoding,
        parser_observation=parser,
        physical_line_endings=("LF",),
    )
    assert len({digest, reordered, extended, renamed}) == 4
    _, different_scanner_policy = fingerprint_raw_schema(
        base,
        encoding=encoding,
        parser_observation=CsvParserObservationV1(
            policy_id="another-inspection-method.v2",
            configured_quotechar="'",
            configured_doublequote=False,
        ),
        physical_line_endings=("LF",),
    )
    assert different_scanner_policy == digest


def test_fingerprint_excludes_archive_path_expansion_tool_and_timestamp(tmp_path: Path) -> None:
    from openmtgdata.header_inspection import CsvParserObservationV1, EncodingObservationV1

    evidence, digest = fingerprint_raw_schema(
        ("a", "b"),
        encoding=EncodingObservationV1(encoding_used="utf-8", bom_present=False),
        parser_observation=CsvParserObservationV1(),
        physical_line_endings=("LF",),
    )
    assert digest == hashlib.sha256(evidence).hexdigest()
    # These values exist only on SourceInspectionV1 / source filename metadata and are
    # intentionally not arguments to the physical fingerprint projection.
    first = _inspect(
        tmp_path / "first",
        b"a,b\nfirst-row\n",
        name="game_data_public.AFR.PremierDraft.csv.gz",
        tool_identity="tool-a",
        inspection_timestamp_utc="2026-01-01T00:00:00Z",
    )
    second = _inspect(
        tmp_path / "second",
        b"a,b\nsecond-row\n",
        name="game_data_public.BLB.TradSealed.csv.gz",
        tool_identity="tool-b",
        inspection_timestamp_utc="2027-01-01T00:00:00Z",
    )
    assert first.header_evidence is not None and second.header_evidence is not None
    assert (
        first.header_evidence.raw_schema_fingerprint
        == second.header_evidence.raw_schema_fingerprint
    )
    assert first.source_inspection_id != second.source_inspection_id


def test_inspection_artifact_id_changes_with_archive_but_not_timestamp(tmp_path: Path) -> None:
    _, first_record = _registered(
        tmp_path / "one",
        "game_data_public.AFR.PremierDraft.csv.gz",
        b"a,b\nfirst-row\n",
    )
    _, other_record = _registered(
        tmp_path / "two",
        "game_data_public.AFR.PremierDraft.csv.gz",
        b"a,b\nsecond-row\n",
    )
    first = inspect_registered_archive(
        first_record,
        compression_evidence=validate_registered_compression(first_record),
        tool_identity="scanner-v1",
        inspection_timestamp_utc="2026-01-01T00:00:00Z",
    )
    second = inspect_registered_archive(
        other_record,
        compression_evidence=validate_registered_compression(other_record),
        tool_identity="scanner-v1",
        inspection_timestamp_utc="2027-01-01T00:00:00Z",
    )

    assert first.source_archive_id != second.source_archive_id
    assert first.header_evidence is not None and second.header_evidence is not None
    assert (
        first.header_evidence.raw_schema_fingerprint
        == second.header_evidence.raw_schema_fingerprint
    )
    assert first.source_inspection_id != second.source_inspection_id


def test_registered_byte_change_before_inspection_is_execution_failure(tmp_path: Path) -> None:
    config, record = _registered(tmp_path, "game_data_public.AFR.PremierDraft.csv.gz", b"a,b\n")
    compression_evidence = validate_registered_compression(record)
    path = config.raw_roots[0] / record.relative_path
    path.write_bytes(gzip.compress(b"changed,bytes\n"))

    with pytest.raises(HeaderInspectionExecutionError, match="registered bytes mismatch"):
        inspect_registered_archive(record, compression_evidence=compression_evidence)


def test_filesystem_mutation_check_failure_is_execution_failure(
    tmp_path: Path, monkeypatch
) -> None:
    _, record = _registered(tmp_path, "game_data_public.AFR.PremierDraft.csv.gz", b"a,b\n")
    compression_evidence = validate_registered_compression(record)

    def changed(*args, **kwargs):
        raise ArchiveChangedDuringRegistrationError("mutated during inspection")

    monkeypatch.setattr(
        "openmtgdata.archive_registration._verify_opened_archive_unchanged",
        changed,
    )
    with pytest.raises(HeaderInspectionExecutionError, match="changed or became unsafe"):
        inspect_registered_archive(record, compression_evidence=compression_evidence)


def test_inventory_groups_by_kind_and_fingerprint_and_binds_manifest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    contents = {
        "game_data_public.AFR.PremierDraft.csv.gz": b"a,b\nafr\n",
        "game_data_public.BLB.TradSealed.csv.gz": b"a,b\nblb\n",
        "replay_data_public.AFR.PremierDraft.csv.gz": b"a,b\nreplay\n",
        "replay_data_public.AFR.TradDraft.csv.gz": b"a,c\n",
    }
    for name, payload in contents.items():
        (config.raw_roots[0] / name).write_bytes(gzip.compress(payload))
    registration = register_inventory_archives(inventory_raw_roots(config))
    manifest = _manifest(registration)
    inventory = build_header_inventory(
        registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )

    assert inventory.configured_source_count == 4
    assert inventory.attempted_inspection_count == 4
    assert inventory.successful_inspection_count == 4
    assert inventory.failed_inspection_count == 0
    assert inventory.semantic_source_catalog_digest == manifest.semantic_source_catalog_digest
    assert inventory.unique_raw_schema_fingerprint_count == 2
    game_groups = [group for group in inventory.schema_groups if group.source_kind.value == "game"]
    replay_groups = [
        group for group in inventory.schema_groups if group.source_kind.value == "replay"
    ]
    assert len(game_groups) == 1 and game_groups[0].archive_count == 2
    assert game_groups[0].expansions == ("AFR", "BLB")
    assert set(game_groups[0].formats) == {"PremierDraft", "TradSealed"}
    assert len(replay_groups) == 2
    assert sum(group.archive_count for group in inventory.schema_groups) == 4


def test_header_inventory_serialization_is_registration_order_independent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for name, content in (
        ("game_data_public.AFR.PremierDraft.csv.gz", b"a,b\nx\n"),
        ("replay_data_public.AFR.TradDraft.csv.gz", b"a,c\ny\n"),
    ):
        (config.raw_roots[0] / name).write_bytes(gzip.compress(content))
    registration = register_inventory_archives(inventory_raw_roots(config))
    manifest = _manifest(registration)
    reversed_registration = replace(registration, records=tuple(reversed(registration.records)))

    original = build_header_inventory(
        registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )
    permuted = build_header_inventory(
        reversed_registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )

    assert original.canonical_bytes == permuted.canonical_bytes
    assert original.semantic_source_catalog_digest == permuted.semantic_source_catalog_digest


def test_inventory_accounts_for_unsupported_archive_instead_of_dropping_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(b"\xff\n")
    )
    (config.raw_roots[0] / "replay_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(b"a,b\n")
    )
    registration = register_inventory_archives(inventory_raw_roots(config))
    manifest = _manifest(registration)

    result = build_header_inventory(
        registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )

    assert result.configured_source_count == 2
    assert result.attempted_inspection_count == 2
    assert result.successful_inspection_count == 1
    assert result.failed_inspection_count == 1
    assert dict(result.diagnostic_counts) == {"INVALID_ENCODING": 1}
    assert len(result.inspections) == 2


def test_duplicate_local_copies_are_both_inspected_but_share_semantic_schema(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    compressed = gzip.compress(b"a,b\nrow\n")
    (config.raw_roots[0] / "one").mkdir()
    (config.raw_roots[0] / "two").mkdir()
    filename = "game_data_public.AFR.PremierDraft.csv.gz"
    (config.raw_roots[0] / "one" / filename).write_bytes(compressed)
    (config.raw_roots[0] / "two" / filename).write_bytes(compressed)
    registration = register_inventory_archives(inventory_raw_roots(config))
    manifest = _manifest(registration)
    result = build_header_inventory(
        registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )

    assert len(registration.records) == 2
    assert registration.records[0].source_archive_id == registration.records[1].source_archive_id
    assert result.attempted_inspection_count == 2
    assert len(result.inspections) == 2
    assert len({item.source_inspection_id for item in result.inspections}) == 1
    assert len(result.schema_groups) == 1
    assert result.schema_groups[0].archive_count == 2
    assert manifest.semantic_source_catalog_digest == result.semantic_source_catalog_digest


def test_manifest_registration_mismatch_fails_before_inspection(tmp_path: Path) -> None:
    first = _config(tmp_path / "first")
    second = _config(tmp_path / "second")
    (first.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(b"a,b\n")
    )
    (second.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(b"x,y\n")
    )
    first_registration = register_inventory_archives(inventory_raw_roots(first))
    second_registration = register_inventory_archives(inventory_raw_roots(second))
    second_manifest = _manifest(second_registration)

    with pytest.raises(HeaderInspectionExecutionError, match="do not match"):
        build_header_inventory(
            first_registration,
            source_manifest=second_manifest,
            configured_raw_roots=first.raw_roots,
        )


def test_header_scan_does_not_write_to_raw_root_or_use_output_roots(tmp_path: Path) -> None:
    config, record = _registered(tmp_path, "game_data_public.AFR.PremierDraft.csv.gz", b"a,b\n")
    before = sorted(path.name for path in config.raw_roots[0].iterdir())

    inspect_registered_archive(
        record,
        compression_evidence=validate_registered_compression(record),
    )

    assert sorted(path.name for path in config.raw_roots[0].iterdir()) == before
    assert not config.intermediate_root.exists()
    assert not config.quarantine_root.exists()
    assert not config.release_root.exists()


def test_inspect_headers_cli_returns_manifest_and_inventory_json(tmp_path: Path, capsys) -> None:
    from openmtgdata.cli import main

    config = _config(tmp_path)
    (config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(b"a,b\nrow\n")
    )
    result = main(
        [
            "inspect-headers",
            "--raw-root",
            str(config.raw_roots[0]),
            "--base-dir",
            str(tmp_path),
            "--intermediate-root",
            str(config.intermediate_root),
            "--quarantine-root",
            str(config.quarantine_root),
            "--release-root",
            str(config.release_root),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["header_inventory"]["attempted_inspection_count"] == 1
    assert output["header_inventory"]["successful_inspection_count"] == 1
    assert (
        output["header_inventory"]["semantic_source_catalog_digest"]
        == output["source_manifest"]["semantic_source_catalog_digest"]
    )


def test_fingerprint_golden_expected_bytes_match_independent_sha_value() -> None:
    expected = b"OpenMTGData raw schema fingerprint test vector\n"
    assert hashlib.sha256(expected).hexdigest() == (
        "3a29f6b33491b39b3b3bea83610b21564b589d01793baa06676672a1eeaff3d2"
    )
