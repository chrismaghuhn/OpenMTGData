"""Minimal command-line entry point for the OpenMTGData package."""

import argparse
from collections.abc import Sequence

from openmtgdata import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openmtgdata",
        description="OpenMTGData package scaffold. Dataset operations are not implemented yet.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scaffold CLI and return a conventional process exit code."""
    parser = _build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
