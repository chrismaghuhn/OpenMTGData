from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from openmtgdata.replay_adapter import ReplayAdapterV1, ReplayNormalizationError
from openmtgdata.replay_schema import (
    REPLAY_FIELD_MAPPING_CONTRACT_ID,
    REPLAY_MAPPING_REGISTRY_CONTRACT_ID,
    FieldDisposition,
    MappingDisposition,
    ReplayMappingReviewV1,
    ReplayQualityCode,
    ReplaySchemaError,
    apply_mapping_dispositions_to_analysis,
    build_replay_field_analysis,
    build_replay_mapping_registry,
    build_turn_slot_mapping,
    derive_replay_field_mapping_id,
)
from openmtgdata.schema_evolution import (
    FieldSurfaceV1,
    RawSchemaGroupEvidenceV1,
    VerifiedM3EvidenceV1,
    load_verified_m3_evidence,
)
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    BATCH_CONTRACT_ID,
    RAW_RECORD_CONTRACT_ID,
    READER_CONTRACT_ID,
    RawCsvBatchV1,
    RawCsvRecordV1,
    VerifiedRegistryGroupV1,
    VerifiedSchemaRegistryV1,
    load_verified_schema_registry,
)


def _group(
    fingerprint: str,
    *,
    source_kind: str = "replay",
    with_turn_slots: bool = True,
    source_archive_id: str = "b" * 64,
) -> RawSchemaGroupEvidenceV1:
    names = ["turns", "on_play", "game_index"]
    if with_turn_slots:
        suffixes = ("cards_drawn", "eot_user_life", "eot_oppo_life")
        for side in ("user", "oppo"):
            for index in range(1, 31):
                names.extend(f"{side}_turn_{index}_{suffix}" for suffix in suffixes)
    fields = tuple(
        FieldSurfaceV1(
            index,
            name,
            ("integer_lexeme",),
            True,
            (("empty", 2), ("integer_lexeme", 2)),
        )
        for index, name in enumerate(names)
    )
    return RawSchemaGroupEvidenceV1(
        "c" * 64,
        source_kind,
        fingerprint,
        1,
        (source_archive_id,),
        ("d" * 64,),
        source_archive_id,
        "replay_data_public.TST.PremierDraft.csv.gz",
        (("TST", "PremierDraft", 1),),
        fields,
        "e" * 64,
        (),
        (),
        (),
    )


def _evidence_and_registry() -> tuple[VerifiedM3EvidenceV1, VerifiedSchemaRegistryV1]:
    supported = _group("a" * 64, source_archive_id="b" * 64)
    no_slots = _group("f" * 64, with_turn_slots=False, source_archive_id="1" * 64)
    evidence = VerifiedM3EvidenceV1(
        "2" * 64,
        "3" * 64,
        "openmtgdata.deep-source-inspection.v1",
        "openmtgdata.header-inventory.v1",
        "4" * 64,
        2,
        (),
        (),
        (supported, no_slots),
    )
    registry = VerifiedSchemaRegistryV1(
        "5" * 64,
        "openmtgdata.schema-registry.v1",
        "openmtgdata.schema-registry-digest.v1",
        "openmtgdata.schema-evolution-policy.v1",
        (
            VerifiedRegistryGroupV1(
                SourceKind.REPLAY,
                "a" * 64,
                "openmtgdata.source-interpretation.v1:" + "6" * 64,
                ("a" * 64,),
                "openmtgdata.csv-header-policy.comma-utf8.v1",
            ),
            VerifiedRegistryGroupV1(
                SourceKind.REPLAY,
                "f" * 64,
                "openmtgdata.source-interpretation.v1:" + "7" * 64,
                ("f" * 64,),
                "openmtgdata.csv-header-policy.comma-utf8.v1",
            ),
        ),
    )
    return evidence, registry


def _mapping_fixture():
    evidence, schema_registry = _evidence_and_registry()
    analysis = build_replay_field_analysis(evidence, schema_registry)
    group = evidence.groups[0]
    mapping = build_turn_slot_mapping(
        group,
        source_interpretation_contract_id=schema_registry.groups[
            0
        ].source_interpretation_contract_id,
        m3_evidence_digest=evidence.deep_inspection_evidence_digest,
        source_archive_id_reviewed=group.representative_source_archive_id,
    )
    return evidence, schema_registry, analysis, group, mapping


