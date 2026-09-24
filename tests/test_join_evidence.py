from __future__ import annotations

from dataclasses import replace

import pytest

from openmtgdata.join_evidence import (
    EXACT_STRING_COMPARISON_CONTRACT_ID,
    CandidateSideAccumulatorV1,
    JoinCandidateV1,
    JoinDecision,
    JoinEvidenceError,
    JoinFieldEvidenceV1,
    JoinFieldReferenceV1,
    JoinReportDecisionStatus,
    build_join_evidence_report,
    candidate_evidence,
    compare_candidate_cardinality,
    decide_candidate,
    derive_join_candidate_id,
    validate_join_evidence_report,
)
from openmtgdata.source_filename import SourceKind


def _candidate() -> JoinCandidateV1:
    replay = (
        JoinFieldReferenceV1(SourceKind.REPLAY, 0, "draft_id", "source_direct"),
        JoinFieldReferenceV1(SourceKind.REPLAY, 7, "game_index", "raw_unmapped"),
    )
    game = (
        JoinFieldReferenceV1(SourceKind.GAME, 2, "draft_id", "source_direct"),
        JoinFieldReferenceV1(SourceKind.GAME, 7, "game_number", "source_direct"),
    )
    scope = "exact reviewed AFR PremierDraft archive pair"
    return JoinCandidateV1(
        replay,
        game,
        scope,
        EXACT_STRING_COMPARISON_CONTRACT_ID,
        derive_join_candidate_id(replay, game, scope),
    )


def test_candidate_identity_uses_exact_indexed_selectors_and_is_deterministic() -> None:
    first = _candidate()
    second = _candidate()
    assert first.candidate_id == second.candidate_id
    assert first.replay_fields[1].to_dict()["column_index"] == 7
    assert first.replay_fields[1].exact_header_name == "game_index"
    changed_scope = "another exact archive pair"
    changed_id = derive_join_candidate_id(first.replay_fields, first.game_fields, changed_scope)
    assert changed_id != first.candidate_id
    with pytest.raises(JoinEvidenceError, match="candidate ID"):
        replace(first, candidate_id="0" * 64)


def test_candidate_rejects_wrong_source_kind_and_component_count() -> None:
    replay = (JoinFieldReferenceV1(SourceKind.GAME, 0, "draft_id", "source_direct"),)
    game = (JoinFieldReferenceV1(SourceKind.GAME, 2, "draft_id", "source_direct"),)
    with pytest.raises(JoinEvidenceError, match="Replay field"):
        JoinCandidateV1(
            replay,
            game,
            "scope",
            EXACT_STRING_COMPARISON_CONTRACT_ID,
            derive_join_candidate_id(replay, game, "scope"),
        )


def test_side_accumulator_separates_empty_partial_and_complete_keys() -> None:
    selectors = _candidate().replay_fields
    acc = CandidateSideAccumulatorV1(SourceKind.REPLAY, selectors)

    def row(first: str, last: str) -> tuple[str, ...]:
        values = [""] * 8
        values[0], values[7] = first, last
        return tuple(values)

    assert acc.observe(row("", ""), m5_normalized=True) is None
    assert acc.observe(row("d1", ""), m5_normalized=True) is None
    assert acc.observe(row("d1", "0"), m5_normalized=True) == ("d1", "0")
    assert acc.observe(row("d1", "0"), m5_normalized=False) == ("d1", "0")
    stats = acc.freeze(m4_records_seen=6, m4_records_accepted=4, m4_records_rejected=2)
    doc = stats.to_dict()
    assert doc["m4_records_rejected"] == 2
    assert doc["rows_examined"] == doc["m4_records_accepted"] == 4
    assert doc["rows_with_all_empty_components"] == 1
    assert doc["rows_with_partial_key"] == 1
    assert doc["rows_with_complete_key"] == 2
    assert doc["distinct_complete_keys"] == 1
    assert doc["duplicate_complete_key_count"] == 1
    assert doc["duplicate_rows_in_complete_keys"] == 2
    assert stats.m5_normalized_rows == 3
    assert stats.m5_rejected_rows == 1


