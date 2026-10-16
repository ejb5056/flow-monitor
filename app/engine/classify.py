"""Turns raw option trades into tape rows: which side of the market they hit, and what kind of order they were.

Side comes from comparing the trade price with the bid/ask on that contract at the moment of the trade.
Kind comes from grouping prints on one contract that land within a few milliseconds of each other:
  SWEEP  the group carries an intermarket-sweep (ISO) condition, or filled on 2+ exchanges
  SPLIT  2+ prints on one exchange at the same price (one order filled in pieces)
  BLOCK  a single print at or above BLOCK_MIN_SIZE contracts
  REG    everything else
Multi-leg (spreads, straddles, combos, stock-tied) comes from the trade's condition code.
"""
from dataclasses import dataclass, field
from itertools import count

from app import config
from app.models import Contract, Print, Quote, Trade, dte, parse_occ

# OPRA sale-condition codes as Alpaca reports them. Replaced at startup by codes derived from
# Alpaca's own condition table (see load_condition_names) so a change on their side doesn't break us.
SWEEP_CODES: set[str] = {"S", "b", "d"}  # ISOI, single-leg auction ISO, single-leg cross ISO
MULTI_CODES: set[str] = set("LMPQ") | set("fghijklmnopqrstuv")  # spreads/straddles/buy-writes/combos, multi-leg and stock-option-tied
CANCEL_CODES: set[str] = {"A", "C", "E", "G"}  # cancels / corrections: ignored


def load_condition_names(names: dict[str, str]) -> None:
    """Rebuild the code sets from a {code: description} map (Alpaca /v1beta1/options/meta/conditions/trade)."""
    sweep, multi, cancel = set(), set(), set()
    for code, desc in names.items():
        d = desc.lower()
        if "cancel" in d or "cncl" in d or "correct" in d:
            cancel.add(code)
        if "iso" in d or "sweep" in d:
            sweep.add(code)
        if any(w in d for w in ("multi", "spread", "straddle", "combo", "buy write", "buy-write", "stock option", "tied")):
            multi.add(code)
    if sweep:
        SWEEP_CODES.clear(); SWEEP_CODES.update(sweep)
    if multi:
        MULTI_CODES.clear(); MULTI_CODES.update(multi)
    if cancel:
        CANCEL_CODES.clear(); CANCEL_CODES.update(cancel)


def side_of(price: float, q: Quote | None, now_ts: float) -> str:
    if q is None or q.ask <= 0 or q.bid < 0 or q.ask < q.bid:
        return "NA"
    if now_ts - q.ts > config.QUOTE_MAX_AGE_S:
        return "NA"
    spread = q.ask - q.bid
    if spread <= 0:
        return "MID" if abs(price - q.ask) < 1e-9 else ("ASK" if price > q.ask else "BID")
    pos = (price - q.bid) / spread
    if pos >= config.SIDE_ASK_AT:
        return "ASK"
    if pos <= config.SIDE_BID_AT:
        return "BID"
    return "MID"


def bull_of(cp: str, side: str) -> bool | None:
    """Buying calls or selling puts reads bullish; buying puts or selling calls reads bearish."""
    if side == "ASK":
        return cp == "C"
    if side == "BID":
        return cp == "P"
    return None


@dataclass
class _Group:
    contract: Contract
    first_ts: float
    last_ts: float
    arrived: float
    trades: list = field(default_factory=list)  # (Trade, side)


class Grouper:
    """Collects prints per contract for GROUP_MS, then emits one Print per group."""

    def __init__(self):
        self._open: dict[str, _Group] = {}
        self._done: list[Print] = []
        self._ids = count(1)

    def add(self, t: Trade, q: Quote | None, arrival_ts: float) -> None:
        if t.condition in CANCEL_CODES or t.size <= 0 or t.price <= 0:
            return
        c = parse_occ(t.symbol)
        if c is None:
            return
        side = side_of(t.price, q, t.ts)
        g = self._open.get(t.symbol)
        if g is not None and t.ts - g.first_ts > config.GROUP_MS / 1000.0:
            self._done.append(self._finish(self._open.pop(t.symbol)))
            g = None
        if g is None:
            g = self._open[t.symbol] = _Group(c, t.ts, t.ts, arrival_ts)
        g.trades.append((t, side))
        g.last_ts = max(g.last_ts, t.ts)

    def flush(self, now_ts: float, force: bool = False) -> list[Print]:
        """Emit groups whose window has closed. `now_ts` is wall-clock arrival time; groups are aged by
        arrival so a feed whose exchange timestamps lag our clock still flushes promptly."""
        out, self._done = self._done, []
        horizon = config.GROUP_MS / 1000.0
        for sym in list(self._open):
            g = self._open[sym]
            if force or now_ts - g.arrived >= horizon + 0.05:
                del self._open[sym]
                out.append(self._finish(g))
        out.sort(key=lambda p: p.ts)
        return out

    def _finish(self, g: _Group) -> Print:
        trades = [t for t, _ in g.trades]
        size = sum(t.size for t in trades)
        notional = sum(t.price * t.size for t in trades)
        vwap = notional / size
        exchanges = {t.exchange for t in trades if t.exchange}
        conds = {t.condition for t in trades}
        multi = bool(conds & MULTI_CODES)

        # Side of the group = side carrying the most premium.
        by_side: dict[str, float] = {}
        for t, s in g.trades:
            by_side[s] = by_side.get(s, 0.0) + t.price * t.size
        side = max(by_side, key=by_side.get)

        if not multi and (conds & SWEEP_CODES or len(exchanges) >= 2):
            kind = "SWEEP"
        elif len(trades) >= 2 and len({round(t.price, 4) for t in trades}) == 1:
            kind = "SPLIT"
        elif size >= config.BLOCK_MIN_SIZE and len(trades) == 1:
            kind = "BLOCK"
        else:
            kind = "REG"

        c = g.contract
        return Print(
            id=next(self._ids), ts=g.first_ts, underlying=c.underlying, symbol=c.symbol,
            strike=c.strike, cp=c.cp, expiry=c.expiry.isoformat(), dte=dte(c.expiry),
            size=size, price=vwap, premium=notional * 100.0, side=side, kind=kind,
            multi=multi, bull=None if multi else bull_of(c.cp, side), exchanges=max(1, len(exchanges)),
        )
