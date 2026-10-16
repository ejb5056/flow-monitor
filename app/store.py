"""SQLite storage: every tape row, the composite once a minute, per-name baselines and settings."""
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta

from app import config
from app.engine.composite import Baseline
from app.models import ET, Print

SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day TEXT, underlying TEXT, symbol TEXT, strike REAL, cp TEXT,
  expiry TEXT, dte INTEGER, size INTEGER, price REAL, premium REAL, side TEXT, kind TEXT, multi INTEGER,
  bull INTEGER, delta REAL, exchanges INTEGER, opening INTEGER);
CREATE INDEX IF NOT EXISTS ix_prints_u_ts ON prints(underlying, ts);
CREATE TABLE IF NOT EXISTS spark (underlying TEXT, ts REAL, score INTEGER);
CREATE INDEX IF NOT EXISTS ix_spark ON spark(underlying, ts);
CREATE TABLE IF NOT EXISTS buckets (underlying TEXT, day TEXT, bucket INTEGER, gross REAL, net REAL,
  PRIMARY KEY (underlying, day, bucket));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def bucket_of(ts: float) -> int:
    dt = datetime.fromtimestamp(ts, ET)
    mins = (dt.hour * 60 + dt.minute) - (9 * 60 + 30)
    return max(0, min(12, mins // config.BUCKET_MIN))


class Store:
    def __init__(self, path: str = config.DB_PATH):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # settings
    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    # prints
    def add_prints(self, prints: list[Print]):
        if not prints:
            return
        rows = []
        for p in prints:
            day = datetime.fromtimestamp(p.ts, ET).date().isoformat()
            rows.append((p.ts, day, p.underlying, p.symbol, p.strike, p.cp, p.expiry, p.dte, p.size, p.price,
                         p.premium, p.side, p.kind, int(p.multi), None if p.bull is None else int(p.bull),
                         p.delta, p.exchanges, int(p.opening)))
        self.db.executemany("INSERT INTO prints (ts,day,underlying,symbol,strike,cp,expiry,dte,size,price,premium,side,"
                            "kind,multi,bull,delta,exchanges,opening) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def recent_prints(self, underlying: str, since_ts: float, limit: int = 5000) -> list[Print]:
        cur = self.db.execute(
            "SELECT id,ts,underlying,symbol,strike,cp,expiry,dte,size,price,premium,side,kind,multi,bull,delta,exchanges,opening "
            "FROM prints WHERE underlying=? AND ts>=? ORDER BY ts DESC LIMIT ?", (underlying, since_ts, limit))
        out = []
        for r in cur.fetchall():
            out.append(Print(id=r[0], ts=r[1], underlying=r[2], symbol=r[3], strike=r[4], cp=r[5], expiry=r[6], dte=r[7],
                             size=r[8], price=r[9], premium=r[10], side=r[11], kind=r[12], multi=bool(r[13]),
                             bull=None if r[14] is None else bool(r[14]), delta=r[15], exchanges=r[16], opening=bool(r[17])))
        out.reverse()
        return out

    # composite history
    def add_spark(self, rows: list[tuple[str, float, int]]):
        self.db.executemany("INSERT INTO spark VALUES (?,?,?)", rows)
        self.db.commit()

    def spark(self, underlying: str, since_ts: float) -> list[tuple[float, int]]:
        return self.db.execute("SELECT ts, score FROM spark WHERE underlying=? AND ts>=? ORDER BY ts",
                               (underlying, since_ts)).fetchall()

    # baselines
    def roll_buckets(self, day: str):
        """Summarise one finished day into per-name, per-30-minute gross and net premium."""
        rows = self.db.execute("SELECT underlying, ts, premium, bull FROM prints WHERE day=? AND bull IS NOT NULL", (day,)).fetchall()
        agg: dict[tuple[str, int], list[float]] = {}
        for u, ts, prem, bull in rows:
            k = (u, bucket_of(ts))
            a = agg.setdefault(k, [0.0, 0.0])
            a[0] += prem
            a[1] += prem if bull else -prem
        self.db.executemany("INSERT OR REPLACE INTO buckets VALUES (?,?,?,?,?)",
                            [(u, day, b, g, n) for (u, b), (g, n) in agg.items()])
        self.db.commit()

    def baselines(self) -> dict[tuple[str, int], Baseline]:
        cutoff = (datetime.now(ET).date() - timedelta(days=config.BASELINE_LOOKBACK_DAYS * 2)).isoformat()
        rows = self.db.execute("SELECT underlying, bucket, net, day FROM buckets WHERE day>=? ORDER BY day DESC", (cutoff,)).fetchall()
        acc: dict[tuple[str, int], list[float]] = {}
        for u, b, net, _ in rows:
            lst = acc.setdefault((u, b), [])
            if len(lst) < config.BASELINE_LOOKBACK_DAYS:
                lst.append(net)
        out = {}
        for k, nets in acc.items():
            rms = math.sqrt(sum(x * x for x in nets) / len(nets)) if nets else None
            out[k] = Baseline(net_std=rms if rms and rms > 0 else None, days=len(nets))
        return out

    def prune(self):
        cutoff = time.time() - config.PRINT_RETENTION_DAYS * 86400
        self.db.execute("DELETE FROM prints WHERE ts<?", (cutoff,))
        self.db.execute("DELETE FROM spark WHERE ts<?", (time.time() - 7 * 86400,))
        self.db.commit()
