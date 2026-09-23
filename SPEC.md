# OpenMTGData Architecture Specification

**Status:** M0 proposal; normative for implementation once accepted.  
**Contract family:** v0.1  
**Scope:** documentation and architecture only. No source CSV schema is asserted here.

The terms **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative. A MUST requirement can be waived only by a documented, versioned decision and an explicit release limitation.

## 1. Purpose

OpenMTGData is an open, reproducible, provenance-preserving, model-agnostic pipeline and public dataset for large-scale Magic: The Gathering (MTG) machine-learning research. Its initial source is the intentionally published 17Lands Public Datasets. Its intended flow is immutable compressed source archives → deterministic ingestion → provenance-preserving normalized records → replay/game-derived views → validated Parquet shards → optional public dataset release.

The dataset is an infrastructure product useful to behavior cloning, offline reinforcement learning, sequence modeling, action prediction, representation learning, and future MTG engines and tools. Laya-style systems, MageZero, and Forge-related research are possible downstream consumers; none defines the core data contract.

## 2. Non-goals for v0.1

OpenMTGData v0.1 MUST NOT implement a Magic rules engine, assert move legality, determine optimal actions, reproduce Arena, train Laya, train MageZero, train any final model, scrape private or unsupported APIs, store card artwork, or commit large raw archives to Git. Draft Data can be discovered and catalogued, but draft-pick learning is outside the initial gameplay-decision dataset.

## 3. Source authority and licensing

The intentionally published 17Lands archive bytes are immutable source artifacts. No build stage may modify, normalize, rename in place, or overwrite them. Every accepted archive MUST be hashed with SHA-256 before processing; digest mismatch or unreadable compressed input is a fatal source error. Processing reads raw archives and writes elsewhere.

17Lands states that its Public Datasets are, unless otherwise noted, under Creative Commons Attribution 4.0 International (CC BY 4.0). This is a source-level starting point, not a blanket license assertion for every file or future source. Before each source is included in a public release, the release process MUST verify and record that source's applicable license/status, attribution wording, and source page/URL. A source of unknown or incompatible status MUST be held out of publication pending review. Derived releases MUST preserve provenance and attribution, identify changes, and MUST NOT imply endorsement by 17Lands. The dataset card MUST link to the source and the CC BY 4.0 legal code when applicable. This document is not legal advice.

Only deliberately published public dumps are in scope. Undocumented 17Lands APIs MUST NOT be used as an alternate source.

## 4. Source manifest contract

Each archive receives one record in a versioned `SourceManifestV1`. A canonical serialization and field ordering MUST be specified by the implementation before hashing the manifest. Required, optional, and derived data are distinguished below; unknown values remain null/unknown and MUST NOT be guessed.

| Field | Requirement | Meaning |
|---|---|---|
| `manifest_schema_id` | Required, fixed | `SourceManifestV1` |
| `source_archive_id` | Required, derived | Deterministic ID from the archive SHA-256 and provider namespace; never a random UUID |
| `provider` | Required | `17lands` for this source |
| `dataset_family` | Required | Public Datasets family as established by source documentation |
| `original_filename` | Required | Observed basename, preserved exactly |
| `source_url` | Required when reliably known | URL to the published file or authoritative listing; null with reason if not recoverable |
| `sha256` | Required | Digest of the exact compressed bytes |
| `compressed_bytes` | Required, derived | Byte length of exact compressed archive |
| `source_kind` | Required, parsed | `replay`, `game`, `draft`, or `unknown` |
| `expansion` | Required for recognized canonical filename | Exact filename token, not normalized by guess |
| `format` | Required for recognized canonical filename | Exact filename token |
| `source_published_at` / `source_updated_at` | Optional | Only when explicitly provided by authoritative source metadata |
| `license_identifier` / `license_status` | Required before publication; may be pending locally | Identifier and review state such as `verified`, `pending`, `restricted`, `unknown` |
| `ingested_at` | Required operational metadata | UTC timestamp of local registration; excluded from semantic dataset identity |
| `builder_version` | Required | Version/commit of the tool creating this manifest record |
| `source_schema_fingerprint` | Added after inspection | Deterministic identity of discovered header/type interpretation; absent before inspection |

The manifest MUST also preserve a diagnostic for unknown filename tokens, metadata gaps, and license review. The manifest MUST NOT represent missing values with fabricated defaults. `ingested_at` is audit metadata and MUST NOT affect semantic IDs or split membership.

