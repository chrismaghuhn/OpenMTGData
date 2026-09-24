from __future__ import annotations

import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest

import openmtgdata.deep_inspection as deep_module
from openmtgdata.archive_registration import register_inventory_archives
from openmtgdata.compression_validation import (
    validate_registration_compression,
)
from openmtgdata.config import RuntimeConfig
from openmtgdata.deep_inspection import (
    DEEP_INSPECTION_METHOD_ID,
    DEEP_INSPECTION_REPORT_CONTRACT_ID,
    LEXICAL_EVIDENCE_CONTRACT_ID,
    MAX_FIELD_CHARS,
    MAX_INTERPRETED_DATA_ROWS,
    MAX_LOGICAL_ROW_BYTES,
    MAX_ROW_FIELDS,
    REPRESENTATIVE_SELECTION_CONTRACT_ID,
    DeepDiagnosticCode,
    DeepInspectionError,
    DeepInspectionStatus,
    SampleTermination,
    build_deep_inspection_report,
    classify_lexeme,
    inspect_registered_archive_rows,
    write_report_no_overwrite,
)
from openmtgdata.header_inspection import (
    HEADER_INVENTORY_CONTRACT_ID,
    build_header_inventory,
)
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_manifest import build_source_manifest


def _config(tmp_path: Path) -> RuntimeConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    return RuntimeConfig(
        raw_roots=(raw_root,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )


def _pipeline(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    max_data_rows: int = MAX_INTERPRETED_DATA_ROWS,
    max_logical_row_bytes: int = MAX_LOGICAL_ROW_BYTES,
    max_row_fields: int = MAX_ROW_FIELDS,
    max_field_chars: int = MAX_FIELD_CHARS,
    tool_identity: str | None = "test-tool-v1",
    inspection_timestamp_utc: str | None = None,
):
    config = _config(tmp_path)
    for filename, csv_bytes in files.items():
        path = config.raw_roots[0] / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(csv_bytes, mtime=0))
    registration = register_inventory_archives(inventory_raw_roots(config))
    compression = validate_registration_compression(registration)
    manifest = build_source_manifest(registration, compression_evidence=compression)
    headers = build_header_inventory(
        registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )
    report = build_deep_inspection_report(
        registration,
        manifest,
        headers,
        max_data_rows=max_data_rows,
        max_logical_row_bytes=max_logical_row_bytes,
        max_row_fields=max_row_fields,
        max_field_chars=max_field_chars,
        tool_identity=tool_identity,
        inspection_timestamp_utc=inspection_timestamp_utc,
    )
    return config, registration, manifest, headers, report


def _record_and_header(tmp_path: Path, csv_bytes: bytes):
    config = _config(tmp_path)
    (config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(csv_bytes, mtime=0)
    )
    registration = register_inventory_archives(inventory_raw_roots(config))
    compression = validate_registration_compression(registration)
    manifest = build_source_manifest(registration, compression_evidence=compression)
    header_inventory = build_header_inventory(
        registration,
        source_manifest=manifest,
        configured_raw_roots=config.raw_roots,
    )
    return config, registration.records[0], header_inventory.inspections[0], compression[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", "empty"),
        ("0", "integer_lexeme"),
        ("-1", "integer_lexeme"),
        ("+1", "integer_lexeme"),
        ("001", "integer_lexeme"),
        ("1.0", "decimal_lexeme"),
        (".5", "decimal_lexeme"),
        ("1.", "decimal_lexeme"),
        ("1e5", "decimal_lexeme"),
        ("-1.2e-3", "decimal_lexeme"),
        ("true", "boolean_lexeme"),
        ("false", "boolean_lexeme"),
        ("TRUE", "other_text"),
        ("False", "other_text"),
        ("null", "other_text"),
        ("None", "other_text"),
        ("abc", "other_text"),
        ("123abc", "other_text"),
        (" NaN ", "other_text"),
    ],
)
def test_lexical_evidence_uses_exact_ascii_grammar(value: str, expected: str) -> None:
    assert classify_lexeme(value).value == expected


