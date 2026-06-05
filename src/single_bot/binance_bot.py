import asyncio
import os
import logging
from dotenv import load_dotenv
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'multi_bot'))
from binance_multi_bot import BinanceGridBot

# Load environment variables
load_dotenv()

# Configuration validation
def validate_config():
    """Validate configuration parameters"""
    api_key = os.getenv("API_KEY", "")
    api_secret = os.getenv("API_SECRET", "")
    
    if not api_key or not api_secret:
        raise ValueError("API_KEY and API_SECRET must be set")
    
    grid_spacing = float(os.getenv("GRID_SPACING", "0.001"))
    if grid_spacing <= 0 or grid_spacing >= 1:
        raise ValueError("GRID_SPACING must be between 0 and 1")
    
    initial_quantity = float(os.getenv("INITIAL_QUANTITY", "3"))
    if initial_quantity <= 0:
        raise ValueError("INITIAL_QUANTITY must be greater than 0")
    
    leverage = int(os.getenv("LEVERAGE", "20"))
    if leverage <= 0 or leverage > 100:
        raise ValueError("LEVERAGE must be between 1 and 100")

    maker_fee_rate = float(os.getenv("MAKER_FEE_RATE", "0.0002"))
    taker_fee_rate = float(os.getenv("TAKER_FEE_RATE", "0.0005"))
    funding_buffer_rate = float(os.getenv("FUNDING_BUFFER_RATE", "0.0003"))
    min_net_edge_rate = float(os.getenv("MIN_NET_EDGE_RATE", "0.0010"))
    max_spread_rate = float(os.getenv("MAX_SPREAD_RATE", "0.0010"))
    range_filter_lookback = int(os.getenv("RANGE_FILTER_LOOKBACK", "60"))
    range_min_samples = int(os.getenv("RANGE_MIN_SAMPLES", "20"))
    range_min_pct = float(os.getenv("RANGE_MIN_PCT", "0.0020"))
    range_breakout_pct = float(os.getenv("RANGE_BREAKOUT_PCT", "0.0080"))
    range_pause_seconds = int(os.getenv("RANGE_PAUSE_SECONDS", "300"))

    if maker_fee_rate < 0 or taker_fee_rate < 0:
        raise ValueError("Fee rates must be non-negative")
    if funding_buffer_rate < 0 or min_net_edge_rate < 0 or max_spread_rate < 0:
        raise ValueError("Guard buffer rates must be non-negative")
    if range_filter_lookback <= 0 or range_min_samples <= 0 or range_pause_seconds <= 0:
        raise ValueError("Range guard timing values must be greater than 0")
    if range_min_pct <= 0 or range_breakout_pct <= 0:
        raise ValueError("Range guard thresholds must be greater than 0")
    
    # Validate Telegram configuration
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    enable_notifications = os.getenv("ENABLE_NOTIFICATIONS", "true").lower() == "true"
    binance_sandbox = os.getenv("BINANCE_SANDBOX", "false").lower() == "true"
    
    if enable_notifications:
        if not telegram_bot_token or not telegram_chat_id:
            print("Warning: Telegram notifications are enabled but BOT_TOKEN or CHAT_ID is missing; notifications will be disabled")
        else:
            print("Telegram notifications are enabled")
    
    coin_name = os.getenv("COIN_NAME", "XRP")
    contract_type = os.getenv("CONTRACT_TYPE", "USDT")
    
    mode_label = "Sandbox" if binance_sandbox else "Live"
    print(f"Configuration validated - symbol: {coin_name}, grid spacing: {grid_spacing}, initial quantity: {initial_quantity}, mode: {mode_label}")

async def main():
    try:
        # Validate configuration
        validate_config()
        
        # Load config from environment variables
        api_key = os.getenv("API_KEY", "")
        api_secret = os.getenv("API_SECRET", "")
        coin_name = os.getenv("COIN_NAME", "XRP")
        contract_type = os.getenv("CONTRACT_TYPE", "USDT")
        grid_spacing = float(os.getenv("GRID_SPACING", "0.001"))
        initial_quantity = float(os.getenv("INITIAL_QUANTITY", "3"))
        leverage = int(os.getenv("LEVERAGE", "20"))
        maker_fee_rate = float(os.getenv("MAKER_FEE_RATE", "0.0002"))
        taker_fee_rate = float(os.getenv("TAKER_FEE_RATE", "0.0005"))
        funding_buffer_rate = float(os.getenv("FUNDING_BUFFER_RATE", "0.0003"))
        min_net_edge_rate = float(os.getenv("MIN_NET_EDGE_RATE", "0.0010"))
        max_spread_rate = float(os.getenv("MAX_SPREAD_RATE", "0.0010"))
        range_filter_lookback = int(os.getenv("RANGE_FILTER_LOOKBACK", "60"))
        range_min_samples = int(os.getenv("RANGE_MIN_SAMPLES", "20"))
        range_min_pct = float(os.getenv("RANGE_MIN_PCT", "0.0020"))
        range_breakout_pct = float(os.getenv("RANGE_BREAKOUT_PCT", "0.0080"))
        range_pause_seconds = int(os.getenv("RANGE_PAUSE_SECONDS", "300"))
        
        # Build config dictionary
        config = {
            'grid_spacing': grid_spacing,
            'initial_quantity': initial_quantity,
            'leverage': leverage,
            'contract_type': contract_type,
            'maker_fee_rate': maker_fee_rate,
            'taker_fee_rate': taker_fee_rate,
            'funding_buffer_rate': funding_buffer_rate,
            'min_net_edge_rate': min_net_edge_rate,
            'max_spread_rate': max_spread_rate,
            'range_filter_lookback': range_filter_lookback,
            'range_min_samples': range_min_samples,
            'range_min_pct': range_min_pct,
            'range_breakout_pct': range_breakout_pct,
            'range_pause_seconds': range_pause_seconds,
        }
        
        # Build trading pair symbol
        symbol = f"{coin_name}{contract_type}"
        
        # Create and start the trading bot
        bot = BinanceGridBot(symbol=symbol, api_key=api_key, api_secret=api_secret, config=config)
        print("Grid trading bot starting...")
        await bot.start()
        
    except ValueError as e:
        print(f"Configuration error: {e}")
        exit(1)
    except KeyboardInterrupt:
        print("Stop signal received; shutting down the bot...")
        if 'bot' in locals():
            await bot.stop()
    except Exception as e:
        print(f"Runtime error: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
