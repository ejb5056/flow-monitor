import time

import msgpack

from app.feeds.alpaca import AlpacaFeed, _ts, parity_spot
from app.models import ChainRow
from datetime import date


class FakeHub:
    def __init__(self):
        self.t = {"NVDA": object()}
        self.trades, self.quotes = [], []
        self.status = {}

    def on_trade(self, t): self.trades.append(t)
    def on_quote(self, q): self.quotes.append(q)


def test_ts_formats():
    assert abs(_ts("2026-10-16T14:30:00.123456789Z") - 1792161000.123456) < 1e-5
    assert abs(_ts(msgpack.Timestamp(1792161000, 5)) - 1792161000.000000005) < 1e-6
    assert _ts(1792161000.5) == 1792161000.5


def test_handle_option_messages_roundtrip_through_msgpack():
    hub = FakeHub()
    f = AlpacaFeed.__new__(AlpacaFeed)
    f.hub = hub
    raw = msgpack.packb([
        {"T": "t", "S": "NVDA261023C00190000", "t": msgpack.Timestamp(1792161000, 0), "p": 2.5, "s": 10, "x": "C", "c": "S"},
        {"T": "t", "S": "AAPL261023C00250000", "t": msgpack.Timestamp(1792161000, 0), "p": 1.0, "s": 1, "x": "C", "c": "I"},
        {"T": "q", "S": "NVDA261023C00190000", "t": msgpack.Timestamp(1792161000, 0), "bp": 2.4, "ap": 2.5, "bs": 5, "as": 7, "bx": "C", "ax": "C"},
        {"T": "success", "msg": "authenticated"},
    ])
    for m in msgpack.unpackb(raw, raw=False, timestamp=1):
        f._handle_option(m)
    assert len(hub.trades) == 1 and hub.trades[0].condition == "S" and hub.trades[0].ts == 1792161000.0
    assert hub.quotes[0].ask == 2.5 and hub.status["authed"]


def test_parity_spot():
    e = date(2026, 10, 16)
    rows = [ChainRow("C100", 100, "C", e, 1, .2, None, None), ChainRow("P100", 100, "P", e, 1, .2, None, None)]
    snaps = {"C100": {"latestQuote": {"bp": 5.0, "ap": 5.2}}, "P100": {"latestQuote": {"bp": 2.0, "ap": 2.2}}}
    assert abs(parity_spot(rows, snaps) - 103.0) < 1e-9
