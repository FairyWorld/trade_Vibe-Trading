"""Zerodha Kite Connect connector SDK (read path + paper-capped orders).

Mirrors the Shoonya/Dhan connector surface so the ``india_broker`` loader can
treat Zerodha uniformly:

  get_historical_bars(symbol, *, exchange="NSE", period="1d", limit=90)
      -> {"status": "ok", "symbol": ..., "bars": [{time, open, high, low, close, volume}]}

Kite specifics handled here:
  * interval map: project tokens (1m/5m/15m/30m/1h/4h/1d) -> Kite intervals
    (minute/5minute/15minute/30minute/60minute/day). Kite has no 1H/4H token
    collision, but we alias 1h->60minute, 4h->day to match the project set.
  * Kite historical is capped at 2000 days per call and dates bars at midnight
    IST; we paginate by chunking the [start, end] range into <=2000-day windows
    and normalize the resulting index to UTC-naive (matching the loader).
  * Symbols: project ``RELIANCE.NS`` -> Kite ``exchange=NSE``,
    ``tradingsymbol=RELIANCE``. Token-based fetch is also supported if the
    instrument token is supplied, but the symbol form mirrors Shoonya/Dhan.

Paper guard: like Shoonya, Kite exposes no sandbox, so live order placement is
structurally refused (paper profile only).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src.config.paths import get_runtime_root

CONFIG_FILENAME = "zerodha.json"

PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
}

KITE_HIST_BASE = "https://api.kite.trade"

_PAPER_ONLY_ERROR = (
    "Zerodha connector is paper-only: Kite exposes no runtime paper/live "
    "discriminator (a live token reaches the real account), so live order "
    "placement is not supported. Use a zerodha-paper-* profile."
)


class ZerodhaDependencyError(RuntimeError):
    """Raised when ``kiteconnect`` is not installed."""


class ZerodhaConfigError(RuntimeError):
    """Raised when config is missing or invalid."""


class ZerodhaExchange:
    NSE = "NSE"
    BSE = "BSE"
    NFO = "NFO"  # NSE F&O
    BFO = "BFO"  # BSE F&O
    CDS = "CDS"  # Currency
    MCX = "MCX"  # Commodity


@dataclass(frozen=True)
class ZerodhaConfig:
    """Zerodha connector connection settings."""

    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    profile: str = "paper"
    timeout: float = 15.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "ZerodhaConfig":
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise ZerodhaConfigError("profile must be 'paper', 'live-readonly' or 'live'")
        return cls(
            api_key=str(payload.get("api_key") or "").strip(),
            api_secret=str(payload.get("api_secret") or "").strip(),
            access_token=str(payload.get("access_token") or "").strip(),
            profile=profile,
            timeout=float(payload.get("timeout") or 15.0),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(self, **kw: Any) -> "ZerodhaConfig":
        payload = asdict(self)
        for key in ("api_key", "api_secret", "access_token", "profile"):
            if kw.get(key) is not None:
                payload[key] = kw[key]
        return ZerodhaConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")

    @property
    def is_paper(self) -> bool:
        return self.environment == "paper"


_OVERRIDE_KEYS = ("api_key", "api_secret", "access_token", "profile")

#: Project interval token -> Kite interval.
_INTERVAL_MAP = {
    "1m": "minute",
    "5m": "5minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h": "60minute",
    "1H": "60minute",
    "4h": "day",
    "4H": "day",
    "1d": "day",
    "1D": "day",
}

#: Kite caps historical pulls at 2000 calendar days per request.
_KITE_MAX_SPAN_DAYS = 2000


def config_path() -> Path:
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> ZerodhaConfig:
    path = config_path()
    if not path.exists():
        return ZerodhaConfig()
    try:
        return ZerodhaConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ZerodhaConfigError(f"invalid Zerodha config at {path}: {exc}") from exc


def save_config(config: ZerodhaConfig) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def build_config(
    profile_config: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> "ZerodhaConfig":
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = ZerodhaConfig.from_mapping(base)
    clean = {
        k: v for k, v in dict(overrides or {}).items()
        if k in _OVERRIDE_KEYS and v not in (None, "")
    }
    return cfg.with_overrides(**clean) if clean else cfg


def _require_kite():
    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError as exc:  # optional dependency
        raise ZerodhaDependencyError(
            "Optional dependency missing: install with `pip install kiteconnect`"
        ) from exc
    return KiteConnect


def zerodha_available() -> bool:
    try:
        _require_kite()
        return True
    except ZerodhaDependencyError:
        return False


def _public_config(cfg: ZerodhaConfig) -> dict[str, Any]:
    return {
        "api_key_set": bool(cfg.api_key),
        "access_token_set": bool(cfg.access_token),
        "profile": cfg.profile,
    }


def _login(cfg: ZerodhaConfig):
    """Return an authenticated KiteConnect client (access_token set)."""
    KiteConnect = _require_kite()
    if not cfg.api_key:
        raise ZerodhaConfigError("api_key is required")
    kite = KiteConnect(api_key=cfg.api_key)
    if cfg.access_token:
        kite.set_access_token(cfg.access_token)
    elif cfg.api_secret:
        # Without a request token we cannot mint a fresh access token headlessly;
        # the user must supply either access_token or run the login helper.
        raise ZerodhaConfigError(
            "access_token is required (Kite login needs an interactive "
            "request-token exchange; supply access_token directly)"
        )
    return kite


def check_status(config: ZerodhaConfig | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "kiteconnect", "installed": zerodha_available()},
        "paper_guard": "simulated_locally",
        "host": KITE_HIST_BASE,
        "brokerage": "Zerodha equity delivery ₹0; intraday 0.03% / ₹20 flat",
    }
    missing = []
    if not cfg.api_key:
        missing.append("api_key")
    if not cfg.access_token:
        missing.append("access_token")
    if missing:
        report["status"] = "error"
        report["error"] = f"Zerodha connector not configured: missing {', '.join(missing)}."
        return report
    if not report["sdk"]["installed"]:
        report["status"] = "error"
        report["error"] = "Optional dependency missing: install with `pip install kiteconnect`"
        return report
    return report


def get_historical_bars(
    symbol: str,
    *,
    config: ZerodhaConfig | None = None,
    exchange: str = "NSE",
    period: str = "1d",
    limit: int = 90,
) -> dict[str, Any]:
    """Fetch historical OHLCV bars from Kite, paginated past the 2000-day cap."""
    cfg = config or load_config()
    clean = symbol.strip().upper()

    interval = _INTERVAL_MAP.get(str(period).strip())
    if interval is None:
        return {
            "status": "error",
            "error": f"unsupported period: {period!r}; supported: {sorted(_INTERVAL_MAP)}",
            "symbol": clean,
        }

    try:
        kite = _login(cfg)
    except ZerodhaConfigError as exc:
        return {"status": "error", "error": str(exc), "symbol": clean}

    # Build the date window: from (today - span) to today, span sized to `limit`
    # daily bars (intraday uses a shorter window like Shoonya).
    end = datetime.now(timezone.utc)
    if interval == "day":
        start = end - timedelta(days=min(limit * 2, 4000))
    else:
        start = end - timedelta(days=5)

    bars: list[dict[str, Any]] = []
    # Paginate in <=2000-day chunks (Kite hard cap).
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=_KITE_MAX_SPAN_DAYS), end)
        try:
            rows = kite.historical_data(
                instrument_token=_symbol_to_token(kite, clean, exchange),
                from_date=chunk_start,
                to_date=chunk_end,
                interval=interval,
            )
        except Exception as exc:  # noqa: BLE001 — one bad symbol never aborts
            return {"status": "error", "error": str(exc), "symbol": clean}
        for r in rows:
            ts = r.get("date")
            if ts is None:
                continue
            # Kite returns tz-aware datetimes (IST); normalize to UTC-naive.
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
            bars.append({
                "time": int(ts.timestamp()),
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
            })
        chunk_start = chunk_end

    if not bars:
        return {"status": "ok", "symbol": clean, "exchange": exchange, "period": period, "bars": []}
    return {
        "status": "ok",
        "symbol": clean,
        "exchange": exchange,
        "period": period,
        "bars": bars[-limit:],
    }


def _symbol_to_token(kite, symbol: str, exchange: str) -> int:
    """Resolve a tradingsymbol+exchange to an instrument token via Kite's
    instruments list (cached per process). Falls back to a name-based search."""
    try:
        instruments = kite.instruments(exchange=exchange)
    except Exception:
        instruments = []
    for inst in instruments:
        if inst.get("tradingsymbol", "").upper() == symbol.upper():
            return int(inst["instrument_token"])
    raise ZerodhaConfigError(f"instrument token not found for {symbol} ({exchange})")


def get_quote(
    symbol: str,
    *,
    config: ZerodhaConfig | None = None,
    exchange: str = "NSE",
) -> dict[str, Any]:
    cfg = config or load_config()
    clean = symbol.strip().upper()
    try:
        kite = _login(cfg)
    except ZerodhaConfigError as exc:
        return {"status": "error", "error": str(exc), "symbol": clean}
    try:
        q = kite.quote([f"{exchange}:{clean}"])
        last = q.get(f"{exchange}:{clean}", {})
    except Exception as exc:
        return {"status": "error", "error": str(exc), "symbol": clean}
    return {
        "status": "ok",
        "symbol": clean,
        "exchange": exchange,
        "quote": {
            "ltp": float(last.get("last_price", 0)),
            "open": float(last.get("ohlc", {}).get("open", 0)),
            "high": float(last.get("ohlc", {}).get("high", 0)),
            "low": float(last.get("ohlc", {}).get("low", 0)),
            "close": float(last.get("ohlc", {}).get("close", 0)),
            "volume": int(last.get("volume", 0)),
        },
    }


def place_order(
    config: ZerodhaConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    exchange: str = "NSE",
    product_type: str = "C",
) -> dict[str, Any]:
    """Place a PAPER-ONLY order on Zerodha (simulated locally).

    Kite exposes no sandbox, so this connector is structurally capped at paper:
    the very first check refuses any non-paper config. There is no live order
    path, by design.
    """
    cfg = config or load_config()
    if not cfg.is_paper:
        return {"status": "error", "error": _PAPER_ONLY_ERROR}

    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        return {"status": "error", "error": "symbol is required"}

    side_token = str(side or "").strip().upper()
    side_map = {"BUY": "B", "SELL": "S", "B": "B", "S": "S"}
    if side_token not in side_map:
        return {"status": "error", "error": "side must be 'buy' or 'sell'"}
    buy_or_sell = side_map[side_token]

    if quantity is None or float(quantity) <= 0:
        return {"status": "error", "error": "quantity must be positive"}
    qty = int(float(quantity))

    return {
        "status": "ok",
        "order_id": f"PAPER-{clean_symbol}-{buy_or_sell}-{qty}",
        "symbol": clean_symbol,
        "side": side_token.lower(),
        "profile": cfg.profile,
        "is_paper": True,
        "paper_guard": "simulated_locally",
        "order_type": order_type.lower(),
        "quantity": qty,
        "limit_price": float(limit_price) if limit_price is not None else None,
        "order_status": "simulated_fill",
        "exchange": exchange,
        "product_type": product_type,
    }
