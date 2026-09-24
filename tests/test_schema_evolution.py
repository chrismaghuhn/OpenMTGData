from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from openmtgdata.schema_evolution import (
    EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID,
    FINDING_COUNT_SCOPE,
    LEXICAL_EVIDENCE_CONTRACT_ID,
    M3_REPORT_CONTRACT_ID,
    M3_REPORT_DIGEST_CONTRACT_ID,
    REFERENCE_SCHEMA_SELECTION_RULE,
    SCHEMA_COMPATIBILITY_ASSESSMENT_ID,
    SCHEMA_EVOLUTION_POLICY_ID,
    SCHEMA_REGISTRY_CONTRACT_ID,
    SCHEMA_REGISTRY_DIGEST_CONTRACT_ID,
    SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID,
    CompatibilityFinding,
    DeepReportValidationError,
    FieldSurfaceV1,
    RawSchemaGroupEvidenceV1,
    RegistryDisposition,
    ReviewStatus,
    RowWidthAnomalyV1,
    SchemaEvolutionError,
    SchemaEvolutionPolicyV1,
    VerifiedDeepEvidenceV1,
    assess_schema_pair,
    build_schema_registry,
    derive_source_interpretation_contract_id,
    load_verified_m3_evidence,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _field(
    index: int,
    name: str,
    classes: tuple[str, ...] = ("integer_lexeme",),
    *,
    empty: bool = False,
) -> FieldSurfaceV1:
    return FieldSurfaceV1(
        column_index=index,
        exact_header_name=name,
        observed_lexical_classes=classes,
        empty_field_observed=empty,
        lexical_class_counts=tuple(
            (name, int(name in classes))
            for name in (
                "empty",
                "integer_lexeme",
                "decimal_lexeme",
                "boolean_lexeme",
                "other_text",
            )
        ),
    )


def _group(
    kind: str,
    names: tuple[str, ...],
    *,
    classes: dict[str, tuple[str, ...]] | None = None,
    empty: set[str] | None = None,
    width_anomalies: tuple[RowWidthAnomalyV1, ...] = (),
    fingerprint_seed: str | None = None,
) -> RawSchemaGroupEvidenceV1:
    fingerprint = _digest(fingerprint_seed or f"{kind}:{names}")
    archive_id = _digest(f"archive:{kind}:{names}:{fingerprint_seed}")
    fields = tuple(
        _field(
            index,
            name,
            (classes or {}).get(name, ("integer_lexeme",)),
            empty=name in (empty or set()),
        )
        for index, name in enumerate(names)
    )
    lexical_digest = _digest(
        json.dumps([field.to_dict() for field in fields], sort_keys=True, separators=(",", ":"))
    )
    return RawSchemaGroupEvidenceV1(
        group_id=_digest(f"group:{kind}:{fingerprint}"),
        source_kind=kind,
        raw_schema_fingerprint=fingerprint,
        archive_count=1,
        source_archive_ids=(archive_id,),
        deep_inspection_ids=(_digest(f"deep:{archive_id}"),),
        representative_source_archive_id=archive_id,
        representative_filename=f"{kind}-{fingerprint[:6]}.csv.gz",
        expansion_format_counts=(("SET", "PremierDraft", 1),),
        fields=fields,
        lexical_profile_digest=lexical_digest,
        duplicate_header_names=tuple(sorted(name for name in set(names) if names.count(name) > 1)),
        empty_header_positions=tuple(index for index, name in enumerate(names) if name == ""),
        row_width_anomalies=width_anomalies,
    )


def _verified(groups: tuple[RawSchemaGroupEvidenceV1, ...]) -> VerifiedDeepEvidenceV1:
    return VerifiedDeepEvidenceV1(
        semantic_source_catalog_digest=_digest("source catalog"),
        deep_inspection_evidence_digest=_digest("deep report"),
        deep_inspection_report_contract_id=M3_REPORT_CONTRACT_ID,
        deep_inspection_method_id="openmtgdata.deep-source-inspection.v2",
        header_inventory_contract_id=EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID,
        header_inventory_evidence_digest=_digest("header inventory"),
        configured_source_count=sum(group.archive_count for group in groups),
        max_interpreted_data_rows=256,
        chronological_coverage_status="unavailable_not_established",
        source_url_status_counts=(("unknown", sum(group.archive_count for group in groups)),),
        license_status_counts=(("unknown", sum(group.archive_count for group in groups)),),
        groups=groups,
    )


def _synthetic_report_document(*, audit_timestamp: str | None = None) -> dict[str, object]:
    source_kind = "game"
    names = ("id", "value")
    fingerprint = _digest("physical schema game id value")
    group_id = _digest("group" + fingerprint)
    source_id = _digest("source archive")
    deep_id = _digest("deep inspection")
    class_counts = {
        "empty": 0,
        "integer_lexeme": 4,
        "decimal_lexeme": 0,
        "boolean_lexeme": 0,
        "other_text": 0,
    }
    inspection = {
        "columns": [
            {
                "column_index": index,
                "exact_header_name": name,
                "empty_field_count": 0,
                "lexical_class_counts": class_counts,
                "rows_observed": 4,
            }
            for index, name in enumerate(names)
        ],
        "deep_inspection_id": deep_id,
        "expected_field_count": 2,
        "maximum_observed_row_width": 2,
        "minimum_observed_row_width": 2,
        "original_filename": "game_data_public.SET.PremierDraft.csv.gz",
        "raw_schema_fingerprint": fingerprint,
        "rows_interpreted": 4,
        "rows_longer_than_header": 0,
        "rows_matching_header_width": 4,
        "rows_shorter_than_header": 0,
        "source_archive_id": source_id,
        "source_kind": source_kind,
        "status": "success",
    }
    representative = {
        "archive_count": 1,
        "counterpart": {"status": "unavailable"},
        "designated_representative": {
            "deep_inspection_id": deep_id,
            "original_filename": inspection["original_filename"],
            "source_archive_id": source_id,
        },
        "expansion_format_counts": [
            {"archive_count": 1, "expansion": "SET", "format": "PremierDraft"}
        ],
        "group_id": group_id,
        "raw_schema_fingerprint": fingerprint,
        "source_kind": source_kind,
    }
    evidence = {
        "deep_inspection_report_contract_id": M3_REPORT_CONTRACT_ID,
        "deep_report_digest_contract_id": M3_REPORT_DIGEST_CONTRACT_ID,
        "deep_inspection_method_id": "openmtgdata.deep-source-inspection.v2",
        "header_inventory_contract_id": EXPECTED_M2_HEADER_INVENTORY_CONTRACT_ID,
        "header_inventory_evidence_digest": _digest("header identity"),
        "semantic_source_catalog_digest": _digest("catalog identity"),
        "source_interpretation_contract_id": None,
        "raw_schema_group_count": 1,
        "covered_raw_schema_group_count": 1,
        "blocked_raw_schema_group_count": 0,
        "registered_archive_count": 1,
        "attempted_deep_inspections": 1,
        "successful_deep_inspections": 1,
        "partial_deep_inspections": 0,
        "unsupported_deep_inspections": 0,
        "header_inspected_archive_count": 1,
        "execution_blocker_count": 0,
        "designated_representative_count": 1,
        "representatives": [representative],
        "inspections": [inspection],
        "source_url_status_counts": {"unknown": 1},
        "license_status_counts": {"unknown": 1},
        "max_interpreted_data_rows": 256,
        "chronological_coverage_status": "unavailable_not_established",
    }
    canonical = json.dumps(evidence, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return {
        "audit": {"inspection_metadata": [{"timestamp": audit_timestamp}]},
        "evidence": evidence,
        "evidence_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def test_versioned_m3_2_contract_ids_are_distinct() -> None:
    assert SCHEMA_EVOLUTION_POLICY_ID == "openmtgdata.schema-evolution-policy.v1"
    assert SCHEMA_COMPATIBILITY_ASSESSMENT_ID == "openmtgdata.schema-compatibility-assessment.v1"
    assert (
        SOURCE_INTERPRETATION_CONTRACT_SCHEMA_ID == "openmtgdata.source-interpretation-contract.v1"
    )
    assert SCHEMA_REGISTRY_CONTRACT_ID == "openmtgdata.schema-registry.v1"
    assert SCHEMA_REGISTRY_DIGEST_CONTRACT_ID == "openmtgdata.schema-registry-digest.v1"
    assert LEXICAL_EVIDENCE_CONTRACT_ID == "openmtgdata.lexical-field-evidence.v1"
    policy = SchemaEvolutionPolicyV1().to_dict()
    assert policy["policy_id"] == SCHEMA_EVOLUTION_POLICY_ID
    assert policy["reference_schema_selection_rule"] == REFERENCE_SCHEMA_SELECTION_RULE
    assert "both A-to-B and B-to-A" in FINDING_COUNT_SCOPE


def test_exact_physical_headers_are_exact_without_claiming_semantics() -> None:
    group = _group("game", ("a", "b", "c"), fingerprint_seed="same")
    assessment = assess_schema_pair(group, group)

    assert CompatibilityFinding.EXACT in assessment.findings
    assert assessment.exact_contract_candidate is True
    assert assessment.findings == (CompatibilityFinding.EXACT,)
    assert assessment.assessment_status == "exact_same_physical_schema"


@pytest.mark.parametrize(
    ("base_names", "candidate_names", "direction", "passed"),
    [
        (("a", "b", "c"), ("a", "b", "c", "d"), "left_to_right", True),
        (("a", "b", "c"), ("a", "x", "b", "c"), "left_to_right", True),
        (("a", "b", "c"), ("a", "b"), None, False),
    ],
)
def test_additive_rule_is_directional_and_allows_inserted_unknown_fields(
    base_names: tuple[str, ...],
    candidate_names: tuple[str, ...],
    direction: str | None,
    passed: bool,
) -> None:
    base = _group("game", base_names, fingerprint_seed="base")
    candidate = _group("game", candidate_names, fingerprint_seed="candidate")

    assessment = assess_schema_pair(base, candidate)

    assert assessment.additive_direction == direction
    assert assessment.additive_rule_passed is passed
    if passed:
        assert CompatibilityFinding.ADDITIVE in assessment.findings
        assert assessment.assessment_status == "additive_compatible_under_policy"
    else:
        assert CompatibilityFinding.REMOVED in assessment.findings
        assert CompatibilityFinding.INCOMPATIBLE in assessment.findings


def test_reordered_fields_are_incompatible_until_review() -> None:
    assessment = assess_schema_pair(
        _group("game", ("a", "b", "c"), fingerprint_seed="left"),
        _group("game", ("b", "a", "c"), fingerprint_seed="right"),
    )

    assert CompatibilityFinding.REORDERED in assessment.findings
    assert CompatibilityFinding.INCOMPATIBLE in assessment.findings
    assert assessment.review_status is ReviewStatus.BLOCKED
    assert assessment.additive_rule_passed is False


def test_removal_is_not_equivalent_to_empty_field_or_default() -> None:
    assessment = assess_schema_pair(
        _group("game", ("a", "b", "c"), fingerprint_seed="left"),
        _group("game", ("a", "b"), fingerprint_seed="right"),
    )

    assert CompatibilityFinding.REMOVED in assessment.findings
    assert CompatibilityFinding.INCOMPATIBLE in assessment.findings
    assert assessment.right_only_exact_field_count == 0
    assert assessment.left_only_exact_field_count == 1


def test_rename_is_only_a_candidate_even_when_lexical_profiles_match() -> None:
    assessment = assess_schema_pair(
        _group("game", ("a", "old_name", "c"), fingerprint_seed="old"),
        _group("game", ("a", "new_name", "c"), fingerprint_seed="new"),
    )

    assert CompatibilityFinding.RENAMED_OR_ALIAS_CANDIDATE in assessment.findings
    assert assessment.assessment_status == "needs_review"
    assert assessment.additive_rule_passed is False


def test_duplicate_and_empty_header_fields_prevent_name_only_additive_mapping() -> None:
    duplicate_base = _group("game", ("a", "a", "b"), fingerprint_seed="dup-base")
    duplicate_extension = _group("game", ("a", "a", "b", "c"), fingerprint_seed="dup-ext")
    duplicate_result = assess_schema_pair(duplicate_base, duplicate_extension)
    empty_base = _group("game", ("a", "", "b"), fingerprint_seed="empty-base")
    empty_extension = _group("game", ("a", "", "b", "c"), fingerprint_seed="empty-ext")
    empty_result = assess_schema_pair(empty_base, empty_extension)

    assert CompatibilityFinding.DUPLICATE_NAME_AMBIGUITY in duplicate_result.findings
    assert duplicate_result.additive_rule_passed is False
    assert empty_result.empty_header_name_present is True
    assert empty_result.additive_rule_passed is False
    assert CompatibilityFinding.INSUFFICIENT_EVIDENCE in empty_result.findings


def test_lexical_and_empty_field_evidence_drift_are_not_semantic_type_or_null_proof() -> None:
    integer_only = _group("game", ("a", "b"), fingerprint_seed="int")
    decimal_observed = _group(
        "game",
        ("a", "b", "c"),
        classes={"a": ("integer_lexeme", "decimal_lexeme")},
        fingerprint_seed="decimal",
    )
    lexical = assess_schema_pair(integer_only, decimal_observed)
    empty = assess_schema_pair(
        _group("replay", ("a",), empty=set(), fingerprint_seed="empty-no"),
        _group("replay", ("a",), empty={"a"}, fingerprint_seed="empty-yes"),
    )

    assert CompatibilityFinding.LEXICAL_TYPE_EVIDENCE_DRIFT in lexical.findings
    assert CompatibilityFinding.SEMANTIC_DRIFT_UNKNOWN in lexical.findings
    assert CompatibilityFinding.EMPTY_FIELD_EVIDENCE_DRIFT in empty.findings
    assert all(field.exact_header_name for field in integer_only.fields)


def test_row_width_drift_blocks_additive_merge_but_preserves_group_evidence() -> None:
    anomaly = RowWidthAnomalyV1(
        source_archive_id=_digest("archive"),
        original_filename="game_sample.csv.gz",
        expected_field_count=2,
        rows_matching_header_width=250,
        rows_shorter_than_header=0,
        rows_longer_than_header=6,
        minimum_observed_row_width=2,
        maximum_observed_row_width=3,
    )
    base = _group("game", ("a", "b"), fingerprint_seed="base")
    candidate = _group(
        "game",
        ("a", "b", "c"),
        width_anomalies=(anomaly,),
        fingerprint_seed="candidate",
    )
    assessment = assess_schema_pair(base, candidate)

    assert CompatibilityFinding.ROW_WIDTH_DRIFT in assessment.findings
    assert assessment.additive_rule_passed is False
    assert candidate.row_width_anomalies[0].rows_longer_than_header == 6


def test_game_and_replay_are_never_pairwise_or_contract_family_members() -> None:
    game = _group("game", ("a", "b"), fingerprint_seed="same-fields-game")
    replay = _group("replay", ("a", "b"), fingerprint_seed="same-fields-replay")

    with pytest.raises(SchemaEvolutionError, match="Game and Replay"):
        assess_schema_pair(game, replay)
    registry = build_schema_registry(_verified((replay, game)))
    assert registry.interpretation_contract_count == 2
    assert {contract.source_kind for contract in registry.interpretation_contracts} == {
        "game",
        "replay",
    }
    assert registry.compatibility_assessments == ()


def test_contract_correction_changes_contract_id_and_registry_digest_not_raw_fingerprint() -> None:
    evidence = _verified((_group("game", ("a", "b"), fingerprint_seed="stable-raw"),))
    registry = build_schema_registry(evidence)
    contract = registry.interpretation_contracts[0]
    corrected_projection = contract.projection_dict()
    corrected_projection["unknown_additional_field_policy"] = "blocked_until_reviewed"
    corrected_id = derive_source_interpretation_contract_id(corrected_projection)
    corrected_contract = replace(
        contract,
        source_interpretation_contract_id=corrected_id,
        unknown_additional_field_policy="blocked_until_reviewed",
    )
    corrected_registry = replace(registry, interpretation_contracts=(corrected_contract,))

    assert (
        registry.groups[0].raw_schema_fingerprint
        == corrected_registry.groups[0].raw_schema_fingerprint
    )
    assert corrected_id != contract.source_interpretation_contract_id
    assert corrected_registry.schema_registry_digest != registry.schema_registry_digest


def test_interpretation_contract_hands_exact_structure_and_fail_closed_rules_to_m4() -> None:
    anomaly = RowWidthAnomalyV1(
        source_archive_id=_digest("afrw"),
        original_filename="replay_data_public.AFR.PremierDraft.csv.gz",
        expected_field_count=2,
        rows_matching_header_width=230,
        rows_shorter_than_header=0,
        rows_longer_than_header=26,
        minimum_observed_row_width=2,
        maximum_observed_row_width=3,
    )
    group = _group(
        "replay",
        ("a", "", "b"),
        width_anomalies=(anomaly,),
        fingerprint_seed="exact-structured-contract",
    )
    registry = build_schema_registry(_verified((group,)))
    contract = registry.interpretation_contracts[0]

    assert contract.required_fields == ((0, "a"), (1, ""), (2, "b"))
    assert "preserved_uninterpreted" in contract.unknown_additional_field_policy
    assert "unseen" in contract.unknown_additional_field_policy
    assert "exact registered member-header width" in contract.row_width_policy
    assert "no padding" in contract.row_width_policy
    assert "semantic mapping is unset" in contract.empty_header_name_policy
    assert contract.null_semantics_status.startswith("not_established")
    assert contract.semantic_mapping_status == "source_schema_compatibility_only"
    assert contract.semantic_drift_status.startswith("not_established")
    assert registry.groups[0].known_row_width_drift is True
    assert registry.groups[0].row_width_anomalies[0].rows_longer_than_header == 26


def test_reviewed_additive_family_preserves_extra_fields_uninterpreted() -> None:
    base = _group("game", ("a", "b"), fingerprint_seed="family-base")
    extension = _group("game", ("a", "x", "b"), fingerprint_seed="family-extension")
    registry = build_schema_registry(_verified((extension, base)))

    assert registry.interpretation_contract_count == 1
    contract = registry.interpretation_contracts[0]
    assert len(contract.member_raw_schema_fingerprints) == 2
    assert contract.reference_raw_schema_fingerprint == base.raw_schema_fingerprint
    assert contract.required_fields == ((0, "a"), (1, "b"))
    assert "preserved_uninterpreted" in contract.unknown_additional_field_policy
    assert registry.supported_group_count == 2
    assert registry.blocked_group_count == 0


def test_registry_digest_changes_when_group_membership_or_disposition_changes() -> None:
    first_group = _group("game", ("a", "b"), fingerprint_seed="registry-a")
    second_group = _group("game", ("a", "b", "c"), fingerprint_seed="registry-b")
    single = build_schema_registry(_verified((first_group,)))
    expanded = build_schema_registry(_verified((first_group, second_group)))
    blocked_entry = replace(
        single.groups[0],
        disposition=RegistryDisposition.BLOCKED_INSUFFICIENT_EVIDENCE,
        review_status=ReviewStatus.BLOCKED,
    )
    blocked = replace(
        single, groups=(blocked_entry,), supported_group_count=0, blocked_group_count=1
    )

    assert expanded.schema_registry_digest != single.schema_registry_digest
    assert blocked.schema_registry_digest != single.schema_registry_digest


def test_registry_is_order_and_runtime_path_independent_and_accounts_every_group() -> None:
    groups = (
        _group("game", ("a", "b"), fingerprint_seed="g1"),
        _group("game", ("a", "b", "c"), fingerprint_seed="g2"),
        _group("replay", ("x", "y"), fingerprint_seed="r1"),
    )
    first = build_schema_registry(_verified(groups))
    permuted = build_schema_registry(_verified(tuple(reversed(groups))))

    assert first.schema_registry_digest == permuted.schema_registry_digest
    assert first.canonical_bytes == permuted.canonical_bytes
    assert len(first.groups) == 3
    assert len({item.group_id for item in first.groups}) == 3
    assert all(item.source_interpretation_contract_id for item in first.groups)
    assert first.supported_group_count + first.blocked_group_count == first.schema_group_count
    assert first.supported_group_count == 3
    assert first.source_url_status_counts == (("unknown", 3),)
    assert first.license_status_counts == (("unknown", 3),)


def test_no_transitive_compatibility_without_direct_reference_check() -> None:
    a = _group("game", ("id",), classes={"id": ("integer_lexeme",)}, fingerprint_seed="a")
    b = _group("game", ("id", "extra_b"), classes={"id": ("integer_lexeme",)}, fingerprint_seed="b")
    c = _group(
        "game",
        ("id", "extra_b", "extra_c"),
        classes={"id": ("integer_lexeme", "decimal_lexeme")},
        fingerprint_seed="c",
    )
    assessments = tuple(
        assess_schema_pair(left, right)
        for left in (a, b, c)
        for right in (a, b, c)
        if left.group_id != right.group_id
    )
    from openmtgdata.schema_evolution import _form_interpretation_families

    families = _form_interpretation_families((a, b, c), assessments)

    assert any(
        {item.group_id for item in family} == {a.group_id, b.group_id} for family in families
    )
    assert any({item.group_id for item in family} == {c.group_id} for family in families)


def test_report_loader_binds_digest_and_ignores_audit_timestamp(tmp_path: Path) -> None:
    first_doc = _synthetic_report_document(audit_timestamp="2026-01-01T00:00:00Z")
    second_doc = _synthetic_report_document(audit_timestamp="2027-01-01T00:00:00Z")
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(json.dumps(first_doc), encoding="utf-8")
    second_path.write_text(json.dumps(second_doc), encoding="utf-8")

    first = load_verified_m3_evidence(first_path)
    second = load_verified_m3_evidence(second_path)
    registry_first = build_schema_registry(first)
    registry_second = build_schema_registry(second)

    assert first.deep_inspection_evidence_digest == second.deep_inspection_evidence_digest
    assert registry_first.schema_registry_digest == registry_second.schema_registry_digest


def test_report_loader_rejects_corrupt_digest_and_wrong_contract(tmp_path: Path) -> None:
    document = _synthetic_report_document()
    document["evidence_digest"] = "0" * 64
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DeepReportValidationError, match="does not match"):
        load_verified_m3_evidence(corrupt)

    document = _synthetic_report_document()
    document["evidence"]["deep_inspection_report_contract_id"] = "unsupported.v2"
    evidence_json = json.dumps(
        document["evidence"], ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    document["evidence_digest"] = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
    wrong_contract = tmp_path / "wrong-contract.json"
    wrong_contract.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DeepReportValidationError, match="unsupported M3.1"):
        load_verified_m3_evidence(wrong_contract)


def test_classify_schemas_cli_uses_persisted_evidence_without_raw_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from openmtgdata.cli import main
    from openmtgdata.config import RuntimeConfig

    raw = tmp_path / "raw"
    raw.mkdir()
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )
    deep_path = tmp_path / "source-evidence.json"
    deep_document = _synthetic_report_document()
    deep_path.write_text(json.dumps(deep_document), encoding="utf-8")
    registry_output = config.intermediate_root / "schema-registry.json"

    def no_raw_inventory(*args, **kwargs):
        raise AssertionError("M3.2 must not inventory or open raw source archives")

    monkeypatch.setattr("openmtgdata.cli.inventory_raw_roots", no_raw_inventory)
    monkeypatch.setattr("openmtgdata.cli.register_inventory_archives", no_raw_inventory)
    exit_code = main(
        [
            "classify-schemas",
            "--raw-root",
            str(raw),
            "--base-dir",
            str(tmp_path),
            "--intermediate-root",
            str(config.intermediate_root),
            "--quarantine-root",
            str(config.quarantine_root),
            "--release-root",
            str(config.release_root),
            "--deep-report",
            str(deep_path),
            "--output",
            str(registry_output),
        ]
    )
    stdout = capsys.readouterr()
    summary = json.loads(stdout.out)
    registry_document = json.loads(registry_output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["raw_schema_groups"] == 1
    assert summary["supported_groups"] == 1
    assert summary["source_url_status_counts"] == {"unknown": 1}
    assert summary["license_status_counts"] == {"unknown": 1}
    assert registry_document["registry_digest"] == summary["schema_registry_digest"]
    assert not list(raw.iterdir())


def test_registry_output_is_compact_value_free_and_safe_under_intermediate(tmp_path: Path) -> None:
    from openmtgdata.config import RuntimeConfig
    from openmtgdata.schema_evolution import write_schema_registry_no_overwrite

    raw = tmp_path / "raw"
    raw.mkdir()
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )
    registry = build_schema_registry(_verified((_group("game", ("a", "b")),)))
    output = write_schema_registry_no_overwrite(
        registry,
        config.intermediate_root / "schema-registry.json",
        config,
        base_dir=tmp_path,
    )
    serialized = output.read_text(encoding="utf-8")

    assert output.is_relative_to(config.intermediate_root)
    assert "C:\\" not in serialized
    assert "example_values" not in serialized
    assert "raw_row_values" not in serialized
    with pytest.raises(SchemaEvolutionError, match="refusing overwrite"):
        write_schema_registry_no_overwrite(
            registry,
            output,
            config,
            base_dir=tmp_path,
        )
