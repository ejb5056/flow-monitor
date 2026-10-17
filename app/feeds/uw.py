"""Live data from the Unusual Whales REST API (FEED=uw).

On the Basic API plan there is no websocket, so this polls:
  GET /api/option-trades?ticker_symbol=A,B,...&newer_than=<ms>   every UW_POLL_S   -> tape
  GET /api/stock/{t}/gex-levels                                   every UW_LEVELS_S -> call/put wall, flip
  GET /api/stock/{t}/greek-exposure?timeframe=1D                  once a day        -> gamma regime (net sign)
  GET /api/stock/{t}/max-pain                                     every UW_LEVELS_S -> nearest-expiry max pain
  GET /api/stock/{t}/stock-state                                  every 15 s        -> spot when no recent print

Each UW trade carries the NBBO at execution, so ask/bid side is classified here with the same rule as the Alpaca
feed rather than taken from UW's tags. UW's own call/put wall and flip are used as-is (their definitions: walls =
largest positive net gamma above/below spot). Field names follow UW's published OpenAPI docs; anything the live
API does differently shows up in /api/status under `uw`.
"""
import asyncio
import logging
import time
from collections import Counter, OrderedDict
from datetime import datetime

import httpx

from app import config
from app.models import ET, ChainRow, Quote, Trade, parse_occ, today_et

log = logging.getLogger("uw")

