"""Model-agnostic source turn-summary sequence contracts for M7.2.

These objects implement next-completed-turn summary prediction. They do not
represent actions, decisions, actor observations, or policy-imitation samples.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from openmtgdata.replay_schema import (
    REPLAY_EVENT_SCHEMA_ID,
    ReplayEventV1,
    ReplayFieldMappingV1,
    ReplayMappingReviewV1,
    ReplayNormalizationResultV1,
    ReplayTurnSideV1,
)
from openmtgdata.resumable_reader import source_reader_config_identity
from openmtgdata.source_reader import (
    CompletionStatus,
    RawCsvBatchV1,
    SourceReaderConfigV1,
    SourceReaderSummaryV1,
)

TURN_SUMMARY_FRAME_CONTRACT_ID = "openmtgdata.turn-summary-frame.v1"
TURN_SUMMARY_SEQUENCE_CONTRACT_ID = "openmtgdata.turn-summary-sequence.v1"
TURN_SUMMARY_SEQUENCE_LOCATOR_CONTRACT_ID = "openmtgdata.turn-summary-sequence-locator.v1"
NEXT_TURN_PREDICTION_VIEW_CONTRACT_ID = "openmtgdata.next-turn-prediction-view.v1"
NEXT_TURN_PREDICTION_VIEW_LOCATOR_CONTRACT_ID = "openmtgdata.next-turn-prediction-view-locator.v1"
TURN_SEQUENCE_BUILD_REPORT_CONTRACT_ID = "openmtgdata.turn-sequence-build-report.v1"
TURN_SEQUENCE_BUILD_DIGEST_CONTRACT_ID = "openmtgdata.turn-sequence-build-digest.v1"
TURN_SEQUENCE_LOGICAL_DIGEST_CONTRACT_ID = "openmtgdata.turn-sequence-logical-digest.v1"
TURN_SUMMARY_FIELD_ORDER_CONTRACT_ID = "openmtgdata.turn-summary-field-order.exact-suffix-v1"
NEXT_TURN_OBJECTIVE_ID = "openmtgdata.next-source-turn-summary.v1"
TURN_SEQUENCE_VIEW_KIND = "source_turn_summary_sequence_prediction"
TURN_SEQUENCE_INFORMATION_SCOPE = "source_record_summary_not_actor_observation"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPLAY_MAPPING_ID = re.compile(r"openmtgdata\.replay-field-mapping\.v1:[0-9a-f]{64}\Z")


class TurnSequenceError(ValueError):
    """Invalid M4/M5 input, sequence structure, or full-stream reconciliation."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8", errors="strict"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class TurnSummarySequenceLocatorV1:
    source_archive_id: str
    data_record_ordinal: int

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.source_archive_id) or self.data_record_ordinal < 1:
            raise TurnSequenceError("turn sequence locator has invalid archive/row identity")

    def to_dict(self) -> dict[str, object]:
        return {
            "data_record_ordinal": self.data_record_ordinal,
            "locator_contract_id": TURN_SUMMARY_SEQUENCE_LOCATOR_CONTRACT_ID,
            "source_archive_id": self.source_archive_id,
        }


@dataclass(frozen=True, slots=True)
class NextTurnPredictionViewLocatorV1:
    source_archive_id: str
    data_record_ordinal: int
    target_event_ordinal: int

    def __post_init__(self) -> None:
        if (
            not _SHA256.fullmatch(self.source_archive_id)
            or self.data_record_ordinal < 1
            or self.target_event_ordinal < 1
        ):
            raise TurnSequenceError("next-turn view locator requires a noninitial target frame")

    def to_dict(self) -> dict[str, object]:
        return {
            "data_record_ordinal": self.data_record_ordinal,
            "locator_contract_id": NEXT_TURN_PREDICTION_VIEW_LOCATOR_CONTRACT_ID,
            "source_archive_id": self.source_archive_id,
            "target_event_ordinal": self.target_event_ordinal,
        }


@dataclass(frozen=True, slots=True)
class TurnSummaryFrameV1:
    event_ordinal_within_source_record: int
    source_turn_side: str
    source_turn_slot_index: int
    field_values: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.event_ordinal_within_source_record < 0 or self.source_turn_slot_index < 1:
            raise TurnSequenceError("frame has invalid chronological/side-local slot index")
        if self.source_turn_side not in {ReplayTurnSideV1.USER.value, ReplayTurnSideV1.OPPO.value}:
            raise TurnSequenceError("frame source side must remain user or oppo")
        if any(not isinstance(value, str) for value in self.field_values):
            raise TurnSequenceError("frame fields must remain exact strings")

    def to_dict(self) -> dict[str, object]:
        """Compact canonical frame; values align to the sequence's exact suffix order."""
        return {
            "event_ordinal_within_source_record": self.event_ordinal_within_source_record,
            "field_values": list(self.field_values),
            "frame_contract_id": TURN_SUMMARY_FRAME_CONTRACT_ID,
            "source_turn_side": self.source_turn_side,
            "source_turn_slot_index": self.source_turn_slot_index,
        }

    def model_projection_dict(self, suffix_order: tuple[str, ...]) -> dict[str, object]:
        if len(suffix_order) != len(self.field_values):
            raise TurnSequenceError("frame values do not match the exact suffix ordering")
        return {
            "event_ordinal_within_source_record": self.event_ordinal_within_source_record,
            "fields": [
                {"exact_suffix": suffix, "source_value": value}
                for suffix, value in zip(suffix_order, self.field_values, strict=True)
            ],
            "source_turn_side": self.source_turn_side,
            "source_turn_slot_index": self.source_turn_slot_index,
        }


@dataclass(frozen=True, slots=True)
class TurnSequenceBindingsV1:
    source_catalog_digest: str
    m3_evidence_digest: str
    schema_registry_digest: str
    compressed_sha256: str
    compressed_size_bytes: int
    source_interpretation_contract_id: str
    m4_reader_contract_id: str
    source_reader_config_digest: str
    replay_mapping_registry_digest: str
    m5_semantic_validation_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.source_catalog_digest,
            self.m3_evidence_digest,
            self.schema_registry_digest,
            self.compressed_sha256,
            self.source_reader_config_digest,
            self.replay_mapping_registry_digest,
            self.m5_semantic_validation_digest,
        ):
            if not _SHA256.fullmatch(value):
                raise TurnSequenceError("sequence input binding must be lowercase SHA-256")
        if not re.fullmatch(
            r"openmtgdata\.source-interpretation\.v1:[0-9a-f]{64}",
            self.source_interpretation_contract_id,
        ):
            raise TurnSequenceError("sequence M3.2 interpretation ID is malformed")
        if self.m4_reader_contract_id != "openmtgdata.raw-source-reader.v2":
            raise TurnSequenceError("turn sequence requires M4 reader v2")
        if self.compressed_size_bytes < 0:
            raise TurnSequenceError("compressed size must be nonnegative")

    def to_dict(self) -> dict[str, object]:
        return {
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "m3_evidence_digest": self.m3_evidence_digest,
            "m4_reader_contract_id": self.m4_reader_contract_id,
            "m5_semantic_validation_digest": self.m5_semantic_validation_digest,
            "replay_mapping_registry_digest": self.replay_mapping_registry_digest,
            "schema_registry_digest": self.schema_registry_digest,
            "source_catalog_digest": self.source_catalog_digest,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_reader_config_digest": self.source_reader_config_digest,
        }


