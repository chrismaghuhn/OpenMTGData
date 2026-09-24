from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from openmtgdata.archive_registration import register_inventory_archives
from openmtgdata.compression_validation import validate_registered_compression
from openmtgdata.config import RuntimeConfig
from openmtgdata.header_inspection import inspect_registered_archive
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.source_container import (
    SourceContainerError,
    expected_tar_csv_member,
    open_csv_source_payload,
)
from openmtgdata.source_filename import SourceKind
from openmtgdata.source_reader import (
    VerifiedRegistryGroupV1,
    VerifiedSchemaRegistryV1,
    open_source_reader,
)


def _tar_gzip(*members: tuple[str, bytes]) -> bytes:
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, body in members:
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    return gzip.compress(tar_bytes.getvalue())


def test_plain_gzip_csv_prefix_is_replayed_exactly() -> None:
    expected = b"a,b\n1,2\n"
    decompressed = gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(expected)), mode="rb")
    payload = open_csv_source_payload(
        decompressed,
        original_filename="game_data_public.TST.PremierDraft.csv.gz",
        source_kind="game",
    )
    assert payload.container_kind == "gzip_csv"
    assert payload.stream.read(4096) == expected
    payload.finish()


def test_exact_game_tar_member_is_exposed_and_tar_trailer_verified() -> None:
    filename = "game_data_public.AFR.PremierDraft.csv.gz"
    body = b"user_win_rate_bucket,draft_id\n0.5,abc\n"
    decompressed = gzip.GzipFile(fileobj=io.BytesIO(_tar_gzip((filename[:-3], body))), mode="rb")
    payload = open_csv_source_payload(decompressed, original_filename=filename, source_kind="game")
    assert payload.container_kind == "gzip_tar_csv"
    assert payload.member_name == filename[:-3]
    assert payload.stream.read(4096) == body
    payload.finish()


def test_replay_tar_member_name_uses_exact_public_export_rule() -> None:
    assert (
        expected_tar_csv_member("replay_data_public.AFR.PremierDraft.csv.gz", source_kind="replay")
        == "replay-data.AFR.PremierDraft.csv"
    )


def test_wrong_tar_member_fails_closed() -> None:
    filename = "game_data_public.AFR.PremierDraft.csv.gz"
    decompressed = gzip.GzipFile(fileobj=io.BytesIO(_tar_gzip(("other.csv", b"x\ny\n"))), mode="rb")
    with pytest.raises(SourceContainerError, match="expected regular CSV member"):
        open_csv_source_payload(decompressed, original_filename=filename, source_kind="game")


def test_additional_tar_member_fails_at_full_completion() -> None:
    filename = "game_data_public.AFR.PremierDraft.csv.gz"
    decompressed = gzip.GzipFile(
        fileobj=io.BytesIO(_tar_gzip((filename[:-3], b"x\ny\n"), ("second.csv", b"other\n"))),
        mode="rb",
    )
    payload = open_csv_source_payload(decompressed, original_filename=filename, source_kind="game")
    assert payload.stream.read(4096) == b"x\ny\n"
    with pytest.raises(SourceContainerError, match="additional member"):
        payload.finish()


def test_header_and_m4_reader_use_extracted_tar_csv(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    config = RuntimeConfig(
        raw_roots=(raw,),
        intermediate_root=tmp_path / "work" / "intermediate",
        quarantine_root=tmp_path / "work" / "quarantine",
        release_root=tmp_path / "work" / "release",
        base_dir=tmp_path,
    )
    name = "game_data_public.AFR.PremierDraft.csv.gz"
    source = raw / name
    source.write_bytes(_tar_gzip((name[:-3], b"a,b\n001,False\n")))
    record = register_inventory_archives(inventory_raw_roots(config)).records[0]
    header = inspect_registered_archive(
        record,
        compression_evidence=validate_registered_compression(record),
    ).header_evidence
    assert header is not None
    assert header.ordered_fields == ("a", "b")
    interpretation_id = "openmtgdata.source-interpretation.v1:" + "a" * 64
    registry = VerifiedSchemaRegistryV1(
        "b" * 64,
        "openmtgdata.schema-registry.v1",
        "openmtgdata.schema-registry-digest.v1",
        "openmtgdata.schema-evolution-policy.v1",
        (
            VerifiedRegistryGroupV1(
                SourceKind.GAME,
                header.raw_schema_fingerprint,
                interpretation_id,
                (header.raw_schema_fingerprint,),
                "openmtgdata.csv-header-policy.comma-utf8.v1",
            ),
        ),
    )
    with open_source_reader(record, registry) as reader:
        records = tuple(row for batch in reader for row in batch.accepted_records)
        assert reader.summary.completion_status.value == "complete"
        assert reader.summary.gzip_integrity_status == "valid"
    assert len(records) == 1
    assert records[0].fields == ("001", "False")
