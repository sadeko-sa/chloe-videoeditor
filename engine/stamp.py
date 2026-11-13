"""
stamp.py - Writes the name of this program into a finished export, as the "Writing application" that MediaInfo shows.

FFmpeg puts its own name there ("Lavf60.16.100") and has no option to change it: the Matroska muxer writes it as a
fixed string and the MP4 muxer overwrites the "encoder" tag. So after the export the file is patched here, in pure
Python (no extra tool): MKV is changed in its header only, MP4 in its moov box. Pure data, no FFmpeg and no Tk.
"""

from __future__ import annotations

import os
import shutil
import struct
from typing import BinaryIO, Iterator, List, Optional, Tuple

from . import APP_NAME, VERSION

WRITING_APP = "%s %s" % (APP_NAME, VERSION)
"""Text of the writing application, like HandBrake's "HandBrake 1.7.3 2024021000"."""

_EBML_INFO = b"\x15\x49\xa9\x66"
_EBML_SEGMENT = b"\x18\x53\x80\x67"
_EBML_SEEKHEAD = b"\x11\x4d\x9b\x74"
_EBML_WRITING_APP = b"\x57\x41"
_EBML_VOID = b"\xec"
_MKV_HEAD = 1 << 16
"""Bytes of an MKV file that are read: FFmpeg writes the segment information in the first few hundred."""

_MP4_ENCODER = b"\xa9too"
_MP4_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl")
_COPY_BLOCK = 1 << 23


