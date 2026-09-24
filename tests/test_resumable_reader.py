from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from openmtgdata.archive_registration import register_inventory_archives
from openmtgdata.compression_validation import validate_registered_compression
from openmtgdata.config import RuntimeConfig
from openmtgdata.header_inspection import inspect_registered_archive
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.resumable_reader import (
    RESUME_STRATEGY_ID,
    RUN_CONTRACT_ID,
    ResumableReaderError,
    SessionStatus,
    _advance_prefix,
    _batch_semantic_digest,
    _canonical_bytes,
    _prefix_genesis,
    _sha256,
    _unit_id,
    open_resumable_source_session,
    source_reader_config_identity,
)
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    SourceReaderConfigV1,
    SourceReaderError,
    VerifiedRegistryGroupV1,
    VerifiedSchemaRegistryV1,
)


def _setup(tmp_path: Path, body: bytes, *, batch_size: int = 2):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "game_data_public.TST.PremierDraft.csv.gz"
    source.write_bytes(gzip.compress(body, mtime=0))
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )
    record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    evidence = inspect_registered_archive(
        record, compression_evidence=validate_registered_compression(record)
    ).header_evidence
    assert evidence is not None
    contract_id = "openmtgdata.source-interpretation.v1:" + "a" * 64
    registry = VerifiedSchemaRegistryV1(
        "b" * 64,
        "openmtgdata.schema-registry.v1",
        "openmtgdata.schema-registry-digest.v1",
        "openmtgdata.schema-evolution-policy.v1",
        (
            VerifiedRegistryGroupV1(
                SourceKind.GAME,
                evidence.raw_schema_fingerprint,
                contract_id,
                (evidence.raw_schema_fingerprint,),
                "openmtgdata.csv-header-policy.comma-utf8.v1",
            ),
        ),
    )
    reader_config = SourceReaderConfigV1(
        max_logical_record_bytes=4096,
        max_fields_per_record=32,
        max_field_chars=1024,
        max_records_per_batch=batch_size,
        max_batch_payload_bytes=4096,
    )
    checkpoint_root = config.intermediate_root / "m4.2" / "checkpoints" / "fixture"
    return config, record, registry, reader_config, checkpoint_root, source


def _consume_to_completion(config, record, registry, reader_config, checkpoint_root):
    units: list[str] = []
    accepted: list[int] = []
    rejected: list[tuple[int | None, str]] = []
    with open_resumable_source_session(
        record,
        registry,
        config,
        checkpoint_root=checkpoint_root,
        reader_config=reader_config,
    ) as session:
        while (unit := session.next_batch()) is not None:
            units.append(unit.unit_id)
            accepted.extend(r.data_record_ordinal for r in unit.batch.accepted_records)
            rejected.extend(
                (d.data_record_ordinal, d.code.value) for d in unit.batch.record_diagnostics
            )
            session.commit_batch(unit.unit_id)
        summary = session.summary
    return units, accepted, rejected, summary


