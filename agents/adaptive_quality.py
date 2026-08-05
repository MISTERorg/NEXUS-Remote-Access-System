"""
agents/adaptive_quality.py
---------------------------
Phase 3: adjusts stream quality (JPEG quality, max width, and effective
frame rate) in real time based on measured network conditions, instead
of the fixed values every stream used before this.

Two independent signals feed the decision, combined conservatively (a
downgrade from either signal wins immediately; an upgrade needs both to
agree — see AdaptiveQualityController.tick()):

  1. RTT, measured via MessageType.NET_PROBE / NET_PROBE_ACK — the agent
     sends a small nonce, the controller (relay.js) echoes it back
     immediately with no processing, so elapsed time is a genuine
     network round-trip, not confounded by browser rendering work.
  2. Loop lag — whether the capture loop (screen or camera) is keeping
     up with its own target interval. This catches CPU/encoding
     bottlenecks that RTT alone wouldn't: a fast network with a slow
     capture loop still needs to shed quality, and RTT wouldn't show it.

Tuning philosophy — AIMD (additive-increase, multiplicative-decrease),
the same family of algorithm TCP congestion control and most adaptive
video streaming use: react FAST to congestion (immediate, large step
down), recover SLOWLY (small step up, only after sustained good
conditions). This avoids the classic adaptive-bitrate failure mode of
oscillating between quality tiers every few seconds.

This module has no dependency on cv2/mss/PIL — it only computes numbers.
agents/camera.py and agents/desktop_agent.py each own applying those
numbers to their own capture/encode calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class QualityTier:
    jpeg_quality: int
    max_width: int
    fps_divisor: int   # 1 = every frame, 2 = every other frame, etc.
    label: str


# Tiers ordered worst -> best. Deliberately coarse (4 tiers, not a
# continuous scale) — a UI/log message like "quality: reduced" means
# something to a person; "quality: 47.3" doesn't, and a continuous scale
# invites constant small flapping for no perceptible benefit.
_TIERS = [
    QualityTier(jpeg_quality=35, max_width=480,  fps_divisor=3, label="low"),
    QualityTier(jpeg_quality=50, max_width=640,  fps_divisor=2, label="reduced"),
    QualityTier(jpeg_quality=65, max_width=960,  fps_divisor=1, label="normal"),
    QualityTier(jpeg_quality=80, max_width=1280, fps_divisor=1, label="high"),
]

DEFAULT_TIER_INDEX = 2  # "normal" — matches this project's pre-existing
                         # fixed defaults, so a fresh session starts where
                         # things already were, and only moves from there
                         # once real measurements exist.

# RTT thresholds (milliseconds) — above HIGH_RTT_MS, network alone is
# reason enough to downgrade regardless of loop lag.
GOOD_RTT_MS = 150
HIGH_RTT_MS = 400

# How far behind its own target interval a capture loop needs to be,
# as a fraction of that interval, before we treat it as "struggling"
# (CPU/encode-bound, independent of network).
LOOP_LAG_THRESHOLD = 0.5   # loop took 50%+ longer than its target interval

# Cooldowns prevent a single noisy sample from causing a step; both
# directions require several consecutive ticks agreeing first.
DOWNGRADE_STREAK_NEEDED = 2   # react fast...
UPGRADE_STREAK_NEEDED = 6     # ...but recover slowly


class AdaptiveQualityController:
    """
    One instance per active stream (one per camera stream, one per
    screen stream — they tune independently, since a laggy screen share
    shouldn't necessarily also throttle a concurrently-running camera
    stream, and vice versa).
    """

    def __init__(self, start_tier: int = DEFAULT_TIER_INDEX):
        self._tier_index = max(0, min(start_tier, len(_TIERS) - 1))
        self._rtt_samples: list = []
        self._last_rtt_ms: Optional[float] = None
        self._downgrade_streak = 0
        self._upgrade_streak = 0
        self._pending_probes: dict = {}   # nonce -> sent_at, for RTT measurement

    # ── RTT measurement ──────────────────────────────────────────────

    def new_probe_nonce(self) -> str:
        import secrets
        nonce = secrets.token_hex(4)
        self._pending_probes[nonce] = time.monotonic()
        # Bound memory if acks never arrive (probe lost, controller gone)
        if len(self._pending_probes) > 20:
            oldest = min(self._pending_probes, key=self._pending_probes.get)
            self._pending_probes.pop(oldest, None)
        return nonce

    def record_probe_ack(self, nonce: str) -> Optional[float]:
        """Call when a NET_PROBE_ACK arrives. Returns the measured RTT
        in ms, or None if the nonce is unrecognized (late/duplicate ack,
        or we already evicted it) — never raises."""
        sent_at = self._pending_probes.pop(nonce, None)
        if sent_at is None:
            return None
        rtt_ms = (time.monotonic() - sent_at) * 1000.0
        self._last_rtt_ms = rtt_ms
        self._rtt_samples.append(rtt_ms)
        if len(self._rtt_samples) > 5:
            self._rtt_samples.pop(0)
        return rtt_ms

    @property
    def smoothed_rtt_ms(self) -> Optional[float]:
        if not self._rtt_samples:
            return None
        return sum(self._rtt_samples) / len(self._rtt_samples)

    # ── Loop-lag feedback + the actual tuning decision ───────────────

    def tick(self, loop_lag_ratio: float) -> QualityTier:
        """
        Call once per capture loop iteration with how far over its
        target interval that iteration ran (0.0 = right on time, 1.0 =
        took 2x as long as intended, etc.). Returns the tier to use for
        the NEXT frame — deliberately one-tick-delayed rather than
        applied mid-frame, so a single capture is never encoded twice
        with different settings.
        """
        rtt = self.smoothed_rtt_ms
        loop_struggling = loop_lag_ratio > LOOP_LAG_THRESHOLD
        network_poor = rtt is not None and rtt > HIGH_RTT_MS
        network_good = rtt is None or rtt < GOOD_RTT_MS  # unknown RTT doesn't block upgrades

        should_downgrade = loop_struggling or network_poor
        should_upgrade = (not loop_struggling) and network_good

        if should_downgrade:
            self._downgrade_streak += 1
            self._upgrade_streak = 0
        elif should_upgrade:
            self._upgrade_streak += 1
            self._downgrade_streak = 0
        else:
            # Ambiguous middle ground — neither clearly bad nor clearly
            # good (e.g. RTT between GOOD and HIGH). Don't move either
            # streak; hold at current tier until a clearer signal arrives.
            self._downgrade_streak = 0
            self._upgrade_streak = 0

        if self._downgrade_streak >= DOWNGRADE_STREAK_NEEDED and self._tier_index > 0:
            self._tier_index -= 1
            self._downgrade_streak = 0
        elif self._upgrade_streak >= UPGRADE_STREAK_NEEDED and self._tier_index < len(_TIERS) - 1:
            self._tier_index += 1
            self._upgrade_streak = 0

        return _TIERS[self._tier_index]

    @property
    def current_tier(self) -> QualityTier:
        return _TIERS[self._tier_index]


def apply_tier(
    baseline_quality: int, baseline_scale: float, tier: QualityTier
) -> tuple[int, float, int]:
    """
    Scales an operator-configured baseline (e.g. --quality 90 on the
    command line) relative to how far the current tier is from
    "normal", rather than replacing it outright with the tier's
    absolute numbers. This means someone who explicitly asked for
    higher-than-default quality still gets more than someone on
    defaults, even while both degrade/recover proportionally with
    network conditions — adaptive tuning adjusts intent, it doesn't
    override it.

    Returns (effective_jpeg_quality, effective_frame_scale, fps_divisor).
    """
    normal = _TIERS[DEFAULT_TIER_INDEX]

    quality_ratio = tier.jpeg_quality / normal.jpeg_quality
    scale_ratio = tier.max_width / normal.max_width

    effective_quality = round(baseline_quality * quality_ratio)
    effective_quality = max(15, min(95, effective_quality))

    effective_scale = baseline_scale * scale_ratio
    effective_scale = max(0.1, min(1.0, effective_scale))

    return effective_quality, effective_scale, tier.fps_divisor