@dataclass(frozen=True, slots=True)
class TurnSummarySequenceIdentityProjectionV1:
    """Unhashed M8 sequence identity inputs; no public hashed ID is finalized in M7.2."""

    locator: TurnSummarySequenceLocatorV1
    bindings: TurnSequenceBindingsV1
    raw_schema_fingerprint: str
    replay_field_mapping_id: str
    field_suffix_order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.raw_schema_fingerprint):
            raise TurnSequenceError("sequence identity raw fingerprint is malformed")
        if not _REPLAY_MAPPING_ID.fullmatch(self.replay_field_mapping_id):
            raise TurnSequenceError("sequence identity M5 mapping ID is malformed")
        if (
            len(self.field_suffix_order) != 33
            or len(self.field_suffix_order) != len(set(self.field_suffix_order))
            or self.field_suffix_order != tuple(sorted(self.field_suffix_order))
        ):
            raise TurnSequenceError("sequence identity exact suffix surface/order is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            **self.bindings.to_dict(),
            "field_order_contract_id": TURN_SUMMARY_FIELD_ORDER_CONTRACT_ID,
            "field_suffix_order": list(self.field_suffix_order),
            "frame_contract_id": TURN_SUMMARY_FRAME_CONTRACT_ID,
            "locator": self.locator.to_dict(),
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "replay_field_mapping_id": self.replay_field_mapping_id,
            "sequence_contract_id": TURN_SUMMARY_SEQUENCE_CONTRACT_ID,
        }


@dataclass(frozen=True, slots=True)
class TurnSummarySequenceV1:
    locator: TurnSummarySequenceLocatorV1
    raw_schema_fingerprint: str
    replay_field_mapping_id: str
    replay_event_contract_id: str
    bindings: TurnSequenceBindingsV1
    field_suffix_order: tuple[str, ...]
    frames: tuple[TurnSummaryFrameV1, ...]
    source_quality_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.raw_schema_fingerprint):
            raise TurnSequenceError("sequence raw fingerprint is malformed")
        if not _REPLAY_MAPPING_ID.fullmatch(self.replay_field_mapping_id):
            raise TurnSequenceError("sequence M5.1 mapping ID is malformed")
        if self.replay_event_contract_id != REPLAY_EVENT_SCHEMA_ID:
            raise TurnSequenceError("sequence must bind the reviewed M5.1 ReplayEventV1 contract")
        if self.bindings.source_interpretation_contract_id == "":
            raise TurnSequenceError("sequence M3.2 interpretation identity is absent")
        if (
            not self.field_suffix_order
            or len(self.field_suffix_order) != 33
            or len(self.field_suffix_order) != len(set(self.field_suffix_order))
            or self.field_suffix_order != tuple(sorted(self.field_suffix_order))
        ):
            raise TurnSequenceError("sequence suffix order must contain 33 unique exact names")
        for ordinal, frame in enumerate(self.frames):
            if frame.event_ordinal_within_source_record != ordinal:
                raise TurnSequenceError("sequence frame ordinals must be contiguous from zero")
            if len(frame.field_values) != len(self.field_suffix_order):
                raise TurnSequenceError("frame does not cover the exact suffix surface")
            if frame.source_turn_slot_index != ordinal // 2 + 1:
                raise TurnSequenceError("side-local slot index does not reconcile with M5 ordinal")
            if ordinal and frame.source_turn_side == self.frames[ordinal - 1].source_turn_side:
                raise TurnSequenceError("M5 source turn-side order must alternate")

    @property
    def sequence_contract_id(self) -> str:
        return TURN_SUMMARY_SEQUENCE_CONTRACT_ID

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def information_scope(self) -> str:
        return TURN_SEQUENCE_INFORMATION_SCOPE

    @property
    def usable_for_policy_imitation(self) -> bool:
        return False

    @property
    def actor_perspective_safe(self) -> bool:
        return False

    @property
    def identity_projection(self) -> TurnSummarySequenceIdentityProjectionV1:
        return TurnSummarySequenceIdentityProjectionV1(
            self.locator,
            self.bindings,
            self.raw_schema_fingerprint,
            self.replay_field_mapping_id,
            self.field_suffix_order,
        )

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "actor_perspective_safe": False,
            "frame_count": self.frame_count,
            "frames": [frame.to_dict() for frame in self.frames],
            "information_scope": TURN_SEQUENCE_INFORMATION_SCOPE,
            "sequence_identity_projection": self.identity_projection.to_dict(),
            "source_quality_codes": list(self.source_quality_codes),
            "usable_for_policy_imitation": False,
            "view_kind": TURN_SEQUENCE_VIEW_KIND,
        }

    def to_dict(self) -> dict[str, object]:
        return self.semantic_projection_dict()

    @property
    def semantic_digest(self) -> str:
        return _sha256(_canonical_bytes(self.semantic_projection_dict()))


def canonical_suffix_order(mapping: ReplayFieldMappingV1) -> tuple[str, ...]:
    surfaces = {tuple(sorted(family.exact_suffixes)) for family in mapping.turn_slot_families}
    if len(surfaces) != 1 or not surfaces:
        raise TurnSequenceError("M5.1 user/oppo suffix surfaces are not exactly identical")
    suffixes = next(iter(surfaces))
    if len(suffixes) != 33:
        raise TurnSequenceError("reviewed AFR M5.1 field surface must contain 33 suffixes")
    return suffixes


