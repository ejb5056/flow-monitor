"""All settings come from environment variables so Railway can change them without a code push."""
import os


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(os.getenv(name, default))


# Which data source drives the app: "sim" (simulated market), "alpaca" (live OPRA), "probe" (Alpaca subscription test only).
FEED = os.getenv("FEED", "sim").lower()

# Login. If unset the app runs open, which is only acceptable on your own machine.
APP_PASSWORD = os.getenv("APP_PASSWORD", "")

# Alpaca credentials and endpoints.
ALPACA_KEY_ID = os.getenv("ALPACA_KEY_ID", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
ALPACA_OPTIONS_FEED = os.getenv("ALPACA_OPTIONS_FEED", "opra")  # "indicative" on the free plan
ALPACA_STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "sip")  # "iex" on the free plan
ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
ALPACA_TRADING_URL = os.getenv("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets")
ALPACA_STREAM_URL = os.getenv("ALPACA_STREAM_URL", "wss://stream.data.alpaca.markets")
# "wildcard" = one subscription for every option trade, filtered here; "contracts" = per-contract trade subscriptions.
TRADES_SUBSCRIPTION = os.getenv("TRADES_SUBSCRIPTION", "contracts").lower()
# Per-contract trade subscriptions (the probe found these aren't capped by the quote limit on the free plan).
TRADE_SUB_MAX = _i("TRADE_SUB_MAX", 6000)
TRADE_MAX_DTE = _i("TRADE_MAX_DTE", 45)
TRADE_MONEYNESS = _f("TRADE_MONEYNESS", 0.20)
QUOTE_BUDGET = _i("QUOTE_BUDGET", 1000)

# Storage. On Railway mount a volume at /data.
DB_PATH = os.getenv("DB_PATH", "/data/flow.db" if os.path.isdir("/data") else "./data/flow.db")
PRINT_RETENTION_DAYS = _i("PRINT_RETENTION_DAYS", 30)

DEFAULT_WATCHLIST = [s.strip().upper() for s in os.getenv(
    "WATCHLIST", "SPY,QQQ,NVDA,TSLA,AAPL,AMD,META,AMZN,MSFT,PLTR").split(",") if s.strip()]
MAX_WATCHLIST = _i("MAX_WATCHLIST", 20)
MAX_EXTRAS = _i("MAX_EXTRAS", 8)  # off-watchlist names tracked because a panel shows them (DAS link or typed)
EXTRA_IDLE_S = _i("EXTRA_IDLE_S", 600)  # stop tracking such a name after no panel has shown it this long

# Print classification.
SIDE_ASK_AT = _f("SIDE_ASK_AT", 0.75)  # position in the spread at or above which a print counts as ask-side
SIDE_BID_AT = _f("SIDE_BID_AT", 0.25)  # at or below = bid-side; between = mid
QUOTE_MAX_AGE_S = _f("QUOTE_MAX_AGE_S", 10.0)  # older quote than this -> side unknown
GROUP_MS = _i("GROUP_MS", 50)  # prints on one contract within this many ms are one order
BLOCK_MIN_SIZE = _i("BLOCK_MIN_SIZE", 100)  # single-print order at or above this many contracts = block

# Levels.
GEX_MAX_DTE = _i("GEX_MAX_DTE", 60)
CHAIN_REFRESH_S = _i("CHAIN_REFRESH_S", 300)
RISK_FREE = _f("RISK_FREE", 0.04)

# Composite.
WINDOW_MIN = _i("WINDOW_MIN", 30)
W_PREMIUM = _f("W_PREMIUM", 0.40)
W_SWEEP = _f("W_SWEEP", 0.20)
W_DELTA = _f("W_DELTA", 0.25)
W_OPENING = _f("W_OPENING", 0.15)
BASELINE_MIN_DAYS = _i("BASELINE_MIN_DAYS", 5)
BASELINE_LOOKBACK_DAYS = _i("BASELINE_LOOKBACK_DAYS", 20)
BUCKET_MIN = 30  # baseline time-of-day buckets
