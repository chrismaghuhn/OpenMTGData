from __future__ import annotations

import gzip
from dataclasses import replace
from pathlib import Path

import pytest

from openmtgdata.archive_registration import register_inventory_archives
from openmtgdata.compression_validation import validate_registered_compression
from openmtgdata.config import RuntimeConfig
from openmtgdata.game_adapter import GameAdapterV1
from openmtgdata.game_schema import (
    GAME_EVIDENCE_BINDINGS,
    GameDisposition,
    GameFieldLineageV1,
    GameFieldMappingV1,
    GameMappingGroupV1,
    GameMappingRegistryV1,
    GameMappingReviewV1,
    GameSchemaError,
    GameSourceSelectorV1,
    derive_game_m4_review_evidence_id,
    derive_game_mapping_id,
    derive_game_registry_digest,
)
from openmtgdata.header_inspection import inspect_registered_archive
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.schema_evolution import load_verified_m3_evidence
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    VerifiedRegistryGroupV1,
    VerifiedSchemaRegistryV1,
    open_source_reader,
)


def _fixture(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )
    source = raw / "game_data_public.TST.PremierDraft.csv.gz"
    source.write_bytes(gzip.compress(b"draft_id,draft_id,empty\n001,second,\n002,,\n"))
    record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    header = inspect_registered_archive(
        record, compression_evidence=validate_registered_compression(record)
    ).header_evidence
    assert header is not None
    interpretation_id = "openmtgdata.source-interpretation.v1:" + "a" * 64
    source_registry = VerifiedSchemaRegistryV1(
        "b" * 64,
        "openmtgdata.schema-registry.v1",
        "openmtgdata.schema-registry-digest.v1",
        "openmtgdata.schema-evolution-policy.v1",
        (
            VerifiedRegistryGroupV1(
                SourceKind.GAME,
                header.raw_schema_fingerprint,
                interpretation_id,
                (header.raw_schema_fingerprint,),
                "openmtgdata.csv-header-policy.comma-utf8.v1",
            ),
        ),
    )
    selectors = (
        GameSourceSelectorV1(0, "draft_id"),
        GameSourceSelectorV1(1, "draft_id"),
    )
    paths = ("source_first_id_raw", "source_second_id_raw")
    lineages = tuple(
        GameFieldLineageV1(
            path,
            "source_direct",
            (selector,),
            (),
            "openmtgdata.preserve-source-csv-string.v1",
            "openmtgdata.source-string-exact.v1",
            "one_source_field_per_source_row",
            "preserve_empty_string",
            "preserve_exact_parser_returned_string",
            ("synthetic exact header/row fixture",),
        )
        for selector, path in zip(selectors, paths, strict=True)
    )
    review = GameMappingReviewV1(
        "openmtgdata.game-mapping-review.v1",
        record.source_archive_id,
        record.compressed_sha256,
        record.compressed_size_bytes,
        header.raw_schema_fingerprint,
        interpretation_id,
        "c" * 64,
        "0" * 64,
        "openmtgdata.raw-source-reader.v2",
        "complete",
        2,
        2,
        0,
        (),
        2,
        2,
        0,
        "d" * 64,
        "m4-full-stream-verified",
    )
    m4_review_id = derive_game_m4_review_evidence_id(review)
    mapping = GameFieldMappingV1(
        "",
        header.raw_schema_fingerprint,
        interpretation_id,
        SourceKind.GAME,
        "one M4 accepted source row; no cross-row game grouping claim",
        tuple(zip(selectors, paths, strict=True)),
        tuple(
            (
                GameSourceSelectorV1(i, name),
                "mapped" if i < 2 else "unmapped_preserved_at_raw_layer",
            )
            for i, name in enumerate(header.ordered_fields)
        ),
        lineages,
        "exact physical header width",
        "preserve empty string",
        "source strings only",
        (*GAME_EVIDENCE_BINDINGS, "M3.2 schema registry:" + "c" * 64, m4_review_id),
    )
    mapping = replace(mapping, game_field_mapping_id=derive_game_mapping_id(mapping))
    review = replace(review, game_field_mapping_id=mapping.game_field_mapping_id)
    group = GameMappingGroupV1(
        mapping.raw_schema_fingerprint,
        interpretation_id,
        GameDisposition.SUPPORTED,
        mapping.game_field_mapping_id,
        1,
        (record.source_archive_id,),
        len(header.ordered_fields),
        "synthetic test mapping",
        "exact physical header width",
        (),
        review,
    )
    registry = GameMappingRegistryV1(
        "c" * 64,
        "e" * 64,
        "d" * 64,
        "openmtgdata.game-mapping-policy.test.v1",
        (mapping,),
        (group,),
        "",
    )
    registry = replace(registry, game_mapping_registry_digest=derive_game_registry_digest(registry))
    return record, header, source_registry, registry


