#!/usr/bin/env python3
"""
Headless Trading Bot
====================
Runs without web interface - purely automated trading.
Designed for 24/7 unattended operation.

Features:
- Auto-reconnect on connection loss
- Crash recovery with auto-restart
- File-based logging
- No browser required
- Daily profit goal tracking
- Auto-close or trailing TP on goal reached
"""

import sys
import os
import time
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

# Setup logging to file
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"trading_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    import pandas as pd
except ImportError as e:
    logger.error(f"Missing dependency: {e}")
    logger.error("Run: pip install MetaTrader5 pandas")
    sys.exit(1)

from gold_scalper import CRTScalpingStrategy, RiskManager
from config import CRT_TBS_CONFIG, RISK_CONFIG, SYMBOLS_CONFIG

# Import daily goal config with defaults
try:
    from config import DAILY_GOAL_CONFIG
except ImportError:
    DAILY_GOAL_CONFIG = {
        'enabled': True,
        'daily_target': 20.0,
        'action_on_goal': 'close_all',
        'trailing_tp_pips': 10,
        'reset_hour_utc': 0,
        'max_daily_loss': -50.0,
    }


class DailyProfitTracker:
    """Track daily profit/loss and manage goal actions"""
    
    def __init__(self, config):
        self.config = config
        self.daily_start_balance = None
        self.daily_start_time = None
        self.goal_reached = False
        self.loss_limit_reached = False
        self.closed_profits = 0.0  # Track closed trade profits today
        self._reset_daily_tracking()
    
    def _reset_daily_tracking(self):
        """Reset daily tracking at configured hour"""
        now = datetime.now(timezone.utc)
        reset_hour = self.config.get('reset_hour_utc', 0)
        self.daily_start_time = now.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        if now.hour < reset_hour:
            # It's before reset hour, so start time was yesterday
            from datetime import timedelta
            self.daily_start_time -= timedelta(days=1)
        self.goal_reached = False
        self.loss_limit_reached = False
        self.closed_profits = 0.0
    
    def check_reset(self):
        """Check if we need to reset daily tracking"""
        now = datetime.now(timezone.utc)
        reset_hour = self.config.get('reset_hour_utc', 0)
        
        # Check if we've passed the reset hour since last reset
        if self.daily_start_time:
            hours_since_start = (now - self.daily_start_time).total_seconds() / 3600
            if hours_since_start >= 24:
                logger.info("Daily reset - new trading day started")
                self._reset_daily_tracking()
                return True
        return False
    
    def set_start_balance(self, balance):
        """Set the starting balance for today"""
        if self.daily_start_balance is None:
            self.daily_start_balance = balance
            logger.info(f"Daily start balance set: ${balance:.2f}")
    
    def get_daily_pnl(self, current_balance, open_profit=0):
        """Calculate today's P&L"""
        if self.daily_start_balance is None:
            return 0
        return (current_balance - self.daily_start_balance) + open_profit + self.closed_profits
    
    def add_closed_profit(self, profit):
        """Add closed trade profit to daily total"""
        self.closed_profits += profit
    
    def check_daily_goal(self, current_balance, open_profit=0):
        """Check if daily goal is reached"""
        if not self.config.get('enabled', True):
            return False, None
        
        daily_pnl = self.get_daily_pnl(current_balance, open_profit)
        daily_target = self.config.get('daily_target', 20.0)
        max_loss = self.config.get('max_daily_loss', -50.0)
        
        # Check loss limit
        if daily_pnl <= max_loss and not self.loss_limit_reached:
            self.loss_limit_reached = True
            logger.warning(f"DAILY LOSS LIMIT REACHED: ${daily_pnl:.2f} (limit: ${max_loss:.2f})")
            return True, 'loss_limit'
        
        # Check profit goal
        if daily_pnl >= daily_target and not self.goal_reached:
            self.goal_reached = True
            logger.info(f"DAILY PROFIT GOAL REACHED: ${daily_pnl:.2f} (target: ${daily_target:.2f})")
            return True, 'goal_reached'
        
        return False, None


