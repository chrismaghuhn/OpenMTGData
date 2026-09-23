"""Deterministic filename-only inventory of configured raw-root entries."""

from __future__ import annotations

import os
import stat
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from openmtgdata.config import RuntimeConfig
from openmtgdata.source_filename import (
    FilenameParseResult,
    RecognizedSourceFilename,
    SourceKind,
    UnrecognizedSourceFilename,
    parse_source_filename,
)

INVENTORY_CONTRACT_ID = "openmtgdata.local-inventory.v1"
INVENTORY_SCOPE_STATEMENT = (
    "Complete for configured raw roots at observed state, excluding reported symlink/reparse "
    "entries under the v1 no-follow policy."
)
_CANDIDATE_SUFFIX = ".csv.gz"
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class InventoryExecutionError(RuntimeError):
    """Raised when safe, complete traversal of configured roots cannot finish."""


class CandidateDisposition(StrEnum):
    """Filename classification for a regular candidate file."""

    RECOGNIZED = "recognized"
    UNRECOGNIZED = "unrecognized"


class SkippedEntryCode(StrEnum):
    """Reasons a filesystem entry was intentionally excluded from candidates."""

    SYMLINK_NOT_FOLLOWED = "SYMLINK_NOT_FOLLOWED"
    REPARSE_POINT_NOT_FOLLOWED = "REPARSE_POINT_NOT_FOLLOWED"
    NON_REGULAR_ENTRY = "NON_REGULAR_ENTRY"


@dataclass(frozen=True, slots=True)
class InventoryItem:
    """One regular `.csv.gz` candidate under a canonical raw root."""

    basename: str
    raw_root: Path
    relative_path: PurePosixPath
    filename_result: FilenameParseResult

    @property
    def disposition(self) -> CandidateDisposition:
        if isinstance(self.filename_result, RecognizedSourceFilename):
            return CandidateDisposition.RECOGNIZED
        return CandidateDisposition.UNRECOGNIZED


@dataclass(frozen=True, slots=True)
class SkippedFilesystemEntry:
    """A symlink, reparse point, or special entry that was not traversed/read."""

    basename: str
    raw_root: Path
    relative_path: PurePosixPath
    code: SkippedEntryCode
    reason: str
    candidate_suffix_match: bool


@dataclass(frozen=True, slots=True)
class InventoryLocation:
    """A local root-relative location used in basename collision diagnostics."""

    raw_root: Path
    relative_path: PurePosixPath


@dataclass(frozen=True, slots=True)
class BasenameCollision:
    """A basename shared by multiple distinct candidate paths."""

    basename: str
    locations: tuple[InventoryLocation, ...]


@dataclass(frozen=True, slots=True)
class InventoryResult:
    """A complete, local-scope inventory result without content identities."""

    contract_id: str
    configured_roots: tuple[Path, ...]
    candidates: tuple[InventoryItem, ...]
    skipped_entries: tuple[SkippedFilesystemEntry, ...]
    basename_collisions: tuple[BasenameCollision, ...]
    scope_statement: str = INVENTORY_SCOPE_STATEMENT

    @property
    def total_candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def recognized_candidate_count(self) -> int:
        return sum(item.disposition is CandidateDisposition.RECOGNIZED for item in self.candidates)

    @property
    def unrecognized_candidate_count(self) -> int:
        return self.total_candidate_count - self.recognized_candidate_count

    @property
    def source_kind_counts(self) -> tuple[tuple[SourceKind, int], ...]:
        counts = {kind: 0 for kind in SourceKind}
        for item in self.candidates:
            if isinstance(item.filename_result, RecognizedSourceFilename):
                counts[item.filename_result.source_kind] += 1
        return tuple((kind, counts[kind]) for kind in SourceKind)

    @property
    def basename_collision_count(self) -> int:
        return len(self.basename_collisions)

    def to_dict(self) -> dict[str, object]:
        """Serialize a deterministic JSON-compatible local inventory report."""
        return {
            "basename_collision_count": self.basename_collision_count,
            "basename_collisions": [
                {
                    "basename": collision.basename,
                    "locations": [
                        {
                            "raw_root": str(location.raw_root),
                            "relative_path": location.relative_path.as_posix(),
                        }
                        for location in collision.locations
                    ],
                }
                for collision in self.basename_collisions
            ],
            "candidate_count": self.total_candidate_count,
            "candidates": [_candidate_to_dict(item) for item in self.candidates],
            "configured_roots": [str(root) for root in self.configured_roots],
            "contract_id": self.contract_id,
            "path_scope": "runtime_local",
            "recognized_candidate_count": self.recognized_candidate_count,
            "skipped_entries": [_skipped_to_dict(item) for item in self.skipped_entries],
            "skipped_entry_count": len(self.skipped_entries),
            "source_kind_counts": {kind.value: count for kind, count in self.source_kind_counts},
            "scope_statement": self.scope_statement,
            "unrecognized_candidate_count": self.unrecognized_candidate_count,
        }


