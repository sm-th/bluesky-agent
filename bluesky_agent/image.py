"""Capture complete research cards as Bluesky-compatible WebP images."""

from __future__ import annotations

import base64
import hashlib
import io
import math
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from PIL import Image

VIEWPORT_WIDTH = 1200
VIEWPORT_HEIGHT = 1600
BLUESKY_IMAGE_MAX_BYTES = 1_000_000
SCREENSHOT_MARGIN = 24
_RKEY_RE = re.compile(r"^[A-Za-z0-9._~:-]+$")


class PageScreenshotError(RuntimeError):
    """Raised when a complete, valid research-card image cannot be produced."""


class PageScreenshot:
    """Render a deployed research page and encode its complete card as WebP."""

    def __init__(
        self,
        chromium_bin: str | Path,
        output_dir: str | Path,
        readiness_timeout: float = 30.0,
        readiness_interval: float = 0.1,
    ) -> None:
        if readiness_timeout <= 0:
            raise ValueError("readiness_timeout must be positive")
        if readiness_interval <= 0:
            raise ValueError("readiness_interval must be positive")
        self.chromium_bin = str(chromium_bin)
        self.output_dir = Path(output_dir)
        self.readiness_timeout = readiness_timeout
        self.readiness_interval = readiness_interval

    def capture(self, page_url: str, rkey: str) -> Path:
        """Capture ``page_url`` to the deterministic image path for ``rkey``."""
        parsed = urlsplit(page_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("page_url must be an absolute HTTP(S) URL")
        if not rkey or not _RKEY_RE.fullmatch(rkey):
            raise ValueError("rkey contains characters unsafe for an image filename")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / f"{rkey}.webp"
        with tempfile.NamedTemporaryFile(
            prefix=f".{rkey}.", suffix=".png", dir=self.output_dir, delete=False
        ) as temporary:
            png_path = Path(temporary.name)

        try:
            self._capture_png(page_url, png_path)
            self._validate_card_png(png_path)
            self._encode_webp(png_path, output_path)
        finally:
            png_path.unlink(missing_ok=True)
        return output_path

    def _capture_png(self, page_url: str, png_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="research-card-chromium-") as profile_dir:
            devtools_file = Path(profile_dir) / "DevToolsActivePort"
            with tempfile.TemporaryFile(mode="w+b") as browser_stderr:
                try:
                    process = subprocess.Popen(
                        [
                            self.chromium_bin,
                            "--headless=new",
                            "--disable-background-networking",
                            "--disable-component-update",
                            "--disable-default-apps",
                            "--disable-extensions",
                            "--disable-gpu",
                            "--disable-sync",
                            "--hide-scrollbars",
                            "--metrics-recording-only",
                            "--mute-audio",
                            "--no-default-browser-check",
                            "--no-first-run",
                            "--no-sandbox",
                            "--remote-debugging-port=0",
                            "--remote-allow-origins=*",
                            f"--user-data-dir={profile_dir}",
                            "about:blank",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=browser_stderr,
                    )
                except OSError as exc:
                    raise PageScreenshotError(
                        f"could not start Chromium at {self.chromium_bin!r}: {exc}"
                    ) from exc

                try:
                    endpoint = self._wait_for_page_endpoint(
                        process, browser_stderr, devtools_file
                    )
                    try:
                        with _DevToolsConnection(
                            endpoint, self.readiness_timeout
                        ) as devtools:
                            self._render_page(devtools, page_url, png_path)
                    except OSError as exc:
                        raise PageScreenshotError(
                            f"Chromium DevTools connection failed: {exc}"
                        ) from exc
                finally:
                    self._stop_browser(process)

    def _wait_for_page_endpoint(
        self,
        process: subprocess.Popen[bytes],
        browser_stderr: io.BufferedRandom,
        devtools_file: Path,
    ) -> str:
        deadline = time.monotonic() + self.readiness_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise PageScreenshotError(
                    self._browser_failure_message(return_code, browser_stderr)
                )
            if devtools_file.is_file():
                try:
                    lines = devtools_file.read_text(encoding="utf-8").splitlines()
                    port = int(lines[0])
                    with urlopen(
                        f"http://127.0.0.1:{port}/json/list",
                        timeout=self.readiness_interval,
                    ) as response:
                        targets = json.load(response)
                    for target in targets:
                        if target.get("type") == "page" and target.get(
                            "webSocketDebuggerUrl"
                        ):
                            return str(target["webSocketDebuggerUrl"])
                except (OSError, ValueError, IndexError, KeyError, URLError) as exc:
                    last_error = exc
            time.sleep(self.readiness_interval)

        detail = f": {last_error}" if last_error else ""
        raise PageScreenshotError(
            f"Chromium DevTools did not become ready within "
            f"{self.readiness_timeout:g} seconds{detail}"
        )

    @staticmethod
    def _browser_failure_message(
        return_code: int, browser_stderr: io.BufferedRandom
    ) -> str:
        browser_stderr.flush()
        browser_stderr.seek(0)
        detail = browser_stderr.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail[-2000:]}" if detail else ""
        return f"Chromium exited with status {return_code}{suffix}"

    @staticmethod
    def _stop_browser(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _render_page(
        self, devtools: "_DevToolsConnection", page_url: str, png_path: Path
    ) -> None:
        devtools.call("Page.enable")
        devtools.call("Runtime.enable")
        devtools.call(
            "Emulation.setScriptExecutionDisabled",
            {"value": True},
        )
        devtools.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT_WIDTH,
                "height": VIEWPORT_HEIGHT,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": VIEWPORT_WIDTH,
                "screenHeight": VIEWPORT_HEIGHT,
            },
        )
        navigation = devtools.call("Page.navigate", {"url": page_url})
        if navigation.get("errorText"):
            raise PageScreenshotError(
                f"Chromium could not load {page_url!r}: {navigation['errorText']}"
            )

        self._wait_until_ready(devtools)
        self._evaluate(
            devtools,
            """(() => {
                let style = document.getElementById('__research_screenshot_style');
                if (!style) {
                    style = document.createElement('style');
                    style.id = '__research_screenshot_style';
                    document.head.appendChild(style);
                }
                style.textContent = '*, *::before, *::after { '
                    + 'scrollbar-width: none !important; '
                    + 'animation: none !important; transition: none !important; '
                    + 'caret-color: transparent !important; } '
                    + '*::-webkit-scrollbar { display: none !important; }';
                document.documentElement.style.overflow = 'hidden';
                document.body.style.overflow = 'hidden';
                return true;
            })()""",
        )
        dimensions = self._evaluate(
            devtools,
            """(() => {
                const card = document.querySelector('.research-card');
                if (!card) return null;
                const rect = card.getBoundingClientRect();
                return {
                    left: rect.left,
                    top: rect.top,
                    right: rect.right,
                    bottom: rect.bottom,
                    width: rect.width,
                    height: rect.height,
                    clientWidth: card.clientWidth,
                    clientHeight: card.clientHeight,
                    scrollWidth: card.scrollWidth,
                    scrollHeight: card.scrollHeight,
                    viewportWidth: window.innerWidth,
                    viewportHeight: window.innerHeight
                };
            })()""",
        )
        if not isinstance(dimensions, dict):
            raise PageScreenshotError("the page has no .research-card element")
        self._assert_card_fits(dimensions)

        left = max(0, math.floor(float(dimensions["left"]) - SCREENSHOT_MARGIN))
        top = max(0, math.floor(float(dimensions["top"]) - SCREENSHOT_MARGIN))
        right = min(
            VIEWPORT_WIDTH,
            math.ceil(float(dimensions["right"]) + SCREENSHOT_MARGIN),
        )
        bottom = min(
            VIEWPORT_HEIGHT,
            math.ceil(float(dimensions["bottom"]) + SCREENSHOT_MARGIN),
        )
        screenshot = devtools.call(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "clip": {
                    "x": left,
                    "y": top,
                    "width": right - left,
                    "height": bottom - top,
                    "scale": 1,
                },
            },
        )
        encoded = screenshot.get("data")
        if not isinstance(encoded, str):
            raise PageScreenshotError("Chromium returned no screenshot data")
        try:
            png_path.write_bytes(base64.b64decode(encoded, validate=True))
        except (OSError, ValueError) as exc:
            raise PageScreenshotError("Chromium returned invalid screenshot data") from exc

    def _wait_until_ready(self, devtools: "_DevToolsConnection") -> None:
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            ready = self._evaluate(
                devtools,
                """(() => {
                    const card = document.querySelector('.research-card');
                    return document.readyState === 'complete'
                        && !!card
                        && (!document.fonts || document.fonts.status === 'loaded')
                        && Array.from(document.images).every(
                            image => image.complete && image.naturalWidth > 0
                        );
                })()""",
            )
            if ready is True:
                return
            time.sleep(self.readiness_interval)
        raise PageScreenshotError(
            f"research page did not become ready within "
            f"{self.readiness_timeout:g} seconds"
        )

    @staticmethod
    def _evaluate(devtools: "_DevToolsConnection", expression: str) -> Any:
        response = devtools.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if response.get("exceptionDetails"):
            text = response["exceptionDetails"].get("text", "JavaScript evaluation failed")
            raise PageScreenshotError(text)
        return response.get("result", {}).get("value")

    @staticmethod
    def _assert_card_fits(dimensions: dict[str, Any]) -> None:
        try:
            viewport_width = float(dimensions["viewportWidth"])
            viewport_height = float(dimensions["viewportHeight"])
            left = float(dimensions["left"])
            top = float(dimensions["top"])
            right = float(dimensions["right"])
            bottom = float(dimensions["bottom"])
            width = float(dimensions["width"])
            height = float(dimensions["height"])
            client_width = float(dimensions["clientWidth"])
            client_height = float(dimensions["clientHeight"])
            scroll_width = float(dimensions["scrollWidth"])
            scroll_height = float(dimensions["scrollHeight"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PageScreenshotError("Chromium returned invalid research-card bounds") from exc

        epsilon = 0.5
        viewport_is_fixed = (
            abs(viewport_width - VIEWPORT_WIDTH) <= epsilon
            and abs(viewport_height - VIEWPORT_HEIGHT) <= epsilon
        )
        card_is_visible = width > 0 and height > 0
        card_is_inside = (
            left >= -epsilon
            and top >= -epsilon
            and right <= viewport_width + epsilon
            and bottom <= viewport_height + epsilon
        )
        content_is_inside = (
            scroll_width <= client_width + epsilon
            and scroll_height <= client_height + epsilon
        )
        if not (viewport_is_fixed and card_is_visible and card_is_inside and content_is_inside):
            raise PageScreenshotError(
                "the complete .research-card does not fit within the 1200x1600 viewport"
            )

    @staticmethod
    def _validate_card_png(png_path: Path) -> None:
        try:
            with Image.open(png_path) as image:
                image.load()
                if image.format != "PNG":
                    raise PageScreenshotError("Chromium screenshot is not a PNG")
                width, height = image.size
                if (
                    width < 1
                    or height < 1
                    or width > VIEWPORT_WIDTH
                    or height > VIEWPORT_HEIGHT
                ):
                    raise PageScreenshotError(
                        "Chromium screenshot exceeds the 1200x1600 viewport"
                    )
        except PageScreenshotError:
            raise
        except (OSError, ValueError) as exc:
            raise PageScreenshotError("Chromium produced an invalid screenshot") from exc

    @staticmethod
    def _sanitized_rgb(png_path: Path) -> Image.Image:
        try:
            with Image.open(png_path) as source:
                source.load()
                if "A" in source.getbands() or source.mode in {"LA", "PA"}:
                    rgba = source.convert("RGBA")
                    clean = Image.new("RGB", rgba.size, "white")
                    clean.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    clean = Image.new("RGB", source.size)
                    clean.paste(source.convert("RGB"))
                return clean
        except (OSError, ValueError) as exc:
            raise PageScreenshotError("could not decode Chromium screenshot") from exc

    @staticmethod
    def _webp_bytes(image: Image.Image, quality: int) -> bytes:
        output = io.BytesIO()
        image.save(
            output,
            format="WEBP",
            quality=quality,
            method=6,
            lossless=False,
            exact=False,
        )
        return output.getvalue()

    def _encode_webp(self, png_path: Path, output_path: Path) -> None:
        original = self._sanitized_rgb(png_path)
        try:
            scale = 1.0
            previous_size: tuple[int, int] | None = None
            while True:
                size = (
                    max(1, round(original.width * scale)),
                    max(1, round(original.height * scale)),
                )
                if size == previous_size:
                    if size == (1, 1):
                        break
                    scale *= 0.85
                    continue
                previous_size = size
                if size == original.size:
                    candidate = original
                else:
                    candidate = original.resize(size, Image.Resampling.LANCZOS)
                try:
                    for quality in range(88, 23, -8):
                        encoded = self._webp_bytes(candidate, quality)
                        if len(encoded) < BLUESKY_IMAGE_MAX_BYTES:
                            self._write_atomic(output_path, encoded)
                            self._validate_webp(output_path)
                            return
                finally:
                    if candidate is not original:
                        candidate.close()
                if size == (1, 1):
                    break
                scale *= 0.85
        finally:
            original.close()
        raise PageScreenshotError(
            "could not encode the screenshot below the 1,000,000-byte Bluesky limit"
        )

    @staticmethod
    def _write_atomic(output_path: Path, data: bytes) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
                delete=False,
            ) as temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, output_path)
        except OSError as exc:
            raise PageScreenshotError(f"could not write {output_path}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_webp(output_path: Path) -> None:
        try:
            size = output_path.stat().st_size
            if size >= BLUESKY_IMAGE_MAX_BYTES:
                raise PageScreenshotError("encoded WebP exceeds the Bluesky image limit")
            with Image.open(output_path) as image:
                image.load()
                if image.format != "WEBP" or image.width < 1 or image.height < 1:
                    raise PageScreenshotError("encoded image is not a valid WebP")
                if any(key in image.info for key in ("exif", "icc_profile", "xmp")):
                    raise PageScreenshotError("encoded WebP contains metadata")
        except PageScreenshotError:
            raise
        except (OSError, ValueError) as exc:
            raise PageScreenshotError("encoded image is not a valid WebP") from exc


class _DevToolsConnection:
    """Minimal synchronous WebSocket client for the Chromium DevTools protocol."""

    def __init__(self, endpoint: str, timeout: float) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._next_id = 0

    def __enter__(self) -> "_DevToolsConnection":
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise PageScreenshotError("Chromium returned an invalid DevTools endpoint")
        connection = socket.create_connection(
            (parsed.hostname, parsed.port), timeout=self.timeout
        )
        connection.settimeout(self.timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            connection.sendall(request.encode("ascii"))
            response = self._read_http_headers(connection)
            status_line = response.split(b"\r\n", 1)[0]
            expected_accept = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            )
            headers = {
                name.strip().lower(): value.strip()
                for name, value in (
                    line.split(b":", 1)
                    for line in response.split(b"\r\n")[1:]
                    if b":" in line
                )
            }
            if (
                not status_line.startswith(b"HTTP/1.1 101 ")
                or headers.get(b"sec-websocket-accept") != expected_accept
            ):
                detail = status_line.decode("ascii", "replace")
                raise PageScreenshotError(
                    f"Chromium rejected the DevTools connection: {detail}"
                )
        except Exception:
            connection.close()
            raise
        self._socket = connection
        return self

    def __exit__(self, *args: object) -> None:
        if self._socket is not None:
            try:
                self._send_frame(b"", opcode=0x8)
            except OSError:
                pass
            self._socket.close()
            self._socket = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        call_id = self._next_id
        message: dict[str, Any] = {"id": call_id, "method": method}
        if params:
            message["params"] = params
        self._send_frame(json.dumps(message, separators=(",", ":")).encode("utf-8"))
        while True:
            payload = self._receive_message()
            try:
                response = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PageScreenshotError("Chromium sent invalid DevTools data") from exc
            if response.get("id") != call_id:
                continue
            if "error" in response:
                error = response["error"]
                raise PageScreenshotError(
                    f"DevTools {method} failed: {error.get('message', error)}"
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise PageScreenshotError(f"DevTools {method} returned an invalid result")
            return result

    @staticmethod
    def _read_http_headers(connection: socket.socket) -> bytes:
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                raise PageScreenshotError("Chromium closed the DevTools connection")
            response.extend(chunk)
            if len(response) > 65536:
                raise PageScreenshotError("Chromium returned oversized DevTools headers")
        headers, _, remainder = bytes(response).partition(b"\r\n\r\n")
        if remainder:
            raise PageScreenshotError("unexpected data in DevTools handshake")
        return headers

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if self._socket is None:
            raise PageScreenshotError("DevTools connection is not open")
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)

    def _receive_message(self) -> bytes:
        chunks: list[bytes] = []
        message_opcode: int | None = None
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length)
            if masked:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                raise PageScreenshotError("Chromium closed the DevTools connection")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                if message_opcode is not None:
                    raise PageScreenshotError("invalid fragmented DevTools message")
                message_opcode = opcode
            elif opcode != 0x0 or message_opcode is None:
                raise PageScreenshotError("invalid DevTools WebSocket frame")
            chunks.append(payload)
            if final:
                if message_opcode != 0x1:
                    raise PageScreenshotError("Chromium sent non-text DevTools data")
                return b"".join(chunks)

    def _read_exact(self, size: int) -> bytes:
        if self._socket is None:
            raise PageScreenshotError("DevTools connection is not open")
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._socket.recv(size - len(chunks))
            if not chunk:
                raise PageScreenshotError("Chromium closed the DevTools connection")
            chunks.extend(chunk)
        return bytes(chunks)
