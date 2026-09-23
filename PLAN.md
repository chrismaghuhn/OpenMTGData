# OpenMTGData Implementation Plan

This plan turns [SPEC.md](SPEC.md) into reviewable work. Milestones are sequential unless a task is explicitly independent. Each task is small enough to implement and review on its own. Schema-dependent work is blocked until M3 records evidence from actual archives. No task authorizes committing large source archives or training a model.

## M0 — Specification

### M0.1 — Architecture specification and implementation plan

- **Objective:** Establish normative contracts and staged implementation work.
- **Allowed scope:** `SPEC.md`, `PLAN.md`, optionally a minimal `README.md`.
- **Dependencies:** None.
- **Implementation requirements:** Documentation only; mark unknown source facts as discovery gates; cross-check internal consistency.
- **Tests:** Manual requirement-by-requirement review against the project brief; confirm no production implementation is present.
- **Acceptance criteria:** Both documents cover source licensing/provenance, bounded-memory processing, truth/quality semantics, privacy/leakage, deterministic identity/splits, validation/failure reporting, Parquet/Hugging Face release, and granular milestones. No schemas/keys are fabricated.
- **Out of scope:** Pipeline code, source schema inspection by guessing, downloaded-data commits, publication, training.

## M1 — Repository and Python package scaffold

### M1.1 — Establish package, tooling, and local data boundaries

- **Objective:** Create a minimal maintainable Python 3.11+ package and contributor workflow.
- **Allowed scope:** Packaging metadata, source package skeleton, CI configuration, lint/type/test configuration, `.gitignore`, contributor docs.
- **Dependencies:** M0 accepted.
- **Implementation requirements:** Define supported Python versions and pinned/managed dependencies; ignore `data/raw/17lands/`, derived build outputs, caches, and local reports while allowing tiny fixtures. Document no full archives in Git. Keep CLI entry point placeholder or scaffold only.
- **Tests:** CI installs package and runs a trivial package/import/configuration check.
- **Acceptance criteria:** Clean checkout can install the package and invoke documented tooling; raw and derived paths are ignored; fixture exceptions are explicit; no production transform logic.
- **Out of scope:** CSV adapters, source schema declarations, data downloads, model code.

### M1.2 — Define configuration and path safety conventions

- **Objective:** Specify runtime configuration locations and prevent source overwrite.
- **Allowed scope:** Config example/schema and path-handling scaffolding.
- **Dependencies:** M1.1.
- **Implementation requirements:** Separate raw, intermediate, quarantine, and release roots; validate resolved paths; make configuration canonicalizable for digests; avoid secrets.
- **Tests:** Path traversal and raw-output collision unit tests.
- **Acceptance criteria:** Invalid roots fail before writes; configuration digest excludes operational timestamps and is deterministic.
- **Out of scope:** Stage execution, source-specific path discovery.

## M2 — Source discovery and `SourceManifestV1`

### M2.1 — Implement canonical filename recognition

- **Objective:** Parse only verified canonical 17Lands public filename families and catalog unknown files.
- **Allowed scope:** Filename parser and focused tests.
- **Dependencies:** M1.1; reference filename patterns in SPEC.
- **Implementation requirements:** Recognize replay/game/draft plus opaque expansion/format tokens; unknown inputs produce structured diagnostics; no expansion-specific code branches.
- **Tests:** Canonical examples, unusual tokens, malformed names, unknown extensions, case behavior.
- **Acceptance criteria:** Parser is deterministic and never silently classifies unknown files.
- **Out of scope:** Reading CSV headers or pairing on unverified keys.

### M2.2 — Define and serialize `SourceManifestV1`

- **Objective:** Record required/optional/derived source metadata canonically.
- **Allowed scope:** Manifest model, canonical serialization, golden fixtures.
- **Dependencies:** M2.1.
- **Implementation requirements:** Include archive SHA-256, byte size, provider/family, original filename, URL when known, source kind/set/format, license review status, ingestion timestamp, builder version, and later schema fingerprint slot. Explicitly encode unavailable metadata.
- **Tests:** Golden serialization/digest tests; missing optional metadata; duplicate archive identity; manifest schema validation.
- **Acceptance criteria:** Same artifact/metadata/config emits identical semantic manifest bytes/digest; operational timestamp does not alter semantic identity.
- **Out of scope:** Declaring all files licensed without per-source review; downloading archives.

### M2.3 — Stream archive hashing and license/source catalog reports

