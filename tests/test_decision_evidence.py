from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from openmtgdata.decision_evidence import (
    ActionStatus,
    BeforeStateStatus,
    DecisionEvidenceError,
    DecisionEvidenceReportV1,
    DecisionFieldReferenceV1,
    DecisionTypeDisposition,
    DecisionTypeEvidenceV1,
    LeakageStatus,
    OverallDecisionStatus,
    PerspectiveEvidenceV1,
    PerspectiveStatus,
    PolicyInputStatus,
    ReconstructionEvidenceLevel,
    TargetStatus,
    TimingStatus,
    VisibilityStatus,
    classify_field,
    classify_value_shape,
    decision_report_digest,
    decision_report_with_digest,
    derive_overall_status,
    source_turn_event_ordinal,
    validate_decision_report,
    validate_perspective_swap_requirement,
    validate_policy_field_for_decision,
)


def _field(name: str = "user_turn_1_cards_played", index: int = 10):
    return classify_field(
        index,
        name,
        lexical_class_counts=(("empty", 2), ("other_text", 3)),
        m5_mapped=False,
        m3_evidence_scope="bounded_prefix",
    )


def _report() -> DecisionEvidenceReportV1:
    source_id = "a" * 64
    field = _field()
    candidate = DecisionTypeEvidenceV1(
        "turn_slot_play_like",
        ("user_turn_N_lands_played", "oppo_turn_N_lands_played"),
        DecisionTypeDisposition.UNSUPPORTED_ACTION_STRUCTURE,
        "turn-summary source strings; delimiter/order grammar unestablished",
        ReconstructionEvidenceLevel.UNSUPPORTED,
        ActionStatus.ACTION_CANDIDATE,
        "unestablished",
        PerspectiveStatus.ACTOR_UNKNOWN,
        "turn side is not decision actor",
        VisibilityStatus.UNKNOWN_VISIBILITY,
        BeforeStateStatus.TURN_AGGREGATE_ONLY,
        TargetStatus.ACTION_TARGET_UNSUPPORTED,
        LeakageStatus.TIMING_UNKNOWN,
        (),
        "M4 bounded prefix + M5 full-stream turn-slot reconciliation",
        "No within-turn decision boundary, actor, or before-state is supported.",
    )
    return DecisionEvidenceReportV1(
        "1" * 64,
        "2" * 64,
        "3" * 64,
        "openmtgdata.replay-field-mapping.v1:" + "4" * 64,
        "5" * 64,
        source_id,
        "6" * 64,
        1234,
        "7" * 64,
        "openmtgdata.source-interpretation.v1:" + "8" * 64,
        "openmtgdata.raw-source-reader.v2",
        "openmtgdata.replay-event.v1",
        "AFR PremierDraft only; field evidence bounded; M5 turn reconciliation full-stream",
        {"m4_seen": 256, "m4_accepted": 230, "m4_rejected": 26},
        (field,),
        (),
        (candidate,),
        PerspectiveEvidenceV1(
            "source turn-slot order established by M5.1; no actor evidence",
            "not independently established",
            PerspectiveStatus.ACTOR_UNKNOWN,
            "not established; Magic interaction can place decisions on either player's turn",
            "not currently supportable",
            False,
            False,
            (
                "known actor + source-side identity must canonicalize actor to SELF and other "
                "to OPPONENT"
            ),
            "requirement only; no augmentation emitted",
            ("decision actor source evidence", "field visibility by actor and time"),
        ),
        (),
        {"game_reads": 0, "m6_join_reads": 0, "raw_values_retained": False},
        OverallDecisionStatus.TURN_SUMMARY_ONLY,
        True,
        False,
        False,
        ("M6.1 join_unsupported; no Game evidence used",),
        "",
    )


def test_field_reference_preserves_index_and_empty_header_name() -> None:
    first = DecisionFieldReferenceV1(0, "duplicate")
    second = DecisionFieldReferenceV1(9, "duplicate")
    blank = DecisionFieldReferenceV1(2, "")
    assert first.to_dict()["column_index"] != second.to_dict()["column_index"]
    assert blank.to_dict()["exact_header_name"] == ""
    with pytest.raises(DecisionEvidenceError):
        DecisionFieldReferenceV1(-1, "x")


