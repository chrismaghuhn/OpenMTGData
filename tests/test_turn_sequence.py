from __future__ import annotations

import gzip
import json
from dataclasses import replace
from functools import lru_cache

import pytest

from openmtgdata.replay_schema import (
    REPLAY_EVENT_ORDINAL_RULE_ID,
    REPLAY_FIELD_MAPPING_CONTRACT_ID,
    REPLAY_FIELD_MAPPING_ID_CONTRACT_ID,
    REPLAY_NORMALIZATION_CONTRACT_ID,
    REPLAY_NORMALIZATION_QUALITY_CONTRACT_ID,
    STRICT_ON_PLAY_PARSER_ID,
    STRICT_TURN_COUNT_PARSER_ID,
    FieldDisposition,
    FieldOrigin,
    ReplayEventLocatorV1,
    ReplayEventV1,
    ReplayFieldMappingV1,
    ReplayFieldValueV1,
    ReplayIndexedFieldFamilyV1,
    ReplayMappingReviewV1,
    ReplayNormalizationRejectionV1,
    ReplayNormalizationResultV1,
    ReplayQualityCode,
    ReplaySourceFieldMappingV1,
    ReplayTurnSideV1,
    SourceColumnSelectorV1,
    derive_replay_field_mapping_id,
)
from openmtgdata.resumable_reader import source_reader_config_identity
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    BATCH_CONTRACT_ID,
    DIAGNOSTIC_CONTRACT_ID,
    READER_CONTRACT_ID,
    CompletionStatus,
    RawCsvBatchV1,
    RawCsvRecordV1,
    ReaderDiagnosticCode,
    SourceReaderConfigV1,
    SourceReaderDiagnosticV1,
    SourceReaderSummaryV1,
)
from openmtgdata.turn_sequence import (
    NEXT_TURN_OBJECTIVE_ID,
    NEXT_TURN_PREDICTION_VIEW_CONTRACT_ID,
    TURN_SUMMARY_FRAME_CONTRACT_ID,
    NextTurnPredictionViewV1,
    TurnSequenceBindingsV1,
    TurnSequenceBuildAccumulatorV1,
    TurnSequenceError,
    TurnSequenceLogicalDigestV1,
    TurnSummarySequenceV1,
    canonical_suffix_order,
    frame_from_replay_event,
    iter_next_turn_views,
    sequence_from_normalization_result,
    source_selectors_for_turn_slot,
    validate_build_report,
)
from scripts.build_m7_2_turn_sequences import _SequenceShardWriter

SOURCE_ID = "a" * 64
RAW_FP = "b" * 64
INTERPRETATION_ID = "openmtgdata.source-interpretation.v1:" + "c" * 64
MAPPING_REGISTRY_DIGEST = "d" * 64
M5_REPLAY_DIGEST = "e" * 64
SUFFIXES = tuple(f"field_{index:02d}" for index in range(33))


@lru_cache(maxsize=1)
def _mapping() -> ReplayFieldMappingV1:
    families = (
        ReplayIndexedFieldFamilyV1("user_turn", 1, 30, 30, SUFFIXES, True, True),
        ReplayIndexedFieldFamilyV1("oppo_turn", 1, 30, 30, SUFFIXES, True, True),
    )
    mapped_fields: list[ReplaySourceFieldMappingV1] = []
    for side_index, side in enumerate(("user", "oppo")):
        for slot in range(1, 31):
            for suffix_index, suffix in enumerate(SUFFIXES):
                column_index = 2 + (side_index * 30 + slot - 1) * len(SUFFIXES) + suffix_index
                header = f"{side}_turn_{slot}_{suffix}"
                selector = SourceColumnSelectorV1(column_index, header)
                mapped_fields.append(
                    ReplaySourceFieldMappingV1(
                        selector,
                        side,
                        slot,
                        suffix,
                        f"source_fields[{column_index}:{header}]",
                        FieldDisposition.MAPPED,
                        FieldOrigin.SOURCE_DIRECT,
                        "openmtgdata.preserve-csv-string.v1",
                        "openmtgdata.preserve-exact-csv-field-string.v1",
                        "preserve_empty_string",
                        "preserve_exact_parser_string",
                    )
                )
    candidate = ReplayFieldMappingV1(
        REPLAY_FIELD_MAPPING_CONTRACT_ID,
        "",
        REPLAY_FIELD_MAPPING_ID_CONTRACT_ID,
        RAW_FP,
        INTERPRETATION_ID,
        SourceKind.REPLAY,
        "openmtgdata.replay-boundary.per-side-turn-slot.v1",
        REPLAY_EVENT_ORDINAL_RULE_ID,
        SourceColumnSelectorV1(0, "turns"),
        SourceColumnSelectorV1(1, "on_play"),
        families,
        tuple(mapped_fields),
        (),
        (),
        STRICT_TURN_COUNT_PARSER_ID,
        STRICT_ON_PLAY_PARSER_ID,
        60,
        "reject_source_record_if_every_field_in_active_slot_is_empty",
        "do_not_emit slots beyond source turn count",
        "reject only invalid source turn metadata or partial active slots",
        REPLAY_NORMALIZATION_QUALITY_CONTRACT_ID,
        "source_direct or deterministic_normalization only",
        "no reconstructed gameplay facts",
        ("synthetic contract fixture",),
    )
    return replace(
        candidate,
        replay_field_mapping_id=derive_replay_field_mapping_id(
            candidate.identity_projection_dict()
        ),
    )