## 5. Source filename discovery and pairing

Discovery MUST recognize the canonical public dump pattern, subject to verification against actual published files: `replay_data_public.<EXPANSION>.<FORMAT>.csv.gz`, `game_data_public.<EXPANSION>.<FORMAT>.csv.gz`, and `draft_data_public.<EXPANSION>.<FORMAT>.csv.gz`. Expansion and format tokens are opaque filename components until verified. Recognition MUST be deterministic and case-sensitive unless source evidence justifies a versioned rule.

Unknown files MUST be rejected as source inputs or catalogued with `source_kind=unknown` and a clear diagnostic; they MUST NOT silently pass as a recognized type. Filename proximity or directory order MUST NOT pair files. Replay/Game pairing requires matching verified expansion, format, and any additional source identity established during inspection, plus proven key semantics. Discovery catalogs Draft Data even when no downstream draft processing is enabled.

## 6. Source schema discovery gate

No source column names, types, identifier meanings, event semantics, or join keys are fixed by this specification. The exact CSV schemas MUST be inspected from real downloaded archives before a source adapter or normalized field mapping is locked down. Inspection MUST cover representative early and recent archives, replay and game families, and each observed schema variant.

Inspection reports MUST capture ordered headers, observed nullability/type evidence, encoding and CSV dialect behavior where relevant, row samples or safe summaries, archive identity, and a deterministic schema fingerprint. A fingerprint MUST include at least the ordered header names and the versioned interpretation rules; the exact fingerprint encoding is an implementation decision. Unexpected incompatible drift MUST fail clearly. Additive compatible evolution MAY be accepted only under an explicit, versioned compatibility policy; ignored columns and their disposition MUST be reported. A column being present does not prove it is safe, complete, or semantically stable.

## 7. Processing architecture

Stages have distinct authority and outputs:

1. **Raw archive:** immutable source bytes; source authority.
2. **Source manifest:** archive identity, acquisition metadata, digest, license review, filename classification.
3. **Source reader:** validates compression, CSV structure, encoding policy, and discovered schema; streams rows/batches without changing source bytes.
4. **Raw typed layer:** parsed source facts with source-native values and row provenance; conversion errors are explicit.
5. **Normalized layer:** versioned, typed representations of source facts only, with source field mapping retained.
6. **Joined replay/game layer:** joins only on verified keys, with cardinality and unmatched reporting.
7. **ML views:** derived/reconstructed views, including `DecisionSampleV1` only where decision semantics are defensible.
8. **Validation:** integrity, provenance, privacy, leakage, quality, and output checks; every stage reports counts.
9. **Parquet shards:** typed, bounded-size columnar output and explicit partition metadata.
10. **Release manifest:** immutable release identity, configs, inputs, schema versions, counts, hashes, and build environment metadata.

Each stage MUST consume declared versioned inputs and write to a separate output location. It SHOULD be independently resumable using content/config identities. Partial outputs MUST not appear as completed releases.

## 8. Large-data requirements and implementation direction

Many large `.csv.gz` archives are expected. Implementations MUST use bounded memory: stream decompression and CSV parsing into bounded record batches, transform batches, and write Parquet incrementally. No operation may require an entire archive or global dataset in RAM. Limits for batch rows/bytes and unusually large fields MUST be configurable and recorded. Stages SHOULD checkpoint by deterministic input and configuration identity so failed work can resume safely.

Recommended first stack: Python 3.11+ and PyArrow for streaming/batch columnar I/O and Parquet. DuckDB or Polars MAY be used where measured benefits justify them and semantics remain explicit. The contract does not depend on a particular library. A pandas-only architecture requiring full-file materialization is prohibited.

## 9. Provenance model

Every normalized or derived record MUST trace to one or more `source_archive_id`s, a source row/event/game locator when available, the source schema identity, builder version, and transformation stage/version. Provenance SHOULD be columnar and queryable in the released views, not only in logs. If a stable native row identifier is unavailable, the reader MUST define a deterministic locator based on archive digest and a stable record ordinal or canonical source content, and document its limitations.

Derived IDs MUST be deterministic and namespaced by ID-contract version. They MUST NOT depend solely on random UUIDs, timestamps, row order that can change without detection, or noncanonical serialization. Where a source's own ID is preserved, the original value and its interpretation MUST remain distinguishable from the OpenMTGData ID.