- **Objective:** Hash exact compressed bytes and produce auditable catalog output.
- **Allowed scope:** File streaming utility and local inspection reports.
- **Dependencies:** M2.2.
- **Implementation requirements:** Bounded-memory SHA-256; record compressed size; collect official URL/license evidence without relying on undocumented APIs; publication status stays pending until reviewed.
- **Tests:** Known digest fixture, empty/truncated/unreadable files, stable catalog output.
- **Acceptance criteria:** No full-file memory read; invalid compressed files are diagnosable; pending license blocks release eligibility.
- **Out of scope:** Processing CSV rows or publishing data.

## M3 — Real source schema inspection (adapter gate)

### M3.1 — Select representative local archives and record evidence

- **Objective:** Inspect actual downloaded Replay and matching Game archives before fixing adapters.
- **Allowed scope:** Inspection report tooling and reports outside committed large-data artifacts; small sanitized metadata reports may be committed.
- **Dependencies:** M2.1–M2.3; user-provided/downloaded archives available locally under ignored raw path.
- **Implementation requirements:** Cover early and recent eras, multiple observed formats, replay and game files, and every distinct header variant. Record exact headers, types/nullability evidence, dialect/encoding behavior, archive digests, and source URLs. Do not infer columns from documentation or filenames.
- **Tests:** Inspection-report determinism and schema fingerprint golden tests using tiny lawful fixtures; manual cross-check against actual archives.
- **Acceptance criteria:** Evidence table identifies all inspected files and variants; unknown fields remain uninterpreted; no adapter locks columns before review.
- **Out of scope:** Normalization, decision extraction, downloading the entire corpus.

### M3.2 — Decide schema identity and evolution policy

- **Objective:** Define compatibility classes from observed drift.
- **Allowed scope:** Schema reports, policy ADR, schema registry metadata.
- **Dependencies:** M3.1.
- **Implementation requirements:** Fingerprint ordered headers plus versioned interpretation; classify additive, type/nullability, reorder, rename, removal, and semantic drift. Specify fail-closed behavior and explicit compatible-addition rules.
- **Tests:** Compatibility matrix tests for observed and synthetic drift cases.
- **Acceptance criteria:** Each inspected archive maps to a documented schema identity; incompatible drift is rejected with actionable diagnostics.
- **Out of scope:** Assuming all eras share one schema; silently dropping unknown columns.

## M4 — Streaming source readers

### M4.1 — Implement bounded-memory gzip CSV reader

- **Objective:** Read supported archives in bounded record batches.
- **Allowed scope:** Reader for schemas approved in M3; parsing diagnostics.
- **Dependencies:** M3.2.
- **Implementation requirements:** Stream gzip and CSV; enforce field/record limits and decoding policy; preserve raw values/row locators; emit batch-level counts; do not mutate source.
- **Tests:** Multiline/quoted fields as observed, malformed rows, invalid encoding, huge field, truncated gzip, memory ceiling integration test.
- **Acceptance criteria:** Memory stays under declared bound independent of total archive row count; errors include archive and deterministic row locator.
- **Out of scope:** Semantic game reconstruction or joins.

### M4.2 — Add resumable reader checkpoints

- **Objective:** Resume source reading safely after interruption.
- **Allowed scope:** Checkpoint metadata and reader orchestration.
- **Dependencies:** M4.1.
- **Implementation requirements:** Checkpoint keys include archive digest, schema fingerprint, builder and config identity; incomplete batches are not exposed as complete outputs.
- **Tests:** Interrupted/resumed versus uninterrupted semantic equivalence; changed-input invalidates checkpoint.
- **Acceptance criteria:** Resume does not skip, duplicate, or reorder source records without detection.
- **Out of scope:** Distributed processing.

## M5 — Normalized source layer

### M5.1 — Define `ReplayEventV1` from inspected evidence

- **Objective:** Normalize verified replay source facts while preserving source lineage.
- **Allowed scope:** Replay schema contract, field map, adapter, migration docs.
- **Dependencies:** M3.2 and M4.1.
- **Implementation requirements:** Separate raw/source facts from normalized and reconstructed values; do not claim event/decision semantics beyond evidence; carry archive/schema/row provenance and typed quality metadata.
- **Tests:** Schema/type/nullability tests, field lineage tests, known-row normalization golden tests.
- **Acceptance criteria:** Every emitted value is source-backed or explicitly labeled as deterministic normalization; unsupported variants fail clearly.
- **Out of scope:** Legal move assertions, optimal targets, game joins.

