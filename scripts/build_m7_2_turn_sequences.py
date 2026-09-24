"""Build the bounded-memory AFR PremierDraft source turn-summary sequences.

This emits a local sequence-prediction intermediate, not DecisionSampleV1,
policy observations, Parquet, or a release artifact.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
from typing import BinaryIO

from openmtgdata.archive_registration import register_archive
from openmtgdata.config import RuntimeConfig
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.replay_adapter import ReplayAdapterV1
from openmtgdata.replay_schema import (
    ReplayMappingReviewV1,
    build_replay_field_analysis,
    build_replay_mapping_registry,
    build_turn_slot_mapping,
)
from openmtgdata.resumable_reader import source_reader_config_identity
from openmtgdata.schema_evolution import load_verified_m3_evidence
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    SourceReaderConfigV1,
    load_verified_schema_registry,
    open_source_reader,
)
from openmtgdata.turn_sequence import (
    TurnSequenceBindingsV1,
    TurnSequenceBuildAccumulatorV1,
    TurnSequenceError,
    sequence_from_normalization_result,
    validate_build_report,
)

SOURCE_CATALOG_DIGEST = "dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602"
M3_EVIDENCE_DIGEST = "c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29"
M3_SCHEMA_REGISTRY_DIGEST = "66d58e20fa2adbcbfdcfd927c1074b5807d43af8af94103b61b26fc06f13661c"
M5_MAPPING_REGISTRY_DIGEST = "43be7080894b55c60f58d569ed8cfeb52a9dbae1fa8688160d91196044ab47f3"
M5_SEMANTIC_VALIDATION_DIGEST = "24db9aba56d48cbf1403a71f128fa870e065df850b9d93db3efdeeac67dbfe08"
SOURCE_ARCHIVE_ID = "b739dacc3082d356b27001aa6fd91257ae766a8d1aec23272e656610cfe3b54b"
SOURCE_RAW_FINGERPRINT = "b4ad0cb27197cf3e37e59d6609a6098da0e0e2f6c4f98b82c45f85fcb7bbea75"
SOURCE_M5_MAPPING_ID = (
    "openmtgdata.replay-field-mapping.v1:"
    "a42f925293705fb89936e3b3b5a6d243027d63953808ccfb154920c0a6031140"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8", errors="strict"
    )


class _SequenceShardWriter:
    """Write each logical sequence once, with deterministic gzip metadata."""

    def __init__(self, staging_directory: Path, *, sequences_per_shard: int) -> None:
        if sequences_per_shard < 1:
            raise ValueError("sequences_per_shard must be positive")
        self.staging_directory = staging_directory
        self.sequences_per_shard = sequences_per_shard
        self.shard_ordinal = -1
        self.shard_sequence_count = 0
        self.shard_frame_count = 0
        self._raw: BinaryIO | None = None
        self._gzip: gzip.GzipFile | None = None
        self._current_path: Path | None = None
        self.shard_count = 0
        self.sequences_written = 0
        self.frames_written = 0

    def _open_shard(self) -> None:
        self.shard_ordinal += 1
        self.shard_count += 1
        self.shard_sequence_count = 0
        self.shard_frame_count = 0
        self._current_path = self.staging_directory / f"part-{self.shard_ordinal:05d}.jsonl.gz"
        self._raw = self._current_path.open("xb")
        self._gzip = gzip.GzipFile(
            filename="", mode="wb", compresslevel=6, fileobj=self._raw, mtime=0
        )

    def write(self, sequence) -> None:
        if self._gzip is None:
            self._open_shard()
        assert self._gzip is not None
        payload = _canonical_json(sequence.to_dict()) + b"\n"
        self._gzip.write(payload)
        self.shard_sequence_count += 1
        self.shard_frame_count += sequence.frame_count
        self.sequences_written += 1
        self.frames_written += sequence.frame_count
        if self.shard_sequence_count >= self.sequences_per_shard:
            self._close_shard()

    def _close_shard(self) -> None:
        if self._gzip is None or self._raw is None or self._current_path is None:
            return
        self._gzip.close()
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()
        self._gzip = None
        self._raw = None
        self._current_path = None

    def close(self) -> None:
        self._close_shard()


def _load_review(document: dict[str, object]) -> ReplayMappingReviewV1:
    raw = document["review_evidence"]
    if not isinstance(raw, dict):
        raise TurnSequenceError("supported M5.1 mapping has no full-stream review")
    histogram_raw = raw["turn_count_histogram"]
    side_counts_raw = raw["event_side_counts"]
    if not isinstance(histogram_raw, list) or not isinstance(side_counts_raw, dict):
        raise TurnSequenceError("M5.1 review histogram/side counts are malformed")
    return ReplayMappingReviewV1(
        str(raw["raw_schema_fingerprint"]),
        str(raw["source_archive_id"]),
        str(raw["m4_reader_completion_status"]),
        int(raw["m4_records_seen"]),
        int(raw["m4_records_accepted"]),
        int(raw["m4_width_rejected_records"]),
        int(raw["m5_records_submitted"]),
        int(raw["m5_records_normalized"]),
        int(raw["m5_records_rejected"]),
        int(raw["event_count"]),
        int(raw["zero_event_records"]),
        int(raw["partial_event_records"]),
        int(raw["turn_count_mismatch_records"]),
        tuple(
            sorted(
                (int(item["turn_count"]), int(item["record_count"]))
                for item in histogram_raw
                if isinstance(item, dict)
            )
        ),
        tuple(sorted((str(side), int(count)) for side, count in side_counts_raw.items())),
        str(raw["evidence_scope"]),
    )


def _write_report(path: Path, report: dict[str, object], operational: dict[str, object]) -> None:
    document = {**report, "operational": operational}
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as stream:
        stream.write(_canonical_json(document) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--sequences-per-shard", type=int, default=5000)
    args = parser.parse_args()
    if args.sequences_per_shard < 1:
        parser.error("--sequences-per-shard must be positive")
    repo_root = args.repo_root.resolve(strict=True)
    intermediate_root = repo_root / "data" / "intermediate"
    output_value = args.output_dir or (intermediate_root / "m7.2" / "turn-summary-sequences")
    if not output_value.is_absolute():
        output_value = repo_root / output_value
    output_directory = output_value.resolve(strict=False)
    config = RuntimeConfig(
        raw_roots=(args.raw_root,),
        intermediate_root=intermediate_root,
        quarantine_root=repo_root / "data" / "quarantine",
        release_root=repo_root / "data" / "release",
        base_dir=repo_root,
    )
    if output_directory != intermediate_root and intermediate_root not in output_directory.parents:
        raise ValueError("turn-sequence output must be beneath the configured intermediate root")
    if output_directory.exists():
        raise FileExistsError("turn-sequence output exists; choose a new empty output directory")
    staging = output_directory.with_name("." + output_directory.name + ".incomplete")
    if staging.exists():
        raise FileExistsError("an incomplete turn-sequence staging directory already exists")

    deep_path = intermediate_root / "m5.2" / "deep-inspection-container-v2-methods.json"
    schema_path = intermediate_root / "m5.2" / "schema-registry-container-v2-methods.json"
    m5_path = intermediate_root / "m5.2" / "replay-mapping-registry-container-v2.json"
    evidence = load_verified_m3_evidence(
        deep_path,
        expected_evidence_digest=M3_EVIDENCE_DIGEST,
        expected_source_catalog_digest=SOURCE_CATALOG_DIGEST,
    )
    schema_registry = load_verified_schema_registry(
        schema_path,
        expected_registry_digest=M3_SCHEMA_REGISTRY_DIGEST,
        expected_source_catalog_digest=SOURCE_CATALOG_DIGEST,
        expected_m3_evidence_digest=M3_EVIDENCE_DIGEST,
    )
    m5_document = json.loads(m5_path.read_text(encoding="utf-8"))
    m5_projection = m5_document["mapping_registry"]
    m5_canonical_bytes = json.dumps(
        m5_projection, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    m5_registry_digest = hashlib.sha256(m5_canonical_bytes).hexdigest()
    if (
        m5_registry_digest != M5_MAPPING_REGISTRY_DIGEST
        or m5_document.get("mapping_registry_digest") != M5_MAPPING_REGISTRY_DIGEST
        or m5_projection.get("source_catalog_digest") != SOURCE_CATALOG_DIGEST
        or m5_projection.get("m3_evidence_digest") != M3_EVIDENCE_DIGEST
        or m5_projection.get("schema_registry_digest") != M3_SCHEMA_REGISTRY_DIGEST
    ):
        raise TurnSequenceError("accepted M5.1 mapping registry authority mismatch")
    group = next(
        item for item in evidence.groups if item.raw_schema_fingerprint == SOURCE_RAW_FINGERPRINT
    )
    registry_group = schema_registry.lookup(SourceKind.REPLAY, SOURCE_RAW_FINGERPRINT)
    if registry_group is None:
        raise TurnSequenceError("AFR Replay fingerprint is not supported by M3.2 registry")
    analysis = build_replay_field_analysis(evidence, schema_registry)
    mapping = build_turn_slot_mapping(
        group,
        source_interpretation_contract_id=registry_group.source_interpretation_contract_id,
        m3_evidence_digest=M3_EVIDENCE_DIGEST,
        source_archive_id_reviewed=SOURCE_ARCHIVE_ID,
    )
    if mapping.replay_field_mapping_id != SOURCE_M5_MAPPING_ID:
        raise TurnSequenceError("rebuilt M5.1 mapping differs from accepted mapping identity")
    review_group = next(
        item
        for item in m5_projection["group_dispositions"]
        if item["raw_schema_fingerprint"] == SOURCE_RAW_FINGERPRINT
        and item["disposition"] == "supported"
    )
    review = _load_review(review_group)
    mapping_registry = build_replay_mapping_registry(
        analysis,
        mapping_candidates=(mapping,),
        reviews=(review,),
    )
    if mapping_registry.mapping_registry_digest != M5_MAPPING_REGISTRY_DIGEST:
        raise TurnSequenceError("rebuilt M5.1 registry differs from accepted registry digest")
    adapter = ReplayAdapterV1(mapping_registry)

    inventory = inventory_raw_roots(config)
    source_item = next(
        item
        for item in inventory.candidates
        if item.basename == "replay_data_public.AFR.PremierDraft.csv.gz"
    )
    source_record = register_archive(source_item)
    if source_record.source_archive_id != SOURCE_ARCHIVE_ID:
        raise TurnSequenceError("selected Replay archive differs from accepted source identity")
    source_schema_registry = schema_registry
    source_config = SourceReaderConfigV1()
    config_identity = source_reader_config_identity(source_config)
    bindings = TurnSequenceBindingsV1(
        SOURCE_CATALOG_DIGEST,
        M3_EVIDENCE_DIGEST,
        M3_SCHEMA_REGISTRY_DIGEST,
        source_record.compressed_sha256,
        source_record.compressed_size_bytes,
        registry_group.source_interpretation_contract_id,
        "openmtgdata.raw-source-reader.v2",
        config_identity.source_reader_config_digest,
        M5_MAPPING_REGISTRY_DIGEST,
        M5_SEMANTIC_VALIDATION_DIGEST,
    )
    accumulator = TurnSequenceBuildAccumulatorV1(
        bindings=bindings,
        source_archive_id=source_record.source_archive_id,
        compressed_sha256=source_record.compressed_sha256,
        compressed_size_bytes=source_record.compressed_size_bytes,
        raw_schema_fingerprint=SOURCE_RAW_FINGERPRINT,
        replay_field_mapping_id=SOURCE_M5_MAPPING_ID,
        replay_mapping_registry_digest=M5_MAPPING_REGISTRY_DIGEST,
        m5_semantic_validation_digest=M5_SEMANTIC_VALIDATION_DIGEST,
        reader_config=source_config,
        m5_review=review,
    )

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    shard_writer = _SequenceShardWriter(staging, sequences_per_shard=args.sequences_per_shard)
    try:
        with open_source_reader(
            source_record, source_schema_registry, config=source_config
        ) as reader:
            header_fields = reader.header_fields
            if reader.raw_schema_fingerprint != SOURCE_RAW_FINGERPRINT:
                raise TurnSequenceError("M4 actual header differs from supported AFR fingerprint")
            for batch in reader:
                accumulator.observe_batch(batch)
                for raw_record in batch.accepted_records:
                    normalized = adapter.normalize_record(raw_record, header_fields=header_fields)
                    sequence = None
                    if normalized.rejection is None:
                        sequence = sequence_from_normalization_result(normalized, mapping, bindings)
                        shard_writer.write(sequence)
                    accumulator.observe_normalization(normalized, sequence)
            reader_summary = reader.summary
        shard_writer.close()
        build_report = accumulator.finish(reader_summary)
        validate_build_report(build_report)
        operational = {
            "compression": "gzip",
            "gzip_mtime": 0,
            "shard_sequence_limit": args.sequences_per_shard,
            "shard_count": shard_writer.shard_count,
            "sequences_written": shard_writer.sequences_written,
            "frames_written": shard_writer.frames_written,
        }
        if (
            shard_writer.sequences_written != build_report.sequences_emitted
            or shard_writer.frames_written != build_report.frames_emitted
        ):
            raise TurnSequenceError("written logical sequence counts differ from build report")
        _write_report(
            staging / "turn-sequence-build-report.json", build_report.to_dict(), operational
        )
        os.replace(staging, output_directory)
    finally:
        shard_writer.close()

    print(
        json.dumps(
            {
                "m4_records_seen": build_report.m4_records_seen,
                "m4_records_accepted": build_report.m4_records_accepted,
                "m4_records_rejected": build_report.m4_records_rejected,
                "m5_records_normalized": build_report.m5_records_normalized,
                "m5_records_rejected": build_report.m5_records_rejected,
                "sequences_emitted": build_report.sequences_emitted,
                "frames_emitted": build_report.frames_emitted,
                "virtual_next_turn_views": build_report.virtual_next_turn_views,
                "minimum_sequence_length": build_report.minimum_sequence_length,
                "maximum_sequence_length": build_report.maximum_sequence_length,
                "source_side_frame_counts": dict(build_report.source_side_frame_counts),
                "logical_sequence_digest": build_report.logical_sequence_digest,
                "build_report_digest": build_report.report_digest,
                "output_directory": str(output_directory),
                "shard_count": shard_writer.shard_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
