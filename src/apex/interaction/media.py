"""Validate uploaded media without trusting names, paths, or declared types.

PNG pixel data is inflated only up to a bound derived from the declared dimensions.
JPEG and WebP are checked as containers and dimension headers. They are not rasterized.
"""

from __future__ import annotations

import io
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path

from apex.errors import DomainError

_PNG = b"\x89PNG\r\n\x1a\n"
_DROP_PNG = {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}
_BITRATE_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)


@dataclass(frozen=True)
class MediaLimits:
    max_bytes: int
    max_pixels: int
    max_edge: int
    max_audio_seconds: float


@dataclass(frozen=True)
class InspectedMedia:
    kind: str
    media_type: str
    data: bytes
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    sample_rate: int | None = None
    channels: int | None = None


def inspect_upload(data: bytes, limits: MediaLimits) -> InspectedMedia:
    """Detect a supported image or audio payload and return sanitized bytes."""
    if not data:
        raise DomainError("invalid_media", "The upload is empty.")
    if len(data) > limits.max_bytes:
        raise DomainError("media_too_large", "The upload exceeds the size limit.")
    if data.startswith(_PNG):
        return _png(data, limits)
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data, limits)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return _webp(data, limits)
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return _wav(data, limits)
    if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return _mp3(data, limits)
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        raise DomainError("unsupported_media", "GIF images are not accepted.")
    if b"ftyp" in data[:16]:
        raise DomainError("unsupported_media", "M4A audio is not accepted.")
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        raise DomainError("unsupported_media", "WebM audio is not accepted.")
    raise DomainError("unsupported_media", "The file type is not accepted.")


def media_path(root: Path, storage_key: str) -> Path:
    """Resolve a generated key. User paths are not accepted."""
    if len(storage_key) != 32 or any(char not in "0123456789abcdef" for char in storage_key):
        raise DomainError("not_found", "The media was not found.")
    base = root.resolve()
    path = (base / storage_key).resolve()
    if path.parent != base:
        raise DomainError("not_found", "The media was not found.")
    return path


def _reject_dimensions(width: int, height: int, limits: MediaLimits) -> None:
    if width < 1 or height < 1:
        raise DomainError("invalid_media", "The image dimensions are not valid.")
    if width > limits.max_edge or height > limits.max_edge:
        raise DomainError("media_too_large", "The image exceeds the dimension limit.")
    if width * height > limits.max_pixels:
        raise DomainError("media_too_large", "The image exceeds the pixel limit.")


def _png(data: bytes, limits: MediaLimits) -> InspectedMedia:
    chunks = _png_chunks(data)
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) < 13:
        raise DomainError("invalid_media", "The PNG header is missing.")
    header = chunks[0][1]
    width = int.from_bytes(header[0:4], "big")
    height = int.from_bytes(header[4:8], "big")
    _reject_dimensions(width, height, limits)
    compressed = b"".join(payload for tag, payload in chunks if tag == b"IDAT")
    if not compressed:
        raise DomainError("invalid_media", "The PNG has no image data.")
    limit = height * (1 + width * 8) + 64
    decoder = zlib.decompressobj()
    try:
        decoder.decompress(compressed, limit)
    except zlib.error as exc:
        raise DomainError("invalid_media", "The PNG could not be decoded.") from exc
    if decoder.unconsumed_tail or not decoder.eof:
        raise DomainError("invalid_media", "The PNG could not be decoded.")
    kept = [item for item in chunks if item[0] not in _DROP_PNG]
    return InspectedMedia(
        kind="image",
        media_type="image/png",
        data=_png_bytes(kept),
        width=width,
        height=height,
    )


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if not data.startswith(_PNG):
        raise DomainError("invalid_media", "The file is not a PNG.")
    pos = 8
    found: list[tuple[bytes, bytes]] = []
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        if length < 0 or pos + 12 + length > len(data):
            raise DomainError("invalid_media", "The PNG is truncated.")
        tag = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        crc = int.from_bytes(data[pos + 8 + length : pos + 12 + length], "big")
        if (zlib.crc32(tag + payload) & 0xFFFFFFFF) != crc:
            raise DomainError("invalid_media", "The PNG checksum does not match.")
        found.append((tag, payload))
        pos += 12 + length
        if tag == b"IEND":
            if pos != len(data):
                raise DomainError("invalid_media", "The PNG has trailing data.")
            return found
    raise DomainError("invalid_media", "The PNG is truncated.")


