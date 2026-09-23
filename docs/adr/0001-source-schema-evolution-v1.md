# ADR 0001: Source schema evolution policy v1

**Status:** Accepted for the M3.2 registry implementation.

**Scope:** Structural source-schema compatibility only. This ADR does not define normalized fields or semantic adapters.

## Context and evidence

The M3.2 registry consumes the digest-verified M3.1 deep-inspection report. The accepted input binds semantic source catalog `dcfbae5b65529ea42d637d2337418401f129ab1169db3c9bf0a7d84809573602` and M3.1 evidence `2572d8825d5eff17ad779695102f97d1ebb04b607545b02d1001d48461744add`.

The configured report contains 77 physical groups: 36 Game and 41 Replay. M3.1 inspected at most 256 data records per archive. Its lexical classes and row-width observations are bounded evidence; they do not establish full-history behavior, source types, null semantics, or semantic equivalence.

The group comparisons show no equal ordered-header surfaces across distinct raw fingerprints. One directional strict-subset candidate exists between the Game SIR group covering Sealed/TradSealed (1,604 fields) and the group covering PremierDraft/TradDraft (1,804 fields): the larger surface has 200 additional exact header names, while 81 shared fields have different observed lexical-class support. The policy therefore records an additive candidate for review but does not merge the groups. Reordered, removal, rename/alias candidate, lexical, empty-field, and row-width findings remain separately visible. The resulting 77 contracts are a conservative empirical result, not a target count.

The pairwise matrix contains 2,900 directed same-kind assessments (both A→B and B→A). Registry finding counts count findings per directed assessment, not unique undirected schema pairs. The real report contains one additive direction, its reverse removal direction, four rename/alias candidate directions (two candidate pairs), and five pair assessments needing review. Group-level dispositions remain separate: all 77 raw groups have a supported exact structural contract, while no pairwise semantic equivalence is claimed.

## Identity boundary

`raw_schema_fingerprint` remains the M2.5 physical-evidence identity. It is never rewritten by this policy. `source_interpretation_contract_id` identifies a separately versioned structural interpretation boundary. Its canonical projection binds source kind, reference and member fingerprints, required fields and ordering, additional-field handling, duplicate/empty-name handling, row-width policy, lexical-evidence constraints, review findings, and M3.1 evidence digests. The ID is SHA-256 over canonical compact UTF-8 JSON, prefixed with `openmtgdata.source-interpretation.v1:`.

Correcting a policy decision creates a different interpretation ID and schema-registry digest while preserving each raw fingerprint.

## Compatibility rules

Comparisons are directional and only occur between groups of the same source kind. Game and Replay groups never share a contract. Every pair assessment records exact-name overlap, left/right-only fields, shared-field relative order, duplicate ambiguity, lexical evidence drift, empty-field evidence drift, and row-width anomalies. Pairwise assessments are evidence, not connected-component membership.

An additive extension MAY join the reference contract only when all of these conditions hold:

1. Source kind is the same.
2. Every required base field appears exactly once with exact spelling.
3. The relative order of required fields is preserved. New fields may be inserted between them; they remain uninterpreted.
4. Required fields have the same observed lexical-class support and empty-field observation across the compared groups.
5. Neither member has a known sampled row-width anomaly.
6. No duplicate-name or empty-required-name ambiguity exists.
7. The added raw fingerprint is explicitly listed in the registry.

The policy is directional: base → extension does not imply extension → base. A family member is checked directly against the deterministic reference surface; pairwise connected components do not establish transitive compatibility. The real SIR candidate fails the lexical-evidence condition and remains in separate contracts.

When an explicitly approved family has multiple possible references, M3.2 chooses the minimum field count, then the lexicographically minimum raw fingerprint. Every other member is checked directly against that reference; there is no transitive inference.

Additional fields are preserved uninterpreted and may not be silently dropped. A fingerprint not listed in the registry fails closed until reviewed. Field names are exact; duplicate names are distinguished by column index and prohibit name-only mapping. Empty header names remain `""` at their exact index; no name is synthesized.

Reorder is incompatible by default. Removal of a required field is incompatible; no default is synthesized and missing is not equated with empty. Similar names or lexical profiles may be reported as `renamed_or_alias_candidate`, but aliases are never applied automatically.

Lexical evidence is limited to M3.1's `empty`, `integer_lexeme`, `decimal_lexeme`, `boolean_lexeme`, and `other_text` observations. A lexical profile difference is not a database-type claim. Empty-field evidence is not nullability proof. Semantic equivalence remains `not_established_beyond_structural_and_bounded_lexical_evidence`.

## Row-width policy

M4 must require each row to match the exact width of its registered member header. Short or long rows are unsupported records with deterministic row ordinals. Readers must not pad, truncate, or synthesize fields.

The two observed AFR Replay archives remain registered under their own structural contracts. `replay_data_public.AFR.PremierDraft.csv.gz` had 26 longer rows among 256 sampled records (expected 2,022 fields; maximum observed 2,036). `replay_data_public.AFR.TradDraft.csv.gz` had one shorter and 15 longer rows (expected 2,022; observed range 2,015–2,036). Exact-width records can be considered under the group contract; anomalous records are unsupported. The groups are not rejected wholesale and no extra positions receive invented names.

## Contract status and M4 handoff

All 77 current groups receive an explicit registry disposition and an exact raw-header structural contract. `supported_interpretation` means the raw structure can be consumed under this policy; it does **not** mean semantic field mappings are known. `semantic_mapping_status` is `source_schema_compatibility_only`. No `ReplayEventV1`, `GameRecordV1`, normalization, joins, or decision fields are defined here.

The registry binds the source-catalog digest, M3.1 evidence digest, M3.1 method, M2.5 header-inventory evidence identity, and policy contract. URL and license status remain unknown and are not resolved by local schema analysis. Chronological coverage remains unavailable. `reviewed` means the explicit M3.2 structural policy was applied; it is not human approval of a normalized schema or a release license.

M4 can use the registry to determine whether a fingerprint is structurally supported, which interpretation contract applies, the exact required header surface and order, how additional fields are preserved, which parser contract is required, and that rows with non-exact widths must be rejected. Checkpoint identity must include the registry/interpretation contract as well as source identity and the M4 stage configuration.
