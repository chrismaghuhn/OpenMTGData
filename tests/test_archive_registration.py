"""Tests for exact compressed-byte identity registration."""

from __future__ import annotations

import builtins
import csv
import gzip
import hashlib
import inspect
import io
import json
import os
import socket
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import pytest

import openmtgdata.archive_registration as registration_module
from openmtgdata.archive_registration import (
    ARCHIVE_REGISTRATION_CONTRACT_ID,
    HASH_CHUNK_SIZE_BYTES,
    PROVIDER_NAMESPACE,
    SOURCE_ARCHIVE_ID_CONTRACT_ID,
    SOURCE_ARCHIVE_RECORD_CONTRACT_ID,
    ArchiveByteCountMismatchError,
    ArchiveChangedDuringRegistrationError,
    ArchiveFileDisappearedError,
    ArchiveRegistrationError,
    ArchiveRegistrationResult,
    ArchiveSymlinkError,
    RegistrationConfigurationError,
    RegistrationExecutionError,
    SourceArchiveRecordV1,
    derive_source_archive_id,
    register_archive,
    register_inventory_archives,
)
from openmtgdata.cli import main
from openmtgdata.config import RuntimeConfig
from openmtgdata.inventory import InventoryItem, inventory_raw_roots
from openmtgdata.source_filename import (
    FILENAME_CONTRACT_ID,
    RecognizedSourceFilename,
    UnrecognizedSourceFilename,
    parse_source_filename,
)

_VECTOR_BYTES = b"OpenMTGData source archive test vector\n"
_VECTOR_SHA256 = "b82e5a848c688c9d37ed304c1c9afbca4f3b2277675c62ea40702ef22c48efda"
_VECTOR_SOURCE_ARCHIVE_ID = "8f2c75c3a5873b785b471bfacffa5d94c524ebb4b3298aff875c6b0effd5b5bb"


def make_config(base: Path, roots: tuple[Path, ...] | None = None) -> RuntimeConfig:
    base.mkdir(parents=True, exist_ok=True)
    raw_roots = roots or (base / "raw",)
    for root in raw_roots:
        root.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig(
        raw_roots=raw_roots,
        intermediate_root=base / "work" / "intermediate",
        quarantine_root=base / "work" / "quarantine",
        release_root=base / "work" / "release",
        base_dir=base,
    )


def make_inventory_item(
    config: RuntimeConfig,
    basename: str,
    content: bytes = _VECTOR_BYTES,
    *,
    relative_parent: str = "",
) -> InventoryItem:
    path = config.raw_roots[0] / relative_parent / basename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    inventory = inventory_raw_roots(config)
    for item in inventory.candidates:
        if item.raw_root == config.raw_roots[0] and item.basename == basename:
            if item.relative_path.parent.as_posix() == (relative_parent or "."):
                return item
    raise AssertionError(f"synthetic candidate missing from inventory: {basename}")


def test_known_binary_vector_hash_and_source_archive_id() -> None:
    assert hashlib.sha256(_VECTOR_BYTES).hexdigest() == _VECTOR_SHA256
    assert (
        derive_source_archive_id(_VECTOR_SHA256, provider_namespace=PROVIDER_NAMESPACE)
        == _VECTOR_SOURCE_ARCHIVE_ID
    )


def test_single_archive_record_contains_exact_sha_size_and_contracts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(
        config,
        "game_data_public.AFR.PremierDraft.csv.gz",
        b"binary\x00bytes\n",
    )

    record = register_archive(item)

    assert isinstance(record, SourceArchiveRecordV1)
    assert record.record_contract_id == SOURCE_ARCHIVE_RECORD_CONTRACT_ID
    assert record.source_archive_id_contract_id == SOURCE_ARCHIVE_ID_CONTRACT_ID
    assert record.filename_contract_id == FILENAME_CONTRACT_ID
    assert record.inventory_contract_id == inventory_raw_roots(config).contract_id
    assert record.provider_namespace == PROVIDER_NAMESPACE
    assert record.compressed_sha256 == hashlib.sha256(b"binary\x00bytes\n").hexdigest()
    assert len(record.compressed_sha256) == 64
    assert record.compressed_sha256 == record.compressed_sha256.lower()
    assert record.compressed_size_bytes == len(b"binary\x00bytes\n")
    assert record.original_basename == item.basename
    assert isinstance(record.filename_result, RecognizedSourceFilename)
    assert record.filename_result == parse_source_filename(item.basename)
    assert record.source_kind is not None and record.source_kind.value == "game"
    assert record.expansion_token == "AFR"
    assert record.format_token == "PremierDraft"
    assert record.registration_status.value == "registered"