## 10. Canonical IDs

The architecture requires deterministic IDs for source archive, source game/match where available, replay event where available, and derived decision sample. Exact compositions are gated on source inspection because source identifiers and their semantics are not yet asserted.

An ID contract MUST specify namespace, canonical inputs, encoding, hash algorithm, collision handling, and version. A source archive ID SHOULD derive from provider plus SHA-256. A game/match ID MUST derive from verified source identity, using a native stable identifier when its uniqueness scope is known; otherwise use archive identity plus a deterministic locator and explicitly limit cross-archive deduplication. Replay event IDs add a verified event identity or locator. Decision IDs derive from game/replay identity plus stable decision position and the decision-contract version. Hash collision or same-ID/different-content cases are integrity failures, not silent overwrites.

## 11. Replay/Game join contract

Replay and Game data MAY be joined only through identifiers verified to exist and have the required meaning and uniqueness scope. No guessed joins, temporal guesses, filename proximity, or card/deck similarity joins are allowed. For each candidate pair, report input counts, distinct key counts, null-key counts, matched and unmatched records on both sides, and observed one-to-many/many-to-many cardinality. Unexpected ambiguity MUST fail or quarantine the affected pair according to a documented rule; it MUST NOT silently multiply records. Every join run emits a machine-readable quality report and identifies the key/schema evidence used.

## 12. Canonical intermediate representation

Versioned contracts such as `ReplayEventV1` and `GameRecordV1` separate:

* **Source facts:** values directly represented in the source, with source field lineage.
* **Normalized facts:** deterministic type/format harmonization of source facts, retaining raw values where practical.
* **Reconstructed facts:** interpretations derived from sequences or other fields, labeled with method and confidence/quality status.
* **Quality metadata:** explicit availability, completeness, and validation status.

Exact fields remain gated on schema and semantics inspection. Every IR record MUST carry contract version, provenance, and typed nullability. A schema change that changes meaning requires a new contract version or an explicit compatible migration. Source-native values MUST NOT be overwritten by derived interpretations.

## 13. `DecisionSampleV1`

`DecisionSampleV1` is a model-agnostic behavioral sample with the conceptual shape **input-safe observation before decision → observed action**. It is not a claim that the action is optimal or legal. The record contract MUST support the following conceptual groups; exact fields depend on inspected source evidence:

* sample ID and schema ID;
* source archive, game/match, replay event/decision locator, and full provenance;
* acting perspective, only when defensibly established;
* pre-decision observation and bounded history, with availability and reconstruction quality;
* `observed_action` (also describable as `human_action` or `behavior_action`), with source-versus-derived status;
* action metadata only when supported by source facts or explicit derivation;
* outcome/post-decision metadata in a separate group that cannot enter policy inputs by default;
* reconstruction, information completeness, legal-action, target, and quality status.

The contract MUST distinguish these roles:

| Role | Contract |
|---|---|
| Input-safe information | Only information available to the acting player before the decision; features require field-level evidence and safe temporal alignment. |
| Label | The action observed in the data. It MUST NOT be named or described as `best_action`, `optimal_action`, or ground-truth policy. |
| Post-decision/evaluation information | Outcomes or later facts may support analysis/value tasks, but MUST be isolated from policy inputs by schema and validation. |

A sample whose action boundary, acting player, or pre-decision state cannot be established MUST be excluded from the policy imitation view or marked unusable by its typed status. No sample is promoted to a more complete reconstruction level by assumption.

## 14. Hidden information and perspective safety

Every policy-training view MUST be perspective-safe: it cannot reveal information unavailable to the acting player at that moment. Safety is established per field and temporal boundary, not inferred from a column's name. Hidden opponent cards, later revealed information, future actions, and post-decision outcomes MUST NOT enter the policy input. If safety cannot be established for a field or sample, it MUST be excluded from that view or carry a non-usable typed status and be filtered by default. Automated checks SHOULD test known future/outcome columns and field allowlists; manual source-semantic review remains necessary where data meaning is uncertain.

## 15. Legal action semantics