def test_method_contracts_and_default_safety_limits() -> None:
    assert DEEP_INSPECTION_METHOD_ID == "openmtgdata.deep-source-inspection.v2"
    assert DEEP_INSPECTION_REPORT_CONTRACT_ID == "openmtgdata.deep-inspection-report.v1"
    assert LEXICAL_EVIDENCE_CONTRACT_ID == "openmtgdata.lexical-field-evidence.v1"
    assert REPRESENTATIVE_SELECTION_CONTRACT_ID == "openmtgdata.deep-representative-selection.v1"
    assert MAX_INTERPRETED_DATA_ROWS == 256
    assert MAX_LOGICAL_ROW_BYTES == 8 * 1024 * 1024
    assert MAX_ROW_FIELDS == 4096
    assert MAX_FIELD_CHARS == 1024 * 1024


def test_sample_limit_stops_after_exact_prefix_and_retains_no_row_values(tmp_path: Path) -> None:
    csv_bytes = (
        b"left,right\n1,true\n2,false\nNEVER_SERIALIZE_THIS_SENTINEL,NEVER_SERIALIZE_THIS_EITHER\n"
    )
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": csv_bytes},
        max_data_rows=2,
    )
    inspection = report.inspections[0]
    serialized = json.dumps(report.to_dict(), sort_keys=True)

    assert inspection.rows_interpreted == 2
    assert inspection.sample_termination is SampleTermination.ROW_LIMIT_REACHED
    assert inspection.status is DeepInspectionStatus.SUCCESS
    assert "NEVER_SERIALIZE_THIS" not in serialized
    assert "NEVER_SERIALIZE_THIS_EITHER" not in serialized
    assert not hasattr(inspection, "example_values")


def test_csv_reader_is_not_asked_for_a_record_after_the_configured_bound(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config, registration, manifest, headers, _ = _pipeline(
        tmp_path,
        {
            "game_data_public.AFR.PremierDraft.csv.gz": (
                b"a\nrow_one\nrow_two\nMUST_NOT_REQUEST_THIS_ROW\n"
            )
        },
    )
    record = registration.records[0]
    header = headers.inspections[0]
    compression = manifest.compression_evidence[0]
    real_reader = deep_module.csv.reader
    next_calls = 0

    def guarded_reader(*args, **kwargs):
        nonlocal next_calls
        reader = real_reader(*args, **kwargs)

        class Guard:
            def __iter__(self):
                return self

            def __next__(self):
                nonlocal next_calls
                next_calls += 1
                if next_calls > 3:
                    raise AssertionError("deep scanner requested a data record beyond its bound")
                return next(reader)

        return Guard()

    monkeypatch.setattr(deep_module.csv, "reader", guarded_reader)
    inspection = inspect_registered_archive_rows(
        record,
        header,
        compression,
        max_data_rows=2,
    )

    assert next_calls == 3  # header plus exactly two data records
    assert inspection.rows_interpreted == 2
    assert config.raw_roots[0].exists()


def test_short_archive_reports_natural_eof_and_exact_rows(tmp_path: Path) -> None:
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"a,b\nx,y\nq,z\n"},
    )
    inspection = report.inspections[0]

    assert inspection.rows_interpreted == 2
    assert inspection.sample_termination is SampleTermination.SOURCE_EOF_REACHED
    assert report.archives_reaching_source_eof == 1
    assert report.archives_reaching_row_limit == 0


def test_quoted_comma_newline_and_doubled_quote_are_lexically_parsed(tmp_path: Path) -> None:
    _, _, _, _, report = _pipeline(
        tmp_path,
        {
            "game_data_public.AFR.PremierDraft.csv.gz": (
                b'first,second,third\n"line one\nline two","a,b","say ""hi"""\n'
            )
        },
    )
    inspection = report.inspections[0]

    assert inspection.rows_interpreted == 1
    assert inspection.sample_termination is SampleTermination.SOURCE_EOF_REACHED
    assert inspection.columns[0].maximum_observed_character_length == len("line one\nline two")
    assert inspection.columns[1].maximum_observed_character_length == len("a,b")
    assert inspection.columns[2].maximum_observed_character_length == len('say "hi"')


