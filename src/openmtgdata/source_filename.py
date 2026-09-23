"""Lexical recognition of canonical 17Lands public-dump basenames."""

from dataclasses import dataclass
from enum import StrEnum

FILENAME_CONTRACT_ID = "openmtgdata.17lands-filename.v1"
_SUFFIX = ".csv.gz"


class SourceKind(StrEnum):
    """Recognized 17Lands public dataset families."""

    REPLAY = "replay"
    GAME = "game"
    DRAFT = "draft"


_PREFIXES: tuple[tuple[str, SourceKind], ...] = (
    ("replay_data_public.", SourceKind.REPLAY),
    ("game_data_public.", SourceKind.GAME),
    ("draft_data_public.", SourceKind.DRAFT),
)


class FilenameDiagnosticCode(StrEnum):
    """Stable diagnostic categories for basenames outside the v1 grammar."""

    EMPTY_NAME = "EMPTY_NAME"
    CONTAINS_PATH_SEPARATOR = "CONTAINS_PATH_SEPARATOR"
    INVALID_CHARACTER = "INVALID_CHARACTER"
    UNRECOGNIZED_PREFIX = "UNRECOGNIZED_PREFIX"
    INVALID_SUFFIX = "INVALID_SUFFIX"
    MISSING_EXPANSION = "MISSING_EXPANSION"
    MISSING_FORMAT = "MISSING_FORMAT"
    UNEXPECTED_STRUCTURE = "UNEXPECTED_STRUCTURE"


@dataclass(frozen=True, slots=True)
class RecognizedSourceFilename:
    """A basename matching the versioned canonical filename grammar."""

    contract_id: str
    original_basename: str
    source_kind: SourceKind
    expansion_token: str
    format_token: str


@dataclass(frozen=True, slots=True)
class UnrecognizedSourceFilename:
    """An unrecognized basename with a stable machine and human diagnostic."""

    contract_id: str
    original_basename: str
    diagnostic_code: FilenameDiagnosticCode
    reason: str


FilenameParseResult = RecognizedSourceFilename | UnrecognizedSourceFilename

_REASONS = {
    FilenameDiagnosticCode.EMPTY_NAME: "filename is empty",
    FilenameDiagnosticCode.CONTAINS_PATH_SEPARATOR: "expected a basename, not a path",
    FilenameDiagnosticCode.INVALID_CHARACTER: "filename contains a NUL character",
    FilenameDiagnosticCode.UNRECOGNIZED_PREFIX: "filename does not use a canonical source prefix",
    FilenameDiagnosticCode.INVALID_SUFFIX: "filename must end with the exact suffix '.csv.gz'",
    FilenameDiagnosticCode.MISSING_EXPANSION: "filename is missing the expansion token",
    FilenameDiagnosticCode.MISSING_FORMAT: "filename is missing the format token",
    FilenameDiagnosticCode.UNEXPECTED_STRUCTURE: (
        "filename does not match the canonical token structure"
    ),
}


def _unrecognized(name: str, code: FilenameDiagnosticCode) -> UnrecognizedSourceFilename:
    return UnrecognizedSourceFilename(
        contract_id=FILENAME_CONTRACT_ID,
        original_basename=name,
        diagnostic_code=code,
        reason=_REASONS[code],
    )


def parse_source_filename(name: str) -> FilenameParseResult:
    """Classify a supplied basename without accessing the filesystem.

    The grammar is exactly ``<kind>_data_public.<EXPANSION>.<FORMAT>.csv.gz``
    for the lowercase structural prefixes ``replay``, ``game``, and ``draft``.
    Expansion and format are opaque, exact, non-empty dot-delimited tokens.
    """
    if name == "":
        return _unrecognized(name, FilenameDiagnosticCode.EMPTY_NAME)
    if "/" in name or "\\" in name:
        return _unrecognized(name, FilenameDiagnosticCode.CONTAINS_PATH_SEPARATOR)
    if "\x00" in name:
        return _unrecognized(name, FilenameDiagnosticCode.INVALID_CHARACTER)

    matched_prefix: tuple[str, SourceKind] | None = None
    for prefix, source_kind in _PREFIXES:
        if name.startswith(prefix):
            matched_prefix = (prefix, source_kind)
            break
    if matched_prefix is None:
        return _unrecognized(name, FilenameDiagnosticCode.UNRECOGNIZED_PREFIX)

    if not name.endswith(_SUFFIX):
        return _unrecognized(name, FilenameDiagnosticCode.INVALID_SUFFIX)

    prefix, source_kind = matched_prefix
    token_text = name[len(prefix) : -len(_SUFFIX)]
    tokens = token_text.split(".")
    if len(tokens) != 2:
        return _unrecognized(name, FilenameDiagnosticCode.UNEXPECTED_STRUCTURE)

    expansion_token, format_token = tokens
    if expansion_token == "":
        return _unrecognized(name, FilenameDiagnosticCode.MISSING_EXPANSION)
    if format_token == "":
        return _unrecognized(name, FilenameDiagnosticCode.MISSING_FORMAT)

    return RecognizedSourceFilename(
        contract_id=FILENAME_CONTRACT_ID,
        original_basename=name,
        source_kind=source_kind,
        expansion_token=expansion_token,
        format_token=format_token,
    )