def _review(mapping, *, source_id: str = "b" * 64) -> ReplayMappingReviewV1:
    return ReplayMappingReviewV1(
        mapping.raw_schema_fingerprint,
        source_id,
        "complete",
        1,
        1,
        0,
        1,
        1,
        0,
        3,
        0,
        0,
        0,
        ((3, 1),),
        (("oppo", 1), ("user", 2)),
        "m4-full-stream-verified",
    )


def test_analysis_accounts_for_each_m3_replay_group_and_registry_disposition() -> None:
    evidence, registry = _evidence_and_registry()
    analysis = build_replay_field_analysis(evidence, registry)
    assert len(analysis.groups) == 2
    assert analysis.groups[0].exact_ordered_header_surface[0] == "turns"
    assert analysis.groups[0].field_count == len(analysis.groups[0].field_evidence)
    assert all(
        item.source_null_semantics == "not_established_from_bounded_lexical_evidence"
        for item in analysis.groups[0].field_evidence
    )
    assert all(item.disposition_reason for item in analysis.groups)


def test_turn_mapping_has_exact_selectors_lineage_and_deterministic_id() -> None:
    _evidence, _registry, _analysis, group, mapping = _mapping_fixture()
    assert mapping.mapping_schema_id == REPLAY_FIELD_MAPPING_CONTRACT_ID
    assert mapping.replay_field_mapping_id == derive_replay_field_mapping_id(
        mapping.identity_projection_dict()
    )
    assert mapping.raw_schema_fingerprint == group.raw_schema_fingerprint
    assert mapping.source_interpretation_contract_id.endswith("6" * 64)
    assert len(mapping.mapped_turn_fields) == 30 * 2 * 3
    assert len(mapping.field_dispositions) == len(group.fields)
    assert all(
        field.disposition is FieldDisposition.MAPPED
        for field in mapping.field_dispositions
        if field.selector.exact_header_name.startswith(("user_turn_", "oppo_turn_"))
    )
    assert any(
        field.disposition is FieldDisposition.UNMAPPED_PRESERVED_AT_RAW_LAYER
        and field.selector.exact_header_name == "game_index"
        for field in mapping.field_dispositions
    )
    assert len(mapping.field_lineage) == len(mapping.mapped_turn_fields) + 6
    lineage_paths = {item.normalized_field_path for item in mapping.field_lineage}
    assert {
        item.normalized_field_path
        for item in mapping.field_lineage
        if item.origin.value == "source_direct"
    } <= lineage_paths


def test_mapping_correction_changes_m5_identity_only() -> None:
    _evidence, _registry, analysis, _group_value, mapping = _mapping_fixture()
    corrected_projection = mapping.identity_projection_dict()
    corrected_projection["out_of_range_slot_policy"] = "strictly-reject-record-v2"
    corrected_id = derive_replay_field_mapping_id(corrected_projection)
    assert corrected_id != mapping.replay_field_mapping_id
    assert mapping.raw_schema_fingerprint == "a" * 64
    assert mapping.source_interpretation_contract_id.endswith("6" * 64)
    corrected = replace(
        mapping,
        replay_field_mapping_id=corrected_id,
        out_of_range_slot_policy="strictly-reject-record-v2",
    )
    original_registry = build_replay_mapping_registry(
        analysis, mapping_candidates=(mapping,), reviews=(_review(mapping),)
    )
    corrected_registry = build_replay_mapping_registry(
        analysis, mapping_candidates=(corrected,), reviews=(_review(corrected),)
    )
    assert corrected_registry.mapping_registry_digest != original_registry.mapping_registry_digest


