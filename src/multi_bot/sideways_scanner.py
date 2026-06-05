from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import math


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _symbol_to_ccxt_symbol(symbol_config: Dict[str, Any]) -> str:
    base = str(symbol_config.get("name", "")).replace("USDT", "").replace("USDC", "")
    quote = str(symbol_config.get("contract_type", "USDT")).upper()
    return f"{base}/{quote}:{quote}"


@dataclass(frozen=True)
class SidewaysMarketScore:
    symbol: str
    ccxt_symbol: str
    score: float
    range_pct: float
    direction_pct: float
    range_to_direction_ratio: float
    spread_pct: Optional[float]
    quote_volume: float
    funding_rate: Optional[float]
    reasons: Tuple[str, ...]


def score_sideways_market(
    symbol: str,
    ccxt_symbol: str,
    candles: Sequence[Sequence[Any]],
    ticker: Optional[Dict[str, Any]] = None,
    funding_rate: Optional[float] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> SidewaysMarketScore:
    """Score a symbol for sideways-grid suitability using recent OHLCV and ticker data."""
    settings = settings or {}
    if not candles or len(candles) < 3:
        raise ValueError("At least 3 candles are required to score a market")

    highs = [_safe_float(candle[2]) for candle in candles if len(candle) >= 5]
    lows = [_safe_float(candle[3]) for candle in candles if len(candle) >= 5]
    closes = [_safe_float(candle[4]) for candle in candles if len(candle) >= 5]

    if not highs or not lows or not closes:
        raise ValueError("Candles must contain high, low, and close values")

    high = max(highs)
    low = min(lows)
    first_close = closes[0]
    last_close = closes[-1]
    mid = (high + low) / 2.0 if (high + low) else 0.0
    if mid <= 0:
        raise ValueError("Invalid candle prices; midpoint is not positive")

    range_pct = (high - low) / mid
    direction_pct = abs(last_close - first_close) / mid
    range_to_direction_ratio = direction_pct / range_pct if range_pct > 0 else 1.0

    spread_pct = None
    if ticker is not None:
        bid = _safe_float(ticker.get("bid"), 0.0)
        ask = _safe_float(ticker.get("ask"), 0.0)
        if bid > 0 and ask > bid:
            spread_pct = (ask - bid) / mid

    quote_volume = _safe_float(ticker.get("quoteVolume") if ticker else None, 0.0)

    min_range_pct = _safe_float(settings.get("min_range_pct"), 0.0020)
    ideal_range_pct = _safe_float(settings.get("ideal_range_pct"), 0.0120)
    max_range_pct = _safe_float(settings.get("max_range_pct"), 0.0500)
    max_direction_pct = _safe_float(settings.get("max_direction_pct"), 0.0060)
    max_spread_pct = _safe_float(settings.get("max_spread_pct"), 0.0015)
    min_quote_volume = _safe_float(settings.get("min_quote_volume"), 0.0)
    max_funding_abs = _safe_float(settings.get("max_funding_abs"), 0.0005)

    # Range quality peaks around the ideal band and fades when the market is too quiet or too wild.
    if range_pct <= 0:
        range_quality = 0.0
    else:
        lower_gap = abs(range_pct - min_range_pct) / max(min_range_pct, 1e-9)
        ideal_gap = abs(range_pct - ideal_range_pct) / max(ideal_range_pct, 1e-9)
        upper_gap = abs(range_pct - max_range_pct) / max(max_range_pct, 1e-9)
        range_quality = 1.0 - min(lower_gap, ideal_gap, upper_gap)
        range_quality = _clamp(range_quality)

    trend_quality = 1.0 - _clamp(direction_pct / max(max_direction_pct, 1e-9))
    chop_quality = 1.0 - _clamp(range_to_direction_ratio)
    spread_quality = 1.0 if spread_pct is None else 1.0 - _clamp(spread_pct / max(max_spread_pct, 1e-9))
    funding_quality = 1.0 if funding_rate is None else 1.0 - _clamp(abs(funding_rate) / max(max_funding_abs, 1e-9))

    if min_quote_volume > 0:
        volume_quality = _clamp(math.log10((quote_volume / min_quote_volume) + 1.0) / math.log10(10.0))
    else:
        volume_quality = 1.0 if quote_volume > 0 else 0.0

    score = (
        0.34 * chop_quality
        + 0.22 * range_quality
        + 0.14 * trend_quality
        + 0.12 * spread_quality
        + 0.10 * volume_quality
        + 0.08 * funding_quality
    )

    reasons = []
    if range_pct < min_range_pct:
        reasons.append("range_too_small")
    if direction_pct > max_direction_pct:
        reasons.append("direction_too_strong")
    if spread_pct is not None and spread_pct > max_spread_pct:
        reasons.append("spread_too_wide")
    if min_quote_volume > 0 and quote_volume < min_quote_volume:
        reasons.append("volume_too_low")
    if funding_rate is not None and abs(funding_rate) > max_funding_abs:
        reasons.append("funding_too_hot")

    return SidewaysMarketScore(
        symbol=symbol,
        ccxt_symbol=ccxt_symbol,
        score=round(float(score), 6),
        range_pct=round(float(range_pct), 6),
        direction_pct=round(float(direction_pct), 6),
        range_to_direction_ratio=round(float(range_to_direction_ratio), 6),
        spread_pct=round(float(spread_pct), 6) if spread_pct is not None else None,
        quote_volume=round(float(quote_volume), 6),
        funding_rate=round(float(funding_rate), 8) if funding_rate is not None else None,
        reasons=tuple(reasons),
    )


def rank_sideways_markets(
    scores: Sequence[SidewaysMarketScore],
    min_score: float = 0.55,
    top_n: Optional[int] = None,
) -> List[SidewaysMarketScore]:
    ranked = [score for score in scores if score.score >= min_score and not score.reasons]
    ranked.sort(key=lambda item: (item.score, item.quote_volume, item.range_pct), reverse=True)
    if top_n is not None:
        ranked = ranked[: max(0, int(top_n))]
    return ranked


def build_scanner_settings(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = config or {}
    scanner_config = config.get("scanner", {}) or {}

    def _env_bool(name: str, default: bool) -> bool:
        import os

        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() == "true"

    def _env_int(name: str, default: int) -> int:
        import os

        value = os.getenv(name)
        if value is None:
            return default
        try:
            return int(float(value))
        except Exception:
            return default

    def _env_float(name: str, default: float) -> float:
        import os

        value = os.getenv(name)
        if value is None:
            return default
        try:
            return float(value)
        except Exception:
            return default

    return {
        "enabled": _env_bool("SIDEWAYS_SCAN_ENABLED", bool(scanner_config.get("enabled", False))),
        "timeframe": str(scanner_config.get("timeframe", "15m")),
        "lookback": _env_int("SIDEWAYS_SCAN_LOOKBACK", int(scanner_config.get("lookback", 96))),
        "top_n": _env_int("SIDEWAYS_SCAN_TOP_N", int(scanner_config.get("top_n", 3))),
        "min_score": _env_float("SIDEWAYS_SCAN_MIN_SCORE", float(scanner_config.get("min_score", 0.55))),
        "min_range_pct": _env_float("SIDEWAYS_SCAN_MIN_RANGE_PCT", float(scanner_config.get("min_range_pct", 0.0020))),
        "ideal_range_pct": _env_float("SIDEWAYS_SCAN_IDEAL_RANGE_PCT", float(scanner_config.get("ideal_range_pct", 0.0120))),
        "max_range_pct": _env_float("SIDEWAYS_SCAN_MAX_RANGE_PCT", float(scanner_config.get("max_range_pct", 0.0500))),
        "max_direction_pct": _env_float("SIDEWAYS_SCAN_MAX_DIRECTION_PCT", float(scanner_config.get("max_direction_pct", 0.0060))),
        "max_spread_pct": _env_float("SIDEWAYS_SCAN_MAX_SPREAD_PCT", float(scanner_config.get("max_spread_pct", 0.0015))),
        "min_quote_volume": _env_float("SIDEWAYS_SCAN_MIN_QUOTE_VOLUME", float(scanner_config.get("min_quote_volume", 0.0))),
        "max_funding_abs": _env_float("SIDEWAYS_SCAN_MAX_FUNDING_ABS", float(scanner_config.get("max_funding_abs", 0.0005))),
    }


def create_public_futures_exchange(sandbox: bool = False):
    import ccxt

    exchange = ccxt.binance(
        {
            "options": {
                "defaultType": "future",
            },
            "enableRateLimit": True,
        }
    )
    if sandbox and hasattr(exchange, "setSandboxMode"):
        exchange.setSandboxMode(True)
    exchange.load_markets(reload=False)
    return exchange


def select_sideways_symbol_configs(
    symbol_configs: Sequence[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
    exchange: Any = None,
    logger: Any = None,
) -> Tuple[List[Dict[str, Any]], List[SidewaysMarketScore]]:
    settings = settings or {}
    if exchange is None:
        exchange = create_public_futures_exchange(bool(settings.get("sandbox", False)))

    try:
        tickers = exchange.fetch_tickers()
    except Exception:
        tickers = {}

    scored: List[SidewaysMarketScore] = []
    for symbol_config in symbol_configs:
        symbol = str(symbol_config.get("name", "")).strip()
        if not symbol:
            continue

        ccxt_symbol = _symbol_to_ccxt_symbol(symbol_config)
        try:
            candles = exchange.fetch_ohlcv(
                ccxt_symbol,
                timeframe=str(settings.get("timeframe", "15m")),
                limit=int(settings.get("lookback", 96)),
            )
            ticker = tickers.get(ccxt_symbol) or {}
            funding_rate = None
            try:
                if hasattr(exchange, "fetch_funding_rate"):
                    funding = exchange.fetch_funding_rate(ccxt_symbol)
                    if isinstance(funding, dict):
                        funding_rate = funding.get("fundingRate")
            except Exception:
                funding_rate = None

            score = score_sideways_market(
                symbol=symbol,
                ccxt_symbol=ccxt_symbol,
                candles=candles,
                ticker=ticker,
                funding_rate=funding_rate,
                settings=settings,
            )
            scored.append(score)
        except Exception as exc:
            if logger is not None:
                logger.warning(f"Skipping {symbol}: unable to score market ({exc})")

    ranked_scores = rank_sideways_markets(
        scored,
        min_score=float(settings.get("min_score", 0.55)),
        top_n=None,
    )

    top_n = int(settings.get("top_n", 3))
    selected_scores = ranked_scores[: max(0, top_n)]
    config_by_name = {str(symbol_config.get("name", "")).strip(): symbol_config for symbol_config in symbol_configs}
    selected_symbols = [
        config_by_name[score.symbol]
        for score in selected_scores
        if score.symbol in config_by_name
    ]

    if logger is not None:
        if selected_scores:
            logger.info(
                "Sideways scanner selected: %s",
                ", ".join(
                    f"{score.symbol}(score={score.score:.3f}, range={score.range_pct:.2%}, move={score.direction_pct:.2%})"
                    for score in selected_scores
                ),
            )
        else:
            logger.warning("Sideways scanner found no candidates above the configured score threshold")

    return selected_symbols, ranked_scores
