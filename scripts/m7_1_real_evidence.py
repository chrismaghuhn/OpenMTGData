"""Rebuild the ignored M7.1 AFR Replay evidence artifacts through M4-v2.

The caller supplies the runtime raw root. The script reads only the selected
Replay archive and consumes the accepted local M3/M5 evidence artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from openmtgdata.archive_registration import register_archive
from openmtgdata.config import RuntimeConfig
from openmtgdata.decision_evidence import (
    ActionStatus,
    BeforeStateStatus,
    DecisionEvidenceReportV1,
    DecisionTypeDisposition,
    DecisionTypeEvidenceV1,
    LeakageStatus,
    OverallDecisionStatus,
    PerspectiveEvidenceV1,
    PerspectiveStatus,
    ReconstructionEvidenceLevel,
    TargetStatus,
    VisibilityStatus,
    build_field_inventory,
    build_turn_family_summaries,
    collect_bounded_replay_evidence,
    decision_report_with_digest,
)
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.replay_schema import build_turn_slot_mapping
from openmtgdata.schema_evolution import load_verified_m3_evidence
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import load_verified_schema_registry

SOURCE_CATALOG_DIGEST = "dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602"
M3_EVIDENCE_DIGEST = "c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29"
M3_REGISTRY_DIGEST = "66d58e20fa2adbcbfdcfd927c1074b5807d43af8af94103b61b26fc06f13661c"
M5_REGISTRY_DIGEST = "43be7080894b55c60f58d569ed8cfeb52a9dbae1fa8688160d91196044ab47f3"
REPLAY_ARCHIVE_ID = "b739dacc3082d356b27001aa6fd91257ae766a8d1aec23272e656610cfe3b54b"
REPLAY_FINGERPRINT = "b4ad0cb27197cf3e37e59d6609a6098da0e0e2f6c4f98b82c45f85fcb7bbea75"
REPLAY_MAPPING_ID = (
    "openmtgdata.replay-field-mapping.v1:"
    "a42f925293705fb89936e3b3b5a6d243027d63953808ccfb154920c0a6031140"
)
M6_REPORT_DIGEST = "24a62c45f0b95de75069e757995a7d9f7e2c4844b0882eeb1550fd1d51827ff5"
TURN_FIELD_RE = re.compile(r"(user|oppo)_turn_([1-9][0-9]*)_(.+)\Z")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _candidate_types(families) -> tuple[DecisionTypeEvidenceV1, ...]:
    return tuple(
        DecisionTypeEvidenceV1(
            f"turn_activity_candidate:{family.exact_suffix}",
            (f"user_turn_N_{family.exact_suffix}", f"oppo_turn_N_{family.exact_suffix}"),
            DecisionTypeDisposition.UNSUPPORTED_ACTION_STRUCTURE,
            "one source string per source-side turn slot; sampled lexemes include integer-like "
            "and pipe-containing forms; count/list/object grammar is unestablished",
            ReconstructionEvidenceLevel.UNSUPPORTED,
            ActionStatus.ACTION_CANDIDATE,
            "not established; delimiter characters do not prove escaping, order, or cardinality",
            PerspectiveStatus.ACTOR_UNKNOWN,
            "turn-side slot label does not establish individual decision actor",
            VisibilityStatus.UNKNOWN_VISIBILITY,
            BeforeStateStatus.TURN_AGGREGATE_ONLY,
            TargetStatus.ACTION_TARGET_UNSUPPORTED,
            LeakageStatus.TIMING_UNKNOWN,
            (),
            "M4 bounded prefix plus M5.1 full-stream turn-summary reconciliation; no full-stream "
            "decision-level invariants",
            (
                "The suffix is only a candidate activity label. No atomic action, exact decision "
                "boundary, actor, before-state, visibility, target, or intra-turn order is "
                "established."
            ),
        )
        for family in families
        if family.action_status is ActionStatus.ACTION_CANDIDATE
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--max-data-rows", type=int, default=256)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve(strict=True)
    intermediate_root = repo_root / "data" / "intermediate"
    config = RuntimeConfig(
        raw_roots=(args.raw_root,),
        intermediate_root=intermediate_root,
        quarantine_root=repo_root / "data" / "quarantine",
        release_root=repo_root / "data" / "release",
        base_dir=repo_root,
    )
    deep_path = intermediate_root / "m5.2" / "deep-inspection-container-v2-methods.json"
    schema_path = intermediate_root / "m5.2" / "schema-registry-container-v2-methods.json"
    m5_path = intermediate_root / "m5.2" / "replay-mapping-registry-container-v2.json"
    evidence = load_verified_m3_evidence(
        deep_path,
        expected_evidence_digest=M3_EVIDENCE_DIGEST,
        expected_source_catalog_digest=SOURCE_CATALOG_DIGEST,
    )
    registry = load_verified_schema_registry(
        schema_path,
        expected_registry_digest=M3_REGISTRY_DIGEST,
        expected_source_catalog_digest=SOURCE_CATALOG_DIGEST,
        expected_m3_evidence_digest=M3_EVIDENCE_DIGEST,
    )
    m5_document = json.loads(m5_path.read_text(encoding="utf-8"))
    m5_projection = m5_document["mapping_registry"]
    actual_m5_digest = hashlib.sha256(
        json.dumps(m5_projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    if (
        actual_m5_digest != M5_REGISTRY_DIGEST
        or m5_document["mapping_registry_digest"] != M5_REGISTRY_DIGEST
    ):
        raise ValueError("accepted M5.1 mapping registry digest mismatch")
    group = next(
        item for item in evidence.groups if item.raw_schema_fingerprint == REPLAY_FINGERPRINT
    )
    source_group = registry.lookup(SourceKind.REPLAY, REPLAY_FINGERPRINT)
    if source_group is None:
        raise ValueError("AFR Replay fingerprint is not supported by the accepted M3.2 registry")
    review = next(
        item["review_evidence"]
        for item in m5_projection["group_dispositions"]
        if item["raw_schema_fingerprint"] == REPLAY_FINGERPRINT
    )
    inventory = inventory_raw_roots(config)
    item = next(
        candidate
        for candidate in inventory.candidates
        if candidate.basename == "replay_data_public.AFR.PremierDraft.csv.gz"
    )
    source = register_archive(item)
    if source.source_archive_id != REPLAY_ARCHIVE_ID:
        raise ValueError("selected Replay bytes differ from the accepted source archive")
    mapping = build_turn_slot_mapping(
        group,
        source_interpretation_contract_id=source_group.source_interpretation_contract_id,
        m3_evidence_digest=M3_EVIDENCE_DIGEST,
        source_archive_id_reviewed=REPLAY_ARCHIVE_ID,
    )
    if mapping.replay_field_mapping_id != REPLAY_MAPPING_ID:
        raise ValueError("reconstructed Replay mapping differs from accepted M5.1 authority")
    bounded = collect_bounded_replay_evidence(
        source, registry, group, mapping, max_data_rows=args.max_data_rows
    )
    fields = build_field_inventory(group, mapping)
    families = build_turn_family_summaries(
        group,
        bounded_counts=bounded.family_cell_counts,
        bounded_rows_seen=bounded.m4_rows_seen,
        bounded_rows_accepted=bounded.m4_rows_accepted,
    )
    candidates = _candidate_types(families)
    field_counts = {
        "physical": len(fields),
        "row_level": sum(item.structural_family == "row_level" for item in fields),
        "indexed_turn_slot": sum(item.structural_family == "indexed_turn_slot" for item in fields),
        "m5_mapped": sum(item.m5_mapping_status == "mapped" for item in fields),
        "m5_raw_only": sum(item.m5_mapping_status == "raw_only" for item in fields),
        "private_or_hand_like_name_candidates": sum(
            item.source_side_private_candidate for item in fields
        ),
        "end_of_turn_prefix_name_candidates": sum(
            item.end_of_turn_name_candidate_only for item in fields
        ),
        "header_names_containing_target": sum(
            "target" in item.selector.exact_header_name.lower() for item in fields
        ),
    }
    visibility_counts = Counter(item.visibility_status.value for item in fields)
    timing_counts = Counter(item.timing_status.value for item in fields)
    policy_counts = Counter(item.policy_input_status.value for item in fields)
    findings: dict[str, object] = {
        "action_target_review": {
            "status": "action_target_unsupported",
            "target_named_header_count": field_counts["header_names_containing_target"],
            "reason": (
                "No target-named physical field is present; object-like values do not establish "
                "target semantics."
            ),
        },
        "bounded_m4_prefix": {
            "max_logical_rows": args.max_data_rows,
            "rows_seen": bounded.m4_rows_seen,
            "rows_accepted": bounded.m4_rows_accepted,
            "rows_width_rejected": bounded.m4_rows_rejected,
            "m5_rows_submitted": bounded.m5_rows_submitted,
            "m5_rows_normalized": bounded.m5_rows_normalized,
            "m5_rows_rejected": bounded.m5_rows_rejected,
            "reader_completion_status": bounded.reader_completion_status,
            "scope": "bounded prefix; no terminal source verification in this M7.1 pass",
        },
        "full_stream_m4_m5_review": {
            "m4_completion_status": review["m4_reader_completion_status"],
            "m4_rows_seen": review["m4_records_seen"],
            "m4_rows_accepted": review["m4_records_accepted"],
            "m4_width_rejected": review["m4_width_rejected_records"],
            "m5_rows_normalized": review["m5_records_normalized"],
            "m5_rows_rejected": review["m5_records_rejected"],
            "m5_turn_summary_slots": review["event_count"],
            "m5_turn_side_counts": review["event_side_counts"],
            "m5_semantic_validation_digest": (
                "24db9aba56d48cbf1403a71f128fa870e065df850b9d93db3efdeeac67dbfe08"
            ),
            "scope": (
                "accepted M5.1 full-stream review validates turn-slot reconciliation only, "
                "not action/actor/decision semantics"
            ),
        },
        "field_counts": field_counts,
        "future_slot_policy": (
            "Convert the exact (source_side, per-side slot_index) to M5.1's zero-based "
            "event_ordinal: 2*(slot_index-1) + 0 when source_side is the on_play-selected "
            "first side, otherwise +1. Compare with the candidate event_ordinal; larger is "
            "FUTURE_TURN and rejected. Equal ordinals still require established PRE_TURN or "
            "TURN_START timing and safe visibility."
        ),
        "m6_boundary": {
            "status": "join_unsupported",
            "report_digest": M6_REPORT_DIGEST,
            "game_data_read": False,
            "m6_1_to_1_candidates_used": 0,
        },
        "m5_rejection_distinction": {
            "full_stream_m5_rejected": review["m5_records_rejected"],
            "population_definition": (
                "M5-normalized semantics use M5-normalized rows; raw field safety uses "
                "M4-accepted rows"
            ),
        },
        "outcome_and_timing": {
            "outcome_like_field_candidates": ["won"],
            "won_semantics_established_by_this_replay_only": False,
            "won_policy_default": "excluded as outcome-like; source meaning remains unconfirmed",
            "whole_record_turn_count": (
                "turns is a full-row GAME_LEVEL summary and POST_DECISION_ONLY"
            ),
            "same_turn": (
                "No intra-slot order establishes that any source field precedes a candidate action."
            ),
            "future_turn": "Later indexed slots are forbidden as inputs for an earlier slot.",
        },
        "perspective": {
            "source_turn_side_order": (
                "M5.1 supports source-side slot ordering from on_play and alternation only"
            ),
            "turn_owner": "not independently established",
            "decision_actor": "unknown",
            "actor_may_differ_from_turn_side": (
                "not represented; equality is unsafe to assume in an interactive game"
            ),
            "participant_alignment": "not established; no M6 join or Game evidence used",
            "self_opponent_transform": (
                "not supportable until decision actor evidence exists; no fixed user=SELF mapping"
            ),
        },
        "private_information": {
            "hand_like_fields_on_both_source_sides": True,
            "actor_visibility": "unknown",
            "source_omniscient_visibility": "not established",
            "policy_default": (
                "exclude hand-like and other-side fields until actor/time visibility is established"
            ),
        },
        "field_status_counts": {
            "timing": dict(sorted(timing_counts.items())),
            "visibility": dict(sorted(visibility_counts.items())),
            "policy_input": dict(sorted(policy_counts.items())),
        },
        "value_structure": (
            "M4 bounded prefix recorded pipe delimiter characters in several turn-family "
            "values; no comma, semicolon, or newline in sampled cells. No delimiter parsing, "
            "escaping, ordering, or cardinality grammar is established."
        ),
        "before_state": {
            "supported": "none",
            "available_level": "turn_aggregate_only",
            "previous_turn_end_equals_current_before_state": "not established",
        },
        "source_empty_semantics": (
            "Empty source fields remain observed empty strings; semantic null is not inferred."
        ),
        "raw_values_retained": False,
        "source_contains_both_user_oppo_families": True,
    }
    perspective = PerspectiveEvidenceV1(
        (
            "M5.1 source turn-side slot order is supported from on_play plus alternation; "
            "not a rules-owner assertion"
        ),
        "not independently established",
        PerspectiveStatus.ACTOR_UNKNOWN,
        "not established; no per-decision actor marker exists in this summary format",
        "not supportable: no decision actor evidence",
        False,
        False,
        (
            "Future actor-relative views must map a verified actor to SELF and the other side "
            "to OPPONENT; consistent source-side/actor swaps must preserve these roles."
        ),
        "future requirement only; no real or synthetic sample augmentation",
        (
            "No field marks each individual decision actor.",
            "No exact intra-turn action order is represented.",
            "Visibility of user/oppo private-looking fields is unknown.",
        ),
    )
    report = decision_report_with_digest(
        DecisionEvidenceReportV1(
            SOURCE_CATALOG_DIGEST,
            M3_EVIDENCE_DIGEST,
            M3_REGISTRY_DIGEST,
            REPLAY_MAPPING_ID,
            M5_REGISTRY_DIGEST,
            REPLAY_ARCHIVE_ID,
            source.compressed_sha256,
            source.compressed_size_bytes,
            REPLAY_FINGERPRINT,
            mapping.source_interpretation_contract_id,
            "openmtgdata.raw-source-reader.v2",
            "openmtgdata.replay-event.v1",
            (
                "AFR PremierDraft: exact header + M3.1 bounded evidence + M4-v2 bounded prefix "
                "+ accepted M5.1 full-stream turn-slot review"
            ),
            {
                "m4_full_rows_seen": review["m4_records_seen"],
                "m4_full_rows_accepted": review["m4_records_accepted"],
                "m4_full_width_rejected": review["m4_width_rejected_records"],
                "m5_full_rows_normalized": review["m5_records_normalized"],
                "m5_full_rows_rejected": review["m5_records_rejected"],
                "m5_full_turn_summary_slots": review["event_count"],
                "m4_bounded_rows_seen": bounded.m4_rows_seen,
                "m4_bounded_rows_accepted": bounded.m4_rows_accepted,
                "m4_bounded_rows_width_rejected": bounded.m4_rows_rejected,
                "m5_bounded_rows_submitted": bounded.m5_rows_submitted,
                "m5_bounded_rows_normalized": bounded.m5_rows_normalized,
                "m5_bounded_rows_rejected": bounded.m5_rows_rejected,
            },
            fields,
            families,
            candidates,
            perspective,
            bounded.examples,
            findings,
            OverallDecisionStatus.TURN_SUMMARY_ONLY,
            True,
            False,
            False,
            (
                "M7.1 scope is only the corrected AFR PremierDraft Replay archive.",
                "M3 lexical profiles and this M4 sample are bounded prefix evidence.",
                (
                    "M5.1 full-stream evidence validates turn-summary counts/occupancy, not "
                    "decision semantics."
                ),
                (
                    "24,668 M4 width-rejected rows are absent from ReplayEventV1; possible "
                    "semantic bias is unresolved."
                ),
                (
                    "133 M5.1 rejected rows are absent from turn-slot normalization; source-field "
                    "safety is based on M4-accepted rows."
                ),
                "No Game data, M6.1 one-to-one candidate values, or M6.2 join was used.",
                "No safe Behavior-Cloning sample is approved; a training smoke is not justified.",
            ),
            "",
        )
    )
    output = intermediate_root / "m7.1"
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "decision-evidence.json", report.to_dict())
    _write_json(
        output / "decision-field-safety.json",
        {
            "contract_id": "openmtgdata.decision-field-safety.v1",
            "source_catalog_digest": SOURCE_CATALOG_DIGEST,
            "m3_evidence_digest": M3_EVIDENCE_DIGEST,
            "schema_registry_digest": M3_REGISTRY_DIGEST,
            "m5_mapping_id": REPLAY_MAPPING_ID,
            "m5_mapping_registry_digest": M5_REGISTRY_DIGEST,
            "source_archive_id": REPLAY_ARCHIVE_ID,
            "compressed_sha256": source.compressed_sha256,
            "compressed_size_bytes": source.compressed_size_bytes,
            "raw_schema_fingerprint": REPLAY_FINGERPRINT,
            "source_interpretation_contract_id": mapping.source_interpretation_contract_id,
            "m4_reader_contract_id": "openmtgdata.raw-source-reader.v2",
            "report_digest": report.report_digest,
            "fields": [item.to_dict() for item in fields],
        },
    )
    _write_json(
        output / "annotated-examples.json",
        {
            "contract_id": "openmtgdata.decision-annotated-example.v1",
            "source_catalog_digest": SOURCE_CATALOG_DIGEST,
            "m3_evidence_digest": M3_EVIDENCE_DIGEST,
            "schema_registry_digest": M3_REGISTRY_DIGEST,
            "m5_mapping_id": REPLAY_MAPPING_ID,
            "m5_mapping_registry_digest": M5_REGISTRY_DIGEST,
            "source_archive_id": REPLAY_ARCHIVE_ID,
            "compressed_sha256": source.compressed_sha256,
            "compressed_size_bytes": source.compressed_size_bytes,
            "raw_schema_fingerprint": REPLAY_FINGERPRINT,
            "source_interpretation_contract_id": mapping.source_interpretation_contract_id,
            "m4_reader_contract_id": "openmtgdata.raw-source-reader.v2",
            "m5_event_contract_id": "openmtgdata.replay-event.v1",
            "report_digest": report.report_digest,
            "examples": [item.to_dict() for item in bounded.examples],
            "raw_values_retained": False,
        },
    )
    print(
        json.dumps(
            {
                "report_digest": report.report_digest,
                "overall_status": report.overall_status.value,
                "physical_fields": field_counts["physical"],
                "row_level_fields": field_counts["row_level"],
                "turn_slot_fields": field_counts["indexed_turn_slot"],
                "turn_suffix_families": len(families),
                "candidate_decision_types": len(candidates),
                "bounded_m4_rows_seen": bounded.m4_rows_seen,
                "bounded_m4_rows_accepted": bounded.m4_rows_accepted,
                "bounded_m4_rows_rejected": bounded.m4_rows_rejected,
                "safe_bc_sample": report.can_build_safe_behavior_cloning_sample,
                "artifacts": [
                    "data/intermediate/m7.1/decision-evidence.json",
                    "data/intermediate/m7.1/decision-field-safety.json",
                    "data/intermediate/m7.1/annotated-examples.json",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