def source_selectors_for_turn_slot(
    mapping: ReplayFieldMappingV1,
    *,
    source_turn_side: str,
    source_turn_slot_index: int,
    field_suffix_order: tuple[str, ...] | None = None,
) -> tuple[tuple[str, int, str], ...]:
    """Resolve frame suffixes to the exact M5 ``(column_index, header_name)`` selectors."""
    if source_turn_side not in {ReplayTurnSideV1.USER.value, ReplayTurnSideV1.OPPO.value}:
        raise TurnSequenceError("frame source side must remain user or oppo")
    if source_turn_slot_index < 1:
        raise TurnSequenceError("frame side-local slot index must be positive")
    suffixes = field_suffix_order or canonical_suffix_order(mapping)
    mappings = [
        item
        for item in mapping.mapped_turn_fields
        if item.event_side == source_turn_side and item.event_slot_index == source_turn_slot_index
    ]
    by_suffix = {item.exact_suffix: item.selector for item in mappings}
    if len(by_suffix) != len(mappings) or by_suffix.keys() != set(suffixes):
        raise TurnSequenceError("M5 slot mapping does not resolve every exact suffix exactly once")
    return tuple(
        (suffix, by_suffix[suffix].column_index, by_suffix[suffix].exact_header_name)
        for suffix in suffixes
    )


def frame_from_replay_event(
    event: ReplayEventV1,
    mapping: ReplayFieldMappingV1,
    *,
    field_suffix_order: tuple[str, ...] | None = None,
) -> TurnSummaryFrameV1:
    """Project one M5.1 event using exact mapping selectors; preserve strings and empties."""
    if event.replay_event_contract_id != REPLAY_EVENT_SCHEMA_ID:
        raise TurnSequenceError("unsupported input; expected M5.1 ReplayEventV1")
    if event.raw_schema_fingerprint != mapping.raw_schema_fingerprint:
        raise TurnSequenceError("ReplayEvent raw fingerprint differs from M5 mapping")
    if event.source_interpretation_contract_id != mapping.source_interpretation_contract_id:
        raise TurnSequenceError("ReplayEvent M3.2 interpretation differs from M5 mapping")
    if event.replay_field_mapping_id != mapping.replay_field_mapping_id:
        raise TurnSequenceError("ReplayEvent mapping ID differs from M5 mapping")
    suffix_order = field_suffix_order or canonical_suffix_order(mapping)
    if suffix_order != canonical_suffix_order(mapping):
        raise TurnSequenceError("requested suffix order differs from exact M5 mapping surface")
    selector_surface = source_selectors_for_turn_slot(
        mapping,
        source_turn_side=event.source_turn_side.value,
        source_turn_slot_index=event.source_turn_slot_index,
        field_suffix_order=suffix_order,
    )
    expected_selectors = {
        (column_index, exact_header_name): suffix
        for suffix, column_index, exact_header_name in selector_surface
    }
    values_by_selector = {
        (item.column_index, item.exact_header_name): item.value for item in event.source_fields
    }
    if len(values_by_selector) != len(event.source_fields):
        raise TurnSequenceError("ReplayEvent has duplicate source field selectors")
    if expected_selectors.keys() != values_by_selector.keys():
        raise TurnSequenceError("ReplayEvent field selectors do not match M5 slot mapping")
    values_by_suffix = {
        expected_selectors[selector]: value for selector, value in values_by_selector.items()
    }
    if values_by_suffix.keys() != set(suffix_order):
        raise TurnSequenceError("ReplayEvent does not provide every exact M5 suffix family")
    return TurnSummaryFrameV1(
        event.locator.event_ordinal_within_source_record,
        event.source_turn_side.value,
        event.source_turn_slot_index,
        tuple(values_by_suffix[suffix] for suffix in suffix_order),
    )


def sequence_from_normalization_result(
    result: ReplayNormalizationResultV1,
    mapping: ReplayFieldMappingV1,
    bindings: TurnSequenceBindingsV1,
    *,
    field_suffix_order: tuple[str, ...] | None = None,
) -> TurnSummarySequenceV1:
    """Create exactly one factored sequence from one M5-normalized Replay source row."""
    if result.rejection is not None:
        raise TurnSequenceError("M5-rejected Replay rows cannot emit a turn sequence")
    if result.replay_normalization_contract_id != "openmtgdata.replay-normalization.v1":
        raise TurnSequenceError("unsupported M5 normalization result contract")
    if (
        mapping.source_interpretation_contract_id != bindings.source_interpretation_contract_id
        or not _REPLAY_MAPPING_ID.fullmatch(mapping.replay_field_mapping_id)
        or bindings.m4_reader_contract_id != "openmtgdata.raw-source-reader.v2"
    ):
        raise TurnSequenceError("sequence binding differs from the exact M3/M4 source authority")
    suffixes = field_suffix_order or canonical_suffix_order(mapping)
    for event in result.events:
        if (
            event.source_archive_id != result.source_archive_id
            or event.locator.data_record_ordinal != result.data_record_ordinal
        ):
            raise TurnSequenceError("M5 event provenance does not match its normalized source row")
    if result.events:
        counts = {event.source_turn_count for event in result.events}
        if len(counts) != 1 or next(iter(counts)) != len(result.events):
            raise TurnSequenceError(
                "M5 emitted frame count does not reconcile with source turn count"
            )
    return TurnSummarySequenceV1(
        TurnSummarySequenceLocatorV1(result.source_archive_id, result.data_record_ordinal),
        mapping.raw_schema_fingerprint,
        mapping.replay_field_mapping_id,
        REPLAY_EVENT_SCHEMA_ID,
        bindings,
        suffixes,
        tuple(
            frame_from_replay_event(event, mapping, field_suffix_order=suffixes)
            for event in result.events
        ),
        tuple(code.value for code in result.row_quality_codes),
    )