def test_short_and_long_rows_are_measured_without_padding_or_truncation(tmp_path: Path) -> None:
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"a,b\nshort\nlong,row,extra\n"},
    )
    inspection = report.inspections[0]

    assert inspection.rows_interpreted == 2
    assert inspection.rows_matching_header_width == 0
    assert inspection.rows_shorter_than_header == 1
    assert inspection.rows_longer_than_header == 1
    assert inspection.minimum_observed_row_width == 1
    assert inspection.maximum_observed_row_width == 3
    assert inspection.columns[1].rows_observed == 1
    assert inspection.columns[1].exact_header_name == "b"
    assert inspection.columns[1].empty_field_count == 0
    assert [column.column_index for column in inspection.columns] == [0, 1]
    assert [finding.code for finding in inspection.diagnostics] == [
        DeepDiagnosticCode.ROW_WIDTH_SHORTER,
        DeepDiagnosticCode.ROW_WIDTH_LONGER,
    ]


def test_empty_field_is_not_called_null_and_duplicate_headers_use_column_index(
    tmp_path: Path,
) -> None:
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"same,,same\n,1,2\nx,,4\n"},
    )
    inspection = report.inspections[0]

    assert inspection.expected_field_count == 3
    assert [column.exact_header_name for column in inspection.columns] == ["same", "", "same"]
    assert [column.column_index for column in inspection.columns] == [0, 1, 2]
    assert inspection.columns[0].empty_field_count == 1
    assert inspection.columns[1].empty_field_count == 1
    assert inspection.columns[1].source_null_semantics == "not_established"
    assert inspection.columns[1].lexical_class_counts.empty == 1
    assert inspection.columns[1].lexical_class_counts.integer_lexeme == 1


@pytest.mark.parametrize(
    ("row", "code", "kwargs"),
    [
        (b"\xff,2\n", DeepDiagnosticCode.INVALID_DATA_ENCODING, {}),
        (b'"unterminated,2\n', DeepDiagnosticCode.MALFORMED_DATA_ROW, {}),
        (b"a" * 64 + b",b\n", DeepDiagnosticCode.DATA_ROW_TOO_LARGE, {"max_logical_row_bytes": 16}),
        (b"toolong,b\n", DeepDiagnosticCode.DATA_FIELD_TOO_LARGE, {"max_field_chars": 4}),
        (b"a,b,c\n", DeepDiagnosticCode.TOO_MANY_ROW_FIELDS, {"max_row_fields": 2}),
    ],
)
def test_sample_row_failures_are_typed_partial_findings(
    tmp_path: Path,
    row: bytes,
    code: DeepDiagnosticCode,
    kwargs: dict[str, int],
) -> None:
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"a,b\n" + row},
        **kwargs,
    )
    inspection = report.inspections[0]

    assert inspection.status is DeepInspectionStatus.PARTIAL
    assert inspection.failed_row_ordinal == 1
    assert inspection.sample_termination is SampleTermination.ROW_PARSE_ERROR
    assert inspection.rows_interpreted == 0
    assert inspection.diagnostics[0].code is code
    assert report.archives_with_parse_error == 1


def test_malformed_row_after_valid_prefix_preserves_only_prior_aggregates(tmp_path: Path) -> None:
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b'a,b\n1,x\n"broken,y\n'},
    )
    inspection = report.inspections[0]

    assert inspection.status is DeepInspectionStatus.PARTIAL
    assert inspection.rows_interpreted == 1
    assert inspection.failed_row_ordinal == 2
    assert inspection.columns[0].lexical_class_counts.integer_lexeme == 1


def test_mixed_lexical_class_evidence_is_column_indexed_and_aggregate_only(tmp_path: Path) -> None:
    csv_bytes = b"value\n1\nabc\ntrue\n"
    _, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": csv_bytes},
    )
    column = report.inspections[0].columns[0]

    assert column.rows_observed == 3
    assert column.lexical_class_counts.integer_lexeme == 1
    assert column.lexical_class_counts.boolean_lexeme == 1
    assert column.lexical_class_counts.other_text == 1
    assert column.mixed_lexical_class_observed is True
    assert "abc" not in json.dumps(report.to_dict())


