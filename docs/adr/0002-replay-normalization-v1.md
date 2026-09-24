# ADR 0002: Conservative Replay turn-summary normalization v1

**Status:** Accepted for the M5.1 implementation on `impl/m5-replay-event-v1`

**Decision date:** 2026-09-24

**Evidence inputs:** M3.1 deep report `2572d8825d5eff17ad779695102f97d1ebb04b607545b02d1001d48461744add`; M3.2 registry `7d3ef3af4304edd9a3aff88aee49f76e5e4c50b2908608b8f0b9e44a3f9fa1fb`; semantic source catalog `dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602`.

## Context and observed structure

M3.1 contains 41 Replay physical-schema groups representing 100 archives. Every inspected group has a large row-wide header and repeated `user_turn_<n>_*` / `oppo_turn_<n>_*` field families over slots 1 through 30. These are physical header facts. The repeated names alone do not establish event semantics.

M5.1 reviewed the `replay_data_public.AFR.PremierDraft.csv.gz` raw schema (`7529bd97caa882625c1f6c250a1765e060bb47db51700e1311fa6caab0e8bead`) against its M3.2 structural interpretation contract and then processed the complete source through M4.1. Its scalar source fields include `turns`, `on_play`, and `game_index`; its per-side indexed families have 30 contiguous slots with the same 33 exact suffixes at each slot. Actual accepted rows contain one total-turn value and per-side slot data. For the reviewed full stream, every normalized row's emitted slot count reconciled with its parsed source `turns` value, and every active slot had at least one populated mapped source field. `on_play` used exact `0`/`1` values for this mapping.

The resulting M5.1 interpretation is deliberately narrow:

> One accepted AFR PremierDraft Replay row represents a game-level replay summary containing zero or more per-side turn-summary slots. It does not represent a sequence of individual actions.

`ReplayEventV1` therefore denotes one **source turn-summary slot**. It carries every exact parser-returned slot field string plus the exact raw `turns` and `on_play` lexemes, alongside deterministic parsed/slot metadata. It does not claim that an individual card/action event was observed or reconstructed.

The source names `user_turn` and `oppo_turn` define the source-relative slot families. The reviewed `on_play` value selects which family starts; the two families then alternate. `event_ordinal_within_source_record` is zero-based chronological turn order under that v1 rule. `source_turn_slot_index` remains the exact one-based index within the corresponding source family. This ordering rule does not identify a policy decision or an acting player perspective.

Some later schema headers contain an `event_type` field whose observed values include format-like strings such as `TradDraft`. That field is not used to establish event boundaries or mapped by this decision.

## M5.1 contract and mapping

The implementation defines versioned Replay event, mapping, lineage, normalization, quality, locator, registry, and registry-digest contracts. Its mapping ID is derived from the canonical field/slot/parser/policy projection, exact raw fingerprint, M3.2 interpretation ID, M3 evidence binding, and reviewed source archive reference. It excludes local paths, timestamps, runtime, and sample values. Revising a mapping creates a new M5.1 mapping ID; it does not alter the M2 raw fingerprint or M3 interpretation ID.

For the supported AFR PremierDraft group, the mapping ID is `openmtgdata.replay-field-mapping.v1:67c0cd7c08142a18ec630a01a9fd6a6fe1bf4fba9271035d1cde386d99693ef3`; the mapping-registry digest is `79388e7df467151d45eb6a4a964aeb9fae1159a913ed8cab4356f373af53f076`. The adapter maps exactly 1,980 turn-slot columns using `(column_index, exact_header_name)` selectors, plus the `turns` and `on_play` source columns. Their parser-returned strings, including the exact `turns` and `on_play` lexemes, empty strings, and whitespace, remain unchanged in each event. It adds four deterministic normalized fields: source turn count, source `on_play`, source-side slot family, and source slot index. The turn count parser accepts canonical nonnegative ASCII decimal strings; `on_play` accepts only `0` or `1`. Thus the mapping has 1,982 mapped physical columns (including the two context selectors), preserves both context lexemes, has four deterministic normalization outputs, and emits no reconstructed gameplay fields.

All other physical fields, including candidate-hand fields and row-level metadata, are explicitly `unmapped_preserved_at_raw_layer`. The M4 raw record remains their source authority. No field is silently discarded from the source representation.

The reviewed occupancy rule derives active side-slot counts from total `turns` and `on_play`, capped by the registered 30 slots per side. Slots beyond that declared count do not emit events; their original cells remain in M4. An active slot whose mapped source strings are all empty rejects that source row as `partial_event_slot`. A zero-turn record emits no events and retains an explicit quality code.

## Coverage decision

All 41 Replay schema groups are present in the M5.1 mapping registry. One group is `supported`: AFR PremierDraft. The other 40 groups remain `needs_review`, even where their headers show similar indexed families. Their exact schemas and M3 evidence are reported, but this task did not complete M4 full-stream occupancy/normalization review for those fingerprints, so no mapping is applied to them. This is intentional; M3.2 singleton interpretation contracts are not merged by M5.1.

The ignored local analysis and mapping registry are written under `data/intermediate/m5.1/`; neither contains raw row values or absolute raw-root paths.

## Full-stream AFR PremierDraft results

The M4.1 reader completed the registered source and verified gzip integrity and compressed SHA-256/size. M3.1's 256-row prefix had found 26 wider rows in AFR PremierDraft; the full M4.1 pass observed 248,073 records, with 223,404 exact-width records submitted to M5.1 and 24,669 width-mismatched records rejected before normalization (24,539 longer and 130 shorter). This difference shows why bounded M3 evidence is not full-history coverage. Potential semantic bias from those M4 width exclusions is unresolved; the accepted subset is not asserted to be representative.

M5.1 normalized 223,271 accepted raw records, rejected 133 for invalid/non-supported `turns` values (125) or values exceeding the physical turn-slot bound (8), and emitted 3,977,270 turn-summary events. No partial active slots, zero-event records, or turn-count mismatches were observed. Normalized records emitted 2–60 events. Event-side counts were 1,990,219 `user` slots and 1,987,051 `oppo` slots. The final incremental semantic validation digest is `2a56eee0f5ab38750b3887623fe8b94b24a18a17d3952625af62f8adda5a4788`; the finalized mapping ID is `openmtgdata.replay-field-mapping.v1:67c0cd7c08142a18ec630a01a9fd6a6fe1bf4fba9271035d1cde386d99693ef3` and mapping-registry digest is `38afd68bd841541e317ad97b2b511a1235a1258d3b4b61ac1bc54e9e9f3b7a44`. Exact counters and the turn-count distribution are in the ignored `normalization-smoke.json` report.

## Limits and deferred semantics

This mapping is evidence-supported for the AFR PremierDraft physical fingerprint under the reviewed M4.1 full-stream run. The M3.1 lexical evidence was bounded to a prefix and is not full-history type proof. Per-slot strings remain untyped source facts; empty strings remain strings. Numeric-looking strings such as card-like codes are not converted or enriched.

No Game archive was read for this mapping. No Replay/Game join, card database lookup, action/decision identification, perspective claim, game-state reconstruction, legal-action assertion, optimal-action assertion, or `DecisionSampleV1` was introduced. Those questions remain deferred to M6/M7 and later evidence review.