def test_golden_vector_registers_invalid_gzip_bytes_without_validation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "replay_data_public.AFR.TradDraft.csv.gz", _VECTOR_BYTES)

    record = register_archive(item)

    assert record.compressed_sha256 == _VECTOR_SHA256
    assert record.source_archive_id == _VECTOR_SOURCE_ARCHIVE_ID
    assert record.compressed_size_bytes == len(_VECTOR_BYTES)


def test_unrecognized_filename_is_preserved_and_registered(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "banana.csv.gz", b"not a gzip stream")

    record = register_archive(item)

    assert isinstance(record.filename_result, UnrecognizedSourceFilename)
    assert record.filename_disposition == "unrecognized"
    assert record.source_kind is None
    assert record.expansion_token is None
    assert record.format_token is None
    assert record.original_basename == "banana.csv.gz"


def test_same_bytes_at_different_paths_and_filenames_share_portable_id(
    tmp_path: Path,
) -> None:
    base = tmp_path / "project"
    root_a = tmp_path / "raw-a"
    root_b = tmp_path / "raw-b"
    config = make_config(base, (root_a, root_b))
    path_a = root_a / "a" / "game_data_public.AFR.PremierDraft.csv.gz"
    path_b = root_b / "b" / "copy-of-bytes.csv.gz"
    for path in (path_a, path_b):
        path.parent.mkdir(parents=True)
        path.write_bytes(_VECTOR_BYTES)
    inventory = inventory_raw_roots(config)

    records = register_inventory_archives(inventory).records

    assert len(records) == 2
    assert records[0].source_archive_id == records[1].source_archive_id
    assert records[0].compressed_sha256 == records[1].compressed_sha256 == _VECTOR_SHA256
    assert records[0].raw_root != records[1].raw_root
    assert records[0].original_basename != records[1].original_basename
    assert records[0].source_archive_id == derive_source_archive_id(_VECTOR_SHA256)


def test_source_id_depends_on_provider_and_sha_only() -> None:
    first = derive_source_archive_id(_VECTOR_SHA256, provider_namespace="provider-a")
    same = derive_source_archive_id(_VECTOR_SHA256, provider_namespace="provider-a")
    other_provider = derive_source_archive_id(_VECTOR_SHA256, provider_namespace="provider-b")
    other_bytes = derive_source_archive_id("0" * 64, provider_namespace="provider-a")

    assert first == same
    assert first != other_provider
    assert first != other_bytes
    assert tuple(inspect.signature(derive_source_archive_id).parameters) == (
        "compressed_sha256",
        "provider_namespace",
    )