def test_row_limit_changes_scope_and_deep_identity_but_not_raw_fingerprint(tmp_path: Path) -> None:
    csv_bytes = b"a\n1\n2\n3\n"
    config, registration, manifest, headers, first_report = _pipeline(
        tmp_path / "first",
        {"game_data_public.AFR.PremierDraft.csv.gz": csv_bytes},
        max_data_rows=1,
    )
    second_report = build_deep_inspection_report(
        registration,
        manifest,
        headers,
        max_data_rows=2,
        tool_identity="test-tool-v1",
    )

    assert (
        first_report.inspections[0].raw_schema_fingerprint
        == second_report.inspections[0].raw_schema_fingerprint
    )
    assert (
        first_report.inspections[0].deep_inspection_id
        != second_report.inspections[0].deep_inspection_id
    )
    assert first_report.inspections[0].rows_interpreted == 1
    assert second_report.inspections[0].rows_interpreted == 2


def test_identity_changes_with_archive_and_tool_but_not_timestamp(tmp_path: Path) -> None:
    config, record, header, compression = _record_and_header(tmp_path / "first", b"a\nfirst\n")
    first = inspect_registered_archive_rows(
        record,
        header,
        compression,
        tool_identity="scanner-a",
        inspection_timestamp_utc="2026-01-01T00:00:00Z",
    )
    same_evidence_later = inspect_registered_archive_rows(
        record,
        header,
        compression,
        tool_identity="scanner-a",
        inspection_timestamp_utc="2027-01-01T00:00:00Z",
    )
    other_tool = inspect_registered_archive_rows(
        record,
        header,
        compression,
        tool_identity="scanner-b",
    )

    assert first.raw_schema_fingerprint == same_evidence_later.raw_schema_fingerprint
    assert first.deep_inspection_id == same_evidence_later.deep_inspection_id
    assert first.deep_inspection_id != other_tool.deep_inspection_id
    assert first.raw_schema_fingerprint == header.raw_schema_fingerprint
    assert config.raw_roots[0].exists()


def test_changed_source_bytes_fail_closed(tmp_path: Path) -> None:
    config, record, header, compression = _record_and_header(tmp_path, b"a\noriginal\n")
    path = config.raw_roots[0] / record.relative_path
    path.write_bytes(gzip.compress(b"a\nreplacement\n", mtime=0))

    with pytest.raises(DeepInspectionError, match="changed during deep inspection"):
        inspect_registered_archive_rows(record, header, compression)


def test_m2_header_fingerprint_mismatch_fails_closed(tmp_path: Path) -> None:
    _, record, header, compression = _record_and_header(tmp_path, b"a,b\nx,y\n")
    tampered_header = replace(header, raw_schema_fingerprint="0" * 64)

    with pytest.raises(DeepInspectionError, match="does not match accepted M2.5"):
        inspect_registered_archive_rows(record, tampered_header, compression)


def test_header_inventory_and_manifest_mismatch_fail_closed(tmp_path: Path) -> None:
    config, registration, manifest, headers, _ = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"a\nx\n"},
    )
    changed_manifest = replace(
        manifest,
        source_manifest_schema_id="openmtgdata.source-manifest.other",
    )
    with pytest.raises(DeepInspectionError, match="unsupported SourceManifestV1"):
        build_deep_inspection_report(registration, changed_manifest, headers)
    other_header = replace(
        headers,
        semantic_source_catalog_digest="f" * 64,
    )
    with pytest.raises(DeepInspectionError, match="different source catalog"):
        build_deep_inspection_report(registration, manifest, other_header)
    assert config.raw_roots[0].exists()


def test_raw_fingerprint_ignores_sampled_rows_source_path_and_timestamp(tmp_path: Path) -> None:
    _, _, _, _, left = _pipeline(
        tmp_path / "left",
        {"game_data_public.AFR.PremierDraft.csv.gz": b"a\nleft row\n"},
        inspection_timestamp_utc="2026-01-01T00:00:00Z",
    )
    _, _, _, _, right = _pipeline(
        tmp_path / "right",
        {"game_data_public.BLB.TradSealed.csv.gz": b"a\nright row\n"},
        inspection_timestamp_utc="2027-01-01T00:00:00Z",
    )

    assert left.inspections[0].raw_schema_fingerprint == right.inspections[0].raw_schema_fingerprint
    assert left.inspections[0].deep_inspection_id != right.inspections[0].deep_inspection_id