def test_unknown_and_raw_only_fields_fail_closed() -> None:
    field = _field("mystery", 3)
    assert field.policy_input_status is PolicyInputStatus.TIMING_AMBIGUOUS
    assert field.timing_status is TimingStatus.UNKNOWN_TIMING
    assert field.visibility_status is VisibilityStatus.UNKNOWN_VISIBILITY
    assert field.m5_mapping_status == "raw_only"
    assert field.action_status is ActionStatus.NOT_ACTION


def test_action_like_name_is_only_a_candidate_not_an_action_or_decision() -> None:
    field = _field("user_turn_4_cards_played", 20)
    assert field.action_status is ActionStatus.ACTION_CANDIDATE
    assert field.policy_input_status is PolicyInputStatus.TIMING_AMBIGUOUS
    assert field.perspective_status is PerspectiveStatus.ACTOR_UNKNOWN
    assert field.before_state_status is BeforeStateStatus.TURN_AGGREGATE_ONLY
    assert field.action_target_status is TargetStatus.ACTION_TARGET_UNSUPPORTED
    assert field.leakage_status is LeakageStatus.TIMING_UNKNOWN


def test_user_oppo_prefix_does_not_establish_visibility_or_actor() -> None:
    user_hand = _field("user_turn_2_eot_user_cards_in_hand", 11)
    oppo_hand = _field("oppo_turn_2_eot_oppo_cards_in_hand", 12)
    assert user_hand.visibility_status is VisibilityStatus.UNKNOWN_VISIBILITY
    assert oppo_hand.visibility_status is VisibilityStatus.UNKNOWN_VISIBILITY
    assert user_hand.source_side_private_candidate
    assert oppo_hand.source_side_private_candidate
    assert user_hand.end_of_turn_name_candidate_only
    assert user_hand.perspective_status is PerspectiveStatus.ACTOR_UNKNOWN
    assert user_hand.policy_input_status is not PolicyInputStatus.SAFE_IF_ACTOR_USER
    assert oppo_hand.policy_input_status is not PolicyInputStatus.SAFE_IF_ACTOR_OPPO


def test_outcome_like_field_is_never_a_policy_input() -> None:
    won = _field("won", 14)
    assert won.leakage_status is LeakageStatus.GAME_OUTCOME
    assert won.policy_input_status is PolicyInputStatus.POST_DECISION_ONLY
    with pytest.raises(DecisionEvidenceError):
        validate_policy_field_for_decision(won, candidate_event_ordinal=0)


def test_future_turn_fields_cannot_become_predecision_inputs() -> None:
    future = _field("oppo_turn_3_lands_played", 70)
    assert future.turn_slot_index == 3
    with pytest.raises(DecisionEvidenceError, match="later chronological"):
        validate_policy_field_for_decision(future, candidate_event_ordinal=2, source_on_play=True)
    with pytest.raises(DecisionEvidenceError, match="timing"):
        validate_policy_field_for_decision(future, candidate_event_ordinal=5, source_on_play=True)


def test_same_turn_and_end_of_turn_name_candidates_are_not_predecision_safe() -> None:
    aggregate = _field("user_turn_2_eot_user_life", 15)
    assert aggregate.timing_status is TimingStatus.WITHIN_TURN_UNKNOWN
    assert aggregate.end_of_turn_name_candidate_only
    with pytest.raises(DecisionEvidenceError):
        validate_policy_field_for_decision(
            aggregate, candidate_event_ordinal=2, source_on_play=True
        )


def test_cross_side_future_order_uses_m5_event_ordinal_not_side_slot_index() -> None:
    user_turn_two = _field("user_turn_2_lands_played", 20)
    oppo_turn_two = _field("oppo_turn_2_lands_played", 80)

    # on_play=1: user turn 2 is event 2, oppo turn 2 is event 3.
    assert source_turn_event_ordinal(user_turn_two, source_on_play=True) == 2
    assert source_turn_event_ordinal(oppo_turn_two, source_on_play=True) == 3
    with pytest.raises(DecisionEvidenceError, match="later chronological"):
        validate_policy_field_for_decision(
            oppo_turn_two, candidate_event_ordinal=2, source_on_play=True
        )
    # The equal chronological position is not rejected as future, but timing still fails closed.
    with pytest.raises(DecisionEvidenceError, match="timing"):
        validate_policy_field_for_decision(
            user_turn_two, candidate_event_ordinal=2, source_on_play=True
        )

    # on_play=0 reverses which source side occupies the earlier slot in each pair.
    assert source_turn_event_ordinal(oppo_turn_two, source_on_play=False) == 2
    assert source_turn_event_ordinal(user_turn_two, source_on_play=False) == 3
    with pytest.raises(DecisionEvidenceError, match="later chronological"):
        validate_policy_field_for_decision(
            user_turn_two, candidate_event_ordinal=2, source_on_play=False
        )