def _bindings(*, m5_digest: str = M5_REPLAY_DIGEST) -> TurnSequenceBindingsV1:
    return TurnSequenceBindingsV1(
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "4" * 64,
        456,
        INTERPRETATION_ID,
        READER_CONTRACT_ID,
        source_reader_config_identity(SourceReaderConfigV1()).source_reader_config_digest,
        MAPPING_REGISTRY_DIGEST,
        m5_digest,
    )


def _event(
    ordinal: int, turn_count: int, *, row: int, on_play: bool, value_tag: str = ""
) -> ReplayEventV1:
    mapping = _mapping()
    first_side = "user" if on_play else "oppo"
    second_side = "oppo" if on_play else "user"
    side = first_side if ordinal % 2 == 0 else second_side
    slot = ordinal // 2 + 1
    source_fields = []
    for item in mapping.mapped_turn_fields:
        if item.event_side != side or item.event_slot_index != slot:
            continue
        if item.exact_suffix == SUFFIXES[0]:
            value = ""
        elif item.exact_suffix == SUFFIXES[1]:
            value = "001"
        elif item.exact_suffix == SUFFIXES[2]:
            value = "  padded source text  "
        elif item.exact_suffix == SUFFIXES[3]:
            value = "A|B"
        else:
            value = f"{value_tag}row-{ordinal}-{item.exact_suffix}"
        source_fields.append(
            ReplayFieldValueV1(
                item.selector.column_index,
                item.selector.exact_header_name,
                value,
            )
        )
    return ReplayEventV1(
        "openmtgdata.replay-event.v1",
        SOURCE_ID,
        RAW_FP,
        INTERPRETATION_ID,
        mapping.replay_field_mapping_id,
        ReplayEventLocatorV1(SOURCE_ID, row, ordinal),
        str(turn_count),
        "1" if on_play else "0",
        ReplayTurnSideV1(side),
        slot,
        turn_count,
        on_play,
        tuple(source_fields),
        (ReplayQualityCode.UNMAPPED_SOURCE_FIELDS_PRESENT,),
    )


def _normalization(
    frame_count: int, *, row: int = 17, on_play: bool = True, value_tag: str = ""
) -> ReplayNormalizationResultV1:
    events = tuple(
        _event(ordinal, frame_count, row=row, on_play=on_play, value_tag=value_tag)
        for ordinal in range(frame_count)
    )
    return ReplayNormalizationResultV1(
        REPLAY_NORMALIZATION_CONTRACT_ID,
        SOURCE_ID,
        row,
        events,
        (ReplayQualityCode.UNMAPPED_SOURCE_FIELDS_PRESENT,),
        None,
    )


def _sequence(
    frame_count: int = 5, *, row: int = 17, on_play: bool = True, value_tag: str = ""
) -> TurnSummarySequenceV1:
    return sequence_from_normalization_result(
        _normalization(frame_count, row=row, on_play=on_play, value_tag=value_tag),
        _mapping(),
        _bindings(),
    )


