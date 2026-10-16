"""Live data from Alpaca.

  Options stream  wss://stream.data.alpaca.markets/v1beta1/{opra|indicative}   msgpack, trades + quotes
  Stock stream    wss://stream.data.alpaca.markets/v2/{sip|iex}                JSON, underlying trades for spot
  Chain snapshot  GET /v1beta1/options/snapshots/{underlying}                  greeks, IV, latest quote, day volume
  Contracts       GET /v2/options/contracts (trading API)                      open interest
  Conditions      GET /v1beta1/options/meta/conditions/trade                   sale-condition names

Written against Alpaca's published docs; the probe (FEED=probe) checks the parts the docs leave open.
"""
import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timedelta

import httpx
import msgpack
import websockets

from app import config
from app.engine import classify
from app.engine import gex
from app.models import ET, ChainRow, Quote, Trade, parse_occ, root_of, today_et

log = logging.getLogger("alpaca")


def _hdr():
    return {"APCA-API-KEY-ID": config.ALPACA_KEY_ID, "APCA-API-SECRET-KEY": config.ALPACA_SECRET}


def _ts(v) -> float:
    if v is None:
        return time.time()
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, msgpack.Timestamp):
        return v.to_unix()
    if isinstance(v, datetime):
        return v.timestamp()
    s = str(v)
    if "." in s:  # trim nanoseconds to microseconds for fromisoformat
        head, frac = s.split(".", 1)
        tz = ""
        for sep in ("Z", "+", "-"):
            if sep in frac:
                i = frac.index(sep)
                frac, tz = frac[:i], frac[i:]
                break
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class AlpacaREST:
    def __init__(self):
        self.http = httpx.AsyncClient(headers=_hdr(), timeout=20.0)

    async def get(self, url, params=None):
        r = await self.http.get(url, params=params)
        r.raise_for_status()
        return r.json()

    async def conditions(self) -> dict[str, str]:
        return await self.get(f"{config.ALPACA_DATA_URL}/v1beta1/options/meta/conditions/trade")

    async def snapshots(self, underlying: str, max_dte: int) -> dict:
        out, token = {}, None
        exp_lte = (today_et() + timedelta(days=max_dte)).isoformat()
        sym = underlying if underlying != "SPX" else "SPXW"
        while True:
            params = {"feed": config.ALPACA_OPTIONS_FEED, "limit": 1000, "expiration_date_lte": exp_lte}
            if token:
                params["page_token"] = token
            d = await self.get(f"{config.ALPACA_DATA_URL}/v1beta1/options/snapshots/{sym}", params)
            out.update(d.get("snapshots") or {})
            token = d.get("next_page_token")
            if not token:
                return out

    async def open_interest(self, underlying: str, max_dte: int) -> dict[str, int]:
        out, token = {}, None
        sym = underlying if underlying != "SPX" else "SPXW"
        while True:
            params = {"underlying_symbols": sym, "expiration_date_gte": today_et().isoformat(),
                      "expiration_date_lte": (today_et() + timedelta(days=max_dte)).isoformat(), "limit": 10000}
            if token:
                params["page_token"] = token
            d = await self.get(f"{config.ALPACA_TRADING_URL}/v2/options/contracts", params)
            for c in d.get("option_contracts") or []:
                try:
                    out[c["symbol"]] = int(float(c.get("open_interest") or 0))
                except (TypeError, ValueError):
                    pass
            token = d.get("next_page_token")
            if not token:
                return out

    async def stock_snapshots(self, symbols: list[str]) -> dict:
        syms = [s for s in symbols if s not in ("SPX", "NDX", "RUT", "VIX")]
        if not syms:
            return {}
        return await self.get(f"{config.ALPACA_DATA_URL}/v2/stocks/snapshots",
                              {"symbols": ",".join(syms), "feed": config.ALPACA_STOCK_FEED})


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def parity_spot(rows: list[ChainRow], snaps: dict) -> float | None:
    """Spot implied by put-call parity on the nearest expiry, for indexes the stock feed doesn't carry."""
    if not rows:
        return None
    exp = min(r.expiry for r in rows)
    calls = {r.strike: r for r in rows if r.expiry == exp and r.cp == "C"}
    puts = {r.strike: r for r in rows if r.expiry == exp and r.cp == "P"}
    ests = []
    for k in set(calls) & set(puts):
        qc = (snaps.get(calls[k].symbol) or {}).get("latestQuote") or {}
        qp = (snaps.get(puts[k].symbol) or {}).get("latestQuote") or {}
        if qc.get("ap") and qc.get("bp") and qp.get("ap") and qp.get("bp"):
            c = (qc["ap"] + qc["bp"]) / 2
            p = (qp["ap"] + qp["bp"]) / 2
            ests.append((abs(c - p), c - p + k))
    if not ests:
        return None
    ests.sort()
    best = [e for _, e in ests[:3]]
    return sum(best) / len(best)


