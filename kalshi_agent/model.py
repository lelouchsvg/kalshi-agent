"""Features and the baseline probability model (Phase 3).

The question each 15-minute market asks is "will the settlement price be above the
strike at close?". The model combines two things:

  1. A volatility-scaled distance to the strike, z = ln(S/K) / (sigma * sqrt(tau)).
     If prices moved randomly with recent volatility sigma, P(above) = Phi(z).
  2. What the market itself is pricing (the bid/ask midpoint), because the crowd is
     usually right and the model should only disagree with good reason.

plus short-term momentum and time left. A logistic regression learns how much to
trust each, and is fitted on past settled markets only. Everything is plain Python
(no numpy) so it installs on any Mac.

The model never sees the future: features come from data that had arrived by the
decision time (see timeseries.py), and labels are the markets' official results.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

FEATURES = ["z", "market_logit", "momentum_5m", "time_left"]
MIN_SIGMA_PER_SEC = 1e-6


def logit(p: float, eps: float = 1e-4) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


def phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def sigma_per_sec(closes: list[float]) -> float | None:
    """Volatility per sqrt(second) from consecutive one-minute closes."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return max(math.sqrt(var / 60.0), MIN_SIGMA_PER_SEC)


@dataclass
class FeatureRow:
    values: dict[str, float]
    sigma: float
    spot: float
    strike: float
    tau_s: float
    market_mid: float

    def vector(self) -> list[float]:
        return [self.values[f] for f in FEATURES]


def compute_features(*, spot: float, strike: float, tau_s: float, closes_1m: list[float],
                     yes_bid: float | None, yes_ask: float | None,
                     window_s: float = 900.0) -> FeatureRow | None:
    """Features for "settles above strike". Returns None when inputs are unusable
    (missing prices, crossed or very wide market, no volatility history)."""
    if not spot or not strike or spot <= 0 or strike <= 0 or tau_s <= 0:
        return None
    if yes_bid is None or yes_ask is None or yes_bid <= 0 or yes_ask >= 1 or yes_ask < yes_bid:
        return None
    if yes_ask - yes_bid > 0.25:
        return None
    sigma = sigma_per_sec(closes_1m[-31:])
    if sigma is None:
        return None
    z = max(-8.0, min(8.0, math.log(spot / strike) / (sigma * math.sqrt(tau_s))))
    mid = (yes_bid + yes_ask) / 2
    mom = 0.0
    if len(closes_1m) >= 6 and closes_1m[-6] > 0:
        mom = max(-8.0, min(8.0, math.log(spot / closes_1m[-6]) / (sigma * math.sqrt(300))))
    return FeatureRow({"z": z, "market_logit": logit(mid), "momentum_5m": mom,
                       "time_left": min(tau_s / window_s, 1.5)}, sigma, spot, strike, tau_s, mid)


# ---------------------------------------------------------------- logistic regression
@dataclass
class LogisticModel:
    weights: list[float] = field(default_factory=list)   # intercept first
    features: list[str] = field(default_factory=lambda: list(FEATURES))
    l2: float = 1.0

    def predict(self, x: list[float]) -> float:
        return sigmoid(self.weights[0] + sum(w * v for w, v in zip(self.weights[1:], x)))

    def fit(self, X: list[list[float]], y: list[int], iters: int = 25) -> "LogisticModel":
        """Newton-Raphson (IRLS) with a small L2 penalty; a handful of features, so
        solving the small linear system directly is fast in plain Python."""
        k = len(X[0]) + 1
        w = [0.0] * k
        for _ in range(iters):
            grad = [0.0] * k
            hess = [[0.0] * k for _ in range(k)]
            for xi, yi in zip(X, y):
                row = [1.0] + xi
                p = sigmoid(sum(a * b for a, b in zip(w, row)))
                g = p - yi
                s = p * (1 - p)
                for a in range(k):
                    grad[a] += g * row[a]
                    ra = s * row[a]
                    for b in range(a, k):
                        hess[a][b] += ra * row[b]
            for a in range(k):
                for b in range(a):
                    hess[a][b] = hess[b][a]
                if a:   # don't penalise the intercept
                    grad[a] += self.l2 * w[a]
                    hess[a][a] += self.l2
            step = _solve(hess, grad)
            w = [wi - si for wi, si in zip(w, step)]
            if max(abs(s) for s in step) < 1e-7:
                break
        self.weights = w
        return self

    def to_dict(self) -> dict:
        return {"weights": self.weights, "features": self.features, "l2": self.l2}

    @classmethod
    def from_dict(cls, d: dict) -> "LogisticModel":
        return cls(list(d["weights"]), list(d["features"]), float(d.get("l2", 1.0)))


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            M[c][c] = 1e-12
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][n] / M[i][i] for i in range(n)]


# ---------------------------------------------------------------- metrics
def brier(p: list[float], y: list[int]) -> float | None:
    return sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / len(y) if y else None


def log_loss(p: list[float], y: list[int], eps: float = 1e-6) -> float | None:
    if not y:
        return None
    return -sum(yi * math.log(max(pi, eps)) + (1 - yi) * math.log(max(1 - pi, eps))
                for pi, yi in zip(p, y)) / len(y)


def calibration(p: list[float], y: list[int], bins: int = 10) -> dict:
    """Reliability table plus expected calibration error (ECE)."""
    rows = []
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, pi in enumerate(p) if lo <= pi < hi or (b == bins - 1 and pi == 1.0)]
        if not idx:
            continue
        mp = sum(p[i] for i in idx) / len(idx)
        fy = sum(y[i] for i in idx) / len(idx)
        ece += len(idx) / len(p) * abs(mp - fy)
        rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx), "predicted": mp, "actual": fy})
    return {"ece": ece if p else None, "bins": rows}
