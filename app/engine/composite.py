"""The bullish/bearish composite, -100 (all bearish) to +100 (all bullish), over a rolling window.

Four components, each scaled to -1..+1, combined with weights from config:

  premium   Net directional premium (bullish minus bearish) in the window. Once a name has
            BASELINE_MIN_DAYS of history, scored as a z-score against that name's own history for the same
            time of day, so a quiet name with a normal-for-NVDA print doesn't light up. Before then it is the
            simple imbalance net / gross ("calibrating").
  sweep     Bullish share of sweep premium (urgency).
  delta     Net customer delta in dollars (calls bought / puts sold add, puts bought / calls sold subtract),
            scaled the same way as premium.
  opening   Imbalance of premium in prints on contracts whose volume today already exceeds open interest
            (more likely new positions than closes).

Prints with unknown side, mid prints and multi-leg prints carry no direction and are excluded.
"""
import math
from dataclasses import dataclass

from app import config
from app.models import Print


@dataclass
class Baseline:
    """Per name, per time-of-day bucket: typical absolute net premium over a window."""
    net_std: float | None = None
    days: int = 0


def _imbalance(bull: float, bear: float) -> float:
    tot = bull + bear
    return 0.0 if tot <= 0 else (bull - bear) / tot


def compute(prints: list[Print], spot: float, baseline: Baseline | None) -> dict:
    bull_p = bear_p = 0.0
    sw_bull = sw_bear = 0.0
    op_bull = op_bear = 0.0
    net_delta = 0.0
    gross = 0.0
    n = 0
    for p in prints:
        gross += p.premium
        if p.bull is None:
            continue
        n += 1
        if p.bull:
            bull_p += p.premium
        else:
            bear_p += p.premium
        if p.kind == "SWEEP":
            if p.bull:
                sw_bull += p.premium
            else:
                sw_bear += p.premium
        if p.opening:
            if p.bull:
                op_bull += p.premium
            else:
                op_bear += p.premium
        if p.delta is not None and spot > 0:
            sign = 1.0 if p.side == "ASK" else -1.0
            net_delta += sign * p.delta * p.size * 100.0 * spot

    net = bull_p - bear_p
    calibrated = baseline is not None and baseline.days >= config.BASELINE_MIN_DAYS and baseline.net_std
    if calibrated:
        z = net / baseline.net_std
        s_prem = math.tanh(z / 2.0)
        s_delta = math.tanh(net_delta / (2.0 * baseline.net_std * 4.0))  # delta $ runs ~4x premium on average
    else:
        z = None
        s_prem = _imbalance(bull_p, bear_p)
        denom = max(gross * 4.0, 1.0)
        s_delta = max(-1.0, min(1.0, net_delta / denom))
    s_sweep = _imbalance(sw_bull, sw_bear)
    s_open = _imbalance(op_bull, op_bear)

    w = [config.W_PREMIUM, config.W_SWEEP, config.W_DELTA, config.W_OPENING]
    s = [s_prem, s_sweep, s_delta, s_open]
    score = 100.0 * sum(a * b for a, b in zip(w, s)) / max(sum(w), 1e-9)
    if n == 0:
        score = 0.0

    return {
        "score": round(score),
        "calibrated": bool(calibrated),
        "n_directional": n,
        "gross": gross,
        "comps": {
            "premium": {"s": s_prem, "net": net, "z": z},
            "sweep": {"s": s_sweep, "bull_share": None if sw_bull + sw_bear == 0 else sw_bull / (sw_bull + sw_bear)},
            "delta": {"s": s_delta, "usd": net_delta},
            "opening": {"s": s_open, "bull_share": None if op_bull + op_bear == 0 else op_bull / (op_bull + op_bear)},
        },
    }


def verdict(score: int) -> str:
    a = abs(score)
    if a < 15:
        return "NEUTRAL"
    if a < 40:
        return "LEANING BULL" if score > 0 else "LEANING BEAR"
    return "BULLISH" if score > 0 else "BEARISH"
