# ADR 0006: Replay decision boundaries and perspective evidence v1

**Status:** M7.1 evidence complete for one corrected AFR PremierDraft Replay archive. Overall result: `turn_summary_only`; no action-level decision type or behavior-cloning input is approved.

## Scope and authority

M7.1 is Replay-only. M6.1's report-level conclusion is `join_unsupported` for the reviewed AFR PremierDraft pair and candidate inventory (report digest `24a62c45f0b95de75069e757995a7d9f7e2c4844b0882eeb1550fd1d51827ff5`). This analysis did not read Game records, use the 607 candidate 1:1 values, infer participant alignment, or run M6.2.

The reviewed Replay archive is `b739dacc3082d356b27001aa6fd91257ae766a8d1aec23272e656610cfe3b54b`, compressed SHA-256 `43aad0b42b6c9bb4af6431e8e1910da6a324d63a87f7cbc6d37057355cb0fccc`, compressed size 139,185,860 bytes. It is bound to raw fingerprint `b4ad0cb27197cf3e37e59d6609a6098da0e0e2f6c4f98b82c45f85fcb7bbea75`, M3.2 contract `openmtgdata.source-interpretation.v1:d93900da88eba55049b3fa80e8c909522201fb05f300e4765dcf814763b06d13`, M5.1 mapping `openmtgdata.replay-field-mapping.v1:a42f925293705fb89936e3b3b5a6d243027d63953808ccfb154920c0a6031140`, M5.1 registry digest `43be7080894b55c60f58d569ed8cfeb52a9dbae1fa8688160d91196044ab47f3`, and M4 reader `openmtgdata.raw-source-reader.v2`. The report also binds source catalog `dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602`, corrected M3.1 evidence `c6c7d6e81c96179f41f32c27e5bfaf52e241a78f00478c8567c12a09ede9ba29`, and corrected M3.2 registry `66d58e20fa2adbcbfdcfd927c1074b5807d43af8af94103b61b26fc06f13661c`.

The deterministic M7.1 report digest is `be2d6ca4b109d1f69d1034ea107e935e37d9a5d78c6aa0bd9518f4bdbf166c59`. It covers the exact physical field inventory, bounded M4 shape/co-occurrence measurements, candidate dispositions, perspective findings, redacted locators/turn slots, and limitations. The separate field matrix and annotated examples repeat these bindings. Local artifacts are ignored under `data/intermediate/m7.1/`.

## Population and evidence scopes

The current M5.1 review records a completed M4-v2 full stream: 248,072 Replay data rows seen, 223,404 exact-width rows accepted, and 24,668 width-rejected rows. M5.1 submitted the 223,404 accepted rows, normalized 223,271, rejected 133, and emitted 3,977,270 per-side turn-summary slots. Its semantic validation digest is `24db9aba56d48cbf1403a71f128fa870e065df850b9d93db3efdeeac67dbfe08`. That full-stream review establishes M5.1 turn-count/slot reconciliation and source integrity; it does not establish action, actor, target, visibility, or decision semantics.

M7.1 independently reopened only this Replay source through M4-v2 and interpreted the deterministic first 256 logical CSV rows. Of these, 230 were accepted and 26 rejected for width mismatch; the 230 accepted rows were submitted to M5.1 and all 230 normalized. The M7.1 prefix reader intentionally ended with `incomplete_consumer_stop`; it did not claim another terminal verification. Its field-specific values were reduced to lexical/shape counters and redacted examples. No raw row or raw field value is retained in the report.

M3.1 lexical counts are grouped by the shared corrected physical fingerprint. They cover bounded prefixes from both PremierDraft and TradDraft archive members in that schema group and are not PremierDraft-only lexical proof. The archive-specific M4 prefix above is separately labeled. No decision type is supported from bounded evidence alone.

## Physical fields and turn families

The exact AFR header contains 2,022 fields: 42 row-level fields and 1,980 indexed turn-slot fields. M5.1 maps 1,982 physical fields (the 1,980 slot fields plus `turns` and `on_play`); 40 remain at M4 raw authority. Each field is listed by `(column_index, exact_header_name)` in `decision-field-safety.json`; no duplicate or empty name is merged or synthesized.

The 42 row-level names are:

```text
draft_id, history_id, time, expansion, format, user_rank, oppo_rank, game_index,
user_deck_colors, oppo_deck_colors, user_mulligans, oppo_mulligans, on_play, turns,
won, candidate_hand_1, candidate_hand_2, candidate_hand_3, candidate_hand_4,
candidate_hand_5, candidate_hand_6, candidate_hand_7, opening_hand,
user_total_cards_drawn, user_total_cards_discarded, user_total_lands_played,
user_total_cards_foretold, user_total_creatures_cast, user_total_non_creatures_cast,
user_total_instants_sorceries_cast, user_total_cards_learned, user_total_mana_spent,
oppo_total_cards_drawn, oppo_total_cards_discarded, oppo_total_lands_played,
oppo_total_cards_foretold, oppo_total_creatures_cast, oppo_total_non_creatures_cast,
oppo_total_instants_sorceries_cast, oppo_total_cards_learned, oppo_total_mana_spent,
missing_diffs
```