def test_registry_explicitly_keeps_unreviewed_groups_and_binds_m3() -> None:
    evidence, registry, analysis, _group_value, mapping = _mapping_fixture()
    review = _review(mapping)
    result = build_replay_mapping_registry(
        analysis,
        mapping_candidates=(mapping,),
        reviews=(review,),
    )
    assert result.mapping_registry_contract_id == REPLAY_MAPPING_REGISTRY_CONTRACT_ID
    assert result.schema_registry_digest == registry.schema_registry_digest
    assert result.m3_evidence_digest == evidence.deep_inspection_evidence_digest
    assert len(result.group_dispositions) == 2
    assert result.supported_group_count == 1
    assert result.group_dispositions[0].disposition is MappingDisposition.SUPPORTED
    assert (
        result.group_dispositions[1].disposition is MappingDisposition.UNSUPPORTED_EVENT_STRUCTURE
    )
    final_analysis = apply_mapping_dispositions_to_analysis(analysis, result)
    assert final_analysis.groups[0].mapping_disposition is MappingDisposition.SUPPORTED
    assert final_analysis.groups[0].mapping_id == mapping.replay_field_mapping_id


def test_game_schema_cannot_be_assigned_a_replay_mapping() -> None:
    game_group = _group("a" * 64, source_kind="game")
    with pytest.raises(ReplaySchemaError, match="non-Replay"):
        build_turn_slot_mapping(
            game_group,
            source_interpretation_contract_id="openmtgdata.source-interpretation.v1:" + "6" * 64,
            m3_evidence_digest="3" * 64,
            source_archive_id_reviewed="b" * 64,
        )


def test_review_without_full_m4_completion_cannot_support_group() -> None:
    _evidence, _registry, analysis, _group_value, mapping = _mapping_fixture()
    review = replace(_review(mapping), m4_reader_completion_status="incomplete")
    with pytest.raises(ReplaySchemaError, match="support gate"):
        build_replay_mapping_registry(analysis, mapping_candidates=(mapping,), reviews=(review,))


def _supported_adapter_fixture():
    evidence, registry, analysis, group, mapping = _mapping_fixture()
    mapping_registry = build_replay_mapping_registry(
        analysis,
        mapping_candidates=(mapping,),
        reviews=(_review(mapping),),
    )
    return group, mapping_registry, ReplayAdapterV1(mapping_registry)


def _raw_turn_record(group: RawSchemaGroupEvidenceV1, *, turns: str = "3", on_play: str = "1"):
    values = [""] * len(group.fields)
    indexes = {field.exact_header_name: field.column_index for field in group.fields}
    values[indexes["turns"]] = turns
    values[indexes["on_play"]] = on_play
    values[indexes["user_turn_1_cards_drawn"]] = "001"
    values[indexes["user_turn_1_eot_user_life"]] = " 20 "
    values[indexes["oppo_turn_1_cards_drawn"]] = "null"
    values[indexes["oppo_turn_1_eot_oppo_life"]] = "20"
    values[indexes["user_turn_2_cards_drawn"]] = "1.0"
    values[indexes["user_turn_2_eot_user_life"]] = "19"
    # Out-of-range slots stay in raw authority but are not emitted as events.
    values[indexes["user_turn_3_cards_drawn"]] = "0"
    return RawCsvRecordV1(
        RAW_RECORD_CONTRACT_ID,
        READER_CONTRACT_ID,
        group.representative_source_archive_id,
        group.raw_schema_fingerprint,
        "openmtgdata.source-interpretation.v1:" + "6" * 64,
        17,
        tuple(values),
    )


def test_adapter_emits_chronological_turn_summary_slots_without_magic_semantics() -> None:
    group, registry, adapter = _supported_adapter_fixture()
    record = _raw_turn_record(group)
    header = group.field_names
    result = adapter.normalize_record(record, header_fields=header)
    assert result.rejection is None
    assert [event.locator.event_ordinal_within_source_record for event in result.events] == [
        0,
        1,
        2,
    ]
    assert [(event.source_turn_side, event.source_turn_slot_index) for event in result.events] == [
        ("user", 1),
        ("oppo", 1),
        ("user", 2),
    ]
    assert result.events[0].source_turn_count == 3
    assert result.events[0].source_on_play is True
    assert result.events[0].source_turn_count_raw == "3"
    assert result.events[0].source_on_play_raw == "1"
    first_values = {
        field.exact_header_name: field.value for field in result.events[0].source_fields
    }
    assert first_values["user_turn_1_cards_drawn"] == "001"
    assert first_values["user_turn_1_eot_user_life"] == " 20 "
    assert first_values["user_turn_1_eot_oppo_life"] == ""
    assert "legal_actions" not in result.events[0].semantic_projection_dict()
    assert (
        result.events[0].replay_field_mapping_id
        == registry.mapping_contracts[0].replay_field_mapping_id
    )