@dataclass(frozen=True, slots=True)
class NextTurnPredictionViewV1:
    sequence_identity_projection: TurnSummarySequenceIdentityProjectionV1
    target_event_ordinal: int

    def __post_init__(self) -> None:
        if self.target_event_ordinal < 1:
            raise TurnSequenceError("next-turn v1 does not emit target ordinal zero")

    @property
    def view_locator(self) -> NextTurnPredictionViewLocatorV1:
        return NextTurnPredictionViewLocatorV1(
            self.sequence_identity_projection.locator.source_archive_id,
            self.sequence_identity_projection.locator.data_record_ordinal,
            self.target_event_ordinal,
        )

    def semantic_identity_projection_dict(self) -> dict[str, object]:
        """Unhashed M8 view identity inputs; no full-sequence content or runtime fields."""
        return {
            "objective_id": NEXT_TURN_OBJECTIVE_ID,
            "sequence_identity_projection": self.sequence_identity_projection.to_dict(),
            "sequence_identity_scope": "source_sequence_provenance_projection",
            "sequence_contract_id": TURN_SUMMARY_SEQUENCE_CONTRACT_ID,
            "target_event_ordinal": self.target_event_ordinal,
            "view_contract_id": NEXT_TURN_PREDICTION_VIEW_CONTRACT_ID,
            "view_locator": self.view_locator.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.semantic_identity_projection_dict(),
            "context_end_event_ordinal_exclusive": self.target_event_ordinal,
            "context_start_event_ordinal": 0,
            "information_scope": TURN_SEQUENCE_INFORMATION_SCOPE,
            "target_kind": "next_source_turn_summary",
            "usable_for_policy_imitation": False,
            "actor_perspective_safe": False,
        }

    def materialize_model_projection(self, sequence: TurnSummarySequenceV1) -> dict[str, object]:
        """Materialize only prior frames plus the target frame; never later frames."""
        if sequence.identity_projection != self.sequence_identity_projection:
            raise TurnSequenceError("view sequence identity differs from supplied sequence")
        target = self.target_event_ordinal
        if target >= len(sequence.frames):
            raise TurnSequenceError("view target ordinal is outside sequence frames")
        return {
            "metadata": {
                "actor_perspective_safe": False,
                "information_scope": TURN_SEQUENCE_INFORMATION_SCOPE,
                "objective_id": NEXT_TURN_OBJECTIVE_ID,
                "usable_for_policy_imitation": False,
                "view_kind": TURN_SEQUENCE_VIEW_KIND,
            },
            "model_projection": {
                "context": [
                    frame.model_projection_dict(sequence.field_suffix_order)
                    for frame in sequence.frames[:target]
                ],
                "target_frame": sequence.frames[target].model_projection_dict(
                    sequence.field_suffix_order
                ),
                "view_contract_id": NEXT_TURN_PREDICTION_VIEW_CONTRACT_ID,
            },
        }


def iter_next_turn_views(
    sequence: TurnSummarySequenceV1,
) -> Iterator[NextTurnPredictionViewV1]:
    """Lazily expose N-1 descriptor views without copying any context prefixes."""
    for target_ordinal in range(1, sequence.frame_count):
        yield NextTurnPredictionViewV1(sequence.identity_projection, target_ordinal)


def materialize_next_turn_view(
    sequence: TurnSummarySequenceV1,
    view: NextTurnPredictionViewV1,
) -> dict[str, object]:
    return view.materialize_model_projection(sequence)


class TurnSequenceLogicalDigestV1:
    """Incremental digest over canonical logical sequence records, independent of shards."""

    def __init__(self, input_bindings: dict[str, object]) -> None:
        self._hasher = hashlib.sha256()
        domain = _canonical_bytes(
            {
                "digest_contract_id": TURN_SEQUENCE_LOGICAL_DIGEST_CONTRACT_ID,
                "input_bindings": input_bindings,
                "purpose": "logical-turn-summary-sequence-stream",
            }
        )
        self._hasher.update(len(domain).to_bytes(8, "big"))
        self._hasher.update(domain)
        self.sequence_count = 0
        self.frame_count = 0

    def update(self, sequence: TurnSummarySequenceV1) -> None:
        payload = _canonical_bytes(sequence.semantic_projection_dict())
        self._hasher.update(len(payload).to_bytes(8, "big"))
        self._hasher.update(payload)
        self.sequence_count += 1
        self.frame_count += sequence.frame_count

    @property
    def hexdigest(self) -> str:
        return self._hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class TurnSequenceBuildReportV1:
    source_catalog_digest: str
    m3_evidence_digest: str
    schema_registry_digest: str
    source_archive_id: str
    compressed_sha256: str
    compressed_size_bytes: int
    raw_schema_fingerprint: str
    source_interpretation_contract_id: str
    m4_reader_contract_id: str
    source_reader_config_digest: str
    m5_event_contract_id: str
    replay_field_mapping_id: str
    replay_mapping_registry_digest: str
    m5_semantic_validation_digest: str
    frame_contract_id: str
    sequence_contract_id: str
    next_turn_view_contract_id: str
    m4_completion_status: str
    m4_compressed_bytes_verified: int
    m4_records_seen: int
    m4_records_accepted: int
    m4_records_rejected: int
    m4_diagnostic_counts: tuple[tuple[str, int], ...]
    m5_records_submitted: int
    m5_records_normalized: int
    m5_records_rejected: int
    m5_rejection_counts: tuple[tuple[str, int], ...]
    sequences_emitted: int
    frames_emitted: int
    sequence_length_histogram: tuple[tuple[int, int], ...]
    minimum_sequence_length: int | None
    maximum_sequence_length: int | None
    source_side_frame_counts: tuple[tuple[str, int], ...]
    virtual_next_turn_views: int
    sequence_rejection_counts: tuple[tuple[str, int], ...]
    m4_gzip_integrity_status: str
    logical_sequence_digest: str
    report_digest: str

    def semantic_projection_dict(self) -> dict[str, object]:
        return {
            "compressed_sha256": self.compressed_sha256,
            "compressed_size_bytes": self.compressed_size_bytes,
            "frame_contract_id": self.frame_contract_id,
            "frames_emitted": self.frames_emitted,
            "m3_evidence_digest": self.m3_evidence_digest,
            "m4_completion_status": self.m4_completion_status,
            "m4_compressed_bytes_verified": self.m4_compressed_bytes_verified,
            "m4_diagnostic_counts": dict(self.m4_diagnostic_counts),
            "m4_reader_contract_id": self.m4_reader_contract_id,
            "m4_records_accepted": self.m4_records_accepted,
            "m4_records_rejected": self.m4_records_rejected,
            "m4_records_seen": self.m4_records_seen,
            "m4_gzip_integrity_status": self.m4_gzip_integrity_status,
            "m5_event_contract_id": self.m5_event_contract_id,
            "m5_records_normalized": self.m5_records_normalized,
            "m5_records_rejected": self.m5_records_rejected,
            "m5_records_submitted": self.m5_records_submitted,
            "m5_rejection_counts": dict(self.m5_rejection_counts),
            "m5_semantic_validation_digest": self.m5_semantic_validation_digest,
            "maximum_sequence_length": self.maximum_sequence_length,
            "minimum_sequence_length": self.minimum_sequence_length,
            "next_turn_view_contract_id": self.next_turn_view_contract_id,
            "raw_schema_fingerprint": self.raw_schema_fingerprint,
            "replay_field_mapping_id": self.replay_field_mapping_id,
            "replay_mapping_registry_digest": self.replay_mapping_registry_digest,
            "schema_registry_digest": self.schema_registry_digest,
            "sequence_contract_id": self.sequence_contract_id,
            "sequence_length_histogram": [
                {"frame_count": length, "sequence_count": count}
                for length, count in self.sequence_length_histogram
            ],
            "sequence_rejection_counts": dict(self.sequence_rejection_counts),
            "sequences_emitted": self.sequences_emitted,
            "source_archive_id": self.source_archive_id,
            "source_catalog_digest": self.source_catalog_digest,
            "source_interpretation_contract_id": self.source_interpretation_contract_id,
            "source_reader_config_digest": self.source_reader_config_digest,
            "source_side_frame_counts": dict(self.source_side_frame_counts),
            "turn_sequence_build_report_contract_id": TURN_SEQUENCE_BUILD_REPORT_CONTRACT_ID,
            "virtual_next_turn_views": self.virtual_next_turn_views,
            "logical_sequence_digest": self.logical_sequence_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "build_report": self.semantic_projection_dict(),
            "build_digest_contract_id": TURN_SEQUENCE_BUILD_DIGEST_CONTRACT_ID,
            "build_report_digest": self.report_digest,
        }


