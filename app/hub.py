"""Central state: receives quotes/trades/spot from a feed, builds tape rows, computes levels and the
composite, and pushes everything to connected windows."""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime

from app import config
from app.engine import composite as comp
from app.engine.classify import Grouper
from app.engine.gex import compute_levels
from app.models import ET, ChainRow, Print, Quote, Trade, root_of, today_et
from app.store import Store, bucket_of

log = logging.getLogger("hub")
TAPE_MAX = 3000
SNAPSHOT_PRINTS = 1500


class TickerState:
    def __init__(self, sym: str):
        self.sym = sym
        self.spot = 0.0
        self.spot_ts = 0.0
        self.prev_close: float | None = None
        self.chain: dict[str, ChainRow] = {}
        self.levels: dict = {}
        self.day_vol: dict[str, int] = {}
        self.tape: deque[Print] = deque(maxlen=TAPE_MAX)
        self.window: deque[Print] = deque()
        self.result = comp.compute([], 0.0, None)
        self.spark: deque[tuple[float, int]] = deque(maxlen=400)
        self.levels_ts = 0.0

    def summary(self) -> dict:
        lv = self.levels or {}
        r = self.result
        return {
            "sym": self.sym, "spot": self.spot, "prev_close": self.prev_close,
            "score": r["score"], "verdict": comp.verdict(r["score"]), "calibrated": r["calibrated"],
            "n": r["n_directional"], "gross": r["gross"],
            "comps": r["comps"],
            "levels": {k: lv.get(k) for k in ("call_wall", "put_wall", "flip", "max_pain", "regime", "net_gex")},
            "chain_size": len(self.chain),
        }


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.view = "strip"
        self.ticker: str | None = None
        self.q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def send(self, msg: dict):
        try:
            self.q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # slow client: drop rather than stall everyone

    async def writer(self):
        while True:
            msg = await self.q.get()
            await self.ws.send_text(json.dumps(msg, separators=(",", ":")))