class AlpacaFeed:
    name = "alpaca"

    def __init__(self, hub):
        self.hub = hub
        self.rest = AlpacaREST()
        self.ows = None
        self.quote_subs: set[str] = set()
        self.trade_subs: set[str] = set()
        self._stock_ws = None
        self._resub = asyncio.Event()
        self.trade_cap = config.TRADE_SUB_MAX  # lowered automatically if Alpaca reports a symbol limit

    # ---- chain + levels -----------------------------------------------------------------------
    async def refresh_chain(self, sym: str):
        snaps, oi = await asyncio.gather(self.rest.snapshots(sym, config.GEX_MAX_DTE),
                                         self.rest.open_interest(sym, config.GEX_MAX_DTE))
        st = self.hub.t.get(sym)
        spot = st.spot if st and st.spot else None
        if spot is None and sym not in ("SPX", "NDX", "RUT"):
            try:
                ss = (await self.rest.stock_snapshots([sym])).get(sym) or {}
                spot = ((ss.get("latestTrade") or {}).get("p")) or ((ss.get("dailyBar") or {}).get("c"))
                if spot:
                    self.hub.on_spot(sym, spot, prev_close=((ss.get("prevDailyBar") or {}).get("c")))
            except Exception as e:
                log.warning("spot %s: %s", sym, e)
        rows = []
        derived = 0
        for osym, s in snaps.items():
            c = parse_occ(osym)
            if c is None:
                continue
            g = s.get("greeks") or {}
            day = s.get("dailyBar") or {}
            iv, gamma, delta = s.get("impliedVolatility"), g.get("gamma"), g.get("delta")
            if iv is None and spot:
                # The feed sent no greeks (the free indicative feed doesn't): solve IV from the quote mid ourselves.
                q = s.get("latestQuote") or {}
                bid, ask = q.get("bp") or 0.0, q.get("ap") or 0.0
                if ask > 0 and bid >= 0 and ask >= bid:
                    t = gex.years_to(c.expiry)
                    iv = gex.implied_vol((bid + ask) / 2, spot, c.strike, t, c.cp)
                    if iv:
                        gamma = gex.bs_gamma(spot, c.strike, t, iv)
                        delta = gex.bs_delta(spot, c.strike, t, iv, c.cp)
                        derived += 1
            rows.append(ChainRow(osym, c.strike, c.cp, c.expiry, oi.get(osym, 0), iv, gamma, delta, int(day.get("v") or 0)))
            q = s.get("latestQuote")
            if q and q.get("ap"):
                self.hub.on_quote(Quote(osym, _ts(q.get("t")), q.get("bp") or 0.0, q["ap"], q.get("bs") or 0, q.get("as") or 0))
        if sym in ("SPX", "NDX", "RUT"):
            spot = parity_spot(rows, snaps)
            if spot:
                self.hub.on_spot(sym, spot)
        await self.hub.set_chain(sym, rows, spot)
        log.info("chain %s: %d contracts, %d with OI, %d greeks solved from quotes", sym, len(rows),
                 sum(1 for r in rows if r.oi), derived)

    async def chain_loop(self):
        while True:
            for sym in self.hub.tracked():
                try:
                    await self.refresh_chain(sym)
                except Exception as e:
                    log.warning("chain %s failed: %s", sym, e)
                    self.hub.status["msg"] = f"chain {sym}: {e}"
            self._resub.set()
            await asyncio.sleep(config.CHAIN_REFRESH_S)

    async def spot_loop(self):
        """REST spot every few seconds as a backstop to the stock stream (and the only source for prev close)."""
        while True:
            try:
                snaps = await self.rest.stock_snapshots(self.hub.tracked())
                for sym, s in (snaps or {}).items():
                    lt = (s or {}).get("latestTrade") or {}
                    pc = ((s or {}).get("prevDailyBar") or {}).get("c")
                    st = self.hub.t.get(sym)
                    if lt.get("p") and st and time.time() - st.spot_ts > 5:
                        self.hub.on_spot(sym, lt["p"], _ts(lt.get("t")), pc)
                    elif st and pc:
                        st.prev_close = pc
            except Exception as e:
                log.warning("stock snapshots: %s", e)
            await asyncio.sleep(10)

    # ---- quote selection ----------------------------------------------------------------------
    def wanted_quotes(self) -> set[str]:
        names = [s for s in self.hub.tracked() if s in self.hub.t and self.hub.t[s].chain]
        if not names:
            return set()
        per = max(10, config.QUOTE_BUDGET // len(names))
        want = set()
        today = today_et()
        for s in names:
            st = self.hub.t[s]
            spot = st.spot or 0
            if spot <= 0:
                continue
            ranked = sorted(st.chain.values(), key=lambda r: abs(r.strike - spot) / spot + 0.0015 * max(0, (r.expiry - today).days))
            want.update(r.symbol for r in ranked[:per])
        return set(list(want)[: config.QUOTE_BUDGET])

    def wanted_trades(self) -> set[str]:
        """Per-contract trade subscriptions: every contract within TRADE_MAX_DTE days and TRADE_MONEYNESS of
        spot on each tracked name, nearest-the-money first, capped at TRADE_SUB_MAX overall."""
        names = [s for s in self.hub.tracked() if s in self.hub.t and self.hub.t[s].chain]
        if not names:
            return set()
        today = today_et()
        per = max(50, config.TRADE_SUB_MAX // len(names))
        want = set()
        for s in names:
            st = self.hub.t[s]
            spot = st.spot or 0
            if spot <= 0:
                continue
            rows = [r for r in st.chain.values() if (r.expiry - today).days <= config.TRADE_MAX_DTE
                    and abs(r.strike - spot) / spot <= config.TRADE_MONEYNESS]
            rows.sort(key=lambda r: abs(r.strike - spot) / spot + 0.001 * max(0, (r.expiry - today).days))
            want.update(r.symbol for r in rows[:per])
        return want

    # ---- options stream -----------------------------------------------------------------------
    async def _osend(self, msg: dict):
        await self.ows.send(msgpack.packb(msg))

    async def _resubscribe(self):
        want_q = self.wanted_quotes()
        add_q, drop_q = want_q - self.quote_subs, self.quote_subs - want_q
        if config.TRADES_SUBSCRIPTION == "wildcard":
            want_t = {"*"}
        else:
            want_t = set(sorted(self.wanted_trades())[: self.trade_cap])
        add_t, drop_t = want_t - self.trade_subs, self.trade_subs - want_t
        for chunk in _chunks(sorted(drop_q), 500):
            await self._osend({"action": "unsubscribe", "quotes": chunk})
        for chunk in _chunks(sorted(drop_t), 500):
            await self._osend({"action": "unsubscribe", "trades": chunk})
        for chunk in _chunks(sorted(add_q), 500):
            await self._osend({"action": "subscribe", "quotes": chunk})
        for chunk in _chunks(sorted(add_t), 500):
            await self._osend({"action": "subscribe", "trades": chunk})
        self.quote_subs, self.trade_subs = want_q, want_t

    async def options_stream(self):
        url = f"{config.ALPACA_STREAM_URL}/v1beta1/{config.ALPACA_OPTIONS_FEED}"
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, max_size=None, ping_interval=20,
                                              additional_headers={"Content-Type": "application/msgpack"}) as ws:
                    self.ows = ws
                    self.quote_subs, self.trade_subs = set(), set()
                    await self._osend({"action": "auth", "key": config.ALPACA_KEY_ID, "secret": config.ALPACA_SECRET})
                    self._resub.set()
                    backoff = 1
                    resub_task = asyncio.create_task(self._resub_loop())
                    try:
                        async for raw in ws:
                            msgs = msgpack.unpackb(raw, raw=False, timestamp=1) if isinstance(raw, bytes) else json.loads(raw)
                            for m in msgs if isinstance(msgs, list) else [msgs]:
                                self._handle_option(m)
                    finally:
                        resub_task.cancel()
            except Exception as e:
                self.hub.status.update(connected=False, msg=f"options stream: {e}")
                log.warning("options stream dropped: %s (retry in %ss)", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _resub_loop(self):
        while True:
            await self._resub.wait()
            self._resub.clear()
            if self.hub.status.get("authed"):
                try:
                    await self._resubscribe()
                except Exception as e:
                    log.warning("resubscribe: %s", e)
            else:
                await asyncio.sleep(0.5)
                self._resub.set()

    def _handle_option(self, m: dict):
        T = m.get("T")
        if T == "t":
            sym = m.get("S", "")
            if root_of(sym) not in self.hub.t:
                return
            self.hub.on_trade(Trade(sym, _ts(m.get("t")), float(m.get("p") or 0), int(m.get("s") or 0),
                                    str(m.get("x") or ""), str(m.get("c") or "")))
        elif T == "q":
            self.hub.on_quote(Quote(m.get("S", ""), _ts(m.get("t")), float(m.get("bp") or 0), float(m.get("ap") or 0),
                                    int(m.get("bs") or 0), int(m.get("as") or 0)))
        elif T == "success":
            if m.get("msg") == "authenticated":
                self.hub.status.update(connected=True, authed=True, msg="live")
        elif T == "subscription":
            log.info("subscribed: %d trades, %d quotes", len(m.get("trades") or []), len(m.get("quotes") or []))
        elif T == "error":
            self.hub.status["msg"] = f"alpaca error {m.get('code')}: {m.get('msg')}"
            log.error("alpaca error %s", m)
            if m.get("code") == 405 and self.trade_subs and "*" not in self.trade_subs:
                # Symbol limit: shrink the trade set by a fifth and try again.
                self.trade_cap = max(200, int(len(self.trade_subs) * 0.8))
                log.warning("symbol limit hit; trade subscriptions capped at %d", self.trade_cap)
                self._resub.set()

    # ---- stock stream for spot ----------------------------------------------------------------
    async def stock_stream(self):
        url = f"{config.ALPACA_STREAM_URL}/v2/{config.ALPACA_STOCK_FEED}"
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._stock_ws = ws
                    await ws.send(json.dumps({"action": "auth", "key": config.ALPACA_KEY_ID, "secret": config.ALPACA_SECRET}))
                    await ws.send(json.dumps({"action": "subscribe", "trades": self.hub.tracked()}))
                    backoff = 1
                    async for raw in ws:
                        for m in json.loads(raw):
                            if m.get("T") == "t":
                                self.hub.on_spot(m["S"], float(m["p"]), _ts(m.get("t")))
                            elif m.get("T") == "error":
                                log.error("stock stream error %s", m)
            except Exception as e:
                log.warning("stock stream dropped: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def on_watchlist(self, symbols):
        for s in symbols:
            if s in self.hub.t and not self.hub.t[s].chain:
                try:
                    await self.refresh_chain(s)
                except Exception as e:
                    log.warning("chain %s: %s", s, e)
        if self._stock_ws:
            try:
                await self._stock_ws.send(json.dumps({"action": "subscribe", "trades": symbols}))
            except Exception:
                pass
        self._resub.set()

    async def run(self):
        try:
            classify.load_condition_names(await self.rest.conditions())
            log.info("conditions: sweep=%s multi=%s", sorted(classify.SWEEP_CODES), sorted(classify.MULTI_CODES))
        except Exception as e:
            log.warning("condition table unavailable, using defaults: %s", e)
        await asyncio.gather(self.chain_loop(), self.spot_loop(), self.options_stream(), self.stock_stream())