def test_sha256_has_lowercase_hex_and_size_is_exact(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    content = bytes(range(256)) * 9 + b"\x00\xff"
    item = make_inventory_item(config, "foo.csv.gz", content)

    record = register_archive(item)

    assert record.compressed_sha256 == hashlib.sha256(content).hexdigest()
    assert len(record.compressed_sha256) == 64
    assert all(character in "0123456789abcdef" for character in record.compressed_sha256)
    assert record.compressed_size_bytes == len(content)


def test_id_does_not_use_file_size_independently_or_filename(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item_a = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)
    record_a = register_archive(item_a)

    other_root = tmp_path / "other-raw"
    other_config = make_config(tmp_path / "second-project", (other_root,))
    item_b = make_inventory_item(other_config, "some-other-name.csv.gz", _VECTOR_BYTES)
    record_b = register_archive(item_b)

    assert record_a.compressed_size_bytes == record_b.compressed_size_bytes
    assert record_a.original_basename != record_b.original_basename
    assert record_a.source_archive_id == record_b.source_archive_id


def test_different_bytes_have_different_hash_and_id(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item_a = make_inventory_item(config, "a.csv.gz", b"first bytes")
    item_b = make_inventory_item(config, "b.csv.gz", b"other bytes")

    first = register_archive(item_a)
    second = register_archive(item_b)

    assert first.compressed_sha256 != second.compressed_sha256
    assert first.source_archive_id != second.source_archive_id


def test_registration_uses_multiple_bounded_binary_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    content = bytes(range(256)) * 10
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", content)
    requested_sizes: list[int] = []
    returned_sizes: list[int] = []
    original_iter_chunks = registration_module._iter_chunks

    class ReadRecorder:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped

        def read(self, size: int) -> bytes:
            requested_sizes.append(size)
            chunk = self.wrapped.read(size)  # type: ignore[attr-defined]
            returned_sizes.append(len(chunk))
            return chunk

    def record_reads(stream: object, chunk_size_bytes: int) -> Iterator[bytes]:
        yield from original_iter_chunks(ReadRecorder(stream), chunk_size_bytes)  # type: ignore[arg-type]

    monkeypatch.setattr(registration_module, "_iter_chunks", record_reads)
    record = register_archive(item, chunk_size_bytes=128)

    assert HASH_CHUNK_SIZE_BYTES == 4 * 1024 * 1024
    assert record.compressed_sha256 == hashlib.sha256(content).hexdigest()
    assert len(returned_sizes) > 1
    assert all(size == 128 for size in requested_sizes)
    assert all(size <= 128 for size in returned_sizes)
    assert sum(returned_sizes) == len(content)


def test_hash_is_independent_of_chunk_boundaries(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    content = bytes(range(251)) * 8
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", content)

    small_chunks = register_archive(item, chunk_size_bytes=127)
    larger_chunks = register_archive(item, chunk_size_bytes=1024)

    assert small_chunks.compressed_sha256 == larger_chunks.compressed_sha256
    assert small_chunks.source_archive_id == larger_chunks.source_archive_id


def test_hashing_never_calls_decompression_csv_text_or_network_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("registration attempted forbidden interpretation or network I/O")

    with monkeypatch.context() as guards:
        guards.setattr(gzip, "open", forbidden)
        guards.setattr(gzip, "GzipFile", forbidden)
        guards.setattr(gzip, "decompress", forbidden)
        guards.setattr(csv, "reader", forbidden)
        guards.setattr(io, "TextIOWrapper", forbidden)
        guards.setattr(builtins, "open", forbidden)
        guards.setattr(Path, "read_bytes", forbidden)
        guards.setattr(Path, "read_text", forbidden)
        guards.setattr(socket, "socket", forbidden)
        guards.setattr(socket, "create_connection", forbidden)

        record = register_archive(item)

    assert record.compressed_sha256 == _VECTOR_SHA256


def test_registration_batch_consumes_inventory_without_rescanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)
    inventory = inventory_raw_roots(config)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("registration attempted independent directory traversal")

    monkeypatch.setattr(os, "scandir", forbidden)
    result = register_inventory_archives(inventory)

    assert result.registered_archive_count == 1


def test_candidate_disappearing_before_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)
    original_open = registration_module.os.open

    def remove_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        Path(path).unlink()  # type: ignore[arg-type]
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(registration_module.os, "open", remove_before_open)

    with pytest.raises(ArchiveFileDisappearedError):
        register_archive(item)


def test_candidate_replaced_by_symlink_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path / "project")
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)
    outside = tmp_path / "outside.csv.gz"
    outside.write_bytes(_VECTOR_BYTES)
    path = item.raw_root.joinpath(*item.relative_path.parts)
    path.unlink()
    try:
        path.symlink_to(outside, target_is_directory=False)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlink creation is unavailable: {exc}")

    with pytest.raises(ArchiveSymlinkError):
        register_archive(item)


