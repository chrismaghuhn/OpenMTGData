# OpenMTGData

OpenMTGData is early infrastructure for building reproducible, provenance-preserving, model-independent Magic: The Gathering datasets. The first intended source is the intentionally published 17Lands Public Datasets. The package can inventory candidate `.csv.gz` filesystem entries, classify canonical basenames, register exact compressed-byte identities with streaming SHA-256, build a versioned source catalog with a semantic source-set digest, validate gzip integrity, inspect CSV headers, collect bounded-prefix lexical/row-width evidence, classify observed raw-schema drift, maintain a deterministic schema registry, and perform evidence-gated Replay and Game source-fact normalization.

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
openmtgdata manifest --help
openmtgdata inspect-headers --help
openmtgdata inspect-deep --help
openmtgdata classify-schemas --help
```

Inventory and registration require explicit `--raw-root` (repeatable), `--base-dir`, `--intermediate-root`, `--quarantine-root`, and `--release-root` options. The writable roots are validated but not created or written. Both commands emit deterministic JSON to stdout with paths labeled runtime-local. Inventory does not open candidate contents. Registration streams the exact compressed bytes to compute SHA-256; it does not decompress or validate gzip, parse CSV, inspect schemas, or determine source licenses. Results cover only the configured roots observed during traversal, not the global 17Lands publication.

Registration reads sequentially in bounded 4 MiB chunks. The v1 `source_archive_id` is SHA-256 over canonical compact UTF-8 JSON containing only its ID contract, provider namespace, and compressed-byte SHA-256; filenames, local paths, size, timestamps, and tool versions are excluded from that ID. File identity/size/time metadata is checked around the read for mutation detection, subject to filesystem race limitations.

`openmtgdata manifest` separates the full runtime-local audit catalog from a canonical semantic source-set projection. Its v1 selection rule includes an exact supplied source URL, license status/identifier, and recognized filename tokens; generic source evidence notes and license evidence references remain audit-only. Use `--validate-compression` to stream and discard decompressed bytes, then verify the registered compressed-byte identity through the M2.3 reader; this does not parse CSV or inspect schemas. Compression evidence is separate and never mutates `SourceArchiveRecordV1`. The source-level publication gate reports unreviewed/unknown source evidence as blocked without making manifest generation itself fail. No final dataset release is produced.

`openmtgdata inspect-headers` reuses M2.4 gzip validation first, then checks each source's registered byte identity and uses a fixed strict comma-separated UTF-8 (optional BOM) policy to parse only the first logical CSV record. Gzip validation streams and discards decompressed bytes for integrity only; it does not parse or interpret CSV records. Header inspection records ordered fields, encoding/parser observations, line endings, and a versioned `raw_schema_fingerprint`; group identity is source kind plus that fingerprint. A bounded 8 KiB reader may read ahead internally, but no later CSV record is interpreted or included in header evidence.

`openmtgdata inspect-deep` reuses M2.3–M2.5 identities/evidence, validates gzip integrity with M2.4, and interprets up to 256 data records per archive by default. It records only aggregate lexical classes, empty-field counts, lengths, and row-width evidence; it never stores row values or assigns semantic types/null meaning. It inspects every configured registered archive and selects one deterministic representative per raw schema group. An optional `--output` writes a detailed report only as a new file under `intermediate_root`; otherwise stdout contains a bounded summary. This evidence does not establish schema compatibility, source interpretations, Replay/Game joins, or chronology.

M4.1's `openmtgdata.source_reader` API accepts an immutable registered source record and a digest-verified M3.2 registry. Reader contract v2 distinguishes direct gzip CSV from a single-member gzip-TAR CSV container using an explicit source-container policy, then recomputes the physical CSV header fingerprint and streams the selected CSV forward into bounded batches of untyped strings with deterministic data-record ordinals. It preserves exact parsed field values, rejects short/long rows without padding or truncation, and fails closed on unsafe parsing, container/gzip integrity, registry, or source-identity errors. Registry verification is independent of one corpus snapshot; callers can optionally pin registry, source-catalog, and M3.1 evidence digests for a reproducible run. Batches are provisional until the reader is fully exhausted and the selected member, TAR trailer, gzip stream, and registered compressed SHA-256/size checks complete. The reader does not normalize Magic data, reconstruct gameplay, join Replay/Game, extract decisions, checkpoint/resume, build Parquet, or train models. See [ADR 0004](docs/adr/0004-csv-container-detection-v1.md) for the corrected source-container evidence and policy.

M4.2's `openmtgdata.resumable_reader` wraps that reader with deterministic restart/re-stream checkpoints stored only beneath an explicitly supplied `intermediate_root` location. It always restarts gzip at byte zero, reparses and verifies the header, then replays committed batches to verify their prefix digest before continuing. A delivered batch is not committed until the caller durably writes it and explicitly calls `commit_batch(unit_id)`. An uncommitted unit may be delivered again after a crash with the same unit ID; committed units are skipped after verified re-stream. This provides exactly-once committed unit identity, not exactly-once callback execution; external sinks must use unit IDs for idempotence. A run is complete only after the M4.1 reader reaches EOF and verifies gzip integrity and the registered compressed-byte digest/size, after which a separate atomic completion marker is written. Incomplete work is provisional. One checkpoint namespace is bound to one run identity; simultaneous writers to the same checkpoint are unsupported. Arbitrary gzip byte-offset resume, distributed processing, normalization, joins, Parquet, and training remain unsupported.

M5.1 adds evidence-gated Replay normalization through `ReplayAdapterV1`. For the reviewed AFR PremierDraft raw fingerprint, an accepted raw row is treated as a game-level summary with indexed per-side turn-summary slots; each emitted `ReplayEventV1` is one such slot, not an individual action. Exact source strings and column-index lineage are preserved, while turn count, source `on_play`, slot side, and slot index use explicit deterministic rules. After correcting gzip-TAR CSV selection, the configured corpus has 40 Replay raw-schema groups; the current registry supports the reviewed AFR fingerprint and the other 39 groups remain `needs_review`. M5.1 does not join Game data, identify decisions, reconstruct authoritative game state, assert action legality/optimality, create `DecisionSampleV1`, build Parquet, or train models. See [ADR 0002](docs/adr/0002-replay-normalization-v1.md) for the evidence boundary and full-stream limitations.

M5.2 adds `GameRecordV1` source-fact normalization for independently reviewed Game schemas. After correcting gzip-TAR CSV selection, the configured corpus has 35 Game raw-schema groups; the current M5.2 registry supports only the AFR PremierDraft Game schema and the other 34 groups remain `needs_review`. Mapped fields preserve exact source strings with index-and-name lineage. Source-native identifiers are candidates only and are not approved join keys; source-relative player/outcome fields are not converted to SELF/OPPONENT or policy inputs. Replay/Game joins belong to M6 and actor-relative policy views to M7. No `DecisionSampleV1`, legal/optimal labels, Parquet release, or training is implemented. See [ADR 0003](docs/adr/0003-game-normalization-v1.md).

M6.1 records exact Replay/Game join-key evidence for the AFR PremierDraft archive pair only. It measures null/empty components, uniqueness, cross-source cardinality, unmatched/ambiguous rows, and M5 Replay survival without emitting joined records. No candidate is an approved join key unless both source semantics and scoped cardinality are established; M6.2 join output is not implemented. See [ADR 0005](docs/adr/0005-replay-game-join-evidence-v1.md).

M7.1 adds an evidence-only field/family safety matrix and decision/perspective report for the corrected AFR PremierDraft Replay archive. The current source supports game-level rows with per-side turn-summary slots, but not a source-backed individual action boundary, decision actor, exact before-state, or safe behavior-cloning input. Potential activity, outcome, hand-like, and future-slot fields remain explicitly unsafe or unresolved. M7.1 does not emit `DecisionSampleV1`, use Game data, infer SELF/OPPONENT, reconstruct targets/legal actions, or run training. The bounded report and redacted annotated examples are ignored under `data/intermediate/m7.1/`; see [ADR 0006](docs/adr/0006-replay-decision-perspective-evidence-v1.md).

M7.2 adds a separate, model-agnostic source turn-summary sequence view for the reviewed AFR PremierDraft Replay mapping. It predicts the next completed source turn summary from earlier summaries, stores each frame once, and describes virtual views by source-row locator and target ordinal rather than duplicating prefixes. Its allowlisted frame projection excludes row-global turn counts, outcomes, totals, and future frames. `source_turn_side=user|oppo` remains source-relative; every sequence/view declares `usable_for_policy_imitation=false` and `actor_perspective_safe=false`. The build writes ignored gzip JSONL sequence intermediates under `data/intermediate/m7.2/`; it does not emit `DecisionSampleV1`, join Game data, tokenize for Laya, build Parquet, or train a model. See [ADR 0007](docs/adr/0007-turn-summary-sequence-training-v1.md).

`openmtgdata classify-schemas --deep-report <path>` validates and analyzes persisted M3.1 evidence without opening raw archives. It produces physical drift assessments and versioned, source-kind-specific interpretation contracts. The contracts define structural support only, not semantic field mappings. Every observed fingerprint remains registered, with explicit fail-closed rules for unregistered fields, reorders, removals, ambiguous names, and row-width mismatches. An optional `--output` writes a new registry under `intermediate_root` without overwriting an existing file.

The durable `SourceArchiveRecordV1` uses the SPEC field names `source_archive_record_schema_id` and `original_filename`, records `provider` separately from `provider_namespace`, and carries source URL/evidence, license-review, acquisition, and ingestion metadata with explicit `unknown` defaults. Local registration does not assert a license or fabricate acquisition/ingestion timestamps.

Registration rejects observable symlink/reparse paths, uses `O_NOFOLLOW` where the platform provides it, and compares path metadata with the opened file descriptor before reading and again afterward. Python's standard library cannot make path validation and opening an atomic filesystem snapshot on every supported platform/filesystem; changes in the residual interval or filesystems without reliable file IDs/timestamps may not be observable. A successful record means the exact bytes read matched the stable metadata visible to these checks, not a transactional filesystem guarantee.

Inventory does not create a transactional filesystem snapshot. An observed traversal error or candidate disappearing during classification fails the run; filesystem changes after an entry has been observed cannot always be detected. Observable symlink/reparse entries are reported and not followed. Alias mechanisms Python cannot identify remain outside this guarantee.

No dataset files are needed to install, import, or run these commands. The license for OpenMTGData source code has not yet been decided; it is separate from the licenses applicable to future input datasets.

## Runtime filesystem roots

`openmtgdata.config.RuntimeConfig` accepts one or more raw input roots and separate intermediate, quarantine, and release roots. Raw roots may be outside the repository. Relative roots are resolved against an explicit absolute `base_dir`; configuration does not depend implicitly on the process working directory. Raw roots must already exist as directories, while writable roots may be configured before creation.

Configuration validation is read-only and does not inspect directory contents or dataset files. It does not create output directories. Canonical path resolution accounts for observable symlink/junction aliases, and all raw and writable roots must be mutually disjoint directory trees. The `runtime_config_digest` is SHA-256 over compact, sorted-key UTF-8 JSON using the `openmtgdata.runtime-config.v1` schema, platform-normalized resolved paths, and a sorted/deduplicated raw-root set. It identifies machine-local runtime paths for local execution/checkpoint use; it is not a source identity or dataset release identity, and Windows and POSIX paths are not treated as equivalent. This validation describes the filesystem topology visible at construction time; callers should validate again immediately before a later stage writes. Directory aliases that Python cannot observe through normal path resolution, such as some mount configurations or topology changes after validation, are outside this guarantee.
