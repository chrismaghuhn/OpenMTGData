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

Source registration, inspection, and catalog aggregation are separate versioned artifacts. A registration is never amended merely because later inspection discovers more about its schema.

### `SourceArchiveRecordV1` — immutable artifact registration

One record identifies one exact compressed artifact. Required fields are `source_archive_record_schema_id`, deterministic `source_archive_id`, provider, exact `original_filename`, exact compressed-byte SHA-256, compressed byte size, parsed filename metadata only when recognized by a versioned rule, source URL/evidence when known, license identifier/status/review evidence, and acquisition/ingestion audit metadata where available. `source_archive_id` is derived from provider namespace and exact compressed SHA-256, independent of the software that registers or inspects it. Once registered, the record is immutable; changed bytes are a different artifact/ID. Missing metadata remains explicitly unknown, not guessed.

### `SourceInspectionV1` — evidence about reading an artifact

An inspection references `source_archive_id` and records `raw_schema_fingerprint`, observed headers, CSV/dialect/encoding observations, type/nullability evidence, evidence scope (which rows/files were inspected and how), `source_interpretation_contract_id` where semantic interpretation has been applied, inspection tool/builder identity, and inspection timestamp. This is a separate artifact; new inspection does not mutate source registration. Inspection artifact identity includes source archive ID, inspection method/tool identity, scope, and report content. Repeated inspections may coexist.

### `SourceManifestV1` — catalog aggregate and semantic projection

`SourceManifestV1` is a versioned catalog of references to archive registrations and zero or more inspection artifacts. Implementations MUST define canonical serialization and two explicit projections:

* The **audit catalog document** contains referenced records/reports plus operational fields such as ingestion/inspection/report timestamps and tool identities.
* The **semantic source-set projection** contains only the versioned catalog schema ID and a canonically ordered set of each included archive's `source_archive_id`, provider, verified source-kind/expansion/format metadata, source URL/evidence identity when part of declared source selection, and license/status identity required by the build contract. It excludes ingestion, inspection, report-creation timestamps, machine-local paths, and inspector/builder identity. Inclusion/projection rules MUST be versioned; unknown fields cannot enter implicitly.

The semantic projection is canonically serialized and hashed to form the semantic source-catalog digest. Tool/builder identity does not participate in source artifact identity or semantic source-set identity; it participates in the audit catalog and relevant inspection artifact identity. Inspection results are referenced by the audit catalog and may be required as a build input through their fingerprint/contract identities, but rerunning inspection does not change source artifact identity. Unknown values MUST NOT be guessed.

The field inventory below is distributed across `SourceArchiveRecordV1`, `SourceInspectionV1`, and `SourceManifestV1`; it is not one mutable record schema.

| Field | Requirement | Meaning |
|---|---|---|
| `source_archive_id` | Required, derived | Provider namespace + exact compressed SHA-256; never random |
| `provider` | Required | `17lands` for this source |
| `dataset_family` | Required when verified | Public Datasets family per source evidence |
| `original_filename` | Required | Exact observed basename |
| `source_url` and evidence | Optional until known; required before publication when recoverable | Published file or authoritative listing evidence |
| `sha256` / `compressed_bytes` | Required, derived | Exact compressed-byte digest and length |
| `source_kind` | Required when recognized | `replay`, `game`, `draft`, or `unknown` |
| `expansion` / `format` | Required when recognized | Exact opaque filename tokens, not guessed normalization |
| source published/updated date | Optional | Only explicit authoritative metadata |
| license identifier/status/evidence | Required before publication; may be pending locally | Review state includes `verified`, `pending`, `restricted`, `unknown` |
| acquisition/ingestion timestamp and local path | Optional audit metadata | Excluded from semantic projection; paths are not portable identity |
| `raw_schema_fingerprint` | `SourceInspectionV1` only | Physical-source identity observed from actual archive evidence |
| `source_interpretation_contract_id` | Inspection/processing identity | Versioned semantic mapping, independent of physical schema |
| inspection timestamp/tool/builder | `SourceInspectionV1` audit metadata | Tool identity affects inspection artifact identity, not source artifact identity |

The catalog MUST preserve diagnostics for unknown filename tokens, metadata gaps, and license review. Audit timestamps MUST NOT affect semantic IDs, semantic catalog identity, split membership, or `semantic_release_id`.

