"""
agents/camera.py
-----------------
Camera capability — split out of the old av_handler.py so screen/camera
work is fully independent of audio work (see agents/audio.py).

Provides, over the existing WebSocket session:

  CAMERA_LIST     controller → agent : "what cameras do you have?"
                  agent → controller : the answer (dynamic — actually
                  probes the connected device, never a static list)
  CAMERA_START    controller → agent : begin streaming a given device
  CAMERA_STOP     controller → agent : stop streaming
  CAMERA_FRAME    agent → controller : one JPEG frame, hex-encoded
  CAMERA_SNAPSHOT controller → agent : "grab me one full-quality frame
                  right now" — independent of the (lower-res, lower-
                  quality) live stream, so a saved photo looks better
                  than a paused video frame.
  CAMERA_AI_RESULT agent → controller : optional per-frame metadata from
                  a registered AIFrameProcessor (see below) — sent
                  separately from the image so the browser can render
                  overlays without decoding a heavier annotated JPEG.

AI extension point
-------------------
`register_frame_processor()` lets an agent plug a callable into the
capture loop that runs on every raw frame *before* it's JPEG-encoded
and streamed — e.g. motion detection, face/object detection, blurring,
background removal. See `AIFrameProcessor` below for the exact contract.
No processor is registered by default, so behaviour is unchanged unless
you add one. This is scaffolding only — it does not ship a model or add
any AI dependency; see README.md's "Camera AI extension point" section
for a worked example and the performance tradeoffs to know about before
plugging in a real model.

Dependency: opencv-python (optional — degrades gracefully; see
av_handler.py's _HAS_CV2 flag, imported from here).
"""

from __future__ import annotations

import asyncio
import logging
import sys as _sys
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from agents.adaptive_quality import AdaptiveQualityController, apply_tier

log = logging.getLogger("nexus.agent.camera")

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False
    log.debug("camera: opencv-python not installed — camera unavailable")


# ── Constants ──────────────────────────────────────────────────────────────
CAMERA_FPS            = 10     # frames/s for the live stream — sufficient
                                # for face/presence, not meant for smooth video
CAMERA_JPEG_QUALITY    = 70    # live-stream quality (bandwidth-optimised)
CAMERA_MAX_WIDTH       = 640   # live-stream downscale width

SNAPSHOT_JPEG_QUALITY  = 92    # snapshots favour quality over bandwidth —
SNAPSHOT_MAX_WIDTH     = 1920  # they're a one-off request, not 10x/second

CAMERA_PROBE_LIMIT     = 8     # highest device index to probe when enumerating
WARMUP_FRAMES          = 5     # discarded on open — sensors need to settle
MAX_CONSECUTIVE_FAILS  = 30    # consecutive read() failures before giving up


# ── AI extension point ───────────────────────────────────────────────────────

class AIFrameProcessor(Protocol):
    """
    The contract for a pluggable camera AI feature.

    Implement `process()` and register an instance via
    `CameraMixin.register_frame_processor()`. It's called once per
    captured frame, synchronously, inside the same executor call that
    reads the frame — so it runs off the asyncio event loop already and
    won't block other agent traffic, but it DOES gate this frame's
    delivery: a slow processor lowers the effective stream FPS. For a
    genuinely heavy model (a real object detector, not a toy), consider
    running it on every Nth frame internally (skip frames inside your
    processor) rather than every single one.
    """

    def process(
        self, frame: "Any"
    ) -> Tuple["Any", Optional[Dict[str, Any]]]:
        """
        Args:
            frame: a BGR numpy array (cv2's native format), full
                   resolution, before any streaming downscale/compression.

        Returns:
            (frame, metadata) — `frame` is what actually gets encoded and
            streamed to the controller: return the input unchanged to
            leave the video untouched, or return a modified copy (e.g.
            with boxes drawn, faces blurred) to alter what's shown.
            `metadata`, if not None, is sent to the controller as its own
            CAMERA_AI_RESULT message (e.g. {"faces": [...], "motion":
            True}) rather than baked into the image.
        """
        ...


class _NoOpProcessor:
    """Registered nowhere — exists only as a documented example of the
    minimal valid AIFrameProcessor (identity transform, no metadata)."""

    def process(self, frame):
        return frame, None


# ── Camera device model ───────────────────────────────────────────────────

class CameraDevice:
    """One entry in a camera_list response."""

    __slots__ = ("id", "name")

    def __init__(self, id: int, name: str):
        self.id = id
        self.name = name

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name}


# ── Module-level helpers — no self; must be picklable for run_in_executor ──