def test_frame_projects_one_m5_event_and_preserves_exact_strings() -> None:
    event = _event(0, 2, row=17, on_play=True)
    frame = frame_from_replay_event(event, _mapping())
    assert frame.event_ordinal_within_source_record == 0
    assert frame.source_turn_side == "user"
    assert frame.source_turn_slot_index == 1
    assert len(frame.field_values) == 33
    assert frame.field_values[0] == ""
    assert frame.field_values[1] == "001"
    assert frame.field_values[2] == "  padded source text  "
    assert frame.field_values[3] == "A|B"
    assert frame.to_dict()["frame_contract_id"] == TURN_SUMMARY_FRAME_CONTRACT_ID


def test_suffix_order_and_exact_selector_lineage_are_recoverable() -> None:
    mapping = _mapping()
    order = canonical_suffix_order(mapping)
    assert order == SUFFIXES
    selectors = source_selectors_for_turn_slot(
        mapping,
        source_turn_side="oppo",
        source_turn_slot_index=2,
        field_suffix_order=order,
    )
    assert tuple(item[0] for item in selectors) == order
    assert all(name == f"oppo_turn_2_{suffix}" for suffix, _index, name in selectors)
    assert len({index for _suffix, index, _name in selectors}) == 33
    assert (
        frame_from_replay_event(_event(1, 2, row=17, on_play=True), mapping).source_turn_side
        == "oppo"
    )


def test_m5_event_field_input_order_does_not_change_frame_projection() -> None:
    event = _event(0, 2, row=17, on_play=True)
    permuted = replace(event, source_fields=tuple(reversed(event.source_fields)))
    assert frame_from_replay_event(event, _mapping()) == frame_from_replay_event(
        permuted, _mapping()
    )


def test_non_replayevent_or_mismatched_mapping_fails_closed() -> None:
    with pytest.raises((TurnSequenceError, AttributeError)):
        frame_from_replay_event({"fields": []}, _mapping())  # type: ignore[arg-type]
    with pytest.raises(TurnSequenceError, match="mapping ID"):
        frame_from_replay_event(
            replace(_event(0, 2, row=17, on_play=True), replay_field_mapping_id="x"),
            _mapping(),
        )


def test_sequence_has_no_row_global_or_policy_perspective_fields() -> None:
    sequence = _sequence(3)
    serialized = json.dumps(sequence.to_dict(), sort_keys=True)
    for forbidden in (
        "source_turn_count",
        "source_turn_count_raw",
        "source_on_play",
        "won",
        "total_cards",
        "DecisionSampleV1",
        "observed_action",
        "legal_actions",
        "optimal_action",
        "SELF",
        "OPPONENT",
        "PLAYER_0",
        "PLAYER_1",
    ):
        assert forbidden not in serialized
    assert sequence.usable_for_policy_imitation is False
    assert sequence.actor_perspective_safe is False
    assert sequence.information_scope == "source_record_summary_not_actor_observation"
    assert {frame.source_turn_side for frame in sequence.frames} == {"user", "oppo"}


@pytest.mark.parametrize(("frame_count", "expected_views"), [(2, 1), (5, 4)])
def test_next_turn_view_count_and_prefix_boundaries(frame_count: int, expected_views: int) -> None:
    sequence = _sequence(frame_count)
    views = tuple(iter_next_turn_views(sequence))
    assert len(views) == expected_views
    for target_ordinal, view in enumerate(views, start=1):
        assert view.target_event_ordinal == target_ordinal
        materialized = view.materialize_model_projection(sequence)
        projection = materialized["model_projection"]
        assert len(projection["context"]) == target_ordinal
        assert [
            frame["event_ordinal_within_source_record"] for frame in projection["context"]
        ] == list(range(target_ordinal))
        assert projection["target_frame"]["event_ordinal_within_source_record"] == target_ordinal
        assert materialized["metadata"]["usable_for_policy_imitation"] is False
        assert materialized["metadata"]["actor_perspective_safe"] is False
        assert materialized["metadata"]["objective_id"] == NEXT_TURN_OBJECTIVE_ID
        assert projection["view_contract_id"] == NEXT_TURN_PREDICTION_VIEW_CONTRACT_ID