def test_source_private_input_requires_matching_actor_side_for_each_sample() -> None:
    source_user_hand = replace(
        _field("user_turn_1_eot_user_cards_in_hand", 40),
        timing_status=TimingStatus.PRE_TURN,
        leakage_status=LeakageStatus.PRE_DECISION_SAFE,
        visibility_status=VisibilityStatus.SOURCE_USER_PRIVATE,
        policy_input_status=PolicyInputStatus.SAFE_IF_ACTOR_USER,
        perspective_status=PerspectiveStatus.ACTOR_SOURCE_USER,
    )
    with pytest.raises(DecisionEvidenceError, match="source-user input"):
        validate_policy_field_for_decision(
            source_user_hand,
            candidate_event_ordinal=0,
            source_on_play=True,
            decision_actor_source_side="oppo",
        )
    validate_policy_field_for_decision(
        source_user_hand,
        candidate_event_ordinal=0,
        source_on_play=True,
        decision_actor_source_side="user",
    )


def test_unordered_or_delimited_source_strings_are_not_split_or_ordered() -> None:
    assert classify_value_shape("") == "empty_string"
    assert "delimiter_characters_observed=|" in classify_value_shape("x|y")
    assert "order_unestablished" in classify_value_shape("x|y")
    assert classify_value_shape("001") == "integer_lexeme"


@pytest.mark.parametrize(
    ("user_actor", "oppo_actor", "swapped_user", "swapped_oppo", "expected"),
    [
        ("actor", "other", "other", "actor", ("SELF", "OPPONENT")),
        ("other", "actor", "actor", "other", ("OPPONENT", "SELF")),
    ],
)
def test_perspective_swap_is_actor_relative_not_user_equals_self(
    user_actor: str,
    oppo_actor: str,
    swapped_user: str,
    swapped_oppo: str,
    expected: tuple[str, str],
) -> None:
    assert (
        validate_perspective_swap_requirement(
            source_user_actor=user_actor,
            source_oppo_actor=oppo_actor,
            swapped_source_user_actor=swapped_user,
            swapped_source_oppo_actor=swapped_oppo,
        )
        == expected
    )


def test_perspective_swap_requires_actor_evidence_and_consistent_swap() -> None:
    with pytest.raises(DecisionEvidenceError, match="actor relation"):
        validate_perspective_swap_requirement(
            source_user_actor="unknown",
            source_oppo_actor="other",
            swapped_source_user_actor="other",
            swapped_source_oppo_actor="unknown",
        )
    with pytest.raises(DecisionEvidenceError, match="exchange"):
        validate_perspective_swap_requirement(
            source_user_actor="actor",
            source_oppo_actor="other",
            swapped_source_user_actor="actor",
            swapped_source_oppo_actor="other",
        )


def test_report_digest_is_deterministic_and_input_order_invariant() -> None:
    report = decision_report_with_digest(_report())
    reversed_report = decision_report_with_digest(
        replace(report, field_inventory=tuple(reversed(report.field_inventory)))
    )
    assert report.report_digest == reversed_report.report_digest
    validate_decision_report(report)


def test_runtime_paths_and_timestamps_are_rejected_from_semantic_report() -> None:
    report = decision_report_with_digest(_report())
    assert "created_at" not in str(report.to_dict())
    assert "absolute_path" not in str(report.to_dict())
    with pytest.raises(DecisionEvidenceError, match="runtime/audit"):
        decision_report_with_digest(
            replace(report, findings={"created_at_utc": "audit-only"}, report_digest="")
        )
    with pytest.raises(DecisionEvidenceError, match="absolute local paths"):
        decision_report_with_digest(
            replace(report, findings={"note": "C:\\local\\raw"}, report_digest="")
        )


