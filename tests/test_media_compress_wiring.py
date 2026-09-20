"""Behavior tests: oversized media is compressed before the Telegram upload.

d1  an oversized photo/video is compressed first and the artifact (not the
    original) is what gets uploaded, then deleted after the send.
d2  compression that yields no usable artifact degrades to the text notice
    (delivered=True, media_failed=True) and never sends the text twice.
d3  compress.enabled=false keeps the old "no compression" path.
d4  a compressor that crashes (or a timeout) cannot lose the item: the call is
    wrapped, logged and degraded, and the artifact is cleaned up either way.
d5  the settings object (targets + knobs) is forwarded to the compressor.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

import media_compress
import telegram_media
from media_compress import CompressResult, CompressSettings

FAKE_TOKEN = "123" + ":AA" + "BB"


def _api_response(status_code=200, ok=True, text="{}"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = ok
    resp.headers = {}
    resp.text = text
    return resp


class _FakeAPI:
    """Stands in for requests.post, recording endpoint + uploaded file names."""

    def __init__(self):
        self.media_responses = []
        self.text_response = _api_response(200, True)
        self.media_exception = None
        self.text_calls = 0
        self.media_urls: list[str] = []
        self.uploaded_names: list[list[str]] = []

    def __call__(self, url, timeout=None, **kwargs):
        if "sendMessage" in url:
            self.text_calls += 1
            return self.text_response
        self.media_urls.append(url)
        files = kwargs.get("files") or {}
        self.uploaded_names.append(
            [value[0] if isinstance(value, tuple) else getattr(value, "name", "?")
             for value in files.values()]
        )
        if self.media_exception is not None:
            raise self.media_exception
        return self.media_responses.pop(0)


class _Compressor:
    """Records compress_image/compress_video calls and returns a scripted result."""

    def __init__(self, result=None, exception=None):
        self.result = result
        self.exception = exception
        self.calls: list[dict] = []

    def __call__(self, path, *, target_bytes, settings):
        self.calls.append({"path": path, "target_bytes": target_bytes, "settings": settings})
        if self.exception is not None:
            raise self.exception
        if self.result is not None:
            return self.result
        artifact = Path(path).with_name(Path(path).stem + ".compressed" + Path(path).suffix)
        artifact.write_bytes(b"C" * 8)
        return CompressResult(
            ok=True, path=artifact, original_bytes=Path(path).stat().st_size,
            target_bytes=target_bytes, output_bytes=8, ladder="test", reason="ok",
        )


def _make_file(target, name, size, fill):
    path = Path(target)
    if path.is_dir() or not path.suffix:
        path = path / name
    path.write_bytes(fill * size)
    return path


def _photo(tmp_path, name="big.jpg", size=500):
    return _make_file(tmp_path, name, size, b"x")


def _video(tmp_path, name="clip.mp4", size=500):
    return _make_file(tmp_path, name, size, b"V")


def _artifact_of(path: Path) -> Path:
    return path.with_name(f"{path.stem}.compressed{path.suffix}")


def _no_tmp_or_artifacts(tmp_path: Path) -> list[str]:
    return sorted(
        p.name for p in tmp_path.rglob("*")
        if p.name.endswith(".tmp") or ".compressed" in p.name
    )


# ---------------------------------------------------------------------------
# d1 — compression happens before the send, artifact is uploaded then removed
# ---------------------------------------------------------------------------


class TestCompressionBeforeSend:
    def test_oversized_photo_uploaded_from_artifact(self, tmp_path):
        src = _photo(tmp_path)
        settings = CompressSettings(photo_target_bytes=10)
        compressor = _Compressor()
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_image", compressor):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "new art", [src], settings
            )

        assert outcome == (True, False)
        assert len(compressor.calls) == 1
        assert api.media_urls and api.media_urls[0].endswith("/sendMediaGroup")
        assert api.uploaded_names == [[_artifact_of(src).name]]
        assert src.exists() and src.read_bytes() == b"x" * 500  # original kept
        assert _no_tmp_or_artifacts(tmp_path) == []  # artifact cleaned up

    def test_oversized_video_uploaded_as_send_video(self, tmp_path):
        src = _video(tmp_path)
        settings = CompressSettings(video_target_bytes=10)
        compressor = _Compressor()
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_video", compressor):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, False)
        assert api.media_urls[0].endswith("/sendVideo")
        assert api.uploaded_names == [[_artifact_of(src).name]]
        assert src.exists() and _no_tmp_or_artifacts(tmp_path) == []

    def test_within_limit_media_is_not_compressed(self, tmp_path):
        src = _video(tmp_path / "small.mp4", size=5)
        settings = CompressSettings(video_target_bytes=10)
        compressor = _Compressor(exception=AssertionError("must not compress"))
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_video", compressor):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, False)
        assert compressor.calls == []
        assert api.uploaded_names == [[src.name]]


# ---------------------------------------------------------------------------
# d2 — no usable artifact => text notice once, media_failed=True
# ---------------------------------------------------------------------------


class TestDegradedFallback:
    def test_video_without_artifact_degrades_to_text_once(self, tmp_path):
        src = _video(tmp_path)
        settings = CompressSettings(video_target_bytes=10)
        failed = CompressResult(
            ok=False, path=None, original_bytes=500, target_bytes=10,
            reason="timeout after 300s: ffmpeg -hide_banner",
        )
        api = _FakeAPI()

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_video", _Compressor(result=failed)):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, True)
        assert api.text_calls == 1  # exactly one text notice, no duplicates
        assert api.media_urls == []
        assert _no_tmp_or_artifacts(tmp_path) == []

    def test_photo_still_oversized_after_compression_degrades(self, tmp_path):
        src = _photo(tmp_path)
        settings = CompressSettings(photo_target_bytes=10)
        artifact = _artifact_of(src)
        artifact.write_bytes(b"C" * 99)  # compression helped, but not enough
        too_big = CompressResult(
            ok=True, path=artifact, original_bytes=500, target_bytes=10,
            output_bytes=99, ladder="jpeg-q92-scale0.80", reason="ok",
        )
        api = _FakeAPI()

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_image", _Compressor(result=too_big)):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, True)
        assert api.text_calls == 1
        assert api.media_urls == []
        assert not artifact.exists()  # even the rejected artifact is removed

    def test_failed_upload_after_compression_falls_back_once(self, tmp_path):
        src = _photo(tmp_path)
        settings = CompressSettings(photo_target_bytes=10)
        api = _FakeAPI()
        api.media_responses = [_api_response(400, False, text="Bad Request")]

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_image", _Compressor()):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, True)
        assert api.text_calls == 1
        assert _no_tmp_or_artifacts(tmp_path) == []

    def test_timeout_after_compression_stays_uncertain_and_cleans_up(self, tmp_path):
        src = _video(tmp_path)
        settings = CompressSettings(video_target_bytes=10)
        api = _FakeAPI()
        api.media_exception = requests.Timeout("read timed out")

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_video", _Compressor()):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (False, True)  # uncertain: no text after a timeout
        assert api.text_calls == 0
        assert _no_tmp_or_artifacts(tmp_path) == []


# ---------------------------------------------------------------------------
# d3 — compress.enabled=false keeps the previous behaviour
# ---------------------------------------------------------------------------


class TestCompressionDisabled:
    def test_within_limit_media_is_sent_without_compression(self, tmp_path):
        src = _video(tmp_path / "small.mp4", size=5)
        settings = CompressSettings(enabled=False, video_target_bytes=10)
        compressor = _Compressor(exception=AssertionError("must not compress"))
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_video", compressor):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, False)
        assert compressor.calls == []
        assert api.uploaded_names == [[src.name]]

    def test_oversized_media_with_compression_disabled_uses_the_degrade_path(self, tmp_path):
        src = _video(tmp_path)
        settings = CompressSettings(enabled=False, video_target_bytes=10)
        compressor = _Compressor(exception=AssertionError("must not compress"))
        api = _FakeAPI()

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_video", compressor):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, True)
        assert compressor.calls == []
        assert api.media_urls == []  # no doomed oversized upload
        assert api.text_calls == 1


# ---------------------------------------------------------------------------
# d4 / d5 — crash containment + settings forwarding
# ---------------------------------------------------------------------------


class TestCrashContainmentAndForwarding:
    def test_compressor_crash_degrades_instead_of_losing_the_item(self, tmp_path):
        src = _photo(tmp_path)
        settings = CompressSettings(photo_target_bytes=10)
        api = _FakeAPI()

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(
                 media_compress, "compress_image",
                 _Compressor(exception=RuntimeError("pillow exploded")),
             ):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, True)  # text notice delivered, media retried later
        assert api.text_calls == 1

    def test_settings_and_injected_target_reach_the_compressor(self, tmp_path):
        src = _photo(tmp_path)
        settings = CompressSettings(photo_target_bytes=100, image_quality_min=88)
        compressor = _Compressor()
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(media_compress, "compress_image", compressor):
            telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert compressor.calls[0]["target_bytes"] == 100
        assert compressor.calls[0]["settings"] is settings

    def test_default_settings_apply_when_no_compress_argument(self, tmp_path):
        src = _video(tmp_path / "small.mp4", size=5)
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src]
            )

        # Defaults come from CompressSettings(); a within-limit file is sent as-is.
        assert outcome == (True, False)
        assert api.uploaded_names == [[src.name]]
        assert media_compress.CompressSettings().video_target_bytes > src.stat().st_size


# ---------------------------------------------------------------------------
# real end-to-end (no compressor mock): Pillow compression through the sender
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestRealCompressionThroughSender:
    def test_real_jpeg_is_compressed_then_uploaded(self, tmp_path):
        import os as _os

        from PIL import Image

        src = tmp_path / "real.jpg"
        Image.frombytes("RGB", (400, 400), _os.urandom(400 * 400 * 3)).save(
            src, format="JPEG", quality=95
        )
        settings = CompressSettings(photo_target_bytes=src.stat().st_size // 2)
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        with patch.object(telegram_media.requests, "post", api):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        assert outcome == (True, False)
        assert api.uploaded_names[0][0].endswith(".jpg")
        assert api.uploaded_names[0][0] != src.name
        assert _no_tmp_or_artifacts(tmp_path) == []
        assert src.read_bytes()[:2] == b"\xff\xd8"  # original untouched

    def test_real_video_is_compressed_then_uploaded(self, tmp_path):
        src = tmp_path / "real.mp4"
        gen = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(src),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert gen.returncode == 0, gen.stderr

        # Inject a target genuinely smaller than the source so the real
        # compression path (not the "already within target" skip) runs.
        target = src.stat().st_size // 2
        settings = CompressSettings(video_target_bytes=target)
        api = _FakeAPI()
        api.media_responses = [_api_response()]

        captured = {}
        real_safe_compress = telegram_media._safe_compress

        def _record(compressor, source, target_bytes, compress_settings):
            result = real_safe_compress(compressor, source, target_bytes, compress_settings)
            captured["result"] = result
            captured["artifact_bytes"] = (
                result.path.read_bytes() if result.path is not None else b""
            )
            return result

        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media, "_safe_compress", _record):
            outcome = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [src], settings
            )

        result = captured["result"]
        assert outcome == (True, False)
        assert api.media_urls[0].endswith("/sendVideo")
        # The real compression must actually run (not skip) and land under the
        # injected target.
        assert result.ladder != "skip"
        assert result.output_bytes <= target
        # The uploaded artifact is the compressed derivative and is
        # ffprobe-parseable.
        probe_src = tmp_path / "uploaded.mp4"
        probe_src.write_bytes(captured["artifact_bytes"])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(probe_src)],
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert probe.returncode == 0 and bool(probe.stdout.strip()), probe.stderr
        assert api.uploaded_names[0][0] == "real.compressed.mp4"
        assert _no_tmp_or_artifacts(tmp_path) == []