def test_source_direct_mapping_preserves_exact_values_duplicate_names_and_empty(
    tmp_path: Path,
) -> None:
    record, _, source_registry, game_registry = _fixture(tmp_path)
    adapter = GameAdapterV1(game_registry)
    with open_source_reader(record, source_registry) as reader:
        batch = next(iter(reader))
        raw = batch.accepted_records[0]
        result = adapter.normalize_record(
            raw, source_archive_record=record, header_fields=reader.header_fields
        )
    assert [(item.normalized_field_path, item.source_value) for item in result.source_facts] == [
        ("source_first_id_raw", "001"),
        ("source_second_id_raw", "second"),
    ]
    assert result.quality_codes[0].value == "unmapped_source_fields_present"
    assert all(len(fact.lineage.source_selectors) == 1 for fact in result.source_facts)
    assert [
        x.column_index for x in (fact.lineage.source_selectors[0] for fact in result.source_facts)
    ] == [0, 1]


def test_mapping_id_correction_changes_m5_only(tmp_path: Path) -> None:
    _, _, _, registry = _fixture(tmp_path)
    mapping = registry.mappings[0]
    changed = replace(mapping, row_width_policy="different explicit policy")
    changed = replace(changed, game_field_mapping_id=derive_game_mapping_id(changed))
    assert changed.game_field_mapping_id != mapping.game_field_mapping_id
    assert changed.raw_schema_fingerprint == mapping.raw_schema_fingerprint
    assert changed.source_interpretation_contract_id == mapping.source_interpretation_contract_id
    changed_registry = replace(
        registry,
        mappings=(changed,),
        groups=(replace(registry.groups[0], game_field_mapping_id=changed.game_field_mapping_id),),
        game_mapping_registry_digest="",
    )
    changed_registry = replace(
        changed_registry,
        game_mapping_registry_digest=derive_game_registry_digest(changed_registry),
    )
    assert changed_registry.game_mapping_registry_digest != registry.game_mapping_registry_digest


def test_mapping_registry_detects_tampering(tmp_path: Path) -> None:
    _, _, _, registry = _fixture(tmp_path)
    with pytest.raises(GameSchemaError, match="digest mismatch"):
        GameAdapterV1(replace(registry, game_mapping_registry_digest="0" * 64))


def test_source_empty_is_not_null_and_source_types_remain_strings(tmp_path: Path) -> None:
    record, _, source_registry, game_registry = _fixture(tmp_path)
    adapter = GameAdapterV1(game_registry)
    with open_source_reader(record, source_registry) as reader:
        batch = next(iter(reader))
        first, second = batch.accepted_records
        output = adapter.normalize_record(
            second, source_archive_record=record, header_fields=reader.header_fields
        )
    assert [fact.source_value for fact in output.source_facts] == ["002", ""]
    assert all(isinstance(fact.source_value, str) for fact in output.source_facts)
    assert first.fields[0] == "001"
    assert output.locator.data_record_ordinal == 2


def test_needs_review_group_cannot_be_normalized(tmp_path: Path) -> None:
    record, _, source_registry, game_registry = _fixture(tmp_path)
    blocked_group = replace(
        game_registry.groups[0],
        disposition=GameDisposition.NEEDS_REVIEW,
        game_field_mapping_id=None,
        review=None,
    )
    blocked = replace(
        game_registry,
        mappings=(),
        groups=(blocked_group,),
        game_mapping_registry_digest="",
    )
    blocked = replace(blocked, game_mapping_registry_digest=derive_game_registry_digest(blocked))
    adapter = GameAdapterV1(blocked)
    with open_source_reader(record, source_registry) as reader:
        raw = next(iter(reader)).accepted_records[0]
        with pytest.raises(GameSchemaError, match="no supported"):
            adapter.normalize_record(
                raw, source_archive_record=record, header_fields=reader.header_fields
            )