### M5.2 — Define `GameRecordV1` from inspected evidence

- **Objective:** Normalize verified game source facts.
- **Allowed scope:** Game schema contract and adapter.
- **Dependencies:** M3.2 and M4.1.
- **Implementation requirements:** Same source/derived distinction, provenance, type, and quality rules as replay records.
- **Tests:** Schema/type/nullability and golden-record tests.
- **Acceptance criteria:** No invented fields or semantics; every mapping traces to inspection evidence.
- **Out of scope:** Replay join and policy view creation.

## M6 — Replay/Game join

### M6.1 — Verify candidate join keys and semantics

- **Objective:** Select only source-verified join key(s) and uniqueness scope.
- **Allowed scope:** Evidence report and ADR.
- **Dependencies:** M3.1, M5.1, M5.2.
- **Implementation requirements:** Measure key nulls, uniqueness, matched/unmatched coverage, and cardinality by relevant era/format; test candidate keys against actual archives.
- **Tests:** Synthetic duplicate/missing/ambiguous-key fixtures plus reproducible report review.
- **Acceptance criteria:** Key is accepted only with documented semantics and observed cardinality; otherwise pairing is declared unsupported.
- **Out of scope:** Guessing keys based on timestamp or proximity.

### M6.2 — Implement explicit join and quality report

- **Objective:** Join supported pairs and expose coverage and ambiguity.
- **Allowed scope:** Join stage and report schema.
- **Dependencies:** M6.1.
- **Implementation requirements:** Refuse unsafe many-to-many expansion; report both-side unmatched counts, key evidence, input/output counts, and pair identity.
- **Tests:** One-to-one/one-to-many allowed cases if justified; ambiguous many-to-many refusal; deterministic output.
- **Acceptance criteria:** No guessed join; result and report reproduce from same inputs/config.
- **Out of scope:** Enriching with external databases.

## M7 — Decision extraction contract

### M7.1 — Establish defensible decision boundaries and perspective evidence

- **Objective:** Determine where an observed action and before-state can be supported.
- **Allowed scope:** Source analysis, decision semantics ADR, field-level safety matrix.
- **Dependencies:** M3.1, M5.1, M6.2 where a game join is needed.
- **Implementation requirements:** Investigate whether replay is event-, turn-, or mixed-level; target consistency; actor identity; information visibility; post-decision leakage. Leave unsupported eras excluded.
- **Tests:** Reviewable annotated tiny examples; automated checks for known future/outcome fields.
- **Acceptance criteria:** Written evidence supports each included decision type and each policy-input field; ambiguous cases have explicit exclusion/status.
- **Out of scope:** Calling actions optimal/legal; model training.

### M7.2 — Define and emit `DecisionSampleV1`

- **Objective:** Produce model-agnostic behavioral samples for only approved decision types.
- **Allowed scope:** Decision schema, extractor, quality codes.
- **Dependencies:** M7.1.
- **Implementation requirements:** Label `observed_action`; separate input, label, and post-decision metadata; include provenance, reconstruction level, completeness, legality/target status; no unsupported field claims.
- **Tests:** Contract/schema tests; no-future-input tests; hidden-information exclusion tests; source-to-sample provenance checks.
- **Acceptance criteria:** Policy view fails closed on unsafe/ambiguous samples and cannot present observed behavior as optimal policy.
- **Out of scope:** Legal candidate generation and model-specific tokenization.

## M8 — IDs, deduplication, and splits

### M8.1 — Finalize deterministic identity contracts

- **Objective:** Implement archive/game/event/decision IDs using verified identifiers.
- **Allowed scope:** ID contract and helpers.
- **Dependencies:** M3.1 and M7.2.
- **Implementation requirements:** Specify namespace, canonical bytes, version, hash, collision handling, and fallback locator when native IDs are unavailable.
- **Tests:** Golden IDs across runs/platforms; collision and changed-input tests.
- **Acceptance criteria:** IDs are stable, deterministic and traceable; no random UUID is sole identity.
- **Out of scope:** Assuming unknown source IDs.

### M8.2 — Implement duplicate classification and quarantine

