import asyncio
import websockets
import json
import logging
import hmac
import hashlib
import time
import ccxt
import math
from collections import deque
from decimal import Decimal, ROUND_HALF_UP
import os
from dotenv import load_dotenv
from typing import Optional
import aiohttp

# Load environment variables
load_dotenv()

# Telegram notification configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ENABLE_NOTIFICATIONS = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
NOTIFICATION_INTERVAL = int(os.getenv("NOTIFICATION_INTERVAL", "3600"))

# Fixed configuration
WEBSOCKET_URL = "wss://fstream.binance.com/ws"
ORDER_COOLDOWN_TIME = 60
SYNC_TIME = 3
ORDER_FIRST_TIME = 1

# Use the optimized logging configuration
try:
    from logging_config import setup_binance_multi_bot_logging, ThresholdStateLogger
    logger = setup_binance_multi_bot_logging()
    threshold_logger = ThresholdStateLogger(logger)
except ImportError:
    # Fall back to the default configuration if the import fails
    os.makedirs("log", exist_ok=True)
    import inspect
    import sys
    
    # Walk the stack to find the caller
    log_filename = None
    for frame_info in inspect.stack():
        frame = frame_info.frame
        filename = frame.f_globals.get('__file__', '')
        if filename and 'single_bot' in filename and 'binance_bot.py' in filename:
            log_filename = "binance_single_bot.log"
            break

    if not log_filename:
        script_name = os.path.splitext(os.path.basename(__file__))[0]
        log_filename = f"{script_name}.log"

    handlers = [logging.StreamHandler()]
    try:
        file_handler = logging.FileHandler(f"log/{log_filename}")
        handlers.append(file_handler)
        print(f"Logs will be written to: log/{log_filename}")
    except PermissionError as e:
        print(f"Warning: unable to create log file (permission denied): {e}")
        print("Logs will be written to the console only")
    except Exception as e:
        print(f"Warning: unable to create log file: {e}")
        print("Logs will be written to the console only")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    logger = logging.getLogger()
    threshold_logger = None


class CustomBinance(ccxt.binance):
    def fetch(self, url, method='GET', headers=None, body=None):
        if headers is None:
            headers = {}
        return super().fetch(url, method, headers, body)