def test_target_zero_is_not_emitted_and_view_descriptor_has_no_prefix_payload() -> None:
    sequence = _sequence(5)
    assert [view.target_event_ordinal for view in iter_next_turn_views(sequence)] == [1, 2, 3, 4]
    with pytest.raises(TurnSequenceError, match="ordinal zero"):
        NextTurnPredictionViewV1(sequence.identity_projection, 0)
    descriptor = next(iter_next_turn_views(sequence)).to_dict()
    assert "context" not in descriptor
    assert "frames" not in descriptor
    assert "target_frame" not in descriptor


def test_future_frame_mutation_cannot_change_an_earlier_view() -> None:
    sequence = _sequence(5)
    view = tuple(iter_next_turn_views(sequence))[1]  # target ordinal 2
    original = view.materialize_model_projection(sequence)
    future = replace(
        sequence.frames[4],
        field_values=("FUTURE_CHANGED",) + sequence.frames[4].field_values[1:],
    )
    changed_sequence = replace(sequence, frames=sequence.frames[:4] + (future,))
    assert sequence.identity_projection == changed_sequence.identity_projection
    assert view.semantic_identity_projection_dict() == view.semantic_identity_projection_dict()
    assert view.materialize_model_projection(changed_sequence) == original
    assert sequence.semantic_digest != changed_sequence.semantic_digest


def test_target_mutation_does_not_change_context() -> None:
    sequence = _sequence(4)
    view = tuple(iter_next_turn_views(sequence))[1]  # target ordinal 2
    before = view.materialize_model_projection(sequence)["model_projection"]
    target = replace(
        sequence.frames[2],
        field_values=("TARGET_CHANGED",) + sequence.frames[2].field_values[1:],
    )
    changed = replace(sequence, frames=sequence.frames[:2] + (target,) + sequence.frames[3:])
    after = view.materialize_model_projection(changed)["model_projection"]
    assert after["context"] == before["context"]
    assert after["target_frame"] != before["target_frame"]


def test_sequence_rejects_invalid_ordinals_slot_indexes_and_source_side_order() -> None:
    sequence = _sequence(3)
    with pytest.raises(TurnSequenceError, match="contiguous"):
        replace(
            sequence,
            frames=(
                sequence.frames[0],
                replace(sequence.frames[1], event_ordinal_within_source_record=5),
                sequence.frames[2],
            ),
        )
    with pytest.raises(TurnSequenceError, match="side-local"):
        replace(
            sequence,
            frames=(
                sequence.frames[0],
                replace(sequence.frames[1], source_turn_slot_index=3),
                sequence.frames[2],
            ),
        )
    with pytest.raises(TurnSequenceError, match="alternate"):
        replace(
            sequence,
            frames=(
                sequence.frames[0],
                replace(sequence.frames[1], source_turn_side=sequence.frames[0].source_turn_side),
                sequence.frames[2],
            ),
        )


def test_sequence_requires_all_33_suffix_values_and_excludes_global_fields_from_view() -> None:
    sequence = _sequence(2)
    with pytest.raises(TurnSequenceError, match="33 unique"):
        replace(sequence, field_suffix_order=sequence.field_suffix_order[:32])
    model_projection = next(iter_next_turn_views(sequence)).materialize_model_projection(sequence)
    serialized = json.dumps(model_projection, sort_keys=True)
    for forbidden in ("source_turn_count", "source_on_play", "won", "user_total_", "oppo_total_"):
        assert forbidden not in serialized


def test_logical_digest_is_deterministic_and_binds_values_and_order() -> None:
    sequence = _sequence(3)
    first = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    second = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    first.update(sequence)
    second.update(sequence)
    assert first.hexdigest == second.hexdigest
    changed_value = replace(
        sequence.frames[1],
        field_values=("CHANGED",) + sequence.frames[1].field_values[1:],
    )
    changed = replace(sequence, frames=(sequence.frames[0], changed_value, sequence.frames[2]))
    changed_digest = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    changed_digest.update(changed)
    assert changed_digest.hexdigest != first.hexdigest

    two_frames = _sequence(2)
    forward_digest = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    forward_digest.update(two_frames)
    reversed_frames = (
        replace(two_frames.frames[1], event_ordinal_within_source_record=0),
        replace(two_frames.frames[0], event_ordinal_within_source_record=1),
    )
    reversed_sequence = replace(two_frames, frames=reversed_frames)
    reversed_digest = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    reversed_digest.update(reversed_sequence)
    assert reversed_digest.hexdigest != forward_digest.hexdigest


