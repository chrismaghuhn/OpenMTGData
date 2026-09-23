"""Tests for the versioned source catalog and semantic source-set identity."""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from openmtgdata.archive_registration import (
    PROVIDER_NAMESPACE,
    AcquisitionMetadataV1,
    ArchiveRegistrationResult,
    IngestionAuditMetadataV1,
    LicenseReviewStatus,
    MetadataAvailability,
    SourceArchiveMetadataV1,
    SourceArchiveRecordV1,
    register_archive,
    register_inventory_archives,
)
from openmtgdata.cli import main
from openmtgdata.compression_validation import (
    CompressionDiagnosticCode,
    CompressionValidationStatus,
    not_checked_compression_evidence,
    validate_registered_compression,
    validate_registration_compression,
)
from openmtgdata.config import RuntimeConfig
from openmtgdata.inventory import inventory_raw_roots
from openmtgdata.source_manifest import (
    AUDIT_CATALOG_SCHEMA_ID,
    SEMANTIC_SOURCE_CATALOG_DIGEST_CONTRACT_ID,
    SEMANTIC_SOURCE_PROJECTION_SCHEMA_ID,
    SOURCE_MANIFEST_SCHEMA_ID,
    SOURCE_SELECTION_CONTRACT_ID,
    ManifestDiagnosticCode,
    PublicationBlockCode,
    PublicationEligibilityStatus,
    SourceManifestAuditMetadataV1,
    SourceManifestV1,
    SourceMetadataConflictError,
    build_source_manifest,
)

_VECTOR_BYTES = b"OpenMTGData source archive test vector\n"
_VECTOR_SHA256 = "b82e5a848c688c9d37ed304c1c9afbca4f3b2277675c62ea40702ef22c48efda"
_VECTOR_SOURCE_ARCHIVE_ID = "8f2c75c3a5873b785b471bfacffa5d94c524ebb4b3298aff875c6b0effd5b5bb"


def make_config(base: Path, roots: tuple[Path, ...] | None = None) -> RuntimeConfig:
    base.mkdir(parents=True, exist_ok=True)
    raw_roots = roots or (base / "raw",)
    for root in raw_roots:
        root.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig(
        raw_roots=raw_roots,
        intermediate_root=base / "work" / "intermediate",
        quarantine_root=base / "work" / "quarantine",
        release_root=base / "work" / "release",
        base_dir=base,
    )


def write_candidate(root: Path, basename: str, content: bytes, relative_parent: str = "") -> Path:
    path = root / relative_parent / basename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def registration_for(
    base: Path,
    candidates: tuple[tuple[str, bytes, str], ...],
    *,
    roots: tuple[Path, ...] | None = None,
) -> ArchiveRegistrationResult:
    config = make_config(base, roots)
    for basename, content, parent in candidates:
        root_index = 0
        if roots is not None and len(roots) > 1 and parent.startswith("root-b/"):
            root_index = 1
            parent = parent.removeprefix("root-b/")
        write_candidate(config.raw_roots[root_index], basename, content, parent)
    return register_inventory_archives(inventory_raw_roots(config))


def record_for(
    base: Path,
    basename: str,
    content: bytes,
    *,
    relative_parent: str = "",
    source_metadata: SourceArchiveMetadataV1 | None = None,
    acquisition_metadata: AcquisitionMetadataV1 | None = None,
    ingestion_audit_metadata: IngestionAuditMetadataV1 | None = None,
) -> SourceArchiveRecordV1:
    config = make_config(base)
    path = write_candidate(config.raw_roots[0], basename, content, relative_parent)
    item = next(
        candidate
        for candidate in inventory_raw_roots(config).candidates
        if candidate.relative_path.as_posix() == path.relative_to(config.raw_roots[0]).as_posix()
    )
    return register_archive(
        item,
        source_metadata=source_metadata,
        acquisition_metadata=acquisition_metadata,
        ingestion_audit_metadata=ingestion_audit_metadata,
    )


