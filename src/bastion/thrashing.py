"""Per-agent swap thrashing detection (M58).

Tracks model swap patterns per agent and detects poorly-batched pipelines
that cause GPU-damaging swap thrashing. Thresholds derived from RTX 5090
crash investigation: crash zone >8 swaps/min.

Design: sliding window of recent requests per agent (keyed by X-Agent-Id
or source IP). Computes swap ratio and returns a verdict (ok/warn/halt).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from bastion.metrics import record_thrashing_verdict
from bastion.models import ThrashingDetectionConfig


class ThrashingVerdict(StrEnum):
    """Verdict from thrashing detection check."""
    OK = "ok"
    WARN = "warn"
    HALT = "halt"


@dataclass
class ThrashingCheckResult:
    """Result of a thrashing check for an agent."""
    level: ThrashingVerdict = ThrashingVerdict.OK
    swap_ratio: float = 0.0
    window_size: int = 0
    cooloff_remaining: float = 0.0
    estimated_penalty_seconds: float = 0.0


@dataclass
class AgentThrashingSnapshot:
    """Point-in-time snapshot of one agent's thrashing state for the API."""
    agent_id: str
    verdict: ThrashingVerdict
    cooloff_remaining_s: float
    swap_ratio: float
    last_run_s: float  # seconds since last record_request call (monotonic delta)


@dataclass
class _AgentWindow:
    """Sliding window of recent model requests for one agent."""
    models: deque[str] = field(default_factory=deque)
    cooloff_until: float = 0.0  # monotonic time when cooloff expires
    # monotonic time of last record_request call
    last_seen: float = field(default_factory=time.monotonic)


class ThrashingDetector:
    """Detects per-agent swap thrashing patterns.

    Parameters
    ----------
    config : ThrashingDetectionConfig
        Detection thresholds and mode.
    """

    def __init__(self, config: ThrashingDetectionConfig) -> None:
        self._config = config
        self._agents: dict[str, _AgentWindow] = {}
        self._total_warnings: int = 0
        self._total_halts: int = 0

    @property
    def total_warnings(self) -> int:
        return self._total_warnings

    @property
    def total_halts(self) -> int:
        return self._total_halts

    def record_request(self, agent_id: str, model: str) -> None:
        """Record a request from an agent for a specific model.

        Parameters
        ----------
        agent_id : str
            Agent identifier (from X-Agent-Id header or source IP).
        model : str
            Model name requested (after any complexity routing override).
        """
        window = self._agents.get(agent_id)
        if window is None:
            window = _AgentWindow(models=deque(maxlen=self._config.window_size))
            self._agents[agent_id] = window
        window.models.append(model)
        window.last_seen = time.monotonic()

    def check(self, agent_id: str) -> ThrashingCheckResult:
        """Check if an agent is thrashing.

        Parameters
        ----------
        agent_id : str
            Agent identifier to check.

        Returns
        -------
        ThrashingCheckResult
            Verdict with swap ratio and cooloff info.
        """
        if not self._config.enabled:
            return ThrashingCheckResult()

        window = self._agents.get(agent_id)
        if window is None:
            return ThrashingCheckResult()

        models = list(window.models)
        n = len(models)

        # Not enough data to evaluate
        if n < self._config.min_requests_before_eval:
            return ThrashingCheckResult(window_size=n)

        # Check cooloff
        now = time.monotonic()
        remaining = max(0.0, window.cooloff_until - now)
        if remaining > 0 and self._config.mode == "strict":
            return ThrashingCheckResult(
                level=ThrashingVerdict.HALT,
                swap_ratio=self._compute_swap_ratio(models),
                window_size=n,
                cooloff_remaining=remaining,
            )

        # Count swaps (consecutive model changes)
        swap_ratio = self._compute_swap_ratio(models)
        result = ThrashingCheckResult(swap_ratio=swap_ratio, window_size=n)

        # Estimate penalty: ~14s per large swap, ~8s per medium swap, avg ~11s
        avg_swap_cost = 11.0
        swaps_in_window = int(swap_ratio * (n - 1))
        result.estimated_penalty_seconds = swaps_in_window * avg_swap_cost

        # Determine verdict
        if swap_ratio >= self._config.halt_swap_ratio and self._config.mode == "strict":
            result.level = ThrashingVerdict.HALT
            window.cooloff_until = now + self._config.cooloff_seconds
            self._total_warnings += 1  # halt implies a warning
            self._total_halts += 1
            # Vision C schema-frozen metric: bastion_thrashing_detector_halt_total
            # Emit HALTED only (do NOT also emit WARNED — the council R3
            # contract requires single-emit per verdict transition; the
            # _total_warnings bump above is internal book-keeping for the
            # /broker/thrashing API and is not exposed as a metric label).
            #
            # IMPORTANT: agent_id MUST be a registered agent name or /24 IP
            # prefix here. The detector's caller (proxy.py / a2a.py) is
            # responsible for not passing task UUIDs. See Risk R3 and the
            # docstring on record_thrashing_verdict in bastion.metrics.
            record_thrashing_verdict(agent_id=agent_id, verdict="HALTED")
        elif swap_ratio >= self._config.warn_swap_ratio:
            result.level = ThrashingVerdict.WARN
            self._total_warnings += 1
            # Vision C metric: only emit WARNED in the pure-warn branch.
            record_thrashing_verdict(agent_id=agent_id, verdict="WARNED")

        return result

    def snapshot(self) -> list[AgentThrashingSnapshot]:
        """Return a point-in-time snapshot of every tracked agent's state.

        Each agent is evaluated via ``check()`` so verdict and cooloff_remaining
        are computed consistently with existing logic.  ``last_run_s`` is derived
        from ``_AgentWindow.last_seen`` (monotonic delta since last
        ``record_request`` call).
        """
        now = time.monotonic()
        results: list[AgentThrashingSnapshot] = []
        for agent_id, window in self._agents.items():
            result = self.check(agent_id)
            results.append(
                AgentThrashingSnapshot(
                    agent_id=agent_id,
                    verdict=result.level,
                    cooloff_remaining_s=result.cooloff_remaining,
                    swap_ratio=result.swap_ratio,
                    last_run_s=now - window.last_seen,
                )
            )
        return results

    @staticmethod
    def _compute_swap_ratio(models: list[str]) -> float:
        """Compute the fraction of consecutive request pairs that differ.

        Parameters
        ----------
        models : list[str]
            Ordered list of model names in the window.

        Returns
        -------
        float
            Swap ratio (0.0 = all same model, 1.0 = every pair different).
        """
        if len(models) < 2:
            return 0.0
        swaps = sum(1 for i in range(1, len(models)) if models[i] != models[i - 1])
        return swaps / (len(models) - 1)