def stamp(path: str, text: str = WRITING_APP) -> bool:
    """
    Set the writing application of a file FFmpeg wrote. The file is left as it was if it cannot be patched.

    @param path  .mkv, .webm or .mp4 / .m4v / .mov file; any other extension is ignored
    @param text  the new writing application
    @return      True if the file was changed
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".mkv", ".webm", ".mka"):
            return _stamp_mkv(path, text)
        if ext in (".mp4", ".m4v", ".mov", ".m4a"):
            return _stamp_mp4(path, text)
    except (OSError, ValueError, struct.error):
        return False
    return False


# --------------------------------------------------------------------------- #
# Matroska
# --------------------------------------------------------------------------- #


def _vint(data: bytes, pos: int) -> Tuple[int, int]:
    """@return (value without the length marker, length in bytes) of the EBML number at pos"""
    first = data[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    if length > 8:
        raise ValueError("bad EBML number")
    value = first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | data[pos + i]
    return value, length


def _ebml_id(data: bytes, pos: int) -> bytes:
    """@return the element id at pos (with its length marker)"""
    length = 1
    mask = 0x80
    while length <= 4 and not data[pos] & mask:
        mask >>= 1
        length += 1
    if length > 4:
        raise ValueError("bad EBML id")
    return data[pos:pos + length]


def _elements(data: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int, int]]:
    """@return (id, element start, payload start, element end) of every element between start and end"""
    pos = start
    while pos < end:
        ident = _ebml_id(data, pos)
        size, length = _vint(data, pos + len(ident))
        payload = pos + len(ident) + length
        yield ident, pos, payload, payload + size
        pos = payload + size


def _size8(size: int) -> bytes:
    """@return size as an EBML number of 8 bytes (always the same width, so it can be rewritten in place)"""
    return b"\x01" + size.to_bytes(7, "big")


def _void(total: int) -> bytes:
    """@return a Void element of exactly `total` bytes (at least 2)"""
    if total - 2 < 127:
        return _EBML_VOID + bytes([0x80 | (total - 2)]) + bytes(total - 2)
    return _EBML_VOID + _size8(total - 9) + bytes(total - 9)


def _stamp_mkv(path: str, text: str) -> bool:
    """
    Replace the WritingApp of the segment information. FFmpeg writes a Void element right before the segment
    information, the new (longer) information takes its place and the Void shrinks by the same amount, so nothing
    after it moves. Only the position of the information in the SeekHead changes.

    @return True if the file was changed; False if its layout is not the one FFmpeg writes
    """
    with open(path, "r+b") as f:
        head = f.read(_MKV_HEAD)
        ident = _ebml_id(head, 0)
        size, length = _vint(head, len(ident))
        pos = len(ident) + length + size
        if _ebml_id(head, pos) != _EBML_SEGMENT:
            return False
        seg_id = _ebml_id(head, pos)
        _, length = _vint(head, pos + len(seg_id))
        base = pos + len(seg_id) + length
        children = []
        for ident, start, payload, end in _elements(head, base, len(head)):
            if end > len(head):
                break
            children.append((ident, start, payload, end))
            if ident == _EBML_INFO:
                break
        if len(children) < 2 or children[-1][0] != _EBML_INFO or children[-2][0] != _EBML_VOID:
            return False
        void_start = children[-2][1]
        _, info_start, info_payload, info_end = children[-1]
        payload = bytearray()
        found = False
        for ident, start, _, end in _elements(head, info_payload, info_end):
            if ident == _EBML_WRITING_APP:
                value = text.encode("utf-8")
                payload += _EBML_WRITING_APP + _size8(len(value)) + value
                found = True
            else:
                payload += head[start:end]
        if not found:
            value = text.encode("utf-8")
            payload += _EBML_WRITING_APP + _size8(len(value)) + value
        info = _EBML_INFO + _size8(len(payload)) + bytes(payload)
        spare = info_end - void_start - len(info)
        if spare < 0 or spare == 1:
            return False
        seek = _seek_position_of(head, children, _EBML_INFO, base)
        if seek is None:
            return False
        offset, width = seek
        if void_start - base >= 1 << (8 * width):
            return False
        patch = info + (_void(spare) if spare else b"")
        f.seek(offset)
        f.write((void_start - base).to_bytes(width, "big"))
        f.seek(void_start)
        f.write(patch)
    return True


def _seek_position_of(head: bytes, children: List[Tuple[bytes, int, int, int]], target: bytes,
                      base: int) -> Optional[Tuple[int, int]]:
    """@return (file offset, width in bytes) of the SeekPosition entry for the element `target`, None if none"""
    for ident, _, payload, end in children:
        if ident != _EBML_SEEKHEAD:
            continue
        for _, _, seek_payload, seek_end in _elements(head, payload, end):
            wanted = False
            for sub, _, value, sub_end in _elements(head, seek_payload, seek_end):
                if sub == b"\x53\xab":
                    wanted = head[value:sub_end] == target
                elif sub == b"\x53\xac" and wanted:
                    return value, sub_end - value
    return None


# --------------------------------------------------------------------------- #
# MP4
# --------------------------------------------------------------------------- #


def _boxes(buf: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """@return (type, box start, box end) of every box between start and end"""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", buf, pos)
        if size < 8:
            raise ValueError("box size not supported")
        yield kind, pos, pos + size
        pos += size


def _top_boxes(f: BinaryIO, total: int) -> Iterator[Tuple[bytes, int, int]]:
    """@return (type, box start, box end) of the top level boxes of an open file (64 bit sizes included)"""
    pos = 0
    while pos + 8 <= total:
        f.seek(pos)
        size, kind = struct.unpack(">I4s", f.read(8))
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
        elif size == 0:
            size = total - pos
        if size < 8:
            raise ValueError("box size not supported")
        yield kind, pos, pos + size
        pos += size


def _child(buf: bytes, box: Tuple[int, int], kind: bytes, skip: int = 8) -> Optional[Tuple[int, int]]:
    """@return (start, end) of the first box of that type inside `box`, None if there is none; skip = header bytes"""
    for found, start, end in _boxes(buf, box[0] + skip, box[1]):
        if found == kind:
            return start, end
    return None


def _offset_tables(buf: bytes, start: int, end: int, out: List[Tuple[int, bool]]) -> None:
    """Collect (position of the first entry count, is 64 bit) of every stco / co64 box inside start..end"""
    for kind, box_start, box_end in _boxes(buf, start, end):
        if kind in _MP4_CONTAINERS:
            _offset_tables(buf, box_start + 8, box_end, out)
        elif kind in (b"stco", b"co64"):
            out.append((box_start + 12, kind == b"co64"))


def _patched_moov(moov: bytes, text: str, shift_offsets: bool) -> bytes:
    """
    @param moov           the moov box
    @param text           the new writing application
    @param shift_offsets  True if the media data follows the moov box: all chunk offsets move by the size change
    @return               the moov box with the new text in its udta / meta / ilst / encoder tag
    @raise ValueError     if there is no encoder tag to replace
    """
    buf = bytearray(moov)
    path = [(0, len(buf))]
    for kind, parent_skip in ((b"udta", 8), (b"meta", 8), (b"ilst", 12), (_MP4_ENCODER, 8), (b"data", 8)):
        step = _child(bytes(buf), path[-1], kind, parent_skip)
        if step is None:
            raise ValueError("no encoder tag")
        path.append(step)
    data_start, data_end = path[-1]
    value = text.encode("utf-8")
    box = struct.pack(">I4s", 16 + len(value), b"data") + bytes(buf[data_start + 8:data_start + 16]) + value
    delta = len(box) - (data_end - data_start)
    if shift_offsets:
        tables: List[Tuple[int, bool]] = []
        _offset_tables(bytes(buf), 8, len(buf), tables)
        for pos, wide in tables:
            count = struct.unpack_from(">I", buf, pos)[0]
            fmt = ">Q" if wide else ">I"
            width = 8 if wide else 4
            for i in range(count):
                at = pos + 4 + i * width
                struct.pack_into(fmt, buf, at, struct.unpack_from(fmt, buf, at)[0] + delta)
    buf[data_start:data_end] = box
    for start, _ in path[:-1]:
        struct.pack_into(">I", buf, start, struct.unpack_from(">I", buf, start)[0] + delta)
    return bytes(buf)


def _stamp_mp4(path: str, text: str) -> bool:
    """
    Replace the encoder tag in the moov box. If the moov box is the last one (no fast start) the file is changed
    in place; if it comes first, the file is copied once with the new box (all chunk offsets move with it).

    @return True if the file was changed
    """
    total = os.path.getsize(path)
    with open(path, "rb") as f:
        boxes = list(_top_boxes(f, total))
        moov = next(((s, e) for k, s, e in boxes if k == b"moov"), None)
        mdat = next(((s, e) for k, s, e in boxes if k == b"mdat"), None)
        if moov is None or mdat is None:
            return False
        f.seek(moov[0])
        old = f.read(moov[1] - moov[0])
        first = moov[0] < mdat[0]
        new = _patched_moov(old, text, first)
        if not first:
            if moov[1] != total:
                return False
            with open(path, "r+b") as out:
                out.seek(moov[0])
                out.write(new)
                out.truncate()
            return True
        temp = path + ".stamp"
        try:
            with open(temp, "wb") as out:
                f.seek(0)
                remaining = moov[0]
                while remaining:
                    block = f.read(min(_COPY_BLOCK, remaining))
                    out.write(block)
                    remaining -= len(block)
                out.write(new)
                f.seek(moov[1])
                shutil.copyfileobj(f, out, _COPY_BLOCK)
        except BaseException:
            _remove(temp)
            raise
    os.replace(temp, path)
    return True


def _remove(path: str) -> None:
    """Delete a file, ignoring that it may not exist."""
    try:
        os.remove(path)
    except OSError:
        pass
