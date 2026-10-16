"""FEED=probe: answers the questions Alpaca's docs leave open before you pay for the $99 plan.

  1. Does the chain snapshot carry greeks + IV, and the contracts endpoint carry open interest?
  2. Is a wildcard ("*") trade subscription accepted, and how many option trades/second does it deliver?
  3. Is a wildcard quote subscription rejected (expected: error 413)?
  4. How many quote subscriptions are accepted before an error (the cap)?
  5. Do per-contract trade subscriptions count against that cap?

The report is logged and served at /api/probe.
"""
import asyncio
import logging
import time
from collections import Counter

import msgpack
import websockets

from app import config
from app.feeds.alpaca import AlpacaREST
from app.models import root_of

log = logging.getLogger("probe")
REPORT: dict = {"state": "not started"}


async def _recv_until(ws, kinds: set[str], timeout: float, sink: Counter | None = None):
    """Read messages until one of `kinds` arrives; count trades/quotes seen along the way."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.time()))
        except asyncio.TimeoutError:
            break
        msgs = msgpack.unpackb(raw, raw=False, timestamp=1) if isinstance(raw, bytes) else []
        for m in msgs:
            T = m.get("T")
            if sink is not None and T in ("t", "q"):
                sink[T] += 1
                if T == "t":
                    sink["root:" + root_of(m.get("S", ""))] += 1
            if T in kinds:
                return m
    return None


async def run_probe(hub=None):
    REPORT.clear()
    REPORT.update(state="running", started=time.time(), feed=config.ALPACA_OPTIONS_FEED)
    rest = AlpacaREST()
    sym = "SPY"
    try:
        snaps = await rest.snapshots(sym, 14)
        any_s = next(iter(snaps.values()), {}) if snaps else {}
        REPORT["chain_contracts"] = len(snaps)
        REPORT["snapshot_has_greeks"] = bool(any_s.get("greeks"))
        REPORT["snapshot_has_iv"] = any_s.get("impliedVolatility") is not None
        REPORT["snapshot_example_fields"] = sorted(any_s.keys())
    except Exception as e:
        snaps = {}
        REPORT["snapshot_error"] = str(e)
    try:
        oi = await rest.open_interest(sym, 14)
        REPORT["contracts_with_oi"] = sum(1 for v in oi.values() if v)
        REPORT["contracts_total"] = len(oi)
    except Exception as e:
        REPORT["open_interest_error"] = str(e)
    try:
        REPORT["condition_codes"] = await rest.conditions()
    except Exception as e:
        REPORT["conditions_error"] = str(e)

    symbols = sorted(snaps)
    url = f"{config.ALPACA_STREAM_URL}/v1beta1/{config.ALPACA_OPTIONS_FEED}"
    try:
        async with websockets.connect(url, max_size=None, additional_headers={"Content-Type": "application/msgpack"}) as ws:
            send = lambda m: ws.send(msgpack.packb(m))
            await _recv_until(ws, {"success"}, 5)
            await send({"action": "auth", "key": config.ALPACA_KEY_ID, "secret": config.ALPACA_SECRET})
            m = await _recv_until(ws, {"success", "error"}, 10)
            REPORT["auth"] = m
            if not m or m.get("T") == "error":
                raise RuntimeError(f"auth failed: {m}")

            # 2. wildcard trades
            seen = Counter()
            await send({"action": "subscribe", "trades": ["*"]})
            m = await _recv_until(ws, {"subscription", "error"}, 10, seen)
            REPORT["wildcard_trades_response"] = m
            t0 = time.time()
            await _recv_until(ws, set(), 30, seen)
            secs = time.time() - t0
            REPORT["wildcard_trades_per_sec"] = round(seen["t"] / max(secs, 1), 1)
            REPORT["wildcard_top_roots"] = [(k[5:], v) for k, v in seen.most_common() if k.startswith("root:")][:10]
            await send({"action": "unsubscribe", "trades": ["*"]})
            await _recv_until(ws, {"subscription", "error"}, 5)

            # 3. wildcard quotes
            await send({"action": "subscribe", "quotes": ["*"]})
            REPORT["wildcard_quotes_response"] = await _recv_until(ws, {"subscription", "error"}, 10)

            # 4. quote cap, in steps of 100
            accepted = 0
            cap_error = None
            for i in range(0, min(len(symbols), 1600), 100):
                chunk = symbols[i:i + 100]
                await send({"action": "subscribe", "quotes": chunk})
                m = await _recv_until(ws, {"subscription", "error"}, 10)
                if not m or m.get("T") == "error":
                    cap_error = m
                    break
                accepted = len(m.get("quotes") or [])
            REPORT["quote_subscriptions_accepted"] = accepted
            REPORT["quote_cap_error"] = cap_error
            REPORT["quote_test_ran_out_of_contracts"] = cap_error is None

            # 5. do per-contract trade subscriptions count against the same cap?
            await send({"action": "unsubscribe", "quotes": symbols[:1600]})
            await _recv_until(ws, {"subscription", "error"}, 5)
            t_acc, t_err = 0, None
            for i in range(0, min(len(symbols), 1600), 200):
                await send({"action": "subscribe", "trades": symbols[i:i + 200]})
                m = await _recv_until(ws, {"subscription", "error"}, 10)
                if not m or m.get("T") == "error":
                    t_err = m
                    break
                t_acc = len(m.get("trades") or [])
            REPORT["contract_trade_subscriptions_accepted"] = t_acc
            REPORT["contract_trade_cap_error"] = t_err
    except Exception as e:
        REPORT["stream_error"] = str(e)
    REPORT["state"] = "done"
    if hub is not None:
        hub.status["msg"] = "probe done, see /api/probe"
    REPORT["finished"] = time.time()
    log.info("PROBE REPORT %s", {k: v for k, v in REPORT.items() if k != "condition_codes"})
    return REPORT
