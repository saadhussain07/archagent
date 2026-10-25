"""Token accounting and USD cost tracking for the DeepSeek API.

Prices are USD per 1M tokens, from https://api-docs.deepseek.com/quick_start/pricing
(checked 2026-10-25). Off-peak is half of peak. Peak hours are
01:00-04:00 and 06:00-10:00 UTC, Monday-Friday.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass, field

PRICING: dict[str, dict[str, dict[str, float]]] = {
    "deepseek-v4-pro": {
        "peak": {"cache_hit": 0.044, "cache_miss": 1.32, "output": 3.96},
        "off_peak": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
    },
    "deepseek-flash": {
        "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
        "off_peak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
    },
}
PEAK_WINDOWS_UTC = ((1, 4), (6, 10))
ZERO = {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0}


def is_peak(now: _dt.datetime | None = None) -> bool:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    now = now.astimezone(_dt.timezone.utc)
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    return any(start <= now.hour < end for start, end in PEAK_WINDOWS_UTC)


def rates_for(model: str, now: _dt.datetime | None = None, assume_peak: bool = False) -> dict[str, float]:
    table = PRICING.get(model, PRICING["deepseek-v4-pro"])  # unknown model -> priciest table
    return table["peak" if (assume_peak or is_peak(now)) else "off_peak"]


@dataclass
class Usage:
    cache_hit: int = 0
    cache_miss: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def prompt(self) -> int:
        return self.cache_hit + self.cache_miss


@dataclass
class CostTracker:
    """Accumulates usage per call and converts it to USD.

    ``assume_peak=True`` prices every call at peak rates, which gives a
    worst-case figure - the default, so the budget is never exceeded
    because of a pricing-window guess.
    """

    model: str = "deepseek-v4-pro"
    budget_usd: float = 1.0
    assume_peak: bool = True
    free: bool = False  # free-tier / local providers: tokens are counted, cost is $0
    total: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: dict) -> float:
        """Add one API ``usage`` object; returns the cost of that call."""
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        miss = usage.get("prompt_cache_miss_tokens")
        miss = int(miss) if miss is not None else max(prompt - hit, 0)
        out = int(usage.get("completion_tokens", 0) or 0)
        details = usage.get("completion_tokens_details") or {}
        reasoning = int(details.get("reasoning_tokens", 0) or 0)

        r = ZERO if self.free else rates_for(self.model, assume_peak=self.assume_peak)
        cost = (hit * r["cache_hit"] + miss * r["cache_miss"] + out * r["output"]) / 1_000_000
        with self._lock:
            self.total.cache_hit += hit
            self.total.cache_miss += miss
            self.total.output += out
            self.total.reasoning += reasoning
            self.cost_usd += cost
            self.calls += 1
        return cost

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self.cost_usd

    @property
    def fraction_used(self) -> float:
        return self.cost_usd / self.budget_usd if self.budget_usd > 0 else 0.0

    def estimate_call_cost(self, prompt_tokens: int, max_output: int) -> float:
        """Worst case for the next call: whole prompt uncached + max output."""
        r = ZERO if self.free else rates_for(self.model, assume_peak=True)
        return (prompt_tokens * r["cache_miss"] + max_output * r["output"]) / 1_000_000

    def estimate_cached_cost(self, cached_tokens: int) -> float:
        r = ZERO if self.free else rates_for(self.model, assume_peak=True)
        return cached_tokens * r["cache_hit"] / 1_000_000

    def output_rate(self) -> float:
        """USD per output token at peak rates."""
        if self.free:
            return 1e-12  # avoids division by zero; free calls never hit the budget
        return rates_for(self.model, assume_peak=True)["output"] / 1_000_000

    def snapshot(self) -> dict:
        return {
            "model": self.model,
            "calls": self.calls,
            "prompt_cache_hit_tokens": self.total.cache_hit,
            "prompt_cache_miss_tokens": self.total.cache_miss,
            "completion_tokens": self.total.output,
            "reasoning_tokens": self.total.reasoning,
            "cost_usd": round(self.cost_usd, 6),
            "budget_usd": self.budget_usd,
            "pricing_mode": "free tier ($0)" if self.free else
                            "peak (worst case)" if self.assume_peak else ("peak" if is_peak() else "off-peak"),
        }
