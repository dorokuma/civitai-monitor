"""Unit tests for media_compress: lossless-first ladders, failure contract.

c1  image ladder: lossless metadata strip is pixel-identical and drops EXIF/ICC,
    the quality bisection picks the largest quality >= the floor, the scale
    ladder only runs when allowed, and alpha modes flatten onto white for JPEG.
c2  image failure contract: an unreachable target returns ok=False with the
    attempts recorded, never an exception; unsupported formats are refused.
c3  the original download is never overwritten, artifacts/tmp files are cleaned,
    and a file already within the target is skipped.
c4  video ladder: stream-copy remux first, then CRF + maxrate/bufsize, then a
    bitrate-targeted (optionally downscaled) encode — with ffmpeg/ffprobe
    mocked so the argv and the timeout are asserted directly.
c5  video failure contract: timeout/missing-tool abort and clean up, an
    unknown duration skips the bitrate ladder, and settings are clamped.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, PngImagePlugin

import media_compress as mc
import telegram_media
from media_compress import CompressSettings, compress_image, compress_video

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _random_rgb(size=(600, 600)):
    """Incompressible pixels: JPEG quality changes move the byte size a lot."""
    return Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))


def _jpeg_bytes(image, quality):
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return len(buf.getvalue())


def _make_jpeg(path, size=(600, 600), orientation=None, icc=False):
    img = _random_rgb(size)
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    exif[271] = "civitai-monitor-test"
    kwargs = {"exif": exif.tobytes()}
    if icc:
        kwargs["icc_profile"] = b"ICCP" * 512
    img.save(path, format="JPEG", quality=95, **kwargs)
    return path


def _no_tmp_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob("*.tmp"))


# ---------------------------------------------------------------------------
# c1 / c2 / c3 — image compression
# ---------------------------------------------------------------------------


class TestImageLosslessStrip:
    def test_strip_is_pixel_identical_and_removes_metadata(self, tmp_path):
        src = _make_jpeg(tmp_path / "photo.jpg", size=(200, 200), icc=True)
        original = src.read_bytes()
        assert b"Exif\x00\x00" in original and b"ICC_PROFILE" in original

        stripped = mc._strip_jpeg_markers(original)

        assert b"Exif\x00\x00" not in stripped
        assert b"ICC_PROFILE" not in stripped
        assert len(stripped) < len(original)
        with Image.open(io.BytesIO(stripped)) as a, Image.open(src) as b:
            a.load()
            b.load()
            assert a.tobytes() == b.tobytes()  # lossless: identical pixels

    def test_malformed_stream_is_returned_unchanged(self):
        assert mc._strip_jpeg_markers(b"not-a-jpeg") == b"not-a-jpeg"
        assert mc._strip_jpeg_markers(b"\xff\xd8\xff\xe1\x00") == b"\xff\xd8\xff\xe1\x00"

    def test_lossless_ladder_used_when_metadata_strip_suffices(self, tmp_path):
        src = _make_jpeg(tmp_path / "meta.jpg", size=(200, 200), icc=True)
        original_bytes = src.stat().st_size
        # A target that only the metadata strip can reach.
        target = original_bytes - 1
        # Sanity: the metadata must be big enough for this to be the strip path.
        assert len(mc._strip_jpeg_markers(src.read_bytes())) <= target

        result = compress_image(src, target_bytes=target)

        assert result.ok is True
        assert result.ladder == "lossless-strip"
        assert result.output_bytes <= target
        assert src.read_bytes()[:2] == b"\xff\xd8"  # original untouched
        assert src.stat().st_size == original_bytes

    def test_baked_orientation_uses_the_quality_ladder(self, tmp_path):
        src = _make_jpeg(tmp_path / "rotated.jpg", size=(300, 200), orientation=6)
        result = compress_image(src, target_bytes=src.stat().st_size - 1)
        # Orientation 6 must be baked in, which needs a decode/re-encode.
        assert result.ok is True
        assert "bake-orientation:6" in result.attempts
        assert result.ladder.startswith("jpeg-q")


class TestImageQualityLadder:
    def test_bisection_picks_largest_quality_within_target(self, tmp_path):
        src = _make_jpeg(tmp_path / "q.jpg")
        rgb = mc._flatten_to_rgb(Image.open(src).convert("RGB"))
        size95, size92 = _jpeg_bytes(rgb, 95), _jpeg_bytes(rgb, 92)
        assert size92 < size95
        target = (size92 + size95) // 2

        result = compress_image(
            src, target_bytes=target, settings=CompressSettings(image_allow_scale=False)
        )

        assert result.ok is True
        assert result.ladder.startswith("jpeg-q")
        quality = int(result.ladder.split("jpeg-q")[1].split("-")[0])
        assert 92 <= quality <= 94
        assert result.output_bytes <= target
        with Image.open(result.path) as out:
            out.load()
            assert out.size == (600, 600)  # no scaling took place
        assert _no_tmp_files(tmp_path) == []

    def test_quality_floor_unreachable_without_scaling_fails_cleanly(self, tmp_path):
        src = _make_jpeg(tmp_path / "q2.jpg")
        result = compress_image(
            src, target_bytes=2_000, settings=CompressSettings(image_allow_scale=False)
        )
        assert result.ok is False
        assert result.path is None
        assert "quality floor 92" in result.reason
        assert "jpeg-quality-floor-q92" in result.attempts
        assert _no_tmp_files(tmp_path) == []
        assert not mc.compressed_path(src, ".jpg").exists()

    def test_scaling_ladder_picks_the_largest_scale_that_fits(self, tmp_path):
        src = _make_jpeg(tmp_path / "s.jpg")
        rgb = mc._flatten_to_rgb(Image.open(src).convert("RGB"))
        size90 = _jpeg_bytes(mc._resize(rgb, 0.90), 92)
        size85 = _jpeg_bytes(mc._resize(rgb, 0.85), 92)
        assert size85 < size90
        target = (size85 + size90) // 2

        result = compress_image(src, target_bytes=target, settings=CompressSettings())

        assert result.ok is True
        assert "scale" in result.ladder
        scale = float(result.ladder.rsplit("scale", 1)[1])
        assert 0.85 <= scale <= 0.90  # least destructive factor wins
        with Image.open(result.path) as out:
            out.load()
            assert out.size[0] == round(600 * scale)

    def test_scaling_exhausted_reports_every_attempt(self, tmp_path):
        src = _make_jpeg(tmp_path / "s2.jpg")
        result = compress_image(src, target_bytes=500, settings=CompressSettings())
        assert result.ok is False
        assert result.path is None
        assert "unreachable" in result.reason
        assert [a.split(":")[0] for a in result.attempts if a.startswith("scale")] == [
            "scale0.95", "scale0.90", "scale0.85", "scale0.80",
        ]
        assert _no_tmp_files(tmp_path) == []


class TestImageAlphaAndFormats:
    @pytest.mark.parametrize("mode", ["RGBA", "LA", "P", "L"])
    def test_alpha_and_palette_images_convert_to_jpeg(self, tmp_path, mode):
        src = tmp_path / f"alpha_{mode}.png"
        if mode == "P":
            img = Image.frombytes("P", (800, 800), os.urandom(800 * 800))
            img.putpalette([i % 256 for i in range(768)])
            img.info["transparency"] = 0
        elif mode == "L":
            img = Image.frombytes("L", (800, 800), os.urandom(800 * 800))
        else:
            img = Image.frombytes(mode, (800, 800), os.urandom(800 * 800 * 4))
        img.save(src, format="PNG")

        result = compress_image(
            src, target_bytes=500_000, settings=CompressSettings()
        )

        assert result.ok is True
        assert "png->jpeg" in result.ladder
        with Image.open(result.path) as out:
            out.load()
            assert out.mode == "RGB"
        assert src.suffix == ".png"  # original file untouched

    def test_png_lossless_is_preferred_over_jpeg(self, tmp_path):
        src = tmp_path / "text.png"
        img = _random_rgb((300, 300))
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "A" * 900_000)
        img.save(src, format="PNG", pnginfo=info)
        assert src.stat().st_size > 1_000_000

        result = compress_image(src, target_bytes=400_000, settings=CompressSettings())

        assert result.ok is True
        assert result.ladder == "png-lossless"
        assert result.path.suffix == ".png"
        with Image.open(result.path) as out:
            assert out.format == "PNG"
        assert b"Comment" not in result.path.read_bytes()  # metadata dropped
        assert _no_tmp_files(tmp_path) == []

    def test_unsupported_format_is_refused(self, tmp_path):
        src = tmp_path / "anim.gif"
        src.write_bytes(b"GIF89a" + b"0" * 4096)
        result = compress_image(src, target_bytes=16)
        assert result.ok is False
        assert result.path is None
        assert "unsupported image format" in result.reason

    def test_already_within_target_is_a_noop(self, tmp_path):
        src = _make_jpeg(tmp_path / "small.jpg", size=(64, 64))
        result = compress_image(src, target_bytes=src.stat().st_size + 1)
        assert result.ok is True
        assert result.path is None
        assert result.ladder == "skip"
        assert not mc.compressed_path(src, ".jpg").exists()

    def test_unreadable_source_is_a_value_not_an_exception(self, tmp_path):
        result = compress_image(tmp_path / "ghost.jpg", target_bytes=10)
        assert result.ok is False
        assert "cannot stat source" in result.reason

    def test_original_is_never_overwritten(self, tmp_path):
        src = _make_jpeg(tmp_path / "keep.jpg")
        before = (src.stat().st_size, src.read_bytes())
        result = compress_image(src, target_bytes=300_000, settings=CompressSettings())
        assert result.ok is True
        assert result.path != src
        assert (src.stat().st_size, src.read_bytes()) == before


# ---------------------------------------------------------------------------
# c4 / c5 — video compression (ffmpeg/ffprobe mocked)
# ---------------------------------------------------------------------------


def _completed(cmd, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _probe_payload(duration=12.0, width=640, height=480, bit_rate=2_000_000):
    return json.dumps({
        "format": {"duration": str(duration), "bit_rate": str(bit_rate)},
        "streams": [{"codec_type": "video", "width": width, "height": height}],
    }).encode()


class _FakeTools:
    """Stands in for subprocess.run, writing fake ffmpeg output of given sizes.

    ``sizes`` maps a marker found in the argv (e.g. "copy", "-crf", "-b:v") to
    the byte size of the file ffmpeg pretends to write.
    """

    def __init__(self, sizes, probe=None, tmp_dir=None):
        self.sizes = sizes
        self.probe = probe
        self.tmp_dir = tmp_dir
        self.calls = []
        self.call_timeouts = set()
        self.timeout_markers = set()
        self.missing = set()
        self.probe_rc = 0
        self.validate_stdout = b"12.0\n"

    def __call__(self, cmd, capture_output=True, timeout=None, check=False):
        self.calls.append(list(cmd))
        assert capture_output is True and check is False
        self.call_timeouts.add(timeout)
        if cmd[0] == "ffprobe":
            if "ffprobe" in self.missing:
                raise FileNotFoundError("ffprobe")
            if "-show_entries" in cmd:  # artifact validation
                return _completed(cmd, stdout=self.validate_stdout)
            if self.probe_rc != 0:
                return _completed(cmd, returncode=self.probe_rc, stderr=b"probe failed")
            return _completed(cmd, stdout=self.probe if self.probe is not None else _probe_payload())
        if "ffmpeg" in self.missing:
            raise FileNotFoundError("ffmpeg")
        out = Path(cmd[-1])
        if "-c" in cmd and "copy" in cmd:
            marker = "copy"
        elif "-b:v" in cmd:
            marker = "-b:v"
        else:
            marker = "-crf"
        if marker in self.timeout_markers:
            raise subprocess.TimeoutExpired(cmd, timeout)
        out.write_bytes(b"V" * self.sizes[marker])
        return _completed(cmd)

    def ffmpeg_calls(self):
        return [c for c in self.calls if c[0] == "ffmpeg"]


def _make_fake_video(path, size=200_000):
    path.write_bytes(b"V" * size)
    return path


class TestVideoLadder:
    def test_remux_copy_is_tried_first_and_wins(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        settings = CompressSettings()
        target = 50_000
        tools = _FakeTools({"copy": target - 1})
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=target, settings=settings)

        assert result.ok is True
        assert result.ladder == "remux-copy"
        assert result.output_bytes == target - 1
        assert src.stat().st_size == 200_000  # original untouched
        remux = tools.ffmpeg_calls()[0]
        assert remux[remux.index("-i") + 1] == str(src)
        assert remux[8:] == [
            "-c", "copy", "-f", "mp4", str(mc._tmp_path(mc.compressed_path(src, ".mp4"))),
        ]
        assert tools.call_timeouts == {settings.timeout_seconds}
        assert _no_tmp_files(tmp_path) == []
        assert mc.compressed_path(src, ".mp4").exists()

    def test_crf_ladder_args_and_budget(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        settings = CompressSettings(video_crf=18, video_preset="slow", video_audio_bitrate="128k")
        target = 60_000
        duration = 12.0
        budget = mc._bitrate_budget(target, duration)
        tools = _FakeTools({"copy": 999_999, "-crf": target - 5})
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=target, settings=settings)

        assert result.ok is True
        assert result.ladder == "crf-18"
        argv = tools.ffmpeg_calls()[1]
        for flag, value in [
            ("-c:v", "libx264"), ("-crf", "18"), ("-preset", "slow"),
            ("-maxrate", str(budget)), ("-bufsize", str(budget * 2)),
            ("-c:a", "aac"), ("-b:a", "128k"), ("-movflags", "+faststart"),
            ("-pix_fmt", "yuv420p"),
        ]:
            assert flag in argv and argv[argv.index(flag) + 1] == value, flag
        assert argv[argv.index("-f") + 1] == "mp4"
        assert tools.call_timeouts == {settings.timeout_seconds}

    def test_bitrate_downgrade_after_crf_miss(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        settings = CompressSettings()
        target = 60_000
        budget = mc._bitrate_budget(target, 12.0)
        tools = _FakeTools({"copy": 999_999, "-crf": 500_000, "-b:v": target - 100})
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=target, settings=settings)

        assert result.ok is True
        assert result.ladder == "bitrate"
        argv = tools.ffmpeg_calls()[-1]
        assert argv[argv.index("-b:v") + 1] == str(budget)
        assert "-crf" not in argv

    def test_bitrate_retries_with_tightened_budgets(self, tmp_path):
        """x264 overshoots a nominal bitrate; ladder 3 retries with less room."""
        src = _make_fake_video(tmp_path / "clip.mp4")
        target = 60_000
        budget = mc._bitrate_budget(target, 12.0)
        tools = _FakeTools(
            {"copy": 999_999, "-crf": 500_000},
            probe=_probe_payload(duration=12.0, width=640, height=480),
        )
        encoded = {"n": 0}

        def run(cmd, capture_output=True, timeout=None, check=False):
            if cmd[0] == "ffmpeg" and "-b:v" in cmd:
                encoded["n"] += 1
                size = 999_999 if encoded["n"] == 1 else target - 1
                Path(cmd[-1]).write_bytes(b"V" * size)
                tools.calls.append(list(cmd))
                return _completed(cmd)
            return _FakeTools.__call__(tools, cmd, capture_output, timeout, check)

        with patch.object(mc.subprocess, "run", run):
            result = compress_video(
                src, target_bytes=target,
                settings=CompressSettings(video_allow_scale=False),
            )

        assert result.ok is True
        assert result.ladder == "bitrate-b80"
        argv = tools.ffmpeg_calls()[-1]
        assert argv[argv.index("-b:v") + 1] == str(int(budget * 0.8))

    def test_scale_step_used_only_when_allowed(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        target = 60_000
        # copy + crf + every full-size bitrate pass miss; the 0.95 step fits.
        sizes = {"copy": 999_999, "-crf": 500_000}
        tools = _FakeTools(sizes, probe=_probe_payload(duration=12.0, width=640, height=480))
        budget = mc._bitrate_budget(target, 12.0)

        def run(cmd, capture_output=True, timeout=None, check=False):
            if cmd[0] == "ffmpeg" and "-b:v" in cmd:
                size = target - 1 if "-vf" in cmd else 999_999
                Path(cmd[-1]).write_bytes(b"V" * size)
                tools.calls.append(list(cmd))
                return _completed(cmd)
            return _FakeTools.__call__(tools, cmd, capture_output, timeout, check)

        with patch.object(mc.subprocess, "run", run):
            result = compress_video(
                src, target_bytes=target,
                settings=CompressSettings(video_allow_scale=True, video_min_scale=0.8),
            )

        assert result.ok is True
        assert result.ladder == "bitrate-b65-scale0.95"
        argv = tools.ffmpeg_calls()[-1]
        assert argv[argv.index("-vf") + 1] == "scale=608:456"
        assert argv[argv.index("-b:v") + 1] == str(int(budget * 0.65))

    def test_scale_disabled_stops_after_full_size_bitrate(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        tools = _FakeTools({"copy": 999_999, "-crf": 500_000, "-b:v": 999_999})
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(
                src, target_bytes=60_000,
                settings=CompressSettings(video_allow_scale=False),
            )
        assert result.ok is False
        assert all("-vf" not in c for c in tools.ffmpeg_calls())
        assert "target 60000B unreachable" in result.reason

    def test_within_target_is_a_noop(self, tmp_path):
        src = _make_fake_video(tmp_path / "small.mp4", size=1_000)
        with patch.object(
            mc.subprocess, "run", side_effect=AssertionError("must not run ffmpeg")
        ):
            result = compress_video(src, target_bytes=10_000)
        assert result.ok is True
        assert result.path is None
        assert result.ladder == "skip"

    def test_timeout_aborts_immediately_and_cleans_up(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        settings = CompressSettings(timeout_seconds=7)
        tools = _FakeTools({"copy": 1}, tmp_dir=tmp_path)
        tools.timeout_markers.add("copy")
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=50_000, settings=settings)

        assert result.ok is False
        assert result.path is None
        assert "timeout after 7s" in result.reason
        assert tools.call_timeouts == {7}  # every call used the configured timeout
        assert len(tools.ffmpeg_calls()) == 1  # terminal: no further ladders
        assert _no_tmp_files(tmp_path) == []
        assert not mc.compressed_path(src, ".mp4").exists()

    def test_missing_tool_is_reported(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        tools = _FakeTools({"copy": 1})
        tools.missing.add("ffprobe")
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=50_000)
        assert result.ok is False
        assert "tool not found: ffprobe" in result.reason

    def test_unknown_duration_skips_bitrate_ladder(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        tools = _FakeTools({"copy": 999_999, "-crf": 999_999}, probe=b"not-json")
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=60_000)
        assert result.ok is False
        assert "bitrate-ladder-skipped:unknown-duration" in result.attempts
        assert all("-b:v" not in c for c in tools.ffmpeg_calls())

    def test_ffprobe_rc_error_keeps_crf_ladder_without_maxrate(self, tmp_path):
        src = _make_fake_video(tmp_path / "clip.mp4")
        tools = _FakeTools({"copy": 999_999, "-crf": 30_000})
        tools.probe_rc = 1
        with patch.object(mc.subprocess, "run", tools):
            result = compress_video(src, target_bytes=50_000)
        assert result.ok is True
        assert "ffprobe:rc=1" in result.attempts
        crf_call = next(c for c in tools.ffmpeg_calls() if "-crf" in c)
        assert "-maxrate" not in crf_call


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class TestCompressSettings:
    def test_defaults_match_telegram_media_limits(self):
        settings = CompressSettings()
        assert settings.photo_target_bytes == telegram_media.TELEGRAM_PHOTO_MAX_BYTES
        assert (
            settings.video_target_bytes
            == telegram_media.TELEGRAM_VIDEO_MAX_MB * 1024 * 1024
        )

    def test_from_config_tolerates_none_dict_and_partial(self):
        assert CompressSettings.from_config(None) == CompressSettings()
        assert CompressSettings.from_config({}) == CompressSettings()
        partial = CompressSettings.from_config({"enabled": False, "video_crf": 40})
        assert partial.enabled is False
        assert partial.video_crf == 40
        assert partial.image_quality_min == 92  # untouched default
        assert CompressSettings.from_config({"unknown": 1}) == CompressSettings()

    def test_from_config_reads_model_objects(self):
        from config_io import CompressConfig

        settings = CompressSettings.from_config(
            CompressConfig(enabled=False, image_min_scale=0.5, video_preset="medium")
        )
        assert settings.enabled is False
        assert settings.image_min_scale == 0.5
        assert settings.video_preset == "medium"

    def test_values_are_clamped(self):
        settings = CompressSettings(
            image_quality_min=200, image_min_scale=0.0, video_min_scale=5.0,
            video_crf=99, timeout_seconds=0,
        )
        assert settings.image_quality_min == 95
        assert settings.image_min_scale == 0.1
        assert settings.video_min_scale == 1.0
        assert settings.video_crf == 51
        assert settings.timeout_seconds == 1

    def test_scale_steps_are_descending(self):
        assert mc._scale_steps(0.8) == [0.95, 0.9, 0.85, 0.8]
        assert mc._scale_steps(1.0) == []

    def test_result_describe_mentions_parameters(self):
        result = mc.CompressResult(
            ok=False, path=None, original_bytes=100, target_bytes=10,
            attempts=("copy:100B",), reason="nope",
        )
        text = result.describe()
        assert "copy:100B" in text and "nope" in text and "100B -> 0B" in text