OpenMTGData v0.1 is not an authoritative MTG rules engine. It MUST NOT synthesize a complete legal-action set from intuition, infer that an observed action was the only legal action, or claim observed actions are legal ground truth. `legal_actions_status` MUST use a typed value such as `unknown`, `source_provided_complete`, `engine_reconstructed_complete`, or `partial`; v0.1 17Lands samples default to `unknown` unless verified evidence supports another state. Status scope and authority MUST accompany any non-unknown value.

Future XMage, Forge, Manafold, or other rules authority augmentation MUST be a separate provenance layer with engine/version/ruleset identity. It may add authoritative legal candidates without rewriting source facts or replacing the observed-action label.

## 16. Information quality contract

Quality MUST be machine-readable and filterable; prose alone is insufficient. `reconstruction_level` is an ordered typed enum: `source_direct`, `deterministically_derived`, `partial_reconstruction`, `unavailable`. It describes the record's state reconstruction, not label optimality. `information_completeness` is typed as `complete_for_declared_view`, `partial`, or `unknown`. `source_observation_kind` identifies whether a value is source-direct or inferred/reconstructed under a named method. `legal_actions_status` and `target_status` are separately typed and authority-scoped. `state_quality_flags` is a stable list of versioned machine codes, with optional explanatory text. `usable_for_policy_imitation` is a derived validation result and MUST NOT be caller-controlled. Enum extensions require compatibility/version policy; unknown values MUST fail closed for policy use.

Suggested `target_status` values are `observed`, `reconstructed`, `missing`, and `ambiguous`; meaning is the presence/reliability of the observed behavior label, not its optimality. Exact enum serialization is fixed when the Decision contract is implemented.

## 17. Dataset splitting contract

Decision rows MUST NOT be split independently. All rows sharing a game/match MUST remain together. Group at the safest available verified higher-level identity (for example session/event/draft) where inspection establishes related-game leakage risk. Selection of hierarchy is a discovery decision, not a filename assumption. Missing grouping identity MUST cause explicit exclusion or a documented conservative group fallback; it MUST NOT trigger random row splitting.

Split membership uses a stable hash partition over the canonical group ID and a versioned split salt/contract, with published algorithm and thresholds. The same group MUST never cross splits. The split contract MUST be recorded in the release manifest and stable across reruns with unchanged inputs/config. Changes to grouping hierarchy, salt, algorithm, or thresholds create a new split contract version and release identity.

## 18. Deduplication

Deduplication MUST classify exact duplicate records, source duplicates, and identity collisions with differing content. Exact comparison uses a versioned canonical representation and excludes explicitly nonsemantic operational metadata. Source duplicates may be reported and collapsed only under a documented policy that retains all contributing provenance. Same identity with differing semantic content MUST be an integrity failure or explicit quarantine; it MUST NOT be silently discarded or resolved by input order. Every stage reports duplicate counts and collision examples/locators.

## 19. Dataset versioning

Independent version identities are required for source manifest schema, normalized schema, decision schema, split contract, builder implementation, and published dataset release. Schema/contract versions describe data meaning; builder version identifies implementation; release ID identifies the complete built artifact set. A Git commit alone is insufficient dataset identity. Breaking semantic changes require a new applicable schema/contract version and release ID.

## 20. Reproducibility

With the same exact raw archive bytes, builder version, dependency environment, schema contracts, and configuration, builds MUST produce logically identical records, IDs, partitions, and semantic manifests. Canonical serialization, ordering, null handling, type conversion, and float behavior MUST be defined where applicable. Operational timestamps MUST be excluded from semantic identity.

Byte-identical Parquet is not promised across library versions or environments unless the implementation pins all relevant writer/runtime settings and demonstrates that guarantee. Until then the contract is semantic reproducibility plus per-output-byte hashes for audit, with dependency lock/environment recorded. Any deterministic output ordering the writer can guarantee SHOULD be used. A rebuild with changed library versions may have different file hashes while carrying equivalent logical data.

## 21. Validation

Validation gates MUST cover input digest and compressed integrity; filename recognition; schema fingerprint/drift; encoding/CSV structure; nullability and type conversion; identifier uniqueness; replay/game join cardinality and coverage; split isolation; duplicate collisions; future leakage where mechanically testable; perspective field policy; provenance completeness; privacy review; output row counts; Parquet readability; and agreement between release manifest and actual output files. Every stage MUST emit auditable counts for read, accepted, rejected, quarantined, emitted, and skipped records as applicable. A release cannot pass with unexplained count discrepancies or fatal integrity failures.

