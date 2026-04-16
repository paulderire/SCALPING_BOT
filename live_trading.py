"""
Live Trading Adapter for MetaTrader 5
This module handles real-time data and order execution for configured symbols
Optimized for 5-minute scalping strategy
"""

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("MetaTrader5 not installed. Run: pip install MetaTrader5")

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import logging
import threading
from typing import Optional, Dict, List, Callable


def _normalize_symbol_name(name: str) -> str:
    """Normalize symbol names so suffix/prefix variants still match."""
    return ''.join(ch for ch in str(name).upper() if ch.isalnum())


def get_symbol_profile(symbol_name: str, default_max_spread: float = 50.0, default_min_strength: int = 50) -> Dict:
    """Resolve per-symbol runtime profile from SYMBOLS_CONFIG by key or normalized symbol."""
    try:
        from config import SYMBOLS_CONFIG
    except Exception:
        return {
            'max_spread': float(default_max_spread),
            'min_signal_strength': int(default_min_strength),
            'lot_size_multiplier': 1.0,
        }

    requested = str(symbol_name or '').upper().strip()
    requested_norm = _normalize_symbol_name(requested)

    for key, cfg in SYMBOLS_CONFIG.items():
        key_u = str(key).upper().strip()
        sym_u = str(cfg.get('symbol', key)).upper().strip()
        if requested in (key_u, sym_u):
            return {
                'max_spread': float(cfg.get('max_spread', default_max_spread)),
                'min_signal_strength': int(cfg.get('min_signal_strength', default_min_strength)),
                'lot_size_multiplier': float(cfg.get('lot_size_multiplier', 1.0)),
            }

        if requested_norm and requested_norm in (_normalize_symbol_name(key_u), _normalize_symbol_name(sym_u)):
            return {
                'max_spread': float(cfg.get('max_spread', default_max_spread)),
                'min_signal_strength': int(cfg.get('min_signal_strength', default_min_strength)),
                'lot_size_multiplier': float(cfg.get('lot_size_multiplier', 1.0)),
            }

    return {
        'max_spread': float(default_max_spread),
        'min_signal_strength': int(default_min_strength),
        'lot_size_multiplier': 1.0,
    }

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class MT5Adapter:
    """
    Enhanced Adapter for MetaTrader 5 live trading
    Optimized for Gold (XAU/USD) 5-minute scalping
    """
    
    def __init__(self, account: int = None, password: str = None, server: str = None, 
                 symbol: str = 'XAUUSD', magic_number: int = 234000):
        if not MT5_AVAILABLE:
            raise ImportError("MetaTrader5 module not available. Install with: pip install MetaTrader5")
        
        self.account = account
        self.password = password
        self.server = server
        self.symbol = symbol
        self.magic_number = magic_number
        self.connected = False
        self.symbol_info = None
        
    def connect(self) -> bool:
        """Connect to MT5 terminal"""
        # Initialize MT5
        if not mt5.initialize():
            logger.error(f"MT5 initialization failed: {mt5.last_error()}")
            return False
        
        # Login if credentials provided
        if self.account and self.password and self.server:
            if not mt5.login(self.account, password=self.password, server=self.server):
                logger.error(f"Login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False
            logger.info(f"Logged in to MT5 - Account: {self.account}")
        else:
            # Use existing terminal session
            account_info = mt5.account_info()
            if account_info:
                self.account = account_info.login
                logger.info(f"Connected to existing MT5 session - Account: {self.account}")
            else:
                logger.warning("Connected to MT5 but no account logged in")
        
        # Enable symbol
        if not self._enable_symbol():
            logger.error(f"Failed to enable symbol {self.symbol}")
            mt5.shutdown()
            return False
        
        self.connected = True
        logger.info(f"MT5 connected successfully - Symbol: {self.symbol}")
        return True
    
    def _enable_symbol(self) -> bool:
        """Ensure the symbol is visible and get its info"""
        resolved = self._resolve_symbol(self.symbol)
        if resolved:
            self.symbol = resolved

        self.symbol_info = mt5.symbol_info(self.symbol)
        
        if self.symbol_info is None:
            logger.error(f"Symbol {self.symbol} not found")
            return False
        
        if not self.symbol_info.visible:
            if not mt5.symbol_select(self.symbol, True):
                logger.error(f"Failed to select {self.symbol}")
                return False
        
        return True

    def _resolve_symbol(self, symbol_name: str) -> str:
        """Resolve configured symbol to broker symbol aliases (e.g. BTCUSD -> XBTUSDm)."""
        raw = str(symbol_name or '').strip()
        if not raw:
            return raw

        exact = mt5.symbol_info(raw)
        if exact:
            if not exact.visible:
                mt5.symbol_select(raw, True)
            return raw

        symbols = mt5.symbols_get()
        if not symbols:
            return raw

        raw_upper = raw.upper()
        raw_norm = _normalize_symbol_name(raw_upper)
        alias_tokens = [raw_upper]
        if raw_upper == 'GOLD':
            alias_tokens.extend(['XAUUSD', 'XAU'])
        elif raw_upper in ('XAUUSD', 'XAU'):
            alias_tokens.append('GOLD')
        elif raw_upper in ('BTC', 'BITCOIN'):
            alias_tokens.extend(['BTCUSD', 'XBTUSD', 'BTCUSDT'])
        elif raw_upper in ('BTCUSD', 'XBTUSD', 'BTCUSDT'):
            alias_tokens.extend(['BTC', 'BITCOIN', 'BTCUSD', 'XBTUSD', 'BTCUSDT'])

        alias_tokens = list(dict.fromkeys(alias_tokens))
        alias_norm_tokens = [tok for tok in {_normalize_symbol_name(t) for t in alias_tokens} if tok]

        best_name = raw
        best_score = -10**9
        for sym in symbols:
            name = sym.name
            name_upper = name.upper()
            name_norm = _normalize_symbol_name(name)
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

        if best_score >= 170:
            mt5.symbol_select(best_name, True)
            if best_name != raw:
                logger.info(f"Symbol mapped: {raw} -> {best_name}")
            return best_name

        return raw
    
    def disconnect(self):
        """Disconnect from MT5"""
        if self.connected:
            mt5.shutdown()
            self.connected = False
            logger.info("Disconnected from MT5")
    
    def get_account_info(self) -> Optional[Dict]:
        """Get account information"""
        if not self.connected:
            return None
        
        account_info = mt5.account_info()
        if account_info is None:
            return None
        
        return {
            'login': account_info.login,
            'server': account_info.server,
            'balance': account_info.balance,
            'equity': account_info.equity,
            'margin': account_info.margin,
            'free_margin': account_info.margin_free,
            'profit': account_info.profit,
            'leverage': account_info.leverage,
            'currency': account_info.currency,
            'trade_allowed': account_info.trade_allowed,
        }
    
    def get_symbol_info(self) -> Optional[Dict]:
        """Get detailed symbol information"""
        if not self.connected:
            return None
        
        info = mt5.symbol_info(self.symbol)
        if info is None:
            return None
        
        return {
            'symbol': info.name,
            'bid': info.bid,
            'ask': info.ask,
            'spread': info.spread,
            'digits': info.digits,
            'point': info.point,
            'trade_contract_size': info.trade_contract_size,
            'volume_min': info.volume_min,
            'volume_max': info.volume_max,
            'volume_step': info.volume_step,
        }
    
    def get_historical_data(self, timeframe: str = '5min', bars: int = 500) -> Optional[pd.DataFrame]:
        """
        Get historical OHLC data
        
        Args:
            timeframe: '1min', '5min', '15min', '1h', etc.
            bars: Number of bars to retrieve
        """
        if not self.connected:
            logger.warning("Not connected to MT5")
            return None
        
        # Map timeframe to MT5 constant
        timeframe_map = {
            '1min': mt5.TIMEFRAME_M1,
            '5min': mt5.TIMEFRAME_M5,
            '15min': mt5.TIMEFRAME_M15,
            '30min': mt5.TIMEFRAME_M30,
            '1h': mt5.TIMEFRAME_H1,
            '4h': mt5.TIMEFRAME_H4,
            '1d': mt5.TIMEFRAME_D1,
        }
        
        mt5_timeframe = timeframe_map.get(timeframe, mt5.TIMEFRAME_M5)
        
        rates = mt5.copy_rates_from_pos(self.symbol, mt5_timeframe, 0, bars)
        
        if rates is None or len(rates) == 0:
            logger.error(f"Failed to get rates: {mt5.last_error()}")
            return None
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        
        return df[['time', 'open', 'high', 'low', 'close', 'tick_volume', 'spread']].rename(
            columns={'tick_volume': 'volume'}
        )
    
    def get_current_price(self) -> Optional[Dict]:
        """Get current bid/ask prices"""
        if not self.connected:
            return None
        
        tick = mt5.symbol_info_tick(self.symbol)
        
        if tick is None:
            logger.error(f"Failed to get tick: {mt5.last_error()}")
            return None
        
        return {
            'bid': tick.bid,
            'ask': tick.ask,
            'spread': round((tick.ask - tick.bid), 2),
            'spread_points': round((tick.ask - tick.bid) / mt5.symbol_info(self.symbol).point),
            'time': datetime.fromtimestamp(tick.time),
            'last': tick.last,
            'volume': tick.volume,
        }
    
    def get_positions(self) -> List[Dict]:
        """Get all open positions for this symbol"""
        if not self.connected:
            return []
        
        positions = mt5.positions_get(symbol=self.symbol)
        
        if positions is None:
            return []
        
        return [
            {
                'ticket': pos.ticket,
                'type': 'buy' if pos.type == mt5.ORDER_TYPE_BUY else 'sell',
                'volume': pos.volume,
                'open_price': pos.price_open,
                'current_price': pos.price_current,
                'profit': pos.profit,
                'sl': pos.sl,
                'tp': pos.tp,
                'magic': pos.magic,
                'comment': pos.comment,
                'time': datetime.fromtimestamp(pos.time),
            }
            for pos in positions
        ]
    
    def get_bot_positions(self) -> List[Dict]:
        """Get positions opened by this bot (matching magic number)"""
        all_positions = self.get_positions()
        return [p for p in all_positions if p.get('magic') == self.magic_number]
    
    def open_position(self, order_type: str, volume: float, sl: float = None, 
                      tp: float = None, comment: str = "") -> Optional[Dict]:
        """
        Open a new position
        
        Args:
            order_type: 'buy' or 'sell'
            volume: Position size (lots)
            sl: Stop loss price
            tp: Take profit price
            comment: Order comment
        
        Returns:
            Dict with order info or None if failed
        """
        if not self.connected:
            logger.error("Not connected to MT5")
            return None
        
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info is None:
            logger.error(f"Symbol {self.symbol} not found")
            return None
        
        if not symbol_info.visible:
            if not mt5.symbol_select(self.symbol, True):
                logger.error(f"Failed to select {self.symbol}")
                return None
        
        # Validate volume
        volume = max(symbol_info.volume_min, min(volume, symbol_info.volume_max))
        volume = round(volume / symbol_info.volume_step) * symbol_info.volume_step
        volume = round(volume, 2)
        
        # Get price
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            logger.error("Failed to get current price")
            return None
        
        price = tick.ask if order_type.lower() == 'buy' else tick.bid
        
        # Round SL and TP to proper digits
        digits = symbol_info.digits
        if sl is not None:
            sl = round(sl, digits)
        if tp is not None:
            tp = round(tp, digits)
        
        # Prepare request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if order_type.lower() == 'buy' else mt5.ORDER_TYPE_SELL,
            "price": price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": comment or "Gold Scalper Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        if sl is not None:
            request["sl"] = sl
        if tp is not None:
            request["tp"] = tp
        
        # Send order
        result = mt5.order_send(request)
        
        if result is None:
            logger.error(f"Order send failed: {mt5.last_error()}")
            return None
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_codes = {
                mt5.TRADE_RETCODE_REQUOTE: "Requote",
                mt5.TRADE_RETCODE_REJECT: "Request rejected",
                mt5.TRADE_RETCODE_CANCEL: "Request canceled",
                mt5.TRADE_RETCODE_PLACED: "Order placed",
                mt5.TRADE_RETCODE_DONE_PARTIAL: "Partial execution",
                mt5.TRADE_RETCODE_ERROR: "Request processing error",
                mt5.TRADE_RETCODE_TIMEOUT: "Request timeout",
                mt5.TRADE_RETCODE_INVALID: "Invalid request",
                mt5.TRADE_RETCODE_INVALID_VOLUME: "Invalid volume",
                mt5.TRADE_RETCODE_INVALID_PRICE: "Invalid price",
                mt5.TRADE_RETCODE_INVALID_STOPS: "Invalid SL/TP",
                mt5.TRADE_RETCODE_TRADE_DISABLED: "Trading disabled",
                mt5.TRADE_RETCODE_MARKET_CLOSED: "Market closed",
                mt5.TRADE_RETCODE_NO_MONEY: "Insufficient funds",
                mt5.TRADE_RETCODE_PRICE_CHANGED: "Price changed",
                mt5.TRADE_RETCODE_PRICE_OFF: "No quotes",
                mt5.TRADE_RETCODE_INVALID_EXPIRATION: "Invalid expiration",
                mt5.TRADE_RETCODE_ORDER_CHANGED: "Order state changed",
                mt5.TRADE_RETCODE_TOO_MANY_REQUESTS: "Too many requests",
            }
            error_msg = error_codes.get(result.retcode, f"Unknown error code: {result.retcode}")
            logger.error(f"Order failed: {error_msg} - {result.comment}")
            return None
        
        logger.info(f"✅ Order executed: {order_type.upper()} {volume} lots @ {result.price:.2f}")
        
        return {
            'ticket': result.order,
            'deal': result.deal,
            'volume': result.volume,
            'price': result.price,
            'type': order_type,
            'sl': sl,
            'tp': tp,
        }
    
    def close_position(self, ticket: int) -> bool:
        """Close an open position by ticket"""
        if not self.connected:
            logger.error("Not connected to MT5")
            return False
        
        position = mt5.positions_get(ticket=ticket)
        
        if position is None or len(position) == 0:
            logger.error(f"Position {ticket} not found")
            return False
        
        position = position[0]
        
        # Get current price
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            logger.error("Failed to get current price")
            return False
        
        # Prepare close request
        close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": "Close by Scalper Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request)
        
        if result is None:
            logger.error(f"Close order failed: {mt5.last_error()}")
            return False
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Close failed: {result.retcode} - {result.comment}")
            return False
        
        logger.info(f"✅ Position {ticket} closed @ {price:.2f}")
        return True
    
    def close_all_positions(self) -> int:
        """Close all positions for this symbol. Returns number of positions closed."""
        positions = self.get_bot_positions()
        closed = 0
        for pos in positions:
            if self.close_position(pos['ticket']):
                closed += 1
        return closed
    
    def modify_position(self, ticket: int, sl: float = None, tp: float = None) -> bool:
        """Modify stop loss and take profit of an open position"""
        if not self.connected:
            logger.error("Not connected to MT5")
            return False
        
        position = mt5.positions_get(ticket=ticket)
        
        if position is None or len(position) == 0:
            logger.error(f"Position {ticket} not found")
            return False
        
        position = position[0]
        
        # Round to proper digits
        symbol_info = mt5.symbol_info(self.symbol)
        digits = symbol_info.digits if symbol_info else 2
        
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": ticket,
            "sl": round(sl, digits) if sl is not None else position.sl,
            "tp": round(tp, digits) if tp is not None else position.tp,
        }
        
        result = mt5.order_send(request)
        
        if result is None:
            logger.error(f"Modify order failed: {mt5.last_error()}")
            return False
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Modify failed: {result.retcode} - {result.comment}")
            return False
        
        logger.info(f"Position {ticket} modified - SL: {sl}, TP: {tp}")
        return True
    
    def check_market_open(self) -> bool:
        """Check if market is currently open for trading"""
        if not self.connected:
            return False
        
        symbol_info = mt5.symbol_info(self.symbol)
        if symbol_info is None:
            return False
        
        # Check trade mode
        return symbol_info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL


