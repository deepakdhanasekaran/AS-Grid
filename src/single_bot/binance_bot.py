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
        
        # Build config dictionary
        config = {
            'grid_spacing': grid_spacing,
            'initial_quantity': initial_quantity,
            'leverage': leverage,
            'contract_type': contract_type
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
