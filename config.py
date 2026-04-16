"""
Configuration file for Multi-Symbol 5-Minute Scalping Robot
=============================================================
Supports: GOLD (XAU/USD), EURUSD, GBPUSD, BTCUSD

STRATEGY: CRT + TBS (M5)

IMPORTANT: 
- Start with a demo account to test settings
- Risk per trade should not exceed 1-2% for safety
- Adjust ATR multipliers based on current volatility
"""

# STRATEGY PARAMETERS - Advanced Multi-Confluence Strategy
STRATEGY_CONFIG = {
    # Traditional Indicators
    'ema_fast': 9,              # Fast EMA period (9 periods = 45 min trend)
    'ema_slow': 21,             # Slow EMA period (21 periods = 105 min trend)
    'rsi_period': 14,           # RSI calculation period
    'rsi_oversold': 35,         # RSI oversold threshold (buy zone below this)
    'rsi_overbought': 65,       # RSI overbought threshold (sell zone above this)
    'bb_period': 20,            # Bollinger Bands period
    'bb_std': 2,                # Bollinger Bands standard deviation

    # Regime filter (new core logic)
    'trend_ema_fast': 50,       # Trend filter fast EMA on M5
    'trend_ema_slow': 200,      # Trend filter slow EMA on M5
    'adx_period': 14,           # ADX period for trend strength
    'min_adx': 18,              # Ignore weak/choppy trend below this ADX
    'pullback_atr_mult': 0.35,  # Pullback tolerance around fast EMA
    'momentum_lookback': 3,     # Breakout confirmation lookback bars
    'require_kill_zone': False, # Kill zone is a score bonus (+1) not a hard gate
    'volume_confirm_mult': 1.1, # Volume filter vs rolling average
    
    # Advanced Strategy Parameters
    'min_confluence_score': 5,  # Minimum score to trigger signal (5 = ~3-8 trades/day)
                                # Lower = more trades, Higher = fewer but stronger signals
}

# 5-MIN BOT STRATEGY: CRT + TBS (Candle Range Theory + Trend Break Structure)
# HTF periods are expressed in M5 bars:
# - H1 ~= 12 bars, H4 ~= 48 bars, D1 ~= 288 bars.
CRT_TBS_CONFIG = {
    'htf_d1_period': 288,
    'htf_h4_period': 48,
    'htf_h1_period': 12,
    'htf_m15_period': 3,
    'atr_period': 14,
    'crt_lookback': 4,
    'min_ref_size_atr': 0.20,
    'min_sweep_depth_atr': 0.08,
    'min_score': 4,
    'require_d1': False,
    'tbs_fast_ema': 20,
    'tbs_slow_ema': 50,
    'tbs_breakout_lookback': 6,
}

# SMART MONEY CONCEPTS (SMC) PARAMETERS
SMC_CONFIG = {
    'swing_lookback': 10,       # Bars to look for swing points
    'fvg_threshold': 0.5,       # FVG min size as ATR multiplier
    'order_block_atr_mult': 1.5,  # Min impulse size for order block
}

# ICT CONCEPTS PARAMETERS
ICT_CONFIG = {
    'lookback': 50,             # Bars for premium/discount calculation
    'ote_lower': 0.62,          # OTE zone lower bound (61.8%)
    'ote_upper': 0.79,          # OTE zone upper bound (78.6%)
    'kill_zones': {
        'london_open': (7, 10),   # UTC hours
        'ny_open': (12, 15),
        'london_close': (15, 17),
    }
}

# ORDER FLOW PARAMETERS
ORDER_FLOW_CONFIG = {
    'volume_lookback': 20,      # Bars for volume analysis
    'high_volume_mult': 1.5,    # Multiplier for high volume detection
    'very_high_volume_mult': 2.0,
}

# FIBONACCI PARAMETERS
FIBONACCI_CONFIG = {
    'lookback': 50,             # Bars for swing detection
    'retracement_levels': [0.236, 0.382, 0.5, 0.618, 0.786],
    'extension_levels': [1.0, 1.272, 1.618, 2.0],
    'confluence_tolerance': 0.002,  # 0.2% tolerance for level matching
}

