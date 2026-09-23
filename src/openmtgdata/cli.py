"""Minimal command-line entry point for the OpenMTGData package."""

import argparse
import json
import sys
from collections.abc import Sequence

from openmtgdata import __version__
from openmtgdata.archive_registration import (
    ArchiveRegistrationError,
    register_inventory_archives,
)
from openmtgdata.compression_validation import validate_registration_compression
from openmtgdata.config import ConfigurationError, RuntimeConfig
from openmtgdata.deep_inspection import (
    MAX_INTERPRETED_DATA_ROWS,
    DeepInspectionError,
    build_deep_inspection_report,
    write_report_no_overwrite,
)
from openmtgdata.header_inspection import (
    HeaderInspectionExecutionError,
    build_header_inventory,
)
from openmtgdata.inventory import InventoryExecutionError, inventory_raw_roots
from openmtgdata.source_manifest import SourceManifestError, build_source_manifest


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


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
    _add_runtime_root_arguments(inventory_parser, writable_root_context="inventory")
    register_parser = subparsers.add_parser(
        "register",
        help="register exact compressed bytes with streaming SHA-256",
        description=(
            "Register candidate file bytes only. Files are not decompressed or interpreted."
        ),
    )
    _add_runtime_root_arguments(register_parser, writable_root_context="registration")
    manifest_parser = subparsers.add_parser(
        "manifest",
        help="build a source catalog and semantic source-set identity",
        description=(
            "Build a source-only catalog. Compression validation is opt-in; no CSV/schema "
            "or dataset release work is performed."
        ),
    )
    _add_runtime_root_arguments(manifest_parser, writable_root_context="manifest")
    manifest_parser.add_argument(
        "--validate-compression",
        action="store_true",
        help="stream gzip integrity validation for each registered source archive",
    )
    header_parser = subparsers.add_parser(
        "inspect-headers",
        help="inspect only the first CSV record for every registered source archive",
        description=(
            "Build a source catalog, then inspect exactly one CSV header record per source. "
            "No data rows are interpreted, and no output is written to disk."
        ),
    )
    _add_runtime_root_arguments(header_parser, writable_root_context="header inspection")
    deep_parser = subparsers.add_parser(
        "inspect-deep",
        help="collect bounded row evidence for all sources and designate schema representatives",
        description=(
            "Reuse archive registration, gzip validation, and M2.5 header evidence, then inspect "
            "a bounded data-row prefix. Raw row values are never retained."
        ),
    )
    _add_runtime_root_arguments(deep_parser, writable_root_context="deep inspection")
    deep_parser.add_argument(
        "--max-data-rows",
        type=_non_negative_int,
        default=MAX_INTERPRETED_DATA_ROWS,
        help=(
            "maximum data CSV records interpreted per archive "
            f"(default: {MAX_INTERPRETED_DATA_ROWS})"
        ),
    )
    deep_parser.add_argument(
        "--output",
        help=(
            "optional new detailed JSON report under intermediate_root; "
            "existing files are never overwritten"
        ),
    )
    return parser


def _add_runtime_root_arguments(
    parser: argparse.ArgumentParser,
    *,
    writable_root_context: str,
) -> None:
    parser.add_argument(
        "--raw-root",
        action="append",
        required=True,
        help="existing raw input root; may be repeated",
    )
    parser.add_argument(
        "--base-dir",
        required=True,
        help="existing absolute base directory for relative root paths",
    )
    parser.add_argument(
        "--intermediate-root",
        required=True,
        help=f"reserved RuntimeConfig writable root; not written by {writable_root_context}",
    )
    parser.add_argument(
        "--quarantine-root",
        required=True,
        help=f"reserved RuntimeConfig writable root; not written by {writable_root_context}",
    )
    parser.add_argument(
        "--release-root",
        required=True,
        help=f"reserved RuntimeConfig writable root; not written by {writable_root_context}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a conventional process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"inventory", "register", "manifest", "inspect-headers", "inspect-deep"}:
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
        if args.command == "inventory":
            report = inventory_raw_roots(config).to_dict()
        elif args.command == "register":
            inventory = inventory_raw_roots(config)
            report = register_inventory_archives(inventory).to_dict()
        elif args.command == "manifest":
            inventory = inventory_raw_roots(config)
            registration = register_inventory_archives(inventory)
            compression_evidence = (
                validate_registration_compression(registration)
                if args.validate_compression
                else None
            )
            report = build_source_manifest(
                registration,
                compression_evidence=compression_evidence,
            ).to_dict()
        elif args.command == "inspect-headers":
            inventory = inventory_raw_roots(config)
            registration = register_inventory_archives(inventory)
            compression_evidence = validate_registration_compression(registration)
            manifest = build_source_manifest(
                registration,
                compression_evidence=compression_evidence,
            )
            header_inventory = build_header_inventory(
                registration,
                source_manifest=manifest,
                configured_raw_roots=config.raw_roots,
            )
            report = {
                "header_inventory": header_inventory.to_dict(),
                "source_manifest": manifest.to_dict(),
            }
        else:
            inventory = inventory_raw_roots(config)
            registration = register_inventory_archives(inventory)
            compression_evidence = validate_registration_compression(registration)
            manifest = build_source_manifest(
                registration,
                compression_evidence=compression_evidence,
            )
            header_inventory = build_header_inventory(
                registration,
                source_manifest=manifest,
                configured_raw_roots=config.raw_roots,
            )
            deep_report = build_deep_inspection_report(
                registration,
                manifest,
                header_inventory,
                max_data_rows=args.max_data_rows,
                tool_identity=f"openmtgdata/{__version__}",
            )
            report = deep_report.summary_dict()
            if args.output is not None:
                report["detailed_report"] = {
                    "path_scope": "runtime_local",
                    "path": str(
                        write_report_no_overwrite(
                            deep_report,
                            args.output,
                            config,
                            base_dir=args.base_dir,
                        )
                    ),
                }
    except (
        ConfigurationError,
        InventoryExecutionError,
        ArchiveRegistrationError,
        SourceManifestError,
        HeaderInspectionExecutionError,
        DeepInspectionError,
    ) as exc:
        print(f"openmtgdata: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 0
