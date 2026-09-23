# OpenMTGData

OpenMTGData is early infrastructure for building reproducible, provenance-preserving, model-independent Magic: The Gathering datasets. The first intended source is the intentionally published 17Lands Public Datasets. The package can inventory candidate `.csv.gz` filesystem entries, classify canonical basenames, and register exact compressed-byte identities with streaming SHA-256. It does not validate gzip/CSV contents, inspect or support any source schema, interpret replay/game semantics, verify provenance URLs or licenses, build a dataset, or publish a public dataset.

Large raw archives and derived datasets do not belong in Git. Raw input roots may live outside the repository and will be supplied explicitly to future tools. The repository-relative `data/raw/17lands/` location is only a possible convenience default. OpenMTGData is independent of any particular model, including Laya and MageZero. An observed human action, if represented by a future view, describes behavior and is not an optimal-action claim.

The architecture authority is [SPEC.md](SPEC.md); the staged implementation plan is [PLAN.md](PLAN.md).

## Development

Use Python 3.11 or newer. From a checkout, install the package and development tools:

```bash
python -m pip install -e ".[dev]"
```

Run the checks:

```bash
python -m pytest
ruff check .
ruff format --check .
mypy src/openmtgdata
```

The CLI supports help, version, filename-only local inventory, and exact-byte registration:

```bash
openmtgdata --help
openmtgdata --version
python -m openmtgdata --help
python -m openmtgdata --version
openmtgdata inventory --help
openmtgdata register --help
```

Inventory and registration require explicit `--raw-root` (repeatable), `--base-dir`, `--intermediate-root`, `--quarantine-root`, and `--release-root` options. The writable roots are validated but not created or written. Both commands emit deterministic JSON to stdout with paths labeled runtime-local. Inventory does not open candidate contents. Registration streams the exact compressed bytes to compute SHA-256; it does not decompress or validate gzip, parse CSV, inspect schemas, or determine source licenses. Results cover only the configured roots observed during traversal, not the global 17Lands publication.

Registration reads sequentially in bounded 4 MiB chunks. The v1 `source_archive_id` is SHA-256 over canonical compact UTF-8 JSON containing only its ID contract, provider namespace, and compressed-byte SHA-256; filenames, local paths, size, timestamps, and tool versions are excluded from that ID. File identity/size/time metadata is checked around the read for mutation detection, subject to filesystem race limitations.

Registration rejects observable symlink/reparse paths, uses `O_NOFOLLOW` where the platform provides it, and compares path metadata with the opened file descriptor before reading and again afterward. Python's standard library cannot make path validation and opening an atomic filesystem snapshot on every supported platform/filesystem; changes in the residual interval or filesystems without reliable file IDs/timestamps may not be observable. A successful record means the exact bytes read matched the stable metadata visible to these checks, not a transactional filesystem guarantee.

Inventory does not create a transactional filesystem snapshot. An observed traversal error or candidate disappearing during classification fails the run; filesystem changes after an entry has been observed cannot always be detected. Observable symlink/reparse entries are reported and not followed. Alias mechanisms Python cannot identify remain outside this guarantee.

No dataset files are needed to install, import, or run these commands. The license for OpenMTGData source code has not yet been decided; it is separate from the licenses applicable to future input datasets.

## Runtime filesystem roots

`openmtgdata.config.RuntimeConfig` accepts one or more raw input roots and separate intermediate, quarantine, and release roots. Raw roots may be outside the repository. Relative roots are resolved against an explicit absolute `base_dir`; configuration does not depend implicitly on the process working directory. Raw roots must already exist as directories, while writable roots may be configured before creation.

Configuration validation is read-only and does not inspect directory contents or dataset files. It does not create output directories. Canonical path resolution accounts for observable symlink/junction aliases, and all raw and writable roots must be mutually disjoint directory trees. The `runtime_config_digest` is SHA-256 over compact, sorted-key UTF-8 JSON using the `openmtgdata.runtime-config.v1` schema, platform-normalized resolved paths, and a sorted/deduplicated raw-root set. It identifies machine-local runtime paths for local execution/checkpoint use; it is not a source identity or dataset release identity, and Windows and POSIX paths are not treated as equivalent. This validation describes the filesystem topology visible at construction time; callers should validate again immediately before a later stage writes. Directory aliases that Python cannot observe through normal path resolution, such as some mount configurations or topology changes after validation, are outside this guarantee.
