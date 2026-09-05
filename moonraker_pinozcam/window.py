"""The failure criterion, the sliding-window criterion PiNozCam uses.

Pure logic: no camera, no printer, no inference. Kept separate so it can be
tested without hardware, and so the numbers below stay in one place.
"""

import collections
import time

# Both bounds must hold before any judgement is made. Frames make the ratio
# statistically meaningful; seconds stop a fast board acting on twenty
# frames gathered inside nine seconds, which could all be one transient.
INIT_MIN_FRAMES = 20
INIT_MIN_SECONDS = 30.0


class FailureWindow(object):
    """Sliding window of per-frame verdicts, judged as a duty cycle.

    The criterion is `alarming frames / total frames` over count_time, NOT
    a count of alarming frames. A count's scale tracks hardware speed: a
    300 s window holds ~61 frames on a Pi Zero 2 W and ~1107 on a Pi 5, an
    18x difference in false-positive exposure for the same threshold. A
    ratio is the sample mean and is speed-invariant by construction, and it
    uses every frame instead of discarding most of them.
    """

    def __init__(self, count_time=300.0, failure_ratio=0.30):
        self.count_time = float(count_time)
        self.failure_ratio = float(failure_ratio)
        self._entries = collections.deque()   # (monotonic, alarming)
        self._started_at = None
        self._frames = 0
        self.last_ratio = 0.0

    def reset(self):
        """Discard all history.

        Used when a setting that changes what severity *means* is saved, or
        when the camera changes: statistics gathered under the old scale are
        not comparable with new ones.
        """
        self._entries.clear()
        self._started_at = None
        self._frames = 0
        self.last_ratio = 0.0

    def add(self, alarming, now=None):
        """Record one frame's verdict."""
        now = time.monotonic() if now is None else now
        if self._started_at is None:
            self._started_at = now
        self._frames += 1
        self._entries.append((now, bool(alarming)))
        self._prune(now)

    def _prune(self, now):
        cutoff = now - self.count_time
        while self._entries and self._entries[0][0] < cutoff:
            self._entries.popleft()

    def armed(self, now=None):
        """Whether enough evidence exists to judge anything yet."""
        if self._started_at is None:
            return False
        now = time.monotonic() if now is None else now
        return (self._frames >= INIT_MIN_FRAMES
                and now - self._started_at >= INIT_MIN_SECONDS)

    def decide(self, now=None):
        """Return (threshold_met, ratio).

        The ratio is always computed and reported, even before the window
        is armed, so a UI can show progress rather than a flat zero.
        """
        now = time.monotonic() if now is None else now
        self._prune(now)
        total = len(self._entries)
        ratio = 0.0
        if total:
            ratio = sum(1 for _, a in self._entries if a) / float(total)
        self.last_ratio = ratio
        if not self.armed(now):
            return False, ratio
        return ratio >= self.failure_ratio, ratio

    @property
    def stats(self):
        total = len(self._entries)
        return {
            "frames_seen": self._frames,
            "window_frames": total,
            "alarming": sum(1 for _, a in self._entries if a),
            "ratio": self.last_ratio,
            "armed": self.armed(),
        }
