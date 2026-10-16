"""Dealer gamma exposure and the levels derived from it.

Convention (the common retail one, an assumption about dealer positioning, not a fact):
dealers are long the calls customers sold them and short the puts customers bought,
so call open interest adds positive gamma and put open interest adds negative gamma.

GEX at a strike = gamma * open interest * 100 * spot^2 * 1%  -> dollars of hedging per 1% move.
"""
import math
from datetime import date, datetime

from app import config
from app.models import ET, ChainRow

_SQ2PI = math.sqrt(2 * math.pi)


def bs_gamma(spot: float, strike: float, t_years: float, iv: float, r: float = config.RISK_FREE) -> float:
    if spot <= 0 or strike <= 0 or iv <= 0:
        return 0.0
    t = max(t_years, 1.0 / (365 * 24))  # floor at one hour so 0DTE doesn't divide by zero
    vs = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / vs
    return math.exp(-0.5 * d1 * d1) / (_SQ2PI * spot * vs)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, iv: float, cp: str, r: float = config.RISK_FREE) -> float:
    t = max(t_years, 1.0 / (365 * 24))
    vs = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / vs
    d2 = d1 - vs
    if cp == "C":
        return spot * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot: float, strike: float, t_years: float, iv: float, cp: str, r: float = config.RISK_FREE) -> float:
    t = max(t_years, 1.0 / (365 * 24))
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    return _ncdf(d1) if cp == "C" else _ncdf(d1) - 1.0


def implied_vol(price: float, spot: float, strike: float, t_years: float, cp: str) -> float | None:
    """Bisection on Black-Scholes. None when the price is below intrinsic or above any sane vol."""
    if price <= 0 or spot <= 0 or strike <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if cp == "C" else max(0.0, strike - spot)
    if price <= intrinsic + 1e-4:
        return None
    lo, hi = 0.01, 5.0
    if bs_price(spot, strike, t_years, hi, cp) < price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t_years, mid, cp) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def years_to(expiry: date, now: datetime | None = None) -> float:
    return _t_years(expiry, now or datetime.now(ET))


def _t_years(expiry: date, now: datetime) -> float:
    close = datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ET)
    return max((close - now).total_seconds(), 0.0) / (365.0 * 86400)


def _gex_at(price: float, rows: list[ChainRow], now: datetime) -> float:
    total = 0.0
    for r in rows:
        if not r.oi or not r.iv:
            continue
        g = bs_gamma(price, r.strike, _t_years(r.expiry, now), r.iv)
        sign = 1.0 if r.cp == "C" else -1.0
        total += sign * g * r.oi * 100.0 * price * price * 0.01
    return total


def compute_levels(spot: float, rows: list[ChainRow], now: datetime | None = None, with_flip: bool = True) -> dict:
    now = now or datetime.now(ET)
    today = now.date()
    rows = [r for r in rows if 0 <= (r.expiry - today).days <= config.GEX_MAX_DTE and r.oi]
    if spot <= 0 or not rows:
        return {"spot": spot, "call_wall": None, "put_wall": None, "flip": None, "max_pain": None,
                "net_gex": None, "regime": 0, "by_strike": []}

    call_g: dict[float, float] = {}
    put_g: dict[float, float] = {}
    for r in rows:
        g = r.gamma if r.gamma is not None and r.iv is None else bs_gamma(spot, r.strike, _t_years(r.expiry, now), r.iv or 0.0)
        val = g * r.oi * 100.0 * spot * spot * 0.01
        (call_g if r.cp == "C" else put_g)[r.strike] = (call_g if r.cp == "C" else put_g).get(r.strike, 0.0) + val

    above = {k: v for k, v in call_g.items() if k >= spot} or call_g
    below = {k: v for k, v in put_g.items() if k <= spot} or put_g
    call_wall = max(above, key=above.get) if above else None
    put_wall = max(below, key=below.get) if below else None
    net = sum(call_g.values()) - sum(put_g.values())

    # Gamma flip: price nearest spot where total GEX, recomputed at that price, crosses zero.
    flip = None
    steps = [] if not with_flip else [spot * (1 + i * 0.0025) for i in range(-80, 81)]
    vals = [_gex_at(p, rows, now) for p in steps]
    best = None
    for i in range(len(steps) - 1):
        a, b = vals[i], vals[i + 1]
        if a == 0 or (a < 0) != (b < 0):
            x = steps[i] + (steps[i + 1] - steps[i]) * (abs(a) / (abs(a) + abs(b))) if a != b else steps[i]
            if best is None or abs(x - spot) < abs(best - spot):
                best = x
    flip = best

    # Max pain on the nearest expiry: strike where option holders collectively lose the most.
    first_exp = min(r.expiry for r in rows)
    near = [r for r in rows if r.expiry == first_exp]
    strikes = sorted({r.strike for r in near})
    max_pain = None
    if strikes:
        def payout(k):
            return sum(r.oi * (max(0.0, k - r.strike) if r.cp == "C" else max(0.0, r.strike - k)) for r in near)
        max_pain = min(strikes, key=payout)

    by_strike = sorted(
        ({"k": k, "call": round(call_g.get(k, 0.0)), "put": round(-put_g.get(k, 0.0))}
         for k in set(call_g) | set(put_g)), key=lambda d: d["k"])

    return {"spot": spot, "call_wall": call_wall, "put_wall": put_wall, "flip": flip, "max_pain": max_pain,
            "net_gex": net, "regime": 1 if net > 0 else -1, "by_strike": by_strike}