- **Objective:** Surface exact/source duplicates and identity-content collisions.
- **Allowed scope:** Dedup stage and quarantine report.
- **Dependencies:** M8.1.
- **Implementation requirements:** Canonical semantic comparison; merge provenance only under explicit source duplicate policy; collision failures never resolve by ordering.
- **Tests:** Exact, source duplicate, and conflicting-identity fixtures.
- **Acceptance criteria:** Every duplicate class has counts and deterministic disposition; conflict is failure/quarantine.
- **Out of scope:** Fuzzy deduplication.

### M8.3 — Choose safe group hierarchy and implement split contract

- **Objective:** Partition by game or stronger verified grouping identity without leakage.
- **Allowed scope:** Split ADR, algorithm, report.
- **Dependencies:** M7.1, M8.1.
- **Implementation requirements:** Select grouping from actual available identities; stable hash, salt and thresholds; version split contract; handle missing group IDs conservatively.
- **Tests:** Same-game/session isolation, deterministic rerun, missing-ID behavior, no row-level split.
- **Acceptance criteria:** No group crosses splits; all partition decisions are reproducible and reported.
- **Out of scope:** Random row-level partitioning.

## M9 — Parquet writer and release manifest

### M9.1 — Implement incremental Parquet sharding

- **Objective:** Write typed bounded-size Parquet outputs.
- **Allowed scope:** Writer, shard layout, schema metadata.
- **Dependencies:** M5.1, M5.2, M7.2, M8.3.
- **Implementation requirements:** Incremental writes; explicit compression; target shard range and partitioning selected by benchmark; avoid empty/tiny partition explosions; record row counts and hashes.
- **Tests:** Round-trip schema/readability, shard boundary behavior, deterministic logical content, nullable fields.
- **Acceptance criteria:** No monolithic full-data materialization; each shard matches declared schema and has auditable counts.
- **Out of scope:** Publishing to Hugging Face.

### M9.2 — Define and emit deterministic release manifest

- **Objective:** Capture identities, config, inputs, counts, and output checksums.
- **Allowed scope:** Manifest contract, serializer, verifier.
- **Dependencies:** M2.2, M9.1.
- **Implementation requirements:** Semantic release identity excludes timestamps; include all schema/split/builder identities, input manifest/config digests, per-view/split counts, rejects/quarantine and file hashes.
- **Tests:** Golden manifest, timestamp invariance for semantic ID, missing/extra output detection.
- **Acceptance criteria:** Manifest verifier detects mismatch between declared and actual artifacts.
- **Out of scope:** Claiming cross-library byte-reproducibility absent proof.

## M10 — Validation, statistics, and performance

### M10.1 — Implement validation gates and build report

- **Objective:** Run integrity and quality checks across the full pipeline.
- **Allowed scope:** Validation commands/reports and tests.
- **Dependencies:** M2–M9.
- **Implementation requirements:** Cover all SPEC validation gates; classify fatal/unsupported/rejected/quarantined/warning; require explained counts and fail release on fatal issues.
- **Tests:** Injected failures per gate; end-to-end tiny fixture.
- **Acceptance criteria:** Reports are machine-readable and auditable; unexplained count discrepancies prevent release completion.
- **Out of scope:** Relaxing failures to make a build pass.

### M10.2 — Add stage statistics and benchmark suite

- **Objective:** Measure throughput and resource use on representative archives.
- **Allowed scope:** `stats` and benchmark tools/reports.
- **Dependencies:** M4, M9, M10.1.
- **Implementation requirements:** Measure compressed GB/min, rows/sec, peak RSS, output size, compression ratio, and stage timings; record environment/config; no full data committed.
- **Tests:** Benchmark smoke run on tiny fixture; metric schema test.
- **Acceptance criteria:** Measurements are repeatable enough to compare implementation changes and used before tuning defaults.
- **Out of scope:** Premature distributed/cluster processing.

## M11 — Hugging Face release packaging

### M11.1 — Define public view/config layout and dataset card

- **Objective:** Package independently consumable Parquet views with complete documentation.
- **Allowed scope:** Dataset card, configuration metadata, release packaging.
- **Dependencies:** M9.2 and M10.1.
- **Implementation requirements:** `replay_events`, `games`, `decision_imitation` where available; document source attribution, per-source licenses, limitations, provenance, schema, method, quality, splits, citation, biases, and reconstruction caveats.
- **Tests:** Card/manifest consistency and configuration loading against tiny release.
- **Acceptance criteria:** Users can select a view; no unverified license/source is included; no endorsement implication.
- **Out of scope:** Publishing without explicit release operation; `draft_decisions` implementation.