def test_manifest_has_distinct_versioned_projection_contracts(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )

    manifest = build_source_manifest(registration)

    assert manifest.source_manifest_schema_id == SOURCE_MANIFEST_SCHEMA_ID
    assert manifest.audit_catalog_schema_id == AUDIT_CATALOG_SCHEMA_ID
    assert manifest.semantic_projection_schema_id == SEMANTIC_SOURCE_PROJECTION_SCHEMA_ID
    assert (
        manifest.semantic_source_catalog_digest_contract_id
        == SEMANTIC_SOURCE_CATALOG_DIGEST_CONTRACT_ID
    )
    assert manifest.source_selection_contract_id == SOURCE_SELECTION_CONTRACT_ID
    assert manifest.inspection_refs == ()
    assert set(manifest.to_dict()) == {
        "audit_catalog",
        "semantic_source_set",
        "semantic_source_catalog_digest",
        "source_publication_eligibility",
    }


def test_audit_and_semantic_serialization_are_deterministic(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    first = build_source_manifest(registration)
    second = build_source_manifest(registration)

    assert first.audit_catalog_bytes == second.audit_catalog_bytes
    assert first.semantic_projection_bytes == second.semantic_projection_bytes
    assert first.semantic_source_catalog_digest == second.semantic_source_catalog_digest


def test_semantic_digest_matches_independent_golden_projection(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    manifest = build_source_manifest(registration)
    expected_projection = {
        "filename_contract_id": "openmtgdata.17lands-filename.v1",
        "semantic_projection_schema_id": "openmtgdata.semantic-source-set.v1",
        "semantic_source_catalog_digest_contract_id": (
            "openmtgdata.semantic-source-catalog-digest.v1"
        ),
        "source_archive_id_contract_id": "openmtgdata.source-archive-id.v1",
        "source_archive_record_schema_id": "openmtgdata.source-archive-record.v1",
        "source_manifest_schema_id": "openmtgdata.source-manifest.v1",
        "source_selection_contract_id": "openmtgdata.source-selection.v1",
        "archives": [
            {
                "source_archive_id": _VECTOR_SOURCE_ARCHIVE_ID,
                "provider": "17lands",
                "provider_namespace": "17lands.public-datasets",
                "filename_disposition": "recognized",
                "source_kind": "game",
                "expansion_token": "AFR",
                "format_token": "PremierDraft",
                "dataset_family_status": "unknown",
                "dataset_family": None,
                "source_url_status": "unknown",
                "source_url": None,
                "license_status": "unknown",
                "license_identifier": None,
            }
        ],
    }
    independent_bytes = json.dumps(
        expected_projection,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    expected_digest = "cb6e00ecece22945ebd2f312b9996f30b3dacefa46df218c421f6607eb4a74f6"

    assert manifest.semantic_projection_bytes == independent_bytes
    assert hashlib.sha256(independent_bytes).hexdigest() == expected_digest
    assert manifest.semantic_source_catalog_digest == expected_digest


def test_recognized_opaque_source_metadata_is_included_exactly(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("replay_data_public.Cube_-_Powered.PickTwoTradDraft.csv.gz", _VECTOR_BYTES, ""),),
    )

    semantic_archive = build_source_manifest(registration).semantic_projection_dict()["archives"][0]

    assert semantic_archive["source_kind"] == "replay"
    assert semantic_archive["provider_namespace"] == PROVIDER_NAMESPACE
    assert semantic_archive["expansion_token"] == "Cube_-_Powered"
    assert semantic_archive["format_token"] == "PickTwoTradDraft"
    assert semantic_archive["filename_disposition"] == "recognized"


def test_unrecognized_filename_is_audited_and_stays_in_semantic_set(tmp_path: Path) -> None:
    registration = registration_for(tmp_path, (("banana.csv.gz", _VECTOR_BYTES, ""),))
    manifest = build_source_manifest(registration)
    semantic_archive = manifest.semantic_projection_dict()["archives"][0]
    diagnostic_codes = {item.code for item in manifest.audit_diagnostics}

    assert len(manifest.archive_records) == 1
    assert semantic_archive["filename_disposition"] == "unrecognized"
    assert semantic_archive["source_kind"] is None
    assert semantic_archive["expansion_token"] is None
    assert semantic_archive["format_token"] is None
    assert ManifestDiagnosticCode.UNRECOGNIZED_FILENAME in diagnostic_codes
    assert manifest.publication_eligibility.status is PublicationEligibilityStatus.BLOCKED


def test_semantic_set_deduplicates_local_copies_but_audit_retains_both(tmp_path: Path) -> None:
    root_a = tmp_path / "raw-a"
    root_b = tmp_path / "raw-b"
    registration = registration_for(
        tmp_path / "project",
        (
            ("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, "root-a/a"),
            ("copy.csv.gz", _VECTOR_BYTES, "root-b/b"),
        ),
        roots=(root_a, root_b),
    )
    manifest = build_source_manifest(registration)

    audit = manifest.audit_catalog_dict()
    semantic = manifest.semantic_projection_dict()

    assert len(audit["archive_records"]) == 2
    assert audit["audit_archive_registration_count"] == 2
    assert audit["semantic_source_count"] == 1
    assert audit["byte_duplicate_group_count"] == 1
    assert len(semantic["archives"]) == 1
    assert semantic["archives"][0]["source_kind"] == "game"


def test_unknown_duplicate_filename_does_not_perturb_recognized_source_set(tmp_path: Path) -> None:
    config_a = make_config(tmp_path / "single")
    write_candidate(
        config_a.raw_roots[0],
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        "a",
    )
    single = register_inventory_archives(inventory_raw_roots(config_a))

    config_b = make_config(tmp_path / "duplicate")
    write_candidate(
        config_b.raw_roots[0],
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        "a",
    )
    write_candidate(config_b.raw_roots[0], "copy.csv.gz", _VECTOR_BYTES, "b")
    duplicate = register_inventory_archives(inventory_raw_roots(config_b))

    assert build_source_manifest(single).semantic_source_catalog_digest == (
        build_source_manifest(duplicate).semantic_source_catalog_digest
    )


def test_input_registration_order_does_not_change_semantic_or_audit_order(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (
            ("replay_data_public.BLB.TradDraft.csv.gz", b"bytes-b", ""),
            ("game_data_public.AFR.PremierDraft.csv.gz", b"bytes-a", ""),
        ),
    )
    permuted = replace(registration, records=tuple(reversed(registration.records)))
    first = build_source_manifest(registration)
    second = build_source_manifest(permuted)

    assert first.semantic_projection_bytes == second.semantic_projection_bytes
    assert first.audit_catalog_bytes == second.audit_catalog_bytes


def test_raw_root_order_and_local_path_do_not_change_semantic_digest(tmp_path: Path) -> None:
    root_a = tmp_path / "one-volume" / "raw"
    root_b = tmp_path / "another-volume" / "raw"
    registration_a = registration_for(
        tmp_path / "project-a",
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
        roots=(root_a,),
    )
    registration_b = registration_for(
        tmp_path / "project-b",
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
        roots=(root_b,),
    )

    assert build_source_manifest(registration_a).semantic_source_catalog_digest == (
        build_source_manifest(registration_b).semantic_source_catalog_digest
    )


def test_raw_root_argument_order_does_not_change_manifest_identity(tmp_path: Path) -> None:
    root_a = tmp_path / "raw-a"
    root_b = tmp_path / "raw-b"
    base = tmp_path / "project"
    candidates = (
        ("game_data_public.AFR.PremierDraft.csv.gz", b"game-bytes", ""),
        ("replay_data_public.AFR.TradDraft.csv.gz", b"replay-bytes", "root-b"),
    )
    registration_ab = registration_for(base, candidates, roots=(root_a, root_b))
    registration_ba = registration_for(base, candidates, roots=(root_b, root_a))

    manifest_ab = build_source_manifest(registration_ab)
    manifest_ba = build_source_manifest(registration_ba)

    assert manifest_ab.semantic_projection_bytes == manifest_ba.semantic_projection_bytes
    assert manifest_ab.semantic_source_catalog_digest == manifest_ba.semantic_source_catalog_digest


def test_audit_timestamp_tool_and_inspection_refs_do_not_change_semantic_digest(
    tmp_path: Path,
) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    baseline = build_source_manifest(registration)
    audited = build_source_manifest(
        registration,
        inspection_refs=("inspection-artifact-ref",),
        audit_metadata=SourceManifestAuditMetadataV1(
            report_generated_at_utc="2026-09-23T12:00:00Z",
            builder_identity="openmtgdata-test-builder/2",
            tool_identity="test-tool/7",
        ),
    )

    assert baseline.semantic_source_catalog_digest == audited.semantic_source_catalog_digest
    assert baseline.audit_catalog_bytes != audited.audit_catalog_bytes
    assert baseline.inspection_refs == ()
    assert audited.inspection_refs == ("inspection-artifact-ref",)


def test_inspection_refs_are_empty_by_default_and_audit_only(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    empty_refs = build_source_manifest(registration)
    with_refs = build_source_manifest(registration, inspection_refs=("opaque-inspection-ref",))

    assert empty_refs.inspection_refs == ()
    assert empty_refs.semantic_source_catalog_digest == with_refs.semantic_source_catalog_digest
    assert empty_refs.audit_catalog_bytes != with_refs.audit_catalog_bytes


def test_acquisition_and_ingestion_times_do_not_change_semantic_digest(tmp_path: Path) -> None:
    base = tmp_path / "project"
    config = make_config(base)
    item = write_candidate(
        config.raw_roots[0], "game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES
    )
    inventory_item = inventory_raw_roots(config).candidates[0]
    plain_record = register_archive(inventory_item)
    audited_record = register_archive(
        inventory_item,
        acquisition_metadata=AcquisitionMetadataV1(
            status=MetadataAvailability.PROVIDED,
            acquired_from="synthetic source receipt",
            acquired_at_utc="2026-01-01T00:00:00Z",
        ),
        ingestion_audit_metadata=IngestionAuditMetadataV1(
            status=MetadataAvailability.PROVIDED,
            ingested_at_utc="2026-02-02T00:00:00Z",
            registration_tool_identity="synthetic-tool/1",
        ),
    )
    plain_batch = replace(
        register_inventory_archives(inventory_raw_roots(config)),
        records=(plain_record,),
    )
    audited_batch = replace(
        register_inventory_archives(inventory_raw_roots(config)),
        records=(audited_record,),
    )

    assert item.exists()
    assert build_source_manifest(plain_batch).semantic_source_catalog_digest == (
        build_source_manifest(audited_batch).semantic_source_catalog_digest
    )


def test_changing_source_membership_changes_semantic_digest(tmp_path: Path) -> None:
    one = registration_for(
        tmp_path / "one",
        (("game_data_public.AFR.PremierDraft.csv.gz", b"bytes-a", ""),),
    )
    two = registration_for(
        tmp_path / "two",
        (
            ("game_data_public.AFR.PremierDraft.csv.gz", b"bytes-a", ""),
            ("replay_data_public.BLB.TradDraft.csv.gz", b"bytes-b", ""),
        ),
    )
    first_manifest = build_source_manifest(one)
    second_manifest = build_source_manifest(two)
    removed = replace(two, records=two.records[:1])

    assert (
        first_manifest.semantic_source_catalog_digest
        != second_manifest.semantic_source_catalog_digest
    )
    assert build_source_manifest(removed).semantic_source_catalog_digest == (
        first_manifest.semantic_source_catalog_digest
    )


def test_manifest_build_does_not_mutate_frozen_source_archive_record(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    record = registration.records[0]
    before = record.canonical_bytes

    manifest = build_source_manifest(registration)

    assert isinstance(manifest, SourceManifestV1)
    assert record.canonical_bytes == before


def test_dataset_family_is_unknown_not_inferred_from_filename(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    manifest = build_source_manifest(registration)
    archive = manifest.semantic_projection_dict()["archives"][0]

    assert archive["dataset_family_status"] == "unknown"
    assert archive["dataset_family"] is None
    assert any(
        diagnostic.code is ManifestDiagnosticCode.DATASET_FAMILY_UNKNOWN
        for diagnostic in manifest.audit_diagnostics
    )


def test_license_evidence_notes_and_ingestion_tools_are_audit_only(tmp_path: Path) -> None:
    first = record_for(
        tmp_path / "first",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            license_status=LicenseReviewStatus.PENDING_REVIEW,
            license_identifier="synthetic-license-id",
            license_evidence_refs=("first synthetic review note",),
        ),
    )
    second = record_for(
        tmp_path / "second",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            license_status=LicenseReviewStatus.PENDING_REVIEW,
            license_identifier="synthetic-license-id",
            license_evidence_refs=("second synthetic review note",),
        ),
        ingestion_audit_metadata=IngestionAuditMetadataV1(
            status=MetadataAvailability.PROVIDED,
            registration_tool_identity="synthetic-tool/other",
        ),
    )
    first_batch = replace(
        registration_for(tmp_path / "batch-first", (("archive.csv.gz", _VECTOR_BYTES, ""),)),
        records=(first,),
    )
    second_batch = replace(
        registration_for(tmp_path / "batch-second", (("archive.csv.gz", _VECTOR_BYTES, ""),)),
        records=(second,),
    )
    first_manifest = build_source_manifest(first_batch)
    second_manifest = build_source_manifest(second_batch)

    assert first_manifest.semantic_source_catalog_digest == (
        second_manifest.semantic_source_catalog_digest
    )
    assert first_manifest.audit_catalog_bytes != second_manifest.audit_catalog_bytes


def test_generic_source_evidence_refs_are_audit_only_without_a_source_url(
    tmp_path: Path,
) -> None:
    no_reference = record_for(
        tmp_path / "none",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
    )
    reference_only = record_for(
        tmp_path / "reference-only",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            source_url_status=MetadataAvailability.PROVIDED,
            source_evidence_refs=("opaque reference token",),
        ),
    )
    no_reference_batch = replace(
        registration_for(tmp_path / "batch-none", (("archive.csv.gz", _VECTOR_BYTES, ""),)),
        records=(no_reference,),
    )
    reference_batch = replace(
        registration_for(
            tmp_path / "batch-reference",
            (("archive.csv.gz", _VECTOR_BYTES, ""),),
        ),
        records=(reference_only,),
    )

    assert build_source_manifest(no_reference_batch).semantic_source_catalog_digest == (
        build_source_manifest(reference_batch).semantic_source_catalog_digest
    )


def test_source_url_is_semantic_but_generic_evidence_refs_remain_audit_only(
    tmp_path: Path,
) -> None:
    first = record_for(
        tmp_path / "first",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            source_url_status=MetadataAvailability.PROVIDED,
            source_url="https://example.invalid/source.csv.gz",
            source_evidence_refs=("ref-b", "ref-a", "ref-a"),
        ),
    )
    second = record_for(
        tmp_path / "second",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            source_url_status=MetadataAvailability.PROVIDED,
            source_url="https://example.invalid/source.csv.gz",
            source_evidence_refs=("ref-a", "ref-b"),
        ),
    )
    first_batch = replace(
        registration_for(tmp_path / "batch-first", (("archive.csv.gz", _VECTOR_BYTES, ""),)),
        records=(first,),
    )
    second_batch = replace(
        registration_for(tmp_path / "batch-second", (("archive.csv.gz", _VECTOR_BYTES, ""),)),
        records=(second,),
    )

    assert build_source_manifest(first_batch).semantic_source_catalog_digest == (
        build_source_manifest(second_batch).semantic_source_catalog_digest
    )
    assert build_source_manifest(first_batch).audit_catalog_bytes != (
        build_source_manifest(second_batch).audit_catalog_bytes
    )


def test_semantic_metadata_conflict_fails_closed_for_duplicate_byte_identity(
    tmp_path: Path,
) -> None:
    first = record_for(
        tmp_path / "first",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
    )
    second = record_for(
        tmp_path / "second",
        "replay_data_public.BLB.TradDraft.csv.gz",
        _VECTOR_BYTES,
    )
    batch = replace(
        registration_for(tmp_path / "batch", (("placeholder.csv.gz", _VECTOR_BYTES, ""),)),
        records=(first, second),
        total_compressed_bytes=first.compressed_size_bytes + second.compressed_size_bytes,
    )

    with pytest.raises(SourceMetadataConflictError, match="filename classification"):
        build_source_manifest(batch)


def test_semantic_contract_schema_changes_change_catalog_digest(tmp_path: Path) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    manifest = build_source_manifest(registration)

    assert (
        replace(
            manifest, semantic_projection_schema_id="openmtgdata.semantic-source-set.v2"
        ).semantic_source_catalog_digest
        != manifest.semantic_source_catalog_digest
    )
    assert (
        replace(
            manifest, source_selection_contract_id="openmtgdata.source-selection.v2"
        ).semantic_source_catalog_digest
        != manifest.semantic_source_catalog_digest
    )
    assert (
        replace(
            manifest, source_manifest_schema_id="openmtgdata.source-manifest.v2"
        ).semantic_source_catalog_digest
        != manifest.semantic_source_catalog_digest
    )
    assert (
        replace(
            manifest,
            semantic_source_catalog_digest_contract_id="openmtgdata.semantic-source-catalog-digest.v2",
        ).semantic_source_catalog_digest
        != manifest.semantic_source_catalog_digest
    )


def test_audit_schema_and_compression_validation_do_not_change_semantic_digest(
    tmp_path: Path,
) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )
    manifest = build_source_manifest(registration)
    record = registration.records[0]
    valid_evidence = replace(
        not_checked_compression_evidence(record),
        status=CompressionValidationStatus.VALID,
        diagnostic_code=None,
        reason=None,
    )
    altered = replace(
        manifest,
        audit_catalog_schema_id="openmtgdata.source-audit-catalog.v2",
        compression_evidence=(valid_evidence,),
    )

    assert altered.semantic_source_catalog_digest == manifest.semantic_source_catalog_digest
    assert altered.audit_catalog_bytes != manifest.audit_catalog_bytes


