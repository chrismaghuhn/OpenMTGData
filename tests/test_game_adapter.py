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
    GameSchemaError,
    GameSourceSelectorV1,
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
        GAME_EVIDENCE_BINDINGS,
    )
    mapping = replace(mapping, game_field_mapping_id=derive_game_mapping_id(mapping))
    group = GameMappingGroupV1(
        mapping.raw_schema_fingerprint,
        interpretation_id,
        GameDisposition.SUPPORTED,
        mapping.game_field_mapping_id,
        1,
        len(header.ordered_fields),
        "synthetic test mapping",
        "exact physical header width",
        (),
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


def test_real_m3_evidence_has_all_36_game_groups_accounted_for_when_available() -> None:
    evidence_path = Path("data/intermediate/m3.1/deep-inspection.json")
    registry_path = Path("data/intermediate/m3.2/schema-registry.json")
    if not evidence_path.exists() or not registry_path.exists():
        pytest.skip("ignored local M3 evidence is unavailable")
    evidence = load_verified_m3_evidence(evidence_path)
    from openmtgdata.source_reader import load_verified_schema_registry

    source_registry = load_verified_schema_registry(registry_path)
    from openmtgdata.game_adapter import build_game_mapping_registry

    result = build_game_mapping_registry(
        evidence.groups,
        source_registry,
        semantic_source_catalog_digest=evidence.semantic_source_catalog_digest,
        deep_inspection_evidence_digest=evidence.deep_inspection_evidence_digest,
    )
    assert len(result.groups) == 36
    assert sum(item.archive_count for item in result.groups) == 100
    assert all(item.disposition is GameDisposition.NEEDS_REVIEW for item in result.groups)
