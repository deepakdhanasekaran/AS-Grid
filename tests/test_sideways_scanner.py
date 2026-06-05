import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_MULTI_BOT = ROOT_DIR / "src" / "multi_bot"
if str(SRC_MULTI_BOT) not in sys.path:
    sys.path.insert(0, str(SRC_MULTI_BOT))

from sideways_scanner import (
    SidewaysMarketScore,
    rank_sideways_markets,
    score_sideways_market,
    select_sideways_symbol_configs,
)


def build_sideways_candles():
    return [
        [1, 100.0, 100.8, 99.6, 100.1, 1200],
        [2, 100.1, 100.7, 99.7, 99.9, 1250],
        [3, 99.9, 100.6, 99.5, 100.0, 1300],
        [4, 100.0, 100.9, 99.7, 100.2, 1280],
        [5, 100.2, 100.8, 99.8, 100.1, 1270],
        [6, 100.1, 100.7, 99.6, 100.0, 1260],
    ]


def build_trending_candles():
    return [
        [1, 100.0, 100.5, 99.8, 100.0, 1200],
        [2, 100.4, 101.0, 100.1, 100.8, 1250],
        [3, 101.0, 101.7, 100.7, 101.5, 1300],
        [4, 101.8, 102.4, 101.4, 102.1, 1280],
        [5, 102.2, 103.0, 101.9, 102.8, 1270],
        [6, 103.0, 103.8, 102.7, 103.6, 1260],
    ]


class _FakeExchange:
    def __init__(self, candles_by_symbol, tickers_by_symbol, funding_by_symbol=None):
        self.candles_by_symbol = candles_by_symbol
        self.tickers_by_symbol = tickers_by_symbol
        self.funding_by_symbol = funding_by_symbol or {}

    def fetch_tickers(self):
        return self.tickers_by_symbol

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=96):
        return self.candles_by_symbol[symbol]

    def fetch_funding_rate(self, symbol):
        return {"fundingRate": self.funding_by_symbol.get(symbol)}


class SidewaysScannerTests(unittest.TestCase):
    def test_sideways_score_beats_trending_score(self):
        settings = {
            "min_range_pct": 0.0020,
            "ideal_range_pct": 0.0120,
            "max_range_pct": 0.0500,
            "max_direction_pct": 0.0200,
            "max_spread_pct": 0.0020,
            "min_quote_volume": 0,
            "max_funding_abs": 0.0010,
        }

        sideways = score_sideways_market(
            "SUI",
            "SUI/USDT:USDT",
            build_sideways_candles(),
            ticker={"bid": 100.0, "ask": 100.05, "quoteVolume": 10000000},
            funding_rate=0.0001,
            settings=settings,
        )
        trending = score_sideways_market(
            "BTC",
            "BTC/USDT:USDT",
            build_trending_candles(),
            ticker={"bid": 103.5, "ask": 103.8, "quoteVolume": 10000000},
            funding_rate=0.0001,
            settings=settings,
        )

        self.assertGreater(sideways.score, trending.score)
        self.assertLess(sideways.direction_pct, trending.direction_pct)

    def test_rank_sideways_markets_applies_threshold(self):
        scores = [
            SidewaysMarketScore("AAA", "AAA/USDT:USDT", 0.91, 0.012, 0.001, 0.08, 0.0002, 1000000, None, ()),
            SidewaysMarketScore("BBB", "BBB/USDT:USDT", 0.54, 0.014, 0.006, 0.42, 0.0004, 1000000, None, ()),
            SidewaysMarketScore("CCC", "CCC/USDT:USDT", 0.82, 0.010, 0.002, 0.20, 0.0003, 1000000, None, ()),
        ]

        ranked = rank_sideways_markets(scores, min_score=0.6, top_n=2)

        self.assertEqual([item.symbol for item in ranked], ["AAA", "CCC"])

    def test_select_sideways_symbol_configs_uses_rank_order(self):
        candles_by_symbol = {
            "BTC/USDT:USDT": build_trending_candles(),
            "ETH/USDT:USDT": build_sideways_candles(),
        }
        tickers_by_symbol = {
            "BTC/USDT:USDT": {"bid": 103.5, "ask": 103.8, "quoteVolume": 10000000},
            "ETH/USDT:USDT": {"bid": 100.0, "ask": 100.04, "quoteVolume": 12000000},
        }
        exchange = _FakeExchange(candles_by_symbol, tickers_by_symbol, {"BTC/USDT:USDT": 0.0001, "ETH/USDT:USDT": 0.0001})
        symbol_configs = [
            {"name": "BTC", "contract_type": "USDT", "grid_spacing": 0.004, "initial_quantity": 0.001},
            {"name": "ETH", "contract_type": "USDT", "grid_spacing": 0.004, "initial_quantity": 0.01},
        ]
        settings = {
            "timeframe": "15m",
            "lookback": 6,
            "top_n": 1,
            "min_score": 0.6,
            "min_range_pct": 0.0020,
            "ideal_range_pct": 0.0120,
            "max_range_pct": 0.0500,
            "max_direction_pct": 0.0200,
            "max_spread_pct": 0.0020,
            "min_quote_volume": 0,
            "max_funding_abs": 0.0010,
        }

        selected_symbols, ranked_scores = select_sideways_symbol_configs(
            symbol_configs,
            settings=settings,
            exchange=exchange,
        )

        self.assertEqual([item["name"] for item in selected_symbols], ["ETH"])
        self.assertEqual(ranked_scores[0].symbol, "ETH")


if __name__ == "__main__":
    unittest.main()