class BinanceGridBot:
    # ===== Lockdown persistence & fixed-r utilities =====
    _state_lock = None

    def _ensure_state_lock(self):
        import threading
        if self._state_lock is None:
            self._state_lock = threading.Lock()

    def _state_dir(self):
        # Return absolute state directory path; uses STATE_DIR env or module dir/state.
        from pathlib import Path
        import os
        base = os.environ.get("STATE_DIR")
        if base:
            p = Path(base).resolve()
        else:
            p = Path(__file__).resolve().parent / "state"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _state_file_path(self):
        from pathlib import Path
        safe_symbol = str(self.symbol).replace("/", "_")
        return self._state_dir() / f"lockdown_{safe_symbol}.json"

    def _atomic_write_json(self, path, data: dict):
        # Write JSON atomically to avoid partial writes; fsync to ensure flush.
        import json, os, tempfile
        from pathlib import Path
        path = Path(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        b = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(b)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _persist_lockdown_state(self):
        # Persist current lockdown_mode for both sides with lock/r/tp and exited_at.
        try:
            self._ensure_state_lock()
            long = self.lockdown_mode.get('long', {})
            short = self.lockdown_mode.get('short', {})
            data = {
                "long": {
                    "active": bool(long.get("active")),
                    "lockdown_price": long.get("lockdown_price"),
                    "tp_price": long.get("tp_price"),
                    "r": long.get("r"),
                    "exited_at": long.get("exited_at"),
                },
                "short": {
                    "active": bool(short.get("active")),
                    "lockdown_price": short.get("lockdown_price"),
                    "tp_price": short.get("tp_price"),
                    "r": short.get("r"),
                    "exited_at": short.get("exited_at"),
                },
                "updated_at": time.time(),
            }
            path = self._state_file_path()
            with self._state_lock:
                self._atomic_write_json(path, data)
            logger.info(f"Wrote lockdown state file: {path} => {data}")
        except Exception as e:
            logger.error(f"Failed to write lockdown state: {e}", exc_info=True)

    def _fixed_r(self):
        # Return fixed r to use for lockdown. Prefers config['lockdown_fixed_r'] or config['fixed_r'].
        r = None
        try:
            r = float(self.config.get("lockdown_fixed_r", self.config.get("fixed_r", None)))
        except Exception:
            r = None
        if not r or r <= 1.0:
            # fallback to dynamic compute once
            try:
                r = float(self._compute_tp_multiplier('long'))
            except Exception:
                r = 1.015
            r = max(1.001, r)
        return r

    def _restore_lockdown_from_local(self):
        # Restore lockdown state from local file only. If r/tp missing, fill using fixed r and persist.
        path = self._state_file_path()
        if not os.path.exists(path):
            logger.info(f"No lockdown state file found: {path}")
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"Successfully read lockdown state file: {path}, data: {data}")
            changed = False
            for side in ("long","short"):
                pos = self.long_position if side=="long" else self.short_position
                if pos is None or self.position_threshold is None:
                    continue
                sd = data.get(side, {}) or {}
                active = bool(sd.get("active"))
                lock = sd.get("lockdown_price")
                r = sd.get("r")
                tp = sd.get("tp_price")
                exited_at = sd.get("exited_at")
                if active and (lock is not None) and (pos > self.position_threshold):
                    if not r or r <= 1.0:
                        r = self._fixed_r(); changed = True
                    if tp is None:
                        tp = (lock * r) if side=="long" else (lock / r); changed = True
                    self.lockdown_mode[side]['active'] = True
                    self.lockdown_mode[side]['lockdown_price'] = float(lock)
                    self.lockdown_mode[side]['r'] = float(r)
                    self.lockdown_mode[side]['tp_price'] = float(tp)
                    self.lockdown_mode[side]['exited_at'] = exited_at
                else:
                    # keep last anchor for potential reuse
                    if lock is not None:
                        self.lockdown_mode[side]['lockdown_price'] = float(lock)
                    if r:
                        self.lockdown_mode[side]['r'] = float(r)
                    if tp:
                        self.lockdown_mode[side]['tp_price'] = float(tp)
                    self.lockdown_mode[side]['exited_at'] = exited_at
            if changed:
                self._persist_lockdown_state()
        except Exception as e:
            logger.error(f"Failed to read lockdown state: {e} @ {path}", exc_info=True)

    def _should_reuse_lock(self, side: str) -> bool:
        # Decide whether to reuse previous lockdown anchor upon re-entry (sticky).
        try:
            m = self.lockdown_mode.get(side, {})
            if not m or m.get("active"):
                return False
            lock = m.get("lockdown_price")
            r = m.get("r")
            tp = m.get("tp_price")
            if lock is None or r is None or tp is None:
                return False
            exited_at = m.get("exited_at") or 0
            now = time.time()
            reuse_window = float(self.config.get("lockdown_reuse_window_sec", 1800))
            max_age_hrs = float(self.config.get("lockdown_reuse_max_age_hours", 6))
            if now - exited_at > max_age_hrs*3600:
                return False
            grid = float(self.grid_spacing or 0)
            band_mult = float(self.config.get("lockdown_reuse_price_band_mult", 1.5))
            if grid and abs(self.latest_price - lock) > band_mult * grid:
                return False
            return (now - exited_at) <= reuse_window
        except Exception:
            return False

    def _enter_lockdown_fixed_r(self, side: str):
        # Enter lockdown using fixed r; reuse previous anchor if eligible; persist state.
        if self._should_reuse_lock(side):
            lock = float(self.lockdown_mode[side]['lockdown_price'])
            r = float(self.lockdown_mode[side]['r'])
            tp = (lock * r) if side=='long' else (lock / r)
            self.lockdown_mode[side].update({
                'active': True, 'tp_price': tp, 'exited_at': None
            })
            logger.info(f"{side} re-entered lockdown: reusing previous anchor lock={lock}, r={r}, tp={tp}")
            self._persist_lockdown_state()
            return lock, r, tp

        lock = float(self.latest_price)  # or your baseline price
        r = float(self.config.get("lockdown_fixed_r", self.config.get("fixed_r", 0)) or 0)
        if not r or r <= 1.0:
            r = self._fixed_r()
        tp = (lock * r) if side=='long' else (lock / r)
        self.lockdown_mode[side].update({
            'active': True, 'lockdown_price': lock, 'r': r, 'tp_price': tp, 'exited_at': None
        })
        logger.info(f"{side} entered lockdown: new anchor lock={lock}, r={r}, tp={tp}")
        self._persist_lockdown_state()
        return lock, r, tp

    def _exit_lockdown_fixed(self, side: str, reason: str = ""):
        # Exit lockdown but keep last anchor for potential short-term reuse; persist.
        try:
            m = self.lockdown_mode.get(side, {})
            if not m.get('active'):
                return
            m['active'] = False
            m['exited_at'] = time.time()
            self._persist_lockdown_state()
            logger.info(f"{side} exited lockdown ({reason}); keeping the last anchor for short-term reuse")
        except Exception as e:
            logger.error(f"Failed to persist lockdown exit: {e}", exc_info=True)

    def _config_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except Exception:
            return float(default)

    def _config_int(self, key: str, default: int) -> int:
        try:
            return int(float(self.config.get(key, default)))
        except Exception:
            return int(default)

    def _compute_min_profitable_grid_spacing(self) -> float:
        """Estimate the minimum grid spacing needed to cover fees, funding, and a small profit buffer."""
        maker_fee = self._config_float("maker_fee_rate", 0.0002)
        taker_fee = self._config_float("taker_fee_rate", 0.0005)
        funding_buffer = self._config_float("funding_buffer_rate", 0.0003)
        min_net_edge = self._config_float("min_net_edge_rate", 0.0010)
        round_trip_fee_floor = max(2.0 * maker_fee, 2.0 * taker_fee)
        return round_trip_fee_floor + funding_buffer + min_net_edge

    def _init_market_guard(self):
        """Initialize fee-aware range guard settings and runtime state."""
        self.maker_fee_rate = self._config_float("maker_fee_rate", 0.0002)
        self.taker_fee_rate = self._config_float("taker_fee_rate", 0.0005)
        self.funding_buffer_rate = self._config_float("funding_buffer_rate", 0.0003)
        self.min_net_edge_rate = self._config_float("min_net_edge_rate", 0.0010)
        self.max_spread_rate = self._config_float("max_spread_rate", 0.0010)
        self.range_filter_lookback = max(1, self._config_int("range_filter_lookback", 60))
        self.range_min_samples = min(
            self.range_filter_lookback,
            max(1, self._config_int("range_min_samples", max(20, self.range_filter_lookback // 3))),
        )
        self.range_pause_seconds = max(1, self._config_int("range_pause_seconds", 300))
        self.min_compression_range_rate = self._config_float(
            "range_min_pct",
            max(self._compute_min_profitable_grid_spacing() * 2.0, self.grid_spacing * 0.5, 0.002),
        )
        self.breakout_range_rate = self._config_float(
            "range_breakout_pct",
            max(self.grid_spacing * 2.5, self._compute_min_profitable_grid_spacing() * 4.0, 0.008),
        )
        self.min_profitable_grid_spacing = self._compute_min_profitable_grid_spacing()
        self._market_guard_static_block = self.grid_spacing < self.min_profitable_grid_spacing
        self._market_guard_static_reason = "grid_too_tight" if self._market_guard_static_block else None
        self._market_guard_static_block_ts = time.time() if self._market_guard_static_block else 0.0
        self._price_window = deque(maxlen=self.range_filter_lookback)
        self._market_guard_paused = False
        self._market_guard_reason = None
        self._market_guard_pause_until_ts = 0.0
        self._market_guard_last_transition_ts = 0.0
        self._market_guard_last_notification_state = None
        self._market_guard_last_log_ts = 0.0

    def _log_market_guard_startup(self):
        """Log the computed profitability floor and the configured guard thresholds."""
        logger.info(
            "Fee-aware guard initialized: min profitable grid spacing %.3f%%, configured grid spacing %.3f%%, "
            "range floor %.3f%%, breakout %.3f%%, max spread %.3f%%",
            self.min_profitable_grid_spacing * 100.0,
            self.grid_spacing * 100.0,
            self.min_compression_range_rate * 100.0,
            self.breakout_range_rate * 100.0,
            self.max_spread_rate * 100.0,
        )
        if self._market_guard_static_block:
            logger.warning(
                "Configured grid spacing %.3f%% is below the fee-aware floor %.3f%%; new entries will stay paused until the grid is widened",
                self.grid_spacing * 100.0,
                self.min_profitable_grid_spacing * 100.0,
            )

    def _append_price_sample(self, price: float):
        """Append a price sample for the range filter."""
        try:
            if price is not None and float(price) > 0:
                self._price_window.append(float(price))
        except Exception:
            pass

    def _calculate_market_regime_metrics(self, bid=None, ask=None):
        """Calculate recent range and trend metrics from the rolling price window."""
        prices = [float(p) for p in self._price_window if p is not None and float(p) > 0]
        metrics = {
            "sample_count": len(prices),
            "range_pct": 0.0,
            "direction_pct": 0.0,
            "spread_pct": None,
            "high": None,
            "low": None,
            "first": None,
            "last": None,
            "mid": None,
        }

        if not prices:
            return metrics

        high = max(prices)
        low = min(prices)
        mid = (high + low) / 2.0 if (high + low) else 0.0
        first = prices[0]
        last = prices[-1]

        metrics.update({
            "high": high,
            "low": low,
            "first": first,
            "last": last,
            "mid": mid,
            "range_pct": ((high - low) / mid) if mid else 0.0,
            "direction_pct": (abs(last - first) / mid) if mid else 0.0,
        })

        try:
            if bid is not None and ask is not None and mid:
                bid = float(bid)
                ask = float(ask)
                if ask > bid:
                    metrics["spread_pct"] = (ask - bid) / mid
        except Exception:
            metrics["spread_pct"] = None

        return metrics

    def _update_market_guard_state(self, price=None, bid=None, ask=None):
        """Update the market-regime pause state and return transition metadata."""
        if self._market_guard_static_block:
            return {
                "changed": False,
                "paused": True,
                "reason": self._market_guard_static_reason,
                "warming_up": False,
                "metrics": {
                    "sample_count": len(self._price_window),
                    "range_pct": 0.0,
                    "direction_pct": 0.0,
                    "spread_pct": None,
                    "high": None,
                    "low": None,
                    "first": None,
                    "last": None,
                    "mid": None,
                },
            }

        if price is not None:
            self._append_price_sample(price)

        metrics = self._calculate_market_regime_metrics(bid=bid, ask=ask)
        now = time.time()

        if metrics["sample_count"] < self.range_min_samples:
            return {
                "changed": False,
                "paused": self._market_guard_paused,
                "reason": self._market_guard_reason,
                "warming_up": True,
                "metrics": metrics,
            }

        reasons = []
        spread_pct = metrics.get("spread_pct")
        if spread_pct is not None and spread_pct > self.max_spread_rate:
            reasons.append("spread_too_wide")

        if metrics["range_pct"] < self.min_compression_range_rate:
            reasons.append("range_too_compressed")

        if metrics["direction_pct"] > self.breakout_range_rate:
            reasons.append("directional_breakout")

        changed = False
        reason = ",".join(reasons) if reasons else None

        if reasons:
            if (not self._market_guard_paused) or (reason != self._market_guard_reason):
                changed = True
                self._market_guard_paused = True
                self._market_guard_reason = reason
                self._market_guard_pause_until_ts = now + self.range_pause_seconds
                self._market_guard_last_transition_ts = now
            else:
                self._market_guard_pause_until_ts = max(
                    self._market_guard_pause_until_ts,
                    now + self.range_pause_seconds,
                )
        else:
            if self._market_guard_paused and now >= self._market_guard_pause_until_ts:
                changed = True
                self._market_guard_paused = False
                self._market_guard_reason = None
                self._market_guard_last_transition_ts = now

        return {
            "changed": changed,
            "paused": self._market_guard_paused,
            "reason": self._market_guard_reason,
            "warming_up": False,
            "metrics": metrics,
        }

    def _can_open_new_entries(self) -> bool:
        """Return True when new entries are allowed."""
        return (
            not self._day_fuse_on
            and not self._emg_in_progress
            and time.time() >= self._grid_pause_until_ts
            and not self._market_guard_paused
            and not self._market_guard_static_block
        )

    def _log_pending_entry_status(self, side: str, remaining: float):
        """Throttle logs for pending initial entries."""
        last_log_ts = self._pending_entry_log_ts.get(side, 0.0)
        now = time.time()
        if now - last_log_ts >= 15:
            self._pending_entry_log_ts[side] = now
            logger.info(f"Skipping {side} entry because a previous order is still pending ({remaining:.0f}s remaining)")

    async def _notify_market_guard_transition(self, transition: dict):
        """Send a Telegram notification when the fee/range guard changes state."""
        if not transition or not transition.get("changed"):
            return

        paused = transition.get("paused", False)
        reason = transition.get("reason") or "unknown"
        metrics = transition.get("metrics", {})
        range_pct = metrics.get("range_pct", 0.0) * 100.0
        direction_pct = metrics.get("direction_pct", 0.0) * 100.0
        spread_pct = metrics.get("spread_pct")
        spread_text = f"{spread_pct * 100.0:.3f}%" if spread_pct is not None else "n/a"

        if paused:
            message = f"""
⏸️ **New entries paused**

📊 **Market guard**
• Symbol: {self.symbol}
• Reason: {reason}
• Recent range: {range_pct:.3f}%
• Directional move: {direction_pct:.3f}%
• Spread: {spread_text}

🛡️ **Protection**
• Fees, funding, or breakout conditions no longer support fresh grid entries
• Existing reduce-only exits remain active
"""
        else:
            message = f"""
▶️ **New entries resumed**

📊 **Market guard**
• Symbol: {self.symbol}
• Reason cleared: {reason}
• Recent range: {range_pct:.3f}%
• Directional move: {direction_pct:.3f}%
• Spread: {spread_text}

✅ **Trading state**
• Market conditions are back inside the configured guard rails
"""

        await self._send_telegram_message(message, urgent=paused, silent=not paused)

    def __init__(self, symbol, api_key, api_secret, config):
        """
        初始化 BinanceGridBot
        
        Args:
            symbol: 交易对符号 (如 "XRPUSDT")
            api_key: API密钥
            api_secret: API密钥
            config: 配置字典，包含以下键:
                - grid_spacing: 网格间距
                - initial_quantity: 初始交易数量
                - leverage: 杠杆倍数
                - contract_type: 合约类型 (USDT/USDC)
        """
        self.symbol = symbol
        self.api_key = api_key
        self.api_secret = api_secret
        self.config = config
        self.binance_sandbox = os.getenv("BINANCE_SANDBOX", "false").lower() == "true"
        
        # 从配置中提取参数
        self.grid_spacing = config.get('grid_spacing', 0.001)
        self.initial_quantity = config.get('initial_quantity', 3)
        self.leverage = config.get('leverage', 20)
        self.contract_type = config.get('contract_type', 'USDT')
        self.ccxt_symbol = f"{symbol.replace('USDT', '').replace('USDC', '')}/{self.contract_type}:{self.contract_type}"
        
        # 计算阈值
        self.position_threshold_factor = float(self.config.get('position_threshold_factor', 10))
        self.position_limit_factor = float(self.config.get('position_limit_factor', 5))
        self.position_threshold = self.position_threshold_factor * self.initial_quantity / self.grid_spacing * 2 / 100
        self.position_limit = self.position_limit_factor * self.initial_quantity / self.grid_spacing * 2 / 100
        self._market_guard_static_block = False
        self._market_guard_static_reason = None
        
        # 初始化交易所
        self.exchange = self._init_exchange()
        
        # 获取价格精度
        self._get_price_precision()
        
        # 初始化状态变量
        # === 紧急减仓配置与状态（Simple Plan, Fixed Quantity） ===
        self.emg_enter_ratio = float(self.config.get('emg_enter_ratio', 0.80))
        self.emg_exit_ratio  = float(self.config.get('emg_exit_ratio', 0.75))
        self.enable_dynamic_enter_075 = bool(self.config.get('enable_dynamic_enter_075', True))
        self.emg_cooldown_s  = int(self.config.get('emg_cooldown_s', 60))
        self.grid_pause_after_emg_s = int(self.config.get('grid_pause_after_emg_s', 90))
        self.emg_batches     = int(self.config.get('emg_batches', 2))
        self.emg_batch_sleep_ms = int(self.config.get('emg_batch_sleep_ms', 300))
        self.emg_slip_cap_bp = int(self.config.get('emg_slip_cap_bp', 15))
        self.emg_daily_fuse_count = int(self.config.get('emg_daily_fuse_count', 3))

        self._emg_last_ts = 0.0
        self._emg_in_progress = False
        self._emg_trigger_count_today = 0
        self._grid_pause_until_ts = 0.0
        self._day_fuse_on = False
        self._emg_day = time.strftime('%Y-%m-%d')



        self._vol_prices = deque(maxlen=60)

        self.long_initial_quantity = 0
        self.short_initial_quantity = 0
        self.long_position = 0
        self.short_position = 0
        self.last_long_order_time = 0
        self.last_short_order_time = 0
        self.long_entry_pending = False
        self.short_entry_pending = False
        self.entry_pending_timeout_s = int(self.config.get('entry_pending_timeout_s', 180))
        self.open_orders_backoff_s = int(self.config.get('open_orders_backoff_s', 30))
        self._open_orders_last_failure_ts = 0.0
        self._order_sync_skip_log_ts = 0.0
        self._last_no_position_log_ts = 0.0
        self._pending_entry_log_ts = {'long': 0.0, 'short': 0.0}
        self._market_guard_last_cleanup_ts = 0.0
        self.buy_long_orders = 0.0
        self.sell_long_orders = 0.0
        self.sell_short_orders = 0.0
        self.buy_short_orders = 0.0
        self.last_position_update_time = 0
        self.last_orders_update_time = 0
        self.last_ticker_update_time = 0
        self.latest_price = 0
        self.best_bid_price = None
        self.best_ask_price = None
        self.balance = {}
        self.mid_price_long = 0
        self.lower_price_long = 0
        self.upper_price_long = 0
        self.mid_price_short = 0
        self.lower_price_short = 0
        self.upper_price_short = 0
        self.listenKey = self._get_listen_key()
        
        # 检查持仓模式
        self._check_and_enable_hedge_mode()
        
        # Telegram通知相关变量
        self.last_summary_time = 0
        self.startup_notified = False
        self.last_balance = None
        
        # 紧急通知状态跟踪
        self.long_threshold_alerted = False
        self.short_threshold_alerted = False
        self.risk_reduction_alerted = False
        
        # 双倍止盈止损通知状态跟踪
        self.long_double_profit_alerted = False
        self.short_double_profit_alerted = False

        # Fee-aware range guard configuration and state
        self._init_market_guard()
        
        # 初始化异步锁（延迟创建，避免在没有事件循环时创建）
        self.lock = None
        
        # 运行状态
        self.running = False
        
        # 装死模式状态记录（新增）
        self.lockdown_mode = {
            'long': {'active': False, 'tp_price': None, 'lockdown_price': None, 'r': None, 'exited_at': None},
            'short': {'active': False, 'tp_price': None, 'lockdown_price': None, 'r': None, 'exited_at': None}
        }

    def _init_exchange(self):
        """初始化交易所 API"""
        use_demo_trading = os.getenv("BINANCE_DEMO_TRADING", os.getenv("BINANCE_SANDBOX", "false")).lower() == "true"
        exchange = CustomBinance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "options": {
                "defaultType": "future",
            },
        })
        if use_demo_trading:
            try:
                if hasattr(exchange, "enable_demo_trading"):
                    exchange.enable_demo_trading(True)
                elif hasattr(exchange, "enableDemoTrading"):
                    exchange.enableDemoTrading(True)
                else:
                    raise AttributeError("CCXT version does not support Binance demo trading helpers")
                logger.info("Binance Demo Trading mode enabled")
            except Exception as e:
                logger.error(
                    "Failed to enable Binance Demo Trading mode. For futures, CCXT no longer supports sandbox/testnet; "
                    "you need Binance Demo Trading API keys and a recent CCXT build.",
                    exc_info=True,
                )
                raise e
        exchange.load_markets(reload=False)
        try:
            exchange.set_leverage(self.leverage, self.ccxt_symbol)
            logger.info(f"Leverage set to {self.leverage}x for {self.ccxt_symbol}")
        except AttributeError:
            try:
                exchange.setLeverage(self.leverage, self.ccxt_symbol)
                logger.info(f"Leverage set to {self.leverage}x for {self.ccxt_symbol}")
            except Exception as e:
                logger.warning(f"Unable to set leverage automatically: {e}")
        except Exception as e:
            logger.warning(f"Unable to set leverage automatically: {e}")
        return exchange

    def _get_price_precision(self):
        """获取交易对的价格精度、数量精度和最小下单数量"""
        markets = self.exchange.fetch_markets()
        symbol_info = next(market for market in markets if market["symbol"] == self.ccxt_symbol)

        # 获取价格精度
        price_precision = symbol_info["precision"]["price"]
        if isinstance(price_precision, float):
            self.price_precision = int(abs(math.log10(price_precision)))
        elif isinstance(price_precision, int):
            self.price_precision = price_precision
        else:
            raise ValueError(f"未知的价格精度类型: {price_precision}")

        # 获取数量精度
        amount_precision = symbol_info["precision"]["amount"]
        if isinstance(amount_precision, float):
            self.amount_precision = int(abs(math.log10(amount_precision)))
        elif isinstance(amount_precision, int):
            self.amount_precision = amount_precision
        else:
            raise ValueError(f"未知的数量精度类型: {amount_precision}")

        # 获取最小下单数量
        self.min_order_amount = symbol_info["limits"]["amount"]["min"]
        cost_limits = symbol_info.get("limits", {}).get("cost", {}) or {}
        self.min_order_notional = cost_limits.get("min") or 50.0

        logger.info(
            f"Price precision: {self.price_precision}, amount precision: {self.amount_precision}, minimum order size: {self.min_order_amount}, minimum notional: {self.min_order_notional}")

    def _round_quantity_up(self, quantity: float) -> float:
        """Round quantity up to the configured amount precision."""
        factor = 10 ** self.amount_precision
        return math.ceil(float(quantity) * factor) / factor

    def _get_entry_quantity(self, price, requested_quantity: float) -> float:
        """Return a Binance-compliant entry quantity for non-reduce-only orders."""
        quantity = max(float(requested_quantity), float(self.min_order_amount))

        reference_price = None
        try:
            if price is not None:
                reference_price = float(price)
            elif self.latest_price:
                reference_price = float(self.latest_price)
            elif self.best_bid_price:
                reference_price = float(self.best_bid_price)
            elif self.best_ask_price:
                reference_price = float(self.best_ask_price)
        except Exception:
            reference_price = None

        if reference_price and reference_price > 0:
            min_quantity_by_notional = float(self.min_order_notional) / reference_price
            quantity = max(quantity, min_quantity_by_notional)

        adjusted_quantity = self._round_quantity_up(quantity)
        if adjusted_quantity > requested_quantity:
            logger.warning(
                f"Entry quantity increased to meet Binance minimums: requested={requested_quantity}, adjusted={adjusted_quantity}, "
                f"min_amount={self.min_order_amount}, min_notional={self.min_order_notional}")
        return adjusted_quantity

    def _get_reduce_only_quantity(self, side: str, requested_quantity: float) -> Optional[float]:
        """Clamp reduce-only quantity to the live position so Binance will accept it."""
        live_position = self.long_position if side == 'long' else self.short_position
        if live_position is None:
            return None

        try:
            live_position = abs(float(live_position))
            requested_quantity = float(requested_quantity)
        except Exception:
            return None

        if live_position <= 0:
            return None

        if live_position < self.min_order_amount:
            logger.warning(
                f"Skipping {side} reduce-only order because the live position {live_position} is below the Binance minimum amount "
                f"{self.min_order_amount}")
            return None

        quantity = min(requested_quantity, live_position)
        quantity = round(quantity, self.amount_precision)

        return quantity

    def _get_position(self):
        """获取当前持仓"""
        params = {
            'type': 'future'
        }
        positions = self.exchange.fetch_positions(params=params)
        long_position = 0
        short_position = 0

        for position in positions:
            if position['symbol'] == self.ccxt_symbol:
                contracts = position.get('contracts', 0)
                side = position.get('side', None)

                if side == 'long':
                    long_position = contracts
                elif side == 'short':
                    short_position = abs(contracts)

        if long_position == 0 and short_position == 0:
            return 0, 0

        return long_position, short_position

    def _get_listen_key(self):
        """Fetch listenKey"""
        try:
            response = self.exchange.fapiPrivatePostListenKey()
            listenKey = response.get("listenKey")
            if not listenKey:
                raise ValueError("listenKey is empty")
            logger.info(f"Successfully obtained listenKey: {listenKey}")
            return listenKey
        except Exception as e:
            logger.error(f"Failed to obtain listenKey: {e}")
            raise e

    def _check_and_enable_hedge_mode(self):
        """Check and enable hedge mode"""
        try:
            try:
                position_mode = self.exchange.fetch_position_mode(symbol=self.ccxt_symbol)
                if not position_mode['hedged']:
                    logger.info("Account is not in hedge mode, attempting to enable it automatically...")
                    self._enable_hedge_mode()
                    logger.info("Hedge mode enabled successfully. Continuing.")
                else:
                    logger.info("Hedge mode is already enabled. Continuing.")
            except AttributeError:
                logger.info("Unable to check current position mode, attempting to enable hedge mode...")
                self._enable_hedge_mode()
                logger.info("Hedge mode enabled. Continuing.")
            except Exception as e:
                logger.warning(f"Unexpected error while checking position mode: {e}")
                logger.info("Continuing, but please make sure hedge mode is enabled manually on Binance.")
                
        except Exception as e:
            if "No need to change position side" in str(e):
                logger.info("Hedge mode is already enabled. Continuing.")
            else:
                logger.error(f"Failed to enable hedge mode: {e}")
                logger.error("Please enable hedge mode manually on Binance before running this bot.")
                raise e

    def _enable_hedge_mode(self):
        """Enable hedge mode"""
        try:
            params = {
                'dualSidePosition': 'true',
            }
            response = self.exchange.fapiPrivatePostPositionSideDual(params)
            logger.info(f"Enable hedge mode response: {response}")
        except AttributeError:
            try:
                response = self.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
                logger.info(f"Enable hedge mode response: {response}")
            except Exception as e:
                logger.error(f"Failed to enable hedge mode: {e}")
                logger.error("Please enable hedge mode manually on Binance.")
                raise e
        except Exception as e:
            if "No need to change position side" in str(e):
                logger.info("Hedge mode is already enabled; no change needed.")
                return
            else:
                logger.error(f"Failed to enable hedge mode: {e}")
                logger.error("Please enable hedge mode manually on Binance.")
                raise e

    async def _send_telegram_message(self, message, urgent=False, silent=False):
        """Send a Telegram message"""
        if not ENABLE_NOTIFICATIONS or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return

        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            formatted_message = f"🤖 **{self.symbol} bot** | {timestamp}\n\n{message}"
            
            if urgent:
                formatted_message = f"🚨 **Urgent** 🚨\n\n{formatted_message}"
            elif silent:
                formatted_message = f"🔇 **Scheduled summary** 🔇\n\n{formatted_message}"
            
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": formatted_message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "disable_notification": silent
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    if response.status == 200:
                        notification_type = "silent" if silent else ("urgent" if urgent else "normal")
                    else:
                        logger.warning(f"Telegram message failed: HTTP {response.status}")
                        
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    async def _send_startup_notification(self):
        """Send startup notification"""
        if self.startup_notified:
            return
            
        message = f"""
🚀 **Bot started successfully**

📊 **Trading configuration**
• Symbol: {self.symbol}
• Grid spacing: {self.grid_spacing:.2%}
• Initial quantity: {self.initial_quantity}
• Leverage: {self.leverage}x

🛡️ **Risk controls**
• Lockdown threshold: {self.position_threshold:.2f}
• Position monitoring threshold: {self.position_limit:.2f}

🧮 **Fee-aware guard**
• Minimum profitable grid spacing: {self.min_profitable_grid_spacing:.2%}
• Market regime filter: {'paused' if self._market_guard_paused or self._market_guard_static_block else 'active'}
• Guard reason: {self._market_guard_reason or self._market_guard_static_reason or 'none'}

✅ The bot is running and will trade automatically...
"""
        await self._send_telegram_message(message)
        self.startup_notified = True

    async def _check_and_notify_position_threshold(self, side, position):
        """Check and notify position threshold state"""
        is_over_threshold = position > self.position_threshold
        
        if side == 'long':
            if is_over_threshold and not self.long_threshold_alerted:
                await self._send_threshold_alert(side, position)
                self.long_threshold_alerted = True
            elif not is_over_threshold and self.long_threshold_alerted:
                await self._send_threshold_recovery(side, position)
                self.long_threshold_alerted = False
                
        elif side == 'short':
            if is_over_threshold and not self.short_threshold_alerted:
                await self._send_threshold_alert(side, position)
                self.short_threshold_alerted = True
            elif not is_over_threshold and self.short_threshold_alerted:
                await self._send_threshold_recovery(side, position)
                self.short_threshold_alerted = False
    
    async def _send_threshold_alert(self, side, position):
        """Send position threshold warning"""
        message = f"""
⚠️ **Position risk warning**

📍 **{side.upper()} position exceeded the limit threshold**
• Current {side} position: {position}
• Limit threshold: {self.position_threshold:.2f}
• Latest price: {self.latest_price:.8f}

🛑 **New entries paused until the position drops back down**
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_threshold_recovery(self, side, position):
        """Send position recovery notification"""
        message = f"""
✅ **Position risk cleared**

📍 **{side.upper()} position returned to a safe range**
• Current {side} position: {position}
• Limit threshold: {self.position_threshold:.2f}
• Latest price: {self.latest_price:.8f}

🟢 **Normal entry strategy restored**
"""
        await self._send_telegram_message(message, urgent=False)

    async def _check_and_notify_risk_reduction(self):
        """Check and notify risk reduction state"""
        local_position_threshold = self.position_threshold * 0.8
        both_over_threshold = (self.long_position >= local_position_threshold and 
                              self.short_position >= local_position_threshold)
        
        if both_over_threshold and not self.risk_reduction_alerted:
            await self._send_risk_reduction_alert()
            self.risk_reduction_alerted = True
        elif not both_over_threshold and self.risk_reduction_alerted:
            await self._send_risk_reduction_recovery()
            self.risk_reduction_alerted = False
    
    async def _send_risk_reduction_alert(self):
        """Send risk reduction notification"""
        message = f"""
📉 **Inventory risk control**

⚖️ **Both sides exceeded the threshold; reducing risk**
• Long position: {self.long_position}
• Short position: {self.short_position}
• Threshold: {self.position_threshold * 0.8:.2f}

✅ Partial close executed to reduce risk
"""
        await self._send_telegram_message(message)
    
    async def _send_risk_reduction_recovery(self):
        """Send risk reduction recovery notification"""
        message = f"""
✅ **Inventory risk has eased**

⚖️ **Position status improved**
• Long position: {self.long_position}
• Short position: {self.short_position}
• Monitoring threshold: {self.position_threshold * 0.8:.2f}

🟢 **Inventory risk control cleared**
"""
        await self._send_telegram_message(message)

    async def _check_and_notify_double_profit(self, side, position):
        """Check and notify double take-profit/stop-loss state"""
        is_over_limit = position > self.position_limit
        
        if side == 'long':
            if is_over_limit and not self.long_double_profit_alerted:
                await self._send_double_profit_alert(side, position)
                self.long_double_profit_alerted = True
            elif not is_over_limit and self.long_double_profit_alerted:
                await self._send_double_profit_recovery(side, position)
                self.long_double_profit_alerted = False
                
        elif side == 'short':
            if is_over_limit and not self.short_double_profit_alerted:
                await self._send_double_profit_alert(side, position)
                self.short_double_profit_alerted = True
            elif not is_over_limit and self.short_double_profit_alerted:
                await self._send_double_profit_recovery(side, position)
                self.short_double_profit_alerted = False
    
    async def _send_double_profit_alert(self, side, position):
        """Send double take-profit/stop-loss enabled notification"""
        message = f"""
📈 **Double take-profit/stop-loss enabled**

📍 **{side.upper()} position exceeded the monitoring threshold**
• Current {side} position: {position}
• Monitoring threshold: {self.position_limit:.2f}
• Latest price: {self.latest_price:.8f}

⚡ **Double take-profit/stop-loss strategy enabled**
• Take-profit quantity: {self.initial_quantity * 2}
• Stop-loss quantity: {self.initial_quantity * 2}

🔄 **Strategy notes**
• When the position exceeds the monitoring threshold, the system enables the double strategy automatically
• This speeds up position reduction and lowers exposure
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_double_profit_recovery(self, side, position):
        """Send double take-profit/stop-loss recovery notification"""
        message = f"""
✅ **Double take-profit/stop-loss cleared**

📍 **{side.upper()} position returned to a safe range**
• Current {side} position: {position}
• Monitoring threshold: {self.position_limit:.2f}
• Latest price: {self.latest_price:.8f}

🟢 **Normal take-profit/stop-loss strategy restored**
• Take-profit quantity: {self.initial_quantity}
• Stop-loss quantity: {self.initial_quantity}

📊 **Strategy notes**
• The position dropped below the monitoring threshold
• The system has switched back to the standard strategy
"""
        await self._send_telegram_message(message, urgent=False)

    async def _get_balance_info(self):
        """Get balance information"""
        try:
            balance = self.exchange.fetch_balance(params={"type": "future"})
            balance_info = []
            
            if 'info' in balance and 'assets' in balance['info']:
                for asset in balance['info']['assets']:
                    asset_name = asset['asset']
                    margin_balance = float(asset.get('marginBalance', 0))
                    wallet_balance = float(asset.get('walletBalance', 0))
                    unrealized_pnl = float(asset.get('unrealizedProfit', 0))
                    
                    if margin_balance > 0 or wallet_balance > 0:
                        if margin_balance > 0:
                            balance_info.append(f"• {asset_name} margin: {margin_balance:.2f}")
                        
                        if wallet_balance > 0:
                            balance_info.append(f"• {asset_name} wallet: {wallet_balance:.2f}")
                        
                        if unrealized_pnl != 0:
                            pnl_sign = "+" if unrealized_pnl > 0 else ""
                            balance_info.append(f"• {asset_name} unrealized PnL: {pnl_sign}{unrealized_pnl:.2f}")
            
            if not balance_info:
                if 'USDT' in balance:
                    usdt_balance = balance['USDT']
                    total = usdt_balance.get('total', 0)
                    if total > 0:
                        balance_info.append(f"• USDT balance: {total:.2f}")
                
                if 'USDC' in balance:
                    usdc_balance = balance['USDC']
                    total = usdc_balance.get('total', 0)
                    if total > 0:
                        balance_info.append(f"• USDC balance: {total:.2f}")
                
                if not balance_info:
                    for currency, info in balance.items():
                        if isinstance(info, dict) and 'total' in info:
                            total = info.get('total', 0)
                            if total > 0:
                                balance_info.append(f"• {currency} balance: {total:.2f}")
            
            if balance_info:
                return "\n".join(balance_info)
            else:
                return "• Account balance: unavailable"
                
        except Exception as e:
            logger.warning(f"Failed to fetch balance: {e}")
            return "• Account balance: loading..."

    async def _send_summary_notification(self):
        """Send periodic summary notification (silent)"""
        current_time = time.time()
        if current_time - self.last_summary_time < NOTIFICATION_INTERVAL:
            return
            
        balance_info = await self._get_balance_info()
        
        message = f"""
📊 **Runtime summary**

💰 **Account info**
{balance_info}

📈 **Positions**
• Long: {self.long_position}
• Short: {self.short_position}

📋 **Open orders**
• Long entry: {self.buy_long_orders}
• Long take-profit: {self.sell_long_orders}
• Short entry: {self.sell_short_orders}
• Short take-profit: {self.buy_short_orders}

💹 **Price info**
• Latest price: {self.latest_price:.8f}
• Best bid: {self.best_bid_price:.8f}
• Best ask: {self.best_ask_price:.8f}

🏃‍♂️ Bot is running normally...
"""
        await self._send_telegram_message(message, urgent=False, silent=True)
        self.last_summary_time = current_time

    async def _send_error_notification(self, error_msg, error_type="Runtime error"):
        """Send error notification"""
        message = f"""
❌ **{error_type}**

🔍 **Error details**
{error_msg}

⏰ **Time**: {time.strftime("%Y-%m-%d %H:%M:%S")}

Please check the bot status...
"""
        await self._send_telegram_message(message, urgent=True)

    def _check_orders_status(self):
        """Check current order state and update long/short order counts"""
        orders = self._safe_fetch_open_orders()
        if orders is None:
            now = time.time()
            if now - self._order_sync_skip_log_ts >= self.open_orders_backoff_s:
                self._order_sync_skip_log_ts = now
                logger.warning("Order sync skipped; keeping last known order state")
            return

        buy_long_orders = 0.0
        sell_long_orders = 0.0
        buy_short_orders = 0.0
        sell_short_orders = 0.0

        for order in orders:
            orig_quantity = abs(float(order.get('info', {}).get('origQty', 0)))
            side = order.get('side')
            position_side = order.get('info', {}).get('positionSide')

            if side == 'buy' and position_side == 'LONG':
                buy_long_orders += orig_quantity
            elif side == 'sell' and position_side == 'LONG':
                sell_long_orders += orig_quantity
            elif side == 'buy' and position_side == 'SHORT':
                buy_short_orders += orig_quantity
            elif side == 'sell' and position_side == 'SHORT':
                sell_short_orders += orig_quantity

        self.buy_long_orders = buy_long_orders
        self.sell_long_orders = sell_long_orders
        self.buy_short_orders = buy_short_orders
        self.sell_short_orders = sell_short_orders

    def _safe_fetch_open_orders(self, symbol=None):
        """Fetch open orders and degrade gracefully when Binance demo returns transient errors.

        Returns:
            list | None: Open orders on success, or None when the exchange request fails.
        """
        try:
            if symbol is None:
                symbol = self.ccxt_symbol
            if self._open_orders_last_failure_ts:
                elapsed = time.time() - self._open_orders_last_failure_ts
                if elapsed < self.open_orders_backoff_s:
                    return None
            return self.exchange.fetch_open_orders(symbol=symbol)
        except Exception as e:
            self._open_orders_last_failure_ts = time.time()
            logger.warning(f"Failed to fetch open orders for {symbol}; skipping order-state dependent logic: {e}")
            return None

    async def _keep_listen_key_alive(self):
        """定期更新 listenKey"""
        while self.running:
            try:
                await asyncio.sleep(1800)  # 每 30 分钟更新一次
                self.exchange.fapiPrivatePutListenKey()
                self.listenKey = self._get_listen_key()
                logger.info(f"listenKey refreshed: {self.listenKey}")
            except Exception as e:
                logger.error(f"Failed to refresh listenKey: {e}")
                await asyncio.sleep(60)

    async def _connect_websocket(self):
        """连接 WebSocket 并订阅 ticker 和持仓数据"""
        try:
            async with websockets.connect(WEBSOCKET_URL) as websocket:
                await self._subscribe_ticker(websocket)
                await self._subscribe_orders(websocket)
                logger.info("WebSocket connected successfully; starting to receive messages")
                while self.running:
                    try:
                        message = await websocket.recv()
                        data = json.loads(message)
                        if data.get("e") == "bookTicker":
                            await self._handle_ticker_update(message)
                        elif data.get("e") == "ORDER_TRADE_UPDATE":
                            await self._handle_order_update(message)
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("WebSocket connection closed; attempting to reconnect...")
                        break
                    except Exception as e:
                        logger.error(f"Failed to process WebSocket message: {e}")
                        break
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise e

    async def _subscribe_ticker(self, websocket):
        """订阅 ticker 数据"""
        coin_name = self.symbol.replace('USDT', '').replace('USDC', '')
        payload = {
            "method": "SUBSCRIBE",
            "params": [f"{coin_name.lower()}{self.contract_type.lower()}@bookTicker"],
            "id": 1
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"Sent ticker subscription request: {payload}")

    async def _subscribe_orders(self, websocket):
        """订阅挂单数据"""
        if not self.listenKey:
            logger.error("listenKey is empty; cannot subscribe to order updates")
            return

        payload = {
            "method": "SUBSCRIBE",
            "params": [f"{self.listenKey}"],
            "id": 3
        }
        await websocket.send(json.dumps(payload))
        logger.info(f"Sent order subscription request: {payload}")

    async def _handle_ticker_update(self, message):
        """处理 ticker 更新"""
        current_time = time.time()
        if current_time - self.last_ticker_update_time < 0.5:
            return

        self.last_ticker_update_time = current_time
        data = json.loads(message)
        if data.get("e") == "bookTicker":
            best_bid_price = data.get("b")
            best_ask_price = data.get("a")

            if best_bid_price is None or best_ask_price is None:
                logger.warning("bookTicker message is missing best bid or best ask")
                return

            try:
                self.best_bid_price = float(best_bid_price)
                self.best_ask_price = float(best_ask_price)
                self.latest_price = (self.best_bid_price + self.best_ask_price) / 2
                guard_transition = self._update_market_guard_state(
                    price=self.latest_price,
                    bid=self.best_bid_price,
                    ask=self.best_ask_price,
                )
                if guard_transition.get("changed"):
                    await self._notify_market_guard_transition(guard_transition)
            except ValueError as e:
                logger.error(f"Failed to parse price: {e}")

            if time.time() - self.last_position_update_time > SYNC_TIME:
                try:
                    self.long_position, self.short_position = self._get_position()
                    self.last_position_update_time = time.time()
                    if self.long_position > 0:
                        self.long_entry_pending = False
                    if self.short_position > 0:
                        self.short_entry_pending = False
                except Exception as e:
                    logger.warning(f"Position sync failed; keeping last known positions: {e}")

            if time.time() - self.last_orders_update_time > SYNC_TIME:
                try:
                    self._check_orders_status()
                    self.last_orders_update_time = time.time()
                except Exception as e:
                    logger.warning(f"Order sync failed; keeping last known order state: {e}")

            try:
                await self._grid_loop()
            except Exception as e:
                logger.error(f"Grid loop failed during ticker processing: {e}", exc_info=True)

            try:
                await self._send_summary_notification()
            except Exception as e:
                logger.warning(f"Summary notification failed; continuing: {e}")

    async def _handle_order_update(self, message):
        """Process order and position updates"""
        # 延迟初始化锁
        if self.lock is None:
            self.lock = asyncio.Lock()
        
        async with self.lock:
            data = json.loads(message)

            if data.get("e") == "ORDER_TRADE_UPDATE":
                order = data.get("o", {})
                symbol = order.get("s")
                if symbol == self.symbol:
                    side = order.get("S")
                    position_side = order.get("ps")
                    reduce_only = order.get("R")
                    status = order.get("X")
                    quantity = float(order.get("q", 0))
                    filled = float(order.get("z", 0))
                    remaining = quantity - filled

                    if status == "NEW":
                        if side == "BUY":
                            if position_side == "LONG":
                                self.buy_long_orders += remaining
                            elif position_side == "SHORT":
                                self.buy_short_orders += remaining
                        elif side == "SELL":
                            if position_side == "LONG":
                                self.sell_long_orders += remaining
                            elif position_side == "SHORT":
                                self.sell_short_orders += remaining
                    elif status == "FILLED":
                        if side == "BUY":
                            if position_side == "LONG":
                                self.long_position += filled
                                self.buy_long_orders = max(0.0, self.buy_long_orders - filled)
                            elif position_side == "SHORT":
                                self.short_position = max(0.0, self.short_position - filled)
                                self.buy_short_orders = max(0.0, self.buy_short_orders - filled)
                        elif side == "SELL":
                            if position_side == "LONG":
                                self.long_position = max(0.0, self.long_position - filled)
                                self.sell_long_orders = max(0.0, self.sell_long_orders - filled)
                            elif position_side == "SHORT":
                                self.short_position += filled
                                self.sell_short_orders = max(0.0, self.sell_short_orders - filled)
                    elif status == "CANCELED":
                        if side == "BUY":
                            if position_side == "LONG":
                                self.buy_long_orders = max(0.0, self.buy_long_orders - quantity)
                                self.long_entry_pending = False
                            elif position_side == "SHORT":
                                self.buy_short_orders = max(0.0, self.buy_short_orders - quantity)
                        elif side == "SELL":
                            if position_side == "LONG":
                                self.sell_long_orders = max(0.0, self.sell_long_orders - quantity)
                            elif position_side == "SHORT":
                                self.sell_short_orders = max(0.0, self.sell_short_orders - quantity)
                                self.short_entry_pending = False
                    elif status in ("FILLED", "EXPIRED", "REJECTED"):
                        if side == "BUY" and position_side == "LONG" and not reduce_only:
                            self.long_entry_pending = False
                        elif side == "SELL" and position_side == "SHORT" and not reduce_only:
                            self.short_entry_pending = False

    def _get_take_profit_quantity(self, position, side):
        """Adjust take-profit order quantity"""
        if side == 'long':
            if position > self.position_limit:
                self.long_initial_quantity = self.initial_quantity * 2
            elif self.short_position >= self.position_threshold:
                self.long_initial_quantity = self.initial_quantity * 2
            else:
                self.long_initial_quantity = self.initial_quantity

        elif side == 'short':
            if position > self.position_limit:
                self.short_initial_quantity = self.initial_quantity * 2
            elif self.long_position >= self.position_threshold:
                self.short_initial_quantity = self.initial_quantity * 2
            else:
                self.short_initial_quantity = self.initial_quantity

    async def _initialize_long_orders(self):
        """初始化多头挂单"""
        current_time = time.time()
        if self.long_entry_pending and current_time - self.last_long_order_time < self.entry_pending_timeout_s:
            remaining = self.entry_pending_timeout_s - (current_time - self.last_long_order_time)
            logger.info(f"Skipping long entry because a previous order is still pending ({remaining:.0f}s remaining)")
            return
        if current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME:
            remaining = ORDER_COOLDOWN_TIME - (current_time - self.last_long_order_time)
            logger.info(f"Skipping long order placement because only {remaining:.0f}s remain in the entry cooldown")
            return

        if not self._cancel_orders_for_side('long'):
            logger.warning("Long open orders could not be verified; placing entry cautiously without cleanup")
        order = self._place_order('buy', self.best_bid_price, self.initial_quantity, False, 'long')
        if order:
            self.long_entry_pending = True
            logger.info(f"Placed long entry order: buy @ {self.latest_price}")
        else:
            logger.warning("Long entry order was rejected by Binance; will retry on the next cycle")

        self.last_long_order_time = time.time()
        logger.info("Long order initialization complete")

    async def _initialize_short_orders(self):
        """初始化空头挂单"""
        current_time = time.time()
        if self.short_entry_pending and current_time - self.last_short_order_time < self.entry_pending_timeout_s:
            remaining = self.entry_pending_timeout_s - (current_time - self.last_short_order_time)
            logger.info(f"Skipping short entry because a previous order is still pending ({remaining:.0f}s remaining)")
            return
        if current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME:
            remaining = ORDER_COOLDOWN_TIME - (current_time - self.last_short_order_time)
            logger.info(f"Skipping short order placement because only {remaining:.0f}s remain in the entry cooldown")
            return

        if not self._cancel_orders_for_side('short'):
            logger.warning("Short open orders could not be verified; placing entry cautiously without cleanup")
        order = self._place_order('sell', self.best_ask_price, self.initial_quantity, False, 'short')
        if order:
            self.short_entry_pending = True
            logger.info(f"Placed short entry order: sell @ {self.latest_price}")
        else:
            logger.warning("Short entry order was rejected by Binance; will retry on the next cycle")

        self.last_short_order_time = time.time()
        logger.info("Short order initialization complete")

    def _cancel_orders_for_side(self, position_side):
        """撤销某个方向的所有挂单"""
        orders = self._safe_fetch_open_orders(self.ccxt_symbol)

        if orders is None:
            return False

        if len(orders) == 0:
            logger.info("No open orders found")
        else:
            try:
                for order in orders:
                    side = order.get('side')
                    reduce_only = order.get('reduceOnly', False)
                    position_side_order = order.get('info', {}).get('positionSide', 'BOTH')

                    if position_side == 'long':
                        if not reduce_only and side == 'buy' and position_side_order == 'LONG':
                            self._cancel_order(order['id'])
                        elif reduce_only and side == 'sell' and position_side_order == 'LONG':
                            self._cancel_order(order['id'])

                    elif position_side == 'short':
                        if not reduce_only and side == 'sell' and position_side_order == 'SHORT':
                            self._cancel_order(order['id'])
                        elif reduce_only and side == 'buy' and position_side_order == 'SHORT':
                            self._cancel_order(order['id'])
            except ccxt.OrderNotFound as e:
                logger.warning(f"Order {order['id']} not found; no cancellation needed: {e}")
                self._check_orders_status()
            except Exception as e:
                logger.error(f"Failed to cancel orders: {e}")
        return True

    def _cancel_order(self, order_id):
        """撤单"""
        try:
            self.exchange.cancel_order(order_id, self.ccxt_symbol)
        except ccxt.BaseError as e:
            logger.error(f"Failed to cancel order: {e}")

    def _adjust_limit_price_for_maker(self, side, price, is_reduce_only=False):
        """Keep limit orders on the maker side when the book is available."""
        try:
            price = float(price)
            tick_size = 10 ** (-max(0, int(self.price_precision)))
            best_bid = float(self.best_bid_price) if self.best_bid_price else None
            best_ask = float(self.best_ask_price) if self.best_ask_price else None

            if side == 'buy':
                reference_price = best_bid or best_ask
                if reference_price is None:
                    return price
                safe_price = reference_price - tick_size
                if safe_price <= 0:
                    return None
                if price >= reference_price:
                    adjusted_price = min(price, safe_price)
                    if adjusted_price != price:
                        logger.info(
                            f"Adjusted buy limit price to remain maker-side: requested={price}, adjusted={adjusted_price}, "
                            f"best_bid={best_bid}, best_ask={best_ask}, reduce_only={is_reduce_only}")
                    return adjusted_price
                return price

            if side == 'sell':
                reference_price = best_ask or best_bid
                if reference_price is None:
                    return price
                safe_price = reference_price + tick_size
                if price <= reference_price:
                    adjusted_price = max(price, safe_price)
                    if adjusted_price != price:
                        logger.info(
                            f"Adjusted sell limit price to remain maker-side: requested={price}, adjusted={adjusted_price}, "
                            f"best_bid={best_bid}, best_ask={best_ask}, reduce_only={is_reduce_only}")
                    return adjusted_price
                return price

            return price
        except Exception as e:
            logger.warning(f"Unable to apply maker-side price guard; using original limit price: {e}")
            return price

    def _place_order(self, side, price, quantity, is_reduce_only=False, position_side=None, order_type='limit'):
        """挂单函数"""
        try:
            if is_reduce_only:
                quantity = round(float(quantity), self.amount_precision)
                quantity = max(quantity, self.min_order_amount)
            else:
                quantity = self._get_entry_quantity(price, quantity)

            import uuid
            client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"

            if order_type == 'market':
                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': is_reduce_only,
                }
                if position_side is not None:
                    params['positionSide'] = position_side.upper()
                order = self.exchange.create_order(self.ccxt_symbol, 'market', side, quantity, params=params)
                return order
            else:
                if price is None:
                    logger.error("Limit order requires a price parameter")
                    return None

                price = round(price, self.price_precision)
                maker_price = self._adjust_limit_price_for_maker(side, price, is_reduce_only)
                if maker_price is None:
                    logger.warning(f"Skipping {side} limit order because the book does not allow a maker-side price")
                    return None
                price = round(maker_price, self.price_precision)

                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': is_reduce_only,
                }
                if position_side is not None:
                    params['positionSide'] = position_side.upper()
                order = self.exchange.create_order(self.ccxt_symbol, 'limit', side, quantity, price, params)
                return order

        except ccxt.BaseError as e:
            logger.error(f"Order placement error: {e}")
            return None

    def _place_take_profit_order(self, ccxt_symbol, side, price, quantity):
        """挂止盈单"""
        # 先按精度 round
        price = round(float(price), self.price_precision)
        maker_side = 'sell' if side == 'long' else 'buy'
        maker_price = self._adjust_limit_price_for_maker(maker_side, price, True)
        if maker_price is None:
            logger.warning(f"Skipping {side} take-profit placement because the book does not allow a maker-side price")
            return
        price = round(float(maker_price), self.price_precision)

        # 如果已有"同价位"的止盈单则跳过（使用 round 后的严格相等判断）
        orders = self._safe_fetch_open_orders(ccxt_symbol)
        if orders is None:
            logger.warning(f"Skipping {side} take-profit placement because open orders could not be verified")
            return
        for order in orders:
            pos = order['info'].get('positionSide')
            s = order['side']
            try:
                op = round(float(order['price']), self.price_precision)
            except Exception:
                op = None
            if (
                pos == side.upper()
                and s == ('sell' if side == 'long' else 'buy')
                and op is not None and op == price
            ):
                logger.info(f"An identical {side} take-profit order already exists at {price}; skipping")
                return

        try:
            adjusted_quantity = self._get_reduce_only_quantity(side, quantity)
            if adjusted_quantity is None:
                logger.warning(f"No {side} position available; skipping {side} take-profit order")
                return

            qty = adjusted_quantity
            if side == 'long':
                import uuid
                client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"
                params = {
                    'newClientOrderId': client_order_id,
                    'reduce_only': True,
                    'positionSide': 'LONG'
                }
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'sell', qty, price, params)
                logger.info(f"Placed long take-profit order successfully: sell {qty} {ccxt_symbol} @ {price}")
            elif side == 'short':
                import uuid
                client_order_id = f"x-TBzTen1X-{uuid.uuid4().hex[:8]}"
                order = self.exchange.create_order(ccxt_symbol, 'limit', 'buy', qty, price, {
                    'newClientOrderId': client_order_id,
                    'reduce_only': True,
                    'positionSide': 'SHORT'
                })
                logger.info(f"Placed short take-profit order successfully: buy {qty} {ccxt_symbol} @ {price}")
        except ccxt.BaseError as e:
            logger.error(f"Failed to place take-profit order: {e}")

    # ===== 核心：多头下单逻辑（修复：只加倍止盈、不加倍补仓；装死限幅；下单后更新冷却时间）=====
    async def _place_long_orders(self, latest_price, allow_new_entries=True):
        """Place long orders"""
        try:
            # 根据当前持仓情况动态调整多头下单数量（可能翻倍）
            self._get_take_profit_quantity(self.long_position, 'long')  # 只影响止盈数量
            if self.long_position <= 0:
                return
            placed_any = False
            
            # 只有在有多头持仓时才进行挂单操作
            if self.long_position > 0:
                # 检查是否超过极限阈值，决定是否进入"装死"模式
                if self.long_position > self.position_threshold:
                    # 装死模式：持仓过大，停止开新仓，只补止盈单
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'long', self.long_position, self.position_threshold, True)
                    else:
                        logger.info(f"Long position {self.long_position} exceeded the limit threshold {self.position_threshold}; entering lockdown")
                    
                    # 检查是否刚进入装死模式，记录固定止盈价
                    if not self.lockdown_mode['long']['active']:
                        lock, r, tp = self._enter_lockdown_fixed_r('long')

                        logger.info(f"Long entered lockdown mode; fixed take-profit price: {self.lockdown_mode['long']['tp_price']} (based on lockdown price: {self.lockdown_mode['long']['lockdown_price']})")
                    
                    # 装死模式下使用固定的止盈价，基于装死时的价格计算
                    fixed_tp_price = self.lockdown_mode['long']['tp_price']
                    placed_any |= self._ensure_lockdown_take_profit(
                        side='long',
                        target_price=fixed_tp_price,
                        quantity=self.long_initial_quantity
                    )
                    
                    # 验证装死模式完整性
                    if not self._validate_lockdown_integrity('long'):
                        logger.error('Long lockdown integrity check failed; exiting while keeping the anchor')
                        self._exit_lockdown_fixed('long', '完整性校验失败')
                else:
                    # 正常网格：先更新中线，再只撤开仓挂单，止盈按目标价"校准/重挂"，补仓用基础数量
                    # 检查是否从装死模式恢复正常
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'long', self.long_position, self.position_threshold, False)
                    
                    # 如果从装死模式恢复正常，退出但保留锚点
                    if self.lockdown_mode['long']['active']:
                        self._exit_lockdown_fixed('long', '仓位回落')
                        logger.info("Long exited lockdown mode and resumed normal trading")

                    existing_tp = self._get_existing_tp_order('long')
                    if not allow_new_entries and existing_tp is not None:
                        now = time.time()
                        if now - getattr(self, '_market_guard_last_log_ts', 0.0) >= 30:
                            self._market_guard_last_log_ts = now
                            logger.info("Long take-profit already exists; market guard keeps it unchanged")
                        return

                    self._update_mid_price('long', latest_price)

                    # 止盈：正常模式下允许随中线重挂；暂停模式下只保持现有止盈，不频繁改价
                    if allow_new_entries or existing_tp is None:
                        placed_any |= self._ensure_take_profit_at(
                            side='long',
                            target_price=self.upper_price_long,
                            quantity=self.long_initial_quantity,
                            tol_ratio=max(self.grid_spacing * 0.2, 0.001),
                        )
                    elif existing_tp:
                        logger.info("Long take-profit already exists; market guard keeps it unchanged")

                    if allow_new_entries:
                        # 补仓：始终使用基础数量 initial_quantity，而不是"加倍后"的 long_initial_quantity
                        placed_any |= self._ensure_entry_order_at(
                            side='long',
                            target_price=self.lower_price_long,
                            quantity=self.initial_quantity,
                            tol_ratio=max(self.grid_spacing * 0.2, 0.001),
                        )
                        if placed_any:
                            logger.info("Placed long take-profit and long entry orders")
                    else:
                        logger.info("Long new entries are paused by the market guard; keeping take-profit orders only")

                # 若本轮确实有挂出新单/重挂，则更新冷却时间戳
                if placed_any:
                    self.last_long_order_time = time.time()

        except Exception as e:
            logger.error(f"Failed to place long orders: {e}")

    async def _place_short_orders(self, latest_price, allow_new_entries=True):
        """Place short orders"""
        try:
            # 根据当前持仓情况动态调整空头下单数量（可能翻倍）
            self._get_take_profit_quantity(self.short_position, 'short')
            if self.short_position <= 0:
                return
            placed_any = False
            
            # 只有在有空头持仓时才进行挂单操作
            if self.short_position > 0:
                # 检查是否超过极限阈值，决定是否进入"装死"模式
                if self.short_position > self.position_threshold:
                    # 装死模式：持仓过大，停止开新仓，只补止盈单
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'short', self.short_position, self.position_threshold, True)
                    else:
                        logger.info(f"Short position {self.short_position} exceeded the limit threshold {self.position_threshold}; entering lockdown")
                    
                    # 检查是否刚进入装死模式，记录固定止盈价
                    if not self.lockdown_mode['short']['active']:
                        lock, r, tp = self._enter_lockdown_fixed_r('short')

                        logger.info(f"Short entered lockdown mode; fixed take-profit price: {self.lockdown_mode['short']['tp_price']} (based on lockdown price: {self.lockdown_mode['short']['lockdown_price']})")
                    
                    # 装死模式下使用固定的止盈价，基于装死时的价格计算
                    fixed_tp_price = self.lockdown_mode['short']['tp_price']
                    placed_any |= self._ensure_lockdown_take_profit(
                        side='short',
                        target_price=fixed_tp_price,
                        quantity=self.short_initial_quantity
                    )
                    
                    # 验证装死模式完整性
                    if not self._validate_lockdown_integrity('short'):
                        logger.error('Short lockdown integrity check failed; exiting while keeping the anchor')
                        self._exit_lockdown_fixed('short', '完整性校验失败')
                else:
                    # 检查是否从装死模式恢复正常
                    if threshold_logger:
                        threshold_logger.log_threshold_status(self.symbol, 'short', self.short_position, self.position_threshold, False)
                    
                    # 如果从装死模式恢复正常，退出但保留锚点
                    if self.lockdown_mode['short']['active']:
                        self._exit_lockdown_fixed('short', '仓位回落')
                        logger.info("Short exited lockdown mode and resumed normal trading")

                    existing_tp = self._get_existing_tp_order('short')
                    if not allow_new_entries and existing_tp is not None:
                        now = time.time()
                        if now - getattr(self, '_market_guard_last_log_ts', 0.0) >= 30:
                            self._market_guard_last_log_ts = now
                            logger.info("Short take-profit already exists; market guard keeps it unchanged")
                        return

                    self._update_mid_price('short', latest_price)

                    if allow_new_entries or existing_tp is None:
                        placed_any |= self._ensure_take_profit_at(
                            side='short',
                            target_price=self.lower_price_short,
                            quantity=self.short_initial_quantity,
                            tol_ratio=max(self.grid_spacing * 0.2, 0.001),
                        )
                    elif existing_tp:
                        logger.info("Short take-profit already exists; market guard keeps it unchanged")

                    if allow_new_entries:
                        placed_any |= self._ensure_entry_order_at(
                            side='short',
                            target_price=self.upper_price_short,
                            quantity=self.initial_quantity,
                            tol_ratio=max(self.grid_spacing * 0.2, 0.001),
                        )
                        if placed_any:
                            logger.info("Placed short take-profit and short entry orders")
                    else:
                        logger.info("Short new entries are paused by the market guard; keeping take-profit orders only")

                # 若本轮确实有挂出新单/重挂，则更新冷却时间戳
                if placed_any:
                    self.last_short_order_time = time.time()

        except Exception as e:
            logger.error(f"Failed to place short orders: {e}")

    def _update_mid_price(self, side, price):
        """Update mid price"""
        if side == 'long':
            self.mid_price_long = price
            self.upper_price_long = self.mid_price_long * (1 + self.grid_spacing)
            self.lower_price_long = self.mid_price_long * (1 - self.grid_spacing)
            logger.info("Updating long mid price")

        elif side == 'short':
            self.mid_price_short = price
            self.upper_price_short = self.mid_price_short * (1 + self.grid_spacing)
            self.lower_price_short = self.mid_price_short * (1 - self.grid_spacing)
            logger.info("Updating short mid price")

    async def _check_risk(self):
        """Check positions and reduce inventory risk"""
        self._reset_emg_daily_counter_if_new_day()
        if self._day_fuse_on:
            return

        enter_ratio = self.emg_enter_ratio
        if self.enable_dynamic_enter_075 and self._is_extreme_vol():
            enter_ratio = min(enter_ratio, 0.75)

        T = self.position_threshold
        now = time.time()

        if self._emg_in_progress:
            if (self.long_position < self.emg_exit_ratio * T and
                self.short_position < self.emg_exit_ratio * T and
                now >= self._grid_pause_until_ts):
                self._emg_in_progress = False
                logger.info(f"[EMG][{self.symbol}] Exiting emergency mode: both sides fell below {self.emg_exit_ratio:.2f}T")
                # 发送退出紧急状态通知
                await self._send_emergency_exit_notification()
            return

        if (self.long_position >= enter_ratio * T and
            self.short_position >= enter_ratio * T and
            (now - self._emg_last_ts >= self.emg_cooldown_s)):
            self._emg_in_progress = True
            self._emg_last_ts = now
            self._grid_pause_until_ts = now + self.grid_pause_after_emg_s
            self._emg_trigger_count_today += 1
            logger.info(f"[EMG][{self.symbol}] Entering emergency reduction: threshold {enter_ratio:.2f}T, cooldown {self.emg_cooldown_s}s, pause {self.grid_pause_after_emg_s}s")
            
            # 发送进入紧急状态通知
            await self._send_emergency_enter_notification(enter_ratio)

            if self._emg_trigger_count_today >= self.emg_daily_fuse_count:
                self._enter_day_fuse_mode()
                # 发送日内封盘通知
                await self._send_daily_fuse_notification()
                return

            try:
                self._cancel_open_orders_for_side('long')
                self._cancel_open_orders_for_side('short')
            except Exception as e:
                logger.warning(f"[EMG] Unexpected error while cancelling entry orders: {e}")

            fixed_qty = max(self.min_order_amount, round(self.position_threshold * 0.1, self.amount_precision))
            long_cut  = min(fixed_qty, max(0.0, self.long_position))
            short_cut = min(fixed_qty, max(0.0, self.short_position))

            if long_cut > 0:
                await self._emg_reduce_side_batched('long', long_cut)
            if short_cut > 0:
                await self._emg_reduce_side_batched('short', short_cut)



    async def _grid_loop(self):
        """Core grid trading loop"""
        # 一次性从本地恢复装死状态
        if not getattr(self, '_lockdown_restored', False):
            try:
                self._restore_lockdown_from_local()
            except Exception as _e:
                logger.warning(f"Failed to restore lockdown state: {_e}")
            finally:
                self._lockdown_restored = True

        await self._check_and_notify_position_threshold('long', self.long_position)
        await self._check_and_notify_position_threshold('short', self.short_position)
        await self._check_and_notify_double_profit('long', self.long_position)
        await self._check_and_notify_double_profit('short', self.short_position)
        await self._check_risk()

        # 记录价格与风控辅助
        self._record_price(self.latest_price)

        self._reset_emg_daily_counter_if_new_day()
        allow_new_entries = self._can_open_new_entries()
        guard_block_active = not allow_new_entries

        if guard_block_active:
            cleanup_due_to_dynamic_guard = self._market_guard_paused and self._market_guard_last_cleanup_ts < self._market_guard_last_transition_ts
            cleanup_due_to_static_guard = self._market_guard_static_block and self._market_guard_last_cleanup_ts < self._market_guard_static_block_ts
            if cleanup_due_to_dynamic_guard or cleanup_due_to_static_guard:
                guard_reason = self._market_guard_reason or self._market_guard_static_reason or "market_guard"
                logger.info(f"Market guard active ({guard_reason}); cancelling non-reduce-only entry orders and keeping reduce-only exits")
                try:
                    self._cancel_open_orders_for_side('long')
                    self._cancel_open_orders_for_side('short')
                except Exception as e:
                    logger.warning(f"Failed to cancel entry orders during market-guard pause: {e}")
                self.long_entry_pending = False
                self.short_entry_pending = False
                self._market_guard_last_cleanup_ts = time.time()

        # 暂停窗口或封盘：不再开新网格/初始化
        if time.time() < self._grid_pause_until_ts or self._day_fuse_on:
            # 避免重复记录暂停日志
            if not hasattr(self, '_last_pause_log_ts') or time.time() - getattr(self, '_last_pause_log_ts', 0) > 60:
                self._last_pause_log_ts = time.time()
                if self._day_fuse_on:
                    logger.info('[EMG] Daily circuit-breaker mode enabled; skipping this cycle')
                else:
                    remaining_time = self._grid_pause_until_ts - time.time()
                    logger.info(f'[EMG] Pause window active; {remaining_time:.0f}s remaining, skipping this cycle')
            return

        current_time = time.time()
        guard_reason = self._market_guard_reason or self._market_guard_static_reason or "market_guard"
        
        # 检测多头持仓
        if self.long_position == 0:
            if self.long_entry_pending:
                remaining = max(0.0, self.entry_pending_timeout_s - (current_time - self.last_long_order_time))
                self._log_pending_entry_status('long', remaining)
            elif not allow_new_entries:
                if current_time - self._market_guard_last_log_ts >= 15:
                    self._market_guard_last_log_ts = current_time
                    logger.info(f"No long position detected ({self.long_position}); new entries paused by {guard_reason}")
            else:
                now = time.time()
                if now - getattr(self, '_last_no_position_log_ts', 0.0) >= 15:
                    self._last_no_position_log_ts = now
                    logger.info(f"No long position detected ({self.long_position}); initializing long orders @ ticker")
                await self._initialize_long_orders()
        else:
            if not (0 < self.buy_long_orders <= self.long_initial_quantity) or not (0 < self.sell_long_orders <= self.long_initial_quantity):
                if self.long_position > self.position_threshold and current_time - self.last_long_order_time < ORDER_COOLDOWN_TIME:
                    logger.info(f"Less than {ORDER_COOLDOWN_TIME}s since the last long take-profit order; skipping this cycle @ ticker")
                else:
                    await self._place_long_orders(self.latest_price, allow_new_entries=allow_new_entries)

        # 检测空头持仓
        if self.short_position == 0:
            if self.short_entry_pending:
                remaining = max(0.0, self.entry_pending_timeout_s - (current_time - self.last_short_order_time))
                self._log_pending_entry_status('short', remaining)
            elif not allow_new_entries:
                if current_time - self._market_guard_last_log_ts >= 15:
                    self._market_guard_last_log_ts = current_time
                    logger.info(f"No short position detected ({self.short_position}); new entries paused by {guard_reason}")
            else:
                await self._initialize_short_orders()
        else:
            if not (0 < self.sell_short_orders <= self.short_initial_quantity) or not (0 < self.buy_short_orders <= self.short_initial_quantity):
                if self.short_position > self.position_threshold and current_time - self.last_short_order_time < ORDER_COOLDOWN_TIME:
                    logger.info(f"Less than {ORDER_COOLDOWN_TIME}s since the last short take-profit order; skipping this cycle @ ticker")
                else:
                    await self._place_short_orders(self.latest_price, allow_new_entries=allow_new_entries)

    # ===== 新增：只撤"开仓"挂单，保留 reduceOnly 的止盈挂单 =====
    def _cancel_open_orders_for_side(self, position_side: str):
        """仅撤销某个方向的开仓挂单（reduceOnly=False），保留止盈单"""
        orders = self._safe_fetch_open_orders(self.ccxt_symbol)
        if orders is None:
            return False
        try:
            for order in orders:
                side = order.get('side')  # 'buy' / 'sell'
                pos = order.get('info', {}).get('positionSide', 'BOTH')  # 'LONG' / 'SHORT'
                # 兼容读取 reduceOnly
                ro = order.get('reduceOnly')
                if ro is None:
                    ro = order.get('info', {}).get('reduceOnly') or order.get('info', {}).get('reduce_only') or False

                if position_side == 'long':
                    # 多头开仓: buy + LONG + 非 reduceOnly
                    if (pos == 'LONG') and (side == 'buy') and (not ro):
                        self._cancel_order(order['id'])
                elif position_side == 'short':
                    # 空头开仓: sell + SHORT + 非 reduceOnly
                    if (pos == 'SHORT') and (side == 'sell') and (not ro):
                        self._cancel_order(order['id'])
        except ccxt.OrderNotFound as e:
            logger.warning(f"Order not found during cancellation: {e}")
            self._check_orders_status()
        except Exception as e:
            logger.error(f"Failed to cancel entry orders: {e}")
        return True

    # ===== 新增：获取当前方向已有的止盈单（reduceOnly=True）=====
    def _get_existing_tp_order(self, side: str):
        """
        返回该方向当前已存在的一张 reduceOnly 止盈单（若有）。
        side: 'long' or 'short'
        """
        orders = self._safe_fetch_open_orders(self.ccxt_symbol)
        if orders is None:
            return None
        for order in orders:
            pos = order.get('info', {}).get('positionSide', 'BOTH')
            s = order.get('side')
            ro = order.get('reduceOnly')
            if ro is None:
                ro = order.get('info', {}).get('reduceOnly') or order.get('info', {}).get('reduce_only') or False

            if side == 'long' and pos == 'LONG' and ro and s == 'sell':
                return order
            if side == 'short' and pos == 'SHORT' and ro and s == 'buy':
                return order
        return None

    def _get_existing_entry_order(self, side: str):
        """Return the current non-reduce-only entry order for a side, if any."""
        orders = self._safe_fetch_open_orders(self.ccxt_symbol)
        if orders is None:
            return None
        for order in orders:
            pos = order.get('info', {}).get('positionSide', 'BOTH')
            s = order.get('side')
            ro = order.get('reduceOnly')
            if ro is None:
                ro = order.get('info', {}).get('reduceOnly') or order.get('info', {}).get('reduce_only') or False

            if side == 'long' and pos == 'LONG' and (not ro) and s == 'buy':
                return order
            if side == 'short' and pos == 'SHORT' and (not ro) and s == 'sell':
                return order
        return None

    def _ensure_entry_order_at(self, side: str, target_price: float, quantity: float, tol_ratio: float = None) -> bool:
        """
        Ensure a fresh entry order exists near the requested price.
        Returns True when a new order is placed or the previous one is replaced.
        """
        if tol_ratio is None:
            tol_ratio = max(self.grid_spacing * 0.2, 0.001)

        target_price = round(float(target_price), self.price_precision)
        existing = self._get_existing_entry_order(side)
        if existing:
            try:
                existing_price = round(float(existing['price']), self.price_precision)
            except Exception:
                existing_price = None

            if existing_price is not None:
                rel_diff = abs(existing_price / target_price - 1.0)
                if rel_diff <= tol_ratio:
                    return False
            self._cancel_order(existing['id'])

        if side == 'long':
            order = self._place_order('buy', target_price, quantity, False, 'long')
        else:
            order = self._place_order('sell', target_price, quantity, False, 'short')

        return bool(order)

    # ===== 新增：确保止盈单在目标价位（偏离超阈值则重挂），返回是否有下单动作 =====
    def _ensure_take_profit_at(self, side: str, target_price: float, quantity: float, tol_ratio: float = None) -> bool:
        """
        side: 'long'/'short'
        target_price: 目标止盈价（会按精度 round）
        quantity: 止盈数量（已考虑 double 逻辑）
        tol_ratio: 相对容忍度（如 0.002 = 0.2%）。默认取 grid_spacing 的 0.2 与 0.1% 的较大值。
        """
        if tol_ratio is None:
            tol_ratio = max(self.grid_spacing * 0.2, 0.001)  # 根据网格间距自适应

        target_price = round(float(target_price), self.price_precision)
        existing = self._get_existing_tp_order(side)
        if existing:
            try:
                existing_price = round(float(existing['price']), self.price_precision)
            except Exception:
                existing_price = None

            if existing_price is not None:
                rel_diff = abs(existing_price / target_price - 1.0)
                if rel_diff <= tol_ratio:
                    # 已有止盈价足够接近，不重挂
                    return False
                else:
                    # 价格偏离明显，先撤再重挂
                    self._cancel_order(existing['id'])

        # 挂新的止盈
        self._place_take_profit_order(self.ccxt_symbol, side, target_price, quantity)
        return True

    def _ensure_lockdown_take_profit(self, side: str, target_price: float, quantity: float):
        """装死模式下的止盈单管理：只在首次进入时挂单，后续不重挂，确保价格完全固定"""
        existing = self._get_existing_tp_order(side)
        if existing:
            # 已有止盈单，验证价格是否与装死时的固定价格一致
            try:
                existing_price = round(float(existing['price']), self.price_precision)
                target_price_rounded = round(float(target_price), self.price_precision)
                
                if existing_price != target_price_rounded:
                    # 在装死模式下，如果价格不一致，强制撤单并重新挂单
                    self._cancel_order(existing['id'])
                    self._place_take_profit_order(self.ccxt_symbol, side, target_price, quantity)
                    return True
                else:
                    # 价格一致，不重挂
                    return False
            except Exception as e:
                logger.error(f"Error validating lockdown take-profit price: {e}")
                return False
        
        # 没有止盈单，挂新的止盈单
        logger.info(f"Lockdown mode: placing fixed take-profit order for {side} @ {target_price}")
        self._place_take_profit_order(self.ccxt_symbol, side, target_price, quantity)
        return True

    # ===== 新增：装死分支的 r 限幅计算 =====
    def _compute_tp_multiplier(self, side: str) -> float:
        """
        计算在"装死"状态下用于调整止盈价的倍数 r，并做上下限约束：
        下限= max(1 + grid_spacing, 1.01)，上限= min(1 + 3*grid_spacing, 1.05)
        """
        if side == 'long':
            pos, opp = self.long_position, self.short_position
        else:
            pos, opp = self.short_position, self.long_position

        if opp > 0:
            r = 1.0 + (pos / opp) / 100.0
        else:
            r = 1.01

        min_r = max(1.0 + self.grid_spacing, 1.01)
        max_r = min(1.0 + 3.0 * self.grid_spacing, 1.05)
        return max(min_r, min(r, max_r))

    
    def _validate_lockdown_integrity(self, side: str) -> bool:
        # Verify lockdown integrity; prefer correcting tp using frozen r/lock rather than exiting.
        if not self.lockdown_mode[side]['active']:
            return True
        tp = self.lockdown_mode[side].get('tp_price')
        lock = self.lockdown_mode[side].get('lockdown_price')
        if tp is None or lock is None:
            logger.error(f"Lockdown data incomplete: {side} - tp_price: {tp}, lockdown_price: {lock}")
            return False  # Only in this rare case allow caller to handle

        # Prefer frozen r; if missing, use fixed r and persist
        r = self.lockdown_mode[side].get('r')
        if not r or r <= 1.0:
            r = self._fixed_r()
            self.lockdown_mode[side]['r'] = r
            try:
                self._persist_lockdown_state()
            except Exception:
                pass

        expected_tp = (lock * r) if side == 'long' else (lock / r)
        prec = getattr(self, 'price_precision', 6)
        if round(float(tp), prec) != round(float(expected_tp), prec):
            logger.warning(f"Lockdown take-profit price mismatch for {side}: actual={tp}, expected={expected_tp}. Fixing in memory and persisting.")
            self.lockdown_mode[side]['tp_price'] = expected_tp
            try:
                self._persist_lockdown_state()
            except Exception:
                pass
            return True
        else:
            logger.debug(f"Lockdown integrity verified: {side}")
            return True

    def _reset_emg_daily_counter_if_new_day(self):
        day = time.strftime('%Y-%m-%d')
        if day != self._emg_day:
            self._emg_day = day
            self._emg_trigger_count_today = 0
            self._day_fuse_on = False

    def _enter_day_fuse_mode(self):
        self._day_fuse_on = True
        try:
            self._cancel_open_orders_for_side('long')
            self._cancel_open_orders_for_side('short')
        except Exception as e:
            logger.warning(f"[EMG] Error while cancelling orders during circuit-breaker entry: {e}")
        logger.warning(f"[EMG][{self.symbol}] Daily triggers reached {self.emg_daily_fuse_count}+; circuit breaker active: keep only reduceOnly take-profit/stop-loss orders")





    def _record_price(self, price: float):
        try:
            if price and price > 0:
                self._vol_prices.append(float(price))
        except Exception:
            pass

    def _is_extreme_vol(self) -> bool:
        if len(self._vol_prices) < 10:
            return False
        hi = max(self._vol_prices)
        lo = min(self._vol_prices)
        mid = (hi + lo) / 2.0 if (hi + lo) else 0.0
        if mid == 0:
            return False
        
        volatility = (hi - lo) / mid
        is_extreme = volatility >= 0.006
        
        if is_extreme:
            # 避免重复通知，只在波动率变化显著时通知，并增加时间间隔控制
            current_time = time.time()
            if (not hasattr(self, '_last_volatility_notification') or 
                abs(volatility - getattr(self, '_last_volatility_notification', 0)) >= 0.002 or
                current_time - getattr(self, '_last_volatility_time', 0) >= 300):  # 至少5分钟间隔
                self._last_volatility_notification = volatility
                self._last_volatility_time = current_time
                logger.info(f"[EMG] Extreme volatility detected: high={hi:.8f}, low={lo:.8f}, volatility={volatility:.4f} ({volatility*100:.2f}%)")
        
        return is_extreme

    async def _emg_reduce_side_batched(self, side: str, qty_total: float):
        batches = max(1, int(self.emg_batches))
        if batches == 1:
            parts = [qty_total]
        else:
            base = qty_total / batches
            parts = [round(base, self.amount_precision)] * (batches - 1)
            last = max(self.min_order_amount, qty_total - sum(parts))
            parts.append(last)

        logger.info(f"[EMG] Starting reduction for {side}; total quantity: {qty_total}, batches: {len(parts)}")
        
        # 发送减仓开始通知
        await self._send_reduction_start_notification(side, qty_total, len(parts))

        for i, part in enumerate(parts, 1):
            try:
                lp, sp = self._get_position()
                if lp is not None:
                    self.long_position = lp
                if sp is not None:
                    self.short_position = sp
            except Exception:
                pass

            if side == 'long' and self.long_position < self.emg_exit_ratio * self.position_threshold:
                logger.info(f"[EMG] {side} position is back in the safe zone; stopping reduction")
                # 发送提前完成通知
                await self._send_reduction_early_complete_notification(side, i-1, len(parts))
                break
            if side == 'short' and self.short_position < self.emg_exit_ratio * self.position_threshold:
                logger.info(f"[EMG] {side} position is back in the safe zone; stopping reduction")
                # 发送提前完成通知
                await self._send_reduction_early_complete_notification(side, i-1, len(parts))
                break

            ok = False
            try:
                bid, ask = self._get_best_quotes()
                slip = self.emg_slip_cap_bp / 10000.0
                if side == 'long' and bid:
                    limit_price = bid * (1 - slip)
                    self._place_order('sell', price=limit_price, quantity=part, is_reduce_only=True, position_side='long', order_type='limit')
                    ok = True
                    # 减少日志频率，只在关键批次记录
                    if i == 1 or i == len(parts):
                        logger.info(f"[EMG] {side} batch {i} limit reduction succeeded: sell {part} @ {limit_price:.8f}")
                elif side == 'short' and ask:
                    limit_price = ask * (1 + slip)
                    self._place_order('buy', price=limit_price, quantity=part, is_reduce_only=True, position_side='short', order_type='limit')
                    ok = True
                    # 减少日志频率，只在关键批次记录
                    if i == 1 or i == len(parts):
                        logger.info(f"[EMG] {side} batch {i} limit reduction succeeded: buy {part} @ {limit_price:.8f}")
            except Exception as e:
                logger.warning(f"[EMG] Limit reduction error ({side} batch {i}): {e}")

            if not ok:
                try:
                    if side == 'long':
                        self._place_order('sell', price=None, quantity=part, is_reduce_only=True, position_side='long', order_type='market')
                        logger.info(f"[EMG] {side} batch {i} market reduction succeeded: sell {part}")
                    else:
                        self._place_order('buy', price=None, quantity=part, is_reduce_only=True, position_side='short', order_type='market')
                        logger.info(f"[EMG] {side} batch {i} market reduction succeeded: buy {part}")
                except Exception as e:
                    logger.error(f"[EMG] Market reduction failed ({side} batch {i}): {e}")

            # 修复异步问题：使用asyncio.sleep替代time.sleep
            if i < len(parts):  # 最后一批不需要等待
                await asyncio.sleep(self.emg_batch_sleep_ms / 1000.0)
        
        # 发送减仓完成通知
        await self._send_reduction_complete_notification(side, qty_total, len(parts))

    def _get_best_quotes(self):
        try:
            t = self.exchange.fetch_ticker(self.ccxt_symbol)
            bid = t.get('bid') or t.get('info', {}).get('bidPrice')
            ask = t.get('ask') or t.get('info', {}).get('askPrice')
            return float(bid) if bid else None, float(ask) if ask else None
        except Exception as e:
            logger.warning(f"[EMG] Failed to fetch quote: {e}")
            return None, None

    def stop(self):
        """停止机器人"""
        logger.info("Stopping bot...")
        self.running = False
        # 发送停止通知
        asyncio.create_task(self._send_telegram_message("🛑 **Bot manually stopped**\n\nThe grid bot was stopped by the user.", urgent=False, silent=True))

    async def start(self):
        """启动机器人"""
        try:
            logger.info("Grid trading bot starting...")
            
            # 初始化时获取一次持仓数据
            self.long_position, self.short_position = self._get_position()
            logger.info(f"Initial positions: long {self.long_position}, short {self.short_position}")

            # 等待状态同步完成
            await asyncio.sleep(5)

            # 初始化时获取一次挂单状态
            try:
                self._check_orders_status()
            except Exception as e:
                logger.warning(f"Initial order sync failed; continuing with empty order state: {e}")
            # 仅用本地持久化恢复装死状态（不读取订单、不反推）
            try:
                self._restore_lockdown_from_local()
            except Exception as e:
                logger.warning(f"Initial lockdown restore failed; continuing: {e}")

            logger.info(
                f"Initial order state: long entry={self.buy_long_orders}, long take-profit={self.sell_long_orders}, short entry={self.sell_short_orders}, short take-profit={self.buy_short_orders}")

            self._log_market_guard_startup()

            # 发送启动通知
            await self._send_startup_notification()

            # 设置运行状态
            self.running = True

            # 启动 listenKey 更新任务
            asyncio.create_task(self._keep_listen_key_alive())

            # 启动 WebSocket 连接
            while self.running:
                try:
                    await self._connect_websocket()
                except Exception as e:
                    logger.error(f"WebSocket connection failed: {e}")
                    await self._send_error_notification(str(e), "WebSocket connection failed")
                    await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Startup failed: {e}")
            await self._send_error_notification(str(e), "Startup failed")
            raise e

    async def _send_daily_circuit_breaker_notification(self):
        """Send daily circuit-breaker notification"""
        message = f"""
🚫 **Daily circuit breaker activated**

⚠️ **Trigger conditions**
• Emergency reductions today: {self.emergency_mode['daily_trigger_count']}
• Maximum allowed reached: 3

🛑 **Restrictions**
• No new entries for the rest of the day
• Keep only existing take-profit orders
• Auto-reset at midnight

📊 **Risk note**
• Market volatility is high; trade carefully
• Consider adjusting strategy parameters manually
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_emergency_enter_notification(self, enter_ratio):
        """Send emergency reduction entry notification"""
        message = f"""
