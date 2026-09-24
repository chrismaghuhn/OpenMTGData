# ADR 0003: Evidence-gated Game source facts

Status: accepted for M5.2, 2026-09-24

## Decision

M5.2 keeps the M3 physical fingerprint and structural interpretation identity unchanged and adds a separate deterministic `game_field_mapping_id`. All 36 observed Game groups (100 archives) are present in the registry. Only a schema independently reviewed through a complete M4 stream may be supported; the other 35 groups remain `needs_review` until their own evidence is examined. The registry binds M3.2 digest `7d3ef3af4304edd9a3aff88aee49f76e5e4c50b2908608b8f0b9e44a3f9fa1fb` and M3.1 evidence digest `2572d8825d5eff17ad779695102f97d1ebb04b607545b02d1001d48461744add`.

The reviewed AFR PremierDraft source has 1,082 physical columns and 366,661 logical rows, of which M4 accepted 366,660 exact-width rows. Its header's first field contains a long NUL/TAR metadata prefix followed by `user_win_rate_bucket`; this exact physical header is retained for fingerprint identity, but column 0 is left unmapped. Columns 1–17 have exact names and were mapped only as source-labeled, exact CSV strings. The remaining card-count columns stay available at the M4 raw layer. The reviewed mapping ID is `openmtgdata.game-field-mapping.v1:ed163bafaed657b8a6a78025809bc0b95aee41444a6fcac156334441e394881c`; registry digest is `d5afa30e5af990bc67dc68e91d0f56ad754c44d20d0df3dbb0dda33f3244f4e3`. This is not a claim that a source row is a globally unique game, nor that rows pair one-to-one with Replay records. Rows are called source rows; observed user/opponent-relative columns remain source-relative.

## Field and row policy

Each mapped field is selected by `(column_index, exact_header_name)`, has a lineage entry that matches the actual one-column copy operation, and preserves the parser-returned string exactly. No trimming, type coercion, empty-to-null conversion, date parsing, player perspective conversion, game-state reconstruction, or derived gameplay field is produced. Source-native `draft_id` and `game_number` remain source lexemes and are not approved join keys or uniqueness claims.

All rows must have the exact physical header width. M4 rejects short and long rows without padding, truncation, or invented names. M5.2 receives accepted M4 records only. For AFR, the single short row observed by M4 is excluded from normalization; any bias from that exclusion is unresolved.

## Explicit limits

M3.1 lexical classes describe bounded observations and do not establish semantic types or nullability. Source empty remains `""`. Unknown columns are marked `unmapped_preserved_at_raw_layer`. Outcome-like `won` is preserved only as `source_won_raw` and is not a policy input. Labels such as `on_play`, `rank`, and `opp_rank` remain source values without actor or perspective interpretation.

Replay/Game joins and join-key approval belong to M6. SELF/OPPONENT policy perspective belongs to M7; future policy views must canonicalize the acting player as SELF and the other player as OPPONENT while keeping source identities as provenance. M5.2 does not emit those views. It also does not assert legality/optimality, enrich card names, access Replay data, publish Parquet, or train models.

Mapping corrections change the M5.2 mapping/registry identities and downstream semantic validation digests only; they do not rewrite source IDs, raw fingerprints, or M3.2 interpretation IDs.
