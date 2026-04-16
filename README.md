# Multi-Asset 5-Minute Scalping Trading Robot 🤖📈

A professional Python-based algorithmic trading bot for scalping Gold and BTC on MetaTrader 5.
Uses a multi-indicator 5-minute scalping strategy with automated risk management.

## ⚠️ DISCLAIMER

**TRADING INVOLVES SUBSTANTIAL RISK OF LOSS.** This software is provided for educational purposes only. 
- Past performance does not guarantee future results
- Always test on a demo account first
- Never risk more than you can afford to lose
- The authors are not responsible for any financial losses

---

## 🎯 Features

- **5-Minute Scalping Strategy**: Optimized for Gold/XAUUSD and BTCUSD
- **Multi-Indicator Analysis**: EMA crossovers, RSI, MACD, Bollinger Bands
- **MetaTrader 5 Integration**: Real-time live trading support
- **Automated Risk Management**: ATR-based position sizing and stop-losses
- **Signal Strength Filtering**: Only takes high-quality trade setups
- **Backtesting Engine**: Test strategies before going live
- **Session Statistics**: Track performance in real-time

---

## 🚀 Quick Start

### 1. Prerequisites

- **MetaTrader 5** installed and running
- **Python 3.8+** installed
- A trading account (start with DEMO!)

### 2. Installation

```bash
# Install dependencies
pip install MetaTrader5 pandas numpy

# Or install all dependencies
pip install -r requirements.txt
```

### 3. Configuration

1. Open MetaTrader 5 and login to your account
2. Ensure "Allow Algorithmic Trading" is enabled in MT5 Tools → Options → Expert Advisors
3. Check your broker's Gold symbol name (usually XAUUSD) in `config.py`

### 4. Run the Bot

```bash
# Check your MT5 connection first
python run_bot.py --check

# Show account info
python run_bot.py --info

# Run backtest
python run_bot.py --backtest

# Start live trading
python run_bot.py

# Quick BTC walk-forward validation
python btc_walkforward.py --profile balanced --train-days 21 --test-days 7 --folds 4
```

---

## 📋 Strategy Overview

### Entry Signals (5-Minute Timeframe)

**BUY (Long)**:
- Fast EMA (9) crosses above Slow EMA (21), OR
- Uptrend with price touching lower Bollinger Band
- RSI between 30-60 (not overbought)
- MACD histogram positive or rising

**SELL (Short)**:
- Fast EMA (9) crosses below Slow EMA (21), OR
- Downtrend with price touching upper Bollinger Band
- RSI between 40-70 (not oversold)
- MACD histogram negative or falling

### Risk Management

- **Stop Loss**: 1.5x ATR from entry
- **Take Profit**: 2.5x ATR from entry (1:1.67 Risk/Reward)
- **Position Size**: Calculated to risk only 1% of account per trade
- **Max Spread Filter**: Avoids trading during high-spread conditions

---

## 📁 Project Files

| File | Description |
|------|-------------|
| `run_bot.py` | Main entry point - run this to start the bot |
| `gold_scalper.py` | Strategy, indicators, and backtesting engine |
| `live_trading.py` | MetaTrader 5 adapter and live trading logic |
| `config.py` | All configurable settings |
| `visualization.py` | Charts and performance visualization |

---

## ⚙️ Configuration

Edit `config.py` to customize the bot:

### Option 2: Download from Broker

```python
from live_trading import MT5Adapter

adapter = MT5Adapter(account=123456, password='pass', server='server')
adapter.connect()
df = adapter.get_historical_data(timeframe='5min', bars=10000)
```

### Option 3: Free Data Sources

- **MetaTrader 5**: Built-in historical data
- **Yahoo Finance**: Use `yfinance` library
- **Alpha Vantage**: Free API for gold data
- **Investing.com**: Export historical data

---

## 🔴 LIVE TRADING (MetaTrader 5)

### Prerequisites

1. **Install MetaTrader 5**: Download from MetaQuotes
2. **Open Demo Account**: Practice before going live
3. **Enable Algo Trading**: In MT5 settings, enable automated trading

### Setup

1. **Edit config.py**:

```python
BROKER_CONFIG = {
    'account_id': 12345678,      # Your MT5 account number
    'password': 'your_password',
    'server': 'YourBroker-Demo', # Your broker's server
    'demo': True,                # ALWAYS start with demo
}
```

2. **Run Live Bot**:

```bash
python live_trading.py
```

3. **Monitor**: The bot will print all activity to console

### Live Trading Checklist

- [ ] Tested on demo account for at least 1 month
- [ ] Verified strategy profitability
- [ ] Checked broker spreads and commissions
- [ ] Set up proper risk limits
- [ ] Monitored during live hours
- [ ] Have a kill switch ready

---

## 📈 Understanding Results

### Key Metrics

- **Win Rate**: Percentage of winning trades (aim for >40%)
- **Profit Factor**: Gross profit / Gross loss (aim for >1.5)
- **Sharpe Ratio**: Risk-adjusted returns (aim for >1.0)
- **Max Drawdown**: Largest peak-to-trough decline (keep <20%)
- **Average Win/Loss**: Risk-reward ratio (aim for >1.5)

### Example Output

```
BACKTEST RESULTS
================
Total Trades:        45
Winning Trades:      28
Losing Trades:       17
Win Rate:            62.22%
Total P&L:           $1,234.56
Profit Factor:       2.15
Max Drawdown:        -8.45%
Final Balance:       $11,234.56
```

