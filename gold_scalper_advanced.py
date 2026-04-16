"""
Advanced Multi-Strategy Scalping Robot
=======================================
Combines:
- Smart Money Concepts (SMC): Order Blocks, FVG, BOS, CHoCH
- ICT Concepts: OTE Zone, Liquidity, Premium/Discount, Kill Zones
- Order Flow: Volume Analysis, Delta Approximation
- Fibonacci Confluence: Key levels, Extensions

Optimized for 5-minute timeframe scalping
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')


class TechnicalIndicators:
    """Calculate technical indicators for trading signals"""
    
    @staticmethod
    def ema(data, period):
        """Exponential Moving Average"""
        return data.ewm(span=period, adjust=False).mean()
    
    @staticmethod
    def sma(data, period):
        """Simple Moving Average"""
        return data.rolling(window=period).mean()
    
    @staticmethod
    def rsi(data, period=14):
        """Relative Strength Index"""
        delta = data.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
    
    @staticmethod
    def bollinger_bands(data, period=20, std_dev=2):
        """Bollinger Bands"""
        sma = data.rolling(window=period).mean()
        std = data.rolling(window=period).std()
        upper = sma + (std * std_dev)
        lower = sma - (std * std_dev)
        return upper, sma, lower
    
    @staticmethod
    def atr(high, low, close, period=14):
        """Average True Range"""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()
    
    @staticmethod
    def macd(data, fast=12, slow=26, signal=9):
        """MACD Indicator"""
        ema_fast = data.ewm(span=fast, adjust=False).mean()
        ema_slow = data.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram


class SmartMoneyConcepts:
    """
    Smart Money Concepts (SMC) Analysis
    - Order Blocks (OB): Institutional candle patterns
    - Fair Value Gaps (FVG): Price imbalances
    - Break of Structure (BOS): Trend continuation
    - Change of Character (CHoCH): Trend reversal
    - Liquidity Sweeps: Stop hunt patterns
    """
    
    def __init__(self, swing_lookback=10, fvg_threshold=0.5):
        self.swing_lookback = swing_lookback
        self.fvg_threshold = fvg_threshold
    
    def identify_swing_points(self, df):
        """Identify swing highs and swing lows"""
        df = df.copy()
        lookback = self.swing_lookback
        
        df['swing_high'] = False
        df['swing_low'] = False
        df['swing_high_price'] = np.nan
        df['swing_low_price'] = np.nan
        
        for i in range(lookback, len(df) - lookback):
            # Swing High: highest point in lookback window
            if df['high'].iloc[i] == df['high'].iloc[i-lookback:i+lookback+1].max():
                df.loc[df.index[i], 'swing_high'] = True
                df.loc[df.index[i], 'swing_high_price'] = df['high'].iloc[i]
            
            # Swing Low: lowest point in lookback window
            if df['low'].iloc[i] == df['low'].iloc[i-lookback:i+lookback+1].min():
                df.loc[df.index[i], 'swing_low'] = True
                df.loc[df.index[i], 'swing_low_price'] = df['low'].iloc[i]
        
        # Forward fill swing points for reference
        df['last_swing_high'] = df['swing_high_price'].ffill()
        df['last_swing_low'] = df['swing_low_price'].ffill()
        
        return df
    
    def detect_market_structure(self, df):
        """
        Detect Break of Structure (BOS) and Change of Character (CHoCH)
        - BOS: Price breaks previous swing in trend direction (continuation)
        - CHoCH: Price breaks previous swing against trend (reversal signal)
        """
        df = df.copy()
        
        df['bos_bullish'] = False
        df['bos_bearish'] = False
        df['choch_bullish'] = False
        df['choch_bearish'] = False
        df['market_structure'] = 0  # 1 = bullish, -1 = bearish
        
        current_structure = 0
        last_high = df['high'].iloc[0]
        last_low = df['low'].iloc[0]
        
        for i in range(1, len(df)):
            high = df['high'].iloc[i]
            low = df['low'].iloc[i]
            
            # Check for Break of Structure
            if high > last_high:
                if current_structure == 1:
                    # Continuation - BOS Bullish
                    df.loc[df.index[i], 'bos_bullish'] = True
                elif current_structure == -1:
                    # Reversal - CHoCH Bullish
                    df.loc[df.index[i], 'choch_bullish'] = True
                    current_structure = 1
                else:
                    current_structure = 1
                last_high = high
            
            if low < last_low:
                if current_structure == -1:
                    # Continuation - BOS Bearish
                    df.loc[df.index[i], 'bos_bearish'] = True
                elif current_structure == 1:
                    # Reversal - CHoCH Bearish
                    df.loc[df.index[i], 'choch_bearish'] = True
                    current_structure = -1
                else:
                    current_structure = -1
                last_low = low
            
            df.loc[df.index[i], 'market_structure'] = current_structure
        
        return df
    
    def identify_order_blocks(self, df):
        """
        Identify Order Blocks (OB)
        - Bullish OB: Last down candle before strong up move
        - Bearish OB: Last up candle before strong down move
        
        Order blocks represent areas where institutional orders were placed.
        """
        df = df.copy()
        
        df['bullish_ob'] = False
        df['bearish_ob'] = False
        df['bullish_ob_high'] = np.nan
        df['bullish_ob_low'] = np.nan
        df['bearish_ob_high'] = np.nan
        df['bearish_ob_low'] = np.nan
        
        atr = TechnicalIndicators.atr(df['high'], df['low'], df['close'], 14)
        
        for i in range(3, len(df) - 1):
            # Check for Bullish Order Block
            # Conditions: Down candle followed by strong impulse up
            if df['close'].iloc[i-1] < df['open'].iloc[i-1]:  # Previous candle is bearish
                # Check if current candle is strong bullish impulse
                current_body = abs(df['close'].iloc[i] - df['open'].iloc[i])
                if (df['close'].iloc[i] > df['open'].iloc[i] and 
                    current_body > atr.iloc[i] * 1.5):
                    df.loc[df.index[i-1], 'bullish_ob'] = True
                    df.loc[df.index[i-1], 'bullish_ob_high'] = df['high'].iloc[i-1]
                    df.loc[df.index[i-1], 'bullish_ob_low'] = df['low'].iloc[i-1]
            
            # Check for Bearish Order Block
            # Conditions: Up candle followed by strong impulse down
            if df['close'].iloc[i-1] > df['open'].iloc[i-1]:  # Previous candle is bullish
                # Check if current candle is strong bearish impulse
                current_body = abs(df['close'].iloc[i] - df['open'].iloc[i])
                if (df['close'].iloc[i] < df['open'].iloc[i] and 
                    current_body > atr.iloc[i] * 1.5):
                    df.loc[df.index[i-1], 'bearish_ob'] = True
                    df.loc[df.index[i-1], 'bearish_ob_high'] = df['high'].iloc[i-1]
                    df.loc[df.index[i-1], 'bearish_ob_low'] = df['low'].iloc[i-1]
        
        # Forward fill order block zones
        df['active_bull_ob_high'] = df['bullish_ob_high'].ffill()
        df['active_bull_ob_low'] = df['bullish_ob_low'].ffill()
        df['active_bear_ob_high'] = df['bearish_ob_high'].ffill()
        df['active_bear_ob_low'] = df['bearish_ob_low'].ffill()
        
        return df
    
    def identify_fair_value_gaps(self, df):
        """
        Identify Fair Value Gaps (FVG) / Imbalances
        - Bullish FVG: Gap between candle 1 high and candle 3 low (price inefficiency)
        - Bearish FVG: Gap between candle 1 low and candle 3 high
        
        FVGs represent areas where price moved too fast without proper price discovery.
        """
        df = df.copy()
        
        df['bullish_fvg'] = False
        df['bearish_fvg'] = False
        df['fvg_upper'] = np.nan
        df['fvg_lower'] = np.nan
        
        for i in range(2, len(df)):
            # Bullish FVG: candle 1 high < candle 3 low
            if df['high'].iloc[i-2] < df['low'].iloc[i]:
                gap = df['low'].iloc[i] - df['high'].iloc[i-2]
                atr_val = df['ATR'].iloc[i] if 'ATR' in df.columns else 1
                if gap > atr_val * self.fvg_threshold:
                    df.loc[df.index[i-1], 'bullish_fvg'] = True
                    df.loc[df.index[i-1], 'fvg_upper'] = df['low'].iloc[i]
                    df.loc[df.index[i-1], 'fvg_lower'] = df['high'].iloc[i-2]
            
            # Bearish FVG: candle 1 low > candle 3 high
            if df['low'].iloc[i-2] > df['high'].iloc[i]:
                gap = df['low'].iloc[i-2] - df['high'].iloc[i]
                atr_val = df['ATR'].iloc[i] if 'ATR' in df.columns else 1
                if gap > atr_val * self.fvg_threshold:
                    df.loc[df.index[i-1], 'bearish_fvg'] = True
                    df.loc[df.index[i-1], 'fvg_upper'] = df['low'].iloc[i-2]
                    df.loc[df.index[i-1], 'fvg_lower'] = df['high'].iloc[i]
        
        return df
    
    def detect_liquidity_sweeps(self, df):
        """
        Detect liquidity sweeps (stop hunts)
        - Bullish sweep: Price briefly goes below swing low then reverses up
        - Bearish sweep: Price briefly goes above swing high then reverses down
        """
        df = df.copy()
        
        df['liquidity_sweep_bull'] = False
        df['liquidity_sweep_bear'] = False
        
        for i in range(2, len(df)):
            # Bullish liquidity sweep
            if (df['low'].iloc[i] < df['last_swing_low'].iloc[i-1] and
                df['close'].iloc[i] > df['last_swing_low'].iloc[i-1]):
                df.loc[df.index[i], 'liquidity_sweep_bull'] = True
            
            # Bearish liquidity sweep
            if (df['high'].iloc[i] > df['last_swing_high'].iloc[i-1] and
                df['close'].iloc[i] < df['last_swing_high'].iloc[i-1]):
                df.loc[df.index[i], 'liquidity_sweep_bear'] = True
        
        return df


class ICTConcepts:
    """
    ICT (Inner Circle Trader) Concepts
    - Optimal Trade Entry (OTE): 62-79% Fibonacci retracement zone
    - Premium/Discount Zones: Price above/below equilibrium
    - Kill Zones: High-probability trading sessions
    - Liquidity Pools: Equal highs/lows where stops cluster
    """
    
    def __init__(self, lookback=50):
        self.lookback = lookback
        self.ote_lower = 0.62  # 62% retracement
        self.ote_upper = 0.79  # 79% retracement
    
    def calculate_premium_discount(self, df):
        """
        Calculate premium and discount zones based on recent swing range
        - Premium: Upper 50% of range (sell zone)
        - Discount: Lower 50% of range (buy zone)
        - Equilibrium: 50% level
        """
        df = df.copy()
        
        lookback = self.lookback
        df['swing_range_high'] = df['high'].rolling(lookback).max()
        df['swing_range_low'] = df['low'].rolling(lookback).min()
        df['equilibrium'] = (df['swing_range_high'] + df['swing_range_low']) / 2
        
        # Premium zone (upper 50%)
        df['premium_zone'] = df['close'] > df['equilibrium']
        
        # Discount zone (lower 50%)
        df['discount_zone'] = df['close'] < df['equilibrium']
        
        # Calculate position in range (0 = low, 1 = high)
        range_size = df['swing_range_high'] - df['swing_range_low']
        df['range_position'] = (df['close'] - df['swing_range_low']) / range_size
        
        return df
    
    def identify_ote_zone(self, df):
        """
        Identify Optimal Trade Entry (OTE) zone
        OTE is the 62-79% Fibonacci retracement zone
        This is where institutional traders often enter positions
        """
        df = df.copy()
        
        lookback = 20
        
        # Calculate rolling high/low
        recent_high = df['high'].rolling(lookback).max()
        recent_low = df['low'].rolling(lookback).min()
        swing_range = recent_high - recent_low
        
        # Calculate OTE zone levels
        ote_buy_upper = recent_low + (swing_range * (1 - self.ote_lower))
        ote_buy_lower = recent_low + (swing_range * (1 - self.ote_upper))
        ote_sell_upper = recent_low + (swing_range * self.ote_upper)
        ote_sell_lower = recent_low + (swing_range * self.ote_lower)
        
        # Vectorized zone detection
        df['ote_buy_zone'] = (df['close'] >= ote_buy_lower) & (df['close'] <= ote_buy_upper)
        df['ote_sell_zone'] = (df['close'] >= ote_sell_lower) & (df['close'] <= ote_sell_upper)
        df['in_ote_zone'] = df['ote_buy_zone'] | df['ote_sell_zone']
        
        return df
    
    def identify_liquidity_pools(self, df):
        """
        Identify liquidity pools (equal highs/lows)
        These are areas where retail stop losses cluster
        """
        df = df.copy()
        
        tolerance = 0.0003  # 0.03% tolerance
        
        # Rolling range calculations for equal highs/lows
        high_max = df['high'].rolling(4).max()
        high_min = df['high'].rolling(4).min()
        high_mean = df['high'].rolling(4).mean()
        
        low_max = df['low'].rolling(4).max()
        low_min = df['low'].rolling(4).min()
        low_mean = df['low'].rolling(4).mean()
        
        df['equal_highs'] = (high_max - high_min) < (high_mean * tolerance)
        df['equal_lows'] = (low_max - low_min) < (low_mean * tolerance)
        
        return df
    
    def identify_kill_zones(self, df):
        """
        Identify ICT Kill Zones (high-probability trading times)
        - Asian Session: 00:00 - 05:00 UTC (consolidation)
        - London Open: 07:00 - 10:00 UTC
        - NY Open: 12:00 - 15:00 UTC
        - London Close: 15:00 - 17:00 UTC
        """
        df = df.copy()
        
        if 'time' not in df.columns:
            # If no time column, assume all are valid
            df['kill_zone'] = True
            df['session'] = 'unknown'
            return df
        
        df['hour'] = pd.to_datetime(df['time']).dt.hour
        
        # Kill zones
        df['asian_session'] = (df['hour'] >= 0) & (df['hour'] < 5)
        df['london_open'] = (df['hour'] >= 7) & (df['hour'] < 10)
        df['ny_open'] = (df['hour'] >= 12) & (df['hour'] < 15)
        df['london_close'] = (df['hour'] >= 15) & (df['hour'] < 17)
        
        # Combined kill zone (best times to trade)
        df['kill_zone'] = df['london_open'] | df['ny_open']
        
        # Session label
        df['session'] = 'low_activity'
        df.loc[df['asian_session'], 'session'] = 'asian'
        df.loc[df['london_open'], 'session'] = 'london'
        df.loc[df['ny_open'], 'session'] = 'new_york'
        df.loc[df['london_close'], 'session'] = 'london_close'
        
        return df


class OrderFlowAnalysis:
    """
    Order Flow Analysis using OHLCV data
    - Volume Analysis: Unusual volume detection
    - Delta Approximation: Buying vs selling pressure
    - Volume Profile: High volume nodes
    - Absorption: Large volume with small price movement
    """
    
    def __init__(self, volume_lookback=20):
        self.volume_lookback = volume_lookback
    
    def analyze_volume(self, df):
        """Analyze volume patterns"""
        df = df.copy()
        
        if 'volume' not in df.columns:
            df['volume'] = 100  # Default if no volume
        
        # Volume moving average
        df['vol_sma'] = df['volume'].rolling(self.volume_lookback).mean()
        df['vol_std'] = df['volume'].rolling(self.volume_lookback).std()
        
        # High volume detection (> 1.5x average)
        df['high_volume'] = df['volume'] > (df['vol_sma'] * 1.5)
        
        # Very high volume (> 2x average)
        df['very_high_volume'] = df['volume'] > (df['vol_sma'] * 2)
        
        # Volume ratio
        df['volume_ratio'] = df['volume'] / df['vol_sma']
        
        return df
    
    def calculate_delta(self, df):
        """
        Approximate buying/selling pressure (delta) from OHLC
        - Bullish candle with close near high = buying pressure
        - Bearish candle with close near low = selling pressure
        """
        df = df.copy()
        
        # Candle body
        df['body'] = df['close'] - df['open']
        df['candle_range'] = df['high'] - df['low']
        
        # Prevent division by zero
        df['candle_range'] = df['candle_range'].replace(0, 0.0001)
        
        # Position of close within the range (0 = low, 1 = high)
        df['close_position'] = (df['close'] - df['low']) / df['candle_range']
        
        # Delta approximation
        # Positive = buying pressure, Negative = selling pressure
        df['delta'] = (df['close_position'] - 0.5) * 2 * df['volume']
        
        # Cumulative delta
        df['cumulative_delta'] = df['delta'].cumsum()
        
        # Delta divergence (price up but delta down, or vice versa)
        df['price_change'] = df['close'].diff()
        df['delta_divergence_bull'] = (df['delta'] > 0) & (df['price_change'] < 0)
        df['delta_divergence_bear'] = (df['delta'] < 0) & (df['price_change'] > 0)
        
        return df
    
    def detect_absorption(self, df):
        """
        Detect absorption patterns
        High volume with small price movement indicates absorption
        """
        df = df.copy()
        
        atr = TechnicalIndicators.atr(df['high'], df['low'], df['close'], 14)
        
        # Small range relative to ATR
        df['small_range'] = df['candle_range'] < (atr * 0.5)
        
        # Absorption = high volume + small range
        df['absorption'] = df['high_volume'] & df['small_range']
        
        # Bullish absorption (at support)
        df['bullish_absorption'] = df['absorption'] & (df['close'] > df['open'])
        
        # Bearish absorption (at resistance)
        df['bearish_absorption'] = df['absorption'] & (df['close'] < df['open'])
        
        return df
    
    def volume_profile_analysis(self, df, num_levels=10):
        """
        Simple volume profile analysis
        Identify high volume nodes (HVN) and low volume nodes (LVN)
        """
        df = df.copy()
        
        lookback = 50
        
        # Simplified volume profile - use rolling calculations
        hvn_levels = np.full(len(df), np.nan)
        lvn_levels = np.full(len(df), np.nan)
        
        # Use simplified approach - HVN is where most volume concentrated
        df['vol_weighted_price'] = df['close'] * df['volume']
        df['vwap_rolling'] = df['vol_weighted_price'].rolling(lookback).sum() / df['volume'].rolling(lookback).sum()
        
        # HVN approximation: VWAP (volume weighted average price)
        df['hvn_level'] = df['vwap_rolling']
        
        # LVN approximation: furthest price from VWAP in recent range
        df['price_range_high'] = df['high'].rolling(lookback).max()
        df['price_range_low'] = df['low'].rolling(lookback).min()
        
        # LVN is at range extremes (low volume typically at swing points)
        df['lvn_level'] = np.where(
            abs(df['close'] - df['price_range_high']) < abs(df['close'] - df['price_range_low']),
            df['price_range_low'],
            df['price_range_high']
        )
        
        # Clean up temp columns
        df = df.drop(columns=['vol_weighted_price', 'vwap_rolling', 'price_range_high', 'price_range_low'], errors='ignore')
        
        return df


class FibonacciAnalysis:
    """
    Fibonacci Analysis for confluence
    - Retracement levels: 0.236, 0.382, 0.5, 0.618, 0.786
    - Extension levels: 1.0, 1.272, 1.618, 2.0
    - OTE Zone: 0.618 - 0.786
    """
    
    def __init__(self, lookback=50):
        self.lookback = lookback
        self.retracement_levels = [0.236, 0.382, 0.5, 0.618, 0.786]
        self.extension_levels = [1.0, 1.272, 1.618, 2.0]
    
    def calculate_fib_levels(self, df):
        """Calculate dynamic Fibonacci levels based on recent swing"""
        df = df.copy()
        
        lookback = self.lookback
        
        # Calculate rolling swing high/low
        df['_swing_high'] = df['high'].rolling(lookback).max()
        df['_swing_low'] = df['low'].rolling(lookback).min()
        df['_swing_range'] = df['_swing_high'] - df['_swing_low']
        
        # Determine trend by comparing position of high vs low in window
        df['_high_pos'] = df['high'].rolling(lookback).apply(lambda x: x.argmax(), raw=True)
        df['_low_pos'] = df['low'].rolling(lookback).apply(lambda x: x.argmin(), raw=True)
        df['_uptrend'] = df['_high_pos'] > df['_low_pos']
        
        # Calculate Fib levels
        for level in self.retracement_levels:
            col_name = f'fib_{int(level*1000)}'
            df[col_name] = np.where(
                df['_uptrend'],
                df['_swing_high'] - (df['_swing_range'] * level),
                df['_swing_low'] + (df['_swing_range'] * level)
            )
        
        # Clean up temp columns
        df = df.drop(columns=['_swing_high', '_swing_low', '_swing_range', '_high_pos', '_low_pos', '_uptrend'], errors='ignore')
        
        return df
    
    def detect_fib_confluence(self, df, tolerance=0.002):
        """
        Detect when price is at Fibonacci confluence zones
        Multiple Fib levels at similar price = strong confluence
        """
        df = df.copy()
        
        fib_cols = [f'fib_{int(l*1000)}' for l in self.retracement_levels]
        
        # Initialize arrays for vectorized operations
        fib_confluence = np.zeros(len(df))
        at_fib_level = np.zeros(len(df), dtype=bool)
        nearest_fib = np.full(len(df), np.nan)
        
        for i in range(len(df)):
            current_price = df['close'].iloc[i]
            confluence_count = 0
            nearest_dist = float('inf')
            nearest_level = np.nan
            
            for col in fib_cols:
                if col in df.columns:
                    fib_price = df[col].iloc[i]
                    if pd.notna(fib_price) and current_price > 0:
                        distance = abs(current_price - fib_price) / current_price
                        
                        if distance < tolerance:
                            confluence_count += 1
                            at_fib_level[i] = True
                        
                        if distance < nearest_dist:
                            nearest_dist = distance
                            nearest_level = fib_price
            
            fib_confluence[i] = confluence_count
            nearest_fib[i] = nearest_level
        
        df['fib_confluence'] = fib_confluence.astype(int)
        df['at_fib_level'] = at_fib_level
        df['nearest_fib'] = nearest_fib
        
        return df
    
    def identify_ote_entries(self, df):
        """
        Identify entries in the Optimal Trade Entry zone (0.618 - 0.786)
        """
        df = df.copy()
        
        df['in_fib_ote'] = False
        
        if 'fib_618' in df.columns and 'fib_786' in df.columns:
            fib_618 = df['fib_618']
            fib_786 = df['fib_786']
            
            # Check if price is between 61.8% and 78.6% retracement
            df['in_fib_ote'] = (
                ((df['close'] >= fib_618) & (df['close'] <= fib_786)) |
                ((df['close'] <= fib_618) & (df['close'] >= fib_786))
            )
        
        return df


class AdvancedScalpingStrategy:
    """
    Advanced 5-Minute Scalping Strategy
    Combines: SMC + ICT + Order Flow + Fibonacci
    
    Signal Generation Logic:
    1. Market Structure: Check for BOS/CHoCH (trend)
    2. SMC: Check for order blocks and FVG
    3. ICT: Check OTE zone and kill zones
    4. Order Flow: Volume confirmation and delta
    5. Fibonacci: Confluence at key levels
    6. Traditional: EMA, RSI, MACD confirmation
    """
    
    def __init__(self, ema_fast=9, ema_slow=21, rsi_period=14,
                 rsi_oversold=30, rsi_overbought=70, bb_period=20, bb_std=2,
                 min_confluence_score=3):
        self.ti = TechnicalIndicators()
        self.smc = SmartMoneyConcepts()
        self.ict = ICTConcepts()
        self.order_flow = OrderFlowAnalysis()
        self.fib = FibonacciAnalysis()
        
        # Traditional params
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_period = bb_period
        self.bb_std = bb_std
        
        # Minimum score for signal
        self.min_confluence_score = min_confluence_score
    
    def generate_signals(self, df):
        """
        Generate trading signals with multi-confluence analysis
        """
        df = df.copy()
        
        # === TRADITIONAL INDICATORS ===
        df['EMA_fast'] = self.ti.ema(df['close'], self.ema_fast)
        df['EMA_slow'] = self.ti.ema(df['close'], self.ema_slow)
        df['RSI'] = self.ti.rsi(df['close'], self.rsi_period)
        df['BB_upper'], df['BB_middle'], df['BB_lower'] = self.ti.bollinger_bands(
            df['close'], self.bb_period, self.bb_std
        )
        df['MACD'], df['MACD_signal'], df['MACD_hist'] = self.ti.macd(df['close'])
        df['ATR'] = self.ti.atr(df['high'], df['low'], df['close'])
        
        # Trend detection
        df['trend'] = np.where(df['EMA_fast'] > df['EMA_slow'], 1, -1)
        
        # === SMART MONEY CONCEPTS ===
        df = self.smc.identify_swing_points(df)
        df = self.smc.detect_market_structure(df)
        df = self.smc.identify_order_blocks(df)
        df = self.smc.identify_fair_value_gaps(df)
        df = self.smc.detect_liquidity_sweeps(df)
        
        # === ICT CONCEPTS ===
        df = self.ict.calculate_premium_discount(df)
        df = self.ict.identify_ote_zone(df)
        df = self.ict.identify_liquidity_pools(df)
        df = self.ict.identify_kill_zones(df)
        
        # === ORDER FLOW ===
        df = self.order_flow.analyze_volume(df)
        df = self.order_flow.calculate_delta(df)
        df = self.order_flow.detect_absorption(df)
        
        # === FIBONACCI ===
        df = self.fib.calculate_fib_levels(df)
        df = self.fib.detect_fib_confluence(df)
        df = self.fib.identify_ote_entries(df)
        
        # === CONFLUENCE SCORING ===
        df['buy_score'] = 0
        df['sell_score'] = 0
        
        # Score each factor for BUY signals
        # 1. Market Structure (+2)
        df.loc[df['market_structure'] == 1, 'buy_score'] += 2
        df.loc[df['choch_bullish'], 'buy_score'] += 3
        df.loc[df['bos_bullish'], 'buy_score'] += 1
        
        # 2. Order Block (+2)
        df.loc[
            (df['close'] >= df['active_bull_ob_low']) & 
            (df['close'] <= df['active_bull_ob_high']), 
            'buy_score'
        ] += 2
        
        # 3. Fair Value Gap (+2)
        df.loc[df['bullish_fvg'], 'buy_score'] += 2
        
        # 4. Liquidity Sweep (+2)
        df.loc[df['liquidity_sweep_bull'], 'buy_score'] += 2
        
        # 5. ICT Premium/Discount (+2)
        df.loc[df['discount_zone'], 'buy_score'] += 2
        
        # 6. OTE Zone (+2)
        df.loc[df['ote_buy_zone'] | df['in_fib_ote'], 'buy_score'] += 2
        
        # 7. Kill Zone (+1)
        df.loc[df['kill_zone'], 'buy_score'] += 1
        
        # 8. Order Flow (+2)
        df.loc[df['delta'] > 0, 'buy_score'] += 1
        df.loc[df['high_volume'] & (df['close'] > df['open']), 'buy_score'] += 1
        df.loc[df['bullish_absorption'], 'buy_score'] += 2
        
        # 9. Fibonacci Confluence (+2)
        df.loc[df['fib_confluence'] >= 2, 'buy_score'] += 2
        df.loc[df['at_fib_level'], 'buy_score'] += 1
        
        # 10. Traditional Indicators
        df.loc[df['RSI'] < 50, 'buy_score'] += 1
        df.loc[df['RSI'] < 30, 'buy_score'] += 1
        df.loc[df['MACD_hist'] > 0, 'buy_score'] += 1
        df.loc[df['MACD_hist'] > df['MACD_hist'].shift(1), 'buy_score'] += 1
        df.loc[df['close'] < df['BB_lower'], 'buy_score'] += 2
        df.loc[(df['EMA_fast'] > df['EMA_slow']) & (df['EMA_fast'].shift(1) <= df['EMA_slow'].shift(1)), 'buy_score'] += 2
        
        # Score each factor for SELL signals
        # 1. Market Structure
        df.loc[df['market_structure'] == -1, 'sell_score'] += 2
        df.loc[df['choch_bearish'], 'sell_score'] += 3
        df.loc[df['bos_bearish'], 'sell_score'] += 1
        
        # 2. Order Block
        df.loc[
            (df['close'] >= df['active_bear_ob_low']) & 
            (df['close'] <= df['active_bear_ob_high']), 
            'sell_score'
        ] += 2
        
        # 3. Fair Value Gap
        df.loc[df['bearish_fvg'], 'sell_score'] += 2
        
        # 4. Liquidity Sweep
        df.loc[df['liquidity_sweep_bear'], 'sell_score'] += 2
        
        # 5. ICT Premium/Discount
        df.loc[df['premium_zone'], 'sell_score'] += 2
        
        # 6. OTE Zone
        df.loc[df['ote_sell_zone'] | df['in_fib_ote'], 'sell_score'] += 2
        
        # 7. Kill Zone
        df.loc[df['kill_zone'], 'sell_score'] += 1
        
        # 8. Order Flow
        df.loc[df['delta'] < 0, 'sell_score'] += 1
        df.loc[df['high_volume'] & (df['close'] < df['open']), 'sell_score'] += 1
        df.loc[df['bearish_absorption'], 'sell_score'] += 2
        
        # 9. Fibonacci Confluence
        df.loc[df['fib_confluence'] >= 2, 'sell_score'] += 2
        df.loc[df['at_fib_level'], 'sell_score'] += 1
        
        # 10. Traditional Indicators
        df.loc[df['RSI'] > 50, 'sell_score'] += 1
        df.loc[df['RSI'] > 70, 'sell_score'] += 1
        df.loc[df['MACD_hist'] < 0, 'sell_score'] += 1
        df.loc[df['MACD_hist'] < df['MACD_hist'].shift(1), 'sell_score'] += 1
        df.loc[df['close'] > df['BB_upper'], 'sell_score'] += 2
        df.loc[(df['EMA_fast'] < df['EMA_slow']) & (df['EMA_fast'].shift(1) >= df['EMA_slow'].shift(1)), 'sell_score'] += 2
        
        # === GENERATE FINAL SIGNALS ===
        df['signal'] = 0
        
        # Buy signal: buy_score >= threshold AND buy_score > sell_score
        buy_condition = (
            (df['buy_score'] >= self.min_confluence_score) & 
            (df['buy_score'] > df['sell_score'] + 2)  # Clear advantage
        )
        
        # Sell signal: sell_score >= threshold AND sell_score > buy_score
        sell_condition = (
            (df['sell_score'] >= self.min_confluence_score) & 
            (df['sell_score'] > df['buy_score'] + 2)  # Clear advantage
        )
        
        df.loc[buy_condition, 'signal'] = 1
        df.loc[sell_condition, 'signal'] = -1
        
        return df
    
    def get_signal_strength(self, df):
        """
        Calculate signal strength from 0 to 100 based on confluence
        """
        if len(df) < 2:
            return 0
        
        latest = df.iloc[-1]
        
        # Get the max possible score (approximately 25 points)
        max_score = 25
        
        if latest.get('signal', 0) == 1:
            score = latest.get('buy_score', 0)
        elif latest.get('signal', 0) == -1:
            score = latest.get('sell_score', 0)
        else:
            score = 0
        
        # Convert to 0-100 scale
        strength = min(100, int((score / max_score) * 100))
        
        return strength
    
    def get_signal_breakdown(self, df):
        """
        Get detailed breakdown of what's contributing to the signal
        """
        if len(df) < 2:
            return {}
        
        latest = df.iloc[-1]
        
        breakdown = {
            'market_structure': latest.get('market_structure', 0),
            'in_order_block': bool(
                (latest.get('close', 0) >= latest.get('active_bull_ob_low', float('inf'))) and
                (latest.get('close', 0) <= latest.get('active_bull_ob_high', float('-inf')))
            ) or bool(
                (latest.get('close', 0) >= latest.get('active_bear_ob_low', float('inf'))) and
                (latest.get('close', 0) <= latest.get('active_bear_ob_high', float('-inf')))
            ),
            'has_fvg': bool(latest.get('bullish_fvg', False)) or bool(latest.get('bearish_fvg', False)),
            'liquidity_sweep': bool(latest.get('liquidity_sweep_bull', False)) or bool(latest.get('liquidity_sweep_bear', False)),
            'in_premium_discount': bool(latest.get('premium_zone', False)) or bool(latest.get('discount_zone', False)),
            'in_ote_zone': bool(latest.get('in_ote_zone', False)) or bool(latest.get('in_fib_ote', False)),
            'in_kill_zone': bool(latest.get('kill_zone', False)),
            'high_volume': bool(latest.get('high_volume', False)),
            'delta_positive': bool(latest.get('delta', 0) > 0),
            'fib_confluence': int(latest.get('fib_confluence', 0)),
            'rsi': float(latest.get('RSI', 50)),
            'macd_hist': float(latest.get('MACD_hist', 0)),
            'buy_score': int(latest.get('buy_score', 0)),
            'sell_score': int(latest.get('sell_score', 0)),
        }
        
        return breakdown


# Keep backward compatibility - alias the advanced strategy as ScalpingStrategy
ScalpingStrategy = AdvancedScalpingStrategy


class RiskManager:
    """Manage position sizing and risk parameters"""
    
    def __init__(self, 
                 account_balance=10000,
                 risk_per_trade=0.01,
                 max_positions=1,
                 stop_loss_atr_multiplier=1.5,
                 take_profit_atr_multiplier=2.5):
        
        self.account_balance = account_balance
        self.risk_per_trade = risk_per_trade
        self.max_positions = max_positions
        self.stop_loss_atr_multiplier = stop_loss_atr_multiplier
        self.take_profit_atr_multiplier = take_profit_atr_multiplier
    
    def calculate_position_size(self, atr, price):
        """Calculate position size based on risk and ATR"""
        risk_amount = self.account_balance * self.risk_per_trade
        stop_loss_distance = atr * self.stop_loss_atr_multiplier
        
        position_size = risk_amount / stop_loss_distance
        position_size = round(position_size / 100, 2)
        position_size = max(0.01, min(position_size, 1.0))
        
        return position_size
    
    def calculate_stop_loss(self, entry_price, atr, signal):
        """Calculate stop loss level"""
        if signal == 1:
            return entry_price - (atr * self.stop_loss_atr_multiplier)
        else:
            return entry_price + (atr * self.stop_loss_atr_multiplier)
    
    def calculate_take_profit(self, entry_price, atr, signal):
        """Calculate take profit level"""
        if signal == 1:
            return entry_price + (atr * self.take_profit_atr_multiplier)
        else:
            return entry_price - (atr * self.take_profit_atr_multiplier)


class Backtester:
    """Backtest the scalping strategy"""
    
    def __init__(self, strategy, risk_manager):
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.trades = []
        
    def run(self, df):
        """Run backtest on historical data"""
        df = self.strategy.generate_signals(df)
        
        position = None
        equity_curve = [self.risk_manager.account_balance]
        
        for i in range(len(df)):
            if i < 50:  # Skip warmup period
                continue
            
            current_bar = df.iloc[i]
            
            if position is not None:
                if position['type'] == 'long':
                    if current_bar['low'] <= position['stop_loss']:
                        exit_price = position['stop_loss']
                        pnl = (exit_price - position['entry_price']) * position['size'] * 100
                        self._close_position(position, exit_price, pnl, current_bar['time'], 'Stop Loss')
                        position = None
                    elif current_bar['high'] >= position['take_profit']:
                        exit_price = position['take_profit']
                        pnl = (exit_price - position['entry_price']) * position['size'] * 100
                        self._close_position(position, exit_price, pnl, current_bar['time'], 'Take Profit')
                        position = None
                
                elif position['type'] == 'short':
                    if current_bar['high'] >= position['stop_loss']:
                        exit_price = position['stop_loss']
                        pnl = (position['entry_price'] - exit_price) * position['size'] * 100
                        self._close_position(position, exit_price, pnl, current_bar['time'], 'Stop Loss')
                        position = None
                    elif current_bar['low'] <= position['take_profit']:
                        exit_price = position['take_profit']
                        pnl = (position['entry_price'] - exit_price) * position['size'] * 100
                        self._close_position(position, exit_price, pnl, current_bar['time'], 'Take Profit')
                        position = None
            
            if position is None and current_bar['signal'] != 0:
                atr = current_bar.get('ATR', 1)
                if pd.isna(atr) or atr <= 0:
                    atr = 1
                
                position_size = self.risk_manager.calculate_position_size(
                    atr, current_bar['close']
                )
                
                if current_bar['signal'] == 1:
                    position = {
                        'type': 'long',
                        'entry_price': current_bar['close'],
                        'entry_time': current_bar['time'],
                        'size': position_size,
                        'stop_loss': self.risk_manager.calculate_stop_loss(
                            current_bar['close'], atr, 1
                        ),
                        'take_profit': self.risk_manager.calculate_take_profit(
                            current_bar['close'], atr, 1
                        )
                    }
                
                elif current_bar['signal'] == -1:
                    position = {
                        'type': 'short',
                        'entry_price': current_bar['close'],
                        'entry_time': current_bar['time'],
                        'size': position_size,
                        'stop_loss': self.risk_manager.calculate_stop_loss(
                            current_bar['close'], atr, -1
                        ),
                        'take_profit': self.risk_manager.calculate_take_profit(
                            current_bar['close'], atr, -1
                        )
                    }
            
            current_equity = self.risk_manager.account_balance + sum(t['pnl'] for t in self.trades)
            equity_curve.append(current_equity)
        
        return self._calculate_performance(equity_curve)
    
    def _close_position(self, position, exit_price, pnl, exit_time, reason):
        trade = {
            'entry_time': position['entry_time'],
            'exit_time': exit_time,
            'type': position['type'],
            'entry_price': position['entry_price'],
            'exit_price': exit_price,
            'size': position['size'],
            'pnl': pnl,
            'reason': reason
        }
        self.trades.append(trade)
        self.risk_manager.account_balance += pnl
    
    def _calculate_performance(self, equity_curve):
        if not self.trades:
            return {
                'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0,
                'win_rate': 0, 'total_pnl': 0, 'avg_win': 0, 'avg_loss': 0,
                'max_drawdown': 0, 'sharpe_ratio': 0, 'profit_factor': 0,
                'final_balance': self.risk_manager.account_balance
            }
        
        trades_df = pd.DataFrame(self.trades)
        winning_trades = trades_df[trades_df['pnl'] > 0]
        losing_trades = trades_df[trades_df['pnl'] < 0]
        total_pnl = trades_df['pnl'].sum()
        
        equity_series = pd.Series(equity_curve)
        running_max = equity_series.cummax()
        drawdown = (equity_series - running_max) / running_max
        max_drawdown = drawdown.min() * 100
        
        returns = equity_series.pct_change().dropna()
        sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() != 0 else 0
        
        profit_factor = 0
        if len(losing_trades) > 0 and losing_trades['pnl'].sum() != 0:
            profit_factor = abs(winning_trades['pnl'].sum() / losing_trades['pnl'].sum())
        
        return {
            'total_trades': len(self.trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(self.trades) * 100 if self.trades else 0,
            'total_pnl': total_pnl,
            'avg_win': winning_trades['pnl'].mean() if len(winning_trades) > 0 else 0,
            'avg_loss': losing_trades['pnl'].mean() if len(losing_trades) > 0 else 0,
            'profit_factor': profit_factor,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'final_balance': self.risk_manager.account_balance
        }
    
    def get_trades_dataframe(self):
        return pd.DataFrame(self.trades)


def generate_sample_data(days=30, timeframe='5min'):
    """Generate sample OHLC data for testing"""
    freq_map = {'1min': '1min', '5min': '5min', '15min': '15min', '1h': '1H'}
    freq = freq_map.get(timeframe, '5min')
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    timestamps = pd.date_range(start=start_date, end=end_date, freq=freq)
    
    np.random.seed(42)
    base_price = 2050
    prices = []
    current_price = base_price
    
    for _ in range(len(timestamps)):
        change = np.random.normal(0, 2)
        current_price += change
        prices.append(current_price)
    
    prices = np.array(prices)
    
    data = {
        'time': timestamps,
        'open': prices,
        'high': prices + np.random.uniform(0.5, 3, len(prices)),
        'low': prices - np.random.uniform(0.5, 3, len(prices)),
        'close': prices + np.random.uniform(-1, 1, len(prices)),
        'volume': np.random.randint(100, 1000, len(prices))
    }
    
    df = pd.DataFrame(data)
    df['high'] = df[['open', 'high', 'close']].max(axis=1)
    df['low'] = df[['open', 'low', 'close']].min(axis=1)
    
    return df


def main():
    print("=" * 70)
    print("ADVANCED MULTI-STRATEGY SCALPING ROBOT")
    print("SMC + ICT + Order Flow + Fibonacci Confluence")
    print("=" * 70)
    print()
    
    print("Loading market data...")
    df = generate_sample_data(days=30, timeframe='5min')
    print(f"Loaded {len(df)} bars of 5-minute data")
    print(f"Period: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    print()
    
    print("Initializing Advanced Strategy...")
    strategy = AdvancedScalpingStrategy(min_confluence_score=5)
    print("Components: SMC, ICT, Order Flow, Fibonacci")
    print()
    
    print("Setting up risk management...")
    risk_manager = RiskManager(
        account_balance=10000,
        risk_per_trade=0.01,
        stop_loss_atr_multiplier=1.5,
        take_profit_atr_multiplier=2.5
    )
    print(f"Initial Balance: ${risk_manager.account_balance:,.2f}")
    print(f"Risk per Trade: {risk_manager.risk_per_trade * 100}%")
    print()
    
    print("Running backtest...")
    backtester = Backtester(strategy, risk_manager)
    results = backtester.run(df)
    print()
    
    print("=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    print(f"Total Trades:        {results['total_trades']}")
    print(f"Winning Trades:      {results['winning_trades']}")
    print(f"Losing Trades:       {results['losing_trades']}")
    print(f"Win Rate:            {results['win_rate']:.2f}%")
    print(f"Total P&L:           ${results['total_pnl']:,.2f}")
    print(f"Average Win:         ${results['avg_win']:,.2f}")
    print(f"Average Loss:        ${results['avg_loss']:,.2f}")
    print(f"Profit Factor:       {results['profit_factor']:.2f}")
    print(f"Max Drawdown:        {results['max_drawdown']:.2f}%")
    print(f"Sharpe Ratio:        {results['sharpe_ratio']:.2f}")
    print(f"Final Balance:       ${results['final_balance']:,.2f}")
    print(f"Return:              {((results['final_balance'] / 10000 - 1) * 100):.2f}%")
    print("=" * 70)
    
    if results['total_trades'] > 0:
        print("\nSample Trades (First 5):")
        print("-" * 70)
        trades_df = backtester.get_trades_dataframe()
        print(trades_df.head(5).to_string(index=False))
    
    print("\nBacktest complete!")


if __name__ == "__main__":
    main()
