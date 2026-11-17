"""Media validators reject unsafe containers before any provider sees them."""

from __future__ import annotations

import io
import wave
import zlib
from pathlib import Path

import pytest

from apex.errors import DomainError
from apex.interaction.media import MediaLimits, inspect_upload, media_path

LIMITS = MediaLimits(max_bytes=2_000_000, max_pixels=1_000_000, max_edge=4096, max_audio_seconds=2)


def test_png_jpeg_and_webp_are_accepted(tmp_path: Path) -> None:
    png = inspect_upload(_png(2, 2), LIMITS)
    jpeg = inspect_upload(_jpeg(1, 1), LIMITS)
    webp = inspect_upload(_webp(3, 4), LIMITS)
    assert (png.media_type, png.width, png.height) == ("image/png", 2, 2)
    assert jpeg.media_type == "image/jpeg"
    assert (webp.width, webp.height) == (3, 4)
    assert b"EXIF" not in jpeg.data
    assert b"comment" not in png.data
    path = media_path(tmp_path, "ab" * 16)
    assert path.parent == tmp_path.resolve()


def test_png_inflate_is_bounded_by_declared_dimensions() -> None:
    header = (1).to_bytes(4, "big") * 2 + bytes((8, 2, 0, 0, 0))
    huge = zlib.compress(b"\x00" * 2_000_000)
    payload = _png_with(header, huge)
    with pytest.raises(DomainError) as bomb:
        inspect_upload(payload, LIMITS)
    assert bomb.value.code == "invalid_media"


def test_rejects_unsafe_or_unknown_media(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as bomb:
        inspect_upload(_png(5000, 5000, pixels=False), LIMITS)
    assert bomb.value.code == "media_too_large"
    corrupt = bytearray(_png(1, 1))
    corrupt[20] ^= 0xFF
    with pytest.raises(DomainError) as bad:
        inspect_upload(bytes(corrupt), LIMITS)
    assert bad.value.code == "invalid_media"
    with pytest.raises(DomainError) as gif:
        inspect_upload(b"GIF89a", LIMITS)
    assert gif.value.code == "unsupported_media"
    with pytest.raises(DomainError) as m4a:
        inspect_upload(b"\x00\x00\x00\x1cftypM4A ", LIMITS)
    assert m4a.value.code == "unsupported_media"
    with pytest.raises(DomainError):
        media_path(tmp_path, "../secret")
    with pytest.raises(DomainError) as duration:
        inspect_upload(_wav(seconds=3), LIMITS)
    assert duration.value.code == "media_duration_exceeded"
    audio = inspect_upload(_mp3(times=2), LIMITS)
    assert audio.media_type == "audio/mpeg"


def _png_with(header: bytes, image: bytes) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", image)
        + _chunk(b"IEND", b"")
    )


def _chunk(tag: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
    return len(payload).to_bytes(4, "big") + tag + payload + crc.to_bytes(4, "big")


def _png(width: int, height: int, *, pixels: bool = True) -> bytes:
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    if pixels:
        raw = b"".join(b"\x00\x00\x00\x00" for _ in range(width * height))
        image = zlib.compress(raw)
    else:
        image = zlib.compress(b"\x00")
    text = _chunk(b"tEXt", b"comment\x00secret")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + text
        + _chunk(b"IDAT", image)
        + _chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int) -> bytes:
    sof = bytes((8,)) + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    sof += bytes((1, 1, 0x11, 0))
    exif = b"EXIF\x00\x00secret"
    return (
        b"\xff\xd8"
        + b"\xff\xe1"
        + (len(exif) + 2).to_bytes(2, "big")
        + exif
        + b"\xff\xc0"
        + (len(sof) + 2).to_bytes(2, "big")
        + sof
        + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9"
    )


def _webp(width: int, height: int) -> bytes:
    payload = bytes(4) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + len(payload).to_bytes(4, "little") + payload
    body = b"WEBP" + chunk
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _wav(seconds: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * (8000 * seconds))
    return buffer.getvalue()


def _mp3(times: int) -> bytes:
    frame = b"\xff\xfb\x90\x00" + (b"\x00" * 413)
    return frame * times
