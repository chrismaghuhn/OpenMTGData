"""Runtime filesystem configuration and read-only root safety validation.

Runtime configuration identity binds canonical, machine-local paths for safe stage
and checkpoint use. It is not a source identity or dataset release identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PathInput = str | os.PathLike[str]


class ConfigurationError(ValueError):
    """Base class for invalid runtime configuration."""


class NoRawRootsError(ConfigurationError):
    """Raised when no input-only raw roots are configured."""


class InvalidBaseDirectoryError(ConfigurationError):
    """Raised when the explicit base directory is not an existing directory."""


class RawRootMissingError(ConfigurationError):
    """Raised when a configured raw input root does not exist."""


class RawRootNotDirectoryError(ConfigurationError):
    """Raised when a configured raw input root is not a directory."""


class WritableRootNotDirectoryError(ConfigurationError):
    """Raised when an existing writable root is not a directory."""


class InvalidRootPathError(ConfigurationError):
    """Raised when a root cannot be resolved safely."""


class RootOverlapError(ConfigurationError):
    """Raised when configured directory roots are equal or nested."""


def _path_text(path: Path) -> str:
    """Return platform-normalized text for a canonical path."""
    return os.path.normcase(os.fspath(path))


def _resolve_base_directory(value: PathInput) -> Path:
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            raise InvalidBaseDirectoryError("base_dir must be an absolute path")
        resolved = candidate.resolve(strict=True)
    except InvalidBaseDirectoryError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise InvalidBaseDirectoryError(f"base_dir cannot be resolved: {value!s}") from exc

    if not resolved.is_dir():
        raise InvalidBaseDirectoryError(f"base_dir is not a directory: {resolved}")
    return resolved


def _reject_dangling_symlink(candidate: Path, *, role: str) -> None:
    """Reject broken symlinks in a proposed path before resolving it."""
    current = candidate
    while True:
        try:
            if current.is_symlink() and not current.exists():
                raise InvalidRootPathError(
                    f"{role} contains a dangling or unresolvable symlink: {current}"
                )
        except OSError as exc:
            raise InvalidRootPathError(f"{role} cannot be inspected: {current}") from exc

        if current.parent == current:
            return
        current = current.parent


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current


def _resolve_root(
    value: PathInput,
    *,
    role: str,
    base_dir: Path,
    must_exist: bool,
) -> Path:
    try:
        text = os.fspath(value)
        if not isinstance(text, str) or text == "":
            raise InvalidRootPathError(f"{role} must be a non-empty text path")
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = base_dir / candidate

        if not must_exist:
            _reject_dangling_symlink(candidate, role=role)
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        if must_exist and isinstance(exc, FileNotFoundError):
            raise RawRootMissingError(f"{role} does not exist: {value!s}") from exc
        if isinstance(exc, ConfigurationError):
            raise
        raise InvalidRootPathError(f"{role} cannot be resolved: {value!s}") from exc

    if must_exist:
        if not resolved.is_dir():
            raise RawRootNotDirectoryError(f"{role} is not a directory: {resolved}")
        return resolved

    if resolved.exists():
        if not resolved.is_dir():
            raise WritableRootNotDirectoryError(f"{role} exists and is not a directory: {resolved}")
    else:
        existing_ancestor = _nearest_existing_path(resolved)
        if not existing_ancestor.is_dir():
            raise InvalidRootPathError(
                f"{role} has an existing non-directory ancestor: {existing_ancestor}"
            )
    return resolved


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def _overlap(left: Path, right: Path) -> bool:
    return _contains(left, right) or _contains(right, left)


@dataclass(frozen=True, slots=True, init=False)
class RuntimeConfig:
    """Canonical, read-only configuration for local filesystem roots.

    Relative roots are interpreted against the required absolute ``base_dir``.
    Raw roots must already exist as directories. Writable roots may be absent;
    validating this object never creates directories or files. Equivalent raw
    roots are deduplicated, then all configured roots must occupy disjoint trees.
    """

    raw_roots: tuple[Path, ...]
    intermediate_root: Path
    quarantine_root: Path
    release_root: Path

    def __init__(
        self,
        *,
        raw_roots: Iterable[PathInput],
        intermediate_root: PathInput,
        quarantine_root: PathInput,
        release_root: PathInput,
        base_dir: PathInput,
    ) -> None:
        if isinstance(raw_roots, (str, os.PathLike)):
            raise ConfigurationError("raw_roots must be a collection of paths")

        base = _resolve_base_directory(base_dir)
        try:
            raw_values = tuple(raw_roots)
        except TypeError as exc:
            raise ConfigurationError("raw_roots must be an iterable of paths") from exc
        if not raw_values:
            raise NoRawRootsError("at least one raw input root must be configured")

        resolved_raw = (
            _resolve_root(value, role=f"raw_roots[{index}]", base_dir=base, must_exist=True)
            for index, value in enumerate(raw_values)
        )
        unique_raw: dict[str, Path] = {}
        for root in resolved_raw:
            unique_raw.setdefault(_path_text(root), root)
        canonical_raw = tuple(sorted(unique_raw.values(), key=_path_text))

        writable_values = (
            ("intermediate_root", intermediate_root),
            ("quarantine_root", quarantine_root),
            ("release_root", release_root),
        )
        writable = tuple(
            (
                role,
                _resolve_root(value, role=role, base_dir=base, must_exist=False),
            )
            for role, value in writable_values
        )

        named_roots = tuple(
            (f"raw_roots[{index}]", root) for index, root in enumerate(canonical_raw)
        )
        all_roots = named_roots + writable
        for index, (left_role, left_path) in enumerate(all_roots):
            for right_role, right_path in all_roots[index + 1 :]:
                if _overlap(left_path, right_path):
                    raise RootOverlapError(
                        f"{left_role} ({left_path}) overlaps {right_role} ({right_path}); "
                        "configured roots must be mutually disjoint directory trees"
                    )

        object.__setattr__(self, "raw_roots", canonical_raw)
        object.__setattr__(self, "intermediate_root", writable[0][1])
        object.__setattr__(self, "quarantine_root", writable[1][1])
        object.__setattr__(self, "release_root", writable[2][1])

    @property
    def canonical_bytes(self) -> bytes:
        """Return versioned compact UTF-8 JSON for the runtime-only identity."""
        projection: dict[str, object] = {
            "intermediate_root": _path_text(self.intermediate_root),
            "path_flavor": "windows" if os.name == "nt" else "posix",
            "quarantine_root": _path_text(self.quarantine_root),
            "raw_roots": [_path_text(root) for root in self.raw_roots],
            "release_root": _path_text(self.release_root),
            "schema": "openmtgdata.runtime-config.v1",
        }
        return json.dumps(
            projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def runtime_config_digest(self) -> str:
        """Return SHA-256 of canonical runtime paths, not a dataset/release ID."""
        return hashlib.sha256(self.canonical_bytes).hexdigest()
