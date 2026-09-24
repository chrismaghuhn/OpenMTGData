"""Strict streaming container selection for registered CSV and TAR.GZ sources."""

from __future__ import annotations

import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol, cast


class SourceContainerError(RuntimeError):
    """The registered gzip payload is neither supported CSV nor a valid CSV TAR."""


SOURCE_CONTAINER_POLICY_ID = "openmtgdata.source-container-policy.csv-gzip-v1"


class ByteReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _PrefixedReader:
    def __init__(self, prefix: bytes, stream: ByteReader) -> None:
        self._prefix = memoryview(prefix)
        self._position = 0
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise SourceContainerError("unbounded source-container reads are forbidden")
        if self._position < len(self._prefix):
            count = min(size, len(self._prefix) - self._position)
            result = self._prefix[self._position : self._position + count].tobytes()
            self._position += count
            if count == size:
                return result
            return result + self._stream.read(size - count)
        return self._stream.read(size)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        return None


@dataclass(slots=True)
class CsvSourcePayloadV1:
    stream: ByteReader
    container_kind: str
    member_name: str | None
    _decompressed: ByteReader
    _archive: tarfile.TarFile | None = None
    _member: ByteReader | None = None
    _finished: bool = False

    def finish(self) -> None:
        """Validate exact TAR membership/trailer and consume gzip to integrity EOF."""
        if self._finished:
            return
        if self._archive is not None:
            if self._member is None or self._member.read(1) != b"":
                raise SourceContainerError("CSV TAR member was not exhausted")
            try:
                extra = self._archive.next()
                if extra is not None:
                    raise SourceContainerError("CSV TAR contains an unexpected additional member")
                tar_stream = self._archive.fileobj
                if tar_stream is None:
                    raise SourceContainerError("CSV TAR stream closed before trailer validation")
                while chunk := tar_stream.read(64 * 1024):
                    if any(chunk):
                        raise SourceContainerError("CSV TAR has non-zero trailing bytes")
            except tarfile.TarError as exc:
                raise SourceContainerError(
                    "CSV TAR trailer or member structure is invalid"
                ) from exc
        while self._decompressed.read(64 * 1024):
            pass
        self._finished = True

    def close(self) -> None:
        if self._member is not None:
            self._member.close()
        if self._archive is not None:
            self._archive.close()


def expected_tar_csv_member(original_filename: str, *, source_kind: str) -> str:
    if not original_filename.endswith(".csv.gz"):
        raise SourceContainerError("registered source filename is not a CSV gzip name")
    csv_name = original_filename[:-3]
    if source_kind == "replay" and csv_name.startswith("replay_data_public."):
        return (
            "replay-data."
            + csv_name.removeprefix("replay_data_public.").removesuffix(".csv")
            + ".csv"
        )
    return csv_name


def open_csv_source_payload(
    decompressed: ByteReader,
    *,
    original_filename: str,
    source_kind: str,
) -> CsvSourcePayloadV1:
    """Peek a fixed prefix, then expose plain CSV or one exact named TAR member."""
    prefix = decompressed.read(512)
    if len(prefix) < 512:
        return CsvSourcePayloadV1(
            _PrefixedReader(prefix, decompressed), "gzip_csv", None, decompressed
        )
    if prefix[257:263] not in (b"ustar\x00", b"ustar "):
        return CsvSourcePayloadV1(
            _PrefixedReader(prefix, decompressed), "gzip_csv", None, decompressed
        )
    expected_name = expected_tar_csv_member(original_filename, source_kind=source_kind)
    try:
        archive = tarfile.open(
            fileobj=cast(BinaryIO, _PrefixedReader(prefix, decompressed)), mode="r|*"
        )
        member = archive.next()
    except (tarfile.TarError, OSError) as exc:
        raise SourceContainerError("USTAR signature is present but TAR header is invalid") from exc
    if (
        member is None
        or not member.isfile()
        or member.name != expected_name
        or PurePosixPath(member.name).name != member.name
    ):
        archive.close()
        raise SourceContainerError("TAR does not contain the one expected regular CSV member")
    extracted = archive.extractfile(member)
    if extracted is None:
        archive.close()
        raise SourceContainerError("could not open selected CSV TAR member")
    return CsvSourcePayloadV1(
        extracted, "gzip_tar_csv", member.name, decompressed, archive, extracted
    )
