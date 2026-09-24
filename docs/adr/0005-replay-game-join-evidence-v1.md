# ADR 0005: AFR PremierDraft Replay/Game join evidence

**Status:** M6.1 evidence complete; no authoritative row join key approved.

## Bound evidence and scope

The evidence report is `openmtgdata.replay-game-join-evidence.v1` with semantic digest `24a62c45f0b95de75069e757995a7d9f7e2c4844b0882eeb1550fd1d51827ff5`. Its report-level decision is `join_unsupported`, scoped to the AFR PremierDraft reviewed archive pair and the declared candidate inventory; no candidate is approved. Both M4-v2 streams have `complete` terminal status. The report binds source catalog `dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602`, corrected M3.1 evidence `c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29`, M3.2 registry `66d58e20fa2adbcbfdcfd927c1074b5807d43af8af94103b61b26fc06f13661c`, Replay mapping `openmtgdata.replay-field-mapping.v1:a42f925293705fb89936e3b3b5a6d243027d63953808ccfb154920c0a6031140` / registry `43be7080894b55c60f58d569ed8cfeb52a9dbae1fa8688160d91196044ab47f3`, and Game mapping `openmtgdata.game-field-mapping.v1:c529133666df442a69b1a4be5e28f743a07098dbb27405ab8499867f7c055e1a` / registry `43d78b9e2cecf5f420046ace93c58f12f7fbfd563b8b3989ca085c90e0533593`.

Only the exact corrected AFR PremierDraft archives were examined: Replay source `b739dacc3082d356b27001aa6fd91257ae766a8d1aec23272e656610cfe3b54b` and Game source `7cc18827a1f4ea5be145d1a31d2a0a78e03e7b747829c8a3c1e1336752381b91`. M4 reader v2 reached complete terminal verification for both. No other archive in a shared physical schema group is implicitly covered.

The analysis unit is one accepted M4 source row on each side. Replay M4 saw 248,072 rows, accepted 223,404, and rejected 24,668 for width mismatch. Those rejected rows were excluded from key-value evidence and remain prominently accounted for. All 366,660 Game rows seen were accepted. The 133 Replay rows rejected by M5.1 were still included in candidate-key analysis; M5 survival is reported separately. No `ReplayEventV1` turn-slot multiplication was used.

## Exact candidate surfaces tested

All values were compared as exact M4 parser-returned strings. No trim, case conversion, integer coercion, leading-zero removal, timestamp, row order, file position, proximity, or fallback rule was used.

| Candidate | Replay selectors | Game selectors | Result |
| --- | --- | --- | --- |
| `a16fdbb414381e847d6f4d3217e4d975762e16b24496a5e3482b1607c5d5b2ce` | `(0, draft_id)` | `(2, draft_id)` | Ambiguous |
| `37932227aeb21844a2cf0e4a7956c45f63547a1e14c285af54dc3769c88900c8` | `(0, draft_id)`, `(7, game_index)` | `(2, draft_id)`, `(7, game_number)` | Ambiguous |
| `1f162ccb86d446278599defbc6856034ab4273d269a467a975fca011dd1e13b7` | `(0, draft_id)`, `(3, expansion)`, `(4, format)`, `(7, game_index)` | `(2, draft_id)`, `(5, expansion)`, `(6, event_type)`, `(7, game_number)` | Ambiguous |

Replay `history_id` and Game `build_index` were inventoried as source-only ID/index-like fields; no exact counterpart field exists in the other header, so they were not paired into a join candidate. `event_type` and `format` were tested only as exact context-value components; their names do not establish equivalent semantics.

For all three tested candidates, the complete key components were non-empty on all eligible accepted rows. Replay had 41,939 distinct complete keys, 40,849 duplicate keys involving 222,314 rows. Game had 62,271 distinct complete keys, 61,224 duplicate keys involving 365,613 rows.

The cardinality was identical across the three candidates:

- 607 keys were 1 Game : 1 Replay.
- 2 keys were 1 Game : N Replay.
- 481 keys were N Game : 1 Replay.
- 40,845 keys were N Game : N Replay.
- Replay-only keys: 4 total, covering 2,750 rows.
- Game-only keys: 20,336 total, covering 119,560 rows.
- Ambiguous shared keys affected 220,047 Replay rows and 246,493 Game rows.

Only 607 rows per side were in 1:1 keys: 0.271705% of complete-key Replay rows (607/223,404), or 0.244684% of all Replay rows seen by M4 (607/248,072); and 0.165548% of Game rows (607/366,660). These percentages describe observed key cardinality, not approved game correspondence.

Among candidate-associated Replay rows, 607 1:1 rows and 219,919 ambiguous rows survived M5.1; 128 ambiguous rows and 5 unmatched rows were rejected by M5.1. The remaining 2,745 unmatched rows survived. Thus the M5.1 total remains 223,271 normalized plus 133 rejected. The M4 width-rejected 24,668 rows are a separate excluded population.

## Numeric relation and Game-row multiplicity

Within 41,935 shared exact `draft_id` groups, all 41,935 distinct canonical Game `game_number` values had an equal Replay `game_index` value; no `+1` or `-1` relation or violation was observed at that distinct-value/group scope. This is a measured value relation only. It does not make `(draft_id, game_number)` a row key: the exact composite still produced the same N:N collisions and unmatched rows. No offset normalization was applied.

For Game `(draft_id, game_number)`, key multiplicities were: 1 row for 1,047 keys; 2 rows for 1,252 keys; 3 for 6,079; 4 for 9,304; 5 for 10,297; 6 for 9,189; 7 for 8,280; 8 for 8,330; and 9 for 8,493. Another 59,972 keys had more than two rows. Among the 1,252 exactly-two-row keys, exact `rank`/`opp_rank` reciprocity was observed for 0; mulligan-count reciprocity for 822; source `on_play` lexemes were equal for 625 and different for 627; source `won` lexemes were equal for 770 and different for 482. These aggregates do not establish participant pairing or perspective.

## Decision and limits

All three candidates are classified `ambiguous`; none is approved. The machine-readable overall status is `join_unsupported`: under this documented candidate scope, no authoritative Replay/Game row key was established. This does not rule out every possible future key or evidence source. The observed equality of numeric game values does not resolve the many-to-many and unmatched row multiplicity or establish source semantic correspondence. M6.2 must not emit joined records from these candidates without additional source evidence and a new scoped review.

Replay `user`/`oppo` and Game source-relative labels remain separate. Participant-side correspondence is **not established**. Actor identity, SELF/OPPONENT policy views, `DecisionSampleV1`, and all downstream behavior semantics remain M7 work. No timestamps/proximity/order fallback, actual join output, Parquet, external lookup, or network work was performed.

The compact ignored report is `data/intermediate/m6.1/replay-game-join-evidence.json`; it contains no source row values or absolute raw-root paths.