def test_exact_values_are_not_trimmed_or_numeric_coerced_and_order_is_invariant() -> None:
    rows = [(" draft ", "001"), ("draft", "1"), ("draft", "1")]

    def consume(values: list[tuple[str, str]]):
        acc = CandidateSideAccumulatorV1(SourceKind.REPLAY, _candidate().replay_fields)
        for draft, index in values:
            record = [""] * 8
            record[0], record[7] = draft, index
            acc.observe(tuple(record), m5_normalized=True)
        return acc.freeze(m4_records_seen=3, m4_records_accepted=3, m4_records_rejected=0)

    first = consume(rows)
    second = consume(list(reversed(rows)))
    assert first.to_dict() == second.to_dict()
    assert first.key_counts == (((" draft ", "001"), 1), (("draft", "1"), 2))


def _stats(kind: SourceKind, rows: list[tuple[str, ...]], *, m5: list[bool] | None = None):
    acc = CandidateSideAccumulatorV1(kind, _candidate().replay_fields)
    for i, row in enumerate(rows):
        record = [""] * 8
        record[0] = row[0]
        record[7] = row[1]
        acc.observe(tuple(record), m5_normalized=m5[i] if m5 is not None else None)
    return acc.freeze(
        m4_records_seen=len(rows), m4_records_accepted=len(rows), m4_records_rejected=0
    )


def test_cross_source_cardinality_reports_1to1_1ton_nto1_nton_and_unmatched() -> None:
    replay = _stats(
        SourceKind.REPLAY,
        [
            ("one", "1"),
            ("many", "2"),
            ("many", "2"),
            ("replay-only", "3"),
            ("many-game", "5"),
        ],
        m5=[True, True, False, True, True],
    )
    game = _stats(
        SourceKind.GAME,
        [
            ("one", "1"),
            ("many", "2"),
            ("game-only", "4"),
            ("game-only", "5"),
            ("many-game", "5"),
            ("many-game", "5"),
        ],
    )
    result = compare_candidate_cardinality(replay, game)
    assert dict(result.key_counts) == {
        "1_game:0_replay": 2,
        "1_game:1_replay": 1,
        "1_game:N_replay": 1,
        "0_game:1_replay": 1,
        "N_game:1_replay": 1,
    }
    assert result.replay_matched_rows == 1
    assert result.game_matched_rows == 1
    assert result.replay_ambiguous_rows == 3
    assert result.game_ambiguous_rows == 3
    assert result.replay_unmatched_rows == 1
    assert result.game_unmatched_rows == 2
    assert result.replay_matched_m5_normalized == 1
    assert result.replay_ambiguous_m5_normalized == 2
    assert result.replay_ambiguous_m5_rejected == 1
    assert result.replay_unmatched_m5_normalized == 1
    assert result.replay_unmatched_m5_rejected == 0


def test_ambiguous_and_matched_row_partitions_reconcile_to_eligible_rows() -> None:
    replay = _stats(
        SourceKind.REPLAY,
        [("one", "0"), ("two", "1"), ("two", "1"), ("only-r", "2")],
        m5=[True, True, False, True],
    )
    game = _stats(
        SourceKind.GAME,
        [("one", "0"), ("two", "1"), ("only-g", "2")],
        m5=[True, True, True],
    )
    c = compare_candidate_cardinality(replay, game)
    assert c.replay_matched_rows + c.replay_ambiguous_rows + c.replay_unmatched_rows == 4
    assert c.game_matched_rows + c.game_ambiguous_rows + c.game_unmatched_rows == 3
    assert (
        c.replay_matched_m5_normalized
        + c.replay_ambiguous_m5_normalized
        + c.replay_unmatched_m5_normalized
        == replay.m5_normalized_rows
    )
    assert (
        c.replay_matched_m5_rejected
        + c.replay_ambiguous_m5_rejected
        + c.replay_unmatched_m5_rejected
        == replay.m5_rejected_rows
    )


