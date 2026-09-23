"""Tests for filesystem-independent 17Lands filename recognition."""

from __future__ import annotations

import builtins
import os
import socket
from pathlib import Path

import pytest

from openmtgdata.source_filename import (
    FILENAME_CONTRACT_ID,
    FilenameDiagnosticCode,
    RecognizedSourceFilename,
    SourceKind,
    UnrecognizedSourceFilename,
    parse_source_filename,
)


@pytest.mark.parametrize(
    ("name", "kind", "expansion", "format_name"),
    [
        ("game_data_public.AFR.PremierDraft.csv.gz", SourceKind.GAME, "AFR", "PremierDraft"),
        ("replay_data_public.AFR.TradDraft.csv.gz", SourceKind.REPLAY, "AFR", "TradDraft"),
        ("draft_data_public.AFR.PremierDraft.csv.gz", SourceKind.DRAFT, "AFR", "PremierDraft"),
        ("replay_data_public.BLB.TradSealed.csv.gz", SourceKind.REPLAY, "BLB", "TradSealed"),
        ("replay_data_public.MSH.PickTwoDraft.csv.gz", SourceKind.REPLAY, "MSH", "PickTwoDraft"),
        (
            "game_data_public.OM1.PickTwoTradDraft.csv.gz",
            SourceKind.GAME,
            "OM1",
            "PickTwoTradDraft",
        ),
        (
            "replay_data_public.Cube_-_Powered.PremierDraft.csv.gz",
            SourceKind.REPLAY,
            "Cube_-_Powered",
            "PremierDraft",
        ),
        (
            "replay_data_public.Cube_-_Powered.PickTwoTradDraft.csv.gz",
            SourceKind.REPLAY,
            "Cube_-_Powered",
            "PickTwoTradDraft",
        ),
    ],
)
def test_recognizes_canonical_filenames_without_normalizing_tokens(
    name: str,
    kind: SourceKind,
    expansion: str,
    format_name: str,
) -> None:
    result = parse_source_filename(name)

    assert isinstance(result, RecognizedSourceFilename)
    assert result.contract_id == FILENAME_CONTRACT_ID
    assert result.original_basename == name
    assert result.source_kind is kind
    assert result.expansion_token == expansion
    assert result.format_token == format_name


def test_opaque_tokens_are_preserved_without_unicode_normalization() -> None:
    expansion = "Cafe\u0301_-_Powered"
    name = f"replay_data_public.{expansion}.Format+Future.csv.gz"

    result = parse_source_filename(name)

    assert isinstance(result, RecognizedSourceFilename)
    assert result.expansion_token == expansion
    assert result.format_token == "Format+Future"


def test_repeated_parse_is_equal_and_deterministic() -> None:
    name = "game_data_public.Cube_-_Powered.FutureFormat.csv.gz"

    first = parse_source_filename(name)
    second = parse_source_filename(name)

    assert first == second


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("", FilenameDiagnosticCode.EMPTY_NAME),
        (
            "game_data_public..PremierDraft.csv.gz",
            FilenameDiagnosticCode.MISSING_EXPANSION,
        ),
        ("game_data_public.AFR..csv.gz", FilenameDiagnosticCode.MISSING_FORMAT),
        ("game_data_public.AFR.PremierDraft.csv", FilenameDiagnosticCode.INVALID_SUFFIX),
        ("game_data_public.AFR.PremierDraft.gz", FilenameDiagnosticCode.INVALID_SUFFIX),
        ("game_data_public.AFR.PremierDraft.csv.zip", FilenameDiagnosticCode.INVALID_SUFFIX),
        (
            "game_data_public.AFR.PremierDraft.csv.gz.bak",
            FilenameDiagnosticCode.INVALID_SUFFIX,
        ),
        ("foo_data_public.AFR.PremierDraft.csv.gz", FilenameDiagnosticCode.UNRECOGNIZED_PREFIX),
        (
            "Replay_data_public.AFR.PremierDraft.csv.gz",
            FilenameDiagnosticCode.UNRECOGNIZED_PREFIX,
        ),
        (
            "game_Data_public.AFR.PremierDraft.csv.gz",
            FilenameDiagnosticCode.UNRECOGNIZED_PREFIX,
        ),
        (
            "GAME_DATA_PUBLIC.AFR.PremierDraft.csv.gz",
            FilenameDiagnosticCode.UNRECOGNIZED_PREFIX,
        ),
        (
            "game_data_public.AFR.PremierDraft.CSV.GZ",
            FilenameDiagnosticCode.INVALID_SUFFIX,
        ),
        (
            "game_data_public.AFR.PremierDraft.extra.csv.gz",
            FilenameDiagnosticCode.UNEXPECTED_STRUCTURE,
        ),
    ],
)
def test_unrecognized_names_have_stable_diagnostics(
    name: str, code: FilenameDiagnosticCode
) -> None:
    result = parse_source_filename(name)

    assert isinstance(result, UnrecognizedSourceFilename)
    assert result.contract_id == FILENAME_CONTRACT_ID
    assert result.original_basename == name
    assert result.diagnostic_code is code
    assert result.reason
    assert parse_source_filename(name) == result


@pytest.mark.parametrize(
    "name",
    [
        "/data/game_data_public.AFR.PremierDraft.csv.gz",
        "folder\\game_data_public.AFR.PremierDraft.csv.gz",
        "game_data_public.AFR.PremierDraft.csv.gz/",
        "game_data_public.AFR.PremierDraft.csv.gz\\backup",
    ],
)
def test_paths_are_rejected_instead_of_reduced_to_basename(name: str) -> None:
    result = parse_source_filename(name)

    assert isinstance(result, UnrecognizedSourceFilename)
    assert result.diagnostic_code is FilenameDiagnosticCode.CONTAINS_PATH_SEPARATOR


def test_nul_is_rejected() -> None:
    result = parse_source_filename("game_data_public.AFR\x00.PremierDraft.csv.gz")

    assert isinstance(result, UnrecognizedSourceFilename)
    assert result.diagnostic_code is FilenameDiagnosticCode.INVALID_CHARACTER


def test_parser_performs_no_filesystem_or_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("parser attempted external I/O")

    with monkeypatch.context() as no_io:
        no_io.setattr(builtins, "open", forbidden)
        no_io.setattr(os, "stat", forbidden)
        no_io.setattr(os, "scandir", forbidden)
        no_io.setattr(os, "listdir", forbidden)
        no_io.setattr(os, "getcwd", forbidden)
        no_io.setattr(Path, "exists", forbidden)
        no_io.setattr(Path, "resolve", forbidden)
        no_io.setattr(Path, "stat", forbidden)
        no_io.setattr(Path, "iterdir", forbidden)
        no_io.setattr(socket, "socket", forbidden)
        no_io.setattr(socket, "create_connection", forbidden)

        result = parse_source_filename("replay_data_public.AFR.PremierDraft.csv.gz")

    assert isinstance(result, RecognizedSourceFilename)
