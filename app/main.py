import asyncio
import hashlib
import hmac
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app import config
from app.hub import Client, Hub
from app.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
WEB = Path(__file__).resolve().parent.parent / "web"
COOKIE = "fm_auth"

app = FastAPI(docs_url=None, redoc_url=None)
store = Store()
hub = Hub(store)


# ---- auth -------------------------------------------------------------------------------------
def _token() -> str:
    key = hashlib.sha256(("fm:" + config.APP_PASSWORD).encode()).digest()
    return hmac.new(key, b"flow-monitor", hashlib.sha256).hexdigest()


def _authed(cookies) -> bool:
    if not config.APP_PASSWORD:
        return True
    return hmac.compare_digest(cookies.get(COOKIE, ""), _token())


@app.middleware("http")
async def guard(request: Request, call_next):
    open_paths = {"/login", "/healthz", "/static/style.css"}
    key_ok = bool(config.APP_PASSWORD) and hmac.compare_digest(request.headers.get("x-app-key", ""), config.APP_PASSWORD)
    if request.url.path not in open_paths and not key_ok and not _authed(request.cookies):
        if request.url.path.startswith("/api"):
            return JSONResponse({"error": "login required"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


@app.get("/login")
async def login_page():
    return FileResponse(WEB / "login.html")


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if config.APP_PASSWORD and hmac.compare_digest(password, config.APP_PASSWORD):
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        r = RedirectResponse("/", status_code=303)
        r.set_cookie(COOKIE, _token(), max_age=180 * 86400, httponly=True, samesite="lax", secure=https)
        return r
    return RedirectResponse("/login?bad=1", status_code=303)


# ---- pages ------------------------------------------------------------------------------------
@app.get("/")
async def strip():
    return FileResponse(WEB / "strip.html")


@app.get("/panel")
async def panel():
    return FileResponse(WEB / "panel.html")


@app.get("/static/{name}")
async def static(name: str):
    p = (WEB / name).resolve()
    if p.parent != WEB.resolve() or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


@app.get("/healthz")
async def health():
    return {"ok": True, "feed": config.FEED}


# ---- api --------------------------------------------------------------------------------------
@app.get("/api/status")
async def status():
    return {"status": hub.status, "watchlist": hub.watchlist,
            "chains": {s: len(st.chain) for s, st in hub.t.items()}}


@app.post("/api/watchlist")
async def set_watchlist(request: Request):
    body = await request.json()
    syms = body.get("symbols") or []
    if isinstance(syms, str):
        syms = syms.replace(" ", ",").split(",")
    await hub.set_watchlist(syms)
    return {"watchlist": hub.watchlist}


@app.post("/api/das")
async def das_update(request: Request):
    """From the DAS Link helper on the trading PC: the symbol in each open montage."""
    body = await request.json()
    await hub.set_das(body.get("montages") or [])
    return {"ok": True, "tracked": hub.tracked()}


@app.get("/api/das")
async def das_state():
    return hub.das


@app.get("/api/probe")
async def probe_report():
    from app.feeds.probe import REPORT
    return REPORT


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _authed(ws.cookies):
        await ws.close(code=4401)
        return
    await ws.accept()
    c = Client(ws)
    hub.clients.add(c)
    writer = asyncio.create_task(c.writer())
    try:
        c.send(hub.summaries())
        while True:
            msg = await ws.receive_json()
            op = msg.get("op")
            if op == "view":
                c.view = msg.get("view", "strip")
                t = (msg.get("ticker") or "").upper() or None
                c.ticker = t
                if c.view == "panel" and t:
                    await hub.ensure_tracked(t)
                    c.send(hub.snapshot_for(t))
            elif op == "ping":
                c.send({"t": "pong", "ts": time.time()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.clients.discard(c)
        writer.cancel()


# ---- startup ----------------------------------------------------------------------------------
@app.on_event("startup")
async def start():
    if not config.APP_PASSWORD:
        log.warning("APP_PASSWORD is not set: the app is open to anyone with the URL")
    if config.FEED == "alpaca":
        from app.feeds.alpaca import AlpacaFeed
        hub.feed = AlpacaFeed(hub)
    elif config.FEED == "uw":
        from app.feeds.uw import UWFeed
        hub.feed = UWFeed(hub)
    elif config.FEED == "probe":
        from app.feeds.probe import run_probe
        hub.status.update(msg="probe running, see /api/probe")
        asyncio.create_task(run_probe(hub))
    else:
        from app.feeds.simulated import SimFeed
        hub.feed = SimFeed(hub)
    if hub.feed:
        asyncio.create_task(hub.feed.run())
    asyncio.create_task(hub.run())