def test_symlinked_parent_cannot_redirect_registration_outside_root(tmp_path: Path) -> None:
    config = make_config(tmp_path / "project")
    candidate_name = "game_data_public.AFR.PremierDraft.csv.gz"
    item = make_inventory_item(
        config,
        candidate_name,
        _VECTOR_BYTES,
        relative_parent="nested",
    )
    parent = config.raw_roots[0] / "nested"
    moved_parent = config.raw_roots[0] / "nested-moved"
    external = tmp_path / "external"
    external.mkdir()
    (external / candidate_name).write_bytes(b"outside bytes")
    parent.rename(moved_parent)
    try:
        parent.symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(ArchiveSymlinkError):
        register_archive(item)


def test_changed_during_hashing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    content = bytes(range(128)) * 4
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", content)
    path = item.raw_root.joinpath(*item.relative_path.parts)
    original_iter = registration_module._iter_chunks

    def change_same_size_during_read(stream: BinaryIO, chunk_size: int) -> Iterator[bytes]:
        iterator = iter(original_iter(stream, chunk_size))
        first_chunk = next(iterator)
        yield first_chunk
        try:
            path.write_bytes(b"x" * len(content))
        except OSError as exc:
            pytest.skip(f"filesystem does not permit concurrent replacement test: {exc}")
        yield from iterator

    monkeypatch.setattr(registration_module, "_iter_chunks", change_same_size_during_read)

    with pytest.raises(ArchiveChangedDuringRegistrationError):
        register_archive(item, chunk_size_bytes=64)


@pytest.mark.parametrize("mutation", ["truncate", "enlarge"])
def test_size_change_during_hashing_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    config = make_config(tmp_path)
    content = bytes(range(128)) * 4
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", content)
    path = item.raw_root.joinpath(*item.relative_path.parts)
    original_iter = registration_module._iter_chunks

    def mutate_size_during_read(stream: BinaryIO, chunk_size: int) -> Iterator[bytes]:
        iterator = iter(original_iter(stream, chunk_size))
        first_chunk = next(iterator)
        yield first_chunk
        try:
            if mutation == "truncate":
                path.write_bytes(content[:7])
            else:
                with path.open("ab") as output:
                    output.write(b"extra appended bytes")
        except OSError as exc:
            pytest.skip(f"filesystem does not permit concurrent size mutation test: {exc}")
        yield from iterator

    monkeypatch.setattr(registration_module, "_iter_chunks", mutate_size_during_read)

    with pytest.raises((ArchiveChangedDuringRegistrationError, ArchiveByteCountMismatchError)):
        register_archive(item, chunk_size_bytes=64)


def test_unsafe_relative_path_is_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "foo.csv.gz")
    unsafe_item = replace(item, relative_path=PurePosixPath("../outside/foo.csv.gz"))

    with pytest.raises(ArchiveRegistrationError, match="unsafe components"):
        register_archive(unsafe_item)


