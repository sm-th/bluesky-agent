from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, PngImagePlugin

import bluesky_agent.image as image_module
from bluesky_agent.image import (
    BLUESKY_IMAGE_MAX_BYTES,
    VIEWPORT_HEIGHT,
    VIEWPORT_WIDTH,
    PageScreenshot,
    PageScreenshotError,
)


def _noise_rgb(width: int, height: int) -> Image.Image:
    needed = width * height * 3
    data = bytearray()
    counter = 0
    while len(data) < needed:
        data.extend(hashlib.sha256(counter.to_bytes(8, "big")).digest())
        counter += 1
    return Image.frombytes("RGB", (width, height), bytes(data[:needed]))


def _card_bounds(**overrides: float) -> dict[str, float]:
    bounds = {
        "left": 20,
        "top": 20,
        "right": 1180,
        "bottom": 1580,
        "width": 1160,
        "height": 1560,
        "clientWidth": 1160,
        "clientHeight": 1560,
        "scrollWidth": 1160,
        "scrollHeight": 1560,
        "viewportWidth": VIEWPORT_WIDTH,
        "viewportHeight": VIEWPORT_HEIGHT,
    }
    bounds.update(overrides)
    return bounds


def test_capture_returns_deterministic_cropped_webp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    screenshot = PageScreenshot("chromium", tmp_path)
    card_size = (1080, 1240)

    def fake_capture(page_url: str, png_path: Path) -> None:
        assert page_url == "https://wiki.example/research/turn"
        Image.new("RGB", card_size, "white").save(png_path, "PNG")

    monkeypatch.setattr(screenshot, "_capture_png", fake_capture)

    first = screenshot.capture("https://wiki.example/research/turn", "turn-key")
    second = screenshot.capture("https://wiki.example/research/turn", "turn-key")

    assert first == tmp_path / "turn-key.webp"
    assert second == first
    assert first.stat().st_size < BLUESKY_IMAGE_MAX_BYTES
    with Image.open(first) as rendered:
        rendered.load()
        assert rendered.format == "WEBP"
        assert rendered.size == card_size

def test_compression_reduces_dimensions_preserving_aspect_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.png"
    destination = tmp_path / "compressed.webp"
    source = _noise_rgb(300, 400)
    source.save(source_path, "PNG")
    source.close()
    monkeypatch.setattr(image_module, "BLUESKY_IMAGE_MAX_BYTES", 12_000)

    PageScreenshot("chromium", tmp_path)._encode_webp(source_path, destination)

    assert destination.stat().st_size < 12_000
    with Image.open(destination) as compressed:
        compressed.load()
        assert compressed.format == "WEBP"
        assert compressed.width < 300
        assert compressed.height < 400
        assert compressed.width / compressed.height == pytest.approx(3 / 4, abs=0.01)


def test_encoding_strips_metadata(tmp_path: Path) -> None:
    source_path = tmp_path / "metadata.png"
    destination = tmp_path / "clean.webp"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "must not survive")
    Image.new("RGBA", (80, 120), (30, 60, 90, 128)).save(
        source_path, "PNG", pnginfo=metadata
    )

    PageScreenshot("chromium", tmp_path)._encode_webp(source_path, destination)

    with Image.open(destination) as clean:
        clean.load()
        assert clean.format == "WEBP"
        assert clean.mode == "RGB"
        assert not any(key in clean.info for key in ("exif", "icc_profile", "xmp"))


def test_encoding_fails_when_no_candidate_can_meet_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.png"
    Image.new("RGB", (2, 2), "black").save(source_path, "PNG")
    screenshot = PageScreenshot("chromium", tmp_path)
    monkeypatch.setattr(image_module, "BLUESKY_IMAGE_MAX_BYTES", 10)
    monkeypatch.setattr(screenshot, "_webp_bytes", lambda candidate, quality: b"x" * 10)

    with pytest.raises(PageScreenshotError, match="below the 1,000,000-byte"):
        screenshot._encode_webp(source_path, tmp_path / "impossible.webp")


def test_rejects_card_extending_below_viewport() -> None:
    with pytest.raises(PageScreenshotError, match="does not fit"):
        PageScreenshot._assert_card_fits(
            _card_bounds(bottom=1601, height=1581, scrollHeight=1581, clientHeight=1581)
        )


def test_rejects_card_with_clipped_internal_content() -> None:
    with pytest.raises(PageScreenshotError, match="does not fit"):
        PageScreenshot._assert_card_fits(
            _card_bounds(scrollHeight=1600, clientHeight=1560)
        )


class _ExitedProcess:
    def poll(self) -> int:
        return 23

    def terminate(self) -> None:
        raise AssertionError("an exited process must not be terminated")


def test_chromium_subprocess_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    screenshot = PageScreenshot(
        "/missing/chromium", tmp_path, readiness_timeout=0.1, readiness_interval=0.01
    )
    monkeypatch.setattr(image_module.subprocess, "Popen", lambda *args, **kwargs: _ExitedProcess())

    with pytest.raises(PageScreenshotError, match="exited with status 23"):
        screenshot.capture("https://wiki.example/research/turn", "turn")
    assert not (tmp_path / "turn.webp").exists()


def test_chromium_launch_os_error_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    screenshot = PageScreenshot("/missing/chromium", tmp_path)
    monkeypatch.setattr(
        image_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("not found")),
    )

    with pytest.raises(PageScreenshotError, match="could not start Chromium"):
        screenshot.capture("https://wiki.example/research/turn", "turn")


def test_capture_rejects_unsafe_record_key(tmp_path: Path) -> None:
    screenshot = PageScreenshot("chromium", tmp_path)
    with patch.object(screenshot, "_capture_png") as capture:
        with pytest.raises(ValueError, match="unsafe"):
            screenshot.capture("https://wiki.example/research/turn", "../escape")
    capture.assert_not_called()