def _path_key(path: Path) -> str:
    return os.path.normcase(os.fspath(path))


def _is_within(root: Path, path: Path) -> bool:
    return root == path or root in path.parents


def _entry_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return os.name == "nt" and bool(attributes & _WINDOWS_REPARSE_POINT)


def _checked_path(path: Path, *, raw_root: Path, role: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InventoryExecutionError(f"cannot resolve {role}: {path}") from exc
    if not _is_within(raw_root, resolved):
        raise InventoryExecutionError(
            f"{role} resolves outside its configured raw root: {path} -> {resolved}"
        )
    return resolved


def _lstat(path: Path, *, role: str) -> os.stat_result:
    try:
        return os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise InventoryExecutionError(f"cannot inspect {role}: {path}") from exc


def _to_relative_posix(relative_parts: tuple[str, ...]) -> PurePosixPath:
    return PurePosixPath(*relative_parts)


def _inventory_one_root(
    raw_root: Path,
) -> tuple[list[InventoryItem], list[SkippedFilesystemEntry]]:
    root = _checked_path(raw_root, raw_root=raw_root, role="configured raw root")
    root_info = _lstat(root, role="configured raw root")
    if not stat.S_ISDIR(root_info.st_mode) or _entry_reparse_point(root_info):
        raise InventoryExecutionError(f"configured raw root changed type during inventory: {root}")

    candidates: list[InventoryItem] = []
    skipped: list[SkippedFilesystemEntry] = []
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    seen_locations: set[tuple[str, str]] = set()

    while pending:
        directory, relative_parts = pending.pop()
        resolved_directory = _checked_path(
            directory,
            raw_root=root,
            role="directory entry",
        )
        directory_info = _lstat(resolved_directory, role="directory entry")
        if not stat.S_ISDIR(directory_info.st_mode) or _entry_reparse_point(directory_info):
            raise InventoryExecutionError(
                f"directory entry changed type during inventory: {directory}"
            )

        try:
            with os.scandir(resolved_directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise InventoryExecutionError(
                f"cannot traverse directory: {resolved_directory}"
            ) from exc

        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            basename = entry.name
            child_parts = relative_parts + (basename,)
            relative_path = _to_relative_posix(child_parts)
            full_path = resolved_directory / basename
            location_key = (_path_key(root), relative_path.as_posix())
            if location_key in seen_locations:
                raise InventoryExecutionError(
                    f"filesystem entry was encountered more than once: {relative_path.as_posix()}"
                )

            info = _lstat(full_path, role="filesystem entry")
            mode = info.st_mode
            if _entry_reparse_point(info):
                skipped.append(
                    SkippedFilesystemEntry(
                        basename=basename,
                        raw_root=root,
                        relative_path=relative_path,
                        code=SkippedEntryCode.REPARSE_POINT_NOT_FOLLOWED,
                        reason="observable Windows reparse point was not followed",
                        candidate_suffix_match=basename.endswith(_CANDIDATE_SUFFIX),
                    )
                )
                seen_locations.add(location_key)
                continue
            if stat.S_ISLNK(mode):
                skipped.append(
                    SkippedFilesystemEntry(
                        basename=basename,
                        raw_root=root,
                        relative_path=relative_path,
                        code=SkippedEntryCode.SYMLINK_NOT_FOLLOWED,
                        reason="symbolic link was not followed or inventoried as a file",
                        candidate_suffix_match=basename.endswith(_CANDIDATE_SUFFIX),
                    )
                )
                seen_locations.add(location_key)
                continue
            if stat.S_ISDIR(mode):
                resolved_child = _checked_path(
                    full_path,
                    raw_root=root,
                    role="directory entry",
                )
                child_info = _lstat(full_path, role="directory entry")
                if (
                    not stat.S_ISDIR(child_info.st_mode)
                    or stat.S_ISLNK(child_info.st_mode)
                    or _entry_reparse_point(child_info)
                ):
                    raise InventoryExecutionError(
                        f"directory entry changed type during inventory: {full_path}"
                    )
                child_directories.append((resolved_child, child_parts))
                seen_locations.add(location_key)
                continue
            if stat.S_ISREG(mode):
                if basename.endswith(_CANDIDATE_SUFFIX):
                    parent = _checked_path(
                        full_path.parent,
                        raw_root=root,
                        role="candidate parent directory",
                    )
                    if parent != resolved_directory:
                        raise InventoryExecutionError(
                            f"candidate parent changed during inventory: {full_path}"
                        )
                    resolved_candidate = _checked_path(
                        full_path,
                        raw_root=root,
                        role="candidate file entry",
                    )
                    final_info = _lstat(full_path, role="candidate file entry")
                    if not stat.S_ISREG(final_info.st_mode) or _entry_reparse_point(final_info):
                        raise InventoryExecutionError(
                            f"candidate entry changed type during inventory: {full_path}"
                        )
                    candidates.append(
                        InventoryItem(
                            basename=basename,
                            raw_root=root,
                            relative_path=relative_path,
                            filename_result=parse_source_filename(basename),
                        )
                    )
                    if resolved_candidate != full_path:
                        raise InventoryExecutionError(
                            f"candidate path changed during inventory: {full_path}"
                        )
                seen_locations.add(location_key)
                continue

            skipped.append(
                SkippedFilesystemEntry(
                    basename=basename,
                    raw_root=root,
                    relative_path=relative_path,
                    code=SkippedEntryCode.NON_REGULAR_ENTRY,
                    reason="non-regular filesystem entry is outside the candidate contract",
                    candidate_suffix_match=basename.endswith(_CANDIDATE_SUFFIX),
                )
            )
            seen_locations.add(location_key)

        pending.extend(reversed(child_directories))

    return candidates, skipped


def _sort_key(item: InventoryItem | SkippedFilesystemEntry) -> tuple[str, str, str]:
    return (_path_key(item.raw_root), item.relative_path.as_posix(), item.basename)


def _build_collisions(candidates: tuple[InventoryItem, ...]) -> tuple[BasenameCollision, ...]:
    locations_by_name: dict[str, list[InventoryLocation]] = defaultdict(list)
    for item in candidates:
        locations_by_name[item.basename].append(
            InventoryLocation(raw_root=item.raw_root, relative_path=item.relative_path)
        )

    collisions = []
    for basename, locations in locations_by_name.items():
        if len(locations) > 1:
            ordered_locations = tuple(
                sorted(
                    locations,
                    key=lambda location: (
                        _path_key(location.raw_root),
                        location.relative_path.as_posix(),
                    ),
                )
            )
            collisions.append(BasenameCollision(basename=basename, locations=ordered_locations))
    return tuple(sorted(collisions, key=lambda collision: collision.basename))


def inventory_raw_roots(config: RuntimeConfig) -> InventoryResult:
    """Inventory `.csv.gz` filesystem entries without opening their contents.

    A result is complete only for the configured roots observed during this
    call. Any traversal or entry-classification error raises
    :class:`InventoryExecutionError`; unrecognized candidate names are findings.
    """
    candidates: list[InventoryItem] = []
    skipped: list[SkippedFilesystemEntry] = []
    roots = tuple(sorted(config.raw_roots, key=_path_key))
    for raw_root in roots:
        root_candidates, root_skipped = _inventory_one_root(raw_root)
        candidates.extend(root_candidates)
        skipped.extend(root_skipped)

    ordered_candidates = tuple(sorted(candidates, key=_sort_key))
    ordered_skipped = tuple(sorted(skipped, key=_sort_key))
    collisions = _build_collisions(ordered_candidates)
    result = InventoryResult(
        contract_id=INVENTORY_CONTRACT_ID,
        configured_roots=roots,
        candidates=ordered_candidates,
        skipped_entries=ordered_skipped,
        basename_collisions=collisions,
    )
    if (
        result.total_candidate_count
        != result.recognized_candidate_count + result.unrecognized_candidate_count
    ):
        raise InventoryExecutionError("candidate count reconciliation failed")
    return result


def _candidate_to_dict(item: InventoryItem) -> dict[str, object]:
    parsed = item.filename_result
    details: dict[str, object] = {
        "contract_id": parsed.contract_id,
        "original_basename": parsed.original_basename,
    }
    if isinstance(parsed, RecognizedSourceFilename):
        details.update(
            {
                "source_kind": parsed.source_kind.value,
                "expansion_token": parsed.expansion_token,
                "format_token": parsed.format_token,
            }
        )
    elif isinstance(parsed, UnrecognizedSourceFilename):
        details.update(
            {
                "diagnostic_code": parsed.diagnostic_code.value,
                "reason": parsed.reason,
            }
        )
    return {
        "basename": item.basename,
        "disposition": item.disposition.value,
        "filename_classification": details,
        "raw_root": str(item.raw_root),
        "relative_path": item.relative_path.as_posix(),
    }


def _skipped_to_dict(item: SkippedFilesystemEntry) -> dict[str, object]:
    return {
        "basename": item.basename,
        "candidate_suffix_match": item.candidate_suffix_match,
        "diagnostic_code": item.code.value,
        "raw_root": str(item.raw_root),
        "reason": item.reason,
        "relative_path": item.relative_path.as_posix(),
    }
