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

    @staticmethod
    def adx(high, low, close, period=14):
        """Average Directional Index with +/-DI."""
        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()

        plus_dm_s = pd.Series(plus_dm, index=high.index).rolling(window=period).mean()
        minus_dm_s = pd.Series(minus_dm, index=high.index).rolling(window=period).mean()

        plus_di = 100 * (plus_dm_s / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm_s / atr.replace(0, np.nan))

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.rolling(window=period).mean()

        return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)


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
        Enhanced Liquidity Concepts - Full ICT/SMC Implementation:
        1. Liquidity Sweeps (Stop Hunts): Price spikes through swing level then reverses
        2. Buyside Liquidity (BSL): Stops clustered above equal highs / swing highs
        3. Sellside Liquidity (SSL): Stops clustered below equal lows / swing lows
        4. Previous Day High/Low (PDH/PDL): Strong daily liquidity pools
        5. Inducement: Small fake move before the real sweep
        6. Liquidity Run: Price is actively targeting a pool
        """
        df = df.copy()

        # --- ATR for threshold scaling ---
        if 'ATR' in df.columns:
            atr = df['ATR']
        else:
            atr = TechnicalIndicators.atr(df['high'], df['low'], df['close'], 14)

        # ── 1. BASIC SWEEP detection (wicked past swing, closed back) ──
        df['liquidity_sweep_bull'] = False
        df['liquidity_sweep_bear'] = False
        df['sweep_strength_bull'] = 0.0
        df['sweep_strength_bear'] = 0.0

        for i in range(2, len(df)):
            swing_low  = df['last_swing_low'].iloc[i - 1]
            swing_high = df['last_swing_high'].iloc[i - 1]
            atr_val    = atr.iloc[i] if not pd.isna(atr.iloc[i]) else 1.0

            # Bullish sweep: wick below swing low, close ABOVE it
            if (df['low'].iloc[i] < swing_low and
                    df['close'].iloc[i] > swing_low):
                df.loc[df.index[i], 'liquidity_sweep_bull'] = True
                # How deep was the sweep relative to ATR?
                depth = (swing_low - df['low'].iloc[i]) / atr_val
                df.loc[df.index[i], 'sweep_strength_bull'] = round(depth, 3)

            # Bearish sweep: wick above swing high, close BELOW it
            if (df['high'].iloc[i] > swing_high and
                    df['close'].iloc[i] < swing_high):
                df.loc[df.index[i], 'liquidity_sweep_bear'] = True
                depth = (df['high'].iloc[i] - swing_high) / atr_val
                df.loc[df.index[i], 'sweep_strength_bear'] = round(depth, 3)

        # ── 2. EQUAL HIGHS / EQUAL LOWS (Buyside / Sellside Liquidity pools) ──
        # Equal highs = buyside liquidity (retail buy-stop cluster above)
        # Equal lows  = sellside liquidity (retail sell-stop cluster below)
        tolerance = 0.0015   # 0.15% tolerance — catches more equal levels
        pool_lookback = 6    # Compare within 6-bar window

        df['buyside_liquidity']  = False   # equal highs → BSL pool present
        df['sellside_liquidity'] = False   # equal lows  → SSL pool present
        df['bsl_level'] = np.nan
        df['ssl_level'] = np.nan

        for i in range(pool_lookback, len(df)):
            window_highs = df['high'].iloc[i - pool_lookback: i]
            window_lows  = df['low'].iloc[i - pool_lookback: i]
            h_mean = window_highs.mean()
            l_mean = window_lows.mean()

            if h_mean > 0:
                high_range_pct = (window_highs.max() - window_highs.min()) / h_mean
                if high_range_pct < tolerance:
                    df.loc[df.index[i], 'buyside_liquidity'] = True
                    df.loc[df.index[i], 'bsl_level'] = window_highs.max()

            if l_mean > 0:
                low_range_pct = (window_lows.max() - window_lows.min()) / l_mean
                if low_range_pct < tolerance:
                    df.loc[df.index[i], 'sellside_liquidity'] = True
                    df.loc[df.index[i], 'ssl_level'] = window_lows.min()

        # Forward-fill pool levels so they remain visible after formation
        df['bsl_level'] = df['bsl_level'].ffill()
        df['ssl_level'] = df['ssl_level'].ffill()

        # ── 3. PREVIOUS DAY HIGH / LOW (PDH / PDL) ──
        if 'time' in df.columns:
            df['_date'] = pd.to_datetime(df['time']).dt.date
            daily_high = df.groupby('_date')['high'].transform('max')
            daily_low  = df.groupby('_date')['low'].transform('min')
            # Shift by one day → yesterday's high/low
            df['PDH'] = daily_high.groupby(df['_date']).transform('first').shift(1).ffill()
            df['PDL'] = daily_low.groupby(df['_date']).transform('first').shift(1).ffill()
            df.drop(columns=['_date'], inplace=True)

            atr_val_s = atr.ffill().fillna(1.0)
            # Price touching PDH/PDL within 0.5 ATR
            df['price_at_pdh'] = abs(df['close'] - df['PDH']) < atr_val_s * 0.5
            df['price_at_pdl'] = abs(df['close'] - df['PDL']) < atr_val_s * 0.5
        else:
            df['PDH'] = np.nan
            df['PDL'] = np.nan
            df['price_at_pdh'] = False
            df['price_at_pdl'] = False

        # ── 4. INDUCEMENT detection ──
        # Inducement = small pullback (< 0.3 ATR) that forms a minor swing, 
        # before the real liquidity grab runs the other way.
        df['inducement_bull'] = False   # minor low that will be swept for longs
        df['inducement_bear'] = False   # minor high that will be swept for shorts

        for i in range(3, len(df)):
            atr_v = atr.iloc[i] if not pd.isna(atr.iloc[i]) else 1.0
            # Inducement bull: tiny pullback down (< 0.3 ATR) in an uptrend
            pullback = df['low'].iloc[i - 1] - df['low'].iloc[i - 2]
            if (df.get('market_structure', pd.Series(0, index=df.index)).iloc[i] == 1 and
                    0 < pullback < atr_v * 0.3):
                df.loc[df.index[i], 'inducement_bull'] = True

            # Inducement bear: tiny pullback up (< 0.3 ATR) in a downtrend
            pullback = df['high'].iloc[i - 2] - df['high'].iloc[i - 1]
            if (df.get('market_structure', pd.Series(0, index=df.index)).iloc[i] == -1 and
                    0 < pullback < atr_v * 0.3):
                df.loc[df.index[i], 'inducement_bear'] = True

        # ── 5. LIQUIDITY RUN — price actively targeting nearest pool ──
        df['liq_run_bull'] = False   # price heading toward BSL above
        df['liq_run_bear'] = False   # price heading toward SSL below

        momentum_bars = 3
        for i in range(momentum_bars, len(df)):
            atr_v = atr.iloc[i] if not pd.isna(atr.iloc[i]) else 1.0
            price_move = df['close'].iloc[i] - df['close'].iloc[i - momentum_bars]

            bsl = df['bsl_level'].iloc[i]
            ssl = df['ssl_level'].iloc[i]

            # Bullish run: strong consecutive up-move toward BSL
            if (not pd.isna(bsl) and
                    price_move > atr_v * 0.5 and
                    df['close'].iloc[i] < bsl):
                df.loc[df.index[i], 'liq_run_bull'] = True

            # Bearish run: strong consecutive down-move toward SSL
            if (not pd.isna(ssl) and
                    price_move < -atr_v * 0.5 and
                    df['close'].iloc[i] > ssl):
                df.loc[df.index[i], 'liq_run_bear'] = True

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
        Identify liquidity pool zones for ICT:
        - Buyside Liquidity (BSL): equal highs, prior swing highs
        - Sellside Liquidity (SSL): equal lows, prior swing lows
        - Pool proximity: is price near a pool right now?
        These were already computed in SmartMoneyConcepts.detect_liquidity_sweeps
        but we add proximity signals here.
        """
        df = df.copy()

        # Map SmartMoney outputs into simpler ICT names
        df['equal_highs'] = df.get('buyside_liquidity', pd.Series(False, index=df.index))
        df['equal_lows']  = df.get('sellside_liquidity', pd.Series(False, index=df.index))

        # Proximity: price within 0.2% of BSL or SSL level
        if 'bsl_level' in df.columns and 'ssl_level' in df.columns:
            bsl = df['bsl_level'].fillna(df['close'] * 999)
            ssl = df['ssl_level'].fillna(0)
            df['near_bsl'] = ((bsl - df['close']) / df['close']).abs() < 0.002
            df['near_ssl'] = ((df['close'] - ssl) / df['close']).abs() < 0.002
        else:
            df['near_bsl'] = False
            df['near_ssl'] = False

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
    Regime-filtered 5-minute trend strategy.

    The goal is to avoid overtrading and only take continuation setups where:
    1) trend is clear (EMA trend + ADX),
    2) price pulls back to value,
    3) momentum confirms re-acceleration,
    4) structure/liquidity context supports the move.
    """
    
    def __init__(self, ema_fast=9, ema_slow=21, rsi_period=14,
                 rsi_oversold=30, rsi_overbought=70, bb_period=20, bb_std=2,
                 min_confluence_score=7,
                 trend_ema_fast=50, trend_ema_slow=200,
                 adx_period=14, min_adx=18,
                 pullback_atr_mult=0.35,
                 momentum_lookback=3,
                 require_kill_zone=True,
                 volume_confirm_mult=1.1,
                 **kwargs):
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
        
        # Regime filter parameters
        self.trend_ema_fast = trend_ema_fast
        self.trend_ema_slow = trend_ema_slow
        self.adx_period = adx_period
        self.min_adx = min_adx
        self.pullback_atr_mult = pullback_atr_mult
        self.momentum_lookback = max(2, int(momentum_lookback))
        self.require_kill_zone = bool(require_kill_zone)
        self.volume_confirm_mult = volume_confirm_mult

        # Signal scoring threshold
        self.min_confluence_score = min_confluence_score
        self.max_score_reference = 14
    
    def generate_signals(self, df):
        """
        Generate M5 signals using regime + pullback + momentum confirmation.
        """
        df = df.copy()
        
        # === TRADITIONAL INDICATORS ===
        df['EMA_fast'] = self.ti.ema(df['close'], self.ema_fast)
        df['EMA_slow'] = self.ti.ema(df['close'], self.ema_slow)
        df['EMA_trend_fast'] = self.ti.ema(df['close'], self.trend_ema_fast)
        df['EMA_trend_slow'] = self.ti.ema(df['close'], self.trend_ema_slow)
        df['RSI'] = self.ti.rsi(df['close'], self.rsi_period)
        df['BB_upper'], df['BB_middle'], df['BB_lower'] = self.ti.bollinger_bands(
            df['close'], self.bb_period, self.bb_std
        )
        df['MACD'], df['MACD_signal'], df['MACD_hist'] = self.ti.macd(df['close'])
        df['ATR'] = self.ti.atr(df['high'], df['low'], df['close'])
        df['ADX'], df['DI_plus'], df['DI_minus'] = self.ti.adx(
            df['high'], df['low'], df['close'], self.adx_period
        )
        
        # Trend detection
        df['trend'] = np.where(df['EMA_trend_fast'] > df['EMA_trend_slow'], 1, -1)
        
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
        
        # === REGIME + PULLBACK LOGIC ===
        df['buy_score'] = 0
        df['sell_score'] = 0

        default_false = pd.Series(False, index=df.index)
        default_true = pd.Series(True, index=df.index)
        atr = df['ATR'].replace(0, np.nan).ffill().bfill().fillna(1.0)

        trend_up = (df['EMA_trend_fast'] > df['EMA_trend_slow']) & (df['close'] > df['EMA_trend_slow'])
        trend_down = (df['EMA_trend_fast'] < df['EMA_trend_slow']) & (df['close'] < df['EMA_trend_slow'])

        adx_ok = df['ADX'] >= self.min_adx
        di_up = df['DI_plus'] > df['DI_minus']
        di_down = df['DI_minus'] > df['DI_plus']

        pullback_up = (
            (df['low'] <= df['EMA_fast']) &
            (df['close'] >= (df['EMA_fast'] - atr * self.pullback_atr_mult))
        )
        pullback_down = (
            (df['high'] >= df['EMA_fast']) &
            (df['close'] <= (df['EMA_fast'] + atr * self.pullback_atr_mult))
        )

        recent_high = df['high'].shift(1).rolling(self.momentum_lookback).max()
        recent_low = df['low'].shift(1).rolling(self.momentum_lookback).min()

        bull_break = df['close'] > recent_high
        bear_break = df['close'] < recent_low

        rsi_bull = df['RSI'].between(45, 70)
        rsi_bear = df['RSI'].between(30, 55)
        macd_bull = (df['MACD_hist'] > 0) & (df['MACD_hist'] >= df['MACD_hist'].shift(1))
        macd_bear = (df['MACD_hist'] < 0) & (df['MACD_hist'] <= df['MACD_hist'].shift(1))

        # Require at least 2 of 3 momentum conditions (not all 3 simultaneously).
        _mom_up_count   = bull_break.astype(int) + rsi_bull.astype(int) + macd_bull.astype(int)
        _mom_down_count = bear_break.astype(int) + rsi_bear.astype(int) + macd_bear.astype(int)
        momentum_up   = _mom_up_count >= 2
        momentum_down = _mom_down_count >= 2

        structure_up = (
            df['choch_bullish'] |
            df['bos_bullish'] |
            df['liquidity_sweep_bull'] |
            df.get('liq_run_bull', default_false)
        )
        structure_down = (
            df['choch_bearish'] |
            df['bos_bearish'] |
            df['liquidity_sweep_bear'] |
            df.get('liq_run_bear', default_false)
        )

        structure_up_recent = structure_up.rolling(5, min_periods=1).max().astype(bool)
        structure_down_recent = structure_down.rolling(5, min_periods=1).max().astype(bool)

        in_bull_ob = (
            (df['close'] >= df['active_bull_ob_low']) &
            (df['close'] <= df['active_bull_ob_high'])
        )
        in_bear_ob = (
            (df['close'] >= df['active_bear_ob_low']) &
            (df['close'] <= df['active_bear_ob_high'])
        )

        ote_buy = df.get('ote_buy_zone', default_false) | df.get('in_fib_ote', default_false)
        ote_sell = df.get('ote_sell_zone', default_false) | df.get('in_fib_ote', default_false)

        liquidity_up = (
            in_bull_ob |
            df.get('discount_zone', default_false) |
            df.get('near_ssl', default_false) |
            df.get('price_at_pdl', default_false)
        )
        liquidity_down = (
            in_bear_ob |
            df.get('premium_zone', default_false) |
            df.get('near_bsl', default_false) |
            df.get('price_at_pdh', default_false)
        )

        volume_ready = df['volume'] >= (df['vol_sma'].fillna(df['volume']) * self.volume_confirm_mult)
        flow_up = ((df['delta'] > 0) & volume_ready) | df['bullish_absorption']
        flow_down = ((df['delta'] < 0) & volume_ready) | df['bearish_absorption']

        kill_zone = df.get('kill_zone', default_true).fillna(False)

        # Scoring (max ~= 14 points)
        df.loc[trend_up, 'buy_score'] += 2
        df.loc[trend_down, 'sell_score'] += 2

        df.loc[adx_ok & di_up, 'buy_score'] += 2
        df.loc[adx_ok & di_down, 'sell_score'] += 2

        df.loc[pullback_up, 'buy_score'] += 2
        df.loc[pullback_down, 'sell_score'] += 2

        df.loc[momentum_up, 'buy_score'] += 2
        df.loc[momentum_down, 'sell_score'] += 2

        df.loc[structure_up_recent, 'buy_score'] += 2
        df.loc[structure_down_recent, 'sell_score'] += 2

        df.loc[liquidity_up, 'buy_score'] += 1
        df.loc[liquidity_down, 'sell_score'] += 1

        df.loc[flow_up, 'buy_score'] += 1
        df.loc[flow_down, 'sell_score'] += 1

        df.loc[ote_buy, 'buy_score'] += 1
        df.loc[ote_sell, 'sell_score'] += 1

        df.loc[kill_zone, 'buy_score'] += 1
        df.loc[kill_zone, 'sell_score'] += 1

        # Hard gate: only the two broadest structural prerequisites must be true.
        # Everything else (pullback, momentum, structure, session) contributes to
        # the score and is filtered by min_confluence_score — this keeps signal
        # rate realistic (~3-8 per day) while still requiring directional trend.
        buy_gate = trend_up & adx_ok
        sell_gate = trend_down & adx_ok

        # Optionally restrict to kill-zone hours (score bonus still applied above).
        if self.require_kill_zone:
            buy_gate = buy_gate & kill_zone
            sell_gate = sell_gate & kill_zone
        
        # === GENERATE FINAL SIGNALS ===
        df['signal'] = 0
        
        buy_condition = (
            buy_gate &
            (df['buy_score'] >= self.min_confluence_score) &
            (df['buy_score'] >= df['sell_score'] + 1)
        )
        
        sell_condition = (
            sell_gate &
            (df['sell_score'] >= self.min_confluence_score) &
            (df['sell_score'] >= df['buy_score'] + 1)
        )
        
        df['buy_score'] = df['buy_score'].fillna(0).astype(int)
        df['sell_score'] = df['sell_score'].fillna(0).astype(int)

        df.loc[buy_condition, 'signal'] = 1
        df.loc[sell_condition, 'signal'] = -1

        # Keep explicit flags for debug/dashboard visibility.
        df['trend_up'] = trend_up
        df['trend_down'] = trend_down
        df['pullback_up'] = pullback_up
        df['pullback_down'] = pullback_down
        df['momentum_up'] = momentum_up
        df['momentum_down'] = momentum_down
        
        return df
    
    def get_signal_strength(self, df):
        """
        Calculate signal strength from 0 to 100 based on confluence
        """
        if len(df) < 2:
            return 0
        
        latest = df.iloc[-1]
        
        max_score = self.max_score_reference
        
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
        prev = df.iloc[-2] if len(df) > 2 else latest
        
        breakdown = {
            # SMC
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
            # Enhanced liquidity
            'buyside_liquidity':  bool(latest.get('buyside_liquidity', False)),
            'sellside_liquidity': bool(latest.get('sellside_liquidity', False)),
            'near_bsl':           bool(latest.get('near_bsl', False)),
            'near_ssl':           bool(latest.get('near_ssl', False)),
            'price_at_pdh':       bool(latest.get('price_at_pdh', False)),
            'price_at_pdl':       bool(latest.get('price_at_pdl', False)),
            'liq_run_bull':       bool(latest.get('liq_run_bull', False)),
            'liq_run_bear':       bool(latest.get('liq_run_bear', False)),
            'inducement_bull':    bool(latest.get('inducement_bull', False)),
            'inducement_bear':    bool(latest.get('inducement_bear', False)),
            'sweep_strength':     float(max(latest.get('sweep_strength_bull', 0), latest.get('sweep_strength_bear', 0))),
            
            # ICT
            'in_premium_discount': bool(latest.get('premium_zone', False)) or bool(latest.get('discount_zone', False)),
            'in_ote_zone': bool(latest.get('in_ote_zone', False)) or bool(latest.get('in_fib_ote', False)),
            'in_kill_zone': bool(latest.get('kill_zone', False)),

            # Regime filters
            'adx': float(latest.get('ADX', 0)),
            'di_plus': float(latest.get('DI_plus', 0)),
            'di_minus': float(latest.get('DI_minus', 0)),
            'trend_up': bool(latest.get('trend_up', False)),
            'trend_down': bool(latest.get('trend_down', False)),
            'pullback_ready': bool(latest.get('pullback_up', False)) or bool(latest.get('pullback_down', False)),
            'momentum_break': bool(latest.get('momentum_up', False)) or bool(latest.get('momentum_down', False)),
            'session_ok': bool(latest.get('kill_zone', True)) if self.require_kill_zone else True,
            
            # Order Flow
            'high_volume': bool(latest.get('high_volume', False)),
            'delta_positive': bool(latest.get('delta', 0) > 0),
            
            # Fibonacci
            'fib_confluence': int(latest.get('fib_confluence', 0)),
            
            # Traditional Indicators (ORIGINAL)
            'rsi': float(latest.get('RSI', 50)),
            'rsi_oversold': bool(latest.get('RSI', 50) < 35),
            'rsi_overbought': bool(latest.get('RSI', 50) > 65),
            
            'macd_hist': float(latest.get('MACD_hist', 0)),
            'macd_bullish': bool(latest.get('MACD_hist', 0) > 0),
            'macd_crossover': bool(
                (latest.get('MACD', 0) > latest.get('MACD_signal', 0)) and
                (prev.get('MACD', 0) <= prev.get('MACD_signal', 0))
            ) or bool(
                (latest.get('MACD', 0) < latest.get('MACD_signal', 0)) and
                (prev.get('MACD', 0) >= prev.get('MACD_signal', 0))
            ),
            
            'ema_bullish': bool(latest.get('EMA_fast', 0) > latest.get('EMA_slow', 0)),
            'ema_crossover': bool(
                (latest.get('EMA_fast', 0) > latest.get('EMA_slow', 0)) and
                (prev.get('EMA_fast', 0) <= prev.get('EMA_slow', 0))
            ) or bool(
                (latest.get('EMA_fast', 0) < latest.get('EMA_slow', 0)) and
                (prev.get('EMA_fast', 0) >= prev.get('EMA_slow', 0))
            ),
            
            'bb_lower_touch': bool(latest.get('close', 0) < latest.get('BB_lower', 0)),
            'bb_upper_touch': bool(latest.get('close', 0) > latest.get('BB_upper', 0)),
            
            # Scores
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
                 max_positions_per_symbol=1,
                 stop_loss_atr_multiplier=1.5,
                 take_profit_atr_multiplier=2.5,
                 **kwargs):  # Accept extra kwargs to avoid errors
        
        self.account_balance = account_balance
        self.risk_per_trade = risk_per_trade
        self.max_positions = max_positions
        self.max_positions_per_symbol = max_positions_per_symbol
        self.stop_loss_atr_multiplier = stop_loss_atr_multiplier
        self.take_profit_atr_multiplier = take_profit_atr_multiplier
    
    def get_lot_size_range(self):
        """Get lot size range based on account balance"""
        balance = self.account_balance
        
        if balance >= 10000:
            # $10,000+: 1.0 lots
            return (1.0, 1.0, 10)
        elif balance >= 1000:
            # $1,000 - $9,999: 0.04 to 0.09 lots
            return (0.04, 0.09, 10)
        else:
            # $100 - $999: 0.01 to 0.03 lots, max 5 trades
            return (0.01, 0.03, 5)
    
    def calculate_position_size(self, atr, price):
        """Calculate position size based on account balance tiers"""
        min_lot, max_lot, _ = self.get_lot_size_range()
        
        # Scale within the range based on balance within tier
        balance = self.account_balance
        
        if balance >= 10000:
            # Fixed 1.0 lot for 10k+
            position_size = 1.0
        elif balance >= 1000:
            # Scale 0.04-0.09 based on balance (1000-9999)
            scale = (balance - 1000) / 9000  # 0 to 1
            position_size = min_lot + (max_lot - min_lot) * scale
        else:
            # Scale 0.01-0.03 based on balance (100-999)
            scale = (balance - 100) / 900  # 0 to 1
            position_size = min_lot + (max_lot - min_lot) * scale
        
        # Round to 2 decimals and clamp
        position_size = round(position_size, 2)
        position_size = max(min_lot, min(position_size, max_lot))
        
        return position_size
    
    def get_max_positions(self):
        """Get max positions based on account balance"""
        _, _, max_pos = self.get_lot_size_range()
        return max_pos
    
    def calculate_stop_loss(self, entry_price, atr, signal, signal_bar=None):
        """
        Structural Stop Loss with SL obfuscation to avoid predictable stop-hunting.

        Problems with the old approach (fixed 0.10 ATR buffer, all candidates stacked):
          - Every SMC bot places stops at the *same* tick → trivially huntable
          - Stacking swing + SSL + PDL candidates creates a "stop cluster zone"
          - Fixed buffer means stops at round .00/.50/.25 levels (prime hunt targets)

        This version uses THREE layers of unpredictability:
          1. PRIORITY-based selection (not multi-candidate): sweep wick first,
             then OB, then swing. Once a level is found, stop there — no stacking.
          2. DYNAMIC buffer (0.20–0.45 ATR): varies by entry price's decimal
             so each trade's stop lands at a different offset from the level.
          3. ROUND-NUMBER NUDGE: if SL falls near .00/.25/.50/.75, shift it
             further away by 0.18 ATR — avoids the highest-density hunt zones.

        BUY SL (below entry): sweep wick → OB low → swing low → SSL → PDL → ATR
        SELL SL (above entry): sweep wick → OB high → swing high → BSL → PDH → ATR
        → Hard cap at 3×ATR.
        """
        # Dynamic buffer: varies 0.20–0.45 ATR based on entry price's sub-unit
        # fraction. Different per price level → stops never cluster at same tick.
        _frac = entry_price % 1.0           # 0.0 – 1.0
        buffer = atr * (0.20 + _frac * 0.25)   # 0.20 – 0.45 ATR
        max_dist     = atr * 3.0
        fallback_dist = atr * self.stop_loss_atr_multiplier

        def _nan(val):
            """Return None if val is None, NaN, or non-finite."""
            try:
                return None if (val is None or (isinstance(val, float) and not np.isfinite(val))) else float(val)
            except Exception:
                return None

        def _avoid_round_number(sl_price, direction, atr_val):
            """Nudge SL away from .00/.25/.50/.75 — prime stop-hunt targets."""
            nudge = atr_val * 0.18
            frac  = sl_price % 1.0
            for lvl in (0.0, 0.25, 0.50, 0.75, 1.0):
                if abs(frac - lvl) < 0.12:
                    return sl_price - nudge if direction == 'buy' else sl_price + nudge
            return sl_price

        if signal_bar is not None:
            sb = signal_bar

            if signal == 1:  # ── BUY ──
                sl = None
                # 1. CRT sweep low (preferred when CRT strategy is active)
                crt_sw_low = _nan(sb.get('crt_sw_low'))
                if crt_sw_low is not None and crt_sw_low < entry_price:
                    sl = crt_sw_low - buffer
                # 2. Sweep candle's wick low — the actual manipulation point
                if sl is None and sb.get('liquidity_sweep_bull', False):
                    low = _nan(sb.get('low'))
                    if low is not None and low < entry_price:
                        sl = low - buffer
                # 3. Bullish OB low (only if no sweep triggered)
                if sl is None:
                    ob_low = _nan(sb.get('active_bull_ob_low'))
                    if ob_low is not None and ob_low < entry_price:
                        sl = ob_low - buffer
                # 4. Structural swing low
                if sl is None:
                    sw_low = _nan(sb.get('last_swing_low'))
                    if sw_low is not None and sw_low < entry_price:
                        sl = sw_low - buffer
                # 5. Sell-Side Liquidity level
                if sl is None:
                    ssl = _nan(sb.get('ssl_level'))
                    if ssl is not None and ssl < entry_price:
                        sl = ssl - buffer
                # 6. Previous Day Low
                if sl is None:
                    pdl = _nan(sb.get('PDL'))
                    if pdl is not None and pdl < entry_price:
                        sl = pdl - buffer

                if sl is not None and (entry_price - sl) <= max_dist:
                    return _avoid_round_number(sl, 'buy', atr)

            else:  # ── SELL ──
                sl = None
                # 1. CRT sweep high (preferred when CRT strategy is active)
                crt_sw_high = _nan(sb.get('crt_sw_high'))
                if crt_sw_high is not None and crt_sw_high > entry_price:
                    sl = crt_sw_high + buffer
                # 2. Sweep candle's wick high
                if sl is None and sb.get('liquidity_sweep_bear', False):
                    high = _nan(sb.get('high'))
                    if high is not None and high > entry_price:
                        sl = high + buffer
                # 3. Bearish OB high
                if sl is None:
                    ob_high = _nan(sb.get('active_bear_ob_high'))
                    if ob_high is not None and ob_high > entry_price:
                        sl = ob_high + buffer
                # 4. Structural swing high
                if sl is None:
                    sw_high = _nan(sb.get('last_swing_high'))
                    if sw_high is not None and sw_high > entry_price:
                        sl = sw_high + buffer
                # 5. Buy-Side Liquidity level
                if sl is None:
                    bsl = _nan(sb.get('bsl_level'))
                    if bsl is not None and bsl > entry_price:
                        sl = bsl + buffer
                # 6. Previous Day High
                if sl is None:
                    pdh = _nan(sb.get('PDH'))
                    if pdh is not None and pdh > entry_price:
                        sl = pdh + buffer

                if sl is not None and (sl - entry_price) <= max_dist:
                    return _avoid_round_number(sl, 'sell', atr)

        # ── Fallback: pure ATR multiplier ──
        if signal == 1:
            return entry_price - fallback_dist
        else:
            return entry_price + fallback_dist

    def calculate_take_profit(self, entry_price, atr, signal, signal_bar=None, sl=None):
        """
        Structural Take Profit — targets the next liquidity pool in the trade direction.

        BUY TP candidates (above entry):
          1. Buy-Side Liquidity (BSL) pool  ← where buy-stops are clustered
          2. Previous Day High (PDH)         ← major daily liquidity
          3. Last structural swing high       ← nearest resistance
          4. Bearish Order Block low          ← bottom of nearest supply zone
        → Minimum 1.5 : 1 Risk-Reward enforced.

        SELL TP is the mirror image below entry.
        """
        # Derive risk distance for RR enforcement
        if sl is not None:
            risk_dist = abs(entry_price - sl)
        else:
            risk_dist = atr * self.stop_loss_atr_multiplier
        min_tp_dist   = risk_dist * 1.5          # at least 1.5 : 1 RR
        fallback_dist = max(atr * self.take_profit_atr_multiplier, min_tp_dist)

        def _nan(val):
            try:
                return None if (val is None or (isinstance(val, float) and not np.isfinite(val))) else float(val)
            except Exception:
                return None

        if signal_bar is not None:
            sb = signal_bar

            if signal == 1:  # ── BUY TP — hunt the liquidity above ──
                candidates = [
                    _nan(sb.get('crt_ref_high')),      # CRT reference high
                    _nan(sb.get('bsl_level')),          # buyside liquidity pool
                    _nan(sb.get('PDH')),                # previous day high
                    _nan(sb.get('last_swing_high')),    # nearest structural high
                    _nan(sb.get('active_bear_ob_low')), # bottom of nearest bearish OB
                ]
                valid = [c for c in candidates
                         if c is not None
                         and c > entry_price
                         and (c - entry_price) >= min_tp_dist]
                if valid:
                    return min(valid)   # nearest qualifying target

            else:  # ── SELL TP — hunt the liquidity below ──
                candidates = [
                    _nan(sb.get('crt_ref_low')),       # CRT reference low
                    _nan(sb.get('ssl_level')),          # sellside liquidity pool
                    _nan(sb.get('PDL')),                # previous day low
                    _nan(sb.get('last_swing_low')),     # nearest structural low
                    _nan(sb.get('active_bull_ob_high')),# top of nearest bullish OB
                ]
                valid = [c for c in candidates
                         if c is not None
                         and c < entry_price
                         and (entry_price - c) >= min_tp_dist]
                if valid:
                    return max(valid)   # nearest qualifying target

        # ── Fallback: ATR multiplier (ensures min RR is still met) ──
        if signal == 1:
            return entry_price + fallback_dist
        else:
            return entry_price - fallback_dist


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
    print("Regime-Filtered Trend Pullback (M5)")
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


# ════════════════════════════════════════════════════════════════════════════
#  GoldM1Strategy — Institutional-grade top-down 1-minute Gold scalper
#
#  FRAMEWORK (strict top-down ICT/SMC):
#
#  HARD GATES (all must pass — zero soft scoring):
#    1. HTF aligned: H4 + H1 EMAs must BOTH point in trade direction
#    2. Structural setup: CHoCH or liquidity sweep in that direction (last 50 bars)
#    3. Zone: price must be INSIDE a demand zone (buy) or supply zone (sell)
#       Zone = OB range ± 0.5 ATR, OR bullish/bearish FVG, OR BB < 0.25 (buy) / > 0.75 (sell)
#    4. Confirmation candle: last closed bar must close in trade direction (no doji)
#
#  SOFT SCORE — entry quality filter (optional, max ~13 pts):
#    OTE fib zone         +2      Liquidity sweep (last 5 bars) +2
#    Stoch RSI cross      +2      MACD aligned                  +1
#    RSI in buy/sell zone +1      Supertrend aligned            +1
#    Bullish/bearish abs. +2      Kill zone (LN/NY open)        +1
#    Volume surge bar     +1
#  Min soft score = 4 (default). Prevents low-quality zone entries.
#
#  SL: placed just below/above the swept swing low/high
#  TP: structural target (BSL for buys / SSL for sells) or 2.5×SL distance
# ════════════════════════════════════════════════════════════════════════════

class GoldM1Strategy:
    """
    Institutional-grade Gold M1 scalper using strict top-down ICT/SMC logic.

    Three hard gates MUST all pass before a trade is considered:
      1. HTF alignment (H4 + H1 EMAs both in trade direction)
      2. Structural setup (CHoCH or sweep in direction within last 50 bars)
      3. Price inside demand (buy) or supply (sell) zone

    Then a soft-score from momentum/confirmation layers filters quality.
    signal = +1 (BUY) / -1 (SELL) / 0 (no trade)
    """

    def __init__(
        self,
        htf_h4_period: int     = 240,
        htf_h1_period: int     = 60,
        ema_fast: int          = 8,
        ema_mid:  int          = 21,
        ema_slow: int          = 50,
        atr_period: int        = 14,
        supertrend_mult: float = 2.5,
        stoch_k: int           = 14,
        stoch_d: int           = 3,
        stoch_smooth: int      = 3,
        rsi_period: int        = 14,
        bb_period: int         = 20,
        bb_std: float          = 2.0,
        swing_lookback: int    = 10,
        fvg_threshold: float   = 0.3,
        setup_lookback: int    = 50,   # bars to look back for CHoCH/sweep setup
        min_score: int         = 4,    # soft confirmation score (max ~13)
    ):
        self.htf_h4_period   = htf_h4_period
        self.htf_h1_period   = htf_h1_period
        self.ema_fast        = ema_fast
        self.ema_mid         = ema_mid
        self.ema_slow        = ema_slow
        self.atr_period      = atr_period
        self.supertrend_mult = supertrend_mult
        self.stoch_k         = stoch_k
        self.stoch_d         = stoch_d
        self.stoch_smooth    = stoch_smooth
        self.rsi_period      = rsi_period
        self.bb_period       = bb_period
        self.bb_std          = bb_std
        self.setup_lookback  = setup_lookback
        self.min_score       = min_score

        self._smc = SmartMoneyConcepts(swing_lookback=swing_lookback,
                                       fvg_threshold=fvg_threshold)
        self._ict = ICTConcepts()
        self._of  = OrderFlowAnalysis()
        self._fib = FibonacciAnalysis()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _atr(high, low, close, period=14):
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _rsi(close, period=14):
        d = close.diff()
        g = d.clip(lower=0).rolling(period).mean()
        l = (-d.clip(upper=0)).rolling(period).mean()
        return 100 - (100 / (1 + g / l.replace(0, np.nan)))

    @staticmethod
    def _stoch_rsi(close, rsi_period=14, stoch_k=14, smooth_k=3, smooth_d=3):
        d   = close.diff()
        g   = d.clip(lower=0).rolling(rsi_period).mean()
        l_  = (-d.clip(upper=0)).rolling(rsi_period).mean()
        rsi = 100 - (100 / (1 + g / l_.replace(0, np.nan)))
        lo  = rsi.rolling(stoch_k).min()
        hi  = rsi.rolling(stoch_k).max()
        k   = 100 * (rsi - lo) / (hi - lo + 1e-9)
        k   = k.rolling(smooth_k).mean()
        d_  = k.rolling(smooth_d).mean()
        return k, d_

    @staticmethod
    def _supertrend(high, low, close, atr, mult=2.5):
        hl2   = (high + low) / 2.0
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        trend = pd.Series(1, index=close.index)
        su, sl = upper.copy(), lower.copy()
        for i in range(1, len(close)):
            sl.iloc[i] = (lower.iloc[i]
                          if lower.iloc[i] > sl.iloc[i-1]
                          or close.iloc[i-1] < sl.iloc[i-1]
                          else sl.iloc[i-1])
            su.iloc[i] = (upper.iloc[i]
                          if upper.iloc[i] < su.iloc[i-1]
                          or close.iloc[i-1] > su.iloc[i-1]
                          else su.iloc[i-1])
            if   close.iloc[i] > su.iloc[i-1]: trend.iloc[i] =  1
            elif close.iloc[i] < sl.iloc[i-1]: trend.iloc[i] = -1
            else:                               trend.iloc[i] = trend.iloc[i-1]
        return trend, sl, su   # trend + support/resistance lines for SL reference

    # ── Main signal generator ─────────────────────────────────────────────
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Top-down pipeline returning df with signal (+1/0/-1) and all intermediate cols."""
        df = df.copy()
        c, h, l, o = df['close'], df['high'], df['low'], df['open']
        vol = df.get('volume', pd.Series(1, index=df.index))

        # ══ Base indicators ═══════════════════════════════════════════════
        atr = self._atr(h, l, c, self.atr_period)
        df['ATR']     = atr
        df['EMA_fast'] = c.ewm(span=self.ema_fast, adjust=False).mean()
        df['EMA_slow'] = c.ewm(span=self.ema_slow, adjust=False).mean()
        df['RSI']      = self._rsi(c, self.rsi_period)

        rsi     = df['RSI']
        stoch_k, stoch_d = self._stoch_rsi(c, self.rsi_period,
                                            self.stoch_k, self.stoch_smooth, self.stoch_d)
        ema8  = c.ewm(span=self.ema_fast, adjust=False).mean()
        ema21 = c.ewm(span=self.ema_mid,  adjust=False).mean()
        ema50 = c.ewm(span=self.ema_slow, adjust=False).mean()

        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        macd_sig  = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = macd_line - macd_sig

        bb_mid   = c.rolling(self.bb_period).mean()
        bb_std_  = c.rolling(self.bb_period).std()
        bb_up    = bb_mid + self.bb_std * bb_std_
        bb_lo    = bb_mid - self.bb_std * bb_std_
        bb_range = (bb_up - bb_lo).replace(0, np.nan)
        bb_pct   = (c - bb_lo) / bb_range

        st_trend, st_support, st_resist = self._supertrend(h, l, c, atr, self.supertrend_mult)

        vol_ma = vol.rolling(20).mean()

        # ══ HTF Bias (simulated from M1) ══════════════════════════════════
        ema_h4 = c.ewm(span=self.htf_h4_period, adjust=False).mean()
        ema_h1 = c.ewm(span=self.htf_h1_period, adjust=False).mean()

        h4_bull = ema_h4 > ema_h4.shift(5)
        h4_bear = ema_h4 < ema_h4.shift(5)
        h1_bull = ema_h1 > ema_h1.shift(3)
        h1_bear = ema_h1 < ema_h1.shift(3)

        # HARD gate 1: BOTH H4 and H1 must agree
        htf_bull = h4_bull & h1_bull   # buys allowed
        htf_bear = h4_bear & h1_bear   # sells allowed
        htf_bias = pd.Series(0, index=df.index)
        htf_bias[htf_bull] =  1
        htf_bias[htf_bear] = -1

        # ══ SMC / ICT sub-engines ══════════════════════════════════════════
        df = self._smc.identify_swing_points(df)
        df = self._smc.detect_market_structure(df)
        df = self._smc.identify_order_blocks(df)
        df = self._smc.identify_fair_value_gaps(df)
        df = self._smc.detect_liquidity_sweeps(df)

        df = self._ict.calculate_premium_discount(df)
        df = self._ict.identify_ote_zone(df)
        df = self._ict.identify_liquidity_pools(df)
        df = self._ict.identify_kill_zones(df)

        df = self._of.analyze_volume(df)
        df = self._of.calculate_delta(df)
        df = self._of.detect_absorption(df)

        df = self._fib.calculate_fib_levels(df)
        df = self._fib.detect_fib_confluence(df)
        df = self._fib.identify_ote_entries(df)

        # ══ HARD GATE 2: Structural setup (CHoCH or sweep in last N bars) ══
        lb = self.setup_lookback
        # Rolling any() over lookback window
        setup_bull = (
            df['choch_bullish'].rolling(lb, min_periods=1).max().astype(bool) |
            df['liquidity_sweep_bull'].rolling(lb, min_periods=1).max().astype(bool)
        )
        setup_bear = (
            df['choch_bearish'].rolling(lb, min_periods=1).max().astype(bool) |
            df['liquidity_sweep_bear'].rolling(lb, min_periods=1).max().astype(bool)
        )

        # ══ HARD GATE 3: Zone presence ════════════════════════════════════
        # Demand zone: price inside bullish OB (± 0.5 ATR tolerance) OR bullish FVG
        #              OR Stoch RSI extreme oversold (proxy for discount)
        atr_tol   = atr * 0.5
        _def_f    = pd.Series(False, index=df.index)
        ob_demand = (
            (c >= df['active_bull_ob_low']  - atr_tol) &
            (c <= df['active_bull_ob_high'] + atr_tol)
        )
        ob_supply = (
            (c >= df['active_bear_ob_low']  - atr_tol) &
            (c <= df['active_bear_ob_high'] + atr_tol)
        )
        fvg_demand = df['bullish_fvg'].rolling(5, min_periods=1).max().astype(bool)
        fvg_supply = df['bearish_fvg'].rolling(5, min_periods=1).max().astype(bool)
        bb_demand  = bb_pct < 0.25   # price deep in lower 25% of BB = discount
        bb_supply  = bb_pct > 0.75   # price deep in upper 25% of BB = premium
        ote_buy    = df.get('ote_buy_zone',  _def_f) | df.get('in_fib_ote', _def_f)
        ote_sell   = df.get('ote_sell_zone', _def_f) | df.get('in_fib_ote', _def_f)

        in_demand = ob_demand | fvg_demand | (bb_demand & (st_trend == 1))
        in_supply = ob_supply | fvg_supply | (bb_supply & (st_trend == -1))

        # ══ HARD GATE 4: Confirmation candle (last bar closes in trade direction) ══
        bull_candle = (c > o) & ((c - o) > (h - c) * 0.5)   # body > upper wick
        bear_candle = (c < o) & ((o - c) > (c - l) * 0.5)   # body > lower wick

        # ══ Soft score — entry quality (max ~13) ═════════════════════════
        buy_score  = pd.Series(0, index=df.index)
        sell_score = pd.Series(0, index=df.index)

        # OTE fibonacci zone  (+2)
        buy_score  += ote_buy.astype(int)  * 2
        sell_score += ote_sell.astype(int) * 2

        # Stoch RSI — oversold cross for buy, overbought for sell  (+2)
        sk_cross_up = (stoch_k < 30) & (stoch_k > stoch_d) & (stoch_k.shift(1) <= stoch_d.shift(1))
        sk_cross_dn = (stoch_k > 70) & (stoch_k < stoch_d) & (stoch_k.shift(1) >= stoch_d.shift(1))
        # Broader oversold/overbought zone also gives +1
        buy_score  += sk_cross_up.astype(int) * 2 + (stoch_k < 35).astype(int)
        sell_score += sk_cross_dn.astype(int) * 2 + (stoch_k > 65).astype(int)

        # RSI zone  (+1 each)
        buy_score  += rsi.between(25, 50).astype(int)
        sell_score += rsi.between(50, 75).astype(int)

        # MACD aligned  (+1)
        macd_bull = (macd_hist > 0) | ((macd_hist > macd_hist.shift(1)) & (macd_line > macd_sig))
        macd_bear = (macd_hist < 0) | ((macd_hist < macd_hist.shift(1)) & (macd_line < macd_sig))
        buy_score  += macd_bull.astype(int)
        sell_score += macd_bear.astype(int)

        # Supertrend aligned  (+1)
        buy_score  += (st_trend ==  1).astype(int)
        sell_score += (st_trend == -1).astype(int)

        # Order flow absorption  (+2)
        buy_score  += df.get('bullish_absorption', _def_f).astype(int) * 2
        sell_score += df.get('bearish_absorption', _def_f).astype(int) * 2

        # Recent liquidity sweep (last 5 bars = momentum)  (+2)
        sweep_b_recent = df['liquidity_sweep_bull'].rolling(5, min_periods=1).max().astype(bool)
        sweep_s_recent = df['liquidity_sweep_bear'].rolling(5, min_periods=1).max().astype(bool)
        buy_score  += sweep_b_recent.astype(int) * 2
        sell_score += sweep_s_recent.astype(int) * 2

        # Kill zone (LN/NY open) — only in HTF direction  (+1)
        kz = df.get('kill_zone', _def_f)
        buy_score  += (kz & htf_bull).astype(int)
        sell_score += (kz & htf_bear).astype(int)

        # Volume surge in direction  (+1)
        vol_surge  = vol > vol_ma * 1.2
        buy_score  += (vol_surge & bull_candle).astype(int)
        sell_score += (vol_surge & bear_candle).astype(int)

        buy_score  = buy_score.clip(lower=0)
        sell_score = sell_score.clip(lower=0)

        # ══ COMBINE: all 4 hard gates + soft score ════════════════════════
        #  BUY  = HTF bull + setup + in_demand zone + bull candle + soft ≥ min_score
        #  SELL = HTF bear + setup + in_supply zone + bear candle + soft ≥ min_score
        buy_valid  = htf_bull & setup_bull & in_demand & bull_candle & (buy_score  >= self.min_score)
        sell_valid = htf_bear & setup_bear & in_supply & bear_candle & (sell_score >= self.min_score)

        signal = pd.Series(0, index=df.index)
        signal[buy_valid]  =  1
        signal[sell_valid] = -1
        # Conflict (extremely rare given hard gates): stronger score wins
        conflict = buy_valid & sell_valid
        signal[conflict & (sell_score >= buy_score)] = -1

        # ══ Store computed series ══════════════════════════════════════════
        df['buy_score']       = buy_score
        df['sell_score']      = sell_score
        df['atr']             = atr
        df['stoch_k']         = stoch_k
        df['stoch_d']         = stoch_d
        df['rsi']             = rsi
        df['macd_hist']       = macd_hist
        df['bb_pct']          = bb_pct
        df['supertrend']      = st_trend
        df['st_support']      = st_support   # for SL placement
        df['st_resist']       = st_resist
        df['htf_bias']        = htf_bias
        df['htf_h4_bull']     = h4_bull.astype(int)
        df['htf_h1_bull']     = h1_bull.astype(int)
        df['in_demand']       = in_demand.astype(int)
        df['in_supply']       = in_supply.astype(int)
        df['setup_bull']      = setup_bull.astype(int)
        df['setup_bear']      = setup_bear.astype(int)
        df['signal']          = signal
        return df

    def get_indicator_snapshot(self, df: pd.DataFrame) -> dict:
        """Return human-readable snapshot of the last confirmed bar for UI and bot logic."""
        bar = df.iloc[-2]
        def _f(k, d=0): return round(float(bar.get(k, d)), 5)
        def _i(k, d=0): return int(bar.get(k, d))
        def _b(k):      return bool(bar.get(k, False))
        return {
            'buy_score':    _i('buy_score'),
            'sell_score':   _i('sell_score'),
            'htf_bias':     _i('htf_bias'),
            'htf_h4':       _i('htf_h4_bull'),
            'htf_h1':       _i('htf_h1_bull'),
            'setup_bull':   _i('setup_bull'),
            'setup_bear':   _i('setup_bear'),
            'in_demand':    _i('in_demand'),
            'in_supply':    _i('in_supply'),
            'stoch_k':      round(_f('stoch_k'), 1),
            'stoch_d':      round(_f('stoch_d'), 1),
            'rsi':          round(_f('rsi'),     1),
            'macd_hist':    round(_f('macd_hist'), 5),
            'bb_pct':       round(_f('bb_pct', 0.5), 2),
            'supertrend':   _i('supertrend'),
            'atr':          round(_f('atr'), 3),
            'mkt_struct':   _i('market_structure'),
            'liq_sweep_b':  _b('liquidity_sweep_bull'),
            'liq_sweep_s':  _b('liquidity_sweep_bear'),
            'choch_bull':   _b('choch_bullish'),
            'choch_bear':   _b('choch_bearish'),
            # SL reference levels (structural)
            'swing_low':    round(_f('last_swing_low',  0), 3),
            'swing_high':   round(_f('last_swing_high', 9e9), 3),
            'st_support':   round(_f('st_support', 0),  3),
            'st_resist':    round(_f('st_resist',  0),  3),
        }