class Hub:
    def __init__(self, store: Store):
        self.store = store
        self.feed = None
        self.watchlist: list[str] = store.get("watchlist", config.DEFAULT_WATCHLIST)
        self.t: dict[str, TickerState] = {s: TickerState(s) for s in self.watchlist}
        self.quotes: dict[str, Quote] = {}
        self.grouper = Grouper()
        self.clients: set[Client] = set()
        self.baselines = store.baselines()
        self._pending: list[Print] = []
        self.status = {"feed": config.FEED, "connected": False, "msg": "starting", "trades_seen": 0, "last_trade_ts": 0}
        self._restore()

    # ---- startup ------------------------------------------------------------------------------
    def _restore(self):
        """Reload today's tape and composite history so a redeploy mid-session loses nothing."""
        start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0).timestamp()
        for s, st in self.t.items():
            for p in self.store.recent_prints(s, start, TAPE_MAX):
                st.tape.append(p)
                st.window.append(p)
                st.day_vol[p.symbol] = st.day_vol.get(p.symbol, 0) + p.size
            for ts, score in self.store.spark(s, time.time() - 6.5 * 3600):
                st.spark.append((ts, score))

    # ---- feed callbacks -----------------------------------------------------------------------
    def on_quote(self, q: Quote):
        self.quotes[q.symbol] = q

    def on_trade(self, t: Trade):
        if root_of(t.symbol) not in self.t:
            return
        self.status["trades_seen"] += 1
        self.status["last_trade_ts"] = t.ts
        self.grouper.add(t, self.quotes.get(t.symbol), time.time())

    def on_spot(self, sym: str, px: float, ts: float | None = None, prev_close: float | None = None):
        st = self.t.get(sym)
        if st is None or px <= 0:
            return
        st.spot = px
        st.spot_ts = ts or time.time()
        if prev_close:
            st.prev_close = prev_close

    async def set_chain(self, sym: str, rows: list[ChainRow], spot: float | None = None):
        st = self.t.get(sym)
        if st is None:
            return
        st.chain = {r.symbol: r for r in rows}
        for r in rows:  # seed today's volume from the snapshot if we joined late
            if r.day_volume and r.day_volume > st.day_vol.get(r.symbol, 0):
                st.day_vol[r.symbol] = r.day_volume
        if spot and not st.spot:
            st.spot = spot
        await self.recompute_levels(sym)

    async def recompute_levels(self, sym: str, with_flip: bool = True):
        """Walls, max pain and regime at the live spot every few seconds; the flip (a price scan) once a minute."""
        st = self.t.get(sym)
        if st is None or not st.chain or st.spot <= 0:
            return
        rows = list(st.chain.values())
        lv = await asyncio.to_thread(compute_levels, st.spot, rows, None, with_flip)
        if not with_flip and st.levels:
            lv["flip"] = st.levels.get("flip")
        st.levels = lv
        st.levels_ts = time.time()

    # ---- watchlist ----------------------------------------------------------------------------
    async def set_watchlist(self, symbols: list[str]):
        clean = []
        for s in symbols:
            s = s.strip().upper()
            if s and s.replace(".", "").isalnum() and s not in clean:
                clean.append(s)
        clean = clean[: config.MAX_WATCHLIST]
        for s in clean:
            if s not in self.t:
                self.t[s] = TickerState(s)
        for s in list(self.t):
            if s not in clean:
                del self.t[s]
        self.watchlist = clean
        self.store.put("watchlist", clean)
        self._restore_missing()
        if self.feed:
            await self.feed.on_watchlist(clean)
        self.broadcast({"t": "watchlist", "symbols": clean})

    def _restore_missing(self):
        start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0).timestamp()
        for s, st in self.t.items():
            if not st.tape:
                for p in self.store.recent_prints(s, start, TAPE_MAX):
                    st.tape.append(p); st.window.append(p)

    # ---- clients ------------------------------------------------------------------------------
    def broadcast(self, msg: dict, ticker: str | None = None):
        for c in list(self.clients):
            if ticker is None or (c.view == "panel" and c.ticker == ticker):
                c.send(msg)

    def snapshot_for(self, ticker: str) -> dict:
        st = self.t.get(ticker)
        if st is None:
            return {"t": "snapshot", "sym": ticker, "missing": True, "watchlist": self.watchlist}
        prints = list(st.tape)[-SNAPSHOT_PRINTS:]
        return {"t": "snapshot", "sym": ticker, "summary": st.summary(),
                "prints": [p.to_wire() for p in prints],
                "spark": [[ts, s] for ts, s in st.spark if ts >= time.time() - 90 * 60]}

    def summaries(self) -> dict:
        return {"t": "summaries", "ts": time.time(), "status": self.status, "watchlist": self.watchlist,
                "data": [self.t[s].summary() for s in self.watchlist if s in self.t]}

    # ---- main loop ----------------------------------------------------------------------------
    def _accept(self, p: Print):
        st = self.t.get(p.underlying)
        if st is None:
            return
        row = st.chain.get(p.symbol)
        if row is not None:
            p.delta = row.delta
            vol = st.day_vol.get(p.symbol, 0) + p.size
            st.day_vol[p.symbol] = vol
            p.opening = bool(row.oi) and vol > row.oi
        else:
            st.day_vol[p.symbol] = st.day_vol.get(p.symbol, 0) + p.size
        st.tape.append(p)
        st.window.append(p)
        self._pending.append(p)
        self.broadcast({"t": "print", "p": p.to_wire()}, ticker=p.underlying)

    def _recompute(self, now: float):
        cutoff = now - config.WINDOW_MIN * 60
        for st in self.t.values():
            while st.window and st.window[0].ts < cutoff:
                st.window.popleft()
            bl = self.baselines.get((st.sym, bucket_of(now)))
            st.result = comp.compute(list(st.window), st.spot, bl)

    async def run(self):
        last_sec = last_min = last_levels = last_walls = 0.0
        while True:
            now = time.time()
            for p in self.grouper.flush(now):
                self._accept(p)
            if now - last_sec >= 1.0:
                last_sec = now
                self._recompute(now)
                if self._pending:
                    batch, self._pending = self._pending, []
                    try:
                        self.store.add_prints(batch)
                    except Exception:
                        log.exception("store prints")
                self.broadcast(self.summaries())
            if now - last_min >= 60.0:
                last_min = now
                rows = []
                for st in self.t.values():
                    st.spark.append((now, st.result["score"]))
                    rows.append((st.sym, now, st.result["score"]))
                    self.broadcast({"t": "spark", "sym": st.sym, "ts": now, "score": st.result["score"]}, ticker=st.sym)
                self.store.add_spark(rows)
                self._daily_jobs()
            if now - last_levels >= 60.0 or now - last_walls >= 5.0:
                flip = now - last_levels >= 60.0
                last_walls = now
                if flip:
                    last_levels = now
                for s in list(self.t):
                    try:
                        await self.recompute_levels(s, with_flip=flip)
                    except Exception:
                        log.exception("levels %s", s)
            await asyncio.sleep(0.05)

    def _daily_jobs(self):
        dt = datetime.now(ET)
        if dt.weekday() >= 5 or (dt.hour, dt.minute) < (16, 20):
            return
        day = dt.date().isoformat()
        if self.store.get("last_roll") == day:
            return
        self.store.roll_buckets(day)
        self.store.prune()
        self.store.put("last_roll", day)
        self.baselines = self.store.baselines()
        log.info("rolled baselines for %s", day)