def test_source_url_and_license_identity_participate_by_v1_rule(tmp_path: Path) -> None:
    baseline_record = record_for(
        tmp_path / "baseline",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
    )
    url_record = record_for(
        tmp_path / "url",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            source_url_status=MetadataAvailability.PROVIDED,
            source_url="https://example.invalid/source.csv.gz",
            source_evidence_refs=("source-page-reference",),
        ),
    )
    license_record = record_for(
        tmp_path / "license",
        "game_data_public.AFR.PremierDraft.csv.gz",
        _VECTOR_BYTES,
        source_metadata=SourceArchiveMetadataV1(
            license_status=LicenseReviewStatus.PENDING_REVIEW,
            license_identifier="synthetic-license-id",
            license_evidence_refs=("pending-review-reference",),
        ),
    )

    base_registration = replace(
        registration_for(
            tmp_path / "batch-base",
            (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
        ),
        records=(baseline_record,),
    )
    url_registration = replace(
        registration_for(
            tmp_path / "batch-url",
            (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
        ),
        records=(url_record,),
    )
    license_registration = replace(
        registration_for(
            tmp_path / "batch-license",
            (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
        ),
        records=(license_record,),
    )

    baseline_digest = build_source_manifest(base_registration).semantic_source_catalog_digest
    assert build_source_manifest(url_registration).semantic_source_catalog_digest != baseline_digest
    assert (
        build_source_manifest(license_registration).semantic_source_catalog_digest
        != baseline_digest
    )


def test_license_gate_blocks_unknown_pending_incompatible_and_passes_verified(
    tmp_path: Path,
) -> None:
    valid_gzip = gzip.compress(b"synthetic payload")
    results = {}
    for status in LicenseReviewStatus:
        metadata = SourceArchiveMetadataV1(
            source_url_status=MetadataAvailability.PROVIDED,
            source_url="https://example.invalid/source.csv.gz",
            source_evidence_refs=("synthetic-source-reference",),
            license_status=status,
            license_identifier=(
                "synthetic-license" if status is not LicenseReviewStatus.UNKNOWN else None
            ),
            license_evidence_refs=(
                ("synthetic-license-evidence",)
                if status in {LicenseReviewStatus.VERIFIED, LicenseReviewStatus.INCOMPATIBLE}
                else ()
            ),
        )
        record = record_for(
            tmp_path / status.value,
            "game_data_public.AFR.PremierDraft.csv.gz",
            valid_gzip,
            source_metadata=metadata,
        )
        compression = validate_registered_compression(record)
        batch = replace(
            registration_for(
                tmp_path / f"batch-{status.value}",
                (("game_data_public.AFR.PremierDraft.csv.gz", valid_gzip, ""),),
            ),
            records=(record,),
        )
        manifest = build_source_manifest(batch, compression_evidence=(compression,))
        results[status] = manifest.publication_eligibility

    assert results[LicenseReviewStatus.UNKNOWN].status is PublicationEligibilityStatus.BLOCKED
    assert (
        results[LicenseReviewStatus.PENDING_REVIEW].status is PublicationEligibilityStatus.BLOCKED
    )
    assert results[LicenseReviewStatus.INCOMPATIBLE].status is PublicationEligibilityStatus.BLOCKED
    assert results[LicenseReviewStatus.VERIFIED].status is PublicationEligibilityStatus.ELIGIBLE


def test_default_manifest_marks_compression_not_checked_and_blocks_source_eligibility(
    tmp_path: Path,
) -> None:
    registration = registration_for(
        tmp_path,
        (("game_data_public.AFR.PremierDraft.csv.gz", _VECTOR_BYTES, ""),),
    )

    manifest = build_source_manifest(registration)

    assert manifest.compression_evidence[0].status is CompressionValidationStatus.NOT_CHECKED
    assert manifest.publication_eligibility.status is PublicationEligibilityStatus.BLOCKED
    assert any(
        finding.code is PublicationBlockCode.COMPRESSION_NOT_CHECKED
        for finding in manifest.publication_eligibility.findings
    )
    assert any(
        finding.code is PublicationBlockCode.LICENSE_UNKNOWN
        for finding in manifest.publication_eligibility.findings
    )


def test_manifest_cli_reports_invalid_gzip_as_finding_with_success_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = tmp_path / "project"
    config = make_config(base)
    (config.raw_roots[0] / "banana.csv.gz").write_bytes(b"not a gzip stream")
    writable = (
        base / "work" / "intermediate",
        base / "work" / "quarantine",
        base / "work" / "release",
    )
    args = [
        "manifest",
        "--raw-root",
        str(config.raw_roots[0]),
        "--base-dir",
        str(base),
        "--intermediate-root",
        str(writable[0]),
        "--quarantine-root",
        str(writable[1]),
        "--release-root",
        str(writable[2]),
        "--validate-compression",
    ]

    exit_code = main(args)
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert report["audit_catalog"]["compression_status_counts"]["invalid"] == 1
    assert report["audit_catalog"]["compression_evidence_count"] == 1
    assert (
        report["audit_catalog"]["compression_validation_evidence"][0]["diagnostic_code"]
        == CompressionDiagnosticCode.INVALID_GZIP_HEADER.value
    )
    assert report["source_publication_eligibility"]["status"] == "blocked"
    assert report["source_publication_eligibility"]["semantic_source_count"] == 1
    assert all(not path.exists() for path in writable)


def test_batch_compression_validation_reconciles_order_and_status(tmp_path: Path) -> None:
    compressed_a = gzip.compress(b"alpha")
    compressed_b = gzip.compress(b"beta")
    registration = registration_for(
        tmp_path,
        (
            ("game_data_public.AFR.PremierDraft.csv.gz", compressed_a, ""),
            ("replay_data_public.BLB.TradDraft.csv.gz", compressed_b, ""),
        ),
    )

    evidence = validate_registration_compression(registration)

    assert tuple(item.source_archive_id for item in evidence) == tuple(
        record.source_archive_id for record in registration.records
    )
    assert all(item.status is CompressionValidationStatus.VALID for item in evidence)