# RISK MANAGEMENT - Dynamic lot sizing based on account balance
# $100-$999:   0.01-0.03 lots, max 5 positions
# $1000-$9999: 0.04-0.09 lots, max 10 positions  
# $10000+:     1.0 lots, max 10 positions
RISK_CONFIG = {
    'account_balance': 10000,           # Starting capital (auto-updated from account)
    'risk_per_trade': 0.01,             # Risk 1% per trade
    'max_positions': 5,                 # Maximum concurrent positions (overridden by RiskManager based on balance)
    'max_positions_per_symbol': 1,      # Max positions per individual symbol
    'stop_loss_atr_multiplier': 1.5,    # Stop loss distance in ATR multiples
    'take_profit_atr_multiplier': 2.5,  # Take profit distance in ATR multiples (1.5:2.5 = 1:1.67 RR)
}

# DAILY PROFIT GOAL SETTINGS
DAILY_GOAL_CONFIG = {
    'enabled': True,                    # Enable daily profit goal
    'daily_target': 20.0,               # Daily profit target in USD
    'action_on_goal': 'trail_and_stop',      # 'close_all' = close all trades when goal reached
                                        # 'trailing_tp' = move TP to lock in profits
                                        # 'stop_trading' = stop opening new trades but let existing run
    'trailing_tp_pips': 10,             # If trailing_tp: move TP this many pips in profit
    'reset_hour_utc': 0,                # Hour (UTC) when daily P&L resets (0 = midnight)
    'max_daily_loss': -50.0,            # Stop trading if daily loss exceeds this (negative value)
}

# EMAIL NOTIFICATIONS
# Configure SMTP credentials to enable trade-close, daily-summary, and risk alert emails.
# Leave enabled=False until you are ready to receive messages.
EMAIL_CONFIG = {
    'enabled': False,
    'smtp_host': '',                    # e.g. smtp.gmail.com
    'smtp_port': 587,
    'use_tls': True,
    'use_ssl': False,
    'username': '',                     # SMTP username / login email
    'password': '',                     # SMTP password or app password
    'from_email': '',                   # Sender address (defaults to username)
    'to_email': '',                     # Recipient address
    'daily_summary_time_utc': '23:55',  # Send one daily summary email after this UTC time
    'risk_alert_thresholds': [25.0, 50.0, 100.0],  # Open loss reach buckets in USD
}

# MULTI-SYMBOL TRADING CONFIGURATION
# Each symbol can have its own settings
SYMBOLS_CONFIG = {
    'GOLD': {
        'enabled': True,
        'symbol': 'GOLD',               # Exact symbol name in MT5 (check with find_symbol.py)
        'max_spread': 50.0,             # Max spread in points for Gold
        'min_signal_strength': 40,
        'lot_size_multiplier': 1.0,     # Adjust position size (1.0 = normal)
    },
    'EURUSD': {
        'enabled': True,
        'symbol': 'EURUSD',             # Exact symbol name in MT5
        'max_spread': 20.0,             # Max spread in points for EUR/USD
        'min_signal_strength': 40,
        'lot_size_multiplier': 1.0,
    },
    'GBPUSD': {
        'enabled': True,
        'symbol': 'GBPUSD',             # Exact symbol name in MT5
        'max_spread': 25.0,             # Max spread in points for GBP/USD
        'min_signal_strength': 40,
        'lot_size_multiplier': 1.0,
    },
    'BTCUSD': {
        'enabled': True,
        'symbol': 'BTCUSD',             # Broker may expose BTCUSDm/XBTUSD/BTCUSDT (alias resolution handles this)
        'max_spread': 1200.0,           # Crypto spreads are structurally wider than FX/Metals
        'min_signal_strength': 50,      # Lowered to avoid long no-trade periods on BTC
        'btc_fallback_min_strength': 35,  # If strict signal is NONE, allow score-bias entries above this strength
        'btc_fallback_min_score_gap': 2,  # Minimum abs(buy_score-sell_score) for BTC fallback
        'lot_size_multiplier': 0.35,    # Reduce effective risk on higher-volatility BTC moves
    },
}