class LiveTradingBot:
    """
    Live trading bot for configured 5-minute scalping strategy.
    Manages the complete trading loop including signal generation,
    position management, and risk control.
    """
    
    def __init__(self, adapter: MT5Adapter, strategy, risk_manager, 
                 timeframe: str = '5min', max_spread: float = 50.0,
                 min_signal_strength: int = 50):
        """
        Initialize the trading bot.
        
        Args:
            adapter: MT5Adapter instance
            strategy: ScalpingStrategy instance
            risk_manager: RiskManager instance
            timeframe: Trading timeframe (default: '5min')
            max_spread: Maximum acceptable spread in points (default: 50)
            min_signal_strength: Minimum signal strength to take trades (0-100)
        """
        self.adapter = adapter
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.timeframe = timeframe
        self.max_spread = max_spread
        self.min_signal_strength = min_signal_strength
        
        self.running = False
        self.last_signal_time = None
        self.last_bar_time = None
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.start_balance = 0.0
        
        # Statistics
        self.stats = {
            'signals_generated': 0,
            'trades_opened': 0,
            'trades_closed': 0,
            'profitable_trades': 0,
            'losing_trades': 0,
        }
        
    def start(self, check_interval: int = 10):
        """
        Start the trading bot.
        
        Args:
            check_interval: Seconds between market checks (default: 10)
        """
        if not self.adapter.connected:
            logger.error("Adapter not connected. Please connect first.")
            return
        
        # Get initial account info
        account_info = self.adapter.get_account_info()
        if account_info:
            self.start_balance = account_info['balance']
            self.risk_manager.account_balance = account_info['balance']
        
        logger.info("=" * 60)
        logger.info("GOLD SCALPER BOT STARTED")
        logger.info("=" * 60)
        logger.info(f"Symbol: {self.adapter.symbol}")
        logger.info(f"Timeframe: {self.timeframe}")
        logger.info(f"Check interval: {check_interval} seconds")
        logger.info(f"Max spread: {self.max_spread} points")
        logger.info(f"Account balance: ${self.start_balance:,.2f}")
        logger.info(f"Risk per trade: {self.risk_manager.risk_per_trade * 100}%")
        logger.info("=" * 60)
        logger.info("Press Ctrl+C to stop the bot")
        logger.info("")
        
        self.running = True
        
        try:
            while self.running:
                self._trading_loop()
                time.sleep(check_interval)
        except KeyboardInterrupt:
            logger.info("")
            logger.info("🛑 Bot stopped by user")
            self._print_session_summary()
        except Exception as e:
            logger.error(f"Error in trading loop: {e}")
            self.running = False
            raise
    
    def stop(self):
        """Stop the trading bot"""
        self.running = False
        logger.info("Bot stopping...")
        self._print_session_summary()
    
    def _trading_loop(self):
        """Main trading logic loop"""
        
        # Check if market is open
        if not self.adapter.check_market_open():
            return
        
        # Get current market data
        df = self.adapter.get_historical_data(timeframe=self.timeframe, bars=200)
        
        if df is None or len(df) < 50:
            logger.warning("Insufficient market data")
            return
        
        # Check if new bar has formed
        current_bar_time = df.iloc[-1]['time']
        if self.last_bar_time == current_bar_time:
            # Still on same bar, just monitor positions
            self._monitor_positions()
            return
        
        self.last_bar_time = current_bar_time
        
        # Generate signals on the completed bar (not the current forming bar)
        df = self.strategy.generate_signals(df)
        
        # Use the second-to-last bar for signals (completed bar)
        signal_bar = df.iloc[-2]
        
        # Get current price info
        price_info = self.adapter.get_current_price()
        
        if price_info is None:
            logger.warning("Could not get current price")
            return
        
        # Check spread
        spread_points = price_info.get('spread_points', 0)
        
        # Get current positions
        positions = self.adapter.get_bot_positions()
        
        # Log status
        signal_str = "BUY 🟢" if signal_bar['signal'] == 1 else ("SELL 🔴" if signal_bar['signal'] == -1 else "NONE")
        logger.info(
            f"[{current_bar_time}] Price: {price_info['bid']:.2f} | "
            f"Spread: {spread_points:.0f}pts | "
            f"Signal: {signal_str} | "
            f"Positions: {len(positions)}"
        )
        
        # Trading logic
        if len(positions) < self.risk_manager.max_positions and signal_bar['signal'] != 0:
            
            # Check if we already traded on this signal
            if self.last_signal_time == signal_bar['time']:
                return
            
            # Check spread
            if spread_points > self.max_spread:
                logger.warning(f"Spread too high: {spread_points:.0f} points (max: {self.max_spread})")
                return
            
            # Check signal strength
            signal_strength = self.strategy.get_signal_strength(df.iloc[:-1])  # Exclude current bar
            if signal_strength < self.min_signal_strength:
                logger.info(f"Signal strength too low: {signal_strength}% (min: {self.min_signal_strength}%)")
                return
            
            self.stats['signals_generated'] += 1
            
            # Get ATR for risk calculations
            atr = signal_bar.get('ATR', 2.0)
            if pd.isna(atr) or atr <= 0:
                atr = 2.0  # Default ATR for gold
            
            # Calculate position size
            position_size = self.risk_manager.calculate_position_size(atr, price_info['bid'])
            
            if signal_bar['signal'] == 1:  # Buy signal
                entry_price = price_info['ask']
                sl = self.risk_manager.calculate_stop_loss(entry_price, atr, 1)
                tp = self.risk_manager.calculate_take_profit(entry_price, atr, 1)
                
                logger.info(f"📊 BUY Signal - Strength: {signal_strength}%")
                logger.info(f"   Entry: {entry_price:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | Size: {position_size}")
                
                result = self.adapter.open_position(
                    'buy',
                    position_size,
                    sl=sl,
                    tp=tp,
                    comment=f"Scalper BUY S{signal_strength}"
                )
                
                if result:
                    self.last_signal_time = signal_bar['time']
                    self.stats['trades_opened'] += 1
                    self.daily_trades += 1
            
            elif signal_bar['signal'] == -1:  # Sell signal
                entry_price = price_info['bid']
                sl = self.risk_manager.calculate_stop_loss(entry_price, atr, -1)
                tp = self.risk_manager.calculate_take_profit(entry_price, atr, -1)
                
                logger.info(f"📊 SELL Signal - Strength: {signal_strength}%")
                logger.info(f"   Entry: {entry_price:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | Size: {position_size}")
                
                result = self.adapter.open_position(
                    'sell',
                    position_size,
                    sl=sl,
                    tp=tp,
                    comment=f"Scalper SELL S{signal_strength}"
                )
                
                if result:
                    self.last_signal_time = signal_bar['time']
                    self.stats['trades_opened'] += 1
                    self.daily_trades += 1
        
        # Monitor existing positions
        self._monitor_positions()
    
    def _monitor_positions(self):
        """Monitor and log existing positions"""
        positions = self.adapter.get_bot_positions()
        
        for pos in positions:
            profit = pos['profit']
            direction = "↑" if profit > 0 else "↓" if profit < 0 else "→"
            logger.debug(
                f"   📍 Ticket {pos['ticket']}: {pos['type'].upper()} | "
                f"P&L: ${profit:+.2f} {direction}"
            )
    
    def _print_session_summary(self):
        """Print session summary when bot stops"""
        account_info = self.adapter.get_account_info()
        current_balance = account_info['balance'] if account_info else self.start_balance
        session_pnl = current_balance - self.start_balance
        
        logger.info("")
        logger.info("=" * 60)
        logger.info("SESSION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Starting Balance: ${self.start_balance:,.2f}")
        logger.info(f"Current Balance:  ${current_balance:,.2f}")
        logger.info(f"Session P&L:      ${session_pnl:+,.2f}")
        logger.info(f"Signals Generated: {self.stats['signals_generated']}")
        logger.info(f"Trades Opened:     {self.stats['trades_opened']}")
        logger.info("=" * 60)