## 22. Failure model

Failures are classified as:

* **Fatal source error:** corrupt archive/checksum, unsafe path, or unrecoverable read failure; stop affected build.
* **Unsupported schema:** no compatible inspected adapter; do not normalize that archive.
* **Rejected record:** malformed or invalid record excluded with reason and source locator.
* **Quarantined record/group:** ambiguity or conflict retained in an auditable quarantine output, excluded from default views.
* **Warning:** nonfatal condition with explicit count and diagnostic.

No malformed or unsupported records may disappear silently. Build reports give per-archive and aggregate counts. Quarantine retention must not inadvertently publish unsafe or identifying fields.

## 23. Privacy

Although 17Lands publishes anonymized data, OpenMTGData MUST minimize unnecessary identifiers and MUST NOT introduce usernames, account IDs, IP addresses, authentication data, or unrelated personal data. Source inspection MUST identify potentially identifying fields. Unexpected identifiers trigger a publication hold for those fields until reviewed; local raw files remain governed by their source terms and access conditions. Released identifiers SHOULD be pseudonymous/source-scoped where raw values are not needed, while preserving stable linkage only when justified and licensed.

## 24. Card metadata

Card artwork MUST NOT be stored. Card identifiers/names present in legitimate source data MAY be preserved subject to source terms. External card metadata requires independent source, provenance, and license tracking; it MUST NOT be silently mixed with 17Lands data or treated as covered by the 17Lands license.

## 25. Output format

Parquet is the primary large-scale publication format. Schemas MUST use stable declared types and explicit nullable semantics; no silent type coercion. Compression SHOULD initially use Zstandard where supported and interoperable, subject to a measured benchmark and consumer compatibility review. The writer MUST configure compression/encoding explicitly and record it.

Avoid one monolithic file and avoid tiny-file explosions. Initial shard goal is 256–1024 MiB compressed per file, configurable by view and workload; actual sizes and row counts are recorded. Partitioning by expansion/format/split MAY aid selective access, but dimensions MUST be chosen after measuring cardinality and skew. Do not create directories for empty combinations or partition on high-cardinality IDs. Release layout and partition contract are versioned; schema metadata carries contract IDs.

## 26. Hugging Face dataset layout

Publication is planned, not part of M0. A Hugging Face dataset repository SHOULD expose independently consumable configurations/views such as `replay_events`, `games`, and `decision_imitation`; `draft_decisions` is future scope. Users should be able to load only the view they need. Dataset scripts/configuration and Parquet layout must preserve stable schemas and provenance.

The Dataset Card MUST include description, intended uses, limitations, 17Lands attribution and source URLs, license per included source, no-endorsement statement, schema, provenance, build method, quality limitations, split semantics, citation, known biases, and reconstruction caveats. Publication tooling MUST compare uploaded files and card metadata with the release manifest. No dataset is published until each included source's license/status is verified.

## 27. Bias and limitations

17Lands is not a uniform sample of all MTG players, skill levels, platforms, or formats. The initial data is Limited-focused and coverage varies by set/format and time. Participation creates player-selection effects; card pools and metagame distributions change; replay context and decision reconstruction may be incomplete. Documentation MUST avoid implying representativeness or optimality. Every release MUST describe included expansion/format/time coverage, observed limitations, and known missingness. Observed human behavior describes behavior in this source population, not ideal play.

## 28. Model independence

Core schemas MUST NOT depend on Laya, MageZero, Forge, a neural-network architecture, or a tokenizer. Model-specific tensorization, tokenization, action encoding, or prompt packaging belongs in separate adapters/export views that retain linkage to core sample IDs and versions. The core dataset remains useful without any such adapter.

## 29. Security and robustness

Raw inputs are untrusted. Readers MUST defend against malformed CSV, decompression/path abuse, unexpectedly huge fields, invalid encodings, and resource exhaustion. Paths MUST be resolved beneath declared input/output roots; archive member paths are not applicable to plain gzip CSV but any future container reader must reject traversal. Output names MUST be generated from validated identifiers, never untrusted row text. The builder MUST refuse output paths resolving to raw source files and MUST NOT overwrite source archives. Limits, decoding policy, and rejected input diagnostics are recorded.

## 30. Deterministic build/release manifest

