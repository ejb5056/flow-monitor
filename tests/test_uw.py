import asyncio
import json
import time
from datetime import datetime, timezone

import httpx

from app.models import today_et


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def test_uw_feed_end_to_end(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "UW_API_TOKEN", "t")
    from app.store import Store
    from app.hub import Hub
    from app.feeds.uw import UWFeed

    hub = Hub(Store(str(tmp_path / "u.db")))
    feed = UWFeed(hub)
    hub.feed = feed
    now = time.time()
    exp = today_et().replace(day=min(28, today_et().day)).strftime("%y%m%d")
    sym = f"NVDA{exp}C00190000"
    trades = [
        # two-exchange ISO sweep at the ask -> one bullish SWEEP row
        {"id": "a", "option_chain_id": sym, "executed_at": _iso(now - 5), "price": "2.10", "size": 50, "exchange": "XCBO",
         "upstream_condition_detail": "isoi", "nbbo_bid": "2.00", "nbbo_ask": "2.10", "nbbo_bid_time": _iso(now - 5.1),
         "nbbo_ask_time": _iso(now - 5.1), "underlying_price": "188.2", "open_interest": 100, "volume": 400,
         "delta": "0.45", "gamma": "0.05", "implied_volatility": "0.4", "tags": ["ask_side"], "canceled": False},
        {"id": "b", "option_chain_id": sym, "executed_at": _iso(now - 5 + 0.002), "price": "2.10", "size": 30, "exchange": "XPHO",
         "upstream_condition_detail": "isoi", "nbbo_bid": "2.00", "nbbo_ask": "2.10", "underlying_price": "188.2",
         "open_interest": 100, "volume": 430, "delta": "0.45", "canceled": False},
        # multi-leg leg at the bid
        {"id": "c", "option_chain_id": sym, "executed_at": _iso(now - 3), "price": "2.00", "size": 10, "exchange": "XCBO",
         "upstream_condition_detail": "mlet", "nbbo_bid": "2.00", "nbbo_ask": "2.10", "underlying_price": "188.3",
         "open_interest": 100, "volume": 440, "canceled": False},
        # canceled: ignored
        {"id": "d", "option_chain_id": sym, "executed_at": _iso(now - 2), "price": "2.05", "size": 5, "canceled": True},
    ]

    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer t"
        p = req.url.path
        if p == "/api/option-trades":
            assert "NVDA" in req.url.params["ticker_symbol"]
            return httpx.Response(200, json={"data": list(reversed(trades))})
        if p.endswith("/gex-levels"):
            return httpx.Response(200, json={"data": {"call_wall": "195", "put_wall": "180", "gamma_flip": "184.5",
                                                      "gamma_magnet": "185", "nearby_flips": ["184.5"]}})
        if p.endswith("/greek-exposure"):
            return httpx.Response(200, json={"data": [{"date": "2026-10-16", "call_gamma": "900", "put_gamma": "-1200"}]})
        if p.endswith("/max-pain"):
            return httpx.Response(200, json={"data": [{"expiry": "2000-01-01", "max_pain": "1"},
                                                      {"expiry": "2099-01-01", "max_pain": "186"}]})
        return httpx.Response(404, text="nope")

    feed.http = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler),
                                  headers={"Authorization": "Bearer t"})

    async def go():
        await feed.poll_once()
        await feed.poll_once()  # same rows again: deduped
        await feed.refresh_levels("NVDA")

    asyncio.run(go())
    prints = hub.grouper.flush(time.time() + 10, force=True)
    for p in prints:
        hub._accept(p)
    tape = list(hub.t["NVDA"].tape)
    assert len(tape) == 2, [(p.kind, p.size) for p in tape]
    sweep, ml = tape
    assert sweep.kind == "SWEEP" and sweep.size == 80 and sweep.side == "ASK" and sweep.bull is True
    assert sweep.delta == 0.45 and sweep.opening is True  # volume 430 > OI 100
    assert ml.multi and ml.side == "BID" and ml.bull is None
    lv = hub.t["NVDA"].levels
    assert (lv["call_wall"], lv["put_wall"], lv["flip"], lv["magnet"], lv["max_pain"], lv["regime"]) == (195, 180, 184.5, 185, 186, -1)
    assert hub.t["NVDA"].spot == 188.3
    assert feed.stats["conditions"]["isoi"] == 2