🚨 **Emergency reduction triggered**

📊 **Position status**
• Symbol: {self.symbol}
• Long position: {self.long_position}
• Short position: {self.short_position}
• Trigger threshold: {enter_ratio:.2f} × {self.position_threshold:.2f} = {enter_ratio * self.position_threshold:.2f}

⚡ **Actions**
• Cancel all entry orders
• Reduce positions in batches
• Pause grid entries for {self.grid_pause_after_emg_s} seconds
• Temporarily adjust parameters: 70% order size, 1.3x grid spacing

📈 **Daily stats**
• Trigger count: {self._emg_trigger_count_today}
• Cooldown: {self.emg_cooldown_s} seconds
• Remaining triggers: {self.emg_daily_fuse_count - self._emg_trigger_count_today}

⏰ **Triggered at**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_emergency_exit_notification(self):
        """Send emergency reduction exit notification"""
        message = f"""
✅ **Emergency reduction cleared**

📊 **Current positions**
• Symbol: {self.symbol}
• Long position: {self.long_position}
• Short position: {self.short_position}
• Safety threshold: {self.emg_exit_ratio:.2f} × {self.position_threshold:.2f} = {self.emg_exit_ratio * self.position_threshold:.2f}

🔄 **Parameter recovery**
• Gradually restoring original parameters
• 10% every 5 minutes
• Estimated recovery time: 15-20 minutes

📈 **Daily stats**
• Triggers so far: {self._emg_trigger_count_today}
• Remaining triggers: {self.emg_daily_fuse_count - self._emg_trigger_count_today}

⏰ **Cleared at**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_daily_fuse_notification(self):
        """Send daily circuit-breaker notification"""
        message = f"""