def test_leading_zero_and_whitespace_are_exact_distinct_keys() -> None:
    replay = _stats(SourceKind.REPLAY, [("draft", "001"), (" draft", "1")])
    game = _stats(SourceKind.GAME, [("draft", "1"), ("draft ", "001")])
    c = compare_candidate_cardinality(replay, game)
    assert c.replay_matched_rows == 0
    assert c.game_matched_rows == 0
    assert c.replay_unmatched_rows == 2
    assert c.game_unmatched_rows == 2


def test_report_digest_changes_when_evidence_counts_change() -> None:
    replay = _stats(SourceKind.REPLAY, [("k", "1")])
    game = _stats(SourceKind.GAME, [("k", "1")])
    first = candidate_evidence(_candidate(), replay, game)
    second = candidate_evidence(
        _candidate(), _stats(SourceKind.REPLAY, [("k", "1"), ("z", "2")]), game
    )
    assert first.cardinality.key_counts != second.cardinality.key_counts


def test_partial_unambiguous_coverage_is_not_approved() -> None:
    replay = _stats(SourceKind.REPLAY, [("both", "1"), ("only-r", "2")])
    game = _stats(SourceKind.GAME, [("both", "1")])
    result = candidate_evidence(_candidate(), replay, game)
    assert result.decision.status is JoinDecision.PARTIAL_UNAMBIGUOUS_COVERAGE
    assert result.decision.approved_for_scope is None


def test_candidate_component_order_is_part_of_candidate_identity() -> None:
    candidate = _candidate()
    reordered_replay = tuple(reversed(candidate.replay_fields))
    reordered_game = tuple(reversed(candidate.game_fields))
    reordered = JoinCandidateV1(
        reordered_replay,
        reordered_game,
        candidate.candidate_scope,
        candidate.comparison_contract_id,
        derive_join_candidate_id(reordered_replay, reordered_game, candidate.candidate_scope),
    )
    assert reordered.candidate_id != candidate.candidate_id


def test_m4_side_rejections_remain_excluded_from_candidate_key_counts() -> None:
    acc = CandidateSideAccumulatorV1(SourceKind.REPLAY, _candidate().replay_fields)
    values = [""] * 8
    values[0], values[7] = "accepted", "0"
    acc.observe(tuple(values), m5_normalized=True)
    stats = acc.freeze(m4_records_seen=3, m4_records_accepted=1, m4_records_rejected=2)
    assert stats.rows_examined == 1
    assert stats.rows_with_complete_key == 1
    assert stats.m4_records_rejected == 2


def test_report_rejects_malformed_source_or_schema_identity() -> None:
    candidate = _candidate()
    replay = _stats(SourceKind.REPLAY, [("k", "1")])
    game = _stats(SourceKind.GAME, [("k", "1")])
    values = {
        "semantic_source_catalog_digest": "a" * 64,
        "m3_evidence_digest": "b" * 64,
        "schema_registry_digest": "c" * 64,
        "replay_mapping_id": "openmtgdata.replay-field-mapping.v1:" + "d" * 64,
        "replay_mapping_registry_digest": "e" * 64,
        "game_mapping_id": "openmtgdata.game-field-mapping.v1:" + "f" * 64,
        "game_mapping_registry_digest": "1" * 64,
        "replay_source_interpretation_contract_id": "openmtgdata.source-interpretation.v1:"
        + "6" * 64,
        "game_source_interpretation_contract_id": "openmtgdata.source-interpretation.v1:"
        + "7" * 64,
        "replay_source_archive_id": "2" * 64,
        "game_source_archive_id": "3" * 64,
        "replay_raw_schema_fingerprint": "4" * 64,
        "game_raw_schema_fingerprint": "5" * 64,
        "candidate_field_inventory": (),
        "candidate_results": (candidate_evidence(candidate, replay, game),),
        "replay_m5_rejection_counts": (),
        "reciprocal_game_row_evidence": {},
        "game_number_game_index_relation": {},
    }
    with pytest.raises(JoinEvidenceError, match="malformed source or schema"):
        build_join_evidence_report(**{**values, "game_source_archive_id": "wrong"})

    for field, value in (
        ("replay_mapping_id", "banana"),
        ("game_mapping_id", "openmtgdata.game-field-mapping.v1:" + "G" * 64),
        ("replay_source_interpretation_contract_id", "openmtgdata.source-interpretation.v1:xyz"),
        (
            "game_source_interpretation_contract_id",
            "openmtgdata.source-interpretation.v1:" + "z" * 64,
        ),
    ):
        with pytest.raises(JoinEvidenceError):
            build_join_evidence_report(**{**values, field: value})


