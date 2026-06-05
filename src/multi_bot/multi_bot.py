import asyncio
import os
import sys
import signal
import threading
import time
import yaml
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
import sys
import os
sys.path.append(os.path.dirname(__file__))
from binance_multi_bot import BinanceGridBot
from logging_config import setup_logging, create_bot_logger, DailyStatusLogger
from sideways_scanner import build_scanner_settings, select_sideways_symbol_configs

# Load environment variables
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(PROJECT_ROOT, "config", ".env"))

# Global state used to control all bots
running_bots = {}
stop_event = threading.Event()

# Configure the optimized logging system
main_logger = setup_logging()
daily_status_logger = DailyStatusLogger(main_logger)

def load_config(config_file='config/symbols.yaml'):
    """Load the configuration file"""
    if not os.path.exists(config_file):
        main_logger.error(f"Configuration file {config_file} does not exist")
        return None
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            if config_file.endswith('.yaml') or config_file.endswith('.yml'):
                config = yaml.safe_load(f)
            elif config_file.endswith('.json'):
                config = json.load(f)
            else:
                main_logger.error(f"Unsupported configuration file format: {config_file}")
                return None
        
        # 验证配置格式
        if 'symbols' not in config:
            main_logger.error("Missing 'symbols' field in the configuration file")
            return None
        
        if not isinstance(config['symbols'], list):
            main_logger.error("'symbols' must be a list")
            return None
        
        # 验证每个币种配置
        for i, symbol_config in enumerate(config['symbols']):
            if 'name' not in symbol_config:
                main_logger.error(f"Symbol config #{i+1} is missing the 'name' field")
                return None
            
            # 设置默认值
            if 'grid_spacing' not in symbol_config:
                symbol_config['grid_spacing'] = 0.001
            if 'initial_quantity' not in symbol_config:
                symbol_config['initial_quantity'] = 3
            if 'leverage' not in symbol_config:
                symbol_config['leverage'] = 20
            if 'contract_type' not in symbol_config:
                symbol_config['contract_type'] = 'USDT'
            if 'maker_fee_rate' not in symbol_config:
                symbol_config['maker_fee_rate'] = 0.0002
            if 'taker_fee_rate' not in symbol_config:
                symbol_config['taker_fee_rate'] = 0.0005
            if 'funding_buffer_rate' not in symbol_config:
                symbol_config['funding_buffer_rate'] = 0.0003
            if 'min_net_edge_rate' not in symbol_config:
                symbol_config['min_net_edge_rate'] = 0.0010
            if 'max_spread_rate' not in symbol_config:
                symbol_config['max_spread_rate'] = 0.0010
            if 'range_filter_lookback' not in symbol_config:
                symbol_config['range_filter_lookback'] = 60
            if 'range_min_samples' not in symbol_config:
                symbol_config['range_min_samples'] = 20
            if 'range_min_pct' not in symbol_config:
                symbol_config['range_min_pct'] = 0.0020
            if 'range_breakout_pct' not in symbol_config:
                symbol_config['range_breakout_pct'] = 0.0080
            if 'range_pause_seconds' not in symbol_config:
                symbol_config['range_pause_seconds'] = 300

        if 'scanner' not in config or not isinstance(config.get('scanner'), dict):
            config['scanner'] = {
                'enabled': False,
                'timeframe': '15m',
                'lookback': 96,
                'top_n': 3,
                'min_score': 0.55,
                'min_range_pct': 0.0020,
                'ideal_range_pct': 0.0120,
                'max_range_pct': 0.0500,
                'max_direction_pct': 0.0060,
                'max_spread_pct': 0.0015,
                'min_quote_volume': 0.0,
                'max_funding_abs': 0.0005,
            }
        
        main_logger.info(f"Configuration file loaded successfully: {config_file}")
        return config
    
    except Exception as e:
        main_logger.error(f"Failed to load configuration file: {e}")
        return None

def validate_environment():
    """Validate environment variables"""
    api_key = os.getenv("API_KEY", "")
    api_secret = os.getenv("API_SECRET", "")
    
    if not api_key or not api_secret:
        main_logger.error("API_KEY and API_SECRET must be set in the .env file")
        return None, None
    
    # 验证其他可选配置
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enable_notifications = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
    
    if enable_notifications:
        if not telegram_bot_token or not telegram_chat_id:
            main_logger.warning("Telegram notifications are enabled but BOT_TOKEN or CHAT_ID is missing; notifications will be disabled")
        else:
            main_logger.info("Telegram notifications are enabled")
    
    return api_key, api_secret