# BTC execution/tuning presets for quick profile switching.
# Notes:
# - conservative: fewer trades, tighter risk, higher confirmation
# - balanced: default production profile
# - aggressive: more entries, higher risk tolerance
BTC_PROFILE_PRESETS = {
    'conservative': {
        'max_spread': 900.0,
        'min_signal_strength': 62,
        'lot_size_multiplier': 0.25,
        'min_confluence_score': 6,
        'min_adx': 22,
        'pullback_atr_mult': 0.30,
        'momentum_lookback': 4,
        'volume_confirm_mult': 1.25,
        'stop_loss_atr_multiplier': 1.3,
        'take_profit_atr_multiplier': 3.0,
    },
    'balanced': {
        'max_spread': 1200.0,
        'min_signal_strength': 50,
        'lot_size_multiplier': 0.35,
        'min_confluence_score': 5,
        'min_adx': 18,
        'pullback_atr_mult': 0.35,
        'momentum_lookback': 3,
        'volume_confirm_mult': 1.10,
        'stop_loss_atr_multiplier': 1.5,
        'take_profit_atr_multiplier': 2.5,
    },
    'aggressive': {
        'max_spread': 1600.0,
        'min_signal_strength': 42,
        'lot_size_multiplier': 0.50,
        'min_confluence_score': 4,
        'min_adx': 15,
        'pullback_atr_mult': 0.45,
        'momentum_lookback': 2,
        'volume_confirm_mult': 1.00,
        'stop_loss_atr_multiplier': 1.7,
        'take_profit_atr_multiplier': 2.2,
    },
}

# Legacy single symbol config (for backward compatibility)
TRADING_CONFIG = {
    'symbol': 'BTCUSD',                 # Default symbol
    'timeframe': '5min',                # Chart timeframe (1min, 5min, 15min, 1h)
    'max_spread': 1200.0,               # Maximum spread in points
    'slippage': 5,                      # Expected slippage in points
    'min_signal_strength': 50,          # Minimum signal strength (0-100) to take trades
}

# BACKTESTING PARAMETERS
BACKTEST_CONFIG = {
    'days': 30,                         # Number of days to backtest
    'start_date': None,                 # Optional: specific start date (YYYY-MM-DD)
    'end_date': None,                   # Optional: specific end date (YYYY-MM-DD)
}

# BROKER API SETTINGS (for live trading)
# Leave empty to use existing MT5 session (recommended)
BROKER_CONFIG = {
    'broker': 'MetaTrader5',            # MetaTrader5 only
    'account_id': '',                   # Your account ID (leave empty to auto-detect)
    'password': '',                     # Account password (leave empty to use logged-in session)
    'server': '',                       # MT5 server name (leave empty to use logged-in session)
}

# LOGGING & MONITORING
LOGGING_CONFIG = {
    'enable_logging': True,
    'log_file': 'gold_scalper.log',
    'log_level': 'INFO',                # DEBUG, INFO, WARNING, ERROR
    'console_output': True,             # Print to console
}

# ADVANCED SETTINGS
ADVANCED_CONFIG = {
    'enable_trailing_stop': False,      # Use trailing stop loss (experimental)
    'trailing_stop_distance': 1.0,      # Trailing stop distance in ATR
    'max_daily_loss': 500,              # Stop trading if daily loss exceeds this ($)
    'max_daily_profit': 1000,           # Optional: Stop after reaching profit target ($)
    'max_daily_trades': 10,             # Maximum trades per day
    'check_interval': 10,               # Seconds between market checks
    'trading_hours': {                  # Trading session times (UTC) - Gold best during London/NY
        'start': '07:00',               # 7 AM UTC (London open)
        'end': '20:00',                 # 8 PM UTC (NY close)
    },
    'avoid_news': False,                # Avoid trading during major news (requires news API)
    'news_buffer': 30,                  # Minutes before/after news to avoid
}


# ============================================================
# QUICK SETUP GUIDE
# ============================================================
"""
1. INSTALL METATRADER 5:
   - Download from https://www.metatrader5.com/
   - Install and login to your broker account
   - Enable "Allow Algorithmic Trading" in MT5 settings

2. INSTALL PYTHON DEPENDENCIES:
   pip install MetaTrader5 pandas numpy

3. CONFIGURE SYMBOL:
   - Check your broker's exact symbol name for Gold
   - Common names: XAUUSD, XAUUSDm, Gold, GOLD.cash
   - Update TRADING_CONFIG['symbol'] if needed

4. START THE BOT:
   - Open MT5 and login to your account
   - Run: python run_bot.py
   
5. IMPORTANT WARNINGS:
   - Always start with a DEMO account
   - Test thoroughly before using real money
   - Never risk more than you can afford to lose
   - Past performance doesn't guarantee future results
"""