def _capture_api() -> int:
    """Prefer DirectShow on Windows — the default MSMF backend frequently
    reports isOpened()=True but delivers black frames or stalls on read()."""
    return cv2.CAP_DSHOW if _sys.platform == "win32" else cv2.CAP_ANY


def _device_display_name(idx: int) -> str:
    """
    Best-effort real device name.

    OpenCV itself exposes no cross-platform way to read a camera's model
    name (e.g. "Logitech BRIO"), only its index — this is a real, known
    limitation of building camera enumeration on top of cv2. We try one
    OS-specific source of a real name where it's cheap and reliable, and
    fall back to a generic "Camera N" label everywhere else — the same
    label the old static dropdown used, but now at least backed by an
    actual probe of what's connected rather than a hardcoded 0/1/2 list.
    """
    if _sys.platform.startswith("linux"):
        try:
            name_file = f"/sys/class/video4linux/video{idx}/name"
            with open(name_file, "r", encoding="utf-8", errors="ignore") as f:
                name = f.read().strip()
                if name:
                    return name
        except OSError:
            pass
    return f"Camera {idx}"


def enumerate_cameras() -> List[Dict[str, Any]]:
    """
    Probe camera indices 0..CAMERA_PROBE_LIMIT-1 and return those that
    open AND deliver a readable frame — i.e. this reflects what's
    actually plugged into the device right now, not a fixed list.
    Runs in the thread-pool executor (blocking OpenCV calls).
    """
    if not _HAS_CV2:
        return []
    cameras: List[Dict[str, Any]] = []
    api = _capture_api()
    for idx in range(CAMERA_PROBE_LIMIT):
        cap = cv2.VideoCapture(idx, api)
        if cap.isOpened():
            ret, _ = cap.read()  # confirm it's actually usable, not just "open"
            if ret:
                cameras.append(CameraDevice(idx, _device_display_name(idx)).to_dict())
        cap.release()
        if idx >= 2 and not cameras:
            break  # nothing found in the first few slots — stop probing
    return cameras


def _open_camera(idx: int):
    """Try to open a camera, returning (cap, error_string_or_None)."""
    backends = []
    if _sys.platform == "win32":
        backends.append(("DirectShow", cv2.CAP_DSHOW))
    backends.append(("default", cv2.CAP_ANY))

    for name, api in backends:
        cap = cv2.VideoCapture(idx, api)
        if cap.isOpened():
            log.info("camera.opened", extra={"device": idx, "backend": name})
            return cap, None
        cap.release()

    return None, (
        f"Could not open camera {idx}. Check that no other application "
        "is using it and that the device index is correct."
    )


def _warmup(cap) -> None:
    for _ in range(WARMUP_FRAMES):
        cap.read()


def _read_raw_frame(cap):
    """One frame read with a single retry (some UVC/V4L2 drivers report
    ret=False on the very first read after warm-up but succeed on retry
    within the same tick). Returns the raw BGR numpy frame, or None."""
    for _attempt in range(2):
        ret, frame = cap.read()
        if ret and frame is not None:
            return frame
    return None


def _encode_frame(frame, quality: int, max_width: int) -> Optional[str]:
    try:
        h, w = frame.shape[:2]
        if w > max_width:
            scale = max_width / w
            frame = cv2.resize(frame, (max_width, max(1, int(h * scale))))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes().hex() if ok else None
    except Exception:
        return None


def _run_processors(frame, processors: List["AIFrameProcessor"]):
    """Run every registered AI processor on one frame. A processor that
    raises is logged and skipped — one bad plugin shouldn't kill the
    camera stream for everyone."""
    merged_meta: Optional[Dict[str, Any]] = None
    for proc in processors:
        try:
            frame, meta = proc.process(frame)
            if meta:
                merged_meta = {**(merged_meta or {}), **meta}
        except Exception as exc:
            log.error("camera.ai_processor_error", extra={"error": str(exc)})
    return frame, merged_meta


def _grab_and_process(cap, processors: List["AIFrameProcessor"], quality: int, max_width: int):
    """Read one frame, run AI processors (if any), JPEG-encode for the
    live stream. Single executor round-trip regardless of processor
    count, so registering zero processors costs nothing extra.

    quality/max_width are passed in (rather than using the
    CAMERA_JPEG_QUALITY/CAMERA_MAX_WIDTH module constants directly) so
    the caller's adaptive quality controller can scale them per-tier —
    see agents/adaptive_quality.py's apply_tier()."""
    frame = _read_raw_frame(cap)
    if frame is None:
        return None, None
    if processors:
        frame, meta = _run_processors(frame, processors)
    else:
        meta = None
    frame_hex = _encode_frame(frame, quality, max_width)
    return frame_hex, meta


