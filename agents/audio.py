"""
agents/audio.py
----------------
Audio capability — split out of the old av_handler.py so audio work is
fully independent of camera work (see agents/camera.py).

Two independent directions, both routed through the existing WebSocket
session:

  MIC  (agent → controller)
    Agent captures microphone PCM via sounddevice (or pyaudio fallback),
    encodes Int16 chunks as base64, sends in AUDIO_DATA messages tagged
    direction='agent_mic'.
    Activated by AUDIO_START { direction: 'listen' | 'both' }.

  SPEAKER  (controller → agent)
    Controller captures browser mic via getUserMedia, sends Int16 PCM as
    base64 in AUDIO_DATA messages tagged direction='controller_mic'.
    Agent decodes and plays through speakers.
    Activated by AUDIO_START { direction: 'speak' | 'both' }.

Audio format (both directions):
    Sample rate : 16 000 Hz
    Channels    : 1 (mono)
    Bit depth   : 16-bit signed PCM, little-endian
    Chunk size  : 1 024 samples ≈ 64 ms latency per packet

Dependencies (all optional — degrades gracefully without them):
    pip install sounddevice numpy
    pip install pyaudio          (fallback if sounddevice unavailable)
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict

log = logging.getLogger("nexus.agent.audio")

try:
    import sounddevice as sd
    import numpy as np
    _HAS_SD = True
except (ImportError, OSError):
    # sounddevice raises OSError (not ImportError) at import time when the
    # Python package is installed but the native PortAudio shared library
    # is missing — common on minimal Linux servers/containers. Catching
    # only ImportError here would crash the whole agent process instead
    # of degrading, defeating the "all optional" promise above.
    _HAS_SD = False

if not _HAS_SD:
    try:
        import pyaudio as _pyaudio_mod
        _HAS_PYAUDIO = True
    except ImportError:
        _HAS_PYAUDIO = False
        log.debug("audio: sounddevice and pyaudio not installed — audio unavailable")
else:
    _HAS_PYAUDIO = False  # prefer sounddevice when available

AUDIO_SAMPLE_RATE = 16_000   # Hz — good balance of quality vs bandwidth
AUDIO_CHANNELS    = 1        # mono (voice)
AUDIO_CHUNK       = 1_024    # samples per packet ≈ 64 ms
AUDIO_DTYPE_SD    = "int16"


# ─────────────────────────────────────────────────────────────────────────────
class AudioMixin:
    """
    Mixed into DesktopAgent (via AVHandlerMixin). Requires
    self._send_session(MessageType, payload) from BaseAgent.
    """

    def _audio_init(self) -> None:
        self._mic_task: "asyncio.Task | None" = None
        self._speaker_task: "asyncio.Task | None" = None
        self._speaker_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._mic_active: bool = False
        self._speaker_active: bool = False

    async def _audio_cleanup(self) -> None:
        await self.on_audio_stop({"direction": "all"})

    def audio_capabilities(self) -> Dict[str, bool]:
        return {"audio": _HAS_SD or _HAS_PYAUDIO}

    # ─────────────────────────────────────────────────────────────────────
    # dispatch
    # ─────────────────────────────────────────────────────────────────────

    async def on_audio_start(self, payload: Dict[str, Any]) -> None:
        """
        Start one or both audio directions.

        direction values accepted:
            "listen"  — stream agent mic → controller speaker
            "speak"   — stream controller mic → agent speaker
            "both"    — both directions
        """
        direction = payload.get("direction", "both")

        if direction in ("listen", "both") and not self._mic_active:
            if _HAS_SD or _HAS_PYAUDIO:
                self._mic_active = True
                self._mic_task = asyncio.create_task(self._mic_loop())
                log.info("audio.mic_started")
            else:
                log.warning("audio.mic_unavailable: install sounddevice or pyaudio")

        if direction in ("speak", "both") and not self._speaker_active:
            if _HAS_SD or _HAS_PYAUDIO:
                self._speaker_active = True
                # Drain stale data before starting
                while not self._speaker_queue.empty():
                    try:
                        self._speaker_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                self._speaker_task = asyncio.create_task(self._speaker_loop())
                log.info("audio.speaker_started")
            else:
                log.warning("audio.speaker_unavailable: install sounddevice or pyaudio")

    async def on_audio_stop(self, payload: Dict[str, Any]) -> None:
        """Stop one or both audio directions."""
        direction = payload.get("direction", "all")

        if direction in ("listen", "all") and self._mic_task:
            self._mic_task.cancel()
            try:
                await self._mic_task
            except (asyncio.CancelledError, Exception):
                pass
            self._mic_task = None
            self._mic_active = False
            log.info("audio.mic_stopped")

        if direction in ("speak", "all") and self._speaker_task:
            self._speaker_task.cancel()
            try:
                await self._speaker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._speaker_task = None
            self._speaker_active = False
            log.info("audio.speaker_stopped")

    async def on_audio_data(self, payload: Dict[str, Any]) -> None:
        """
        Receive a PCM chunk from the controller (controller mic → agent speaker).
        Only queued if speaker mode is active.
        """
        if not self._speaker_active:
            return
        b64 = payload.get("data")
        if not b64:
            return
        try:
            pcm_bytes = base64.b64decode(b64)
            self._speaker_queue.put_nowait(pcm_bytes)
        except (ValueError, asyncio.QueueFull):
            pass  # drop frame on overflow — better than blocking

    # ─────────────────────────────────────────────────────────────────────
    # Mic capture (agent → controller)
    # ─────────────────────────────────────────────────────────────────────

    async def _mic_loop(self) -> None:
        if _HAS_SD:
            await self._mic_sounddevice()
        else:
            await self._mic_pyaudio()

    async def _mic_sounddevice(self) -> None:
        from core.session import MessageType

        pcm_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        loop = asyncio.get_running_loop()

        def _cb(indata, frames, time_info, status):
            i16 = (indata[:, 0] * 32767.0).clip(-32768, 32767).astype("int16")
            asyncio.run_coroutine_threadsafe(pcm_queue.put(i16.tobytes()), loop)

        try:
            with sd.InputStream(
                samplerate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
                dtype="float32",
                blocksize=AUDIO_CHUNK,
                callback=_cb,
            ):
                log.info("audio.mic_sounddevice_streaming")
                while True:
                    pcm = await pcm_queue.get()
                    b64 = base64.b64encode(pcm).decode("ascii")
                    await self._send_session(
                        MessageType.AUDIO_DATA,
                        {
                            "data": b64,
                            "direction": "agent_mic",
                            "sample_rate": AUDIO_SAMPLE_RATE,
                            "channels": AUDIO_CHANNELS,
                        },
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("audio.mic_sounddevice_error", extra={"error": str(exc)})

    async def _mic_pyaudio(self) -> None:
        from core.session import MessageType
        import pyaudio

        loop = asyncio.get_running_loop()
        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_SAMPLE_RATE,
                input=True,
                frames_per_buffer=AUDIO_CHUNK,
            )
            log.info("audio.mic_pyaudio_streaming")
            while True:
                pcm = await loop.run_in_executor(None, stream.read, AUDIO_CHUNK, False)
                b64 = base64.b64encode(pcm).decode("ascii")
                await self._send_session(
                    MessageType.AUDIO_DATA,
                    {
                        "data": b64,
                        "direction": "agent_mic",
                        "sample_rate": AUDIO_SAMPLE_RATE,
                        "channels": AUDIO_CHANNELS,
                    },
                )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("audio.mic_pyaudio_error", extra={"error": str(exc)})
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                pa.terminate()
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────
    # Speaker playback (controller → agent)
    # ─────────────────────────────────────────────────────────────────────

    async def _speaker_loop(self) -> None:
        if _HAS_SD:
            await self._speaker_sounddevice()
        else:
            await self._speaker_pyaudio()

    async def _speaker_sounddevice(self) -> None:
        import numpy as np
        loop = asyncio.get_running_loop()

        try:
            with sd.OutputStream(
                samplerate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
                dtype=AUDIO_DTYPE_SD,
                blocksize=AUDIO_CHUNK,
            ) as out:
                log.info("audio.speaker_sounddevice_ready")
                while True:
                    pcm_bytes = await self._speaker_queue.get()
                    audio_arr = np.frombuffer(pcm_bytes, dtype="int16")
                    await loop.run_in_executor(None, out.write, audio_arr)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("audio.speaker_sounddevice_error", extra={"error": str(exc)})

    async def _speaker_pyaudio(self) -> None:
        import pyaudio
        loop = asyncio.get_running_loop()
        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_SAMPLE_RATE,
                output=True,
                frames_per_buffer=AUDIO_CHUNK,
            )
            log.info("audio.speaker_pyaudio_ready")
            while True:
                pcm_bytes = await self._speaker_queue.get()
                await loop.run_in_executor(None, stream.write, pcm_bytes)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("audio.speaker_pyaudio_error", extra={"error": str(exc)})
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                pa.terminate()
            except Exception:
                pass
