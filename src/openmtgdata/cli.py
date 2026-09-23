"""Minimal command-line entry point for the OpenMTGData package."""

import argparse
import json
import sys
from collections.abc import Sequence

from openmtgdata import __version__
from openmtgdata.config import ConfigurationError, RuntimeConfig
from openmtgdata.inventory import InventoryExecutionError, inventory_raw_roots


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openmtgdata",
        description="Model-independent MTG dataset infrastructure.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    inventory_parser = subparsers.add_parser(
        "inventory",
        help="list candidate .csv.gz filenames under configured raw roots",
        description=(
            "Inventory candidate filenames only. Archive contents are not opened or validated."
        ),
    )
    inventory_parser.add_argument(
        "--raw-root",
        action="append",
        required=True,
        help="existing raw input root; may be repeated",
    )
    inventory_parser.add_argument(
        "--base-dir",
        required=True,
        help="existing absolute base directory for relative root paths",
    )
    inventory_parser.add_argument(
        "--intermediate-root",
        required=True,
        help="reserved writable root required by RuntimeConfig; not written by inventory",
    )
    inventory_parser.add_argument(
        "--quarantine-root",
        required=True,
        help="reserved writable root required by RuntimeConfig; not written by inventory",
    )
    inventory_parser.add_argument(
        "--release-root",
        required=True,
        help="reserved writable root required by RuntimeConfig; not written by inventory",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a conventional process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "inventory":
        parser.print_help()
        return 0

    try:
        config = RuntimeConfig(
            raw_roots=args.raw_root,
            intermediate_root=args.intermediate_root,
            quarantine_root=args.quarantine_root,
            release_root=args.release_root,
            base_dir=args.base_dir,
        )
        result = inventory_raw_roots(config)
    except (ConfigurationError, InventoryExecutionError) as exc:
        print(f"openmtgdata: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0
