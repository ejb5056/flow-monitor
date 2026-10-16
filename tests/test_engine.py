import time
from datetime import date, datetime, timedelta

from app.engine import classify, composite
from app.engine.gex import bs_gamma, compute_levels
from app.models import ET, ChainRow, Print, Quote, Trade, parse_occ, root_of

NOW = time.time()


def q(sym, bid, ask, ts=NOW):
    return Quote(sym, ts, bid, ask)


def test_parse_occ_and_roots():
    c = parse_occ("NVDA251017C00185000")
    assert (c.underlying, c.cp, c.strike, c.expiry) == ("NVDA", "C", 185.0, date(2025, 10, 17))
    assert parse_occ("SPXW251017P06650000").underlying == "SPX"
    assert parse_occ("F251017C00012500").strike == 12.5
    assert root_of("SPXW251017P06650000") == "SPX"
    assert parse_occ("garbage") is None


def test_side_and_bull():
    s = "NVDA251017C00185000"
    assert classify.side_of(2.10, q(s, 2.00, 2.10), NOW) == "ASK"
    assert classify.side_of(2.00, q(s, 2.00, 2.10), NOW) == "BID"
    assert classify.side_of(2.05, q(s, 2.00, 2.10), NOW) == "MID"
    assert classify.side_of(2.05, None, NOW) == "NA"
    assert classify.side_of(2.10, q(s, 2.00, 2.10, ts=NOW - 60), NOW) == "NA"  # stale quote
    assert classify.bull_of("C", "ASK") is True and classify.bull_of("P", "ASK") is False
    assert classify.bull_of("P", "BID") is True and classify.bull_of("C", "MID") is None


def _group(trades, quote):
    g = classify.Grouper()
    for t in trades:
        g.add(t, quote, t.ts)
    return g.flush(NOW + 10)


def test_grouping_kinds():
    s = "NVDA251017C00185000"
    qt = q(s, 2.00, 2.10)
    sweep = _group([Trade(s, NOW, 2.10, 10, "A", "S"), Trade(s, NOW + 0.002, 2.10, 15, "C", "S")], qt)
    assert len(sweep) == 1 and sweep[0].kind == "SWEEP" and sweep[0].size == 25 and sweep[0].bull is True
    split = _group([Trade(s, NOW, 2.00, 50, "C", "I"), Trade(s, NOW + 0.003, 2.00, 50, "C", "I")], qt)
    assert split[0].kind == "SPLIT" and split[0].side == "BID" and split[0].bull is False
    block = _group([Trade(s, NOW, 2.10, 500, "C", "I")], qt)
    assert block[0].kind == "BLOCK" and abs(block[0].premium - 2.10 * 500 * 100) < 1e-6
    ml = _group([Trade(s, NOW, 2.10, 20, "C", "f")], qt)
    assert ml[0].multi and ml[0].bull is None
    two = _group([Trade(s, NOW, 2.10, 1, "C", "I"), Trade(s, NOW + 1, 2.10, 1, "C", "I")], qt)
    assert len(two) == 2  # a second apart = separate orders
    assert _group([Trade(s, NOW, 2.10, 5, "C", "A")], qt) == []  # cancel ignored


def test_condition_table_mapping():
    saved = (set(classify.SWEEP_CODES), set(classify.MULTI_CODES), set(classify.CANCEL_CODES))
    try:
        classify.load_condition_names({"S": "Intermarket Sweep Order", "f": "Multi Leg auto-electronic trade",
                                       "L": "Spread", "A": "Canceled", "I": "Auto Execution"})
        assert classify.SWEEP_CODES == {"S"} and classify.MULTI_CODES == {"f", "L"} and classify.CANCEL_CODES == {"A"}
    finally:
        for live, old in zip((classify.SWEEP_CODES, classify.MULTI_CODES, classify.CANCEL_CODES), saved):
            live.clear(); live.update(old)