## 5. Source filename discovery and pairing

Discovery MUST recognize the canonical public dump pattern, subject to verification against actual published files: `replay_data_public.<EXPANSION>.<FORMAT>.csv.gz`, `game_data_public.<EXPANSION>.<FORMAT>.csv.gz`, and `draft_data_public.<EXPANSION>.<FORMAT>.csv.gz`. Expansion and format tokens are opaque filename components until verified. Recognition MUST be deterministic and case-sensitive unless source evidence justifies a versioned rule.

Unknown files MUST be rejected as source inputs or catalogued with `source_kind=unknown` and a clear diagnostic; they MUST NOT silently pass as a recognized type. Filename proximity or directory order MUST NOT pair files. Replay/Game pairing requires matching verified expansion, format, and any additional source identity established during inspection, plus proven key semantics. Discovery catalogs Draft Data even when no downstream draft processing is enabled.

## 6. Source schema discovery gate

No source column names, types, identifier meanings, event semantics, or join keys are fixed by this specification. The exact CSV schemas MUST be inspected from real downloaded archives before a source adapter or normalized field mapping is locked down. Inspection MUST cover representative early and recent archives, replay and game families, and each observed schema variant.

Inspection reports MUST capture ordered headers, observed nullability/type evidence, encoding and CSV dialect behavior where relevant, row samples or safe summaries, archive identity, evidence scope, and a deterministic `raw_schema_fingerprint`. Its versioned physical fingerprint inputs MUST be explicitly defined from source-observed structure (at least ordered raw headers and relevant CSV dialect/encoding characteristics); semantic adapter/type interpretations and builder identity MUST NOT affect it. Type/nullability evidence remains separately recorded in the inspection report. A separate `source_interpretation_contract_id` identifies the versioned semantic mapping applied to that physical input. The same raw fingerprint MAY be paired with a corrected interpretation contract without claiming the CSV schema changed. Fingerprint encoding is an implementation decision and MUST be versioned. Unexpected incompatible physical drift MUST fail clearly. Additive compatible evolution MAY be accepted only under an explicit, versioned compatibility policy; ignored columns and their disposition MUST be reported. A column being present does not prove it is safe, complete, or semantically stable.

Header-only inventory MUST inspect every readable candidate archive under configured local input roots, without requiring full row processing. It records observed header/dialect facts and groups archives by source kind, raw fingerprint, and relevant verified filename metadata. Deep type/semantic inspection MAY then select representative archives from every observed group. Reports MUST state their roots and scope; “all observed variants” means all variants in those configured roots as of that inventory run, not all 17Lands history.

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

Many large `.csv.gz` archives are expected. Implementations MUST use bounded memory: stream decompression and CSV parsing into bounded record batches, transform batches, and write Parquet incrementally. No operation may require an entire archive or global dataset in RAM. Limits for batch rows/bytes and unusually large fields MUST be configurable and recorded. Stages SHOULD checkpoint by deterministic input and configuration identity so failed work can resume safely. Raw input roots MAY be outside the Git repository; repository-relative `data/raw/17lands/` is only an optional convenient default. The pipeline MUST accept explicit input roots. Absolute user-specific paths MUST NOT be committed to repository configuration. External and in-repository source files receive identical immutable registration and hashing treatment. Intermediate, quarantine, and release roots MUST resolve outside every configured raw root and MUST never overwrite or alias raw source files.

A plain gzip CSV MUST NOT resume from an arbitrary compressed-byte offset unless a separately proven and indexed mechanism establishes correctness. Initial resume strategies MAY deterministically re-stream to a stable record/batch ordinal or checkpoint only completed durable stage/batch/shard units and restart streaming as needed; completed units are preferred to fragile transport offsets. Checkpoint identity MUST bind exact source archive digest, raw schema fingerprint, interpretation contract, builder/stage version, and relevant canonical configuration. Partial outputs MUST use incomplete/temporary identities and be atomically promoted only after validation. A crash MUST NOT skip or duplicate accepted records, reorder semantic identities, or make a partial artifact appear complete.

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

