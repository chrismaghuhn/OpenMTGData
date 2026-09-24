# ADR 0004: Select CSV payloads from registered gzip containers

Status: accepted as a source-layer correction, 2026-09-24

## Context

The configured 200-archive corpus contains two observed outer-container forms under `.csv.gz` names: 193 direct gzip-compressed CSV files and 7 gzip-compressed USTAR files. The seven TAR files each contain one regular CSV member: Game AFR PremierDraft/TradDraft and STX PremierDraft/TradDraft; Replay AFR PremierDraft/TradDraft and STX TradDraft. In the earlier scanner, TAR header bytes were consumed as the first CSV header field; this yielded incorrect physical fingerprints for those seven sources and contaminated bounded row evidence with container metadata.

## Policy

`openmtgdata.source-container-policy.csv-gzip-v1` peeks a fixed 512 decompressed bytes. If the USTAR magic at offset 257 is absent, the bytes are replayed into the existing strict CSV scanner as direct gzip CSV. If the USTAR magic is present, the stream is parsed forward-only as TAR and exactly one expected regular CSV member is selected. Game member names match the registered CSV basename. Replay member names use the observed exact `replay-data.<expansion>.<format>.csv` export name corresponding to `replay_data_public.<expansion>.<format>.csv.gz`. Wrong names, non-regular entries, extra members, malformed TAR headers, and non-zero trailing TAR data fail closed. No member is extracted to disk.

M2.5 fingerprints only the actual CSV header fields. Container kind and selected member are recorded as inspection provenance and excluded from `raw_schema_fingerprint`. M3.1 uses the same member selection and retains its existing bounded row-prefix scope. M4 validates the selected member through EOF, validates the TAR trailer and sole-member rule, then reaches gzip EOF and verifies the registered outer compressed SHA-256/size. The M4 reader method is versioned as `openmtgdata.raw-source-reader.v2`; M2.5 and M3.1 method identities are also versioned to v2. Existing M4.2 checkpoints bind the reader method identity and therefore cannot be resumed under the corrected method.

## Observed correction

For the reviewed AFR PremierDraft Game archive, the actual CSV header has 1,082 fields and begins with `user_win_rate_bucket`. Its corrected physical fingerprint is `a2bddf4d55c72c4c329eda7af4740d0fce03a66a626f1c44148282ae80b5b840`. The compressed archive identity is unchanged because it continues to identify the exact outer registered bytes.

This correction reduced the configured corpus from 77 to 75 physical schema groups: 35 Game and 40 Replay. All 200 source registrations remain present. M3.1 and M3.2 evidence must be rebuilt from the selected CSV payloads before M5 mappings or M6 joins use those identities.