def test_logical_digest_is_independent_of_shard_grouping_and_runtime_paths() -> None:
    sequences = (_sequence(2, row=1), _sequence(3, row=2), _sequence(5, row=3))
    unsplit = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    for sequence in sequences:
        unsplit.update(sequence)
    split = TurnSequenceLogicalDigestV1(_bindings().to_dict())
    for shard_group in ((sequences[0],), (sequences[1], sequences[2])):
        for sequence in shard_group:
            split.update(sequence)
    assert split.hexdigest == unsplit.hexdigest
    descriptor = json.dumps(next(iter_next_turn_views(sequences[0])).to_dict())
    assert "runtime_path" not in descriptor
    assert "created_at" not in descriptor
    assert "shard" not in descriptor


def test_wrong_raw_fingerprint_or_interpretation_fails_closed() -> None:
    event = _event(0, 2, row=17, on_play=True)
    with pytest.raises(TurnSequenceError, match="fingerprint"):
        frame_from_replay_event(replace(event, raw_schema_fingerprint="f" * 64), _mapping())
    with pytest.raises(TurnSequenceError, match="interpretation"):
        frame_from_replay_event(
            replace(
                event,
                source_interpretation_contract_id="openmtgdata.source-interpretation.v1:"
                + "f" * 64,
            ),
            _mapping(),
        )


def test_m5_rejected_rows_never_emit_a_sequence() -> None:
    rejection = ReplayNormalizationRejectionV1(
        99,
        ReplayQualityCode.INVALID_SOURCE_TURN_COUNT,
        "synthetic reject",
        (SourceColumnSelectorV1(0, "turns"),),
    )
    result = ReplayNormalizationResultV1(
        REPLAY_NORMALIZATION_CONTRACT_ID,
        SOURCE_ID,
        99,
        (),
        (),
        rejection,
    )
    with pytest.raises(TurnSequenceError, match="M5-rejected"):
        sequence_from_normalization_result(result, _mapping(), _bindings())


def _m5_review() -> ReplayMappingReviewV1:
    return ReplayMappingReviewV1(
        RAW_FP,
        SOURCE_ID,
        CompletionStatus.COMPLETE.value,
        4,
        3,
        1,
        3,
        2,
        1,
        7,
        0,
        0,
        0,
        ((2, 1), (5, 1)),
        (("oppo", 4), ("user", 3)),
        "m4-full-stream-verified",
    )


def test_build_report_reconciles_and_derives_virtual_count() -> None:
    config = SourceReaderConfigV1()
    bindings = _bindings()
    accumulator = TurnSequenceBuildAccumulatorV1(
        bindings=bindings,
        source_archive_id=SOURCE_ID,
        compressed_sha256="4" * 64,
        compressed_size_bytes=456,
        raw_schema_fingerprint=RAW_FP,
        replay_field_mapping_id=_mapping().replay_field_mapping_id,
        replay_mapping_registry_digest=MAPPING_REGISTRY_DIGEST,
        m5_semantic_validation_digest=M5_REPLAY_DIGEST,
        reader_config=config,
        m5_review=_m5_review(),
    )
    accepted = tuple(
        RawCsvRecordV1(
            "openmtgdata.raw-csv-record.v1",
            READER_CONTRACT_ID,
            SOURCE_ID,
            RAW_FP,
            INTERPRETATION_ID,
            ordinal,
            (),
        )
        for ordinal in (1, 2, 3)
    )
    diagnostic = SourceReaderDiagnosticV1(
        DIAGNOSTIC_CONTRACT_ID,
        ReaderDiagnosticCode.ROW_WIDTH_LONGER,
        SOURCE_ID,
        "synthetic.csv.gz",
        4,
        "rejected",
        "synthetic width fixture",
    )
    batch = RawCsvBatchV1(
        BATCH_CONTRACT_ID,
        READER_CONTRACT_ID,
        SOURCE_ID,
        RAW_FP,
        INTERPRETATION_ID,
        "3" * 64,
        1,
        1,
        4,
        accepted,
        (diagnostic,),
        4,
        3,
        1,
        0,
    )
    accumulator.observe_batch(batch)
    first = _normalization(2, row=1, on_play=True)
    second = _normalization(5, row=2, on_play=False)
    accumulator.observe_normalization(
        first, sequence_from_normalization_result(first, _mapping(), bindings)
    )
    accumulator.observe_normalization(
        second, sequence_from_normalization_result(second, _mapping(), bindings)
    )
    rejected = ReplayNormalizationResultV1(
        REPLAY_NORMALIZATION_CONTRACT_ID,
        SOURCE_ID,
        3,
        (),
        (),
        ReplayNormalizationRejectionV1(
            3,
            ReplayQualityCode.INVALID_SOURCE_TURN_COUNT,
            "synthetic reject",
            (SourceColumnSelectorV1(0, "turns"),),
        ),
    )
    accumulator.observe_normalization(rejected, None)
    summary = SourceReaderSummaryV1(
        "openmtgdata.source-reader-summary.v1",
        READER_CONTRACT_ID,
        SOURCE_ID,
        "4" * 64,
        456,
        SourceKind.REPLAY,
        RAW_FP,
        INTERPRETATION_ID,
        "3" * 64,
        (),
        4,
        3,
        1,
        ((ReaderDiagnosticCode.ROW_WIDTH_LONGER.value, 1),),
        1,
        CompletionStatus.COMPLETE,
        456,
        "valid",
    )
    report = accumulator.finish(summary)
    validate_build_report(report)
    assert report.sequences_emitted == 2
    assert report.frames_emitted == 7
    assert report.virtual_next_turn_views == 5
    assert report.minimum_sequence_length == 2
    assert report.maximum_sequence_length == 5
    assert dict(report.source_side_frame_counts) == {"oppo": 4, "user": 3}


