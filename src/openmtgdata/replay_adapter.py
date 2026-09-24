"""Pure M5.1 normalization for reviewed Replay turn-summary slot mappings."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

from openmtgdata.replay_schema import (
    REPLAY_EVENT_SCHEMA_ID,
    REPLAY_NORMALIZATION_CONTRACT_ID,
    ReplayEventLocatorV1,
    ReplayEventV1,
    ReplayFieldMappingV1,
    ReplayFieldValueV1,
    ReplayMappingRegistryV1,
    ReplayNormalizationRejectionV1,
    ReplayNormalizationResultV1,
    ReplayQualityCode,
    ReplaySourceFieldMappingV1,
    ReplayTurnSideV1,
    SourceColumnSelectorV1,
    _canonical_bytes,
)
from openmtgdata.source_reader import (
    BATCH_CONTRACT_ID,
    RAW_RECORD_CONTRACT_ID,
    READER_CONTRACT_ID,
    RawCsvBatchV1,
    RawCsvRecordV1,
)

_STRICT_NONNEGATIVE_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class ReplayNormalizationError(RuntimeError):
    """Fatal adapter/mapping contract mismatch; no fallback mapping is attempted."""


def parse_strict_turn_count(value: str) -> int:
    """Accept canonical nonnegative ASCII decimal integer lexemes only."""
    if not _STRICT_NONNEGATIVE_INTEGER.fullmatch(value):
        raise ValueError("turn count is not a canonical nonnegative integer lexeme")
    return int(value)


def parse_strict_on_play(value: str) -> bool:
    """The reviewed AFR Replay mapping uses exact source lexemes 0 and 1."""
    if value == "0":
        return False
    if value == "1":
        return True
    raise ValueError("on_play must be the exact source string '0' or '1'")


class ReplayAdapterV1:
    """Stateless adapter; record semantics do not depend on batch/checkpoint boundaries."""

    _registry: ReplayMappingRegistryV1 | None
    _contracts: dict[str, ReplayFieldMappingV1]
    _slot_fields: dict[str, dict[tuple[str, int], tuple[ReplaySourceFieldMappingV1, ...]]]

    def __init__(self, registry: ReplayMappingRegistryV1) -> None:
        self._registry = registry
        self._contracts = {item.raw_schema_fingerprint: item for item in registry.mapping_contracts}
        if len(self._contracts) != len(registry.mapping_contracts):
            raise ReplayNormalizationError("mapping registry contains duplicate raw fingerprints")
        self._slot_fields = {
            fingerprint: _index_turn_fields(mapping)
            for fingerprint, mapping in self._contracts.items()
        }

    @classmethod
    def _for_review_candidate(cls, mapping: ReplayFieldMappingV1) -> ReplayAdapterV1:
        """Internal evidence-pass adapter; final use must go through a reviewed registry."""
        instance = object.__new__(cls)
        instance._registry = None
        instance._contracts = {mapping.raw_schema_fingerprint: mapping}
        instance._slot_fields = {mapping.raw_schema_fingerprint: _index_turn_fields(mapping)}
        return instance

    @property
    def mapping_registry(self) -> ReplayMappingRegistryV1:
        if self._registry is None:
            raise ReplayNormalizationError(
                "review-candidate adapter has no published mapping registry"
            )
        return self._registry

    def normalize_record(
        self,
        raw_record: RawCsvRecordV1,
        *,
        header_fields: tuple[str, ...],
    ) -> ReplayNormalizationResultV1:
        if raw_record.raw_csv_record_contract_id != RAW_RECORD_CONTRACT_ID:
            raise ReplayNormalizationError("unsupported M4.1 RawCsvRecordV1 contract")
        mapping = self._contracts.get(raw_record.raw_schema_fingerprint)
        if mapping is None:
            raise ReplayNormalizationError("unknown Replay raw fingerprint; no fallback mapping")
        if raw_record.source_interpretation_contract_id != (
            mapping.source_interpretation_contract_id
        ):
            raise ReplayNormalizationError(
                "M4.1 interpretation identity disagrees with M5.1 mapping"
            )
        if len(raw_record.fields) != len(header_fields):
            raise ReplayNormalizationError("accepted raw record width differs from supplied header")
        self._validate_mapping_header(mapping, header_fields)
        values = raw_record.fields
        turn_count_raw = values[mapping.turn_count_selector.column_index]
        on_play_raw = values[mapping.on_play_selector.column_index]
        try:
            turn_count = parse_strict_turn_count(turn_count_raw)
        except ValueError:
            return self._reject(
                raw_record,
                ReplayQualityCode.INVALID_SOURCE_TURN_COUNT,
                "source turns value does not match the strict v1 integer grammar",
                mapping.turn_count_selector,
            )
        try:
            on_play = parse_strict_on_play(on_play_raw)
        except ValueError:
            return self._reject(
                raw_record,
                ReplayQualityCode.INVALID_SOURCE_ON_PLAY,
                "source on_play value is outside the reviewed exact 0/1 vocabulary",
                mapping.on_play_selector,
            )
        if turn_count > mapping.maximum_supported_turn_count:
            return self._reject(
                raw_record,
                ReplayQualityCode.TURN_COUNT_EXCEEDS_PHYSICAL_SLOTS,
                "source turn count exceeds the exact registered per-side slot range",
                mapping.turn_count_selector,
            )
        if turn_count == 0:
            return ReplayNormalizationResultV1(
                REPLAY_NORMALIZATION_CONTRACT_ID,
                raw_record.source_archive_id,
                raw_record.data_record_ordinal,
                (),
                (ReplayQualityCode.SOURCE_ZERO_TURN_COUNT,),
                None,
            )

        events: list[ReplayEventV1] = []
        first_side = "user" if on_play else "oppo"
        second_side = "oppo" if on_play else "user"
        selectors_by_slot = self._slot_fields[raw_record.raw_schema_fingerprint]

        for event_ordinal in range(turn_count):
            side = first_side if event_ordinal % 2 == 0 else second_side
            slot_index = event_ordinal // 2 + 1
            mappings = selectors_by_slot.get((side, slot_index), ())
            if not mappings:
                raise ReplayNormalizationError("reviewed turn slot selector set is incomplete")
            source_fields = tuple(
                ReplayFieldValueV1(
                    item.selector.column_index,
                    item.selector.exact_header_name,
                    values[item.selector.column_index],
                )
                for item in mappings
            )
            if all(item.value == "" for item in source_fields):
                return ReplayNormalizationResultV1(
                    REPLAY_NORMALIZATION_CONTRACT_ID,
                    raw_record.source_archive_id,
                    raw_record.data_record_ordinal,
                    (),
                    (),
                    ReplayNormalizationRejectionV1(
                        raw_record.data_record_ordinal,
                        ReplayQualityCode.PARTIAL_EVENT_SLOT,
                        "active indexed turn slot has no populated source field",
                        tuple(
                            SourceColumnSelectorV1(item.column_index, item.exact_header_name)
                            for item in source_fields
                        ),
                    ),
                )
            events.append(
                ReplayEventV1(
                    REPLAY_EVENT_SCHEMA_ID,
                    raw_record.source_archive_id,
                    raw_record.raw_schema_fingerprint,
                    raw_record.source_interpretation_contract_id,
                    mapping.replay_field_mapping_id,
                    ReplayEventLocatorV1(
                        raw_record.source_archive_id,
                        raw_record.data_record_ordinal,
                        event_ordinal,
                    ),
                    turn_count_raw,
                    on_play_raw,
                    ReplayTurnSideV1(side),
                    slot_index,
                    turn_count,
                    on_play,
                    source_fields,
                    (ReplayQualityCode.UNMAPPED_SOURCE_FIELDS_PRESENT,),
                )
            )
        return ReplayNormalizationResultV1(
            REPLAY_NORMALIZATION_CONTRACT_ID,
            raw_record.source_archive_id,
            raw_record.data_record_ordinal,
            tuple(events),
            (ReplayQualityCode.UNMAPPED_SOURCE_FIELDS_PRESENT,),
            None,
        )

    def normalize_batch(
        self,
        batch: RawCsvBatchV1,
        *,
        header_fields: tuple[str, ...],
    ) -> Iterator[ReplayNormalizationResultV1]:
        if batch.batch_contract_id != BATCH_CONTRACT_ID:
            raise ReplayNormalizationError("unsupported M4.1 raw batch contract")
        if batch.reader_contract_id != READER_CONTRACT_ID:
            raise ReplayNormalizationError("batch was not produced by the accepted M4.1 reader")
        if batch.raw_schema_fingerprint not in self._contracts:
            raise ReplayNormalizationError("batch fingerprint is not in the M5.1 registry")
        mapping = self._contracts[batch.raw_schema_fingerprint]
        if batch.source_interpretation_contract_id != mapping.source_interpretation_contract_id:
            raise ReplayNormalizationError("batch M3.2 interpretation ID differs from M5.1 mapping")
        if self._registry is not None and batch.schema_registry_digest != (
            self._registry.schema_registry_digest
        ):
            raise ReplayNormalizationError("batch M3.2 registry digest differs from M5.1 registry")
        for record in batch.accepted_records:
            if (
                record.source_archive_id != batch.source_archive_id
                or record.raw_schema_fingerprint != batch.raw_schema_fingerprint
                or record.source_interpretation_contract_id
                != batch.source_interpretation_contract_id
            ):
                raise ReplayNormalizationError("raw record lineage disagrees with its M4.1 batch")
            yield self.normalize_record(record, header_fields=header_fields)

    @staticmethod
    def _validate_mapping_header(
        mapping: ReplayFieldMappingV1,
        header_fields: tuple[str, ...],
    ) -> None:
        if not _matches(header_fields, mapping.turn_count_selector):
            raise ReplayNormalizationError(
                "turn count header selector does not match actual header"
            )
        if not _matches(header_fields, mapping.on_play_selector):
            raise ReplayNormalizationError("on_play header selector does not match actual header")
        if len(mapping.field_dispositions) != len(header_fields):
            raise ReplayNormalizationError(
                "mapping does not account for every physical header field"
            )
        for disposition in mapping.field_dispositions:
            if not _matches(header_fields, disposition.selector):
                raise ReplayNormalizationError("field disposition selector/header mismatch")
        for field in mapping.mapped_turn_fields:
            if not _matches(header_fields, field.selector):
                raise ReplayNormalizationError("turn field selector/header mismatch")

    @staticmethod
    def _reject(
        raw_record: RawCsvRecordV1,
        code: ReplayQualityCode,
        reason: str,
        selector: SourceColumnSelectorV1,
    ) -> ReplayNormalizationResultV1:
        return ReplayNormalizationResultV1(
            REPLAY_NORMALIZATION_CONTRACT_ID,
            raw_record.source_archive_id,
            raw_record.data_record_ordinal,
            (),
            (),
            ReplayNormalizationRejectionV1(
                raw_record.data_record_ordinal,
                code,
                reason,
                (selector,),
            ),
        )


def _matches(header_fields: tuple[str, ...], selector: SourceColumnSelectorV1) -> bool:
    return (
        0 <= selector.column_index < len(header_fields)
        and header_fields[selector.column_index] == selector.exact_header_name
    )


def _index_turn_fields(
    mapping: ReplayFieldMappingV1,
) -> dict[tuple[str, int], tuple[ReplaySourceFieldMappingV1, ...]]:
    grouped: dict[tuple[str, int], list[ReplaySourceFieldMappingV1]] = {}
    for field in mapping.mapped_turn_fields:
        grouped.setdefault((field.event_side, field.event_slot_index), []).append(field)
    return {
        key: tuple(sorted(fields, key=lambda item: item.selector.column_index))
        for key, fields in grouped.items()
    }


class ReplaySemanticStreamDigestV1:
    """Incremental validation digest over canonical semantic ReplayEvent projections."""

    def __init__(self) -> None:
        self._hasher = hashlib.sha256()
        domain = _canonical_bytes(
            {
                "contract_id": REPLAY_NORMALIZATION_CONTRACT_ID,
                "purpose": "local-validation-stream-digest",
            }
        )
        self._hasher.update(len(domain).to_bytes(8, "big"))
        self._hasher.update(domain)
        self.event_count = 0

    def update(self, event: ReplayEventV1) -> None:
        payload = _canonical_bytes(event.semantic_projection_dict())
        self._hasher.update(len(payload).to_bytes(8, "big"))
        self._hasher.update(payload)
        self.event_count += 1

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()
