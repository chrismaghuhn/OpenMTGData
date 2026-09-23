"""Tests for deterministic filename-only local inventory."""

from __future__ import annotations

import builtins
import gzip
import hashlib
import io
import json
import socket
from pathlib import Path, PurePosixPath

import pytest

import openmtgdata.inventory as inventory_module
from openmtgdata.cli import main
from openmtgdata.config import RuntimeConfig
from openmtgdata.inventory import (
    INVENTORY_CONTRACT_ID,
    INVENTORY_SCOPE_STATEMENT,
    CandidateDisposition,
    InventoryExecutionError,
    SkippedEntryCode,
    inventory_raw_roots,
)
from openmtgdata.source_filename import (
    FilenameDiagnosticCode,
    RecognizedSourceFilename,
    SourceKind,
    UnrecognizedSourceFilename,
    parse_source_filename,
)


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


def create_placeholder(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic placeholder; not a dataset archive")


def test_empty_raw_root_returns_complete_empty_inventory(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    result = inventory_raw_roots(config)

    assert result.contract_id == INVENTORY_CONTRACT_ID
    assert result.configured_roots == config.raw_roots
    assert result.candidates == ()
    assert result.total_candidate_count == 0
    assert result.recognized_candidate_count == 0
    assert result.unrecognized_candidate_count == 0
    assert result.scope_statement == INVENTORY_SCOPE_STATEMENT


@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("game_data_public.AFR.PremierDraft.csv.gz", SourceKind.GAME),
        ("replay_data_public.AFR.TradDraft.csv.gz", SourceKind.REPLAY),
        ("draft_data_public.AFR.PremierDraft.csv.gz", SourceKind.DRAFT),
    ],
)
def test_one_candidate_for_each_source_kind(
    tmp_path: Path, filename: str, kind: SourceKind
) -> None:
    config = make_config(tmp_path)
    create_placeholder(config.raw_roots[0] / filename)

    result = inventory_raw_roots(config)

    assert result.total_candidate_count == 1
    item = result.candidates[0]
    assert item.disposition is CandidateDisposition.RECOGNIZED
    assert isinstance(item.filename_result, RecognizedSourceFilename)
    assert item.filename_result.source_kind is kind
    assert result.source_kind_counts == tuple(
        (source_kind, int(source_kind is kind)) for source_kind in SourceKind
    )