def test_source_digest_unknown_metadata_and_null_interpretation_are_preserved(
    tmp_path: Path,
) -> None:
    _, _, manifest, headers, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b'a\n""\n'},
    )
    inspection = report.inspections[0]

    assert report.semantic_source_catalog_digest == manifest.semantic_source_catalog_digest
    assert inspection.source_url_status == "unknown"
    assert inspection.source_url is None
    assert inspection.license_status == "unknown"
    assert inspection.source_interpretation_contract_id is None
    assert inspection.columns[0].empty_field_count == 1
    assert inspection.columns[0].source_null_semantics == "not_established"
    assert report.source_url_status_counts == (("unknown", 1),)
    assert report.license_status_counts == (("unknown", 1),)
    assert headers.semantic_source_catalog_digest == report.semantic_source_catalog_digest


def test_exactly_one_deterministic_representative_per_raw_group_with_pair_context(
    tmp_path: Path,
) -> None:
    _, registration, manifest, headers, report = _pipeline(
        tmp_path,
        {
            "game_data_public.AFR.PremierDraft.csv.gz": b"a\ngame\n",
            "game_data_public.BLB.TradSealed.csv.gz": b"c\ngame2\n",
            "replay_data_public.AFR.PremierDraft.csv.gz": b"b\nreplay\n",
        },
    )

    assert len(report.representatives) == len(headers.schema_groups)
    assert report.designated_representative_count == report.raw_schema_group_count
    assert report.covered_raw_schema_group_count == report.raw_schema_group_count
    game_representative = next(
        item
        for item in report.representatives
        if item.source_kind is SourceKind.GAME and item.representative.expansion_token == "AFR"
    )
    replay_representative = next(
        item for item in report.representatives if item.source_kind is SourceKind.REPLAY
    )
    assert game_representative.counterpart.status == "available"
    assert replay_representative.counterpart.status == "available"
    assert "no row correspondence" in game_representative.counterpart.interpretation
    assert manifest.semantic_source_catalog_digest == report.semantic_source_catalog_digest
    assert registration.registered_archive_count == 3


def test_duplicate_local_copy_does_not_add_a_second_semantic_representative(tmp_path: Path) -> None:
    csv_bytes = gzip.compress(b"a\nx\n", mtime=0)
    files = {
        "one/game_data_public.AFR.PremierDraft.csv.gz": gzip.decompress(csv_bytes),
        "two/game_data_public.AFR.PremierDraft.csv.gz": gzip.decompress(csv_bytes),
    }
    _, registration, _, headers, report = _pipeline(tmp_path, files)

    assert registration.registered_archive_count == 2
    assert registration.unique_source_archive_id_count == 1
    assert len(headers.schema_groups) == 1
    assert report.designated_representative_count == 1
    assert report.representatives[0].archive_count == 2
    assert report.attempted_deep_inspections == 2


def test_operational_timing_does_not_change_evidence_digest(tmp_path: Path) -> None:
    files = {"game_data_public.AFR.PremierDraft.csv.gz": b"a\n1\n"}
    _, _, _, _, first = _pipeline(tmp_path / "first", files)
    _, _, _, _, second = _pipeline(tmp_path / "second", files)

    assert first.evidence_digest == second.evidence_digest
    assert first.operational_metrics.wall_clock_seconds >= 0