def _snapshot_from_open_cap(cap, processors: List["AIFrameProcessor"]):
    """Grab one full-quality frame from an already-open, already-warmed-up
    capture (used when a snapshot is requested while the stream is live)."""
    frame = _read_raw_frame(cap)
    if frame is None:
        return None
    if processors:
        frame, _meta = _run_processors(frame, processors)
    return _encode_frame(frame, SNAPSHOT_JPEG_QUALITY, SNAPSHOT_MAX_WIDTH)


def _snapshot_from_new_capture(idx: int, processors: List["AIFrameProcessor"]):
    """Open a short-lived capture just to take one snapshot (used when
    nothing is currently streaming), then release it."""
    cap, err = _open_camera(idx)
    if cap is None:
        return None, err
    try:
        _warmup(cap)
        frame_hex = _snapshot_from_open_cap(cap, processors)
        return frame_hex, (None if frame_hex else "Could not read a frame.")
    finally:
        cap.release()


# ─────────────────────────────────────────────────────────────────────────────
class CameraMixin:
    """
    Mixed into DesktopAgent (via AVHandlerMixin). Requires
    self._send_session(MessageType, payload) and self._send_av_error(...)
    from BaseAgent / AVHandlerMixin respectively.
    """

    def _camera_init(self) -> None:
        self._camera_task: Optional[asyncio.Task] = None
        self._camera_index: int = 0
        self._camera_cap = None                 # shared with snapshot when streaming
        self._camera_lock: asyncio.Lock = asyncio.Lock()
        self._frame_processors: List["AIFrameProcessor"] = []
        # Phase 3: adaptive quality, independent from the screen stream's
        # own controller (agents/desktop_agent.py) — see
        # agents/adaptive_quality.py's module docstring for why they're
        # separate instances.
        self._camera_quality = AdaptiveQualityController()
        self._last_camera_probe_at: float = 0.0

    async def _camera_cleanup(self) -> None:
        await self.on_camera_stop({})

    def camera_capabilities(self) -> Dict[str, bool]:
        return {"camera": _HAS_CV2}

    def register_frame_processor(self, processor: "AIFrameProcessor") -> None:
        """Extension point for AI camera features — see the
        AIFrameProcessor docstring above. Safe to call at any time,
        including while a stream is active."""
        self._frame_processors.append(processor)

    def unregister_frame_processor(self, processor: "AIFrameProcessor") -> None:
        try:
            self._frame_processors.remove(processor)
        except ValueError:
            pass

    # ── CAMERA_LIST ──────────────────────────────────────────────────────

    async def on_camera_list(self, payload: Dict[str, Any]) -> None:
        """Controller requests the list of cameras actually connected to
        this device right now (not a static/hardcoded list)."""
        from core.session import MessageType
        loop = asyncio.get_running_loop()
        cameras = await loop.run_in_executor(None, enumerate_cameras)
        await self._send_session(MessageType.CAMERA_LIST, {"cameras": cameras})

    # ── CAMERA_START / CAMERA_STOP ──────────────────────────────────────

    async def on_camera_start(self, payload: Dict[str, Any]) -> None:
        if not _HAS_CV2:
            await self._send_av_error(
                "camera",
                "opencv-python is not installed on this device. "
                "Run:  pip install opencv-python",
            )
            return
        # Stop any existing stream first so the caller doesn't have to
        # send camera_stop before switching devices.
        if self._camera_task and not self._camera_task.done():
            self._camera_task.cancel()
            try:
                await self._camera_task
            except (asyncio.CancelledError, Exception):
                pass
        self._camera_index = int(payload.get("device", 0))
        self._camera_task = asyncio.create_task(self._camera_loop())
        log.info("camera.start", extra={"device": self._camera_index})

    async def on_camera_stop(self, payload: Dict[str, Any]) -> None:
        if self._camera_task:
            self._camera_task.cancel()
            try:
                await self._camera_task
            except (asyncio.CancelledError, Exception):
                pass
            self._camera_task = None
        log.info("camera.stop")

    # ── CAMERA_SNAPSHOT ──────────────────────────────────────────────────

    async def on_camera_snapshot(self, payload: Dict[str, Any]) -> None:
        """
        Grab one full-quality frame on demand, independent of the live
        stream's lower resolution/quality. Works whether or not the
        stream is currently active:
          - streaming:     borrow the already-open device (under a lock,
                            so we don't race the stream loop's own read())
          - not streaming: open a short-lived capture just for this shot
        """
        from core.session import MessageType

        if not _HAS_CV2:
            await self._send_av_error(
                "camera",
                "opencv-python is not installed on this device. "
                "Run:  pip install opencv-python",
            )
            return

        loop = asyncio.get_running_loop()
        requested_idx = int(payload.get("device", self._camera_index))

        if (
            self._camera_cap is not None
            and self._camera_task is not None
            and not self._camera_task.done()
            and requested_idx == self._camera_index
        ):
            async with self._camera_lock:
                frame_hex = await loop.run_in_executor(
                    None, _snapshot_from_open_cap, self._camera_cap, self._frame_processors
                )
            err = None if frame_hex else "Could not read a frame from the active stream."
        else:
            frame_hex, err = await loop.run_in_executor(
                None, _snapshot_from_new_capture, requested_idx, self._frame_processors
            )

        if not frame_hex:
            await self._send_av_error("camera", err or "Snapshot failed.")
            return

        await self._send_session(
            MessageType.CAMERA_SNAPSHOT,
            {"frame": frame_hex, "device": requested_idx, "timestamp": time.time()},
        )
        log.info("camera.snapshot", extra={"device": requested_idx})

    # ── Streaming loop ───────────────────────────────────────────────────

    async def _camera_loop(self) -> None:
        """
        Capture webcam frames and push them to the controller.

        - All OpenCV calls happen inside run_in_executor so they share
          one OS thread (cv2 objects aren't safe to touch across threads
          otherwise).
        - Warm-up discards the first WARMUP_FRAMES reads — sensors need
          a moment to stabilise; early frames are often black/corrupted.
        - A consecutive-failure watchdog sends av_error and exits rather
          than looping silently forever if the camera disconnects.
        - self._camera_cap is published for on_camera_snapshot() to
          borrow (under self._camera_lock) while streaming is active.
        """
        from core.session import MessageType

        base_interval = 1.0 / CAMERA_FPS
        loop = asyncio.get_running_loop()
        device_idx = self._camera_index
        PROBE_INTERVAL_S = 2.0

        cap, open_err = await loop.run_in_executor(None, _open_camera, device_idx)
        if cap is None:
            await self._send_av_error("camera", open_err)
            return

        await loop.run_in_executor(None, _warmup, cap)

        self._camera_cap = cap
        log.info("camera.streaming", extra={"device": device_idx})
        consecutive_failures = 0

        try:
            while True:
                t0 = loop.time()

                if t0 - self._last_camera_probe_at > PROBE_INTERVAL_S:
                    nonce = self._camera_quality.new_probe_nonce()
                    await self._send_session(MessageType.NET_PROBE, {"nonce": nonce})
                    self._last_camera_probe_at = t0

                quality, scale_ratio, fps_divisor = apply_tier(
                    CAMERA_JPEG_QUALITY, 1.0, self._camera_quality.current_tier
                )
                effective_max_width = int(CAMERA_MAX_WIDTH * scale_ratio)
                interval = base_interval * fps_divisor

                async with self._camera_lock:
                    frame_hex, ai_meta = await loop.run_in_executor(
                        None, _grab_and_process, cap, self._frame_processors,
                        quality, max(160, effective_max_width),
                    )

                if frame_hex:
                    consecutive_failures = 0
                    await self._send_session(
                        MessageType.CAMERA_FRAME,
                        {"frame": frame_hex, "timestamp": time.time()},
                    )
                    if ai_meta:
                        await self._send_session(
                            MessageType.CAMERA_AI_RESULT,
                            {"result": ai_meta, "timestamp": time.time()},
                        )
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILS:
                        await self._send_av_error(
                            "camera",
                            f"Camera {device_idx} stopped delivering frames after "
                            f"{MAX_CONSECUTIVE_FAILS} consecutive failed reads. It may "
                            "have been disconnected or grabbed by another process.",
                        )
                        break

                elapsed = loop.time() - t0
                loop_lag_ratio = max(0.0, (elapsed - interval) / interval) if interval > 0 else 0.0
                self._camera_quality.tick(loop_lag_ratio)

                await asyncio.sleep(max(0.0, interval - elapsed))

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self._send_av_error("camera", f"Camera loop crashed: {exc}")
            log.error("camera.error", extra={"error": str(exc)})
        finally:
            self._camera_cap = None
            await loop.run_in_executor(None, cap.release)
            log.info("camera.loop_ended")