# Example usage for live trading
if __name__ == "__main__":
    from gold_scalper import ScalpingStrategy, RiskManager
    from config import BROKER_CONFIG, STRATEGY_CONFIG, RISK_CONFIG, TRADING_CONFIG
    
    logger.info("=" * 60)
    logger.info("SCALPER - LIVE TRADING MODE")
    logger.info("=" * 60)
    
    # Initialize MT5 adapter
    adapter = MT5Adapter(
        account=int(BROKER_CONFIG.get('account_id')) if BROKER_CONFIG.get('account_id') else None,
        password=BROKER_CONFIG.get('password') or None,
        server=BROKER_CONFIG.get('server') or None,
        symbol=TRADING_CONFIG.get('symbol', 'XAUUSD')
    )
    
    # Connect to MT5
    if not adapter.connect():
        logger.error("Failed to connect to MT5")
        exit(1)
    
    # Show account info
    account_info = adapter.get_account_info()
    if account_info:
        logger.info(f"Account: {account_info['login']}")
        logger.info(f"Server: {account_info['server']}")
        logger.info(f"Balance: ${account_info['balance']:,.2f}")
        logger.info(f"Equity: ${account_info['equity']:,.2f}")
        logger.info(f"Leverage: 1:{account_info['leverage']}")
    
    # Show symbol info
    symbol_info = adapter.get_symbol_info()
    if symbol_info:
        logger.info(f"Symbol: {symbol_info['symbol']}")
        logger.info(f"Current Spread: {symbol_info['spread']} points")
        logger.info(f"Min Lot: {symbol_info['volume_min']}")
    
    # Initialize strategy and risk manager
    strategy = ScalpingStrategy(**STRATEGY_CONFIG)
    risk_manager = RiskManager(**RISK_CONFIG)

    profile = get_symbol_profile(
        TRADING_CONFIG.get('symbol', 'GOLD'),
        default_max_spread=TRADING_CONFIG.get('max_spread', 50.0),
        default_min_strength=TRADING_CONFIG.get('min_signal_strength', 50),
    )
    
    if account_info:
        risk_manager.account_balance = account_info['balance']
    
    # Create and start bot
    bot = LiveTradingBot(
        adapter=adapter,
        strategy=strategy,
        risk_manager=risk_manager,
        timeframe=TRADING_CONFIG.get('timeframe', '5min'),
        max_spread=profile['max_spread'],
        min_signal_strength=profile['min_signal_strength'],
    )
    
    try:
        bot.start(check_interval=10)  # Check every 10 seconds
    finally:
        adapter.disconnect()