---

## ⚙️ Optimization Tips

### 1. Parameter Tuning

Test different combinations:
- EMA periods: (5, 13), (9, 21), (12, 26)
- RSI thresholds: 20/80, 30/70, 40/60
- ATR multipliers: 1.0-3.0 for SL/TP

### 2. Timeframe Selection

- **1-minute**: Very fast, high frequency, needs excellent execution
- **5-minute**: Good balance for scalping
- **15-minute**: Slower scalping, fewer trades
- **Test all**: Results vary significantly by timeframe

### 3. Market Conditions

Gold scalping works best during:
- **High volatility periods**: Major news, market open
- **Trending markets**: Clear directional moves
- **Avoid**: Low volatility, sideways markets, major news events

### 4. Spread and Commissions

- Keep spreads <2-3 pips for profitable scalping
- Account for commissions in backtests
- Choose brokers with tight spreads

---

## 🛠️ Advanced Features

### Add Trailing Stop

Modify `live_trading.py`:

```python
# In _trading_loop(), after position opens:
if enable_trailing_stop:
    new_sl = calculate_trailing_stop(position, current_price, atr)
    adapter.modify_position(position['ticket'], sl=new_sl)
```

### Multiple Timeframe Analysis

```python
# Get data from multiple timeframes
df_5min = adapter.get_historical_data('5min', 500)
df_15min = adapter.get_historical_data('15min', 200)

# Check trend on higher timeframe
trend = 'up' if df_15min['EMA_fast'].iloc[-1] > df_15min['EMA_slow'].iloc[-1] else 'down'

# Only take trades in direction of higher timeframe trend
if trend == 'up' and signal == 1:
    # Take long trade
```

### News Filter

```python
import requests

def is_major_news_time():
    # Check economic calendar API
    # Return True if major news in next 30 minutes
    pass

# In trading loop:
if is_major_news_time():
    print("Major news ahead, skipping trade")
    return
```

---

## 🐛 Troubleshooting

### "MetaTrader5 not installed"
```bash
pip install MetaTrader5
```

### "MT5 initialization failed"
- Ensure MT5 is installed and running
- Check account credentials
- Verify server name is correct

### "Failed to get rates"
- Check symbol name (might be 'XAUUSD', 'GOLD', etc.)
- Ensure symbol is available in Market Watch
- Check internet connection

### Poor Backtest Results
- Try different parameter combinations
- Test on more data (3-6 months minimum)
- Consider market regime (trending vs ranging)
- Account for spreads and commissions

### High Drawdown
- Reduce risk per trade (0.5% instead of 1%)
- Widen stop loss
- Add filters to reduce trade frequency
- Trade only during favorable conditions

---

## 📚 Further Learning

### Recommended Resources

1. **Books**:
   - "Building Algorithmic Trading Systems" by Kevin Davey
   - "Quantitative Trading" by Ernest Chan

2. **Courses**:
   - Quantitative Trading on Coursera
   - AlgoTrading101

3. **Communities**:
   - Reddit: r/algotrading
   - Elite Trader forums
   - MQL5 community

### Strategy Improvements

- Add machine learning predictions
- Implement order flow analysis
- Use multiple timeframe confirmation
- Add volatility filters
- Implement session-based rules (London/NY open)

---

## 📝 Code Structure

```
.
├── gold_scalper.py      # Main bot with backtesting
├── live_trading.py      # Live trading adapter
├── config.py            # Configuration parameters
├── requirements.txt     # Python dependencies
└── README.md           # This file
```

### Key Classes

- `TechnicalIndicators`: Calculate EMA, RSI, Bollinger Bands, ATR, MACD
- `ScalpingStrategy`: Trading logic and signal generation
- `RiskManager`: Position sizing and risk calculations
- `Backtester`: Historical performance testing
- `MT5Adapter`: MetaTrader 5 integration
- `LiveTradingBot`: Real-time trading execution

---

## 🤝 Contributing

Feel free to:
- Add new indicators
- Improve the strategy
- Fix bugs
- Add new broker integrations

---

## 📄 License

This project is provided as-is for educational purposes.

---

## 🆘 Support

For issues or questions:
1. Check the troubleshooting section
2. Review MT5 documentation
3. Test with demo account first
4. Start with small position sizes

---

## ⚡ Quick Reference

### Run Backtest
```bash
python gold_scalper.py
```

### Run Live (Demo)
```bash
python live_trading.py
```

### Common Parameters

| Parameter | Description | Typical Range |
|-----------|-------------|---------------|
| EMA Fast | Fast moving average | 5-12 |
| EMA Slow | Slow moving average | 20-30 |
| RSI Period | RSI calculation | 14 |
| Risk per Trade | % of account | 0.5-2% |
| Stop Loss (ATR) | SL distance | 1.0-2.0 |
| Take Profit (ATR) | TP distance | 2.0-4.0 |

---

## 🎓 Remember

1. **Always test first** - Demo account for at least 1 month
2. **Start small** - Begin with minimum position sizes
3. **Monitor closely** - Don't leave bot unattended initially
4. **Keep learning** - Markets change, strategies must adapt
5. **Risk management** - Never risk more than 1-2% per trade
6. **Be patient** - Profitable trading takes time to master

---

**Good luck and trade safely! 🚀**