def test_on_play_selects_first_side_but_does_not_claim_acting_player() -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    record = _raw_turn_record(group, turns="4", on_play="0")
    values = list(record.fields)
    oppo_second_turn_card = next(
        field.column_index
        for field in group.fields
        if field.exact_header_name == "oppo_turn_2_cards_drawn"
    )
    values[oppo_second_turn_card] = "source-card-token"
    record = replace(record, fields=tuple(values))
    result = adapter.normalize_record(record, header_fields=group.field_names)
    assert [(event.source_turn_side, event.source_turn_slot_index) for event in result.events] == [
        ("oppo", 1),
        ("user", 1),
        ("oppo", 2),
        ("user", 2),
    ]


@pytest.mark.parametrize("value", ["", "+1", "01", " 1", "1 ", "1.0", "1e3", "-1"])
def test_strict_turn_count_rejects_noncanonical_or_invalid_values(
    value: str,
) -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    result = adapter.normalize_record(
        _raw_turn_record(group, turns=value), header_fields=group.field_names
    )
    assert not result.events
    assert result.rejection is not None
    assert result.rejection.quality_code is ReplayQualityCode.INVALID_SOURCE_TURN_COUNT
    assert result.row_quality_codes == ()


@pytest.mark.parametrize("value", ["", "true", "False", " 1", "2", "+1"])
def test_strict_on_play_rejects_unreviewed_boolean_lexemes(value: str) -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    result = adapter.normalize_record(
        _raw_turn_record(group, on_play=value), header_fields=group.field_names
    )
    assert result.rejection is not None
    assert result.rejection.quality_code is ReplayQualityCode.INVALID_SOURCE_ON_PLAY


def test_zero_turn_row_emits_no_event_and_is_explicit() -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    result = adapter.normalize_record(
        _raw_turn_record(group, turns="0"), header_fields=group.field_names
    )
    assert result.events == ()
    assert result.rejection is None
    assert result.row_quality_codes == (ReplayQualityCode.SOURCE_ZERO_TURN_COUNT,)


def test_turn_count_beyond_registered_slots_is_rejected() -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    result = adapter.normalize_record(
        _raw_turn_record(group, turns="61"), header_fields=group.field_names
    )
    assert not result.events
    assert result.rejection is not None
    assert result.rejection.quality_code is ReplayQualityCode.TURN_COUNT_EXCEEDS_PHYSICAL_SLOTS


def test_partial_active_turn_slot_rejects_the_source_record() -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    record = _raw_turn_record(group)
    values = list(record.fields)
    for field in group.fields:
        if field.exact_header_name.startswith("oppo_turn_1_"):
            values[field.column_index] = ""
    result = adapter.normalize_record(
        replace(record, fields=tuple(values)), header_fields=group.field_names
    )
    assert result.events == ()
    assert result.rejection is not None
    assert result.rejection.quality_code is ReplayQualityCode.PARTIAL_EVENT_SLOT


def test_header_and_m3_interpretation_mismatches_fail_closed() -> None:
    group, _registry, adapter = _supported_adapter_fixture()
    record = _raw_turn_record(group)
    with pytest.raises(ReplayNormalizationError, match="header selector"):
        adapter.normalize_record(record, header_fields=("wrong", *group.field_names[1:]))
    with pytest.raises(ReplayNormalizationError, match="interpretation identity"):
        adapter.normalize_record(
            replace(
                record,
                source_interpretation_contract_id="openmtgdata.source-interpretation.v1:"
                + "8" * 64,
            ),
            header_fields=group.field_names,
        )
    with pytest.raises(ReplayNormalizationError, match="unknown Replay raw fingerprint"):
        adapter.normalize_record(
            replace(record, raw_schema_fingerprint="9" * 64),
            header_fields=group.field_names,
        )