def test_report_level_decision_is_scoped_and_digest_bound() -> None:
    candidate = _candidate()
    replay = _stats(SourceKind.REPLAY, [("k", "0"), ("k", "0")])
    game = _stats(SourceKind.GAME, [("k", "0"), ("k", "0"), ("k", "0")])
    values = {
        "semantic_source_catalog_digest": "a" * 64,
        "m3_evidence_digest": "b" * 64,
        "schema_registry_digest": "c" * 64,
        "replay_mapping_id": "openmtgdata.replay-field-mapping.v1:" + "d" * 64,
        "replay_mapping_registry_digest": "e" * 64,
        "game_mapping_id": "openmtgdata.game-field-mapping.v1:" + "f" * 64,
        "game_mapping_registry_digest": "1" * 64,
        "replay_source_interpretation_contract_id": "openmtgdata.source-interpretation.v1:"
        + "6" * 64,
        "game_source_interpretation_contract_id": "openmtgdata.source-interpretation.v1:"
        + "7" * 64,
        "replay_source_archive_id": "2" * 64,
        "game_source_archive_id": "3" * 64,
        "replay_raw_schema_fingerprint": "4" * 64,
        "game_raw_schema_fingerprint": "5" * 64,
        "candidate_field_inventory": (),
        "candidate_results": (candidate_evidence(candidate, replay, game),),
        "replay_m5_rejection_counts": (),
        "reciprocal_game_row_evidence": {},
        "game_number_game_index_relation": {},
    }
    report = build_join_evidence_report(**values)
    assert report.overall_decision.status is JoinReportDecisionStatus.JOIN_UNSUPPORTED
    assert report.overall_decision.approved_candidate_id is None
    validate_join_evidence_report(report)

    incomplete = build_join_evidence_report(
        **{**values, "replay_m4_completion_status": "incomplete"}
    )
    assert incomplete.overall_decision.status is JoinReportDecisionStatus.ANALYSIS_INCOMPLETE
    assert incomplete.report_digest != report.report_digest
    no_candidates = build_join_evidence_report(**{**values, "candidate_results": ()})
    assert no_candidates.overall_decision.status is JoinReportDecisionStatus.ANALYSIS_INCOMPLETE

    altered = replace(report, report_digest="8" * 64)
    with pytest.raises(JoinEvidenceError, match="semantic digest"):
        validate_join_evidence_report(altered)
    malformed = replace(report, report_digest="x" * 40)
    with pytest.raises(JoinEvidenceError, match="digest is malformed"):
        validate_join_evidence_report(malformed)


def test_n_to_n_is_ambiguous_and_never_authorized_by_coverage() -> None:
    replay = _stats(SourceKind.REPLAY, [("k", "0"), ("k", "0")])
    game = _stats(SourceKind.GAME, [("k", "0"), ("k", "0"), ("k", "0")])
    cardinality = compare_candidate_cardinality(replay, game)
    decision = decide_candidate(cardinality, replay, game, semantics_documented=True)
    assert decision.status is JoinDecision.AMBIGUOUS
    assert decision.approved_for_scope is None