def validate_build_report(report: TurnSequenceBuildReportV1) -> None:
    for digest in (
        report.source_catalog_digest,
        report.m3_evidence_digest,
        report.schema_registry_digest,
        report.source_archive_id,
        report.compressed_sha256,
        report.raw_schema_fingerprint,
        report.source_reader_config_digest,
        report.replay_mapping_registry_digest,
        report.m5_semantic_validation_digest,
        report.logical_sequence_digest,
        report.report_digest,
    ):
        if not _SHA256.fullmatch(digest):
            raise TurnSequenceError("build report contains a malformed SHA-256 identity")
    if report.m4_reader_contract_id != "openmtgdata.raw-source-reader.v2":
        raise TurnSequenceError("build report does not bind M4-v2")
    if report.m5_event_contract_id != REPLAY_EVENT_SCHEMA_ID:
        raise TurnSequenceError("build report does not bind the accepted M5.1 event contract")
    if not _REPLAY_MAPPING_ID.fullmatch(report.replay_field_mapping_id):
        raise TurnSequenceError("build report M5 mapping ID is malformed")
    if report.m4_completion_status != CompletionStatus.COMPLETE.value:
        raise TurnSequenceError("sequence corpus requires terminal M4 source verification")
    if report.m4_gzip_integrity_status != "valid" or (
        report.m4_compressed_bytes_verified != report.compressed_size_bytes
    ):
        raise TurnSequenceError("sequence corpus requires successful M4 gzip/container integrity")
    if sum(count for _code, count in report.m4_diagnostic_counts) != report.m4_records_rejected:
        raise TurnSequenceError("M4 diagnostic counts do not reconcile with rejected rows")
    if any(
        code not in {"ROW_WIDTH_SHORTER", "ROW_WIDTH_LONGER"}
        for code, _count in report.m4_diagnostic_counts
    ):
        raise TurnSequenceError("completed sequence source has unexpected M4 diagnostics")
    if report.m4_records_seen != report.m4_records_accepted + report.m4_records_rejected:
        raise TurnSequenceError("M4 full-stream counts do not reconcile")
    if report.m5_records_submitted != report.m4_records_accepted:
        raise TurnSequenceError("M5 submission count differs from accepted M4 population")
    if report.m5_records_normalized + report.m5_records_rejected != report.m5_records_submitted:
        raise TurnSequenceError("M5 normalization counts do not reconcile")
    if sum(count for _code, count in report.m5_rejection_counts) != report.m5_records_rejected:
        raise TurnSequenceError("M5 typed rejection counts do not reconcile")
    if report.sequence_rejection_counts:
        raise TurnSequenceError("sequence rejection is fatal to the v1 full-stream build")
    if report.sequences_emitted != report.m5_records_normalized:
        raise TurnSequenceError("each normalized source row must produce exactly one sequence")
    if (
        sum(count for _length, count in report.sequence_length_histogram)
        != report.sequences_emitted
    ):
        raise TurnSequenceError("sequence length histogram does not reconcile with sequence count")
    if sum(count for _side, count in report.source_side_frame_counts) != report.frames_emitted:
        raise TurnSequenceError("source-side frame counts do not reconcile")
    if any(side not in {"user", "oppo"} for side, _count in report.source_side_frame_counts):
        raise TurnSequenceError("source-side frame counts contain an unknown side")
    if report.frames_emitted != sum(
        length * count for length, count in report.sequence_length_histogram
    ):
        raise TurnSequenceError("sequence length histogram does not reconcile with frame count")
    if report.virtual_next_turn_views != sum(
        max(length - 1, 0) * count for length, count in report.sequence_length_histogram
    ):
        raise TurnSequenceError("virtual view count must equal sum(max(sequence length - 1, 0))")
    if report.sequence_length_histogram:
        lengths = [length for length, count in report.sequence_length_histogram if count]
        if report.minimum_sequence_length != min(lengths) or report.maximum_sequence_length != max(
            lengths
        ):
            raise TurnSequenceError("minimum/maximum sequence lengths do not match histogram")
    elif report.minimum_sequence_length is not None or report.maximum_sequence_length is not None:
        raise TurnSequenceError("empty sequence histogram cannot declare min/max lengths")
    if report.report_digest != _sha256(
        _canonical_bytes(
            {
                "digest_contract_id": TURN_SEQUENCE_BUILD_DIGEST_CONTRACT_ID,
                "build_report": report.semantic_projection_dict(),
            }
        )
    ):
        raise TurnSequenceError("turn sequence build report digest mismatch")