def test_uninterrupted_run_commits_and_completes(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(
        tmp_path, b"a,b\n1,2\n3,4,5\n6\n7,8\n9,10\n"
    )
    units, accepted, rejected, summary = _consume_to_completion(
        config, record, registry, reader_config, root
    )
    assert len(units) == 3
    assert accepted == [1, 4, 5]
    assert rejected == [(2, "ROW_WIDTH_LONGER"), (3, "ROW_WIDTH_SHORTER")]
    assert summary.completion_status is SessionStatus.COMPLETE
    assert summary.records_seen == 5
    assert summary.records_accepted == 3
    assert summary.records_rejected == 2
    assert summary.committed_prefix_digest != _prefix_genesis()


def test_uncommitted_unit_replays_identically_after_restart(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n3,4\n5,6\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as first_session:
        first = first_session.next_batch()
        assert first is not None
        first_id = first.unit_id
        assert first.batch.batch_ordinal == 1
        assert first_session.summary.committed_batch_ordinal == 0
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as resumed:
        replay = resumed.next_batch()
        assert replay is not None
        assert replay.unit_id == first_id
        assert replay.batch == first.batch
        with pytest.raises(ResumableReaderError, match="previous batch"):
            resumed.next_batch()
        with pytest.raises(ResumableReaderError, match="does not match"):
            resumed.commit_batch("wrong")
        resumed.commit_batch(replay.unit_id)
        second = resumed.next_batch()
        assert second is not None and second.batch.batch_ordinal == 2


def test_committed_batch_is_skipped_after_restart(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n3,4\n5,6\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        first = session.next_batch()
        assert first is not None
        session.commit_batch(first.unit_id)
        first_id = first.unit_id
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as resumed:
        second = resumed.next_batch()
        assert second is not None
        assert second.batch.batch_ordinal == 2
        assert second.unit_id != first_id


def test_interrupted_and_resumed_output_matches_uninterrupted(tmp_path: Path) -> None:
    body = (
        b'a,b,c\n1,2,3\n4,"comma,value",6\n7,8,9,extra\n10\n'
        b'11,"multi\nline","say ""hi"""\n14,15,16\n'
    )
    config, record, registry, reader_config, root, _ = _setup(
        tmp_path / "resumed", body, batch_size=2
    )
    uninterrupted = _consume_to_completion(
        config, record, registry, reader_config, root / "uninterrupted"
    )
    first_records: list[int] = []
    first_diagnostics: list[tuple[int | None, str]] = []
    first_unit_id = ""
    with open_resumable_source_session(
        record,
        registry,
        config,
        checkpoint_root=root / "interrupted",
        reader_config=reader_config,
    ) as session:
        first = session.next_batch()
        assert first is not None
        first_unit_id = first.unit_id
        first_records.extend(r.data_record_ordinal for r in first.batch.accepted_records)
        first_diagnostics.extend(
            (d.data_record_ordinal, d.code.value) for d in first.batch.record_diagnostics
        )
        session.commit_batch(first.unit_id)
        second = session.next_batch()
        assert second is not None
        # Simulated process loss before acknowledgement; this batch will replay.
    resumed = _consume_to_completion(config, record, registry, reader_config, root / "interrupted")
    assert [first_unit_id, *resumed[0]] == uninterrupted[0]
    assert [*first_records, *resumed[1]] == uninterrupted[1]
    assert [*first_diagnostics, *resumed[2]] == uninterrupted[2]
    assert resumed[3].committed_prefix_digest == uninterrupted[3].committed_prefix_digest
    assert resumed[3].records_seen == uninterrupted[3].records_seen
    assert resumed[3].records_accepted == uninterrupted[3].records_accepted
    assert resumed[3].records_rejected == uninterrupted[3].records_rejected
    assert resumed[3].completion_status is uninterrupted[3].completion_status
    assert resumed[3].completion_digest == uninterrupted[3].completion_digest


def test_config_or_source_change_is_not_silently_restarted(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(
        tmp_path, b"a,b\n1,2\n3,4\n5,6\n7,8\n"
    )
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        session.commit_batch(unit.unit_id)
    changed = SourceReaderConfigV1(
        max_logical_record_bytes=reader_config.max_logical_record_bytes,
        max_fields_per_record=reader_config.max_fields_per_record,
        max_field_chars=reader_config.max_field_chars,
        max_records_per_batch=1,
        max_batch_payload_bytes=reader_config.max_batch_payload_bytes,
    )
    with pytest.raises(ResumableReaderError, match="different source/config/schema"):
        with open_resumable_source_session(
            record, registry, config, checkpoint_root=root, reader_config=changed
        ):
            pass


def test_checkpoint_restream_detects_corrupted_progress(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n3,4\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        session.commit_batch(unit.unit_id)
        checkpoint_path = session.checkpoint_file
    assert checkpoint_path is not None
    envelope = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    envelope["checkpoint"]["last_committed_data_record_ordinal"] += 1
    checkpoint_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ResumableReaderError):
        with open_resumable_source_session(
            record, registry, config, checkpoint_root=root, reader_config=reader_config
        ):
            pass


def test_resealed_checkpoint_with_wrong_prefix_fails_restream_validation(
    tmp_path: Path,
) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n3,4\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        session.commit_batch(unit.unit_id)
        checkpoint_path = session.checkpoint_file
    assert checkpoint_path is not None
    envelope = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    envelope["checkpoint"]["committed_prefix_digest"] = "a" * 64
    envelope["checkpoint_digest"] = _sha256(_canonical_bytes(envelope["checkpoint"]))
    checkpoint_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ResumableReaderError, match="re-streamed committed prefix"):
        with open_resumable_source_session(
            record, registry, config, checkpoint_root=root, reader_config=reader_config
        ):
            pass


def test_changed_registered_source_cannot_reuse_checkpoint(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, source = _setup(tmp_path, b"a,b\n1,2\n3,4\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        session.commit_batch(unit.unit_id)
    source.write_bytes(gzip.compress(b"a,b\n9,8\n7,6\n", mtime=0))
    changed_record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    with pytest.raises(ResumableReaderError, match="different source/config/schema"):
        with open_resumable_source_session(
            changed_record,
            registry,
            config,
            checkpoint_root=root,
            reader_config=reader_config,
        ):
            pass


def test_checkpoint_audit_sequence_is_not_semantic_digest(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(
        tmp_path, b"a,b\n1,2\n3,4\n5,6\n7,8\n"
    )
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        checkpoint = session.commit_batch(unit.unit_id)
        checkpoint_path = session.checkpoint_file
    assert checkpoint_path is not None
    document = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    document["audit"]["write_sequence"] += 100
    checkpoint_path.write_text(json.dumps(document), encoding="utf-8")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as resumed:
        assert resumed.summary.committed_batch_ordinal == checkpoint.committed_batch_ordinal
        unit = resumed.next_batch()
        assert unit is not None
        resumed.commit_batch(unit.unit_id)


def test_zero_prefix_checkpoint_is_explicit_and_complete(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        assert session.summary.committed_batch_ordinal == 0
        assert session.next_batch() is None
        assert session.status is SessionStatus.COMPLETE
        checkpoint_path = session.checkpoint_file
    assert checkpoint_path is not None
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))["checkpoint"]
    assert checkpoint["checkpoint_contract_id"] == "openmtgdata.source-reader-checkpoint.v1"
    assert checkpoint["checkpoint_digest_contract_id"] == (
        "openmtgdata.source-reader-checkpoint-digest.v1"
    )
    assert checkpoint["completion_status"] == "incomplete"
    assert checkpoint["committed_batch_ordinal"] == 0
    assert checkpoint["last_committed_data_record_ordinal"] == 0
    assert checkpoint["cumulative_records_seen"] == 0
    assert checkpoint["cumulative_records_accepted"] == 0
    assert checkpoint["cumulative_records_rejected"] == 0


@pytest.mark.parametrize("phase", ["before_replace", "after_replace"])
def test_checkpoint_atomic_replace_crash_windows(tmp_path: Path, phase: str) -> None:
    config, record, registry, reader_config, root, _ = _setup(
        tmp_path, b"a,b\n1,2\n3,4\n5,6\n7,8\n"
    )
    fired = False

    def fail_once(event: str) -> None:
        nonlocal fired
        if event == phase and not fired:
            fired = True
            raise RuntimeError("injected crash")

    with pytest.raises(RuntimeError, match="injected crash"):
        with open_resumable_source_session(
            record,
            registry,
            config,
            checkpoint_root=root,
            reader_config=reader_config,
            fault_hook=fail_once,
        ) as session:
            unit = session.next_batch()
            assert unit is not None
            session.commit_batch(unit.unit_id)
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as resumed:
        unit = resumed.next_batch()
        assert unit is not None
        if phase == "before_replace":
            assert unit.batch.batch_ordinal == 1
        else:
            assert unit.batch.batch_ordinal == 2
        if phase == "before_replace":
            assert list(root.rglob("*.tmp"))
        resumed.commit_batch(unit.unit_id)


def test_complete_marker_only_after_reader_eof_and_already_complete(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n")
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        session.commit_batch(unit.unit_id)
        assert not list(root.rglob("complete.json"))
        assert session.next_batch() is None
        assert session.status is SessionStatus.COMPLETE
        marker_path = next(root.rglob("complete.json"))
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as finished:
        assert finished.status is SessionStatus.ALREADY_COMPLETE
        assert finished.next_batch() is None
    assert (
        json.loads(marker_path.read_text(encoding="utf-8"))["completion"][
            "terminal_m4_reader_completion_status"
        ]
        == "complete"
    )


@pytest.mark.parametrize("phase", ["before_replace", "after_replace"])
def test_completion_marker_atomic_failure_remains_safe(tmp_path: Path, phase: str) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n")
    replace_count = 0

    def fail_on_marker(event: str) -> None:
        nonlocal replace_count
        if event == phase:
            replace_count += 1
            if replace_count == 2:  # checkpoint, complete marker
                raise RuntimeError("marker crash")

    with pytest.raises(RuntimeError, match="marker crash"):
        with open_resumable_source_session(
            record,
            registry,
            config,
            checkpoint_root=root,
            reader_config=reader_config,
            fault_hook=fail_on_marker,
        ) as session:
            unit = session.next_batch()
            assert unit is not None
            session.commit_batch(unit.unit_id)
            session.next_batch()
    if phase == "before_replace":
        assert not list(root.rglob("complete.json"))
    else:
        assert list(root.rglob("complete.json"))
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as resumed:
        assert resumed.next_batch() is None
        if phase == "before_replace":
            assert resumed.status is SessionStatus.COMPLETE
        else:
            assert resumed.status is SessionStatus.ALREADY_COMPLETE


def test_checkpoint_root_must_be_under_intermediate_and_source_is_unchanged(
    tmp_path: Path,
) -> None:
    config, record, registry, reader_config, root, source = _setup(tmp_path, b"a,b\n1,2\n")
    before = source.read_bytes()
    with pytest.raises(ResumableReaderError, match="beneath intermediate_root"):
        open_resumable_source_session(
            record,
            registry,
            config,
            checkpoint_root=source.parent / "bad-checkpoints",
            reader_config=reader_config,
        )
    _consume_to_completion(config, record, registry, reader_config, root)
    assert source.read_bytes() == before


def test_run_identity_and_config_digest_are_deterministic_and_path_free(tmp_path: Path) -> None:
    body = b"a,b\n1,2\n"
    config_a, record_a, registry_a, reader_a, root_a, _ = _setup(tmp_path / "a", body)
    config_b, record_b, registry_b, reader_b, root_b, _ = _setup(tmp_path / "b", body)
    assert record_a.source_archive_id == record_b.source_archive_id
    assert source_reader_config_identity(reader_a) == source_reader_config_identity(reader_b)
    with open_resumable_source_session(
        record_a, registry_a, config_a, checkpoint_root=root_a, reader_config=reader_a
    ) as a:
        run_a = a.identity.resumable_source_run_id
    with open_resumable_source_session(
        record_b, registry_b, config_b, checkpoint_root=root_b, reader_config=reader_b
    ) as b:
        run_b = b.identity.resumable_source_run_id
    assert run_a == run_b
    assert run_a.startswith(f"{RUN_CONTRACT_ID}:")
    assert RESUME_STRATEGY_ID.endswith("restream-v1")


def test_prefix_and_batch_digests_are_stable(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, _ = _setup(tmp_path, b"a,b\n1,2\n")
    assert _prefix_genesis() == _prefix_genesis()
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        unit = session.next_batch()
        assert unit is not None
        assert unit.batch_semantic_digest == _batch_semantic_digest(unit.batch)
        assert unit.unit_id == _unit_id(
            session.identity.resumable_source_run_id,
            unit.batch,
            unit.batch_semantic_digest,
        )
        assert _advance_prefix(_prefix_genesis(), unit.batch_semantic_digest) != _prefix_genesis()


def test_fatal_next_batch_does_not_advance_checkpoint(tmp_path: Path) -> None:
    config, record, registry, reader_config, root, source = _setup(
        tmp_path, b"a,b\n" + b"1,2\n" * 600, batch_size=256
    )
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as session:
        first = session.next_batch()
        assert first is not None
        checkpoint = session.commit_batch(first.unit_id)
    mutated_body = b"a,b\n" + b"1,2\n" * 399 + b"3,\xff\n" + b"1,2\n" * 200
    source.write_bytes(gzip.compress(mutated_body, mtime=0))
    with open_resumable_source_session(
        record, registry, config, checkpoint_root=root, reader_config=reader_config
    ) as resumed:
        with pytest.raises(SourceReaderError):
            resumed.next_batch()
        assert resumed.summary.committed_batch_ordinal == checkpoint.committed_batch_ordinal
        assert resumed.status is SessionStatus.INCOMPLETE