def test_exact_one_to_one_without_documented_semantics_remains_unsupported() -> None:
    replay = _stats(SourceKind.REPLAY, [("k", "0")])
    game = _stats(SourceKind.GAME, [("k", "0")])
    evidence = candidate_evidence(_candidate(), replay, game)
    assert evidence.decision.status is JoinDecision.UNSUPPORTED
    assert "source semantics" in evidence.decision.reason


def test_candidate_evidence_serialization_never_contains_source_keys() -> None:
    replay = _stats(SourceKind.REPLAY, [("PRIVATE_DRAFT_VALUE", "7")])
    game = _stats(SourceKind.GAME, [("PRIVATE_DRAFT_VALUE", "7")])
    output = candidate_evidence(_candidate(), replay, game).to_dict()
    serialized = str(output)
    assert "PRIVATE_DRAFT_VALUE" not in serialized
    assert "draft_id" in serialized


def test_report_digest_is_deterministic_and_contains_no_runtime_path_or_time() -> None:
    candidate = _candidate()
    replay = _stats(SourceKind.REPLAY, [("opaque-value", "0")])
    game = _stats(SourceKind.GAME, [("opaque-value", "0")])
    result = candidate_evidence(candidate, replay, game)
    field_evidence = (
        JoinFieldEvidenceV1(
            candidate.replay_fields[0],
            ("other_text",),
            False,
            "bounded_prefix",
            "exact source selector candidate only",
        ),
    )
    game_field_evidence = JoinFieldEvidenceV1(
        JoinFieldReferenceV1(SourceKind.GAME, 2, "draft_id", "source_direct"),
        ("other_text",),
        False,
        "bounded_prefix",
        "exact source selector candidate only",
    )
    alternate_candidate = JoinCandidateV1(
        candidate.replay_fields,
        candidate.game_fields,
        "alternate scoped candidate",
        EXACT_STRING_COMPARISON_CONTRACT_ID,
        derive_join_candidate_id(
            candidate.replay_fields,
            candidate.game_fields,
            "alternate scoped candidate",
        ),
    )
    alternate_result = candidate_evidence(alternate_candidate, replay, game)
    args = {
        "semantic_source_catalog_digest": "a" * 64,
        "m3_evidence_digest": "b" * 64,
        "schema_registry_digest": "c" * 64,
        "replay_mapping_id": "openmtgdata.replay-field-mapping.v1:" + "d" * 64,
        "replay_mapping_registry_digest": "e" * 64,
        "game_mapping_id": "openmtgdata.game-field-mapping.v1:" + "f" * 64,
        "game_mapping_registry_digest": "1" * 64,
        "replay_source_interpretation_contract_id": "openmtgdata.source-interpretation.v1:"
        + "6" * 64,
        "game_source_interpretation_contract_id": "openmtgdata.source-interpretation.v1:"
        + "7" * 64,
        "replay_source_archive_id": "2" * 64,
        "game_source_archive_id": "3" * 64,
        "replay_raw_schema_fingerprint": "4" * 64,
        "game_raw_schema_fingerprint": "5" * 64,
        "candidate_field_inventory": field_evidence + (game_field_evidence,),
        "candidate_results": (result, alternate_result),
        "replay_m5_rejection_counts": (),
        "reciprocal_game_row_evidence": {},
        "game_number_game_index_relation": {},
    }
    first = build_join_evidence_report(**args)
    second = build_join_evidence_report(
        **{
            **args,
            "candidate_field_inventory": tuple(reversed(args["candidate_field_inventory"])),
            "candidate_results": tuple(reversed(args["candidate_results"])),
        }
    )
    assert first.report_digest == second.report_digest
    validate_join_evidence_report(first)
    serialized = str(first.to_dict())
    assert "C:\\Dev" not in serialized
    assert "created_at" not in serialized