def test_overall_report_cannot_claim_bc_safe_without_approved_decision_type() -> None:
    report = decision_report_with_digest(_report())
    invalid = replace(
        report,
        can_build_safe_behavior_cloning_sample=True,
        overall_status=OverallDecisionStatus.DECISION_EXTRACTION_SUPPORTED,
        report_digest="0" * 64,
    )
    with pytest.raises(DecisionEvidenceError, match="without a supported"):
        validate_decision_report(invalid)


def test_bounded_evidence_cannot_approve_a_full_decision_type() -> None:
    with pytest.raises(DecisionEvidenceError, match="full-stream"):
        DecisionTypeEvidenceV1(
            "bounded_candidate",
            ("user_turn_N_cards_played",),
            DecisionTypeDisposition.SUPPORTED,
            "source string",
            ReconstructionEvidenceLevel.SOURCE_DIRECT,
            ActionStatus.DECISION_BOUNDARY_SUPPORTED,
            "unknown",
            PerspectiveStatus.ACTOR_UNKNOWN,
            "unknown",
            VisibilityStatus.UNKNOWN_VISIBILITY,
            BeforeStateStatus.NO_BEFORE_STATE,
            TargetStatus.ACTION_TARGET_UNSUPPORTED,
            LeakageStatus.TIMING_UNKNOWN,
            (DecisionFieldReferenceV1(10, "user_turn_1_cards_played"),),
            "bounded_prefix",
            "not enough evidence",
        )
    with pytest.raises(DecisionEvidenceError, match="actor/timing/state/visibility/action"):
        DecisionTypeEvidenceV1(
            "actor_unknown_candidate",
            ("user_turn_N_cards_played",),
            DecisionTypeDisposition.SUPPORTED,
            "one source value",
            ReconstructionEvidenceLevel.SOURCE_DIRECT,
            ActionStatus.DECISION_BOUNDARY_SUPPORTED,
            "intra-turn order claimed",
            PerspectiveStatus.ACTOR_UNKNOWN,
            "unknown",
            VisibilityStatus.PUBLIC,
            BeforeStateStatus.EXACT_BEFORE_STATE,
            TargetStatus.NOT_APPLICABLE,
            LeakageStatus.PRE_DECISION_SAFE,
            (DecisionFieldReferenceV1(10, "user_turn_1_cards_played"),),
            "m4-full-stream-verified",
            "actor must still be established",
        )
    assert (
        derive_overall_status((), turn_summary_observed=True)
        is OverallDecisionStatus.TURN_SUMMARY_ONLY
    )
    assert (
        derive_overall_status((), turn_summary_observed=False)
        is OverallDecisionStatus.ANALYSIS_INCOMPLETE
    )


def test_partial_requires_observed_action_boundary_and_supported_actor() -> None:
    with pytest.raises(DecisionEvidenceError, match="source-backed action boundary"):
        DecisionTypeEvidenceV1(
            "partial_unknown_actor",
            ("user_turn_N_cards_played",),
            DecisionTypeDisposition.PARTIAL,
            "turn-summary candidate string",
            ReconstructionEvidenceLevel.UNSUPPORTED,
            ActionStatus.ACTION_CANDIDATE,
            "order unknown",
            PerspectiveStatus.ACTOR_UNKNOWN,
            "turn side is not actor",
            VisibilityStatus.UNKNOWN_VISIBILITY,
            BeforeStateStatus.NO_BEFORE_STATE,
            TargetStatus.ACTION_TARGET_UNSUPPORTED,
            LeakageStatus.TIMING_UNKNOWN,
            (),
            "bounded_prefix",
            "Partial must still prove that an observed actor made an observed action.",
        )

    partial = DecisionTypeEvidenceV1(
        "partial_action_actor_known",
        ("oppo_turn_N_action_source_field",),
        DecisionTypeDisposition.PARTIAL,
        "source-direct action lexeme",
        ReconstructionEvidenceLevel.SOURCE_DIRECT,
        ActionStatus.DECISION_BOUNDARY_SUPPORTED,
        "action boundary supported; before-state incomplete",
        PerspectiveStatus.ACTOR_SOURCE_OPPO,
        "source-side actor evidence only for this partial type",
        VisibilityStatus.UNKNOWN_VISIBILITY,
        BeforeStateStatus.PARTIAL_BEFORE_STATE,
        TargetStatus.ACTION_TARGET_UNSUPPORTED,
        LeakageStatus.TIMING_UNKNOWN,
        (),
        "bounded_prefix",
        "An observed action/actor may be known while policy inputs remain unsafe.",
    )
    assert derive_overall_status((partial,), turn_summary_observed=True) is (
        OverallDecisionStatus.PARTIAL_DECISION_EXTRACTION_SUPPORTED
    )
    base = _report()
    partial_report = decision_report_with_digest(
        replace(
            base,
            decision_types=(partial,),
            overall_status=OverallDecisionStatus.PARTIAL_DECISION_EXTRACTION_SUPPORTED,
            observed_behavior_supported=True,
            can_build_safe_behavior_cloning_sample=False,
            report_digest="",
        )
    )
    validate_decision_report(partial_report)
    assert partial_report.observed_behavior_supported
    assert not partial_report.can_build_safe_behavior_cloning_sample


