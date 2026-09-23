"""Safety and identity tests for runtime filesystem configuration."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from openmtgdata.config import (
    ConfigurationError,
    InvalidBaseDirectoryError,
    InvalidRootPathError,
    NoRawRootsError,
    RawRootMissingError,
    RawRootNotDirectoryError,
    RootOverlapError,
    RuntimeConfig,
    WritableRootNotDirectoryError,
)


def make_config(
    base: Path,
    *,
    raw_roots: tuple[Path, ...] | None = None,
    intermediate_root: Path | None = None,
    quarantine_root: Path | None = None,
    release_root: Path | None = None,
) -> RuntimeConfig:
    """Build a valid config with roots scoped to a pytest temporary directory."""
    raw = raw_roots or (base / "raw",)
    for root in raw:
        root.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig(
        raw_roots=raw,
        intermediate_root=intermediate_root or base / "work" / "intermediate",
        quarantine_root=quarantine_root or base / "work" / "quarantine",
        release_root=release_root or base / "work" / "release",
        base_dir=base,
    )


def tree_entries(root: Path) -> tuple[str, ...]:
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


def test_valid_configuration_with_one_raw_root_allows_missing_outputs(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    config = make_config(tmp_path)

    assert config.raw_roots == (raw.resolve(),)
    assert config.intermediate_root == (tmp_path / "work/intermediate").resolve()
    assert not config.intermediate_root.exists()
    assert not config.quarantine_root.exists()
    assert not config.release_root.exists()


def test_multiple_external_style_temporary_raw_roots_are_supported(tmp_path: Path) -> None:
    base = tmp_path / "project"
    base.mkdir()
    raw_one = tmp_path / "separate-volume-one" / "raw"
    raw_two = tmp_path / "separate-volume-two" / "archive"
    raw_one.mkdir(parents=True)
    raw_two.mkdir(parents=True)

    config = make_config(base, raw_roots=(raw_two, raw_one))

    assert config.raw_roots == tuple(sorted((raw_one.resolve(), raw_two.resolve()), key=os.fspath))
    assert all(root.is_absolute() for root in config.raw_roots)


def test_zero_raw_roots_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(NoRawRootsError, match="at least one raw input root"):
        RuntimeConfig(
            raw_roots=(),
            intermediate_root=tmp_path / "intermediate",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_raw_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(RawRootMissingError, match=r"raw_roots\[0\].*does not exist"):
        RuntimeConfig(
            raw_roots=(tmp_path / "missing",),
            intermediate_root=tmp_path / "intermediate",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_raw_root_must_be_a_directory(tmp_path: Path) -> None:
    raw_file = tmp_path / "raw-file"
    raw_file.write_text("temporary test file", encoding="utf-8")

    with pytest.raises(RawRootNotDirectoryError, match="is not a directory"):
        RuntimeConfig(
            raw_roots=(raw_file,),
            intermediate_root=tmp_path / "intermediate",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_existing_writable_path_must_be_a_directory(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    output_file = tmp_path / "output-file"
    output_file.write_text("temporary test file", encoding="utf-8")

    with pytest.raises(WritableRootNotDirectoryError, match="intermediate_root.*not a directory"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=output_file,
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


@pytest.mark.parametrize("output_name", ["intermediate_root", "quarantine_root", "release_root"])
def test_writable_root_equal_to_raw_root_is_rejected(tmp_path: Path, output_name: str) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    outputs: dict[str, Path] = {
        "intermediate_root": tmp_path / "intermediate",
        "quarantine_root": tmp_path / "quarantine",
        "release_root": tmp_path / "release",
    }
    outputs[output_name] = raw

    with pytest.raises(RootOverlapError, match="overlaps.*mutually disjoint"):
        RuntimeConfig(raw_roots=(raw,), base_dir=tmp_path, **outputs)


def test_writable_root_beneath_raw_root_is_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    with pytest.raises(RootOverlapError, match="overlaps"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=raw / "work",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_raw_root_beneath_writable_root_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    raw = workspace / "raw"
    raw.mkdir(parents=True)

    with pytest.raises(RootOverlapError, match="overlaps"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=workspace,
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_writable_roots_must_be_distinct(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    same = tmp_path / "same-output"

    with pytest.raises(RootOverlapError, match="intermediate_root.*quarantine_root"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=same,
            quarantine_root=same,
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_nested_writable_roots_are_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    workspace = tmp_path / "workspace"

    with pytest.raises(RootOverlapError, match="intermediate_root.*quarantine_root"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=workspace,
            quarantine_root=workspace / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_dot_and_dotdot_cannot_bypass_overlap_checks(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "nested").mkdir(parents=True)
    disguised_child = raw / "nested" / ".." / "work"

    with pytest.raises(RootOverlapError, match="overlaps"):
        RuntimeConfig(
            raw_roots=(raw / "." / "nested" / "..",),
            intermediate_root=disguised_child,
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_equivalent_duplicate_raw_roots_are_deduplicated(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "nested").mkdir(parents=True)

    config = make_config(tmp_path, raw_roots=(raw, raw / "nested" / "..", raw))

    assert config.raw_roots == (raw.resolve(),)


def test_raw_root_order_does_not_change_canonical_identity(tmp_path: Path) -> None:
    base = tmp_path / "project"
    base.mkdir()
    raw_a = tmp_path / "external-a"
    raw_b = tmp_path / "external-b"
    raw_a.mkdir()
    raw_b.mkdir()
    outputs = {
        "intermediate_root": base / "intermediate",
        "quarantine_root": base / "quarantine",
        "release_root": base / "release",
    }

    first = RuntimeConfig(raw_roots=(raw_a, raw_b), base_dir=base, **outputs)
    second = RuntimeConfig(raw_roots=(raw_b, raw_a), base_dir=base, **outputs)

    assert first.canonical_bytes == second.canonical_bytes
    assert first.runtime_config_digest == second.runtime_config_digest


def test_semantic_runtime_root_change_changes_digest(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    first = make_config(tmp_path)
    second = make_config(tmp_path, release_root=tmp_path / "other-release")

    assert first.runtime_config_digest != second.runtime_config_digest


def test_timestamps_and_environment_time_do_not_change_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    first_digest = config.runtime_config_digest
    monkeypatch.setattr(time, "time", lambda: 123456789.0)
    monkeypatch.setenv("TZ", "Pacific/Honolulu")

    repeated = make_config(tmp_path)

    assert repeated.runtime_config_digest == first_digest


def test_relative_paths_use_explicit_base_not_current_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "raw").mkdir()
    unrelated_cwd = tmp_path / "other-cwd"
    unrelated_cwd.mkdir()
    expected = make_config(base)

    monkeypatch.chdir(unrelated_cwd)
    relative = RuntimeConfig(
        raw_roots=(Path("raw"),),
        intermediate_root=Path("work/intermediate"),
        quarantine_root=Path("work/quarantine"),
        release_root=Path("work/release"),
        base_dir=base,
    )

    assert relative.canonical_bytes == expected.canonical_bytes


def test_relative_base_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidBaseDirectoryError, match="absolute path"):
        RuntimeConfig(
            raw_roots=(tmp_path,),
            intermediate_root="intermediate",
            quarantine_root="quarantine",
            release_root="release",
            base_dir=".",
        )


def test_unresolvable_path_raises_configuration_error(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    with pytest.raises(InvalidRootPathError, match="release_root cannot be resolved"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=tmp_path / "intermediate",
            quarantine_root=tmp_path / "quarantine",
            release_root="invalid\0path",
            base_dir=tmp_path,
        )


def test_writable_root_below_existing_file_is_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("temporary test file", encoding="utf-8")

    with pytest.raises(InvalidRootPathError, match="non-directory ancestor"):
        RuntimeConfig(
            raw_roots=(raw,),
            intermediate_root=parent_file / "child",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_validation_performs_no_filesystem_writes(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    before = tree_entries(tmp_path)

    make_config(tmp_path)

    assert tree_entries(tmp_path) == before


def test_existing_symlink_alias_cannot_bypass_overlap_detection(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    alias = tmp_path / "raw-alias"
    try:
        alias.symlink_to(raw, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(RootOverlapError, match="overlaps"):
        RuntimeConfig(
            raw_roots=(alias,),
            intermediate_root=raw / "work",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_canonical_json_and_sha256_match_golden_projection(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    config = make_config(tmp_path)
    expected_projection = {
        "intermediate_root": os.path.normcase(str((tmp_path / "work/intermediate").resolve())),
        "path_flavor": "windows" if os.name == "nt" else "posix",
        "quarantine_root": os.path.normcase(str((tmp_path / "work/quarantine").resolve())),
        "raw_roots": [os.path.normcase(str(raw.resolve()))],
        "release_root": os.path.normcase(str((tmp_path / "work/release").resolve())),
        "schema": "openmtgdata.runtime-config.v1",
    }
    expected_bytes = json.dumps(
        expected_projection,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert config.canonical_bytes == expected_bytes
    assert config.runtime_config_digest == hashlib.sha256(expected_bytes).hexdigest()


def test_canonical_projection_contains_only_runtime_root_contract(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    projection = json.loads(config.canonical_bytes)

    assert set(projection) == {
        "intermediate_root",
        "path_flavor",
        "quarantine_root",
        "raw_roots",
        "release_root",
        "schema",
    }


def test_raw_roots_cannot_overlap_each_other(tmp_path: Path) -> None:
    raw_parent = tmp_path / "raw"
    raw_child = raw_parent / "child"
    raw_child.mkdir(parents=True)

    with pytest.raises(RootOverlapError, match=r"raw_roots\[0\].*raw_roots\[1\]"):
        RuntimeConfig(
            raw_roots=(raw_parent, raw_child),
            intermediate_root=tmp_path / "intermediate",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )


def test_empty_path_values_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="non-empty text path"):
        RuntimeConfig(
            raw_roots=("",),
            intermediate_root=tmp_path / "intermediate",
            quarantine_root=tmp_path / "quarantine",
            release_root=tmp_path / "release",
            base_dir=tmp_path,
        )
