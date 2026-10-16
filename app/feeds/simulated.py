"""A fake market for building and testing the app without a data subscription.
Every number it produces is invented; the UI shows SIMULATED while it runs."""
import asyncio
import math
import random
import time
from datetime import date, timedelta

from app.engine.gex import bs_gamma
from app.models import ChainRow, Quote, Trade, today_et

START = {"SPY": 668.4, "QQQ": 604.1, "IWM": 247.8, "NVDA": 188.2, "TSLA": 431.5, "AAPL": 249.3, "AMD": 214.7,
         "META": 712.4, "AMZN": 221.6, "MSFT": 512.8, "PLTR": 181.3, "SMCI": 49.2, "COIN": 342.1}


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _occ(root, exp: date, cp, k):
    return f"{root}{exp:%y%m%d}{cp}{int(round(k * 1000)):08d}"


def _expiries(n=8):
    d = today_et()
    out = [d]
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() == 4 or (len(out) < 3 and d.weekday() < 5):
            out.append(d)
    return out


class SimFeed:
    name = "sim"

    def __init__(self, hub):
        self.hub = hub
        self.rng = random.Random(7)
        self.px: dict[str, float] = {}
        self.chains: dict[str, list[ChainRow]] = {}
        self.bias: dict[str, float] = {}

    def _step_k(self, s):
        return 1 if s < 100 else 2.5 if s < 300 else 5

    def _build_chain(self, sym):
        s = self.px[sym]
        step = self._step_k(s)
        atm = round(s / step) * step
        rows = []
        for exp in _expiries():
            t = max((exp - today_et()).days, 0.3) / 365
            for i in range(-12, 13):
                k = atm + i * step
                for cp in "CP":
                    iv = 0.32 + 0.0015 * abs(i) ** 1.5 + (0.03 if cp == "P" and i < 0 else 0)
                    vs = iv * math.sqrt(t)
                    d1 = (math.log(s / k) + 0.5 * iv * iv * t) / vs
                    delta = _ncdf(d1) if cp == "C" else _ncdf(d1) - 1
                    oi = int(self.rng.lognormvariate(7.2, 1.0) * math.exp(-abs(i) / 7))
                    if cp == "C" and i in (3, 6):
                        oi *= 4
                    if cp == "P" and i in (-4, -8):
                        oi *= 4
                    rows.append(ChainRow(_occ(sym, exp, cp, k), k, cp, exp, oi, iv, bs_gamma(s, k, t, iv), delta))
        self.chains[sym] = rows

    async def on_watchlist(self, symbols):
        for s in symbols:
            if s not in self.px:
                self.px[s] = START.get(s, self.rng.uniform(40, 400))
                self.bias[s] = self.rng.uniform(-0.6, 0.6)
                self._build_chain(s)
            st = self.hub.t.get(s)
            if st is not None and not st.chain:
                self.hub.on_spot(s, self.px[s], prev_close=self.px[s] * self.rng.uniform(0.98, 1.02))
                await self.hub.set_chain(s, self.chains[s], self.px[s])

    async def run(self):
        await self.on_watchlist(self.hub.watchlist)
        self.hub.status.update(connected=True, msg="simulated market")
        exch = list("ABCHIJMNPQTWXZ")
        while True:
            now = time.time()
            for sym in list(self.hub.watchlist):
                if sym not in self.px:
                    continue
                s = self.px[sym] = self.px[sym] * math.exp(self.rng.gauss(0.00002 * self.bias[sym], 0.0006))
                self.hub.on_spot(sym, s, now)
                if self.rng.random() < 0.01:
                    self.bias[sym] = max(-0.9, min(0.9, self.bias[sym] + self.rng.gauss(0, 0.3)))
                n = self.rng.choices([0, 1, 2, 3], [55, 30, 10, 5])[0]
                rows = self.chains[sym]
                for _ in range(n):
                    near = [r for r in rows if abs(r.strike - s) / s < 0.06]
                    r = self.rng.choice(near or rows)
                    t = max((r.expiry - today_et()).days, 0.3) / 365
                    intr = max(0.0, s - r.strike if r.cp == "C" else r.strike - s)
                    fair = max(0.05, intr + s * r.iv * math.sqrt(t) * 0.4 * math.exp(-abs(r.strike - s) / (s * r.iv * math.sqrt(t) + 1e-9)))
                    spr = max(0.01, round(fair * 0.04, 2))
                    bid, ask = round(fair - spr / 2, 2), round(fair + spr / 2, 2)
                    self.hub.on_quote(Quote(r.symbol, now, bid, ask, 10, 10))
                    want_bull = self.rng.random() < 0.5 + self.bias[sym] / 2
                    buy = (r.cp == "C") == want_bull
                    u = self.rng.random()
                    px = ask if (buy and u < 0.8) else bid if (not buy and u < 0.8) else round((bid + ask) / 2, 2)
                    size = self.rng.choices([1, 2, 5, 10, 20, 50, 100, 200, 500], [18, 16, 16, 15, 12, 10, 7, 4, 2])[0]
                    kind = self.rng.random()
                    if kind < 0.12:  # sweep across exchanges with ISO flag
                        for j in range(self.rng.randint(2, 5)):
                            self.hub.on_trade(Trade(r.symbol, now + j * 0.002, px, max(1, size // 3), exch[j], "S"))
                    elif kind < 0.18:  # split fill on one exchange
                        for j in range(self.rng.randint(2, 4)):
                            self.hub.on_trade(Trade(r.symbol, now + j * 0.003, px, max(1, size // 2), "C", "I"))
                    elif kind < 0.26:  # multi-leg
                        self.hub.on_trade(Trade(r.symbol, now, px, size, "C", "f"))
                    else:
                        self.hub.on_trade(Trade(r.symbol, now, px, size, self.rng.choice(exch), "I"))
            await asyncio.sleep(0.25)
