"""AI Decision Engine: market regime, signal quality and explanations.

Boundaries (enforced by design, not by convention):

* The engine can only *veto* a strategy signal or annotate it. It cannot widen
  stops, raise risk, bypass the Risk Manager or invent prices - it receives no
  handle to the gateway, the risk config or the executor.
* The final go/no-go for any order is the deterministic RiskManager.
* An optional learned model (logistic regression) is trained only with
  time-ordered walk-forward splits and an embargo gap (see backtester).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from .market_analyzer import MarketSnapshot
from .models import Signal, utcnow


class Regime:
    TREND = "TREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RegimeAssessment:
    regime: str
    confidence: float
    reasons: tuple[str, ...]


def classify_regime(snapshot: MarketSnapshot, timeframe: str = "H1") -> RegimeAssessment:
    f = snapshot.frame(timeframe)
    if f is None:
        return RegimeAssessment(Regime.UNKNOWN, 0.0, (f"no {timeframe} data",))
    bar_range_atr = (f.high - f.low) / f.atr if f.atr > 0 else 0.0
    if f.atr_rank >= 0.95 or bar_range_atr >= 2.5:
        return RegimeAssessment(
            Regime.HIGH_VOLATILITY, min(1.0, max(f.atr_rank, bar_range_atr / 3)),
            (f"ATR rank {f.atr_rank:.2f}", f"last bar range {bar_range_atr:.1f} ATR"),
        )
    aligned = f.trend in ("UP", "DOWN")
    if not math.isnan(f.adx) and f.adx >= 20 and aligned:
        return RegimeAssessment(Regime.TREND, min(1.0, f.adx / 40), (f"ADX {f.adx:.1f} >= 20", f"EMA alignment {f.trend}"))
    reasons = [f"ADX {f.adx:.1f}" + (" < 20" if f.adx < 20 else ""), f"EMA alignment {f.trend}"]
    return RegimeAssessment(Regime.RANGE, 0.6 if f.adx < 20 else 0.4, tuple(reasons))


class QualityModel(Protocol):
    version: str

    def score(self, features: dict[str, float]) -> float: ...


class HeuristicQualityModel:
    """Transparent confluence score in [0, 1]. Weights are documented, not fitted."""

    version = "heuristic-1"

    WEIGHTS = {
        "trend_strength": 0.25,
        "structure": 0.15,
        "context": 0.15,
        "momentum": 0.15,
        "cost": 0.15,
        "volatility": 0.10,
        "retest": 0.05,
    }

    def components(self, f: dict[str, float]) -> dict[str, float]:
        def clip(x: float) -> float:
            return float(min(1.0, max(0.0, x)))

        return {
            "trend_strength": clip((f.get("h1_adx", 0) - 15) / 20) * 0.6 + clip(f.get("h1_slope_atr", 0) / 1.0) * 0.4,
            "structure": f.get("h1_structure_aligned", 0.0),
            "context": f.get("h4_aligned", 0.5),
            # RSI just back on the trend side of 50, not stretched.
            "momentum": clip(1 - abs(f.get("m15_rsi_centered", 0) - 7) / 20),
            # Spread as a share of the stop: 0 -> 1.0, >= 20% -> 0.
            "cost": clip(1 - f.get("spread_risk_ratio", 1.0) / 0.2),
            "volatility": clip(1 - max(0.0, f.get("m15_atr_rank", 0.5) - 0.6) / 0.35),
            "retest": f.get("retest", 0.0),
        }

    def score(self, features: dict[str, float]) -> float:
        comps = self.components(features)
        return float(sum(self.WEIGHTS[k] * v for k, v in comps.items()))


@dataclass
class LogisticQualityModel:
    """L2-regularised logistic regression on standardised features (numpy only)."""

    feature_names: list[str]
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    mean: list[float] = field(default_factory=list)
    std: list[float] = field(default_factory=list)
    version: str = "logreg-1"
    trained_from: str | None = None
    trained_to: str | None = None
    n_samples: int = 0

    def _matrix(self, rows: list[dict[str, float]]) -> np.ndarray:
        return np.array([[r.get(n, 0.0) for n in self.feature_names] for r in rows], dtype=float)

    def fit(self, rows: list[dict[str, float]], labels: list[int], *, l2: float = 1.0, lr: float = 0.1, epochs: int = 500) -> "LogisticQualityModel":
        x = self._matrix(rows)
        y = np.asarray(labels, dtype=float)
        mean, std = x.mean(axis=0), x.std(axis=0)
        std[std == 0] = 1.0
        z = (x - mean) / std
        w = np.zeros(z.shape[1])
        b = 0.0
        n = len(y)
        for _ in range(epochs):
            p = 1 / (1 + np.exp(-(z @ w + b)))
            grad_w = z.T @ (p - y) / n + l2 * w / n
            grad_b = float((p - y).mean())
            w -= lr * grad_w
            b -= lr * grad_b
        self.weights, self.bias = w.tolist(), b
        self.mean, self.std = mean.tolist(), std.tolist()
        self.n_samples = n
        return self

    def score(self, features: dict[str, float]) -> float:
        if not self.weights:
            raise RuntimeError("model not trained")
        x = (np.array([features.get(n, 0.0) for n in self.feature_names]) - np.array(self.mean)) / np.array(self.std)
        return float(1 / (1 + np.exp(-(x @ np.array(self.weights) + self.bias))))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "LogisticQualityModel":
        return cls(**json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class PerformanceStats:
    """Validated results of a strategy on a symbol (from backtest / demo)."""

    source: str  # BACKTEST_OOS | DEMO
    trades: int
    expectancy_r: float
    profit_factor: float
    validated: bool


@dataclass(frozen=True)
class AIDecision:
    approve: bool
    score: float
    threshold: float
    regime: str
    regime_confidence: float
    model_version: str
    reasons_for: tuple[str, ...]
    reasons_against: tuple[str, ...]
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


class AIDecisionEngine:
    def __init__(
        self,
        model: QualityModel | None = None,
        *,
        min_quality: float = 0.55,
        allowed_regimes: tuple[str, ...] = (Regime.TREND,),
        performance: dict[tuple[str, str], PerformanceStats] | None = None,
    ):
        self.model = model or HeuristicQualityModel()
        self.min_quality = min_quality
        self.allowed_regimes = allowed_regimes
        self.performance = performance or {}

    def evaluate(self, signal: Signal, snapshot: MarketSnapshot) -> AIDecision:
        regime = classify_regime(snapshot)
        score = self.model.score(signal.features)
        reasons_for: list[str] = list(signal.reasons)
        against: list[str] = []
        if isinstance(self.model, HeuristicQualityModel):
            for name, value in self.model.components(signal.features).items():
                (reasons_for if value >= 0.5 else against).append(f"quality.{name}={value:.2f}")
        if regime.regime not in self.allowed_regimes:
            against.append(f"regime {regime.regime} not in {self.allowed_regimes}: {', '.join(regime.reasons)}")
        else:
            reasons_for.append(f"regime {regime.regime}: {', '.join(regime.reasons)}")
        if score < self.min_quality:
            against.append(f"quality score {score:.2f} < threshold {self.min_quality:.2f}")
        stats = self.performance.get((signal.strategy_id, signal.symbol))
        if stats is not None and stats.trades >= 30 and stats.expectancy_r <= 0:
            against.append(f"{stats.source} expectancy {stats.expectancy_r:+.2f}R over {stats.trades} trades on {signal.symbol}")
        approve = (
            score >= self.min_quality
            and regime.regime in self.allowed_regimes
            and not (stats is not None and stats.trades >= 30 and stats.expectancy_r <= 0)
        )
        return AIDecision(
            approve=approve, score=round(score, 4), threshold=self.min_quality, regime=regime.regime,
            regime_confidence=round(regime.confidence, 3), model_version=self.model.version,
            reasons_for=tuple(reasons_for), reasons_against=tuple(against),
        )


def brier_score(predictions: list[float], outcomes: list[int]) -> float | None:
    """Calibration of stored predictions against realised outcomes (lower is better)."""
    if not predictions:
        return None
    p = np.asarray(predictions, float)
    o = np.asarray(outcomes, float)
    return float(((p - o) ** 2).mean())