Exact fields remain gated on schema and semantics inspection. Every IR record MUST carry contract version, provenance, and typed nullability. A schema change that changes meaning requires a new contract version or an explicit compatible migration. Source-native values MUST NOT be overwritten by derived interpretations. Each schema MUST declare a versioned typed field-origin/derivation contract sufficient to classify fields as `source_direct`, `normalized_source`, `deterministically_derived`, `reconstructed`, `engine_augmented`, or `unknown`. Exact encoding is fixed with the schema. Per-cell metadata is not required where schema/field-level declarations and record-level exceptions express the same facts; exceptions MUST be representable. Consumers MUST be able to distinguish directly sourced, normalized, reconstructed, and externally augmented policy fields. Origin is separate from privacy/perspective status, which remains field- and temporal-boundary-specific.

## 13. `DecisionSampleV1`

`DecisionSampleV1` is a model-agnostic behavioral sample with the conceptual shape **input-safe observation before decision → observed action**. It is not a claim that the action is optimal or legal. The record contract MUST support the following conceptual groups; exact fields depend on inspected source evidence:

* sample ID and schema ID;
* source archive, game/match, replay event/decision locator, and full provenance;
* acting perspective, only when defensibly established;
* pre-decision observation and bounded history, with availability and reconstruction quality;
* `observed_action` (also describable as `human_action` or `behavior_action`), with source-versus-derived status;
* action metadata only when supported by source facts or explicit derivation;
* outcome/post-decision metadata in a separate group that cannot enter policy inputs by default;
* reconstruction, information completeness, legal-action, `observed_action_status`, optional Magic `action_target_status`, and other quality status.

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

Quality MUST be machine-readable and filterable; prose alone is insufficient. `reconstruction_level` is an ordered typed enum: `source_direct`, `deterministically_derived`, `partial_reconstruction`, `unavailable`. It describes the record's state reconstruction, not label optimality. `information_completeness` is typed as `complete_for_declared_view`, `partial`, or `unknown`. Field origin is declared under §12. `legal_actions_status`, `observed_action_status`, and (only where an MTG object/player/card target is represented) `action_target_status` are distinct typed values with explicit scope/authority. Bare `target_status` MUST NOT be used. `state_quality_flags` is a stable list of versioned machine codes, with optional explanatory text. `usable_for_policy_imitation` is a derived validation result and MUST NOT be caller-controlled. Enum extensions require compatibility/version policy; unknown values MUST fail closed for policy use.

`observed_action_status` describes presence, completeness, and reliability of the behavior label, with typed states such as `observed`, `reconstructed`, `missing`, and `ambiguous`; it says nothing about optimality. `action_target_status` separately describes availability/reliability of an MTG object/player/card target associated with an observed action. Exact enum serialization is fixed when the Decision contract is implemented.

## 17. Dataset splitting contract

Decision rows MUST NOT be split independently. All rows sharing a game/match MUST remain together. Group at the safest available verified higher-level identity (for example session/event/draft) where inspection establishes related-game leakage risk. Selection of hierarchy is a discovery decision, not a filename assumption. Missing grouping identity MUST cause explicit exclusion or a documented conservative group fallback; it MUST NOT trigger random row splitting.

Split membership uses a stable hash partition over the canonical group ID and a versioned split salt/contract, with published algorithm and thresholds. The same group MUST never cross splits. The split contract MUST be recorded in the release manifest and stable across reruns with unchanged inputs/config. Changes to grouping hierarchy, salt, algorithm, or thresholds create a new split contract version and release identity.

## 18. Deduplication

Deduplication MUST classify exact duplicate records, source duplicates, and identity collisions with differing content. Exact comparison uses a versioned canonical representation and excludes explicitly nonsemantic operational metadata. Source duplicates may be reported and collapsed only under a documented policy that retains all contributing provenance. Same identity with differing semantic content MUST be an integrity failure or explicit quarantine; it MUST NOT be silently discarded or resolved by input order. Every stage reports duplicate counts and collision examples/locators.

## 19. Dataset versioning

Independent version identities are required for source archive record, source inspection, source catalog, raw schema, interpretation contract, normalized schema, decision schema, split contract, builder implementation, semantic dataset release, build/audit execution, and emitted artifact manifest. Schema/contract versions describe data meaning; builder/tool versions identify implementation; a Git commit alone is insufficient dataset identity. Breaking semantic changes require a new applicable schema/contract version and semantic release ID.

## 20. Reproducibility

