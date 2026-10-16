# Flow Monitor

Real-time options flow for a 10–20 name watchlist. It has two windows:

- **Strip** (`/`): one row per ticker, showing the composite score, gamma regime, and where price sits between the put and call wall. Tap a row for detail; **Edit list** changes the watchlist.
- **Panel** (`/panel?t=NVDA`): one ticker, meant to sit beside the execution window. It shows the score, the 90-minute trend, the score's components, the levels, and the full print tape. The tape has min/max filters for premium, size and DTE; side, kind and multi-leg toggles; and a 5/10/All row view. Type a new symbol into the ticker field and press Enter to switch. Filters and the row view are remembered per browser.

## How it works

| Part | File | What it does |
|---|---|---|
| Feed | `app/feeds/alpaca.py` | Alpaca OPRA trades and quotes (websocket), stock trades for spot, chain snapshots (greeks/IV), and open interest. |
| | `app/feeds/simulated.py` | An invented market for building and testing (`FEED=sim`). |
| | `app/feeds/probe.py` | Tests Alpaca's subscription limits (`FEED=probe`). |
| Classification | `app/engine/classify.py` | **Side:** where the print fell in that contract's bid/ask (top 25% of the spread = ASK, bottom 25% = BID, else MID; no fresh quote = unknown). **Kind:** prints on one contract within 50 ms are one order. It's a SWEEP if ISO-flagged or filled on 2+ exchanges, a SPLIT if same price in pieces, a BLOCK if a single print of 100+ contracts, otherwise REG. **Multi-leg** comes from the trade's condition code. |
| Levels | `app/engine/gex.py` | GEX by strike = gamma × OI × 100 × spot² × 1%. Calls count positive and puts negative (the standard dealer-positioning assumption). Computes the call wall, put wall, gamma flip (a price scan, refreshed each minute), and max pain on the nearest expiry. Walls refresh every 5 s at the live spot. |
| Composite | `app/engine/composite.py` | −100 to +100 over a rolling 30-minute window: net directional premium (40%), sweep bullish share (20%), net customer delta in dollars (25%), and the opening-trade imbalance where volume exceeds OI (15%). A name shows *calibrating* until it has 5 sessions of history. Until then net premium is scored as a bull/bear imbalance; after that, as a z-score against that name's own history for the same 30-minute slot. |
| Storage | `app/store.py` | SQLite holds every tape row (30 days), the composite once a minute, and per-name baselines rolled up at 16:20 ET. |
| Server | `app/main.py`, `app/hub.py` | FastAPI server, password login, and a websocket that pushes summaries every second plus each print as it happens. |

Every threshold and weight is an environment variable (`app/config.py`), so it can be changed in Railway without a code change.

## Deploy on Railway

1. On railway.com, choose **New Project → Deploy from GitHub repo → `flow-monitor`**.
2. In the service, open **Settings → Networking → Generate Domain**.
3. Add a volume: right-click the service, choose **Attach Volume**, and set the mount path to `/data`. Without it, history is lost on every redeploy.
4. Under **Variables**, add:

| Variable | Value |
|---|---|
| `APP_PASSWORD` | your choice |
| `FEED` | `probe` first, then `alpaca` |
| `ALPACA_KEY_ID` / `ALPACA_SECRET` | from the Alpaca dashboard |
| `ALPACA_OPTIONS_FEED` | `indicative` on the free plan, `opra` on Algo Trader Plus |
| `ALPACA_STOCK_FEED` | `iex` on the free plan, `sip` on Algo Trader Plus |
| `ALPACA_TRADING_URL` | `https://paper-api.alpaca.markets` for paper keys (the default), `https://api.alpaca.markets` for live keys |
| `WATCHLIST` | the starting list, e.g. `SPY,QQQ,NVDA,...`. After the first run, edit it in the app. |

## Before paying for Alpaca: the probe

With `FEED=probe` and free-plan keys, open `https://<your-domain>/api/probe` after about a minute. It reports:

- whether snapshots carry greeks and IV, and whether contracts carry open interest;
- whether a wildcard trade subscription is accepted, and how many trades per second it delivers;
- whether a wildcard quote subscription is rejected;
- how many quote subscriptions are accepted before an error;
- whether per-contract trade subscriptions hit the same cap.

## DAS Link (panel follows your DAS montage)

`tools/das-link/` is a small PowerShell helper for the trading PC. It needs no install. Four times a second it reads DAS window titles. A montage title starts with the symbol, e.g. `AAPL  345.6 - 345.75  Apple Inc.`. It then sends the app the symbol in each montage. Montages are numbered by screen position: DAS 1 is the top-left one, counting down, then right. In a panel, set **LINK** to `DAS 1`, `DAS 2`, … and the panel switches whenever that montage's symbol changes. Typing a symbol into the panel puts it back on Manual. A montage symbol that isn't on the watchlist is tracked while it's open, but stays off the strip.

The helper only reads window titles. It never clicks, types, or sends anything to DAS.

1. Put the three files in one folder, e.g. `C:\DAS Link`.
2. Double-click **DAS Link - test.cmd** with DAS open. It lists the montages it sees and doesn't send anything.
3. Double-click **DAS Link.cmd**. The first run asks for the app address and password, and saves them next to the script in `das-link.json`. Keep that window open (minimized is fine) while you trade.

## Run locally

```
pip install -r requirements.txt
uvicorn app.main:app --port 8000      # FEED defaults to sim
pytest
```

## Known limits

- Index tickers (SPX, NDX) have no stock quote on Alpaca, so their spot is derived from put-call parity on the nearest expiry.
- Ask/bid side needs a quote on that contract. Alpaca's 1,000-quote cap is spread across the watchlist on the strikes nearest the money. Prints on unquoted contracts show side "—" and don't count toward the score.
- Baselines need live sessions: historical option trades without historical quotes can't be side-tagged.