def test_bs_gamma_peaks_at_the_money():
    atm = bs_gamma(100, 100, 30 / 365, 0.3)
    assert atm > bs_gamma(100, 110, 30 / 365, 0.3) > 0
    assert abs(atm - 0.0461) < 0.002  # textbook value for S=K=100, 30d, 30% vol


def test_levels():
    now = datetime(2026, 10, 16, 11, 0, tzinfo=ET)
    exp = date(2026, 10, 23)
    rows = []
    for k in range(80, 121, 5):
        rows.append(ChainRow(f"X{k}C", k, "C", exp, 5000 if k == 105 else 500, 0.3, None, 0.5))
        rows.append(ChainRow(f"X{k}P", k, "P", exp, 5000 if k == 95 else 500, 0.3, None, -0.5))
    lv = compute_levels(100.0, rows, now)
    assert lv["call_wall"] == 105 and lv["put_wall"] == 95
    assert lv["max_pain"] in (95, 100, 105)
    assert lv["flip"] is None or 80 < lv["flip"] < 120
    # Put-heavy book below spot -> negative gamma, positive flip above when calls dominate higher.
    heavy_puts = [ChainRow(r.symbol, r.strike, r.cp, r.expiry, r.oi * (10 if r.cp == "P" else 1), r.iv, None, r.delta) for r in rows]
    assert compute_levels(100.0, heavy_puts, now)["regime"] == -1


def _p(prem, bull, kind="REG", side=None, delta=None, opening=False):
    side = side or ("ASK" if bull else "BID")
    return Print(1, NOW, "X", "S", 100, "C", "2026-10-23", 7, 10, prem / 1000, prem, side, kind, False, bull, delta, 1, opening)


def test_composite_signs_and_bounds():
    bull = composite.compute([_p(1e5, True, "SWEEP", delta=0.5, opening=True)] * 5, 100.0, None)
    bear = composite.compute([_p(1e5, False, "SWEEP", side="ASK", delta=-0.5, opening=True)] * 5, 100.0, None)
    assert bull["score"] > 60 and bear["score"] < -60
    assert -100 <= bull["score"] <= 100
    assert composite.compute([], 100.0, None)["score"] == 0
    mixed = composite.compute([_p(1e5, True), _p(1e5, False)], 100.0, None)
    assert abs(mixed["score"]) < 15


def test_composite_uses_baseline_once_calibrated():
    prints = [_p(2e5, True)]
    raw = composite.compute(prints, 100.0, None)
    cal = composite.compute(prints, 100.0, composite.Baseline(net_std=2e6, days=10))
    assert raw["comps"]["premium"]["s"] == 1.0  # all-bull imbalance
    assert cal["calibrated"] and cal["comps"]["premium"]["s"] < 0.1  # small vs this name's normal


def test_store_baselines(tmp_path):
    from app.store import Store
    st = Store(str(tmp_path / "t.db"))
    base = datetime(2026, 10, 1, 10, 5, tzinfo=ET)
    for d in range(6):
        day = base + timedelta(days=d)
        st.add_prints([Print(0, day.timestamp(), "NVDA", "S", 100, "C", "2026-10-23", 7, 1, 1, 1e6 * (1 + d % 2), "ASK", "REG", False, True)])
        st.roll_buckets(day.date().isoformat())
    bl = st.baselines()
    from app.store import bucket_of
    b = bl[("NVDA", bucket_of(base.timestamp()))]
    assert b.days == 6 and 1e6 < b.net_std < 2e6


def test_implied_vol_roundtrip():
    from app.engine.gex import bs_price, implied_vol, bs_delta
    for cp in "CP":
        for k in (90, 100, 110):
            p = bs_price(100, k, 20 / 365, 0.35, cp)
            iv = implied_vol(p, 100, k, 20 / 365, cp)
            assert iv is not None and abs(iv - 0.35) < 1e-3
    assert implied_vol(0.5, 100, 90, 20 / 365, "C") is None  # below intrinsic
    assert 0.45 < bs_delta(100, 100, 30 / 365, 0.3, "C") < 0.56
    assert -0.56 < bs_delta(100, 100, 30 / 365, 0.3, "P") < -0.44
