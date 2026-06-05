import sys
import unittest
from collections import deque
from pathlib import Path
import types


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_MULTI_BOT = ROOT_DIR / "src" / "multi_bot"
if str(SRC_MULTI_BOT) not in sys.path:
    sys.path.insert(0, str(SRC_MULTI_BOT))

if "dotenv" not in sys.modules:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv_stub

if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.ModuleType("websockets")

if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    sys.modules["aiohttp"] = aiohttp_stub

if "ccxt" not in sys.modules:
    ccxt_stub = types.ModuleType("ccxt")

    class _BaseError(Exception):
        pass

    class _OrderNotFound(_BaseError):
        pass

    class _Binance:
        def __init__(self, *args, **kwargs):
            pass

    ccxt_stub.BaseError = _BaseError
    ccxt_stub.OrderNotFound = _OrderNotFound
    ccxt_stub.binance = _Binance
    sys.modules["ccxt"] = ccxt_stub

from binance_multi_bot import BinanceGridBot


def build_bot(config=None):
    bot = BinanceGridBot.__new__(BinanceGridBot)
    bot.symbol = "BTCUSDT"
    bot.config = config or {}
    bot.grid_spacing = float(bot.config.get("grid_spacing", 0.006))
    bot.initial_quantity = float(bot.config.get("initial_quantity", 0.0005))
    bot.best_bid_price = 100.0
    bot.best_ask_price = 100.2
    bot.latest_price = 100.1
    bot.price_precision = 1
    bot.amount_precision = 4
    bot.min_order_amount = 0.0001
    bot.min_order_notional = 50.0
    bot._price_window = deque(maxlen=int(bot.config.get("range_filter_lookback", 10)))
    bot._market_guard_paused = False
    bot._market_guard_reason = None
    bot._market_guard_pause_until_ts = 0.0
    bot._market_guard_last_transition_ts = 0.0
    bot._market_guard_last_notification_state = None
    bot._market_guard_last_log_ts = 0.0
    bot._market_guard_static_block = False
    bot._market_guard_static_reason = None
    bot._market_guard_static_block_ts = 0.0
    bot._open_orders_last_failure_ts = 0.0
    bot._day_fuse_on = False
    bot._emg_in_progress = False
    bot._grid_pause_until_ts = 0.0
    bot.position_limit = 2.0
    bot.short_position = 0.0
    return bot


class FeeAwareGuardTests(unittest.TestCase):
    def test_fee_floor_blocks_tight_grid(self):
        bot = build_bot(
            {
                "grid_spacing": 0.001,
                "maker_fee_rate": 0.0002,
                "taker_fee_rate": 0.0005,
                "funding_buffer_rate": 0.0003,
                "min_net_edge_rate": 0.0010,
                "max_spread_rate": 0.0010,
                "range_filter_lookback": 10,
                "range_min_samples": 5,
                "range_min_pct": 0.0020,
                "range_breakout_pct": 0.0080,
                "range_pause_seconds": 300,
            }
        )

        bot._init_market_guard()

        self.assertGreater(bot.min_profitable_grid_spacing, bot.grid_spacing)
        self.assertTrue(bot._market_guard_static_block)
        self.assertFalse(bot._can_open_new_entries())

    def test_compressed_range_pauses_entries(self):
        bot = build_bot(
            {
                "grid_spacing": 0.01,
                "maker_fee_rate": 0.0002,
                "taker_fee_rate": 0.0005,
                "funding_buffer_rate": 0.0003,
                "min_net_edge_rate": 0.0010,
                "max_spread_rate": 0.0010,
                "range_filter_lookback": 10,
                "range_min_samples": 5,
                "range_min_pct": 0.0020,
                "range_breakout_pct": 0.0080,
                "range_pause_seconds": 300,
            }
        )

        bot._init_market_guard()
        bot._price_window = deque([100.0, 100.01, 100.0, 100.02, 100.01], maxlen=10)

        transition = bot._update_market_guard_state(bid=99.99, ask=100.01)

        self.assertTrue(transition["changed"])
        self.assertTrue(transition["paused"])
        self.assertEqual(transition["reason"], "range_too_compressed")
        self.assertFalse(bot._can_open_new_entries())

    def test_breakout_pauses_then_recovers(self):
        bot = build_bot(
            {
                "grid_spacing": 0.01,
                "maker_fee_rate": 0.0002,
                "taker_fee_rate": 0.0005,
                "funding_buffer_rate": 0.0003,
                "min_net_edge_rate": 0.0010,
                "max_spread_rate": 0.0010,
                "range_filter_lookback": 10,
                "range_min_samples": 5,
                "range_min_pct": 0.0020,
                "range_breakout_pct": 0.0080,
                "range_pause_seconds": 1,
            }
        )

        bot._init_market_guard()
        bot._price_window = deque([100.0, 100.01, 100.0, 100.02, 101.5], maxlen=10)

        paused_transition = bot._update_market_guard_state(bid=101.49, ask=101.51)
        self.assertTrue(paused_transition["changed"])
        self.assertTrue(paused_transition["paused"])
        self.assertEqual(paused_transition["reason"], "directional_breakout")

        bot._price_window = deque([100.0, 100.21, 100.08, 100.19, 100.05], maxlen=10)
        bot._market_guard_pause_until_ts = 0.0

        resumed_transition = bot._update_market_guard_state(bid=99.99, ask=100.01)
        self.assertTrue(resumed_transition["changed"])
        self.assertFalse(resumed_transition["paused"])
        self.assertIsNone(resumed_transition["reason"])
        self.assertTrue(bot._can_open_new_entries())

    def test_maker_price_guard_keeps_orders_off_the_spread(self):
        bot = build_bot({"grid_spacing": 0.01})
        bot.price_precision = 1
        bot.best_bid_price = 100.0
        bot.best_ask_price = 100.2

        adjusted_buy = bot._adjust_limit_price_for_maker("buy", 100.2, False)
        adjusted_sell = bot._adjust_limit_price_for_maker("sell", 100.0, False)

        self.assertLess(adjusted_buy, bot.best_bid_price)
        self.assertGreater(adjusted_sell, bot.best_ask_price)


class FeeAwareGuardAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_market_guard_keeps_existing_take_profit_unchanged(self):
        bot = build_bot(
            {
                "grid_spacing": 0.01,
                "maker_fee_rate": 0.0002,
                "taker_fee_rate": 0.0005,
                "funding_buffer_rate": 0.0003,
                "min_net_edge_rate": 0.0010,
                "max_spread_rate": 0.0010,
                "range_filter_lookback": 10,
                "range_min_samples": 5,
                "range_min_pct": 0.0020,
                "range_breakout_pct": 0.0080,
                "range_pause_seconds": 300,
            }
        )
        bot._init_market_guard()
        bot.long_position = 0.05
        bot.position_threshold = 1.0
        bot.lockdown_mode = {
            "long": {"active": False, "tp_price": None, "lockdown_price": None, "r": None, "exited_at": None},
            "short": {"active": False, "tp_price": None, "lockdown_price": None, "r": None, "exited_at": None},
        }
        bot.long_initial_quantity = 0.0005
        bot.upper_price_long = 101.0
        bot.lower_price_long = 99.0
        bot.last_long_order_time = 0.0

        called = {"tp": False, "entry": False}

        def _fail_cancel(*args, **kwargs):
            raise AssertionError("entry cancellation should not run while entries are paused")

        def _fake_existing_tp(side):
            return {"id": "tp-1", "price": "101.0", "side": "sell", "info": {"positionSide": "LONG"}}

        def _fake_ensure_tp(*args, **kwargs):
            called["tp"] = True
            return True

        def _fake_place_order(*args, **kwargs):
            called["entry"] = True
            return {"id": "entry-1"}

        bot._cancel_open_orders_for_side = _fail_cancel
        bot._get_existing_tp_order = _fake_existing_tp
        bot._ensure_take_profit_at = _fake_ensure_tp
        bot._place_order = _fake_place_order

        await bot._place_long_orders(100.0, allow_new_entries=False)

        self.assertFalse(called["tp"])
        self.assertFalse(called["entry"])

    async def test_take_profit_quantity_is_clamped_to_live_position(self):
        bot = build_bot({"grid_spacing": 0.01})
        bot.long_position = 0.0008
        bot.short_position = 0.0008
        bot.min_order_amount = 0.0001
        bot.amount_precision = 4
        bot.price_precision = 1

        captured = {}

        class _Exchange:
            def fetch_open_orders(self, symbol):
                return []

            def create_order(self, symbol, type_, side, amount, price, params):
                captured["symbol"] = symbol
                captured["type"] = type_
                captured["side"] = side
                captured["amount"] = amount
                captured["price"] = price
                captured["params"] = params
                return {"id": "tp-1"}

        bot.exchange = _Exchange()

        bot._place_take_profit_order(bot.symbol, "long", 101.0, 0.001)

        self.assertEqual(captured["amount"], 0.0008)
        self.assertEqual(captured["side"], "sell")
        self.assertEqual(captured["params"]["positionSide"], "LONG")

    async def test_take_profit_is_skipped_when_live_position_is_too_small(self):
        bot = build_bot({"grid_spacing": 0.01})
        bot.long_position = 0.00005
        bot.short_position = 0.00005
        bot.min_order_amount = 0.0001
        bot.amount_precision = 4
        bot.price_precision = 1

        called = {"create_order": False}

        class _Exchange:
            def fetch_open_orders(self, symbol):
                return []

            def create_order(self, *args, **kwargs):
                called["create_order"] = True
                return {"id": "tp-1"}

        bot.exchange = _Exchange()

        bot._place_take_profit_order(bot.symbol, "long", 101.0, 0.001)

        self.assertFalse(called["create_order"])


if __name__ == "__main__":
    unittest.main()
