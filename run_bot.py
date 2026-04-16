#!/usr/bin/env python3
"""
Scalper Bot - Main Entry Point
=====================================
5-Minute Scalping Bot for MT5 symbols (including GOLD and BTCUSD)

This script provides a simple interface to run the trading bot.
Make sure MT5 is running and logged into your trading account.

Usage:
    python run_bot.py           # Run with existing MT5 session
    python run_bot.py --demo    # Run in demo/paper mode only
    python run_bot.py --backtest # Run backtest first

Author: Scalper Bot
Version: 1.0
"""

import sys
import os
import argparse
import logging
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f'scalper_{datetime.now().strftime("%Y%m%d")}.log')
    ]
)
logger = logging.getLogger(__name__)


def print_banner():
    """Print welcome banner"""
    banner = """
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║           SCALPER BOT v1.0                                   ║
║           5-Minute Multi-Asset Strategy                      ║
║                                                              ║
║           MetaTrader 5 Live Trading                          ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
    print(banner)


def check_mt5():
    """Check if MT5 is available"""
    try:
        import MetaTrader5 as mt5
        return True
    except ImportError:
        logger.error("MetaTrader5 not installed!")
        logger.error("Install with: pip install MetaTrader5")
        return False


def run_backtest():
    """Run a backtest with sample data"""
    from gold_scalper import ScalpingStrategy, RiskManager, Backtester, generate_sample_data
    from config import STRATEGY_CONFIG, RISK_CONFIG
    
    logger.info("Running backtest with sample data...")
    logger.info("")
    
    # Generate sample data
    df = generate_sample_data(days=30, timeframe='5min')
    logger.info(f"Loaded {len(df)} bars of 5-minute data")
    
    # Initialize strategy and risk manager
    strategy = ScalpingStrategy(**STRATEGY_CONFIG)
    risk_manager = RiskManager(**RISK_CONFIG)
    
    # Run backtest
    backtester = Backtester(strategy, risk_manager)
    results = backtester.run(df)
    
    # Print results
    logger.info("")
    logger.info("=" * 50)
    logger.info("BACKTEST RESULTS")
    logger.info("=" * 50)
    logger.info(f"Total Trades:     {results['total_trades']}")
    logger.info(f"Win Rate:         {results['win_rate']:.1f}%")
    logger.info(f"Total P&L:        ${results['total_pnl']:,.2f}")
    logger.info(f"Profit Factor:    {results['profit_factor']:.2f}")
    logger.info(f"Max Drawdown:     {results['max_drawdown']:.2f}%")
    logger.info(f"Final Balance:    ${results['final_balance']:,.2f}")
    logger.info("=" * 50)
    logger.info("")
    
    return results


def run_live_trading(demo_only=False):
    """Run the live trading bot"""
    from live_trading import MT5Adapter, LiveTradingBot, get_symbol_profile
    from gold_scalper import ScalpingStrategy, RiskManager
    from config import BROKER_CONFIG, STRATEGY_CONFIG, RISK_CONFIG, TRADING_CONFIG
    
    # Initialize MT5 adapter
    adapter = MT5Adapter(
        account=int(BROKER_CONFIG['account_id']) if BROKER_CONFIG.get('account_id') else None,
        password=BROKER_CONFIG.get('password') or None,
        server=BROKER_CONFIG.get('server') or None,
        symbol=TRADING_CONFIG.get('symbol', 'BTCUSD')
    )
    
    # Connect to MT5
    logger.info("Connecting to MetaTrader 5...")
    if not adapter.connect():
        logger.error("Failed to connect to MT5")
        logger.error("Make sure MT5 is running and logged into your account")
        return False
    
    # Get account info
    account_info = adapter.get_account_info()
    if not account_info:
        logger.error("Failed to get account info")
        adapter.disconnect()
        return False
    
    logger.info("")
    logger.info("=" * 50)
    logger.info("ACCOUNT INFORMATION")
    logger.info("=" * 50)
    logger.info(f"Login:          {account_info['login']}")
    logger.info(f"Server:         {account_info['server']}")
    logger.info(f"Balance:        ${account_info['balance']:,.2f}")
    logger.info(f"Equity:         ${account_info['equity']:,.2f}")
    logger.info(f"Leverage:       1:{account_info['leverage']}")
    logger.info(f"Trade Allowed:  {account_info['trade_allowed']}")
    logger.info("=" * 50)
    
    # Check if trading is allowed
    if not account_info['trade_allowed']:
        logger.error("Trading is not allowed on this account!")
        adapter.disconnect()
        return False
    
    # Demo mode warning
    if demo_only:
        logger.warning("")
        logger.warning("⚠️  DEMO MODE - No real trades will be executed")
        logger.warning("")
    else:
        logger.warning("")
        logger.warning("WARNING: LIVE TRADING MODE - Real money at risk!")
        logger.warning("")
        
        # Confirmation prompt
        try:
            response = input("Type 'START' to begin live trading: ")
            if response.upper() != 'START':
                logger.info("Cancelled by user")
                adapter.disconnect()
                return False
        except EOFError:
            # Non-interactive mode
            pass
    
    # Show symbol info
    symbol_info = adapter.get_symbol_info()
    if symbol_info:
        logger.info(f"Trading Symbol: {symbol_info['symbol']}")
        logger.info(f"Spread:         {symbol_info['spread']} points")
        logger.info(f"Min Lot:        {symbol_info['volume_min']}")
        logger.info(f"Max Lot:        {symbol_info['volume_max']}")
    
    # Initialize strategy and risk manager
    strategy = ScalpingStrategy(**STRATEGY_CONFIG)
    risk_manager = RiskManager(**RISK_CONFIG)
    risk_manager.account_balance = account_info['balance']

    profile = get_symbol_profile(
        TRADING_CONFIG.get('symbol', 'GOLD'),
        default_max_spread=TRADING_CONFIG.get('max_spread', 50.0),
        default_min_strength=TRADING_CONFIG.get('min_signal_strength', 50),
    )
    
    # Create trading bot
    bot = LiveTradingBot(
        adapter=adapter,
        strategy=strategy,
        risk_manager=risk_manager,
        timeframe=TRADING_CONFIG.get('timeframe', '5min'),
        max_spread=profile['max_spread'],
        min_signal_strength=profile['min_signal_strength'],
    )
    
    logger.info("")
    logger.info("Starting trading bot...")
    logger.info("Press Ctrl+C to stop")
    logger.info("")
    
    try:
        # Run the bot
        bot.start(check_interval=10)
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        adapter.disconnect()
    
    return True


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Scalper Bot - 5-Minute Scalping for MT5 symbols',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_bot.py              Run the live trading bot
  python run_bot.py --backtest   Run backtest with sample data
  python run_bot.py --demo       Run in demo mode (paper trading)
  python run_bot.py --info       Show account information only

Make sure MetaTrader 5 is:
  1. Installed and running
  2. Logged into your trading account
    3. Your configured symbol is available (GOLD/BTCUSD/etc.)
        """
    )
    
    parser.add_argument('--backtest', action='store_true', 
                        help='Run backtest with sample data')
    parser.add_argument('--demo', action='store_true',
                        help='Run in demo/paper mode only')
    parser.add_argument('--info', action='store_true',
                        help='Show account info and exit')
    parser.add_argument('--check', action='store_true',
                        help='Check MT5 connection and exit')
    
    args = parser.parse_args()
    
    # Print banner
    print_banner()
    
    # Check MT5
    if not check_mt5():
        return 1
    
    # Handle different modes
    if args.check:
        logger.info("Checking MT5 connection...")
        from live_trading import MT5Adapter
        import MetaTrader5 as mt5
        
        # First just initialize to check connection
        if not mt5.initialize():
            logger.error("MT5 connection failed!")
            return 1
        
        account_info = mt5.account_info()
        if account_info:
            logger.info(f"MT5 connected successfully!")
            logger.info(f"Account: {account_info.login}")
            logger.info(f"Balance: ${account_info.balance:,.2f}")
            
            # List available gold/crypto symbols
            symbols = mt5.symbols_get()
            trade_symbols = [
                s.name for s in symbols
                if any(tok in s.name.upper() for tok in ('XAU', 'GOLD', 'BTC', 'XBT', 'CRYPTO'))
            ]
            if trade_symbols:
                logger.info(f"Available Gold/Crypto symbols: {', '.join(trade_symbols)}")
            else:
                logger.warning("No Gold/Crypto symbols found. Check your broker.")
        
        mt5.shutdown()
        return 0
    
    if args.info:
        from live_trading import MT5Adapter
        from config import TRADING_CONFIG
        adapter = MT5Adapter(symbol=TRADING_CONFIG.get('symbol', 'BTCUSD'))
        if adapter.connect():
            account_info = adapter.get_account_info()
            symbol_info = adapter.get_symbol_info()
            price_info = adapter.get_current_price()
            
            if account_info:
                print(f"\nAccount: {account_info['login']}")
                print(f"Server: {account_info['server']}")
                print(f"Balance: ${account_info['balance']:,.2f}")
                print(f"Equity: ${account_info['equity']:,.2f}")
                print(f"Leverage: 1:{account_info['leverage']}")
            
            if symbol_info:
                print(f"\nSymbol: {symbol_info['symbol']}")
                print(f"Spread: {symbol_info['spread']} points")
            
            if price_info:
                print(f"\nBid: {price_info['bid']:.2f}")
                print(f"Ask: {price_info['ask']:.2f}")
                print(f"Spread: {price_info['spread']:.2f}")
            
            adapter.disconnect()
            return 0
        return 1
    
    if args.backtest:
        run_backtest()
        
        # Ask to continue with live trading
        try:
            response = input("\nStart live trading? (y/n): ")
            if response.lower() != 'y':
                logger.info("Exiting...")
                return 0
        except EOFError:
            return 0
    
    # Run live trading
    run_live_trading(demo_only=args.demo)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