def _png_bytes(chunks: list[tuple[bytes, bytes]]) -> bytes:
    body = bytearray(_PNG)
    for tag, payload in chunks:
        body += len(payload).to_bytes(4, "big")
        body += tag
        body += payload
        body += (zlib.crc32(tag + payload) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(body)


def _jpeg(data: bytes, limits: MediaLimits) -> InspectedMedia:
    if not data.startswith(b"\xff\xd8"):
        raise DomainError("invalid_media", "The file is not a JPEG.")
    width: int | None = None
    height: int | None = None
    out = bytearray(b"\xff\xd8")
    pos = 2
    saw_eoi = False
    while pos + 1 < len(data):
        if data[pos] != 0xFF:
            raise DomainError("invalid_media", "The JPEG is truncated.")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            out += b"\xff\xd9"
            saw_eoi = True
            break
        if marker == 0xDA:
            end = data.find(b"\xff\xd9", pos)
            if end < 0:
                raise DomainError("invalid_media", "The JPEG is truncated.")
            out += b"\xff\xda" + data[pos:end] + b"\xff\xd9"
            saw_eoi = True
            break
        if 0xD0 <= marker <= 0xD8:
            out += bytes((0xFF, marker))
            continue
        if pos + 2 > len(data):
            raise DomainError("invalid_media", "The JPEG is truncated.")
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > len(data):
            raise DomainError("invalid_media", "The JPEG is truncated.")
        segment = data[pos : pos + length]
        if marker in {0xC0, 0xC1, 0xC2} and len(segment) >= 7:
            height = int.from_bytes(segment[3:5], "big")
            width = int.from_bytes(segment[5:7], "big")
        if marker not in {0xE1, 0xE2}:
            out += bytes((0xFF, marker)) + segment
        pos += length
    if not saw_eoi or width is None or height is None:
        raise DomainError("invalid_media", "The JPEG is not a complete image.")
    _reject_dimensions(width, height, limits)
    return InspectedMedia(
        kind="image",
        media_type="image/jpeg",
        data=bytes(out),
        width=width,
        height=height,
    )


def _webp(data: bytes, limits: MediaLimits) -> InspectedMedia:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise DomainError("invalid_media", "The file is not a WebP.")
    if int.from_bytes(data[4:8], "little") != len(data) - 8:
        raise DomainError("invalid_media", "The WebP size does not match.")
    pos = 12
    chunks: list[tuple[bytes, bytes]] = []
    while pos < len(data):
        if pos + 8 > len(data):
            raise DomainError("invalid_media", "The WebP is truncated.")
        tag = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        end = pos + 8 + size
        if size < 0 or end > len(data):
            raise DomainError("invalid_media", "The WebP is truncated.")
        chunks.append((tag, data[pos + 8 : end]))
        pos = end + (size & 1)
        if pos > len(data):
            raise DomainError("invalid_media", "The WebP is truncated.")
    if not chunks:
        raise DomainError("invalid_media", "The WebP has no image data.")
    width, height = _webp_size(chunks[0][0], chunks[0][1])
    _reject_dimensions(width, height, limits)
    kept = [(tag, payload) for tag, payload in chunks if tag != b"EXIF"]
    body = bytearray(b"WEBP")
    for tag, payload in kept:
        body += tag
        body += len(payload).to_bytes(4, "little")
        body += payload
        if len(payload) & 1:
            body += b"\x00"
    return InspectedMedia(
        kind="image",
        media_type="image/webp",
        data=b"RIFF" + len(body).to_bytes(4, "little") + bytes(body),
        width=width,
        height=height,
    )


def _webp_size(tag: bytes, payload: bytes) -> tuple[int, int]:
    if tag == b"VP8X" and len(payload) >= 10:
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return width, height
    if tag == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return width, height
    if tag == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise DomainError("invalid_media", "The WebP dimensions could not be read.")


def _wav(data: bytes, limits: MediaLimits) -> InspectedMedia:
    try:
        with wave.open(io.BytesIO(data)) as audio:
            rate = audio.getframerate()
            channels = audio.getnchannels()
            frames = audio.getnframes()
    except wave.Error as exc:
        raise DomainError("invalid_media", "The WAV file could not be read.") from exc
    if rate < 1 or frames < 1 or channels < 1:
        raise DomainError("invalid_media", "The WAV file has no audio.")
    if channels > 2 or rate > 48_000:
        raise DomainError("unsupported_media", "The WAV format exceeds the supported range.")
    duration = frames / rate
    if duration > limits.max_audio_seconds:
        raise DomainError("media_duration_exceeded", "The audio exceeds the duration limit.")
    return InspectedMedia(
        kind="audio",
        media_type="audio/wav",
        data=data,
        duration_ms=int(duration * 1000),
        sample_rate=rate,
        channels=channels,
    )


def _mp3(data: bytes, limits: MediaLimits) -> InspectedMedia:
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 10:
            raise DomainError("invalid_media", "The MP3 file is truncated.")
        size = _synchsafe(data[6:10])
        offset = 10 + size
    while offset + 4 <= len(data):
        if data[offset] == 0xFF and data[offset + 1] & 0xE0 == 0xE0:
            break
        offset += 1
    else:
        raise DomainError("invalid_media", "The MP3 file has no audio frame.")
    header = int.from_bytes(data[offset : offset + 4], "big")
    version = (header >> 19) & 0x3
    layer = (header >> 17) & 0x3
    bitrate_index = (header >> 12) & 0xF
    sample_index = (header >> 10) & 0x3
    if version != 3 or layer != 1 or sample_index == 3 or bitrate_index in {0, 15}:
        raise DomainError("unsupported_media", "Only MPEG-1 Layer III audio is accepted.")
    bitrate = _BITRATE_V1_L3[bitrate_index] * 1000
    audio_bytes = len(data) - offset
    duration = audio_bytes * 8 / bitrate
    if duration > limits.max_audio_seconds:
        raise DomainError("media_duration_exceeded", "The audio exceeds the duration limit.")
    return InspectedMedia(
        kind="audio",
        media_type="audio/mpeg",
        data=data,
        duration_ms=int(duration * 1000),
        sample_rate=(44100, 48000, 32000)[sample_index],
        channels=1,
    )


def _synchsafe(raw: bytes) -> int:
    value = 0
    for byte in raw:
        if byte & 0x80:
            raise DomainError("invalid_media", "The MP3 tag size is not valid.")
        value = (value << 7) | byte
    return value


def silence_wav() -> bytes:
    """A short WAV the mock speech adapter can return and the API can validate."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()