def test_inspection_timestamp_changes_audit_only_not_evidence_digest(tmp_path: Path) -> None:
    files = {"game_data_public.AFR.PremierDraft.csv.gz": b"a,b\n1,true\n2,false\n"}
    _, _, _, _, first = _pipeline(
        tmp_path / "first",
        files,
        inspection_timestamp_utc="2026-01-01T00:00:00Z",
    )
    _, _, _, _, second = _pipeline(
        tmp_path / "second",
        files,
        inspection_timestamp_utc="2027-01-01T00:00:00Z",
    )

    assert first.inspections[0].deep_inspection_id == second.inspections[0].deep_inspection_id
    assert (
        first.inspections[0].raw_schema_fingerprint == second.inspections[0].raw_schema_fingerprint
    )
    assert first.evidence_bytes == second.evidence_bytes
    assert first.evidence_digest == second.evidence_digest
    assert first.to_dict()["audit"] != second.to_dict()["audit"]
    assert first.inspections[0].to_dict()["inspection_timestamp_utc"] == "2026-01-01T00:00:00Z"
    assert "inspection_timestamp_utc" not in first.inspections[0].to_evidence_dict()


def test_evidence_projection_is_independent_of_local_paths_and_registration_order(
    tmp_path: Path,
) -> None:
    files = {
        "game_data_public.AFR.PremierDraft.csv.gz": b"a,b\n1,true\n",
        "replay_data_public.AFR.PremierDraft.csv.gz": b"a,b\n2,false\n",
    }
    _, _, _, _, left = _pipeline(tmp_path / "left", files)
    right_config, right_registration, right_manifest, right_headers, _ = _pipeline(
        tmp_path / "right",
        files,
    )
    permuted = replace(
        right_registration,
        records=tuple(reversed(right_registration.records)),
    )
    right = build_deep_inspection_report(
        permuted,
        right_manifest,
        right_headers,
        tool_identity="test-tool-v1",
    )

    assert left.semantic_source_catalog_digest == right.semantic_source_catalog_digest
    assert left.header_inventory_evidence_digest == right.header_inventory_evidence_digest
    assert left.evidence_digest == right.evidence_digest
    assert str(right_config.raw_roots[0]) not in right.evidence_bytes.decode("utf-8")


def test_deep_report_keeps_m2_header_and_manifest_contracts_unchanged(tmp_path: Path) -> None:
    _, _, manifest, headers, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"x\n1\n"},
    )
    original_source_digest = manifest.semantic_source_catalog_digest
    original_header_digest = headers.semantic_source_catalog_digest

    assert original_source_digest == report.semantic_source_catalog_digest
    assert headers.contract_id == HEADER_INVENTORY_CONTRACT_ID
    assert headers.semantic_source_catalog_digest == original_header_digest
    assert all(item.source_interpretation_contract_id is None for item in report.inspections)


def test_detailed_report_is_no_overwrite_and_stays_under_intermediate_root(
    tmp_path: Path,
) -> None:
    config, _, _, _, report = _pipeline(
        tmp_path,
        {"game_data_public.AFR.PremierDraft.csv.gz": b"a\nPRIVATE_ROW_SENTINEL\n"},
    )
    output = write_report_no_overwrite(
        report,
        config.intermediate_root / "reports" / "deep.json",
        config,
        base_dir=tmp_path,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert output.is_relative_to(config.intermediate_root)
    assert payload["evidence_digest"] == report.evidence_digest
    assert "PRIVATE_ROW_SENTINEL" not in output.read_text(encoding="utf-8")
    with pytest.raises(DeepInspectionError, match="refusing overwrite"):
        write_report_no_overwrite(report, output, config, base_dir=tmp_path)
    with pytest.raises(DeepInspectionError, match="under intermediate_root"):
        write_report_no_overwrite(
            report,
            config.raw_roots[0] / "bad.json",
            config,
            base_dir=tmp_path,
        )


def test_inspect_deep_cli_summary_and_explicit_output(tmp_path: Path, capsys) -> None:
    from openmtgdata.cli import main

    config = _config(tmp_path)
    (config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz").write_bytes(
        gzip.compress(b"a\n1\n2\n", mtime=0)
    )
    output_path = config.intermediate_root / "inspection.json"
    result = main(
        [
            "inspect-deep",
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
            "--max-data-rows",
            "1",
            "--output",
            str(output_path),
        ]
    )
    stdout = capsys.readouterr()
    summary = json.loads(stdout.out)

    assert result == 0
    assert summary["attempted_deep_inspections"] == 1
    assert summary["sample_rows_interpreted_total"] == 1
    assert summary["detailed_report"]["path_scope"] == "runtime_local"
    assert output_path.exists()
