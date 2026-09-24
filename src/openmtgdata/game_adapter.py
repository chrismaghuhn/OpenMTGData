"""Deterministic M5.2 source-fact mapping for reviewed Game schemas."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from openmtgdata.game_schema import (
    GAME_EVIDENCE_BINDINGS,
    GAME_RECORD_SCHEMA_ID,
    GameDisposition,
    GameFieldLineageV1,
    GameFieldMappingV1,
    GameMappingGroupV1,
    GameMappingRegistryV1,
    GameMappingReviewV1,
    GameQualityCode,
    GameRecordLocatorV1,
    GameRecordV1,
    GameSchemaError,
    GameSourceFactV1,
    GameSourceSelectorV1,
    derive_game_m4_review_evidence_id,
    derive_game_mapping_id,
    derive_game_registry_digest,
    validate_game_mapping,
    validate_game_mapping_review,
    validate_game_registry,
)
from openmtgdata.source_filename import RecognizedSourceFilename, SourceKind
from openmtgdata.source_reader import RAW_RECORD_CONTRACT_ID, READER_CONTRACT_ID

if TYPE_CHECKING:
    from openmtgdata.archive_registration import SourceArchiveRecordV1
    from openmtgdata.schema_evolution import RawSchemaGroupEvidenceV1
    from openmtgdata.source_reader import RawCsvRecordV1, VerifiedSchemaRegistryV1

GAME_MAPPING_POLICY_ID = "openmtgdata.game-mapping-policy.afr-source-facts.v1"
AFR_GAME_RAW_FINGERPRINT = "a2bddf4d55c72c4c329eda7af4740d0fce03a66a626f1c44148282ae80b5b840"
_AFR_SOURCE_FIELDS = (
    (0, "user_win_rate_bucket", "source_user_win_rate_bucket_raw"),
    (1, "user_n_games_bucket", "source_user_n_games_bucket_raw"),
    (2, "draft_id", "source_draft_id_raw"),
    (3, "build_index", "source_build_index_raw"),
    (4, "draft_time", "source_draft_time_raw"),
    (5, "expansion", "source_expansion_raw"),
    (6, "event_type", "source_event_type_raw"),
    (7, "game_number", "source_game_number_raw"),
    (8, "rank", "source_rank_raw"),
    (9, "opp_rank", "source_opp_rank_raw"),
    (10, "main_colors", "source_main_colors_raw"),
    (11, "splash_colors", "source_splash_colors_raw"),
    (12, "on_play", "source_on_play_raw"),
    (13, "num_mulligans", "source_num_mulligans_raw"),
    (14, "opp_num_mulligans", "source_opp_num_mulligans_raw"),
    (15, "opp_colors", "source_opp_colors_raw"),
    (16, "num_turns", "source_num_turns_raw"),
    (17, "won", "source_won_raw"),
)


def _mapping_for_group(
    group: RawSchemaGroupEvidenceV1,
    source_interpretation_contract_id: str,
    *,
    schema_registry_digest: str,
    m4_review_evidence_id: str,
) -> GameFieldMappingV1:
    if group.raw_schema_fingerprint != AFR_GAME_RAW_FINGERPRINT:
        raise GameSchemaError("no reviewed M5.2 mapping exists for this Game fingerprint")
    names = group.field_names
    if len(names) <= 17 or any(names[index] != name for index, name, _ in _AFR_SOURCE_FIELDS):
        raise GameSchemaError("AFR Game header does not match reviewed exact selectors")
    selectors = tuple(GameSourceSelectorV1(index, name) for index, name, _ in _AFR_SOURCE_FIELDS)
    mappings = tuple(
        (selector, path)
        for selector, (_, _, path) in zip(selectors, _AFR_SOURCE_FIELDS, strict=True)
    )
    lineages = tuple(
        GameFieldLineageV1(
            normalized_field_path=path,
            origin="source_direct",
            source_selectors=(selector,),
            derived_inputs=(),
            transformation_id="openmtgdata.preserve-source-csv-string.v1",
            parser_id="openmtgdata.source-string-exact.v1",
            input_cardinality="one_source_field_per_source_row",
            empty_value_policy="preserve_empty_string",
            failure_behavior="preserve_exact_parser_returned_string",
            evidence_basis=(
                "M3.1 bounded lexical evidence",
                "full M4.1 AFR PremierDraft source stream",
                "exact index-and-name header selector",
            ),
        )
        for (selector, path), (_, _, _) in zip(mappings, _AFR_SOURCE_FIELDS, strict=True)
    )
    dispositions = tuple(
        (
            GameSourceSelectorV1(field.column_index, field.exact_header_name),
            "mapped"
            if field.column_index in {item.column_index for item in selectors}
            else "unmapped_preserved_at_raw_layer",
        )
        for field in group.fields
    )
    initial = GameFieldMappingV1(
        game_field_mapping_id="",
        raw_schema_fingerprint=group.raw_schema_fingerprint,
        source_interpretation_contract_id=source_interpretation_contract_id,
        source_kind=SourceKind.GAME,
        row_granularity=(
            "one accepted source CSV row; paired user/opponent-like rows were observed, "
            "but game identity/cardinality is not asserted"
        ),
        mappings=mappings,
        field_dispositions=dispositions,
        lineages=lineages,
        row_width_policy=(
            "exact physical header width; M4 rejects shorter/longer rows "
            "without padding or truncation"
        ),
        empty_value_policy="preserve exact empty string; no null semantics assigned",
        semantic_boundary=(
            "source-labeled raw facts only; no actor, outcome, join, "
            "or game-identity interpretation"
        ),
        evidence_bindings=(
            *GAME_EVIDENCE_BINDINGS,
            f"M3.2 schema registry:{schema_registry_digest}",
            m4_review_evidence_id,
        ),
    )
    result = replace(initial, game_field_mapping_id=derive_game_mapping_id(initial))
    validate_game_mapping(result)
    return result


def build_game_mapping_registry(
    groups: tuple[RawSchemaGroupEvidenceV1, ...],
    schema_registry: VerifiedSchemaRegistryV1,
    *,
    semantic_source_catalog_digest: str,
    deep_inspection_evidence_digest: str,
    reviews: tuple[GameMappingReviewV1, ...] = (),
) -> GameMappingRegistryV1:
    """Account for all M3 Game groups; support only independently reviewed groups."""
    if schema_registry.schema_registry_digest == "":
        raise GameSchemaError("M3.2 registry digest is required")
    game_groups = sorted(
        (item for item in groups if item.source_kind == SourceKind.GAME.value),
        key=lambda item: item.raw_schema_fingerprint,
    )
    m32 = {
        item.raw_schema_fingerprint: item
        for item in schema_registry.groups
        if item.source_kind is SourceKind.GAME
    }
    if len(game_groups) != len(m32):
        raise GameSchemaError("M3.1 Game groups do not match M3.2 schema authority")
    mapping_by_fp: dict[str, GameFieldMappingV1] = {}
    group_entries: list[GameMappingGroupV1] = []
    seen_review_fingerprints: set[str] = set()
    review_by_fp = {item.raw_schema_fingerprint: item for item in reviews}
    if len(review_by_fp) != len(reviews):
        raise GameSchemaError("duplicate Game mapping review fingerprint")
    for group in game_groups:
        authority = m32.get(group.raw_schema_fingerprint)
        if authority is None:
            raise GameSchemaError("M3.2 registry is missing an observed Game fingerprint")
        mapping = None
        disposition = GameDisposition.NEEDS_REVIEW
        reason = (
            "No M5.2 full-stream review and explicit mapping approval for this physical schema."
        )
        review = review_by_fp.get(group.raw_schema_fingerprint)
        if review is not None:
            seen_review_fingerprints.add(group.raw_schema_fingerprint)
            mapping = _mapping_for_group(
                group,
                authority.source_interpretation_contract_id,
                schema_registry_digest=schema_registry.schema_registry_digest,
                m4_review_evidence_id=derive_game_m4_review_evidence_id(review),
            )
            validate_game_mapping_review(
                review,
                group=GameMappingGroupV1(
                    group.raw_schema_fingerprint,
                    authority.source_interpretation_contract_id,
                    GameDisposition.NEEDS_REVIEW,
                    None,
                    group.archive_count,
                    group.source_archive_ids,
                    len(group.fields),
                    reason,
                    None,
                    (),
                ),
                mapping=mapping,
                schema_registry_digest=schema_registry.schema_registry_digest,
            )
            disposition = GameDisposition.SUPPORTED
            reason = (
                "AFR PremierDraft Game source facts were checked over the complete M4.1 stream."
            )
            mapping_by_fp[group.raw_schema_fingerprint] = mapping
        anomalies = tuple(
            sorted(
                {
                    "row_width_drift"
                    for anomaly in group.row_width_anomalies
                    if anomaly.rows_shorter_than_header or anomaly.rows_longer_than_header
                }
            )
        )
        group_entries.append(
            GameMappingGroupV1(
                raw_schema_fingerprint=group.raw_schema_fingerprint,
                source_interpretation_contract_id=authority.source_interpretation_contract_id,
                disposition=disposition,
                game_field_mapping_id=mapping.game_field_mapping_id if mapping else None,
                archive_count=group.archive_count,
                source_archive_ids=group.source_archive_ids,
                field_count=len(group.fields),
                reason=reason,
                row_width_policy=mapping.row_width_policy if mapping else None,
                known_anomalies=anomalies,
                review=review,
            )
        )
    if seen_review_fingerprints != set(review_by_fp):
        raise GameSchemaError("Game mapping review references an unknown group")
    registry = GameMappingRegistryV1(
        schema_registry_digest=schema_registry.schema_registry_digest,
        semantic_source_catalog_digest=semantic_source_catalog_digest,
        deep_inspection_evidence_digest=deep_inspection_evidence_digest,
        game_mapping_policy_id=GAME_MAPPING_POLICY_ID,
        mappings=tuple(mapping_by_fp[key] for key in sorted(mapping_by_fp)),
        groups=tuple(group_entries),
        game_mapping_registry_digest="",
    )
    return replace(registry, game_mapping_registry_digest=derive_game_registry_digest(registry))


class GameAdapterV1:
    """Pure raw-record mapper; source records and M3 identities remain immutable."""

    def __init__(self, registry: GameMappingRegistryV1) -> None:
        self._registry = registry
        self._mappings = {item.raw_schema_fingerprint: item for item in registry.mappings}
        validate_game_registry(registry)

    def normalize_record(
        self,
        raw_record: RawCsvRecordV1,
        *,
        source_archive_record: SourceArchiveRecordV1,
        header_fields: tuple[str, ...],
    ) -> GameRecordV1:
        if raw_record.source_archive_id != source_archive_record.source_archive_id:
            raise GameSchemaError("raw record does not belong to supplied registered source")
        if raw_record.raw_csv_record_contract_id != RAW_RECORD_CONTRACT_ID:
            raise GameSchemaError("raw record contract is not accepted M4.1 RawCsvRecordV1")
        if raw_record.reader_contract_id != READER_CONTRACT_ID:
            raise GameSchemaError("raw record reader contract is not accepted M4.1")
        if (
            not isinstance(source_archive_record.filename_result, RecognizedSourceFilename)
            or source_archive_record.filename_result.source_kind is not SourceKind.GAME
        ):
            raise GameSchemaError("Game adapter accepts registered Game sources only")
        mapping = self._mappings.get(raw_record.raw_schema_fingerprint)
        if mapping is None:
            raise GameSchemaError("raw Game schema has no supported M5.2 mapping")
        group = next(
            (
                item
                for item in self._registry.groups
                if item.raw_schema_fingerprint == raw_record.raw_schema_fingerprint
            ),
            None,
        )
        if group is None or group.disposition is not GameDisposition.SUPPORTED:
            raise GameSchemaError("Game schema group is not supported")
        if (
            raw_record.source_interpretation_contract_id
            != mapping.source_interpretation_contract_id
            or group.source_interpretation_contract_id != mapping.source_interpretation_contract_id
        ):
            raise GameSchemaError("M3.2 interpretation identity mismatch")
        if len(raw_record.fields) != len(header_fields):
            raise GameSchemaError("accepted M4 record width differs from supplied header")
        facts: list[GameSourceFactV1] = []
        for selector, path in mapping.mappings:
            if (
                selector.column_index >= len(header_fields)
                or header_fields[selector.column_index] != selector.exact_header_name
            ):
                raise GameSchemaError("Game mapping selector does not match actual M4 header")
            lineage = next(item for item in mapping.lineages if item.normalized_field_path == path)
            facts.append(GameSourceFactV1(path, raw_record.fields[selector.column_index], lineage))
        qualities = (
            (GameQualityCode.UNMAPPED_SOURCE_FIELDS_PRESENT,)
            if len(mapping.mappings) < len(header_fields)
            else (GameQualityCode.COMPLETE_SOURCE_MAPPING,)
        )
        return GameRecordV1(
            game_record_contract_id=GAME_RECORD_SCHEMA_ID,
            source_archive_id=raw_record.source_archive_id,
            raw_schema_fingerprint=raw_record.raw_schema_fingerprint,
            source_interpretation_contract_id=raw_record.source_interpretation_contract_id,
            game_field_mapping_id=mapping.game_field_mapping_id,
            locator=GameRecordLocatorV1(
                raw_record.source_archive_id, raw_record.data_record_ordinal
            ),
            source_facts=tuple(facts),
            quality_codes=qualities,
        )


def write_game_artifacts(
    analysis_path: Path,
    registry_path: Path,
    analysis: dict[str, object],
    registry: GameMappingRegistryV1,
) -> None:
    """Write deterministic local evidence artifacts; never stores row values/paths."""
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    registry_path.write_text(
        json.dumps(registry.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