# OPRA sale-condition names as UW passes them through in `upstream_condition_detail` (lower-cased match).
ISO_DETAILS = {"isoi", "slai", "slci", "iso", "intermarket_sweep"}
MULTI_DETAILS = {"mlet", "mlat", "mlct", "mlft", "mesl", "tlat", "tlet", "tlct", "tlft", "tesl", "tasl",
                 "sprd", "stdl", "bwrt", "cmbo", "multi_leg", "spread"}


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class UWFeed:
    name = "uw"
    provides_levels = True

    def __init__(self, hub):
        self.hub = hub
        self.http = httpx.AsyncClient(base_url=config.UW_BASE_URL, timeout=15.0,
                                      headers={"Authorization": f"Bearer {config.UW_API_TOKEN}", "Accept": "application/json"})
        self.cursor_ms: int | None = None
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.stats = {"requests": 0, "errors": 0, "last_error": "", "trades": 0, "last_poll": 0.0,
                      "conditions": Counter(), "rows_per_poll": 0, "order": "unknown"}
        self._gex_day: dict[str, str] = {}
        self._wake = asyncio.Event()
        hub.status["uw"] = self.stats

    # ---- http ---------------------------------------------------------------------------------
    async def get(self, path: str, params: dict | None = None):
        self.stats["requests"] += 1
        r = await self.http.get(path, params=params)
        if r.status_code >= 400:
            self.stats["errors"] += 1
            body = r.text[:300]
            self.stats["last_error"] = f"{r.status_code} {path}: {body}"
            raise RuntimeError(self.stats["last_error"])
        return r.json()

    # ---- tape ---------------------------------------------------------------------------------
    def _remember(self, tid: str) -> bool:
        if tid in self.seen:
            return False
        self.seen[tid] = None
        if len(self.seen) > 200_000:
            for _ in range(50_000):
                self.seen.popitem(last=False)
        return True

    def ingest(self, row: dict) -> float | None:
        """Turn one UW trade into quote + trade (+ chain row) events for the hub. Returns its timestamp."""
        tid = str(row.get("id") or f"{row.get('option_chain_id')}|{row.get('executed_at')}|{row.get('size')}|{row.get('price')}")
        ts = _ts(row.get("executed_at"))
        if ts is None or not self._remember(tid) or row.get("canceled"):
            return ts
        osym = row.get("option_chain_id") or ""
        c = parse_occ(osym)
        if c is None:
            return ts
        st = self.hub.t.get(c.underlying)
        if st is None:
            return ts
        price, size = _f(row.get("price"), 0.0), int(_f(row.get("size"), 0) or 0)
        detail = str(row.get("upstream_condition_detail") or "").strip().lower()
        self.stats["conditions"][detail or "(none)"] += 1
        tags = {str(t).lower() for t in (row.get("tags") or [])}
        if detail in MULTI_DETAILS:
            cond = "f"
        elif detail in ISO_DETAILS or "sweep" in tags:
            cond = "S"
        else:
            cond = "I"

        # Contract snapshot carried on the trade: OI, IV, greeks, today's volume.
        oi = int(_f(row.get("open_interest"), 0) or 0)
        vol = int(_f(row.get("volume"), 0) or 0)
        prev = st.chain.get(osym)
        pick = lambda new, old: new if new is not None else old  # keep the last known value when a row omits it
        st.chain[osym] = ChainRow(osym, c.strike, c.cp, c.expiry, oi or (prev.oi if prev else 0),
                                  pick(_f(row.get("implied_volatility")), prev.iv if prev else None),
                                  pick(_f(row.get("gamma")), prev.gamma if prev else None),
                                  pick(_f(row.get("delta")), prev.delta if prev else None), vol)
        if vol:
            st.day_vol[osym] = max(st.day_vol.get(osym, 0), vol - size)

        und = _f(row.get("underlying_price"))
        if und and ts >= st.spot_ts:
            self.hub.on_spot(c.underlying, und, ts)

        bid, ask = _f(row.get("nbbo_bid")), _f(row.get("nbbo_ask"))
        if bid is not None and ask:
            qts = max(_ts(row.get("nbbo_bid_time")) or ts, _ts(row.get("nbbo_ask_time")) or ts)
            self.hub.on_quote(Quote(osym, min(qts, ts), bid, ask, int(_f(row.get("nbbo_bid_size"), 0) or 0),
                                    int(_f(row.get("nbbo_ask_size"), 0) or 0)))
        self.hub.on_trade(Trade(osym, ts, price, size, str(row.get("exchange") or ""), cond))
        self.stats["trades"] += 1
        return ts

    async def poll_once(self):
        syms = [s for s in self.hub.tracked() if s in self.hub.t]
        if not syms:
            return
        if self.cursor_ms is None:
            # Start of today's session (pre-market included) so a restart refills the tape.
            start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0).timestamp()
            self.cursor_ms = int(start * 1000)
        newest = self.cursor_ms
        params = {"ticker_symbol": ",".join(syms), "newer_than": self.cursor_ms, "limit": 500}
        rows_total = 0
        batch: list[dict] = []
        for _ in range(config.UW_MAX_PAGES):
            data = (await self.get("/api/option-trades", params)).get("data") or []
            rows_total += len(data)
            batch.extend(data)
            stamps = [t for t in (_ts(r.get("executed_at")) for r in data) if t is not None]
            if stamps:
                newest = max(newest, int(max(stamps) * 1000))
            if len(data) < 500 or not stamps:
                break
            # Full page: walk further. Newest-first -> page backwards; oldest-first -> page forwards.
            if stamps[0] >= stamps[-1]:
                self.stats["order"] = "newest-first"
                params = {**params, "older_than": int(min(stamps) * 1000)}
            else:
                self.stats["order"] = "oldest-first"
                params = {**params, "newer_than": int(max(stamps) * 1000)}
                params.pop("older_than", None)
        for r in sorted(batch, key=lambda r: _ts(r.get("executed_at")) or 0):
            self.ingest(r)
        self.cursor_ms = newest
        self.stats["rows_per_poll"] = rows_total
        self.stats["last_poll"] = time.time()

    async def tape_loop(self):
        while True:
            t0 = time.time()
            try:
                await self.poll_once()
                self.hub.status.update(connected=True, msg="live (Unusual Whales, polling)")
            except Exception as e:
                self.hub.status.update(connected=False, msg=f"UW: {e}"[:200])
                log.warning("tape poll: %s", e)
            await asyncio.sleep(max(0.5, config.UW_POLL_S - (time.time() - t0)))

    # ---- levels -------------------------------------------------------------------------------
    async def refresh_levels(self, sym: str):
        st = self.hub.t.get(sym)
        if st is None:
            return
        lv = dict(st.levels or {})
        d = (await self.get(f"/api/stock/{sym}/gex-levels")).get("data")
        g = d if isinstance(d, dict) else {}
        lv.update(call_wall=_f(g.get("call_wall")), put_wall=_f(g.get("put_wall")), flip=_f(g.get("gamma_flip")),
                  magnet=_f(g.get("gamma_magnet")))
        today = today_et().isoformat()
        if self._gex_day.get(sym) != today:
            rows = (await self.get(f"/api/stock/{sym}/greek-exposure", {"timeframe": "1D"})).get("data") or []
            if rows:
                latest = max(rows, key=lambda r: str(r.get("date")))
                net = (_f(latest.get("call_gamma"), 0) or 0) + (_f(latest.get("put_gamma"), 0) or 0)
                lv.update(net_gex=net, regime=1 if net > 0 else -1)
                self._gex_day[sym] = today
        mp = (await self.get(f"/api/stock/{sym}/max-pain")).get("data") or []
        future = sorted((r for r in mp if str(r.get("expiry", ""))[:10] >= today), key=lambda r: str(r.get("expiry")))
        if future:
            lv["max_pain"] = _f(future[0].get("max_pain"))
        lv["spot"] = st.spot
        st.levels = lv
        st.levels_ts = time.time()

    async def levels_loop(self):
        while True:
            for sym in self.hub.tracked():
                try:
                    await self.refresh_levels(sym)
                except Exception as e:
                    log.warning("levels %s: %s", sym, e)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=config.UW_LEVELS_S)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def spot_loop(self):
        while True:
            for sym in self.hub.tracked():
                st = self.hub.t.get(sym)
                if st is None or time.time() - st.spot_ts < 20:
                    continue
                try:
                    d = (await self.get(f"/api/stock/{sym}/stock-state")).get("data") or {}
                    px = _f(d.get("close"))
                    if px:
                        self.hub.on_spot(sym, px, _ts(d.get("tape_time")) or time.time(), _f(d.get("prev_close")))
                except Exception as e:
                    log.warning("spot %s: %s", sym, e)
            await asyncio.sleep(15)

    async def on_watchlist(self, symbols):
        self._wake.set()  # fetch levels for new names now rather than at the next minute

    async def run(self):
        if not config.UW_API_TOKEN:
            self.hub.status.update(connected=False, msg="UW_API_TOKEN is not set")
            return
        await asyncio.gather(self.tape_loop(), self.levels_loop(), self.spot_loop())