Each exact suffix below appears for slots 1–30 under both `user_turn_N_` and `oppo_turn_N_`:

```text
cards_discarded, cards_drawn, cards_foretold, creatures_attacked, creatures_blocked,
creatures_blocking, creatures_cast, creatures_unblocked, eot_oppo_cards_in_hand,
eot_oppo_creatures_in_play, eot_oppo_lands_in_play, eot_oppo_life,
eot_oppo_non_creatures_in_play, eot_user_cards_in_hand, eot_user_creatures_in_play,
eot_user_lands_in_play, eot_user_life, eot_user_non_creatures_in_play, lands_played,
non_creatures_cast, oppo_abilities, oppo_cards_learned, oppo_creatures_killed_combat,
oppo_creatures_killed_non_combat, oppo_instants_sorceries_cast, oppo_mana_spent,
player_combat_damage_dealt, user_abilities, user_cards_learned,
user_creatures_killed_combat, user_creatures_killed_non_combat,
user_instants_sorceries_cast, user_mana_spent
```

M5.1 establishes that an accepted source row is a game-level Replay summary with zero or more chronologically indexed **source turn-summary slots**. M5.1 uses the exact `on_play` lexeme and alternating source-side families to determine source turn-slot order. `ReplayEventV1` remains one source turn-summary slot. It is not an individual event, human action, decision, or policy sample. The turn-slot order rule does not establish a rules-level turn owner or the actor for any decision.

## Per-field safety findings

The versioned matrix assigns a timing, visibility, policy-input, action-candidate, perspective, before-state, target, and leakage status to every one of the 2,022 exact selectors. Counts are:

| Dimension | Observed disposition counts |
|---|---:|
| Timing | 1,980 `within_turn_unknown`; 24 `game_level`; 18 `unknown_timing` |
| Visibility | 2,022 `unknown_visibility`; no field was proven public or actor-private |
| Policy input | 1,988 `timing_ambiguous`; 20 `post_decision_only`; 8 `hidden_info_unsafe`; 6 `unsupported` |
| Source action-like candidate | 1,260 indexed cells across 21 suffix families; 762 `not_action` by this candidate screen |
| Before-state | 1,980 `turn_aggregate_only`; 42 `no_before_state` |
| Leakage | 2,002 `timing_unknown`; 19 `post_decision`; 1 `game_outcome` |

The 21 candidate activity families are `cards_discarded`, `cards_drawn`, `cards_foretold`, `creatures_attacked`, `creatures_blocked`, `creatures_blocking`, `creatures_cast`, `creatures_unblocked`, `lands_played`, `non_creatures_cast`, `oppo_abilities`, `oppo_cards_learned`, `oppo_creatures_killed_combat`, `oppo_creatures_killed_non_combat`, `oppo_instants_sorceries_cast`, `player_combat_damage_dealt`, `user_abilities`, `user_cards_learned`, `user_creatures_killed_combat`, `user_creatures_killed_non_combat`, and `user_instants_sorceries_cast`. Every candidate is `unsupported_action_structure`. The exact per-family bounded and M3 evidence, including lexical classes, empty-cell rates, same-slot co-occurrence, delimiter-character counts, and safety statuses, is in the machine report.

M4 bounded values include pipe characters in multiple families. This is only evidence that the character occurs. It does not establish a delimiter grammar, escaping, item cardinality, order, or a sequence. The sampled cells did not contain comma, semicolon, or newline separators. Several names begin with `eot_` (600 physical cells); this is a name-only end-of-turn candidate and remains `within_turn_unknown`, not a proven timing claim.

The row-level field `turns` is treated as a whole-row game-level summary and excluded as post-decision input. `won` is flagged as outcome-like by name and excluded defensively; this Replay-only task does not establish its exact source semantics. Row-level `time` is not interpreted as an event timestamp. Source IDs, rank/color/setup, totals, and other raw-only fields are not approved policy inputs.

The header includes 308 hand/draw/learned-name candidate cells and 8 row-level `candidate_hand_*` / `opening_hand` fields. These names and opaque value shapes do not prove that each value is a private card identity. Both source-side turn families and hand-like candidates are present, but actor visibility and recorder omniscience are not established. Every field's visibility remains unknown and these candidates must be excluded from policy input by default. Source presence is not actor visibility.

