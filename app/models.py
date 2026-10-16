import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# OCC option symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits), e.g. NVDA251017C00185000
_OCC = re.compile(r"^([A-Z.]{1,6}?)(\d{6})([CP])(\d{8})$")
# Option roots that trade on a different underlying ticker.
ROOT_ALIASES = {"SPXW": "SPX", "NDXP": "NDX", "RUTW": "RUT", "VIXW": "VIX"}


@dataclass(slots=True, frozen=True)
class Contract:
    symbol: str
    underlying: str
    expiry: date
    cp: str  # "C" or "P"
    strike: float


_cache: dict[str, Contract | None] = {}


def parse_occ(symbol: str) -> Contract | None:
    c = _cache.get(symbol)
    if c is not None or symbol in _cache:
        return c
    m = _OCC.match(symbol)
    if not m:
        _cache[symbol] = None
        return None
    root, ymd, cp, k = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    c = Contract(symbol, ROOT_ALIASES.get(root, root), exp, cp, int(k) / 1000.0)
    if len(_cache) > 200_000:
        _cache.clear()
    _cache[symbol] = c
    return c


def root_of(symbol: str) -> str:
    """Cheap root extraction for filtering the wildcard trade firehose before full parsing."""
    i = 0
    n = len(symbol)
    while i < n and not symbol[i].isdigit():
        i += 1
    root = symbol[:i]
    return ROOT_ALIASES.get(root, root)


def today_et() -> date:
    return datetime.now(ET).date()


def dte(expiry: date, ref: date | None = None) -> int:
    return (expiry - (ref or today_et())).days


@dataclass(slots=True)
class Quote:
    symbol: str
    ts: float
    bid: float
    ask: float
    bid_size: int = 0
    ask_size: int = 0


@dataclass(slots=True)
class Trade:
    symbol: str
    ts: float
    price: float
    size: int
    exchange: str = ""
    condition: str = ""


@dataclass(slots=True)
class ChainRow:
    symbol: str
    strike: float
    cp: str
    expiry: date
    oi: int
    iv: float | None
    gamma: float | None
    delta: float | None
    day_volume: int = 0


@dataclass
class Print:
    """One order as shown on the tape: a single print, or several prints on one contract grouped together."""
    id: int
    ts: float
    underlying: str
    symbol: str
    strike: float
    cp: str
    expiry: str  # ISO date
    dte: int
    size: int
    price: float
    premium: float
    side: str  # ASK / BID / MID / NA
    kind: str  # SWEEP / BLOCK / SPLIT / REG
    multi: bool
    bull: bool | None
    delta: float | None = None
    exchanges: int = 1
    opening: bool = False

    def to_wire(self) -> dict:
        d = asdict(self)
        d["premium"] = round(self.premium, 2)
        d["price"] = round(self.price, 4)
        return d
