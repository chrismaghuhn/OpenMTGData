# ADR 0007: Model source turn-summary sequences separately from decisions

Status: accepted for the reviewed AFR PremierDraft Replay source, 2026-09-24

## Context

M7.1 established that the supported AFR PremierDraft Replay source contains game-level rows with alternating per-side turn-summary slots. It does not establish atomic Magic actions, decision boundaries, a decision actor, an exact before-state, or actor-visible information. Its report status is `turn_summary_only`; action-level `DecisionSampleV1` and behavior cloning remain unsupported for this source.

That result does not block the project's original source-sequence objective. M7.2 defines a distinct prediction task: completed source turn summaries before ordinal K predict the completed source turn summary at K. This predicts source summaries; it does not claim to predict a choice made by a player.

## Authority and population

This decision applies only to the exact AFR PremierDraft Replay archive:

| Authority | Value |
| --- | --- |
| Source archive ID | `b739dacc3082d356b27001aa6fd91257ae766a8d1aec23272e656610cfe3b54b` |
| Compressed SHA-256 | `43aad0b42b6c9bb4af6431e8e1910da6a324d63a87f7cbc6d37057355cb0fccc` |
| Source catalog digest | `dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602` |
| M3.1 evidence digest | `c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29` |
| M3.2 registry digest | `66d58e20fa2adbcbfdcfd927c1074b5807d43af8af94103b61b26fc06f13661c` |
| Raw schema fingerprint | `b4ad0cb27197cf3e37e59d6609a6098da0e0e2f6c4f98b82c45f85fcb7bbea75` |
| M3.2 interpretation | `openmtgdata.source-interpretation.v1:d93900da88eba55049b3fa80e8c909522201fb05f300e4765dcf814763b06d13` |
| M4 reader | `openmtgdata.raw-source-reader.v2` |
| M5.1 mapping | `openmtgdata.replay-field-mapping.v1:a42f925293705fb89936e3b3b5a6d243027d63953808ccfb154920c0a6031140` |
| M5.1 registry digest | `43be7080894b55c60f58d569ed8cfeb52a9dbae1fa8688160d91196044ab47f3` |
| M5.1 semantic validation digest | `24db9aba56d48cbf1403a71f128fa870e065df850b9d93db3efdeeac67dbfe08` |
| M7.1 decision evidence digest | `be2d6ca4b109d1f69d1034ea107e935e37d9a5d78c6aa0bd9518f4bdbf166c59` |

The stream was read through M4-v2 and normalized through M5.1. M4 saw 248,072 rows: 223,404 accepted and 24,668 width-rejected (24,539 longer, 129 shorter). M5.1 normalized 223,271 accepted rows and rejected 133 (125 invalid/noncanonical turn counts; 8 above the physical slot capacity). Only normalized M5.1 rows produce sequences; rejected M4/M5 rows are not repaired or admitted.

## Contracts and projection

The implementation introduces:

- `openmtgdata.turn-summary-frame.v1`
- `openmtgdata.turn-summary-sequence.v1`
- `openmtgdata.turn-summary-sequence-locator.v1`
- `openmtgdata.next-turn-prediction-view.v1`
- `openmtgdata.next-turn-prediction-view-locator.v1`
- `openmtgdata.turn-sequence-build-report.v1`
- `openmtgdata.turn-sequence-build-digest.v1`
- `openmtgdata.turn-sequence-logical-digest.v1`

One sequence represents one M5.1-normalized source row. Each frame retains the M5.1 event ordinal, exact source side (`user` or `oppo`), side-local slot index, and all 33 exact decoded suffix values as strings. Empty strings, whitespace, delimiters, and spelling are preserved. Values are not parsed, split, interpreted as card names, or converted to action/target semantics.

The suffix values are stored in this exact canonical, lexicographically sorted order:

```text
cards_discarded
cards_drawn
cards_foretold
creatures_attacked
creatures_blocked
creatures_blocking
creatures_cast
creatures_unblocked
eot_oppo_cards_in_hand
eot_oppo_creatures_in_play
eot_oppo_lands_in_play
eot_oppo_life
eot_oppo_non_creatures_in_play
eot_user_cards_in_hand
eot_user_creatures_in_play
eot_user_lands_in_play
eot_user_life
eot_user_non_creatures_in_play
lands_played
non_creatures_cast
oppo_abilities
oppo_cards_learned
oppo_creatures_killed_combat
oppo_creatures_killed_non_combat
oppo_instants_sorceries_cast
oppo_mana_spent
player_combat_damage_dealt
user_abilities
user_cards_learned
user_creatures_killed_combat
user_creatures_killed_non_combat
user_instants_sorceries_cast
user_mana_spent
```

