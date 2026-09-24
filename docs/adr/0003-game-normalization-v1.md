# ADR 0003: Evidence-gated Game source facts

Status: accepted for M5.2 on the corrected source-container authority, 2026-09-24

## Decision and evidence binding

M5.2 adds a deterministic `game_field_mapping_id` without changing source archive IDs or M3 physical fingerprints/interpretation contracts. It accounts for all **35 Game schema groups / 100 archives** in the corrected M3 evidence. One group is supported after complete M4-v2 and M5.2 review; the other 34 remain `needs_review`.

The registry binds source catalog `dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602`, M3.1 evidence `c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29`, and M3.2 registry `66d58e20fa2adbcbfdcfd927c1074b5807d43af8af94103b61b26fc06f13661c`.

## AFR PremierDraft source

The registered `.csv.gz` artifact is gzip containing one TAR member, `game_data_public.AFR.PremierDraft.csv`. Container policy `openmtgdata.source-container-policy.csv-gzip-v1` selects that exact regular member; M4 verifies it through CSV EOF, the single-member TAR trailer, gzip EOF, and the registered outer compressed bytes. The corrected CSV header has 1,082 physical fields and begins with exact field `user_win_rate_bucket`. Its physical fingerprint is `a2bddf4d55c72c4c329eda7af4740d0fce03a66a626f1c44148282ae80b5b840`; source archive ID remains `7cc18827a1f4ea5be145d1a31d2a0a78e03e7b747829c8a3c1e1336752381b91`.

The M4-v2 full stream saw and accepted 366,660 CSV data records, rejected zero for row width, and verified 26,186,466 registered compressed bytes. M5.2 submitted and normalized all 366,660 accepted records, rejected zero, and emitted 18 source-direct string facts per record. The mapping ID is `openmtgdata.game-field-mapping.v1:c529133666df442a69b1a4be5e28f743a07098dbb27405ab8499867f7c055e1a`; the mapping-registry digest is `43d78b9e2cecf5f420046ace93c58f12f7fbfd563b8b3989ca085c90e0533593`. The incremental semantic validation digest is `822b56cd838c13268c30344c02ffe96553336301b5e2849a830eb879b8689cb6`.

Exact source strings from columns 0–17 are selected by `(column_index, exact_header_name)`. All 18 use source-direct lineage and preserve empty strings. No type conversion, date parsing, null inference, Unicode/string normalization, or reconstructed field is emitted. The other 1,064 physical fields, including card-count columns, remain explicitly `unmapped_preserved_at_raw_layer`. The local machine-readable analysis and registry are ignored under `data/intermediate/m5.2/` and contain no source row values or raw-root paths.

## Row and field meaning limits

One accepted CSV row is the normalization unit. No cross-row grouping, global game uniqueness, participant pairing, or Replay correspondence is asserted. `draft_id`, `game_number`, `on_play`, `rank`, `opp_rank`, and `won` remain source-labeled lexemes; no join key, actor, outcome policy, or player perspective is inferred. Source URLs and license status remain unknown, and chronological coverage remains unavailable.

M4 width rejection does not exclude any rows from this corrected AFR source. Other sources may still have width-rejected rows; any downstream bias must be reviewed per source. M6 owns joins and join-key approval. M7 owns actor-relative policy views: future `DecisionSampleV1` policy views must map the acting player to SELF and the other player to OPPONENT while retaining absolute/source identity only as provenance. M5.2 does not implement these views, Replay/Game joins, game-state reconstruction, legal/optimal labels, card enrichment, Parquet, or training.