def create_bot_logger(symbol):
    """Create a dedicated logger for each symbol"""
    from logging_config import create_bot_logger as create_logger
    return create_logger(symbol)

def run_single_bot(symbol_config, api_key, api_secret):
    """Run a single-symbol grid bot"""
    symbol = symbol_config['name']
    logger = create_bot_logger(symbol)
    
    try:
        # 构建配置字典
        config = {
            'grid_spacing': symbol_config['grid_spacing'],
            'initial_quantity': symbol_config['initial_quantity'],
            'leverage': symbol_config['leverage'],
            'contract_type': symbol_config['contract_type'],
            'maker_fee_rate': symbol_config['maker_fee_rate'],
            'taker_fee_rate': symbol_config['taker_fee_rate'],
            'funding_buffer_rate': symbol_config['funding_buffer_rate'],
            'min_net_edge_rate': symbol_config['min_net_edge_rate'],
            'max_spread_rate': symbol_config['max_spread_rate'],
            'range_filter_lookback': symbol_config['range_filter_lookback'],
            'range_min_samples': symbol_config['range_min_samples'],
            'range_min_pct': symbol_config['range_min_pct'],
            'range_breakout_pct': symbol_config['range_breakout_pct'],
            'range_pause_seconds': symbol_config['range_pause_seconds'],
        }
        
        logger.info(f"Starting {symbol} grid bot")
        logger.info(f"Config: grid spacing={config['grid_spacing']:.3f}, initial quantity={config['initial_quantity']}, leverage={config['leverage']}")
        
        # Create the bot instance
        bot = BinanceGridBot(symbol=symbol, api_key=api_key, api_secret=api_secret, config=config)
        
        # Store the bot instance for shutdown
        running_bots[symbol] = bot
        
        # Create an event loop in a new thread and run the bot
        def run_bot_with_loop():
            try:
                # Create a new event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Run the bot
                loop.run_until_complete(bot.start())
            except Exception as e:
                logger.error(f"Bot runtime error: {e}")
            finally:
                try:
                    loop.close()
                except:
                    pass
        
        # Run in a new thread
        import threading
        bot_thread = threading.Thread(target=run_bot_with_loop, name=f"bot-{symbol}")
        bot_thread.daemon = True
        bot_thread.start()
        
        # Give the thread a moment to start
        import time
        time.sleep(0.1)
        
        # Wait until the bot really starts
        max_wait_time = 30  # Wait at most 30 seconds
        wait_time = 0
        while wait_time < max_wait_time:
            if symbol in running_bots:
                # Check whether the bot is actually running
                bot = running_bots[symbol]
                if hasattr(bot, 'running') and bot.running:
                    logger.info(f"{symbol} bot added to the running list")
                    return symbol, True, None
            time.sleep(1)
            wait_time += 1
        
        # Remove it from the running list if it timed out
        if symbol in running_bots:
            del running_bots[symbol]
        
        return symbol, False, "Bot startup timed out"
        
    except Exception as e:
        error_msg = f"Failed to start {symbol} bot: {str(e)}"
        logger.error(error_msg)
        return symbol, False, error_msg

def signal_handler(signum, frame):
    """
    Signal handler used to stop all bots gracefully
    """
    main_logger.info("Stop signal received; stopping all bots...")
    stop_event.set()
    
    # Stop all bots
    for symbol, bot in running_bots.items():
        try:
            bot.stop()
            main_logger.info(f"Stopped {symbol} bot")
        except Exception as e:
            main_logger.error(f"Failed to stop {symbol} bot: {e}")
    
    sys.exit(0)

def print_status():
    """
    Print current runtime status and write the summary log
    """
    while not stop_event.is_set():
        try:
            active_bots = len(running_bots)
            if active_bots > 0:
                symbols = list(running_bots.keys())
                status_info = f"Active bots: {active_bots} - {', '.join(symbols)}"
                # Use the daily status logger once per day
                daily_status_logger.log_status(status_info)
                
                # Write the summary log (keep the live updates)
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                status_summary = f"[{timestamp}] Active Bots: {', '.join([f'{s}=Running' for s in symbols])}"
                
                # Write the summary file
                try:
                    with open('log/status_summary.log', 'a', encoding='utf-8') as f:
                        f.write(status_summary + '\n')
                except Exception as e:
                    main_logger.error(f"Failed to write status summary log: {e}")
            else:
                daily_status_logger.log_status("No active bots")
                
                # Write the summary file
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                status_summary = f"[{timestamp}] Active bots: None"
                try:
                    with open('log/status_summary.log', 'a', encoding='utf-8') as f:
                        f.write(status_summary + '\n')
                except Exception as e:
                    main_logger.error(f"Failed to write status summary log: {e}")
                    
            time.sleep(30)  # Check every 30 seconds
        except KeyboardInterrupt:
            break