The suffix ordering contract plus the reviewed M5.1 mapping recovers the exact physical `(column_index, header_name)` selector for each side/slot/suffix. This is a source projection, not a renaming into stronger game semantics.

The model-facing frame allowlist is exactly:

```text
event_ordinal_within_source_record
source_turn_side
source_turn_slot_index
the 33 exact suffix string values
```

The projection excludes row-global fields, including `turns`, `source_turn_count`, `source_turn_count_raw`, `on_play`, `won`, `user_total_*`, `oppo_total_*`, draft/player/rank/time/color/hand context, all Game fields, and all fields outside the reviewed turn-slot family. It also excludes frames after the target ordinal. No static context is added in v1.

`source_turn_side` is the source-side ordering label established by M5.1. It is neither a decision actor nor `SELF`/`OPPONENT`. The source does not establish that all activity in a turn summary was performed by the side whose turn it is. Visibility is unknown, including whether the source contains information private to either side. Accordingly each sequence/view declares `information_scope=source_record_summary_not_actor_observation`, `usable_for_policy_imitation=false`, and `actor_perspective_safe=false`. This task creates no actor-relative transformation.

## Virtual next-turn views

For a sequence with frames `T0` through `T(N-1)`, v1 defines views only for target ordinal `K` in `1..N-1`:

```text
context = frames[0:K]
target  = frames[K]
objective = next_source_turn_summary
```

There is no target for ordinal zero. A view stores its source-row locator and target ordinal; a resolver reads the context prefix and target from the sequence on demand. Full prefixes are not copied or persisted. Later frames cannot enter an earlier view. The target can summarize its completed turn because the task predicts a completed source turn summary, not an intra-turn action.

No final M8 IDs are assigned. Sequence locator is `(source_archive_id, data_record_ordinal)`; virtual view locator adds `target_event_ordinal`. M8 must derive final deterministic IDs from the sequence identity and view contract, keep every view from a sequence in the same split, and investigate stronger grouping identities before selecting the split hierarchy.

## Full-stream result

One streaming build produced the following reconciled result:

| Measure | Result |
| --- | ---: |
| M4 rows seen / accepted / rejected | 248,072 / 223,404 / 24,668 |
| M5 submitted / normalized / rejected | 223,404 / 223,271 / 133 |
| Sequences | 223,271 |
| Frames | 3,977,270 |
| Virtual next-turn views | 3,753,999 |
| Sequence length | 2–60 |
| `user` frames | 1,990,219 |
| `oppo` frames | 1,987,051 |
| Shards | 45 gzip JSONL shards, at most 5,000 sequences each |

The incremental logical sequence digest is:

```text
428d2637726efbbcfd6c9629e158f70b96acc144a8510332edc696df8ff498c1
```

The semantic build-report digest is:

```text
9e922cf7a444f2c32610a7f7193f3f23823ced00e1f8d05eeb46be0a4dc98ae8
```

Logical identity hashes canonical sequence projections with input contract/source bindings and length-delimited records. It excludes paths, timestamps, host/runtime details, shard boundaries, and gzip bytes. Gzip metadata uses `mtime=0`, but the semantic digest is over logical sequence records.

The local ignored intermediate is `data/intermediate/m7.2/turn-summary-sequences/`, with `part-NNNNN.jsonl.gz` shards and `turn-sequence-build-report.json`. Shards contain each sequence once, not a materialized training-view table. These are local training intermediates, not approved release artifacts; source provenance/license publication gates remain in force.

## Decision and boundaries

This is source-sequence modeling, not action-level behavior cloning. It does not emit `DecisionSampleV1`, claim an action/decision/target/actor, assert legal or optimal play, infer visibility, join Game records, use the M6.1 1:1 candidates, construct SELF/OPPONENT observations, tokenize for Laya, build Parquet, or train a model. M7.3 remains deferred until an authoritative action-level source establishes decision boundaries, actors, before-state, and information visibility. The broader current 17Lands path may proceed through M8 identity/dedup/splits for this non-policy sequence objective without weakening M7.3 requirements.