def test_report_never_marks_incomplete_source_as_complete() -> None:
    accumulator = TurnSequenceBuildAccumulatorV1(
        bindings=_bindings(),
        source_archive_id=SOURCE_ID,
        compressed_sha256="4" * 64,
        compressed_size_bytes=456,
        raw_schema_fingerprint=RAW_FP,
        replay_field_mapping_id=_mapping().replay_field_mapping_id,
        replay_mapping_registry_digest=MAPPING_REGISTRY_DIGEST,
        m5_semantic_validation_digest=M5_REPLAY_DIGEST,
        reader_config=SourceReaderConfigV1(),
        m5_review=_m5_review(),
    )
    incomplete = SourceReaderSummaryV1(
        "openmtgdata.source-reader-summary.v1",
        READER_CONTRACT_ID,
        SOURCE_ID,
        "4" * 64,
        456,
        SourceKind.REPLAY,
        RAW_FP,
        INTERPRETATION_ID,
        "3" * 64,
        (),
        0,
        0,
        0,
        (),
        0,
        CompletionStatus.INCOMPLETE_CONSUMER_STOP,
        0,
        "not_verified",
    )
    with pytest.raises(TurnSequenceError, match="completed M4"):
        accumulator.finish(incomplete)


def test_local_turn_sequence_shards_serialize_each_sequence_once(tmp_path) -> None:
    sequences = (_sequence(2, row=1), _sequence(3, row=2))
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    writer = _SequenceShardWriter(shard_dir, sequences_per_shard=1)
    for sequence in sequences:
        writer.write(sequence)
    writer.close()
    assert writer.sequences_written == 2
    assert writer.frames_written == 5
    assert len(writer.shards) == 2
    documents = []
    for shard in sorted(shard_dir.glob("*.jsonl.gz")):
        with gzip.open(shard, "rt", encoding="utf-8") as stream:
            documents.extend(json.loads(line) for line in stream)
    assert len(documents) == 2
    assert sum(document["frame_count"] for document in documents) == 5
    assert all("context" not in document for document in documents)


def test_no_game_join_policy_view_or_decision_sample_contract_is_introduced() -> None:
    sequence = _sequence(2)
    projection = json.dumps(sequence.to_dict(), sort_keys=True)
    view = next(iter_next_turn_views(sequence)).to_dict()
    assert view["usable_for_policy_imitation"] is False
    assert view["actor_perspective_safe"] is False
    for forbidden in ("GameRecordV1", "DecisionSampleV1", "legal_actions", "optimal_action"):
        assert forbidden not in projection
        assert forbidden not in json.dumps(view)