### M11.2 — Add publication tooling and dry-run verification

- **Objective:** Make release upload auditable and repeatable.
- **Allowed scope:** Upload tooling/config and release preflight.
- **Dependencies:** M11.1.
- **Implementation requirements:** Verify hashes, manifest/card/config agreement, license gates, and destination identity before upload; support dry-run; never embed credentials in repository.
- **Tests:** Mocked upload, mismatch rejection, dry-run no-write.
- **Acceptance criteria:** Publication cannot proceed from an incomplete or invalid manifest; uploaded artifacts correspond to one release ID.
- **Out of scope:** Publishing M0 docs or data before source review.

## M12 — First public dataset build

### M12.1 — Select and build a bounded representative release candidate

- **Objective:** Exercise the end-to-end system against selected compatible real archives.
- **Allowed scope:** Ignored local data, reports, small committed metadata if appropriate.
- **Dependencies:** M10 and M11.
- **Implementation requirements:** Choose representative replay/game pairs covering supported schema families; record exclusions, license checks, join coverage, decision yield, quality distributions, and resource use.
- **Tests:** Full validation gates; manual review of sample records and perspective safety.
- **Acceptance criteria:** Candidate passes all release gates, or produces a precise blocker report and no publishable release.
- **Out of scope:** Training models; claiming all-history coverage.

### M12.2 — Review candidate limitations and approve release contract

- **Objective:** Resolve open release-critical issues and freeze v0.1 scope.
- **Allowed scope:** ADRs, dataset card, release metadata.
- **Dependencies:** M12.1.
- **Implementation requirements:** Review schemas, joins, privacy, attribution/licenses, split isolation, reconstruction quality, and observed biases.
- **Tests:** Independent reproducibility check from same inputs/config.
- **Acceptance criteria:** Release scope and limitations are explicit; unresolved critical issues block publication rather than being assumed away.
- **Out of scope:** Expanding to unsupported archive families.

## M13 — Scale-out build

### M13.1 — Inventory all available compatible archives

- **Objective:** Catalog user-downloaded Replay/Game/Draft archives and classify compatibility.
- **Allowed scope:** Manifests, schema reports, compatibility inventory.
- **Dependencies:** M12 release contract.
- **Implementation requirements:** Hash every archive; verify filename/source/license; classify supported, unsupported, incomplete, and unknown; Draft remains catalogued even when not processed.
- **Tests:** Inventory count reconciliation and deterministic rerun.
- **Acceptance criteria:** Every discovered file is accounted for; unsupported archives have actionable reasons.
- **Out of scope:** Forcing incompatible history into v0.1.

### M13.2 — Build all compatible gameplay views

- **Objective:** Produce a full-scale release from compatible licensed Replay/Game archives.
- **Allowed scope:** Local ignored inputs and release artifacts.
- **Dependencies:** M13.1, M10, M11.
- **Implementation requirements:** Bounded-memory, resumable execution; per-source reports; complete release manifest; all quality and split gates.
- **Tests:** Full validation; sample and shard audit; deterministic semantic rebuild check on selected subset.
- **Acceptance criteria:** No silent exclusions; all files accounted for; complete release passes privacy/license/provenance checks.
- **Out of scope:** Draft decision view and model training.

## M14 — Optional model adapters

### M14.1 — Add a consumer adapter only after core stability

- **Objective:** Convert stable core views into one requested model/tool format.
- **Allowed scope:** Separate adapter package/export layer.
- **Dependencies:** Published/stable DecisionSample contract and explicit consumer request.
- **Implementation requirements:** Preserve core sample IDs and contract versions; document lossy transformations; keep architecture/tokenizer-specific fields outside core schemas.
- **Tests:** Adapter contract and round-trip/linkage tests where possible.
- **Acceptance criteria:** Adapter output maps back to model-independent records and does not alter the core release.
- **Out of scope:** Training Laya, MageZero, Forge models, or redefining OpenMTGData around a consumer.

## Cross-milestone review gates

1. M3 evidence is a hard gate before source adapters lock columns or types.
2. M6 evidence is a hard gate before replay/game records are joined.
3. M7 safety evidence is a hard gate before policy imitation samples are emitted.
4. M8 grouping evidence is a hard gate before split labels are published.
5. M11 license and privacy preflight is a hard gate before any dataset release.
6. Every release records unresolved questions and affected exclusions; no milestone may resolve them by silent inference.