`semantic_release_id` MUST be a SHA-256 identity over a versioned canonical tuple containing the semantic source-set projection digest, raw schema fingerprints, interpretation/normalized/decision/split contract IDs, canonical semantic configuration digest, and a digest of the canonically ordered logical records plus group/split/partition assignments. Canonical record serialization MUST exclude operational/audit fields and define ordering, null handling, type conversion, and float behavior. This makes the semantic ID independent of Parquet container bytes while still binding it to actual logical output. Operational timestamps, machine-local paths, inspector identities, and build execution identity are excluded. With identical semantic inputs and contracts, builders MUST establish whether logical records, IDs, and partitions are equivalent; compatible runtime/writer changes MUST NOT automatically be assumed equivalent.

Byte-identical Parquet is not promised across library versions or environments unless the implementation pins all relevant writer/runtime settings and demonstrates that guarantee. Until then the contract is semantic reproducibility plus per-output-byte hashes for audit, with dependency lock/environment recorded. Any deterministic output ordering the writer can guarantee SHOULD be used. Same logical records/partitions with different compatible writer/runtime versions MAY share an equivalent semantic dataset identity only when equivalence is established under the declared contract; output hashes may differ.

## 21. Validation

Validation gates MUST cover input digest and compressed integrity; filename recognition; schema fingerprint/drift; encoding/CSV structure; nullability and type conversion; identifier uniqueness; replay/game join cardinality and coverage; split isolation; duplicate collisions; future leakage where mechanically testable; perspective field policy; provenance completeness; privacy review; output row counts; Parquet readability; and agreement between release manifest and actual output files. Every stage MUST emit auditable counts for read, accepted, rejected, quarantined, emitted, and skipped records as applicable. A release cannot pass with unexplained count discrepancies or fatal integrity failures.

## 22. Failure model

Failures are classified as:

* **Command/tool execution failure:** the requested inventory, inspection, build, or validation operation itself cannot complete; return a failure diagnostic.
* **Fatal source error:** corrupt archive/checksum, unsafe path, or unrecoverable read failure; stop processing that source/build as scoped.
* **Unsupported source/schema finding:** unknown source type or no compatible inspected adapter; catalog it and continue inventory where possible, but do not normalize or include it in a release.
* **Rejected record:** malformed or invalid record excluded with reason and source locator.
* **Quarantined record/group:** ambiguity or conflict retained in an auditable quarantine output, excluded from default views.
* **Warning:** nonfatal condition with explicit count and diagnostic.

Release-blocking validation failure is a separate release disposition: validation may execute successfully and produce a complete report that blocks publication. Unsupported findings, command failure, and release-blocking findings MUST remain distinguishable in reports and eventual exit-code mapping.

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

A versioned release record MUST distinguish three identities. (1) `semantic_release_id` identifies logical dataset contract/input/configuration identity by the canonical, versioned function in §20 and excludes wall-clock/audit metadata. (2) `build_audit_id` identifies a concrete build execution, including builder commit, dependency/runtime/writer versions, execution metadata, and reports; it is audit identity and is not folded into source or semantic identity. (3) `artifact_manifest_digest` hashes the canonical artifact manifest containing emitted paths, byte sizes, row counts, and per-file byte hashes. The artifact manifest MUST also record all schema/split/interpretation IDs, source catalog digest, canonical config digest, record/reject/quarantine counts, per-view/expansion/format/split counts, license review, creation metadata, and reproducibility mode. Two builds MAY have the same established semantic release identity and different build audit/artifact digests and Parquet byte hashes. A manifest verifier MUST validate declared artifacts against their actual files.

## 31. CLI contract

The eventual CLI SHOULD expose composable commands such as `openmtgdata inventory`, `inspect`, `manifest`, `normalize`, `build`, `validate`, and `stats`. Commands MUST support explicit input/output/configuration roots, including external raw roots, and emit machine-readable reports alongside concise human diagnostics. Inventory/inspection may successfully catalog unknown or unsupported files as findings; a finding is not itself tool execution failure. Reports MUST distinguish command/tool execution failure, source classified unsupported, record rejection, and release-blocking validation failure. Exact exit codes remain an implementation decision. `build` may orchestrate stages, but individual stages remain invocable and resumable; no giant opaque command is required.

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
