"""
agents/av_handler.py
--------------------
Composition point for DesktopAgent's audio/video capability.

This file used to contain ALL camera and audio logic in one ~590-line
mixin. It's now a thin seam that combines two independent, single-
responsibility modules:

    agents/camera.py  — camera enumeration, streaming, snapshot,
                          and the AI frame-processor extension point
    agents/audio.py   — mic capture + speaker playback

...so each can be edited, debugged, and extended without touching the
other. DesktopAgent only ever talks to AVHandlerMixin (_av_init,
_av_cleanup, av_capabilities) — nothing else needed to change there.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core.session import MessageType
from agents.camera import CameraMixin
from agents.audio import AudioMixin

log = logging.getLogger("nexus.agent.av")


class AVHandlerMixin(CameraMixin, AudioMixin):
    """
    Mixed into DesktopAgent.
    Requires self._send_session(MessageType, payload) from BaseAgent.
    Call AVHandlerMixin._av_init(self) from DesktopAgent.__init__().
    Call AVHandlerMixin._av_cleanup(self) from on_session_end().
    """

    def _av_init(self) -> None:
        """Initialise camera + audio state. Call from DesktopAgent.__init__."""
        self._camera_init()
        self._audio_init()

    async def _av_cleanup(self) -> None:
        """Stop all camera + audio loops. Call from on_session_end."""
        await self._camera_cleanup()
        await self._audio_cleanup()

    def av_capabilities(self) -> Dict[str, bool]:
        """Return dict suitable for inclusion in DeviceCapabilities."""
        return {**self.camera_capabilities(), **self.audio_capabilities()}

    # ─────────────────────────────────────────────────────────────────────
    # Shared by both CameraMixin and AudioMixin (Python resolves self at
    # call time, so defining this once here — rather than duplicating it
    # in both camera.py and audio.py — works fine via the MRO).
    # ─────────────────────────────────────────────────────────────────────

    async def _send_av_error(self, source: str, message: str) -> None:
        """Send an av_error message to the controller so the UI can display it."""
        try:
            await self._send_session(
                MessageType.AV_ERROR,
                {"source": source, "message": message},
            )
        except Exception:
            pass
        log.warning("av.error", extra={"source": source, "message": message})