class HeadlessTradingBot:
    """Headless trading bot for 24/7 operation"""
    
    def __init__(self):
        self.strategy = CRTScalpingStrategy(**CRT_TBS_CONFIG)
        self.running = True
        self.last_signal_times = {}
        self.daily_tracker = DailyProfitTracker(DAILY_GOAL_CONFIG)
        self.symbol_alias_cache = {}
        self.symbol_alias_logged = set()
        self.symbol_alias_failed = set()
        self.stats = {
            'signals': 0,
            'trades': 0,
            'errors': 0,
            'reconnects': 0,
            'daily_pnl': 0,
        }

    @staticmethod
    def _normalize_symbol_name(name):
        return re.sub(r'[^A-Z0-9]', '', str(name).upper())

    def _resolve_symbol(self, symbol_name):
        """Resolve configured symbol to an actual broker symbol name."""
        raw = str(symbol_name or '').strip()
        if not raw:
            return raw

        cache_key = raw.upper()
        cached = self.symbol_alias_cache.get(cache_key)
        if cached and mt5.symbol_info(cached):
            return cached

        exact = mt5.symbol_info(raw)
        if exact:
            if not exact.visible:
                mt5.symbol_select(raw, True)
            self.symbol_alias_cache[cache_key] = raw
            return raw

        symbols = mt5.symbols_get()
        if not symbols:
            return raw

        raw_upper = raw.upper()
        raw_norm = self._normalize_symbol_name(raw)

        alias_tokens = [raw_upper]
        if raw_upper == 'GOLD':
            alias_tokens.extend(['XAUUSD', 'XAU'])
        elif raw_upper in ('XAUUSD', 'XAU'):
            alias_tokens.append('GOLD')
        elif raw_upper == 'SILVER':
            alias_tokens.extend(['XAGUSD', 'XAG'])
        elif raw_upper in ('XAGUSD', 'XAG'):
            alias_tokens.append('SILVER')
        elif raw_upper in ('BTC', 'BITCOIN'):
            alias_tokens.extend(['BTCUSD', 'XBTUSD', 'BTCUSDT'])
        elif raw_upper in ('BTCUSD', 'XBTUSD', 'BTCUSDT'):
            alias_tokens.extend(['BTC', 'BITCOIN', 'BTCUSD', 'XBTUSD', 'BTCUSDT'])

        alias_tokens = list(dict.fromkeys(alias_tokens))
        alias_norm_tokens = [tok for tok in {self._normalize_symbol_name(t) for t in alias_tokens} if tok]

        best_name = None
        best_score = -10**9

        for sym in symbols:
            name = sym.name
            name_upper = name.upper()
            name_norm = self._normalize_symbol_name(name)

            score = -1000
            if name_upper in alias_tokens:
                score = 300
            elif name_norm and name_norm in alias_norm_tokens:
                score = 260
            else:
                for token in alias_tokens:
                    if name_upper.startswith(token):
                        score = max(score, 220)
                    if token in name_upper:
                        score = max(score, 180)
                for token in alias_norm_tokens:
                    if token and name_norm.startswith(token):
                        score = max(score, 200)
                    if token and token in name_norm:
                        score = max(score, 170)

            if score < 0:
                continue

            if getattr(sym, 'visible', False):
                score += 12
            if getattr(sym, 'select', False):
                score += 6

            extra = max(0, len(name_norm) - len(raw_norm))
            score -= min(extra, 20)

            if score > best_score:
                best_score = score
                best_name = name

        if best_name and best_score >= 170:
            mt5.symbol_select(best_name, True)
            self.symbol_alias_cache[cache_key] = best_name
            if best_name != raw and raw not in self.symbol_alias_logged:
                logger.info(f"Symbol mapped: {raw} -> {best_name}")
                self.symbol_alias_logged.add(raw)
            return best_name

        if raw not in self.symbol_alias_failed:
            logger.warning(f"No broker symbol match for {raw}. Check names with find_symbol.py")
            self.symbol_alias_failed.add(raw)

        return raw
        
    def connect_mt5(self, max_retries=5):
        """Connect to MT5 with retry logic"""
        for attempt in range(max_retries):
            if mt5.initialize():
                account = mt5.account_info()
                if account:
                    logger.info(f"Connected to MT5 - Account: {account.login}, Balance: ${account.balance:.2f}")
                    return True
            
            logger.warning(f"MT5 connection attempt {attempt + 1}/{max_retries} failed")
            time.sleep(10)
        
        return False
    
    def get_market_data(self, symbol):
        """Get market data for a symbol"""
        symbol = self._resolve_symbol(symbol)
        if not symbol:
            return None, None

        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            return None, None

        if not sym_info.visible:
            mt5.symbol_select(symbol, True)

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            mt5.symbol_select(symbol, True)
            tick = mt5.symbol_info_tick(symbol)

        if tick is not None:
            bid = float(tick.bid)
            ask = float(tick.ask)
        else:
            bid = float(getattr(sym_info, 'bid', 0.0) or 0.0)
            ask = float(getattr(sym_info, 'ask', 0.0) or 0.0)

        if bid <= 0 and ask <= 0:
            return None, None
        if bid <= 0:
            bid = ask
        if ask <= 0:
            ask = bid

        spread = max(0.0, ask - bid)
        price_info = {
            'symbol': symbol,
            'bid': bid,
            'ask': ask,
            'spread_points': round(spread / sym_info.point) if sym_info and sym_info.point else 0,
        }
        
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 200)
        if rates is None:
            return price_info, None
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df.rename(columns={'tick_volume': 'volume'})
        
        return price_info, df
    
    def get_positions(self, symbol):
        """Get open positions for a symbol"""
        symbol = self._resolve_symbol(symbol)
        if not symbol:
            return []
        positions = mt5.positions_get(symbol=symbol)
        return list(positions) if positions else []
    
    def open_trade(self, symbol, order_type, volume, sl, tp):
        """Open a trade"""
        symbol = self._resolve_symbol(symbol)
        if not symbol:
            return None

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return None
        
        if not symbol_info.visible:
            mt5.symbol_select(symbol, True)
        
        volume = max(symbol_info.volume_min, min(volume, symbol_info.volume_max))
        volume = round(volume / symbol_info.volume_step) * symbol_info.volume_step
        volume = round(volume, 2)
        
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None:
            price = float(tick.ask if order_type == 'buy' else tick.bid)
        else:
            price = float(symbol_info.ask if order_type == 'buy' else symbol_info.bid)

        if price <= 0:
            logger.warning(f"Order blocked: no valid price for {symbol}")
            return None

        digits = symbol_info.digits
        sl = round(sl, digits) if sl else None
        tp = round(tp, digits) if tp else None
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if order_type == 'buy' else mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 20,
            "magic": 234000,
            "comment": f"HeadlessBot {symbol}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        if sl:
            request["sl"] = sl
        if tp:
            request["tp"] = tp
        
        result = mt5.order_send(request)
        
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {'ticket': result.order, 'price': result.price, 'volume': result.volume}
        else:
            logger.error(f"Order failed: {result.comment if result else 'Unknown error'}")
            return None
    
    def close_all_positions(self):
        """Close all open positions"""
        positions = mt5.positions_get()
        if not positions:
            return 0
        
        closed_count = 0
        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue
            
            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket,
                "price": price,
                "deviation": 20,
                "magic": 234000,
                "comment": "Daily goal reached",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                closed_count += 1
                logger.info(f"Closed position {pos.ticket} ({pos.symbol}) - Daily goal reached")
            else:
                logger.error(f"Failed to close {pos.ticket}: {result.comment if result else 'Unknown'}")
        
        return closed_count
    
    def apply_trailing_tp(self, trailing_pips):
        """Apply trailing TP to lock in profits"""
        positions = mt5.positions_get()
        if not positions:
            return 0
        
        modified_count = 0
        for pos in positions:
            if pos.profit <= 0:
                continue
            
            symbol_info = mt5.symbol_info(pos.symbol)
            if not symbol_info:
                continue
            
            tick = mt5.symbol_info_tick(pos.symbol)
            if not tick:
                continue
            
            point = symbol_info.point
            trailing_distance = trailing_pips * point
            
            if pos.type == mt5.ORDER_TYPE_BUY:
                new_tp = tick.bid - trailing_distance
                if pos.tp == 0 or new_tp < pos.tp:
                    request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "symbol": pos.symbol,
                        "position": pos.ticket,
                        "sl": pos.sl,
                        "tp": round(new_tp, symbol_info.digits),
                    }
                    result = mt5.order_send(request)
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        modified_count += 1
                        logger.info(f"Trailing TP applied to {pos.ticket} - new TP: {new_tp:.5f}")
            else:
                new_tp = tick.ask + trailing_distance
                if pos.tp == 0 or new_tp > pos.tp:
                    request = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "symbol": pos.symbol,
                        "position": pos.ticket,
                        "sl": pos.sl,
                        "tp": round(new_tp, symbol_info.digits),
                    }
                    result = mt5.order_send(request)
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        modified_count += 1
                        logger.info(f"Trailing TP applied to {pos.ticket} - new TP: {new_tp:.5f}")
        
        return modified_count
    
    def process_symbol(self, sym_key, sym_config, account_balance):
        """Process a single symbol for trading signals"""
        symbol = sym_config.get('symbol', sym_key)
        
        price_info, df = self.get_market_data(symbol)
        if price_info is None or df is None:
            return

        symbol = price_info.get('symbol', symbol)
        
        if len(df) < 50:
            return
        
        # Check spread
        max_spread = sym_config.get('max_spread', 50)
        if price_info['spread_points'] > max_spread:
            return
        
        # Check positions
        positions = self.get_positions(symbol)
        max_per_symbol = RISK_CONFIG.get('max_positions_per_symbol', 1)
        if len(positions) >= max_per_symbol:
            return
        
        # Generate signals
        df = self.strategy.generate_signals(df)
        signal_bar = df.iloc[-2]
        
        # Check if we already processed this signal
        signal_time = signal_bar['time']
        if self.last_signal_times.get(sym_key) == signal_time:
            return
        
        # Check signal strength
        signal_strength = self.strategy.get_signal_strength(df.iloc[:-1])
        min_strength = sym_config.get('min_signal_strength', 50)
        if signal_strength < min_strength:
            return
        
        # Process signal
        if signal_bar['signal'] != 0:
            atr = signal_bar.get('ATR', 0.001)
            if pd.isna(atr) or atr <= 0:
                atr = 0.001 if 'USD' in symbol else 2.0
            
            risk_manager = RiskManager(**RISK_CONFIG)
            risk_manager.account_balance = account_balance
            
            lot_mult = sym_config.get('lot_size_multiplier', 1.0)
            position_size = risk_manager.calculate_position_size(atr, price_info['bid']) * lot_mult
            
            if signal_bar['signal'] == 1:
                entry = price_info['ask']
                sl = risk_manager.calculate_stop_loss(entry, atr, 1)
                tp = risk_manager.calculate_take_profit(entry, atr, 1)
                
                logger.info(f"{sym_key}: BUY signal detected (Score: {signal_bar.get('buy_score', 0)})")
                result = self.open_trade(symbol, 'buy', position_size, sl, tp)
                
                if result:
                    self.last_signal_times[sym_key] = signal_time
                    self.stats['trades'] += 1
                    logger.info(f"{sym_key}: BUY executed - Ticket #{result['ticket']}, Price: {result['price']}, Lots: {result['volume']}")
            
            elif signal_bar['signal'] == -1:
                entry = price_info['bid']
                sl = risk_manager.calculate_stop_loss(entry, atr, -1)
                tp = risk_manager.calculate_take_profit(entry, atr, -1)
                
                logger.info(f"{sym_key}: SELL signal detected (Score: {signal_bar.get('sell_score', 0)})")
                result = self.open_trade(symbol, 'sell', position_size, sl, tp)
                
                if result:
                    self.last_signal_times[sym_key] = signal_time
                    self.stats['trades'] += 1
                    logger.info(f"{sym_key}: SELL executed - Ticket #{result['ticket']}, Price: {result['price']}, Lots: {result['volume']}")
            
            self.stats['signals'] += 1
    
    def run(self):
        """Main trading loop"""
        logger.info("=" * 60)
        logger.info("HEADLESS TRADING BOT STARTED")
        logger.info("Strategy: CRT + TBS (M5)")
        logger.info("Symbols: " + ", ".join(SYMBOLS_CONFIG.keys()))
        logger.info("=" * 60)
        
        if not self.connect_mt5():
            logger.error("Failed to connect to MT5. Exiting.")
            return
        
        # Enable symbols (with broker alias resolution)
        for sym_config in SYMBOLS_CONFIG.values():
            symbol = sym_config.get('symbol')
            if symbol:
                resolved = self._resolve_symbol(symbol)
                if resolved:
                    mt5.symbol_select(resolved, True)
        
        last_status_time = 0
        daily_tracker = self.daily_tracker
        daily_goal_reached = False
        
        try:
            while self.running:
                try:
                    # Check connection
                    if not mt5.terminal_info():
                        logger.warning("MT5 connection lost. Reconnecting...")
                        self.stats['reconnects'] += 1
                        if not self.connect_mt5():
                            time.sleep(60)
                            continue
                    
                    account = mt5.account_info()
                    if not account:
                        time.sleep(10)
                        continue
                    
                    # Daily goal tracking
                    if DAILY_GOAL_CONFIG.get('enabled', False):
                        # Check if new day started
                        if daily_tracker.check_reset():
                            daily_goal_reached = False
                            logger.info("New trading day - Daily tracker reset")
                        
                        # Set start balance if not set
                        if daily_tracker.daily_start_balance is None:
                            daily_tracker.set_start_balance(account.balance)
                        
                        # Get daily P&L (unrealized + realized)
                        daily_pnl = daily_tracker.get_daily_pnl(account.equity)
                        goal_hit, goal_status = daily_tracker.check_daily_goal(account.equity)
                        
                        if goal_hit and goal_status == 'goal_reached' and not daily_goal_reached:
                            daily_goal_reached = True
                            action = DAILY_GOAL_CONFIG.get('action_on_goal', 'stop_trading')
                            logger.info(f"DAILY GOAL REACHED! P&L: ${daily_pnl:.2f}")
                            
                            if action == 'close_all':
                                logger.info("Closing all positions...")
                                closed = self.close_all_positions()
                                logger.info(f"Closed {closed} positions. Done for the day!")
                            elif action == 'trailing_tp':
                                trailing_pips = DAILY_GOAL_CONFIG.get('trailing_tp_pips', 10)
                                logger.info(f"Applying trailing TP ({trailing_pips} pips)...")
                                modified = self.apply_trailing_tp(trailing_pips)
                                logger.info(f"Modified {modified} positions with trailing TP")
                                daily_goal_reached = False  # Allow continued trading with trailing
                            elif action == 'stop_trading':
                                logger.info("Stopping new trades for the day...")
                        
                        elif goal_hit and goal_status == 'loss_limit':
                            logger.warning(f"DAILY LOSS LIMIT HIT! P&L: ${daily_pnl:.2f}")
                            logger.info("Closing all positions and stopping...")
                            self.close_all_positions()
                            daily_goal_reached = True
                        
                        # Skip trading if goal reached and action is close_all or stop_trading
                        if daily_goal_reached and DAILY_GOAL_CONFIG.get('action_on_goal') != 'trailing_tp':
                            time.sleep(60)  # Check less frequently when done
                            continue
                    
                    # Count total positions
                    total_positions = sum(
                        len(self.get_positions(cfg.get('symbol', key))) 
                        for key, cfg in SYMBOLS_CONFIG.items() 
                        if cfg.get('enabled', True)
                    )
                    
                    # Get dynamic max positions based on account balance
                    temp_rm = RiskManager(**RISK_CONFIG)
                    temp_rm.account_balance = float(account.balance)
                    dynamic_max_pos = temp_rm.get_max_positions()
                    
                    if total_positions >= dynamic_max_pos:
                        time.sleep(10)
                        continue
                    
                    # Process each symbol
                    for sym_key, sym_config in SYMBOLS_CONFIG.items():
                        if not sym_config.get('enabled', True):
                            continue
                        
                        try:
                            self.process_symbol(sym_key, sym_config, account.balance)
                        except Exception as e:
                            logger.error(f"{sym_key} error: {e}")
                            self.stats['errors'] += 1
                    
                    # Log status every 5 minutes
                    current_time = time.time()
                    if current_time - last_status_time > 300:
                        daily_info = ""
                        if DAILY_GOAL_CONFIG.get('enabled', False):
                            daily_pnl = daily_tracker.get_daily_pnl(account.equity)
                            target = DAILY_GOAL_CONFIG.get('daily_target', 20)
                            daily_info = f", Daily P&L=${daily_pnl:.2f}/{target:.2f}"
                        
                        logger.info(f"Status: Balance=${account.balance:.2f}, Equity=${account.equity:.2f}, "
                                   f"Profit=${account.profit:.2f}, Positions={total_positions}, "
                                   f"Trades={self.stats['trades']}{daily_info}")
                        last_status_time = current_time
                    
                    time.sleep(10)
                    
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(f"Loop error: {e}")
                    self.stats['errors'] += 1
                    time.sleep(30)
        
        except KeyboardInterrupt:
            logger.info("Shutdown requested by user")
        finally:
            logger.info(f"Bot stopped. Stats: {self.stats}")
            mt5.shutdown()


def main():
    """Main entry point with crash recovery"""
    max_restarts = 10
    restart_count = 0
    
    while restart_count < max_restarts:
        try:
            bot = HeadlessTradingBot()
            bot.run()
            break  # Clean exit
        except Exception as e:
            restart_count += 1
            logger.error(f"Bot crashed: {e}")
            logger.info(f"Restarting... (attempt {restart_count}/{max_restarts})")
            time.sleep(60)
    
    if restart_count >= max_restarts:
        logger.error("Max restarts reached. Exiting.")


if __name__ == "__main__":
    main()