def test_batch_keeps_duplicate_content_records_and_reconciles(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = config.raw_roots[0] / "a" / "game_data_public.AFR.PremierDraft.csv.gz"
    second = config.raw_roots[0] / "b" / "same-bytes-different-name.csv.gz"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_bytes(_VECTOR_BYTES)
    inventory = inventory_raw_roots(config)

    result = register_inventory_archives(inventory)

    assert isinstance(result, ArchiveRegistrationResult)
    assert result.registration_contract_id == ARCHIVE_REGISTRATION_CONTRACT_ID
    assert result.registered_archive_count == inventory.total_candidate_count == 2
    assert result.unique_source_archive_id_count == 1
    assert result.duplicate_content_group_count == 1
    assert result.duplicate_content_groups[0].record_count == 2
    assert result.total_compressed_bytes == len(_VECTOR_BYTES) * 2
    assert len(result.records) == 2
    assert result.records[0].source_archive_id == result.records[1].source_archive_id


def test_batch_registration_order_and_serialization_are_deterministic(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    make_inventory_item(config, "z.csv.gz", b"last")
    make_inventory_item(config, "a.csv.gz", b"first")
    inventory = inventory_raw_roots(config)

    first = register_inventory_archives(inventory)
    second = register_inventory_archives(inventory)

    assert first.records == second.records
    assert first.canonical_bytes == second.canonical_bytes
    assert [record.relative_path.as_posix() for record in first.records] == [
        "a.csv.gz",
        "z.csv.gz",
    ]


def test_record_portable_and_runtime_local_serialization_are_separate(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)
    record = register_archive(item)
    serialized = record.to_dict()

    assert set(serialized) == {"portable_semantic", "runtime_local"}
    assert serialized["portable_semantic"]["source_archive_id"] == record.source_archive_id
    assert serialized["runtime_local"]["path_scope"] == "runtime_local"
    assert b"runtime_local" in record.canonical_bytes
    assert b"timestamp" not in record.canonical_bytes


def test_batch_all_or_error_does_not_return_partial_records(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = make_inventory_item(config, "a.csv.gz", _VECTOR_BYTES)
    second = make_inventory_item(config, "b.csv.gz", b"different")
    inventory = inventory_raw_roots(config)
    second_path = second.raw_root.joinpath(*second.relative_path.parts)
    second_path.unlink()

    with pytest.raises(RegistrationExecutionError, match="batch registration aborted"):
        register_inventory_archives(inventory)

    assert first.raw_root.joinpath(*first.relative_path.parts).exists()


def test_registration_does_not_modify_raw_file_metadata(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    item = make_inventory_item(config, "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES)
    path = item.raw_root.joinpath(*item.relative_path.parts)
    before = path.stat()
    directory_entries_before = tuple(sorted(child.name for child in item.raw_root.iterdir()))

    register_archive(item)

    after = path.stat()
    directory_entries_after = tuple(sorted(child.name for child in item.raw_root.iterdir()))
    assert (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert directory_entries_after == directory_entries_before


def test_register_cli_emits_final_json_and_does_not_create_writable_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = tmp_path / "project"
    config = make_config(base)
    item = make_inventory_item(config, "banana.csv.gz", _VECTOR_BYTES)
    writable = (
        base / "work" / "intermediate",
        base / "work" / "quarantine",
        base / "work" / "release",
    )
    assert all(not path.exists() for path in writable)

    exit_code = main(
        [
            "register",
            "--raw-root",
            str(config.raw_roots[0]),
            "--base-dir",
            str(base),
            "--intermediate-root",
            str(writable[0]),
            "--quarantine-root",
            str(writable[1]),
            "--release-root",
            str(writable[2]),
        ]
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert report["registered_archive_count"] == 1
    assert report["unique_source_archive_id_count"] == 1
    assert report["total_compressed_bytes"] == len(_VECTOR_BYTES)
    assert report["provider_namespace"] == PROVIDER_NAMESPACE
    assert report["record_contract_id"] == SOURCE_ARCHIVE_RECORD_CONTRACT_ID
    assert report["source_archive_id_contract_id"] == SOURCE_ARCHIVE_ID_CONTRACT_ID
    assert (
        report["records"][0]["portable_semantic"]["filename_classification"]["disposition"]
        == "unrecognized"
    )
    assert report["records"][0]["runtime_local"]["path_scope"] == "runtime_local"
    assert item.basename == "banana.csv.gz"
    assert all(not path.exists() for path in writable)


def test_source_archive_id_rejects_noncanonical_sha_text() -> None:
    for value in ("A" * 64, "sha256:" + "a" * 64, "a" * 63, "a" * 64 + " "):
        with pytest.raises(RegistrationConfigurationError):
            derive_source_archive_id(value)