def test_m4_batch_boundary_does_not_change_replay_event_semantics() -> None:
    group, registry, adapter = _supported_adapter_fixture()
    first_record = _raw_turn_record(group)
    second_record = replace(first_record, data_record_ordinal=18)
    whole_batch = RawCsvBatchV1(
        BATCH_CONTRACT_ID,
        READER_CONTRACT_ID,
        first_record.source_archive_id,
        first_record.raw_schema_fingerprint,
        first_record.source_interpretation_contract_id,
        registry.schema_registry_digest,
        1,
        17,
        18,
        (first_record, second_record),
        (),
        2,
        2,
        0,
        16,
    )
    split_batches = (
        replace(
            whole_batch,
            batch_ordinal=8,
            last_data_record_ordinal=17,
            accepted_records=(first_record,),
            records_seen=1,
            records_accepted=1,
            payload_utf8_bytes=8,
        ),
        replace(
            whole_batch,
            batch_ordinal=9,
            first_data_record_ordinal=18,
            last_data_record_ordinal=18,
            accepted_records=(second_record,),
            records_seen=1,
            records_accepted=1,
            payload_utf8_bytes=8,
        ),
    )
    whole_events = tuple(
        event
        for result in adapter.normalize_batch(whole_batch, header_fields=group.field_names)
        for event in result.events
    )
    split_events = tuple(
        event
        for batch in split_batches
        for result in adapter.normalize_batch(batch, header_fields=tuple(group.field_names))
        for event in result.events
    )
    assert tuple(event.semantic_projection_dict() for event in whole_events) == tuple(
        event.semantic_projection_dict() for event in split_events
    )
    assert tuple(event.semantic_digest for event in whole_events) == tuple(
        event.semantic_digest for event in split_events
    )


def test_every_emitted_source_field_has_exact_indexed_lineage() -> None:
    group, registry, adapter = _supported_adapter_fixture()
    mapping = registry.mapping_contracts[0]
    lineage_paths = {item.normalized_field_path for item in mapping.field_lineage}
    assert {
        "source_turn_count_raw",
        "source_on_play_raw",
        "source_turn_count",
        "source_on_play",
        "source_turn_side",
        "source_turn_slot_index",
    } <= lineage_paths
    assert all(item.normalized_field_path in lineage_paths for item in mapping.mapped_turn_fields)
    result = adapter.normalize_record(_raw_turn_record(group), header_fields=group.field_names)
    for event in result.events:
        for value in event.source_fields:
            path = f"source_fields[{value.column_index}:{value.exact_header_name}]"
            assert path in lineage_paths
            assert group.field_names[value.column_index] == value.exact_header_name


def test_mapping_registry_permutation_has_same_digest() -> None:
    evidence, schema_registry, analysis, group, mapping = _mapping_fixture()
    review = _review(mapping)
    registry = build_replay_mapping_registry(
        analysis, mapping_candidates=(mapping,), reviews=(review,)
    )
    permuted_analysis = replace(analysis, groups=tuple(reversed(analysis.groups)))
    permuted = build_replay_mapping_registry(
        permuted_analysis, mapping_candidates=(mapping,), reviews=(review,)
    )
    assert registry.mapping_registry_digest == permuted.mapping_registry_digest
    assert registry.to_dict() == permuted.to_dict()


def test_real_replay_mapping_review_artifact_is_optional_and_ignored() -> None:
    analysis_path = Path("data/intermediate/m5.1/replay-field-analysis.json")
    if not analysis_path.exists():
        pytest.skip("local M5.1 analysis report is not generated")
    document = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert len(document["groups"]) == 41


def test_real_m3_replay_analysis_covers_41_groups_when_local_artifacts_exist() -> None:
    deep_path = Path("data/intermediate/m3.1/deep-inspection.json")
    registry_path = Path("data/intermediate/m3.2/schema-registry.json")
    if not deep_path.exists() or not registry_path.exists():
        pytest.skip("ignored local M3 evidence artifacts are unavailable")
    evidence = load_verified_m3_evidence(
        deep_path,
        expected_evidence_digest=(
            "2572d8825d5eff17ad779695102f97d1ebb04b607545b02d1001d48461744add"
        ),
        expected_source_catalog_digest=(
            "dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602"
        ),
    )
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
    analysis = build_replay_field_analysis(evidence, registry)
    assert len(analysis.groups) == 41
    assert sum(item.archive_count for item in analysis.groups) == 100
    assert all(
        len(item.exact_ordered_header_surface) == item.field_count for item in analysis.groups
    )
