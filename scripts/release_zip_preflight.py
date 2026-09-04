"""Bounded classic-ZIP metadata scanning before ``zipfile`` allocation."""

from __future__ import annotations

from dataclasses import dataclass
import os
import struct
from typing import BinaryIO


ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
ZIP64_EOCD_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP_EOCD = struct.Struct("<4s4H2LH")
ZIP_CENTRAL_DIRECTORY_HEADER = struct.Struct("<4s6H3L5H2L")
ZIP64_UINT16_SENTINEL = 0xFFFF
ZIP64_UINT32_SENTINEL = 0xFFFFFFFF


class ZipPreflightError(RuntimeError):
    """A ZIP cannot be passed safely to the standard-library parser."""


@dataclass(frozen=True)
class ZipCentralDirectoryEntry:
    """Security-relevant metadata from one classic-ZIP central record."""

    name: str
    creator_system: int
    flag_bits: int
    compression_method: int
    compressed_size: int
    file_size: int
    external_attributes: int
    local_header_offset: int
    extra_size: int
    comment_size: int


def _read_exact(source: BinaryIO, size: int, *, label: str) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = source.read(size - len(result))
        if not chunk:
            break
        result.extend(chunk)
    if len(result) != size:
        raise ZipPreflightError(
            f"{label} is truncated: read {len(result)} bytes, expected {size}"
        )
    return bytes(result)


def _decode_member_name(encoded: bytes, flag_bits: int) -> str:
    encoding = "utf-8" if flag_bits & 0x800 else "cp437"
    try:
        return encoded.decode(encoding, errors="strict")
    except UnicodeDecodeError as error:
        raise ZipPreflightError(
            f"ZIP member name is not valid {encoding}"
        ) from error