def maybe_select_sideways_symbols(config, symbols):
    """Return a filtered symbol list when the sideways scanner is enabled."""
    scanner_settings = build_scanner_settings(config)
    if not scanner_settings.get("enabled", False):
        return symbols, None

    main_logger.info(
        "Sideways scanner enabled: timeframe=%s lookback=%s top_n=%s min_score=%.2f",
        scanner_settings["timeframe"],
        scanner_settings["lookback"],
        scanner_settings["top_n"],
        scanner_settings["min_score"],
    )

    selected_symbols, ranked_scores = select_sideways_symbol_configs(
        symbols,
        settings=scanner_settings,
        logger=main_logger,
    )

    if not selected_symbols:
        main_logger.warning("Sideways scanner produced no selectable symbols; falling back to the configured list")
        return symbols, ranked_scores

    selected_names = [symbol_config["name"] for symbol_config in selected_symbols]
    main_logger.info("Sideways scanner selected %s/%s symbols: %s", len(selected_symbols), len(symbols), selected_names)
    return selected_symbols, ranked_scores

def main():
    """
    Main entry point
    """
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    main_logger.info("Multi-symbol grid trading bot starting...")
    
    # Validate environment variables
    api_key, api_secret = validate_environment()
    if not api_key or not api_secret:
        main_logger.error("Environment validation failed; exiting")
        sys.exit(1)
    
    # Load configuration
    config = load_config()
    if not config:
        main_logger.error("Configuration load failed; exiting")
        sys.exit(1)
    
    symbols = config['symbols']
    main_logger.info(f"Configured {len(symbols)} symbol(s): {[s['name'] for s in symbols]}")

    symbols, ranked_scores = maybe_select_sideways_symbols(config, symbols)
    if ranked_scores is not None:
        main_logger.info(
            "Sideways ranking: %s",
            ", ".join(
                f"{score.symbol}={score.score:.3f}" for score in ranked_scores[: min(len(ranked_scores), 10)]
            ) if ranked_scores else "none",
        )
        main_logger.info(f"Selected {len(symbols)} symbol(s) after sideways filtering: {[s['name'] for s in symbols]}")
    
    # Start the status monitor thread
    status_thread = threading.Thread(target=print_status, daemon=True)
    status_thread.start()
    
    # Start all bots directly, without a thread pool
    bot_threads = {}
    for symbol_config in symbols:
        symbol = symbol_config['name']
        main_logger.info(f"Starting {symbol} grid bot")
        
        # Create bot thread
        bot_thread = threading.Thread(
            target=run_single_bot, 
            args=(symbol_config, api_key, api_secret),
            name=f"bot-{symbol}",
            daemon=True
        )
        bot_thread.start()
        bot_threads[symbol] = bot_thread
    
    # Wait for all bots to finish starting
    main_logger.info("Waiting for all bots to finish starting...")
    for symbol, thread in bot_threads.items():
        thread.join(timeout=60)  # Wait up to 60 seconds
    
    # Main loop: monitor bot state
    try:
        while not stop_event.is_set():
            active_bots = len(running_bots)
            if active_bots > 0:
                symbols = list(running_bots.keys())
                # Use the daily status logger once per day
                daily_status_logger.log_status(f"Active bots: {active_bots} - {', '.join(symbols)}")
            else:
                daily_status_logger.log_status("No active bots")
            
            time.sleep(30)  # Check every 30 seconds
    except KeyboardInterrupt:
        main_logger.info("Interrupt signal received; stopping all bots...")
        stop_event.set()
        
        # Stop all bots
        for symbol, bot in running_bots.items():
            try:
                bot.stop()
                main_logger.info(f"Stopped {symbol} bot")
            except Exception as e:
                main_logger.error(f"Failed to stop {symbol} bot: {e}")

if __name__ == "__main__":
    main()