A versioned release manifest MUST identify at least release ID; all schema and split contract IDs; builder version and commit; source manifest digest; canonical configuration digest; dependency/runtime identity; counts read/accepted/rejected/quarantined/emitted; per-view, per-expansion/format and per-split counts; source archive digests; output paths, sizes, row counts, and SHA-256 hashes; license/attribution review status; creation metadata; and reproducibility mode. Semantic release identity MUST derive from canonical input and configuration identities, not wall-clock timestamps. Creation timestamps are audit metadata only. The manifest MUST be sufficient to identify inputs and reproduce or explain the release.

## 31. CLI contract

The eventual CLI SHOULD expose composable commands such as `openmtgdata inspect`, `manifest`, `normalize`, `build`, `validate`, and `stats`. Commands MUST support explicit input/output/configuration roots, return nonzero on fatal/unsupported conditions, and emit machine-readable reports alongside concise human diagnostics. `build` may orchestrate stages, but individual stages remain invocable and resumable; no giant opaque command is required. Exact flags and command names are implementation decisions.

## 32. Test strategy

Tests MUST cover unit behavior, schema fingerprints/compatibility, golden source manifests, deterministic IDs, split leakage, join cardinality, malformed input and decompression failures, duplicate collisions, perspective/leakage guards, bounded-memory integration, and an end-to-end tiny fixture build. Tests MUST NOT require the entire public dataset. Use tiny synthetic fixtures or legally reusable samples with clear provenance/license. Tests of real schemas run on explicitly acquired representative archives and MUST NOT make CI download all source data. Bounded-memory checks should assert a declared operational ceiling with generous platform-aware tolerance.

## 33. Performance measurement

Before optimization, benchmark representative archive classes and sizes. Record compressed input GB/min, rows/sec, peak RSS, Parquet output size, compression ratio, and per-stage timings, plus hardware/runtime/dependency versions and configuration. Benchmarking MUST exercise streaming and record-batch paths and MUST NOT require committing benchmark datasets. Results guide batch sizes, shard targets, compression, and optional engines.

## 34. Future authoritative augmentation

An authoritative rules engine may later add legal action candidates, exact phases/priority when reconstructable, game-state validation, and search/MCTS targets. Such data is an independently versioned augmentation with engine, ruleset, input, and transformation provenance. It MUST never rewrite or obscure 17Lands source facts or observed behavior labels. Compatibility and disagreement with the source-derived view must be visible.

## 35. Future teacher/model workflow

One possible downstream workflow is 17Lands human decisions → OpenMTGData `DecisionSample` → optional Laya fine-tuning → specialized MTG teacher → downstream student/search systems. This is a consumer workflow only. It is not a promise about label quality, the definition of OpenMTGData, or a v0.1 implementation task.

## 36. Architectural decisions and future ADRs

The following decisions require explicit ADRs when implementation evidence is available: (1) canonical source authority and per-file licensing review; (2) provenance and identity composition; (3) source-schema fingerprint/evolution compatibility; (4) deterministic split algorithm and grouping hierarchy; (5) decision reconstruction/target semantics; (6) field-level perspective safety; (7) Parquet partitioning/sharding/compression; (8) Hugging Face release identity/layout. M0 records requirements but does not settle evidence-dependent choices.

## 37. Unresolved questions and discovery gates

The following remain open and MUST be answered from actual source evidence before the affected contract is locked:

1. What identifiers exist in each generation of Replay Data, and what are their uniqueness scopes?
2. Which keys safely join Replay Data to matching Game Data? What are coverage and cardinality by set/format/era?
3. How much pre-decision state can be reconstructed from replay records, and what is the event/decision boundary?
4. Are targets/actions represented explicitly and consistently across releases?
5. Which fields are visible to the acting player at the decision point, and which reveal hidden/future information?
6. What schema drift exists between early and recent replay/game archives? Which changes are additive versus semantic/incompatible?
7. What verified grouping identity provides safe train/validation/test isolation across related games/sessions/events?
8. Are replay files event-level, turn-level, or mixed by era/format?
9. Which historical archives can share one normalized contract without losing meaning?
10. Which official URLs, publication/update dates, and license exceptions apply to every downloaded file?
11. Which fields, if any, need privacy review or suppression before release?
12. Which columnar partition/shard/compression settings perform well for representative consumers and archives?

Until resolved, implementations MUST represent uncertainty explicitly, exclude unsupported claims from policy views, and record the evidence and scope of each decision.