def test_m4_raw_record_contracts_are_checked_explicitly(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace

    record, _, source_registry, game_registry = _fixture(tmp_path)
    adapter = GameAdapterV1(game_registry)
    with open_source_reader(record, source_registry) as reader:
        raw = next(iter(reader)).accepted_records[0]
        with pytest.raises(GameSchemaError, match="record contract"):
            adapter.normalize_record(
                dc_replace(raw, raw_csv_record_contract_id="wrong.v1"),
                source_archive_record=record,
                header_fields=reader.header_fields,
            )
        with pytest.raises(GameSchemaError, match="reader contract"):
            adapter.normalize_record(
                dc_replace(raw, reader_contract_id="wrong.v1"),
                source_archive_record=record,
                header_fields=reader.header_fields,
            )


def test_reviewed_group_must_match_interpretation_and_exact_header(tmp_path: Path) -> None:
    record, _, source_registry, registry = _fixture(tmp_path)
    adapter = GameAdapterV1(registry)
    with open_source_reader(record, source_registry) as reader:
        raw = next(iter(reader)).accepted_records[0]
        with pytest.raises(GameSchemaError, match="selector"):
            adapter.normalize_record(
                raw,
                source_archive_record=record,
                header_fields=("wrong", "draft_id", "empty"),
            )


def test_real_m3_evidence_has_all_corrected_game_groups_accounted_for_when_available() -> None:
    evidence_path = Path("data/intermediate/m5.2/deep-inspection-container-v2-methods.json")
    registry_path = Path("data/intermediate/m5.2/schema-registry-container-v2-methods.json")
    if not evidence_path.exists() or not registry_path.exists():
        pytest.skip("ignored local M3 evidence is unavailable")
    evidence = load_verified_m3_evidence(
        evidence_path,
        expected_evidence_digest="c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29",
        expected_source_catalog_digest="dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602",
    )
    from openmtgdata.source_reader import load_verified_schema_registry

    source_registry = load_verified_schema_registry(registry_path)
    from openmtgdata.game_adapter import (
        AFR_GAME_RAW_FINGERPRINT,
        _mapping_for_group,
        build_game_mapping_registry,
    )

    result = build_game_mapping_registry(
        evidence.groups,
        source_registry,
        semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
        deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
    )
    assert len(result.groups) == 35
    assert sum(item.archive_count for item in result.groups) == 100
    assert all(item.disposition is GameDisposition.NEEDS_REVIEW for item in result.groups)

    afr = next(
        item for item in evidence.groups if item.raw_schema_fingerprint == AFR_GAME_RAW_FINGERPRINT
    )
    interpretation = next(
        item.source_interpretation_contract_id
        for item in source_registry.groups
        if item.raw_schema_fingerprint == AFR_GAME_RAW_FINGERPRINT
    )
    review = GameMappingReviewV1(
        "openmtgdata.game-mapping-review.v1",
        afr.representative_source_archive_id,
        "f" * 64,
        100,
        afr.raw_schema_fingerprint,
        interpretation,
        source_registry.schema_registry_digest,
        "0" * 64,
        "openmtgdata.raw-source-reader.v2",
        "complete",
        2,
        2,
        0,
        (),
        2,
        2,
        0,
        "a" * 64,
        "m4-full-stream-verified",
    )
    mapping = _mapping_for_group(
        afr,
        interpretation,
        schema_registry_digest=source_registry.schema_registry_digest,
        m4_review_evidence_id=derive_game_m4_review_evidence_id(review),
    )
    review = replace(review, game_field_mapping_id=mapping.game_field_mapping_id)
    supported = build_game_mapping_registry(
        evidence.groups,
        source_registry,
        semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
        deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
        reviews=(review,),
    )
    afr_disposition = next(
        item
        for item in supported.groups
        if item.raw_schema_fingerprint == afr.raw_schema_fingerprint
    )
    assert afr_disposition.disposition is GameDisposition.SUPPORTED
    assert afr_disposition.review == review
    bad_review = replace(review, m4_records_seen=1, game_field_mapping_id="0" * 64)
    bad_mapping = _mapping_for_group(
        afr,
        interpretation,
        schema_registry_digest=source_registry.schema_registry_digest,
        m4_review_evidence_id=derive_game_m4_review_evidence_id(bad_review),
    )
    bad_review = replace(bad_review, game_field_mapping_id=bad_mapping.game_field_mapping_id)
    with pytest.raises(GameSchemaError, match="counts do not reconcile"):
        build_game_mapping_registry(
            evidence.groups,
            source_registry,
            semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
            deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
            reviews=(bad_review,),
        )
    with pytest.raises(GameSchemaError, match="lacks complete M4"):
        build_game_mapping_registry(
            evidence.groups,
            source_registry,
            semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
            deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
            reviews=(replace(review, m4_completion_status="incomplete"),),
        )
    with pytest.raises(GameSchemaError, match="submissions do not reconcile"):
        build_game_mapping_registry(
            evidence.groups,
            source_registry,
            semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
            deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
            reviews=(replace(review, m5_records_submitted=1),),
        )