# ═══════════════════════════════════════════════════════════════════════════════════
#  CRT SCALPING STRATEGY — Candle Range Theory (ICT) with Top-Down Analysis
# ═══════════════════════════════════════════════════════════════════════════════════

class CRTScalpingStrategy:
    """
    ICT Candle Range Theory (CRT) + Trend Break Structure (TBS) strategy.

    ── CRT Pattern (3-bar micro-structure) ─────────────────────────────────────
    BULLISH CRT:
      bar[-3]  = Reference candle   (defines the range: ref_low, ref_high)
      bar[-2]  = Sweep bar          (wick goes BELOW ref_low = liquidity raid)
      bar[-1]  = Displacement bar   (CLOSES BACK ABOVE ref_low = reversal)
                 → Signal fires on this bar. Entry on bar[0] (live bar).

    BEARISH CRT (mirror):
      bar[-2] sweeps ABOVE ref_high, bar[-1] closes back BELOW ref_high → SELL.

    An extended search (lookback 2–5) also accepts:
      bar[-4/-5] as reference, with a sweep somewhere between ref and current.

    ── Top-Down Analysis ────────────────────────────────────────────────────────
    D1  bias  → EMA-1440 on M1 bars (slope indicates daily trend)
    H4  bias  → EMA-240  on M1 bars (EMA above/below its 5-bar lag)
    H1  bias  → EMA-60   on M1 bars (price above/below EMA)
    M15 bias  → EMA-15   on M1 bars (short-term momentum)

    ALL HTF layers must agree with the CRT direction for a trade to fire.

    ── SL / TP (structural — directly from CRT geometry) ───────────────────────
    Bull: SL = sweep_bar_low  - 0.2×ATR guard
          TP = ref_high       (top of the reference candle range)
    Bear: SL = sweep_bar_high + 0.2×ATR guard
          TP = ref_low        (bottom of the reference candle range)

    ── Soft Score (confluence gate) ─────────────────────────────────────────────
    RSI aligned    +1   (bull <55, bear >45)
    BB% aligned    +1   (bull in lower half, bear in upper half)
    Vol on sweep   +1   (sweep bar volume above average)
    Kill zone      +1   (London / NY open hours)
    Sweep depth    +1   (sweep > 0.15×ATR = meaningful raid)
    OB alignment   +1   (recent bullish/bearish OB in direction)
    Fib OTE        +1   (price near 0.618-0.79 fib retracement)
    max = 7  (default min_score = 2, requiring at least 2 soft confirmations)
    """

    # London open 07-10 UTC, NY open 13-16 UTC
    _KILL_HOURS = frozenset([7, 8, 9, 13, 14, 15])

    def __init__(
        self,
        htf_d1_period:  int   = 1440,   # D1  simulation on M1 bars
        htf_h4_period:  int   = 240,    # H4  simulation on M1 bars
        htf_h1_period:  int   = 60,     # H1  simulation on M1 bars
        htf_m15_period: int   = 15,     # M15 momentum on M1 bars
        atr_period:     int   = 14,
        crt_lookback:   int   = 4,      # check ref candles 2..crt_lookback bars back
        min_ref_size_atr: float = 0.25, # ref candle range ≥ N×ATR (filters tiny candles)
        min_sweep_depth_atr: float = 0.10,  # sweep must go at least N×ATR beyond ref edge
        min_score:      int   = 3,      # soft confluence gates out of 10
        require_d1:     bool  = False,  # set True to also require D1 alignment
        tbs_fast_ema:   int   = 20,     # TBS trend fast EMA
        tbs_slow_ema:   int   = 50,     # TBS trend slow EMA
        tbs_breakout_lookback: int = 6, # structure break lookback bars
    ):
        self.htf_d1_period       = htf_d1_period
        self.htf_h4_period       = htf_h4_period
        self.htf_h1_period       = htf_h1_period
        self.htf_m15_period      = htf_m15_period
        self.atr_period          = atr_period
        self.crt_lookback        = max(2, crt_lookback)
        self.min_ref_size_atr    = min_ref_size_atr
        self.min_sweep_depth_atr = min_sweep_depth_atr
        self.min_score           = min_score
        self.require_d1          = require_d1
        self.tbs_fast_ema        = max(5, int(tbs_fast_ema))
        self.tbs_slow_ema        = max(self.tbs_fast_ema + 1, int(tbs_slow_ema))
        self.tbs_breakout_lookback = max(3, int(tbs_breakout_lookback))
        self.max_score_reference = 10

        # sub-engines for OB detection only
        self._smc = SmartMoneyConcepts(swing_lookback=8)

    # ── Static helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _atr(high, low, close, period=14):
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def _rsi(close, period=14):
        d = close.diff()
        g = d.clip(lower=0).rolling(period).mean()
        lo = (-d.clip(upper=0)).rolling(period).mean()
        return 100 - (100 / (1 + g / lo.replace(0, np.nan)))

    # ── CRT pattern detection across multiple reference offsets ───────────────
    def _detect_crt(self, high, low, close, open_, atr):
        """
        Scan offsets 2..crt_lookback for a CRT pattern.
        Returns:
            bull_crt     : Series[bool]
            bear_crt     : Series[bool]
            crt_ref_high : Series[float]  — reference candle high (for TP)
            crt_ref_low  : Series[float]  — reference candle low  (for TP)
            crt_sw_high  : Series[float]  — sweep bar high (for SL on sells)
            crt_sw_low   : Series[float]  — sweep bar low  (for SL on buys)
        """
        n = len(close)
        bull = pd.Series(False, index=close.index)
        bear = pd.Series(False, index=close.index)
        ref_h = pd.Series(np.nan, index=close.index)
        ref_l = pd.Series(np.nan, index=close.index)
        sw_h  = pd.Series(np.nan, index=close.index)
        sw_l  = pd.Series(np.nan, index=close.index)

        # For each reference offset (how many bars back is the reference candle):
        #   ref = bar[i - offset]
        #   sweep = the MINIMUM low / MAXIMUM high between ref+1 and i-1 inclusive
        #   signal bar = bar[i]
        for offset in range(2, self.crt_lookback + 1):
            rh = high.shift(offset)
            rl = low.shift(offset)
            rc = close.shift(offset)
            ro = open_.shift(offset)

            # Ref candle must have meaningful range
            ref_size   = rh - rl
            ref_size_ok = ref_size >= atr * self.min_ref_size_atr

            # Sweep: the EXTREME of all bars BETWEEN the reference and the current bar
            # (bars at indices offset-1, offset-2, … 1 relative to current = shifts 1..offset-1)
            sw_lo_min = pd.Series(np.inf, index=close.index)
            sw_hi_max = pd.Series(-np.inf, index=close.index)
            for k in range(1, offset):
                sw_lo_min = pd.concat([sw_lo_min, low.shift(k)],  axis=1).min(axis=1)
                sw_hi_max = pd.concat([sw_hi_max, high.shift(k)], axis=1).max(axis=1)

            # Guard against fully-nan sequences (fewer bars than offset)
            sw_lo_min = sw_lo_min.replace(np.inf,  np.nan)
            sw_hi_max = sw_hi_max.replace(-np.inf, np.nan)

            # ── BULL CRT ──────────────────────────────────────────────────
            #  1. Sweep went below ref_low (raid)
            #  2. Sweep depth ≥ min (not a trivial dip)
            #  3. Current bar closes ABOVE ref_low (displacement up)
            #  4. Current bar is bullish (close > open)
            swept_below   = sw_lo_min < rl
            sweep_depth_b = (rl - sw_lo_min) >= atr * self.min_sweep_depth_atr
            close_inside_b = close > rl
            bull_candle   = (close > open_) & ((close - open_) > 0.0)

            this_bull = (ref_size_ok & swept_below & sweep_depth_b
                         & close_inside_b & bull_candle)

            # ── BEAR CRT ──────────────────────────────────────────────────
            swept_above   = sw_hi_max > rh
            sweep_depth_s = (sw_hi_max - rh) >= atr * self.min_sweep_depth_atr
            close_inside_s = close < rh
            bear_candle   = (close < open_) & ((open_ - close) > 0.0)

            this_bear = (ref_size_ok & swept_above & sweep_depth_s
                         & close_inside_s & bear_candle)

            # Update output series (first matching offset wins)
            new_bull = this_bull & ~bull
            new_bear = this_bear & ~bear

            ref_h = ref_h.where(~new_bull, rh)
            ref_l = ref_l.where(~new_bull, rl)
            sw_l  = sw_l.where(~new_bull,  sw_lo_min)
            ref_h = ref_h.where(~new_bear, rh)
            ref_l = ref_l.where(~new_bear, rl)
            sw_h  = sw_h.where(~new_bear,  sw_hi_max)

            bull = bull | new_bull
            bear = bear | new_bear

        return bull, bear, ref_h, ref_l, sw_h, sw_l

    # ── Main signal generator ─────────────────────────────────────────────────
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Top-down CRT signal engine.
        Returns df with: signal (+1/0/-1), buy_score, sell_score, ATR,
        crt_ref_high, crt_ref_low, crt_sw_high, crt_sw_low, htf_bias, rsi, bb_pct
        """
        df = df.copy()
        c = df['close'];  h = df['high'];  l = df['low'];  o = df['open']
        vol = df.get('volume', pd.Series(1, index=df.index))

        # ── ATR ───────────────────────────────────────────────────────────────
        atr = self._atr(h, l, c, self.atr_period)
        df['ATR'] = atr

        # ── Top-down HTF bias ─────────────────────────────────────────────────
        n = len(df)

        # H1 (EMA-60): price above → bullish; below → bearish
        ema_h1 = c.ewm(span=self.htf_h1_period, adjust=False).mean()
        h1_bull = c > ema_h1
        h1_bear = c < ema_h1

        # H4 (EMA-240): EMA slope — use a meaningful lookback (≥ 1/5 of the period)
        # e.g. htf_h4_period=240 → shift(48) = 4 hours of slope on M1 data
        #      htf_h4_period=60  → shift(12) = 1 hour of slope on M1 data
        h4_slope_window = max(5, self.htf_h4_period // 5)
        ema_h4 = c.ewm(span=self.htf_h4_period, adjust=False).mean()
        h4_bull = ema_h4 > ema_h4.shift(h4_slope_window)
        h4_bear = ema_h4 < ema_h4.shift(h4_slope_window)

        # D1 (EMA-1440): only meaningful when we have ≥1440 bars; graceful fallback
        if n >= self.htf_d1_period + 20:
            ema_d1  = c.ewm(span=self.htf_d1_period, adjust=False).mean()
            d1_bull = ema_d1 > ema_d1.shift(10)
            d1_bear = ema_d1 < ema_d1.shift(10)
        else:
            # Fallback: use H4 slope over a longer window as proxy for daily bias
            ema_d1  = c.ewm(span=min(self.htf_h4_period, n // 3), adjust=False).mean()
            d1_bull = ema_d1 > ema_d1.shift(max(1, n // 20))
            d1_bear = ema_d1 < ema_d1.shift(max(1, n // 20))

        # M15 momentum (EMA-15): short-term entry momentum confirmation
        ema_m15   = c.ewm(span=self.htf_m15_period, adjust=False).mean()
        m15_bull  = c > ema_m15
        m15_bear  = c < ema_m15

        # ── HARD GATE: HTF alignment ──────────────────────────────────────────
        # H4 + H1 mandatory (slow + medium trend). M15 is too noisy for a hard gate;
        # it is scored as a soft point below. D1 optional via require_d1.
        if self.require_d1:
            htf_bull = d1_bull & h4_bull & h1_bull
            htf_bear = d1_bear & h4_bear & h1_bear
        else:
            htf_bull = h4_bull & h1_bull
            htf_bear = h4_bear & h1_bear

        htf_bias = pd.Series(0, index=df.index)
        htf_bias[htf_bull] =  1
        htf_bias[htf_bear] = -1

        # ── TBS (Trend Break Structure) layer ───────────────────────────────
        tbs_ema_fast = c.ewm(span=self.tbs_fast_ema, adjust=False).mean()
        tbs_ema_slow = c.ewm(span=self.tbs_slow_ema, adjust=False).mean()
        tbs_trend_up = (tbs_ema_fast > tbs_ema_slow) & (c > tbs_ema_fast)
        tbs_trend_down = (tbs_ema_fast < tbs_ema_slow) & (c < tbs_ema_fast)

        recent_high = h.shift(1).rolling(self.tbs_breakout_lookback).max()
        recent_low = l.shift(1).rolling(self.tbs_breakout_lookback).min()
        tbs_break_bull = c > recent_high
        tbs_break_bear = c < recent_low

        # ── CRT pattern detection ─────────────────────────────────────────────
        bull_crt, bear_crt, ref_h, ref_l, sw_h, sw_l = self._detect_crt(h, l, c, o, atr)

        # ── Soft confluence score (0–7) ───────────────────────────────────────
        rsi   = self._rsi(c, 14)
        bb_m  = c.rolling(20).mean()
        bb_sd = c.rolling(20).std()
        bb_lo = bb_m - 2 * bb_sd
        bb_hi = bb_m + 2 * bb_sd
        bb_pct = (c - bb_lo) / ((bb_hi - bb_lo).replace(0, np.nan))

        vol_ma   = vol.rolling(20).mean()
        # Volume on the sweep zone (bars -1 to -crt_lookback relative to current)
        # Use the max volume of those bars vs the rolling average
        sweep_vol_max = pd.Series(0.0, index=df.index)
        for k in range(1, self.crt_lookback):
            sweep_vol_max = pd.concat([sweep_vol_max, vol.shift(k)], axis=1).max(axis=1)
        high_sweep_vol = sweep_vol_max > vol_ma * 1.05

        # Kill zone (London / NY open)
        if 'time' in df.columns:
            hour = pd.to_datetime(df['time']).dt.hour
        else:
            hour = pd.Series(0, index=df.index)
        kill_zone = hour.isin(self._KILL_HOURS)

        # OB alignment + structure context
        structure_bull_recent = pd.Series(False, index=df.index)
        structure_bear_recent = pd.Series(False, index=df.index)
        try:
            df_tmp = self._smc.identify_swing_points(df.copy())
            df_tmp = self._smc.detect_market_structure(df_tmp)
            df_tmp = self._smc.detect_liquidity_sweeps(df_tmp)
            df_tmp = self._smc.identify_order_blocks(df_tmp)
            ob_bull  = df_tmp.get('active_bull_ob_low',  pd.Series(np.nan, index=df.index)).notna()
            ob_bear  = df_tmp.get('active_bear_ob_high', pd.Series(np.nan, index=df.index)).notna()

            structure_bull = (
                df_tmp.get('bos_bullish', pd.Series(False, index=df.index)) |
                df_tmp.get('choch_bullish', pd.Series(False, index=df.index)) |
                df_tmp.get('liquidity_sweep_bull', pd.Series(False, index=df.index))
            )
            structure_bear = (
                df_tmp.get('bos_bearish', pd.Series(False, index=df.index)) |
                df_tmp.get('choch_bearish', pd.Series(False, index=df.index)) |
                df_tmp.get('liquidity_sweep_bear', pd.Series(False, index=df.index))
            )
            structure_bull_recent = structure_bull.rolling(5, min_periods=1).max().astype(bool)
            structure_bear_recent = structure_bear.rolling(5, min_periods=1).max().astype(bool)

            # Only count if price is near the OB
            atr_tol = atr * 0.5
            ob_in_bull = ob_bull & (c >= df_tmp.get('active_bull_ob_low', 0) - atr_tol)
            ob_in_bear = ob_bear & (c <= df_tmp.get('active_bear_ob_high', 9e9) + atr_tol)
        except Exception:
            ob_in_bull = pd.Series(False, index=df.index)
            ob_in_bear = pd.Series(False, index=df.index)

        # Fib OTE — ICT OTE is a RETRACEMENT zone, measured FROM the swing extreme.
        # BULL: price pulled back from the swing HIGH into the 61.8%-78.6% retrace
        #       = swing_hi - 0.786*range  to  swing_hi - 0.614*range  (discount zone)
        # BEAR: price rallied from the swing LOW into the 61.8%-78.6% retrace
        #       = swing_lo + 0.614*range  to  swing_lo + 0.786*range  (premium zone)
        swing_hi_50 = h.rolling(50).max()
        swing_lo_50 = l.rolling(50).min()
        swing_range = (swing_hi_50 - swing_lo_50).replace(0, np.nan)
        # Bull OTE: discount retrace below the swing high
        ote_bull_lo  = swing_hi_50 - 0.786 * swing_range   # deepest acceptable retrace
        ote_bull_hi  = swing_hi_50 - 0.614 * swing_range   # shallowest acceptable retrace
        fib_ote_bull = (c >= ote_bull_lo) & (c <= ote_bull_hi)
        # Bear OTE: premium retrace above the swing low
        ote_bear_lo  = swing_lo_50 + 0.614 * swing_range   # shallowest retrace
        ote_bear_hi  = swing_lo_50 + 0.786 * swing_range   # deepest retrace
        fib_ote_bear = (c >= ote_bear_lo) & (c <= ote_bear_hi)

        # Sweep depth score (extra quality bonus — sweep must be meaningful)
        sweep_depth_bull = (ref_l - sw_l).fillna(0) > atr * 0.25
        sweep_depth_bear = (sw_h - ref_h).fillna(0) > atr * 0.25

        buy_score  = pd.Series(0, index=df.index)
        sell_score = pd.Series(0, index=df.index)

        buy_score  += (rsi < 55).astype(int)                   # RSI in discount/neutral
        sell_score += (rsi > 45).astype(int)                   # RSI in premium/neutral

        buy_score  += (bb_pct.fillna(0.5) < 0.45).astype(int) # price below BB midpoint
        sell_score += (bb_pct.fillna(0.5) > 0.55).astype(int) # price above BB midpoint

        buy_score  += high_sweep_vol.astype(int)               # volume on sweep bars
        sell_score += high_sweep_vol.astype(int)

        buy_score  += (kill_zone & htf_bull).astype(int)       # kill zone in direction
        sell_score += (kill_zone & htf_bear).astype(int)

        # M15 momentum (now a soft score item, removed from hard HTF gate)
        buy_score  += m15_bull.astype(int)
        sell_score += m15_bear.astype(int)

        buy_score  += sweep_depth_bull.astype(int)             # meaningful sweep depth
        sell_score += sweep_depth_bear.astype(int)

        buy_score  += ob_in_bull.astype(int)                   # near OB in direction
        sell_score += ob_in_bear.astype(int)

        buy_score  += fib_ote_bull.astype(int)                 # in Fibonacci OTE
        sell_score += fib_ote_bear.astype(int)

        # TBS confluence (+2 max)
        buy_score  += tbs_trend_up.astype(int)
        sell_score += tbs_trend_down.astype(int)
        buy_score  += (tbs_break_bull | structure_bull_recent).astype(int)
        sell_score += (tbs_break_bear | structure_bear_recent).astype(int)

        buy_score  = buy_score.clip(lower=0)
        sell_score = sell_score.clip(lower=0)

        # ── FINAL SIGNAL ──────────────────────────────────────────────────────
        # All gates: HTF alignment + TBS alignment + CRT pattern + score threshold.
        tbs_bull = tbs_trend_up & (tbs_break_bull | structure_bull_recent)
        tbs_bear = tbs_trend_down & (tbs_break_bear | structure_bear_recent)

        buy_valid  = htf_bull & tbs_bull & bull_crt & (buy_score  >= self.min_score)
        sell_valid = htf_bear & tbs_bear & bear_crt & (sell_score >= self.min_score)

        signal = pd.Series(0, index=df.index)
        signal[buy_valid]  =  1
        signal[sell_valid] = -1
        # Conflict resolution: stronger score wins
        conflict = buy_valid & sell_valid
        signal[conflict & (sell_score >= buy_score)] = -1

        # ── Store computed columns ─────────────────────────────────────────────
        df['signal']       = signal
        df['buy_score']    = buy_score
        df['sell_score']   = sell_score
        df['htf_bias']     = htf_bias
        df['htf_h4_bull']  = h4_bull.astype(int)
        df['htf_h1_bull']  = h1_bull.astype(int)
        df['htf_d1_bull']  = d1_bull.astype(int)
        df['htf_m15_bull'] = m15_bull.astype(int)
        df['tbs_ema_fast'] = tbs_ema_fast
        df['tbs_ema_slow'] = tbs_ema_slow
        df['tbs_trend_up'] = tbs_trend_up.astype(int)
        df['tbs_trend_down'] = tbs_trend_down.astype(int)
        df['tbs_break_bull'] = tbs_break_bull.astype(int)
        df['tbs_break_bear'] = tbs_break_bear.astype(int)
        df['tbs_bull'] = tbs_bull.astype(int)
        df['tbs_bear'] = tbs_bear.astype(int)
        df['rsi']          = rsi
        df['bb_pct']       = bb_pct
        df['crt_bull']     = bull_crt.astype(int)
        df['crt_bear']     = bear_crt.astype(int)
        df['crt_ref_high'] = ref_h           # TP for sells / target for buys
        df['crt_ref_low']  = ref_l           # TP for buys  / target for sells
        df['crt_sw_high']  = sw_h            # SL guard for sells
        df['crt_sw_low']   = sw_l            # SL guard for buys
        df['ema_h1']       = ema_h1
        df['ema_h4']       = ema_h4
        df['vol_ma']       = vol_ma
        df['kill_zone']    = kill_zone.astype(int)
        df['high_volume']  = high_sweep_vol.astype(int)
        df['ob_in_bull']   = ob_in_bull.astype(int)
        df['ob_in_bear']   = ob_in_bear.astype(int)
        df['fib_ote_bull'] = fib_ote_bull.astype(int)
        df['fib_ote_bear'] = fib_ote_bear.astype(int)
        df['structure_bull_recent'] = structure_bull_recent.astype(int)
        df['structure_bear_recent'] = structure_bear_recent.astype(int)
        return df

    # ── Indicator snapshot for UI + bot logic ────────────────────────────────
    def get_indicator_snapshot(self, df: pd.DataFrame) -> dict:
        """Return flat dict of key values from the last confirmed bar (df.iloc[-2])."""
        bar = df.iloc[-2]
        def _f(k, d=0.0): return round(float(bar.get(k, d) if bar.get(k, d) == bar.get(k, d) else d), 5)
        def _i(k, d=0):   return int(bar.get(k, d) if bar.get(k, d) == bar.get(k, d) else d)
        return {
            'buy_score':    _i('buy_score'),
            'sell_score':   _i('sell_score'),
            'htf_bias':     _i('htf_bias'),
            'htf_h4':       _i('htf_h4_bull'),
            'htf_h1':       _i('htf_h1_bull'),
            'htf_d1':       _i('htf_d1_bull'),
            'htf_m15':      _i('htf_m15_bull'),
            'rsi':          round(_f('rsi', 50), 1),
            'bb_pct':       round(_f('bb_pct', 0.5), 3),
            'atr':          round(_f('ATR', 0), 5),
            'crt_bull':     _i('crt_bull'),
            'crt_bear':     _i('crt_bear'),
            # CRT structural levels for SL + TP
            'crt_ref_high': round(_f('crt_ref_high', 0), 5),
            'crt_ref_low':  round(_f('crt_ref_low',  0), 5),
            'crt_sw_high':  round(_f('crt_sw_high',  0), 5),
            'crt_sw_low':   round(_f('crt_sw_low',   0), 5),
        }

    def get_signal_strength(self, df: pd.DataFrame) -> int:
        """Compatibility API for dashboard/headless filtering (0-100)."""
        if df is None or len(df) < 2:
            return 0

        latest = df.iloc[-1]
        if latest.get('signal', 0) == 1:
            score = float(latest.get('buy_score', 0))
        elif latest.get('signal', 0) == -1:
            score = float(latest.get('sell_score', 0))
        else:
            score = float(max(latest.get('buy_score', 0), latest.get('sell_score', 0)))

        max_score = max(1, int(self.max_score_reference))
        return int(min(100, max(0, round((score / max_score) * 100))))

    def get_signal_breakdown(self, df: pd.DataFrame) -> dict:
        """Compatibility API returning tag booleans consumed by dashboard UI."""
        if df is None or len(df) == 0:
            return {}

        latest = df.iloc[-1]
        return {
            'buy_score': int(latest.get('buy_score', 0)),
            'sell_score': int(latest.get('sell_score', 0)),
            'in_order_block': bool(latest.get('ob_in_bull', 0) or latest.get('ob_in_bear', 0)),
            'has_fvg': False,
            'in_ote_zone': bool(latest.get('fib_ote_bull', 0) or latest.get('fib_ote_bear', 0)),
            'in_kill_zone': bool(latest.get('kill_zone', 0)),
            'high_volume': bool(latest.get('high_volume', 0)),
            'fib_confluence': int(latest.get('fib_ote_bull', 0) or latest.get('fib_ote_bear', 0)),
            'liquidity_sweep': bool(latest.get('crt_bull', 0) or latest.get('crt_bear', 0)),
            'buyside_liquidity': bool(latest.get('crt_ref_high', 0) > 0),
            'sellside_liquidity': bool(latest.get('crt_ref_low', 0) > 0),
            'near_bsl': bool(latest.get('crt_ref_high', 0) > 0),
            'near_ssl': bool(latest.get('crt_ref_low', 0) > 0),
            'price_at_pdh': False,
            'price_at_pdl': False,
            'liq_run_bull': bool(latest.get('structure_bull_recent', 0)),
            'liq_run_bear': bool(latest.get('structure_bear_recent', 0)),
            'inducement_bull': bool(latest.get('crt_bull', 0)),
            'inducement_bear': bool(latest.get('crt_bear', 0)),
            'rsi_oversold': bool(float(latest.get('rsi', 50)) <= 35),
            'rsi_overbought': bool(float(latest.get('rsi', 50)) >= 65),
            'macd_crossover': False,
            'macd_bullish': bool(latest.get('tbs_trend_up', 0)),
            'ema_crossover': bool(latest.get('tbs_break_bull', 0) or latest.get('tbs_break_bear', 0)),
            'ema_bullish': bool(latest.get('tbs_trend_up', 0)),
            'bb_lower_touch': bool(float(latest.get('bb_pct', 0.5)) <= 0.2),
            'bb_upper_touch': bool(float(latest.get('bb_pct', 0.5)) >= 0.8),
            'signal': int(latest.get('signal', 0)),
            'htf_bias': int(latest.get('htf_bias', 0)),
            'tbs_bull': bool(latest.get('tbs_bull', 0)),
            'tbs_bear': bool(latest.get('tbs_bear', 0)),
            'crt_bull': bool(latest.get('crt_bull', 0)),
            'crt_bear': bool(latest.get('crt_bear', 0)),
            'atr': float(latest.get('ATR', 0) or 0),
        }