For a candidate decision at chronological event ordinal K, convert a source-side one-based slot index N using the M5.1 `on_play` order: `2 * (N - 1)` for the first source side and `2 * (N - 1) + 1` for the alternating side. An ordinal greater than K is future information and is rejected. Equal/current ordinals still require `PRE_TURN`/`TURN_START` timing and explicit safe policy status; no such safe field currently exists. This conversion matters because equal per-side slot indexes can have different chronological ordinals.

No physical header field names a target. The action-target status is `ACTION_TARGET_UNSUPPORTED`; object-like or pipe-containing source strings are not interpreted as Magic targets. No exact before-state is supported. Prior turn-end fields cannot be treated as the state before a later decision because intervening actions, responses, and timing are not reconstructed.

## Actor and perspective

`on_play` plus the M5.1 alternating family rule establishes source turn-side slot order only. The source does not mark an actor for each individual decision. A turn-side label is not a decision-actor label: interactive decisions, responses, abilities, blockers, triggers, and choices can occur during another player's turn. Whether actor can differ is not represented by these summaries, so equality must not be assumed. Replay `user`/`oppo` is not aligned to Game `user`/`opponent` and no participant correspondence was inferred.

Current perspective status is `actor_unknown`; `SELF`/`OPPONENT` transformation is not supportable. The permanent mapping `user = SELF` is forbidden. Future policy views must derive the actor from evidence for each decision, map that actor to `SELF`, and map the other side to `OPPONENT`. A consistent swap of source sides together with actor identity must preserve those actor-relative roles. This is a requirement for a future canonicalization layer, not a claim that the current source supports player-swap augmentation. No `PLAYER_0`/`PLAYER_1` policy identities or augmentation are introduced.

## Decision-type disposition and result

All 21 activity candidates remain `unsupported_action_structure`; none has an approved atomic action identity or decision boundary. Actor status is unknown, before-state is turn-aggregate-only, visibility is unknown, targets are unsupported, and same-slot order is unestablished. The pipe character seen in values is not parsed. No field is both a policy input and an action label. A future `PARTIAL` disposition still requires a source-backed observed-action lexeme, supported decision boundary, and supported actor; it may then describe incomplete before-state or visibility while remaining ineligible for BC use.

The report-level result is `turn_summary_only`. A turn-summary record is observed, but a source-backed human behavior label is not established (`observed_behavior_supported = false`). No safe before-state, decision actor, target, or policy-input allowlist is supported. Therefore M7.2 action-level `DecisionSampleV1` extraction cannot proceed for this slice, and a first behavior-cloning training smoke is not justified. This does not say Replay is unusable for future research; it says these data do not yet prove an action-level imitation sample.

The report accounts separately for M4's 24,668 width-rejected rows and M5.1's 133 normalized-row rejections. Neither population is silently relabeled or repaired. Potential selection bias from the M4 width exclusions remains unresolved; the accepted subset is not assumed representative.

## Future safety invariants

1. Convert per-side slot position to M5.1 chronology before leakage checks: `ordinal = 2 * (slot_index - 1) + side_offset`, where `side_offset` is zero for the `on_play`-selected first source side and one for the other side. A slot with ordinal greater than the candidate event ordinal is future and must be rejected. Equal ordinal is still unsafe unless pre-action timing is established.
2. No same-slot aggregate or end-of-turn value may enter an intra-turn input without exact pre-action temporal evidence.
3. Outcome/post-decision fields remain isolated from policy inputs.
4. A source-private field can be used only when the decision actor is proven to be that same source side at that sample; source `user` is not unconditionally `SELF`.
5. Unknown visibility, actor, timing, target, or action structure fails closed.
6. A supported decision type requires full-stream evidence, a source-backed action and boundary, actor evidence, before-state evidence, safe exact input selectors, visibility safety, and target evidence if a target is required.
7. Evidence artifacts retain source archive/schema/M3.2/M5.1/M4 bindings and exact `(column_index, exact_header_name)` selectors. Operational paths, raw values, and timestamps do not enter report identity.

## Reproduction and limits

Run `python scripts/m7_1_real_evidence.py --repo-root <checkout> --raw-root <configured-root> --max-data-rows 256`. It verifies the accepted source catalog, M3.1/M3.2 evidence and M5.1 registry pins, registers only the selected Replay archive, reads its bounded prefix via M4-v2, and writes ignored machine artifacts under `data/intermediate/m7.1/`. It does not read Game archives, access network/card databases, emit `DecisionSampleV1`, or retain source row values. The report is limited to this exact archive and prefix plus the existing M5.1 turn-summary full-stream review; it makes no claims about the other 39 Replay schema groups or later/earlier corpus history.