def test_malformed_and_changed_digests_fail_closed() -> None:
    report = decision_report_with_digest(_report())
    with pytest.raises(DecisionEvidenceError, match="malformed"):
        validate_decision_report(replace(report, report_digest="abc"))
    with pytest.raises(DecisionEvidenceError, match="mismatch"):
        validate_decision_report(replace(report, source_archive_id="9" * 64))


def test_report_does_not_retain_raw_values_or_operational_identity() -> None:
    report = decision_report_with_digest(_report()).to_dict()
    serialized = str(report)
    for forbidden in ("example_value", "raw_root", "created_at", "hostname", "absolute_path"):
        assert forbidden not in serialized
    assert report["evidence"]["findings"]["raw_values_retained"] is False


def test_local_afr_m7_evidence_accounts_for_every_physical_field_when_available() -> None:
    root = Path("data/intermediate/m7.1")
    report_path = root / "decision-evidence.json"
    matrix_path = root / "decision-field-safety.json"
    if not report_path.exists() or not matrix_path.exists():
        pytest.skip("ignored local M7.1 evidence artifacts are unavailable")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    examples = json.loads((root / "annotated-examples.json").read_text(encoding="utf-8"))
    evidence = report["evidence"]
    assert report["report_digest"] == decision_report_digest(evidence)
    assert len(matrix["fields"]) == 2022
    assert [field["selector"]["column_index"] for field in matrix["fields"]] == list(range(2022))
    assert sum(field["m5_mapping_status"] == "mapped" for field in matrix["fields"]) == 1982
    assert sum(field["m5_mapping_status"] == "raw_only" for field in matrix["fields"]) == 40
    assert len(evidence["turn_families"]) == 33
    assert all(family["slot_count_per_side"] == 30 for family in evidence["turn_families"])
    for family in evidence["turn_families"]:
        assert family["m4_bounded_cells_observed"] == 230 * 60
        assert (
            family["m4_bounded_empty_field_count"] + family["m4_bounded_non_empty_field_count"]
            == family["m4_bounded_cells_observed"]
        )
        assert (
            family["m4_bounded_empty_slot_count"] + family["m4_bounded_non_empty_slot_count"]
            == family["m4_bounded_cells_observed"]
        )
    assert evidence["overall_status"] == "turn_summary_only"
    assert evidence["observed_behavior_supported"] is False
    assert evidence["can_build_safe_behavior_cloning_sample"] is False
    assert all(item["disposition"] != "supported" for item in evidence["decision_types"])
    assert matrix["m5_mapping_id"] == evidence["m5_mapping_id"]
    assert examples["report_digest"] == report["report_digest"]
    assert all(
        example["raw_schema_fingerprint"] == evidence["raw_schema_fingerprint"]
        and example["source_interpretation_contract_id"]
        == evidence["source_interpretation_contract_id"]
        and example["replay_field_mapping_id"] == evidence["m5_mapping_id"]
        and example["m4_reader_contract_id"] == evidence["m4_reader_contract_id"]
        and [slot["event_ordinal_within_source_record"] for slot in example["source_turn_slots"]]
        == list(range(example["sampled_event_slot_count"]))
        for example in examples["examples"]
    )
    combined = json.dumps([report, matrix, examples], ensure_ascii=False)
    assert "C:\\Dev\\src\\Laya-mtg" not in combined
    assert all(
        example["raw_values_retained"] is False for example in evidence["annotated_examples"]
    )
    assert '"value":' not in combined