def test_unrecognized_candidate_is_reported_without_failing(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    create_placeholder(config.raw_roots[0] / "foo.csv.gz")

    result = inventory_raw_roots(config)

    item = result.candidates[0]
    assert isinstance(item.filename_result, UnrecognizedSourceFilename)
    assert item.filename_result.diagnostic_code is FilenameDiagnosticCode.UNRECOGNIZED_PREFIX
    assert result.unrecognized_candidate_count == 1


def test_candidate_filter_is_case_sensitive_and_ignores_unrelated_files(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    create_placeholder(config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz")
    create_placeholder(config.raw_roots[0] / "foo.CSV.GZ")
    create_placeholder(config.raw_roots[0] / "notes.txt")
    create_placeholder(config.raw_roots[0] / "archive.zip")

    result = inventory_raw_roots(config)

    assert [item.basename for item in result.candidates] == [
        "game_data_public.AFR.PremierDraft.csv.gz"
    ]


def test_nested_directories_are_traversed_and_report_relative_paths(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = config.raw_roots[0]
    create_placeholder(raw / "z" / "deep" / "replay_data_public.AFR.PremierDraft.csv.gz")
    create_placeholder(raw / "a" / "game_data_public.AFR.PremierDraft.csv.gz")

    result = inventory_raw_roots(config)

    assert [item.relative_path for item in result.candidates] == [
        PurePosixPath("a/game_data_public.AFR.PremierDraft.csv.gz"),
        PurePosixPath("z/deep/replay_data_public.AFR.PremierDraft.csv.gz"),
    ]
    assert all(item.raw_root == raw for item in result.candidates)


def test_inventory_order_is_deterministic_independent_of_creation_order(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = config.raw_roots[0]
    names = [
        "z/game_data_public.ZZZ.Format.csv.gz",
        "a/replay_data_public.AAA.Format.csv.gz",
        "m/draft_data_public.MMM.Format.csv.gz",
    ]
    for name in reversed(names):
        create_placeholder(raw / name)

    first = inventory_raw_roots(config)
    second = inventory_raw_roots(config)

    assert first.to_dict() == second.to_dict()
    assert [item.relative_path.as_posix() for item in first.candidates] == sorted(names)


def test_raw_root_input_order_does_not_change_logical_inventory(tmp_path: Path) -> None:
    base = tmp_path / "project"
    root_a = tmp_path / "outside-a"
    root_b = tmp_path / "outside-b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    create_placeholder(root_a / "game_data_public.AFR.PremierDraft.csv.gz")
    create_placeholder(root_b / "replay_data_public.BLB.TradDraft.csv.gz")
    first_config = make_config(base, (root_a, root_b))
    second_config = make_config(base, (root_b, root_a))

    first = inventory_raw_roots(first_config)
    second = inventory_raw_roots(second_config)

    assert first.to_dict() == second.to_dict()


def test_same_basename_in_two_directories_is_not_collapsed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    filename = "game_data_public.AFR.PremierDraft.csv.gz"
    create_placeholder(config.raw_roots[0] / "a" / filename)
    create_placeholder(config.raw_roots[0] / "b" / filename)

    result = inventory_raw_roots(config)

    assert result.total_candidate_count == 2
    assert result.basename_collision_count == 1
    assert result.basename_collisions[0].basename == filename
    assert [
        location.relative_path.as_posix() for location in result.basename_collisions[0].locations
    ] == [
        f"a/{filename}",
        f"b/{filename}",
    ]


def test_same_basename_across_raw_roots_is_not_collapsed(tmp_path: Path) -> None:
    base = tmp_path / "project"
    root_a = tmp_path / "external-a"
    root_b = tmp_path / "external-b"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    filename = "replay_data_public.AFR.TradDraft.csv.gz"
    create_placeholder(root_a / filename)
    create_placeholder(root_b / filename)
    config = make_config(base, (root_b, root_a))

    result = inventory_raw_roots(config)

    assert result.total_candidate_count == 2
    assert result.basename_collision_count == 1
    assert {location.raw_root for location in result.basename_collisions[0].locations} == {
        root_a.resolve(),
        root_b.resolve(),
    }


def test_every_candidate_appears_once_and_counts_reconcile(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = config.raw_roots[0]
    create_placeholder(raw / "a.csv.gz")
    create_placeholder(raw / "nested" / "b.csv.gz")
    create_placeholder(raw / "nested" / "game_data_public.AFR.PremierDraft.csv.gz")

    result = inventory_raw_roots(config)
    locations = [(item.raw_root, item.relative_path) for item in result.candidates]

    assert len(locations) == len(set(locations)) == 3
    assert result.total_candidate_count == 3
    assert result.total_candidate_count == (
        result.recognized_candidate_count + result.unrecognized_candidate_count
    )


@pytest.mark.parametrize("link_kind", ["directory", "file"])
def test_symlink_entries_are_skipped_and_never_followed(tmp_path: Path, link_kind: str) -> None:
    config = make_config(tmp_path / "project")
    raw = config.raw_roots[0]
    external = tmp_path / "external"
    external.mkdir()
    filename = "game_data_public.AFR.PremierDraft.csv.gz"
    create_placeholder(external / filename)
    link = raw / ("alias" if link_kind == "directory" else filename)
    try:
        link.symlink_to(
            external if link_kind == "directory" else external / filename,
            target_is_directory=link_kind == "directory",
        )
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = inventory_raw_roots(config)

    assert result.total_candidate_count == 0
    assert len(result.skipped_entries) == 1
    skipped = result.skipped_entries[0]
    assert skipped.code in {
        SkippedEntryCode.SYMLINK_NOT_FOLLOWED,
        SkippedEntryCode.REPARSE_POINT_NOT_FOLLOWED,
    }
    assert skipped.candidate_suffix_match is (link_kind == "file")
    assert skipped.relative_path.as_posix() == ("alias" if link_kind == "directory" else filename)


def test_symlink_to_outside_root_cannot_escape_inventory(tmp_path: Path) -> None:
    config = make_config(tmp_path / "project")
    external = tmp_path / "outside"
    external.mkdir()
    create_placeholder(external / "replay_data_public.AFR.PremierDraft.csv.gz")
    link = config.raw_roots[0] / "escape"
    try:
        link.symlink_to(external, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    result = inventory_raw_roots(config)

    assert result.total_candidate_count == 0
    assert len(result.skipped_entries) == 1


def test_disappearing_candidate_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    candidate = config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz"
    create_placeholder(candidate)
    original_lstat = inventory_module._lstat
    removed = False

    def disappear(path: Path, *, role: str) -> object:
        nonlocal removed
        result = original_lstat(path, role=role)
        if role == "filesystem entry" and path == candidate and not removed:
            path.unlink()
            removed = True
        return result

    monkeypatch.setattr(inventory_module, "_lstat", disappear)

    with pytest.raises(InventoryExecutionError, match="cannot resolve candidate file entry"):
        inventory_raw_roots(config)


def test_raw_root_disappearing_after_config_fails_closed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    raw = config.raw_roots[0]
    raw.rmdir()

    with pytest.raises(InventoryExecutionError, match="cannot resolve configured raw root"):
        inventory_raw_roots(config)


def test_inventory_reuses_m2_1_parser_result(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    filename = "replay_data_public.Cube_-_Powered.PickTwoTradDraft.csv.gz"
    create_placeholder(config.raw_roots[0] / filename)

    result = inventory_raw_roots(config)

    assert result.candidates[0].filename_result == parse_source_filename(filename)
    assert result.candidates[0].filename_result.contract_id == "openmtgdata.17lands-filename.v1"


def test_inventory_result_states_local_scope_and_deterministic_json(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    create_placeholder(config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz")

    result = inventory_raw_roots(config)
    report = result.to_dict()

    assert report["contract_id"] == INVENTORY_CONTRACT_ID
    assert report["scope_statement"] == INVENTORY_SCOPE_STATEMENT
    assert "global" not in str(report["scope_statement"]).lower()
    assert json.dumps(report, sort_keys=True, ensure_ascii=False) == json.dumps(
        inventory_raw_roots(config).to_dict(), sort_keys=True, ensure_ascii=False
    )


def test_inventory_does_not_open_read_decompress_or_hash_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    create_placeholder(config.raw_roots[0] / "game_data_public.AFR.PremierDraft.csv.gz")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("inventory attempted candidate content access")

    with monkeypatch.context() as no_content_access:
        no_content_access.setattr(builtins, "open", forbidden)
        no_content_access.setattr(io, "open", forbidden)
        no_content_access.setattr(Path, "open", forbidden)
        no_content_access.setattr(Path, "read_bytes", forbidden)
        no_content_access.setattr(Path, "read_text", forbidden)
        no_content_access.setattr(gzip, "open", forbidden)
        no_content_access.setattr(gzip, "decompress", forbidden)
        no_content_access.setattr(hashlib, "sha256", forbidden)
        no_content_access.setattr(socket, "socket", forbidden)
        no_content_access.setattr(socket, "create_connection", forbidden)

        result = inventory_raw_roots(config)

    assert result.total_candidate_count == 1
    assert result.recognized_candidate_count == 1


def test_inventory_cli_emits_json_and_unknown_candidate_is_successful(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = tmp_path / "project"
    config = make_config(base)
    create_placeholder(config.raw_roots[0] / "foo.csv.gz")
    args = [
        "inventory",
        "--raw-root",
        str(config.raw_roots[0]),
        "--base-dir",
        str(base),
        "--intermediate-root",
        str(base / "work" / "intermediate"),
        "--quarantine-root",
        str(base / "work" / "quarantine"),
        "--release-root",
        str(base / "work" / "release"),
    ]
    writable_roots = (
        base / "work" / "intermediate",
        base / "work" / "quarantine",
        base / "work" / "release",
    )
    assert all(not path.exists() for path in writable_roots)

    exit_code = main(args)
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert report["candidate_count"] == 1
    assert report["unrecognized_candidate_count"] == 1
    assert report["path_scope"] == "runtime_local"
    assert all(not path.exists() for path in writable_roots)
    assert report["candidates"][0]["filename_classification"]["diagnostic_code"] == (
        FilenameDiagnosticCode.UNRECOGNIZED_PREFIX.value
    )


def test_inventory_cli_returns_nonzero_for_execution_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "project"
    config = make_config(base)

    def fail_inventory(_config: RuntimeConfig) -> object:
        raise InventoryExecutionError("synthetic traversal failure")

    monkeypatch.setattr("openmtgdata.cli.inventory_raw_roots", fail_inventory)
    exit_code = main(
        [
            "inventory",
            "--raw-root",
            str(config.raw_roots[0]),
            "--base-dir",
            str(base),
            "--intermediate-root",
            str(base / "work" / "intermediate"),
            "--quarantine-root",
            str(base / "work" / "quarantine"),
            "--release-root",
            str(base / "work" / "release"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "synthetic traversal failure" in captured.err