def scan_classic_zip(
    archive_file: BinaryIO,
    archive_size: int,
    *,
    max_entries: int,
    max_name_bytes: int,
    max_central_directory_bytes: int,
    max_archive_comment_bytes: int,
) -> tuple[ZipCentralDirectoryEntry, ...]:
    """Scan actual central records under fixed bounds without constructing ``ZipInfo``."""

    if (
        archive_size <= 0
        or max_entries <= 0
        or max_name_bytes <= 0
        or max_central_directory_bytes <= 0
        or max_archive_comment_bytes < 0
        or max_archive_comment_bytes > ZIP64_UINT16_SENTINEL
    ):
        raise ValueError("invalid classic-ZIP preflight bounds")

    archive_file.seek(0, os.SEEK_END)
    if archive_file.tell() != archive_size:
        raise ZipPreflightError("ZIP size differs from its stable-file snapshot")
    tail_size = min(
        archive_size,
        ZIP_EOCD.size + max_archive_comment_bytes,
    )
    tail_offset = archive_size - tail_size
    archive_file.seek(tail_offset)
    tail = _read_exact(archive_file, tail_size, label="ZIP end-record window")
    relative_eocd_offset = tail.rfind(ZIP_EOCD_SIGNATURE)
    if (
        relative_eocd_offset < 0
        or relative_eocd_offset + ZIP_EOCD.size > len(tail)
    ):
        raise ZipPreflightError("ZIP has no bounded end record")

    (
        signature,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        entry_count,
        central_directory_size,
        central_directory_offset,
        comment_size,
    ) = ZIP_EOCD.unpack_from(tail, relative_eocd_offset)
    eocd_offset = tail_offset + relative_eocd_offset
    if signature != ZIP_EOCD_SIGNATURE:
        raise ZipPreflightError("ZIP end-record signature is invalid")
    if eocd_offset + ZIP_EOCD.size + comment_size != archive_size:
        raise ZipPreflightError("ZIP has trailing or malformed end-record data")
    if comment_size > max_archive_comment_bytes:
        raise ZipPreflightError("ZIP archive comment exceeds its size limit")
    if disk_number != 0 or central_directory_disk != 0:
        raise ZipPreflightError("multi-disk ZIP archives are not supported")
    if entries_on_disk != entry_count:
        raise ZipPreflightError("ZIP central-directory entry counts disagree")
    if (
        entry_count == ZIP64_UINT16_SENTINEL
        or central_directory_size == ZIP64_UINT32_SENTINEL
        or central_directory_offset == ZIP64_UINT32_SENTINEL
    ):
        raise ZipPreflightError("ZIP64 archives are not supported")
    if entry_count == 0 or entry_count > max_entries:
        raise ZipPreflightError("ZIP member count exceeds its limit")
    if (
        central_directory_size == 0
        or central_directory_size > max_central_directory_bytes
    ):
        raise ZipPreflightError("ZIP central directory exceeds its metadata size limit")
    if central_directory_offset + central_directory_size != eocd_offset:
        raise ZipPreflightError("ZIP central-directory bounds are inconsistent")
    if eocd_offset >= 20:
        archive_file.seek(eocd_offset - 20)
        if _read_exact(
            archive_file,
            4,
            label="ZIP64 locator probe",
        ) == ZIP64_EOCD_LOCATOR_SIGNATURE:
            raise ZipPreflightError("ZIP64 archives are not supported")

    central_directory_end = central_directory_offset + central_directory_size
    archive_file.seek(central_directory_offset)
    entries: list[ZipCentralDirectoryEntry] = []
    for _index in range(entry_count):
        fields = ZIP_CENTRAL_DIRECTORY_HEADER.unpack(
            _read_exact(
                archive_file,
                ZIP_CENTRAL_DIRECTORY_HEADER.size,
                label="ZIP central-directory header",
            )
        )
        if fields[0] != ZIP_CENTRAL_DIRECTORY_SIGNATURE:
            raise ZipPreflightError("ZIP central directory contains an invalid header")

        flag_bits = fields[3]
        creator_system = (fields[1] >> 8) & 0xFF
        compression_method = fields[4]
        compressed_size = fields[8]
        file_size = fields[9]
        name_size = fields[10]
        extra_size = fields[11]
        comment_size = fields[12]
        member_disk = fields[13]
        external_attributes = fields[15]
        local_header_offset = fields[16]
        if (
            compressed_size == ZIP64_UINT32_SENTINEL
            or file_size == ZIP64_UINT32_SENTINEL
            or member_disk == ZIP64_UINT16_SENTINEL
            or local_header_offset == ZIP64_UINT32_SENTINEL
        ):
            raise ZipPreflightError("ZIP64 members are not supported")
        if member_disk != 0:
            raise ZipPreflightError("multi-disk ZIP members are not supported")
        if name_size == 0 or name_size > max_name_bytes:
            raise ZipPreflightError("ZIP member name exceeds its size limit")
        if local_header_offset >= central_directory_offset:
            raise ZipPreflightError("ZIP member has an invalid local-header offset")
        record_end = archive_file.tell() + name_size + extra_size + comment_size
        if record_end > central_directory_end:
            raise ZipPreflightError("ZIP central-directory record exceeds its bounds")

        encoded_name = _read_exact(
            archive_file,
            name_size,
            label="ZIP member name",
        )
        name = _decode_member_name(encoded_name, flag_bits)
        archive_file.seek(extra_size + comment_size, os.SEEK_CUR)
        entries.append(
            ZipCentralDirectoryEntry(
                name=name,
                creator_system=creator_system,
                flag_bits=flag_bits,
                compression_method=compression_method,
                compressed_size=compressed_size,
                file_size=file_size,
                external_attributes=external_attributes,
                local_header_offset=local_header_offset,
                extra_size=extra_size,
                comment_size=comment_size,
            )
        )

    if archive_file.tell() != central_directory_end:
        raise ZipPreflightError(
            "ZIP central directory contains an unexpected number of records"
        )
    return tuple(entries)
