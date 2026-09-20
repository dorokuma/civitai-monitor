"""Lossless-first media compression for Telegram size limits.

Telegram rejects photos above 10 MiB and the local Bot API server refuses
uploads above 2 GiB, so an oversized download must be *shrunk* before the
upload instead of being rerouted through the Bot API *document* endpoint, which
no longer exists in this codebase. This module owns that shrink step.

Design contract
---------------
* **Lossless first.** :func:`compress_image` tries, in order, a metadata-only
  rewriting of the JPEG marker stream (DCT coefficients copied verbatim), a
  lossless PNG re-deflate, then a JPEG quality bisection above the configured
  quality floor, and only finally — when the configuration allows it — a small
  downscale. :func:`compress_video` tries a stream-copy remux, then a
  ``libx264`` CRF encode capped by ``-maxrate/-bufsize``, then a
  bitrate-targeted (optionally downscaled) encode.
* **The original download is never overwritten.** Candidates are written to
  ``<dest>.tmp`` and atomically ``os.replace``-d onto ``<stem>.compressed<ext>``
  beside the original. Callers delete that artifact right after the upload and
  let :func:`monitor.cleanup_old_caches` reclaim the original by age/size.
* **Failures are values, not exceptions.** A missing tool, a subprocess
  timeout, an unsupported format or an unreachable target yields
  ``CompressResult(ok=False, ...)`` carrying the reason and every parameter set
  that was tried. Callers decide whether to upload something else or degrade.
* **No mutable module state.** Every knob travels in the frozen
  :class:`CompressSettings` (config-derived) or an explicit argument, which also
  makes the size targets injectable for tests.
* **Serialized transcodes.** ffmpeg/ffprobe run through :mod:`subprocess` with
  a hard ``timeout_seconds`` cap (the child is killed on expiry). The scanner's
  process lock plus its sequential push loop keep a single transcode running at
  a time — this module never fans work out to parallel processes.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger("civitai-monitor")

# ---------------------------------------------------------------------------
# Telegram size budgets
# ---------------------------------------------------------------------------

# Mirrors telegram_media.TELEGRAM_PHOTO_MAX_BYTES / TELEGRAM_VIDEO_MAX_MB.
# Duplicated rather than imported because the dependency is one-way
# (telegram_media imports this module); a regression test pins both pairs
# equal so the numbers cannot drift apart.
DEFAULT_PHOTO_TARGET_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_VIDEO_TARGET_BYTES = 2048 * 1024 * 1024  # 2048 MiB (local Bot API server cap)

_JPEG_QUALITY_MAX = 95
_SCALE_STEP = 0.05
_BITRATE_SAFETY = 0.9
_BUFSIZE_FACTOR = 2
# Ladder 3 retries the rate-targeted encode with tighter budgets: x264
# overshoots a nominal bitrate slightly (min-QP floors), so the first pass
# alone can miss the target by a few percent on short clips.
_LADDER3_FACTORS = (1.0, 0.8, 0.65)
_LOSSLESS_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
# APP1 (EXIF/XMP), APP2 (ICC), APP13 (IPTC/Photoshop), COM (comments).
# APP0 (JFIF) and APP14 (Adobe) are kept: they carry colour interpretation.
_JPEG_METADATA_MARKERS = frozenset({0xE1, 0xE2, 0xED, 0xFE})


# ---------------------------------------------------------------------------
# Settings / result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompressSettings:
    """Compression knobs, all defaulted; mirrors ``config_io.CompressConfig``.

    Frozen on purpose — the module holds no mutable global state, so every call
    receives its configuration explicitly and tests can inject tight targets
    (``photo_target_bytes`` / ``video_target_bytes``) without patching globals.
    """

    enabled: bool = True
    image_quality_min: int = 92
    image_allow_scale: bool = True
    image_min_scale: float = 0.8
    video_crf: int = 18
    video_preset: str = "slow"
    video_audio_bitrate: str = "128k"
    video_allow_scale: bool = True
    video_min_scale: float = 0.8
    timeout_seconds: int = 300
    photo_target_bytes: int = DEFAULT_PHOTO_TARGET_BYTES
    video_target_bytes: int = DEFAULT_VIDEO_TARGET_BYTES

    def __post_init__(self) -> None:
        # Clamp instead of raising: a hand-edited config.yaml must never take
        # the scanner down mid-scan.
        object.__setattr__(
            self, "image_quality_min",
            min(max(int(self.image_quality_min), 1), _JPEG_QUALITY_MAX),
        )
        object.__setattr__(
            self, "image_min_scale", min(max(float(self.image_min_scale), 0.1), 1.0)
        )
        object.__setattr__(
            self, "video_min_scale", min(max(float(self.video_min_scale), 0.1), 1.0)
        )
        object.__setattr__(self, "video_crf", min(max(int(self.video_crf), 0), 51))
        object.__setattr__(self, "timeout_seconds", max(int(self.timeout_seconds), 1))
        object.__setattr__(self, "photo_target_bytes", max(int(self.photo_target_bytes), 1))
        object.__setattr__(self, "video_target_bytes", max(int(self.video_target_bytes), 1))

    @classmethod
    def from_config(cls, config: Any) -> CompressSettings:
        """Build settings from a config model/dict, tolerant of missing keys.

        Unknown or absent fields keep their defaults, which is what makes a
        ``config.yaml`` without a ``compress:`` section (or with only some of
        its keys) load unchanged.
        """
        if config is None:
            return cls()
        values: dict[str, Any] = {}
        for name in _SETTINGS_FIELD_NAMES:
            if isinstance(config, dict):
                if name in config:
                    values[name] = config[name]
            elif hasattr(config, name):
                values[name] = getattr(config, name)
        try:
            return cls(**values)
        except (TypeError, ValueError) as e:
            log.warning("compress: ignoring invalid settings %r (%s); using defaults", values, e)
            return cls()


_SETTINGS_FIELD_NAMES = frozenset(f.name for f in fields(CompressSettings))


@dataclass(frozen=True)
class CompressResult:
    """Outcome of one compression attempt.

    ``ok`` / ``path`` contract for callers:

    * ``ok=True, path is None`` — the input already fits the target; no
      artifact exists, upload the original.
    * ``ok=True, path=<file>`` — upload ``path`` instead of the original and
      delete it afterwards (the original is left untouched on disk).
    * ``ok=False`` — no usable artifact: ``reason`` explains why (missing tool,
      timeout, unsupported format, unreachable target) and ``attempts`` lists
      every parameter set that was tried.
    """

    ok: bool
    path: Path | None
    original_bytes: int
    target_bytes: int
    output_bytes: int = 0
    ladder: str = ""
    reason: str = ""
    attempts: tuple[str, ...] = ()

    def describe(self) -> str:
        """One-line summary for logs (parameters actually used + sizes)."""
        base = (
            f"ok={self.ok} ladder={self.ladder or '-'} "
            f"{self.original_bytes}B -> {self.output_bytes}B (target {self.target_bytes}B)"
        )
        if self.attempts:
            base += f" attempts=[{'; '.join(self.attempts)}]"
        if self.reason:
            base += f" reason={self.reason}"
        return base


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def compressed_path(src: Path | str, suffix: str | None = None) -> Path:
    """``<stem>.compressed<suffix>`` beside ``src`` (the original is untouched)."""
    src = Path(src)
    return src.with_name(f"{src.stem}.compressed{suffix or src.suffix}")


def _tmp_path(dest: Path) -> Path:
    """Temporary sibling of ``dest`` (repo-wide ``.tmp`` convention)."""
    return dest.with_name(dest.name + ".tmp")


def _remove(path: Path | str | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as e:  # pragma: no cover - best-effort cleanup
        log.debug("media_compress: cannot remove %s: %s", path, e)


def _scale_steps(min_scale: float) -> list[float]:
    """Descending downscale candidates ``0.95, 0.90, ... >= min_scale``.

    Downscaling is the last resort, so the caller walks these from the largest
    (least destructive) factor and keeps the first one that fits.
    """
    steps: list[float] = []
    scale = round(1.0 - _SCALE_STEP, 2)
    while scale >= min_scale - 1e-9:
        steps.append(round(scale, 2))
        scale = round(scale - _SCALE_STEP, 2)
    return steps


def _assert_readable_image(path: Path) -> None:
    from PIL import Image

    with Image.open(path) as img:
        img.verify()


# ---------------------------------------------------------------------------
# Image compression
# ---------------------------------------------------------------------------


def compress_image(
    src: Path | str,
    *,
    target_bytes: int = DEFAULT_PHOTO_TARGET_BYTES,
    settings: CompressSettings | None = None,
) -> CompressResult:
    """Shrink an image to ``target_bytes`` or less, lossless first.

    Ladder 1 is lossless (lossless JPEG marker strip / PNG re-deflate), ladder 2
    is a JPEG quality bisection between 95 and ``settings.image_quality_min``,
    ladder 3 adds a downscale from 0.95 down to ``settings.image_min_scale``
    when ``settings.image_allow_scale`` is set. Alpha is flattened onto white
    when a JPEG output is required. Returns a :class:`CompressResult`; never
    raises for a bad input or an unreachable target.
    """
    settings = settings or CompressSettings()
    src = Path(src)
    try:
        original_bytes = src.stat().st_size
    except OSError as e:
        return CompressResult(False, None, 0, target_bytes, reason=f"cannot stat source: {e}")

    if original_bytes <= target_bytes:
        return CompressResult(
            True, None, original_bytes, target_bytes,
            output_bytes=original_bytes, ladder="skip", reason="already within target",
        )

    suffix = src.suffix.lower()
    if suffix not in _LOSSLESS_IMAGE_SUFFIXES:
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=f"unsupported image format {suffix or '<none>'}",
        )

    attempts: list[str] = []
    try:
        ImageOps, UnidentifiedImageError = _pil_ops()
    except ImportError as e:  # pragma: no cover - Pillow is in requirements
        return CompressResult(
            False, None, original_bytes, target_bytes, reason=f"Pillow not available: {e}"
        )
    try:
        if suffix == ".png":
            return _compress_png(src, target_bytes, settings, original_bytes, attempts, ImageOps)
        return _compress_jpeg(src, target_bytes, settings, original_bytes, attempts, ImageOps)
    except (UnidentifiedImageError, OSError, ValueError) as e:
        log.warning("compress_image: %s failed: %s", src, e)
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=f"{type(e).__name__}: {e}", attempts=tuple(attempts),
        )


def _pil_ops():
    """Import the orientation helper lazily; a missing Pillow is a value."""
    from PIL import ImageOps, UnidentifiedImageError

    return ImageOps, UnidentifiedImageError


def _compress_jpeg(src, target_bytes, settings, original_bytes, attempts, ImageOps) -> CompressResult:
    from PIL import Image

    with Image.open(src) as img:
        orientation = _exif_orientation(img)
        if orientation in (None, 1):
            # Ladder 1 (lossless): drop EXIF/XMP/ICC/IPTC/COM segments. The
            # entropy-coded scan data is copied verbatim, so pixels are
            # bit-identical and no decode is needed at all.
            data = _strip_jpeg_markers(src.read_bytes())
            attempts.append(f"lossless-strip:{len(data)}B")
            if 0 < len(data) <= target_bytes:
                return _commit(
                src, data, ".jpg", "lossless-strip", target_bytes, original_bytes, attempts,
            )
            img.load()
            rgb = _flatten_to_rgb(img)
        else:
            # EXIF orientation has to be baked into the pixels, and JPEG cannot
            # be rotated without re-encoding — so the quality ladder carries
            # both the rotation and the size reduction.
            attempts.append(f"bake-orientation:{orientation}")
            img.load()
            rgb = _flatten_to_rgb(ImageOps.exif_transpose(img))

    quality, payload = _best_jpeg_quality(rgb, target_bytes, settings.image_quality_min)
    if payload is not None:
        attempts.append(f"jpeg-q{quality}:{len(payload)}B")
        return _commit(
            src, payload, ".jpg", f"jpeg-q{quality}", target_bytes, original_bytes, attempts,
        )
    attempts.append(f"jpeg-quality-floor-q{settings.image_quality_min}")

    if not settings.image_allow_scale:
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=(
                f"quality floor {settings.image_quality_min} insufficient and "
                "image scaling is disabled"
            ),
            attempts=tuple(attempts),
        )

    for scale in _scale_steps(settings.image_min_scale):
        payload = _encode_jpeg(_resize(rgb, scale), settings.image_quality_min)
        attempts.append(f"scale{scale:.2f}:{len(payload)}B")
        if 0 < len(payload) <= target_bytes:
            ladder = f"jpeg-q{settings.image_quality_min}-scale{scale:.2f}"
            return _commit(
                src, payload, ".jpg", ladder, target_bytes, original_bytes, attempts,
            )

    return CompressResult(
        False, None, original_bytes, target_bytes,
        reason=(
            f"target {target_bytes}B unreachable down to scale "
            f"{settings.image_min_scale:.2f}"
        ),
        attempts=tuple(attempts),
    )


def _compress_png(src, target_bytes, settings, original_bytes, attempts, ImageOps) -> CompressResult:
    from PIL import Image

    with Image.open(src) as img:
        img.load()
        oriented = ImageOps.exif_transpose(img)
        # Ladder 1 (lossless): PNG pixels are re-deflated unchanged; text/iCCP/
        # eXIf chunks are dropped because ``pnginfo`` is never passed through.
        payload = _encode_png(oriented)
        attempts.append(f"png-lossless:{len(payload)}B")
        if 0 < len(payload) <= target_bytes:
            return _commit(
                src, payload, ".png", "png-lossless", target_bytes, original_bytes, attempts,
            )
        rgb = _flatten_to_rgb(oriented)

    # Lossless PNG cannot shrink any further, so fall back to a high-quality
    # JPEG and say so in the log (alpha is flattened onto white).
    log.info(
        "compress_image: %s stays over %dB as lossless PNG (%dB); converting to JPEG",
        src.name, target_bytes, len(payload),
    )
    quality, jpeg_payload = _best_jpeg_quality(rgb, target_bytes, settings.image_quality_min)
    if jpeg_payload is not None:
        attempts.append(f"jpeg-q{quality}:{len(jpeg_payload)}B")
        return _commit(
            src, jpeg_payload, ".jpg", f"png->jpeg-q{quality}", target_bytes,
            original_bytes, attempts,
        )
    attempts.append(f"jpeg-quality-floor-q{settings.image_quality_min}")

    if not settings.image_allow_scale:
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=(
                "PNG cannot reach the target losslessly and JPEG quality floor "
                f"{settings.image_quality_min} is insufficient (scaling disabled)"
            ),
            attempts=tuple(attempts),
        )

    for scale in _scale_steps(settings.image_min_scale):
        payload = _encode_jpeg(_resize(rgb, scale), settings.image_quality_min)
        attempts.append(f"scale{scale:.2f}:{len(payload)}B")
        if 0 < len(payload) <= target_bytes:
            ladder = f"png->jpeg-q{settings.image_quality_min}-scale{scale:.2f}"
            return _commit(
                src, payload, ".jpg", ladder, target_bytes, original_bytes, attempts,
            )

    return CompressResult(
        False, None, original_bytes, target_bytes,
        reason=(
            f"target {target_bytes}B unreachable down to scale "
            f"{settings.image_min_scale:.2f}"
        ),
        attempts=tuple(attempts),
    )


def _exif_orientation(image) -> int | None:
    """EXIF orientation tag (274), or None when absent/unreadable."""
    try:
        return image.getexif().get(274)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        log.debug("compress_image: cannot read EXIF orientation: %s", e)
        return None


def _strip_jpeg_markers(data: bytes) -> bytes:
    """Copy a JPEG byte stream minus its metadata segments (lossless).

    Drops APP1 (EXIF/XMP), APP2 (ICC), APP13 (IPTC/Photoshop) and COM
    (comments); APP0/JFIF, APP14/Adobe and the entropy-coded scan data are
    copied verbatim, so decoded pixels are bit-identical. A stream that does
    not parse cleanly is returned unchanged rather than truncated.
    """
    if not data.startswith(b"\xff\xd8"):
        return data
    out = bytearray(data[:2])
    i = 2
    while True:
        if i + 1 >= len(data):
            return data  # no SOS found: never hand back a truncated stream
        if data[i] != 0xFF:
            return data
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: everything after it is image data
            out += data[i:]
            return bytes(out)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        if i + 4 > len(data):
            return data
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            return data
        if marker not in _JPEG_METADATA_MARKERS:
            out += data[i:i + 2 + length]
        i += 2 + length


def _flatten_to_rgb(image):
    """Flatten alpha onto white so RGBA/LA/P images can be written as JPEG."""
    from PIL import Image

    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _encode_jpeg(image, quality: int) -> bytes:
    buf = io.BytesIO()
    # EXIF/ICC are dropped implicitly: Pillow only writes what it is passed,
    # and only ``quality``/``optimize``/``progressive`` are passed here.
    image.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _encode_png(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True, compress_level=9)
    return buf.getvalue()


def _best_jpeg_quality(image, target_bytes: int, quality_min: int) -> tuple[int | None, bytes | None]:
    """Largest JPEG quality in ``[quality_min, 95]`` whose payload fits."""
    lo, hi = quality_min, _JPEG_QUALITY_MAX
    best_quality: int | None = None
    best_payload: bytes | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        payload = _encode_jpeg(image, mid)
        if 0 < len(payload) <= target_bytes:
            best_quality, best_payload = mid, payload
            lo = mid + 1
        else:
            hi = mid - 1
    return best_quality, best_payload


def _resize(image, scale: float):
    from PIL import Image

    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _commit(
    src, payload, suffix, ladder, target_bytes, original_bytes, attempts,
) -> CompressResult:
    """Write, validate and atomically publish one candidate image payload."""
    dest = compressed_path(src, suffix)
    tmp = _tmp_path(dest)
    try:
        tmp.write_bytes(payload)
        size = tmp.stat().st_size
        if size <= 0 or size > target_bytes:
            raise ValueError(f"candidate is {size}B (target {target_bytes}B)")
        _assert_readable_image(tmp)
        os.replace(tmp, dest)
    except (OSError, ValueError, SyntaxError) as e:  # SyntaxError: corrupt PIL decode
        _remove(tmp)
        log.warning("compress_image: rejected %s candidate for %s: %s", ladder, src, e)
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=f"{type(e).__name__}: {e}", ladder=ladder, attempts=tuple(attempts),
        )
    log.info(
        "compress_image: %s -> %s (ladder=%s, %dB -> %dB, target %dB)",
        src.name, dest.name, ladder, original_bytes, size, target_bytes,
    )
    return CompressResult(
        True, dest, original_bytes, target_bytes,
        output_bytes=size, ladder=ladder, reason="ok", attempts=tuple(attempts),
    )


# ---------------------------------------------------------------------------
# Video compression
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _VideoInfo:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    bit_rate: int | None = None


class _ToolAborted(RuntimeError):
    """Internal: terminal ffmpeg/ffprobe failure (timeout or missing tool)."""


def compress_video(
    src: Path | str,
    *,
    target_bytes: int = DEFAULT_VIDEO_TARGET_BYTES,
    settings: CompressSettings | None = None,
) -> CompressResult:
    """Shrink a video to ``target_bytes`` or less, lossless first.

    Ladder 1 is a stream-copy remux into a faststart MP4 (container work only),
    ladder 2 is ``libx264 -crf`` with ``-maxrate/-bufsize`` sized from the
    duration so the result cannot blow past the budget, ladder 3 is a
    bitrate-targeted encode (optionally downscaled to ``video_min_scale``).
    Every ffmpeg/ffprobe call is capped at ``settings.timeout_seconds``; a
    timeout kills the child and fails the compression. Partial output is always
    cleaned up and the original file is never touched.
    """
    settings = settings or CompressSettings()
    src = Path(src)
    try:
        original_bytes = src.stat().st_size
    except OSError as e:
        return CompressResult(False, None, 0, target_bytes, reason=f"cannot stat source: {e}")

    if original_bytes <= target_bytes:
        return CompressResult(
            True, None, original_bytes, target_bytes,
            output_bytes=original_bytes, ladder="skip", reason="already within target",
        )

    dest = compressed_path(src, ".mp4")
    tmp = _tmp_path(dest)
    attempts: list[str] = []
    try:
        try:
            info = _probe_video(src, settings, attempts)
        except _ToolAborted as e:
            log.warning("compress_video: %s aborted while probing: %s", src.name, e)
            return CompressResult(
                False, None, original_bytes, target_bytes,
                reason=str(e), attempts=tuple(attempts),
            )

        # Ladder 1 (lossless): stream copy + faststart — no re-encode at all.
        if _try_ffmpeg(src, tmp, ["-c", "copy"], settings, attempts, "remux-copy", target_bytes):
            return _commit_video(
                src, tmp, "remux-copy", target_bytes, original_bytes, settings, attempts,
            )

        budget = _bitrate_budget(target_bytes, info.duration)

        # Ladder 2: CRF quality with the size budget as a hard rate ceiling.
        args = _encode_args(settings, quality_crf=settings.video_crf)
        if budget is not None:
            args += ["-maxrate", str(budget), "-bufsize", str(budget * _BUFSIZE_FACTOR)]
        label = f"crf-{settings.video_crf}"
        if _try_ffmpeg(src, tmp, args, settings, attempts, label, target_bytes):
            return _commit_video(
                        src, tmp, label, target_bytes, original_bytes, settings, attempts,
                    )

        # Ladder 3: rate-targeted encoding, then the same with downscale steps.
        if budget is None:
            attempts.append("bitrate-ladder-skipped:unknown-duration")
        else:
            strategies: list[tuple[float, float]] = [
                (1.0, factor) for factor in _LADDER3_FACTORS
            ]
            if settings.video_allow_scale:
                for scale in _scale_steps(settings.video_min_scale):
                    strategies.append((scale, _LADDER3_FACTORS[-1]))
            for scale, factor in strategies:
                args = _encode_args(
                    settings, target_bitrate=max(1, int(budget * factor)),
                )
                label = "bitrate" if factor == 1.0 else f"bitrate-b{int(factor * 100)}"
                if scale < 1.0:
                    size = _scaled_size(info, scale)
                    if size is None:
                        continue
                    args += ["-vf", f"scale={size[0]}:{size[1]}"]
                    label += f"-scale{scale:.2f}"
                if _try_ffmpeg(src, tmp, args, settings, attempts, label, target_bytes):
                    return _commit_video(
                        src, tmp, label, target_bytes, original_bytes, settings, attempts,
                    )
    except _ToolAborted as e:
        log.warning("compress_video: %s aborted: %s", src.name, e)
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=str(e), attempts=tuple(attempts),
        )
    except OSError as e:
        log.warning("compress_video: %s failed: %s", src.name, e)
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=f"{type(e).__name__}: {e}", attempts=tuple(attempts),
        )
    finally:
        _remove(tmp)

    log.warning(
        "compress_video: %s could not be compressed to %dB (attempts: %s)",
        src.name, target_bytes, attempts,
    )
    return CompressResult(
        False, None, original_bytes, target_bytes,
        reason=f"target {target_bytes}B unreachable", attempts=tuple(attempts),
    )


def _run_tool(cmd: list[str], settings: CompressSettings) -> subprocess.CompletedProcess:
    """Run one ffmpeg/ffprobe invocation with a hard timeout.

    ``subprocess.run`` kills and reaps the child when the timeout expires, so a
    stuck encoder cannot outlive the call. Timeout/missing-binary/OS errors are
    raised as :class:`_ToolAborted` and turn into a failed result upstream.
    """
    log.info(
        "media_compress: running %s (timeout=%ds)", " ".join(cmd), settings.timeout_seconds
    )
    try:
        return subprocess.run(cmd, capture_output=True, timeout=settings.timeout_seconds, check=False)
    except subprocess.TimeoutExpired as e:
        raise _ToolAborted(
            f"timeout after {settings.timeout_seconds}s: {' '.join(cmd[:2])}"
        ) from e
    except FileNotFoundError as e:
        raise _ToolAborted(f"tool not found: {cmd[0]}") from e
    except OSError as e:
        raise _ToolAborted(f"cannot run {cmd[0]}: {e}") from e


def _probe_video(src: Path, settings: CompressSettings, attempts: list[str]) -> _VideoInfo:
    """Read duration/resolution/bitrate with ffprobe (best effort).

    A probe *error* (non-zero exit, unparseable JSON) is tolerated — the caller
    then skips the duration-dependent ladders — while a timeout or a missing
    ffprobe is terminal (:class:`_ToolAborted`).
    """
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(src),
    ]
    proc = _run_tool(cmd, settings)
    if proc.returncode != 0:
        attempts.append(f"ffprobe:rc={proc.returncode}")
        return _VideoInfo()
    try:
        payload = json.loads(proc.stdout or b"{}")
    except (TypeError, ValueError):
        attempts.append("ffprobe:unparseable")
        return _VideoInfo()
    if not isinstance(payload, dict):
        attempts.append("ffprobe:not-an-object")
        return _VideoInfo()

    fmt = payload.get("format")
    fmt = fmt if isinstance(fmt, dict) else {}
    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []
    stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None
    )
    stream = stream or {}
    info = _VideoInfo(
        duration=_as_float(fmt.get("duration")),
        width=_as_int(stream.get("width")),
        height=_as_int(stream.get("height")),
        bit_rate=_as_int(fmt.get("bit_rate")),
    )
    attempts.append(f"probe:{info.duration}s/{info.width}x{info.height}@{info.bit_rate}bps")
    return info


def _encode_args(
    settings: CompressSettings,
    *,
    quality_crf: int | None = None,
    target_bitrate: int | None = None,
) -> list[str]:
    """ffmpeg output options for an H.264/AAC MP4."""
    args = ["-c:v", "libx264", "-preset", settings.video_preset, "-pix_fmt", "yuv420p"]
    if quality_crf is not None:
        args += ["-crf", str(quality_crf)]
    if target_bitrate is not None:
        args += [
            "-b:v", str(target_bitrate),
            "-maxrate", str(target_bitrate),
            "-bufsize", str(target_bitrate * _BUFSIZE_FACTOR),
        ]
    args += [
        "-c:a", "aac", "-b:a", settings.video_audio_bitrate,
        "-movflags", "+faststart",
    ]
    return args


def _bitrate_budget(target_bytes: int, duration: float | None) -> int | None:
    """Bits/s that fit ``target_bytes`` in ``duration`` seconds (10% headroom)."""
    if not duration or duration <= 0:
        return None
    return max(1, int(target_bytes * 8 * _BITRATE_SAFETY / duration))


def _try_ffmpeg(
    src: Path,
    tmp: Path,
    args: list[str],
    settings: CompressSettings,
    attempts: list[str],
    label: str,
    target_bytes: int,
) -> bool:
    """Run one ffmpeg ladder step; True when the output fits the target."""
    _remove(tmp)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src), *args, "-f", "mp4", str(tmp),
    ]
    proc = _run_tool(cmd, settings)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[:200].strip()
        attempts.append(f"{label}:ffmpeg rc={proc.returncode} {stderr}")
        _remove(tmp)
        return False
    try:
        size = tmp.stat().st_size
    except OSError:
        size = 0
    attempts.append(f"{label}:{size}B")
    if size <= 0 or size > target_bytes:
        _remove(tmp)
        return False
    return True


def _commit_video(
    src: Path,
    tmp: Path,
    ladder: str,
    target_bytes: int,
    original_bytes: int,
    settings: CompressSettings,
    attempts: list[str],
) -> CompressResult:
    """Validate (non-empty, within budget, ffprobe-parseable) and publish."""
    dest = compressed_path(src, ".mp4")
    try:
        size = tmp.stat().st_size
        if size <= 0 or size > target_bytes:
            raise ValueError(f"candidate is {size}B (target {target_bytes}B)")
        if not _video_readable(tmp, settings):
            raise ValueError("ffprobe cannot parse the candidate")
        os.replace(tmp, dest)
    except (OSError, ValueError) as e:
        _remove(tmp)
        log.warning("compress_video: rejected %s candidate for %s: %s", ladder, src, e)
        return CompressResult(
            False, None, original_bytes, target_bytes,
            reason=f"{type(e).__name__}: {e}", ladder=ladder, attempts=tuple(attempts),
        )
    log.info(
        "compress_video: %s -> %s (ladder=%s, %dB -> %dB, target %dB)",
        src.name, dest.name, ladder, original_bytes, size, target_bytes,
    )
    return CompressResult(
        True, dest, original_bytes, target_bytes,
        output_bytes=size, ladder=ladder, reason="ok", attempts=tuple(attempts),
    )


def _video_readable(path: Path, settings: CompressSettings) -> bool:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ]
    try:
        proc = _run_tool(cmd, settings)
    except _ToolAborted as e:
        log.warning("media_compress: candidate validation aborted: %s", e)
        return False
    return proc.returncode == 0 and bool((proc.stdout or b"").strip())


def _scaled_size(info: _VideoInfo, scale: float) -> tuple[int, int] | None:
    if not info.width or not info.height:
        return None
    return (_even(info.width * scale), _even(info.height * scale))


def _even(value: float) -> int:
    """Nearest even integer >= 2 — H.264 yuv420p requires even dimensions."""
    rounded = int(value)
    return max(2, rounded - rounded % 2)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