def verify_sequence_shard_directory(
    directory: Path,
    report: TurnSequenceBuildReportV1,
) -> dict[str, object]:
    """Stream-verify compressed JSONL sequences, order, safety allowlist, and logical digest."""
    paths = sorted(directory.glob("part-*.jsonl.gz"), key=lambda path: path.name)
    if not paths:
        raise TurnSequenceError("sequence artifact directory contains no gzip JSONL shards")
    expected_names = [f"part-{index:05d}.jsonl.gz" for index in range(len(paths))]
    if [path.name for path in paths] != expected_names:
        raise TurnSequenceError("sequence shard ordinals are missing, duplicated, or unordered")
    identity = {
        "compressed_sha256": report.compressed_sha256,
        "compressed_size_bytes": report.compressed_size_bytes,
        "m4_reader_contract_id": report.m4_reader_contract_id,
        "m3_evidence_digest": report.m3_evidence_digest,
        "m5_semantic_validation_digest": report.m5_semantic_validation_digest,
        "replay_mapping_registry_digest": report.replay_mapping_registry_digest,
        "schema_registry_digest": report.schema_registry_digest,
        "source_catalog_digest": report.source_catalog_digest,
        "source_interpretation_contract_id": report.source_interpretation_contract_id,
        "source_reader_config_digest": report.source_reader_config_digest,
    }
    domain = _canonical_bytes(
        {
            "digest_contract_id": TURN_SEQUENCE_LOGICAL_DIGEST_CONTRACT_ID,
            "input_bindings": {
                **identity,
                "frame_contract_id": report.frame_contract_id,
                "m5_event_contract_id": report.m5_event_contract_id,
                "replay_field_mapping_id": report.replay_field_mapping_id,
                "sequence_contract_id": report.sequence_contract_id,
                "source_archive_id": report.source_archive_id,
            },
            "purpose": "logical-turn-summary-sequence-stream",
        }
    )
    hasher = hashlib.sha256()
    hasher.update(len(domain).to_bytes(8, "big"))
    hasher.update(domain)
    sequence_count = frame_count = views = 0
    side_counts: Counter[str] = Counter()
    length_counts: Counter[int] = Counter()
    previous_row = 0
    forbidden_keys = {
        "source_turn_count",
        "source_turn_count_raw",
        "source_on_play",
        "source_on_play_raw",
        "won",
    }
    for path in paths:
        with gzip.open(path, "rb") as stream:
            for raw_line in stream:
                if not raw_line.endswith(b"\n"):
                    raise TurnSequenceError("sequence JSONL shard has an unterminated record")
                try:
                    document = json.loads(raw_line)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise TurnSequenceError("sequence JSONL record is invalid") from exc
                if not isinstance(document, dict):
                    raise TurnSequenceError("sequence JSONL record must be a JSON object")
                _reject_model_global_fields(document, forbidden_keys)
                projection_bytes = _canonical_bytes(document)
                hasher.update(len(projection_bytes).to_bytes(8, "big"))
                hasher.update(projection_bytes)
                try:
                    projection = document["sequence_identity_projection"]
                    locator = projection["locator"]
                    row_ordinal = int(locator["data_record_ordinal"])
                    archive_id = str(locator["source_archive_id"])
                    suffix_order = projection["field_suffix_order"]
                    frames = document["frames"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise TurnSequenceError(
                        "sequence record identity/projection is malformed"
                    ) from exc
                if (
                    archive_id != report.source_archive_id
                    or row_ordinal <= previous_row
                    or projection.get("raw_schema_fingerprint") != report.raw_schema_fingerprint
                    or projection.get("replay_field_mapping_id") != report.replay_field_mapping_id
                    or projection.get("source_interpretation_contract_id")
                    != report.source_interpretation_contract_id
                    or projection.get("source_reader_config_digest")
                    != report.source_reader_config_digest
                    or projection.get("field_order_contract_id")
                    != TURN_SUMMARY_FIELD_ORDER_CONTRACT_ID
                    or not isinstance(suffix_order, list)
                    or len(suffix_order) != 33
                    or len(suffix_order) != len(set(suffix_order))
                    or not isinstance(frames, list)
                    or document.get("frame_count") != len(frames)
                ):
                    raise TurnSequenceError("sequence record identity/order does not match build")
                previous_row = row_ordinal
                if document.get("usable_for_policy_imitation") is not False or (
                    document.get("actor_perspective_safe") is not False
                    or document.get("information_scope") != TURN_SEQUENCE_INFORMATION_SCOPE
                ):
                    raise TurnSequenceError("sequence artifact is mislabeled as policy-safe")
                for ordinal, frame in enumerate(frames):
                    if (
                        not isinstance(frame, dict)
                        or frame.get("event_ordinal_within_source_record") != ordinal
                        or frame.get("source_turn_slot_index") != ordinal // 2 + 1
                        or frame.get("source_turn_side") not in {"user", "oppo"}
                        or not isinstance(frame.get("field_values"), list)
                        or len(frame["field_values"]) != 33
                        or any(not isinstance(value, str) for value in frame["field_values"])
                    ):
                        raise TurnSequenceError(
                            "sequence frame order/surface/value types are invalid"
                        )
                    if (
                        ordinal
                        and frame["source_turn_side"] == frames[ordinal - 1]["source_turn_side"]
                    ):
                        raise TurnSequenceError("sequence source sides do not alternate")
                    side_counts[frame["source_turn_side"]] += 1
                length = len(frames)
                sequence_count += 1
                frame_count += length
                length_counts[length] += 1
                views += max(length - 1, 0)
    if (
        sequence_count != report.sequences_emitted
        or frame_count != report.frames_emitted
        or views != report.virtual_next_turn_views
        or tuple(sorted(side_counts.items())) != report.source_side_frame_counts
        or tuple(sorted(length_counts.items())) != report.sequence_length_histogram
        or hasher.hexdigest() != report.logical_sequence_digest
    ):
        raise TurnSequenceError("streamed sequence shards do not match logical build report")
    return {
        "sequence_count": sequence_count,
        "frame_count": frame_count,
        "virtual_next_turn_views": views,
        "source_side_frame_counts": dict(sorted(side_counts.items())),
        "sequence_length_histogram": dict(sorted(length_counts.items())),
        "logical_sequence_digest": hasher.hexdigest(),
        "shard_count": len(paths),
    }


def _reject_model_global_fields(value: object, forbidden_keys: set[str]) -> None:
    """Recursively reject row-global fields even if accidentally added under a nested object."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in forbidden_keys or key_text.startswith(("user_total_", "oppo_total_")):
                raise TurnSequenceError(
                    f"row-global field {key_text!r} entered sequence projection"
                )
            _reject_model_global_fields(child, forbidden_keys)
    elif isinstance(value, list):
        for child in value:
            _reject_model_global_fields(child, forbidden_keys)


class TurnSequenceBuildAccumulatorV1:
    """Bounded counters plus streaming logical digest for one complete source archive."""

    def __init__(
        self,
        *,
        bindings: TurnSequenceBindingsV1,
        source_archive_id: str,
        compressed_sha256: str,
        compressed_size_bytes: int,
        raw_schema_fingerprint: str,
        replay_field_mapping_id: str,
        replay_mapping_registry_digest: str,
        m5_semantic_validation_digest: str,
        reader_config: SourceReaderConfigV1,
        m5_review: ReplayMappingReviewV1,
    ) -> None:
        if (
            not _SHA256.fullmatch(source_archive_id)
            or not _SHA256.fullmatch(compressed_sha256)
            or not _SHA256.fullmatch(raw_schema_fingerprint)
        ):
            raise TurnSequenceError("build accumulator source identity is malformed")
        if m5_review.source_archive_id != source_archive_id:
            raise TurnSequenceError("M5 full-stream review refers to another archive")
        if (
            m5_review.raw_schema_fingerprint != raw_schema_fingerprint
            or m5_review.evidence_scope != "m4-full-stream-verified"
            or m5_review.m4_reader_completion_status != CompletionStatus.COMPLETE.value
            or m5_review.m4_records_seen
            != m5_review.m4_records_accepted + m5_review.m4_width_rejected_records
            or m5_review.m5_records_submitted != m5_review.m4_records_accepted
            or m5_review.m5_records_normalized + m5_review.m5_records_rejected
            != m5_review.m5_records_submitted
            or sum(count for _turns, count in m5_review.turn_count_histogram)
            != m5_review.m5_records_normalized
            or sum(turns * count for turns, count in m5_review.turn_count_histogram)
            != m5_review.event_count
            or sum(count for _side, count in m5_review.event_side_counts) != m5_review.event_count
        ):
            raise TurnSequenceError(
                "sequence build requires matching complete M5 full-stream review"
            )
        if (
            bindings.compressed_sha256 != compressed_sha256
            or bindings.compressed_size_bytes != compressed_size_bytes
            or bindings.replay_mapping_registry_digest != replay_mapping_registry_digest
            or bindings.m5_semantic_validation_digest != m5_semantic_validation_digest
        ):
            raise TurnSequenceError("build inputs differ from immutable sequence bindings")
        self.bindings = bindings
        self.source_archive_id = source_archive_id
        self.compressed_sha256 = compressed_sha256
        self.compressed_size_bytes = compressed_size_bytes
        self.raw_schema_fingerprint = raw_schema_fingerprint
        self.replay_field_mapping_id = replay_field_mapping_id
        self.replay_mapping_registry_digest = replay_mapping_registry_digest
        self.m5_semantic_validation_digest = m5_semantic_validation_digest
        self.reader_config = reader_config
        self.m5_review = m5_review
        self.m4_seen = self.m4_accepted = self.m4_rejected = 0
        self.m4_diagnostics: Counter[str] = Counter()
        self.m4_batches_seen = 0
        self.last_m4_data_record_ordinal = 0
        self.m5_submitted = self.m5_normalized = self.m5_rejected = 0
        self.m5_rejections: Counter[str] = Counter()
        self.last_m5_data_record_ordinal = 0
        self.sequence_lengths: Counter[int] = Counter()
        self.side_frames: Counter[str] = Counter()
        self.sequence_rejections: Counter[str] = Counter()
        self.sequences = 0
        self.frames = 0
        config_identity = source_reader_config_identity(reader_config)
        if config_identity.source_reader_config_digest != bindings.source_reader_config_digest:
            raise TurnSequenceError("M4 reader configuration differs from sequence bindings")
        self.source_reader_config_digest = config_identity.source_reader_config_digest
        self.logical_digest = TurnSequenceLogicalDigestV1(
            {
                **bindings.to_dict(),
                "frame_contract_id": TURN_SUMMARY_FRAME_CONTRACT_ID,
                "m5_event_contract_id": REPLAY_EVENT_SCHEMA_ID,
                "replay_field_mapping_id": replay_field_mapping_id,
                "sequence_contract_id": TURN_SUMMARY_SEQUENCE_CONTRACT_ID,
                "source_archive_id": source_archive_id,
                "source_reader_config_digest": self.source_reader_config_digest,
            }
        )

    def observe_batch(self, batch: RawCsvBatchV1) -> None:
        if batch.batch_ordinal != self.m4_batches_seen + 1:
            raise TurnSequenceError("M4 batches must be consumed in contiguous source order")
        if batch.records_seen != batch.records_accepted + batch.records_rejected:
            raise TurnSequenceError("M4 batch record counts do not reconcile")
        if len(batch.accepted_records) != batch.records_accepted:
            raise TurnSequenceError("M4 batch accepted rows do not reconcile")
        if len(batch.record_diagnostics) != batch.records_rejected:
            raise TurnSequenceError("M4 batch reject diagnostics do not reconcile")
        if batch.reader_contract_id != self.bindings.m4_reader_contract_id:
            raise TurnSequenceError("M4 batch reader contract differs from build binding")
        if (
            batch.source_archive_id != self.source_archive_id
            or batch.raw_schema_fingerprint != self.raw_schema_fingerprint
            or batch.source_interpretation_contract_id
            != self.bindings.source_interpretation_contract_id
        ):
            raise TurnSequenceError("M4 batch source/schema identity differs from build binding")
        ordinals = [record.data_record_ordinal for record in batch.accepted_records]
        for record in batch.accepted_records:
            if (
                record.raw_csv_record_contract_id != "openmtgdata.raw-csv-record.v1"
                or record.reader_contract_id != self.bindings.m4_reader_contract_id
                or record.source_archive_id != self.source_archive_id
                or record.raw_schema_fingerprint != self.raw_schema_fingerprint
                or record.source_interpretation_contract_id
                != self.bindings.source_interpretation_contract_id
            ):
                raise TurnSequenceError("M4 accepted record authority differs from the batch")
        for diagnostic in batch.record_diagnostics:
            if (
                diagnostic.source_archive_id != self.source_archive_id
                or diagnostic.data_record_ordinal is None
                or diagnostic.code.value not in {"ROW_WIDTH_SHORTER", "ROW_WIDTH_LONGER"}
            ):
                raise TurnSequenceError("M4 sequence input has an unsupported row diagnostic")
            ordinals.append(diagnostic.data_record_ordinal)
        expected_ordinals = list(
            range(
                self.last_m4_data_record_ordinal + 1,
                self.last_m4_data_record_ordinal + batch.records_seen + 1,
            )
        )
        if sorted(ordinals) != expected_ordinals:
            raise TurnSequenceError("M4 batch locators contain a gap, duplicate, or reordered row")
        if batch.records_seen and (
            batch.first_data_record_ordinal != expected_ordinals[0]
            or batch.last_data_record_ordinal != expected_ordinals[-1]
        ):
            raise TurnSequenceError("M4 batch ordinal bounds disagree with its record locators")
        self.m4_seen += batch.records_seen
        self.m4_accepted += batch.records_accepted
        self.m4_rejected += batch.records_rejected
        for diagnostic in batch.record_diagnostics:
            self.m4_diagnostics[diagnostic.code.value] += 1
        self.m4_batches_seen += 1
        self.last_m4_data_record_ordinal += batch.records_seen

    def observe_normalization(
        self,
        result: ReplayNormalizationResultV1,
        sequence: TurnSummarySequenceV1 | None,
    ) -> None:
        if result.data_record_ordinal <= self.last_m5_data_record_ordinal:
            raise TurnSequenceError("M5 rows must preserve strictly increasing source locators")
        self.last_m5_data_record_ordinal = result.data_record_ordinal
        self.m5_submitted += 1
        if result.rejection is not None:
            self.m5_rejected += 1
            self.m5_rejections[result.rejection.quality_code.value] += 1
            if sequence is not None:
                raise TurnSequenceError("M5-rejected row unexpectedly produced a sequence")
            return
        if sequence is None:
            raise TurnSequenceError("M5-normalized source row did not produce a sequence")
        if sequence.locator.source_archive_id != result.source_archive_id or (
            sequence.locator.data_record_ordinal != result.data_record_ordinal
        ):
            raise TurnSequenceError("sequence locator differs from its M5 source row")
        if sequence.frame_count != len(result.events):
            raise TurnSequenceError("sequence frame count differs from M5 normalized event count")
        self.m5_normalized += 1
        self.sequences += 1
        self.frames += sequence.frame_count
        self.sequence_lengths[sequence.frame_count] += 1
        for frame in sequence.frames:
            self.side_frames[frame.source_turn_side] += 1
        self.logical_digest.update(sequence)

    def finish(self, reader_summary: SourceReaderSummaryV1) -> TurnSequenceBuildReportV1:
        review = self.m5_review
        if reader_summary.completion_status is not CompletionStatus.COMPLETE:
            raise TurnSequenceError("full sequence artifact requires completed M4 EOF/integrity")
        if reader_summary.gzip_integrity_status != "valid":
            raise TurnSequenceError("M4 gzip/container integrity did not complete successfully")
        if reader_summary.compressed_bytes_verified != self.compressed_size_bytes:
            raise TurnSequenceError("M4 did not verify the registered compressed byte count")
        if (
            reader_summary.source_archive_id != self.source_archive_id
            or reader_summary.compressed_sha256 != self.compressed_sha256
        ):
            raise TurnSequenceError("M4 terminal archive identity differs from registered source")
        if (
            reader_summary.raw_schema_fingerprint != self.raw_schema_fingerprint
            or reader_summary.source_interpretation_contract_id
            != self.bindings.source_interpretation_contract_id
            or reader_summary.reader_contract_id != self.bindings.m4_reader_contract_id
            or reader_summary.schema_registry_digest != self.bindings.schema_registry_digest
        ):
            raise TurnSequenceError("M4 terminal summary schema authority differs from build")
        if reader_summary.compressed_size_bytes != self.compressed_size_bytes:
            raise TurnSequenceError("registered compressed size differs from M4 terminal summary")
        if (
            self.m4_seen != reader_summary.records_seen
            or self.m4_accepted != reader_summary.records_accepted
            or self.m4_rejected != reader_summary.records_rejected
            or self.m4_seen != self.m4_accepted + self.m4_rejected
            or dict(self.m4_diagnostics) != dict(reader_summary.diagnostic_counts)
        ):
            raise TurnSequenceError(
                "streamed M4 batch counts do not reconcile with terminal summary"
            )
        if any(
            code not in {"ROW_WIDTH_SHORTER", "ROW_WIDTH_LONGER"}
            for code, _count in reader_summary.diagnostic_counts
        ):
            raise TurnSequenceError("unexpected M4 record diagnostic in supported sequence build")
        if self.m4_seen != review.m4_records_seen or (
            self.m4_accepted != review.m4_records_accepted
            or self.m4_rejected != review.m4_width_rejected_records
        ):
            raise TurnSequenceError("new M4 full-stream counts differ from accepted M5 review")
        if (
            self.m5_submitted != review.m5_records_submitted
            or self.m5_normalized != review.m5_records_normalized
            or self.m5_rejected != review.m5_records_rejected
        ):
            raise TurnSequenceError("new M5 normalization counts differ from accepted review")
        if self.m5_submitted != self.m4_accepted:
            raise TurnSequenceError("every accepted M4 row must be submitted to M5")
        if sum(self.m4_diagnostics.values()) != self.m4_rejected:
            raise TurnSequenceError("M4 rejected row diagnostics do not reconcile")
        if sum(self.m5_rejections.values()) != self.m5_rejected:
            raise TurnSequenceError("M5 typed rejection counts do not reconcile")
        expected_histogram = dict(review.turn_count_histogram)
        if dict(self.sequence_lengths) != expected_histogram:
            raise TurnSequenceError("full-stream sequence lengths differ from M5 turn histogram")
        if self.frames != review.event_count or dict(self.side_frames) != dict(
            review.event_side_counts
        ):
            raise TurnSequenceError("full-stream frame/side totals differ from M5 review")
        virtual_views = sum(
            max(length - 1, 0) * count for length, count in self.sequence_lengths.items()
        )
        logical_digest = self.logical_digest.hexdigest
        provisional = TurnSequenceBuildReportV1(
            self.bindings.source_catalog_digest,
            self.bindings.m3_evidence_digest,
            self.bindings.schema_registry_digest,
            self.source_archive_id,
            reader_summary.compressed_sha256,
            self.compressed_size_bytes,
            self.raw_schema_fingerprint,
            self.bindings.source_interpretation_contract_id,
            reader_summary.reader_contract_id,
            self.source_reader_config_digest,
            REPLAY_EVENT_SCHEMA_ID,
            self.replay_field_mapping_id,
            self.replay_mapping_registry_digest,
            self.m5_semantic_validation_digest,
            TURN_SUMMARY_FRAME_CONTRACT_ID,
            TURN_SUMMARY_SEQUENCE_CONTRACT_ID,
            NEXT_TURN_PREDICTION_VIEW_CONTRACT_ID,
            reader_summary.completion_status.value,
            reader_summary.compressed_bytes_verified,
            self.m4_seen,
            self.m4_accepted,
            self.m4_rejected,
            reader_summary.diagnostic_counts,
            self.m5_submitted,
            self.m5_normalized,
            self.m5_rejected,
            tuple(sorted(self.m5_rejections.items())),
            self.sequences,
            self.frames,
            tuple(sorted(self.sequence_lengths.items())),
            min(self.sequence_lengths) if self.sequence_lengths else None,
            max(self.sequence_lengths) if self.sequence_lengths else None,
            tuple(sorted(self.side_frames.items())),
            virtual_views,
            tuple(sorted(self.sequence_rejections.items())),
            reader_summary.gzip_integrity_status,
            logical_digest,
            "",
        )
        report = replace(
            provisional,
            report_digest=_sha256(
                _canonical_bytes(
                    {
                        "digest_contract_id": TURN_SEQUENCE_BUILD_DIGEST_CONTRACT_ID,
                        "build_report": provisional.semantic_projection_dict(),
                    }
                )
            ),
        )
        validate_build_report(report)
        return report