🚫 **Daily circuit breaker activated**

⚠️ **Trigger conditions**
• Symbol: {self.symbol}
• Emergency reductions today: {self._emg_trigger_count_today}
• Maximum allowed reached: {self.emg_daily_fuse_count}

🛑 **Restrictions**
• No new entries for the rest of the day
• Keep only existing take-profit orders
• Auto-reset at midnight

📊 **Risk note**
• Market volatility is high; trade carefully
• Consider adjusting strategy parameters manually
• Check market conditions and strategy settings

⏰ **Activated at**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=True)
    
    async def _send_reduction_start_notification(self, side: str, qty_total: float, batch_count: int):
        """Send reduction-start notification"""
        side_name = "long" if side == 'long' else "short"
        action = "sell" if side == 'long' else "buy"
        
        message = f"""
🔄 **Emergency reduction started**

📊 **Reduction details**
• Symbol: {self.symbol}
• Side: {side_name}
• Total quantity: {qty_total}
• Batches: {batch_count}
• Action: {action}

⚡ **Execution strategy**
• Prefer limit orders (slippage tolerance: {self.emg_slip_cap_bp} bps)
• Fall back to market orders if limit orders fail
• Batch interval: {self.emg_batch_sleep_ms} ms

⏰ **Started at**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_reduction_early_complete_notification(self, side: str, completed_batches: int, total_batches: int):
        """Send early-completion notification"""
        side_name = "long" if side == 'long' else "short"
        
        message = f"""
✅ **Emergency reduction completed early**

📊 **Completion details**
• Symbol: {self.symbol}
• Side: {side_name}
• Completed batches: {completed_batches}/{total_batches}
• Reason: position returned to the safe zone

🎯 **Safe state**
• Position is now below the exit threshold
• No further reduction is needed
• Parameter recovery will begin

⏰ **Completed at**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
    async def _send_reduction_complete_notification(self, side: str, qty_total: float, batch_count: int):
        """Send reduction-complete notification"""
        side_name = "long" if side == 'long' else "short"
        action = "sell" if side == 'long' else "buy"
        
        message = f"""
✅ **Emergency reduction completed**

📊 **Execution results**
• Symbol: {self.symbol}
• Side: {side_name}
• Total quantity: {qty_total}
• Batches: {batch_count}
• Action: {action}

🔄 **Next steps**
• Reduction is complete
• Parameter recovery will begin
• Grid entries will remain paused

⏰ **Completed at**: {time.strftime("%Y-%m-%d %H:%M:%S")}
"""
        await self._send_telegram_message(message, urgent=False)
    
