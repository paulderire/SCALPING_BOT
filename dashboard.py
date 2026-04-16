#!/usr/bin/env python3
"""
Multi-Symbol Scalper Bot - Web Dashboard
==========================================
Advanced Strategy: CRT + TBS (M5)
Supports: GOLD, EURUSD, GBPUSD, BTCUSD on 5-minute timeframe

Usage:
    python dashboard.py
    
Then open http://localhost:5000 in your browser.
"""

import sys
import json
import os
import re
import copy
import csv
import io
import threading
import time
import smtplib
import ssl
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
import numpy as np
from flask import Flask, render_template_string, jsonify, request, make_response
from flask.json.provider import DefaultJSONProvider

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from gold_scalper import AdvancedScalpingStrategy, RiskManager, GoldM1Strategy, CRTScalpingStrategy
from config import (
    BROKER_CONFIG,
    STRATEGY_CONFIG,
    CRT_TBS_CONFIG,
    RISK_CONFIG,
    SYMBOLS_CONFIG,
    DAILY_GOAL_CONFIG,
    EMAIL_CONFIG,
    BTC_PROFILE_PRESETS,
)


# Custom JSON encoder for numpy types
class NumpyJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, (np.integer, np.int64, np.int32)):
            return int(obj)
        elif isinstance(obj, (np.floating, np.float64, np.float32)):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, np.bool_):
            return bool(obj)
        return super().default(obj)


app = Flask(__name__)
app.json = NumpyJSONProvider(app)

# Daily goal tracking
daily_goal_state = {
    'start_balance': None,
    'start_date': None,
    'current_pnl': 0.0,
    'goal_reached': False,
    'closed_profit': 0.0,
}

# Global state
bot_state = {
    'running': False,
    'connected': False,
    'account': None,
    'symbols': {},
    'logs': [],
    'stats': {
        'signals_generated': 0,
        'trades_opened': 0,
        'session_pnl': 0,
    },
    'total_positions': 0,
    'trade_history': [],          # closed trades (most recent first)
    'strategy_name': 'CRT + TBS (M5)',
    'daily_goal': {
        'enabled': DAILY_GOAL_CONFIG.get('enabled', True),
        'target': DAILY_GOAL_CONFIG.get('daily_target', 20.0),
        'action': DAILY_GOAL_CONFIG.get('action_on_goal', 'close_all'),
        'current_pnl': 0.0,
        'goal_reached': False,
        'progress_pct': 0.0,
    },
    'btc_mode': {
        'm1': False,
        'm5': False,
        'h4': False,
    },
    'btc_backup': {
        'm1': None,
        'm5': None,
        'h4': None,
    },
    'notifications': {
        'daily_summary_sent_for': None,
        'risk_alert_buckets_sent_for': {},
        'daily_report_sent_for': None,
    },
    'watchdogs': {
        'heartbeat': {
            'smc': None,
            'gold': None,
            'daytrade': None,
        },
        'session_alert_sent': {
            'smc': False,
            'gold': False,
            'daytrade': False,
        },
        'broker_alert_sent': False,
        'last_broker_ok': None,
    },
    'push_config': {
        'enabled': False,
        'telegram_bot_token': '',
        'telegram_chat_id': '',
        'discord_webhook_url': '',
    },
    'config_history': [],
    'emergency_pause': {
        'active': False,
        'reason': '',
        'triggered_at': None,
        'close_positions': False,
    },
}

# Initialize symbol states
for sym_key in SYMBOLS_CONFIG:
    bot_state['symbols'][sym_key] = {
        'enabled': SYMBOLS_CONFIG[sym_key].get('enabled', True),
        'symbol': SYMBOLS_CONFIG[sym_key].get('symbol', sym_key),
        'price': None,
        'spread': 0,
        'signal': 'NONE',
        'signal_strength': 0,
        'buy_score': 0,
        'sell_score': 0,
        'positions': [],
        'last_signal_time': None,
        'analysis': {},
        # Per-symbol trade config (editable from the dashboard)
        'lot_size': 0.0,        # 0 = auto (risk-based); >0 = fixed lots
        'max_positions': RISK_CONFIG.get('max_positions_per_symbol', 1),
    }

bot_thread = None

# ── Global Trading-Enabled kill-switch ────────────────────────────────
# When False, ALL bots refuse to open new trades (existing positions are unaffected).
trading_enabled = True   # toggled via the header button or /api/trading_enabled

# ── Gold Day Trading Bot (D1 candles, SMC+ICT+Fib strategy, GOLD only) ─
DAYTRADE_MAGIC   = 237000
_day_positions   = {}    # ticket → snapshot
daytrade_thread  = None
daytrade_state   = {
    'running':     False,
    'manual_running': False,
    'stop_reason': 'Not started — click ▶ Start above.',
    'logs':        [],
    'stats':       {'trades_opened': 0, 'recycled': 0, 'wins': 0, 'losses': 0},
    'config': {
        'symbol_key':       'GOLD',  # Any key from SYMBOLS_CONFIG (e.g. GOLD, BTCUSD)
        'lot_size':         0.0,   # 0 = auto risk-based
        'max_positions':    2,
        'max_spread':       80,
        'sl_atr_mult':      1.5,
        'tp_atr_mult':      3.0,
        'confluence_score': 8,     # min score from SMC+ICT+Fib strategy
        'session_filter':   True,
        'candle_patterns':  True,
        'recycle_pct':      0.60,
        'timeframe':        'D1',  # daily candle strategy
    },
    'live': {
        'price':           None,
        'signal':          'NONE',
        'buy_score':       0,
        'sell_score':      0,
        'atr':             0.0,
        'spread':          0,
        'in_session':      False,
        'session_name':    '',
        'pattern':         '',
        'pattern_dir':     '',
        'positions':       [],
        'total_positions': 0,
        'indicators':      {},
    },
}

# ── Gold 1-Min Bot (GOLD-specific parameters) ─────────────────────────
GOLD_MAGIC       = 236000
_gold_positions  = {}    # ticket → direction (+1/-1)
gold_thread      = None
gold_state       = {
    'running': False,
    'manual_running': False,
    'stop_reason': 'Not started — click \u25B6 Start above.',
    'logs':    [],
    'stats':   {'trades_opened': 0, 'recycled': 0, 'wins': 0, 'losses': 0},
    'config': {
        'symbol_key':       'GOLD',  # Any key from SYMBOLS_CONFIG (e.g. GOLD, BTCUSD)
        'lot_size':         0.0,   # 0 = auto risk-based
        'max_positions':    3,
        'max_spread':       80,    # max spread in points (gold has wide spreads)
        'sl_atr_mult':      1.2,   # SL = entry +/- sl_mult x ATR
        'tp_atr_mult':      2.2,   # TP = entry +/- tp_mult x ATR
        'confluence_score': 5,     # min confluence score needed (lower = more trades)
        'session_filter':   True,  # trade London Open 07-10 + NY 12-17 UTC only
        'candle_patterns':  True,  # bonus +2 score for pin bars / engulfing candles
        'recycle_pct':      0.50,  # close position at this % of TP distance
    },
    'live': {
        'price':           None,
        'signal':          'NONE',
        'buy_score':       0,
        'sell_score':      0,
        'atr':             0.0,
        'spread':          0,
        'in_session':      False,
        'session_name':    '',
        'pattern':         '',
        'pattern_dir':     '',
        'positions':       [],
        'total_positions': 0,
    },
}

# ── Persistence paths ──
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(_DATA_DIR, exist_ok=True)
_HISTORY_FILE    = os.path.join(_DATA_DIR, 'trade_history.json')
_DAILY_GOAL_FILE = os.path.join(_DATA_DIR, 'daily_goal_state.json')
_ARCHIVE_FILE    = os.path.join(_DATA_DIR, 'trade_archive.json')   # permanent cross-day store

# ── New-Day reset guard: trades closed BEFORE this time are ignored on reload ──
_reset_cutoff_time: datetime = None   # type: ignore

_notification_lock = threading.Lock()
_notification_thread_started = False
_watchdog_thread_started = False
_config_history_lock = threading.Lock()
_CONFIG_HISTORY_FILE = os.path.join(_DATA_DIR, 'config_history.json')
_NOTIFY_SETTINGS_FILE = os.path.join(_DATA_DIR, 'notification_settings.json')

# ── Stop-out confirmation: require N consecutive missing cycles before recording close ──
# This prevents a 1-cycle MT5 API blip from recording a trade as closed with wrong P&L.
CLOSE_CONFIRM_CYCLES = 2
_missing_since_gold:  dict = {}   # ticket -> consecutive-missing-cycle count


def _save_history_to_disk():
    """Persist today's trade history to disk."""
    try:
        login = bot_state['account']['login'] if bot_state.get('account') else None
        payload = {
            'date':    datetime.now().strftime('%Y-%m-%d'),
            'account': login,
            'trades':  bot_state['trade_history'],
            'stats':   bot_state['stats'],
        }
        with open(_HISTORY_FILE, 'w') as f:
            json.dump(payload, f, default=str)
    except Exception:
        pass


_archive_lock  = threading.Lock()
# Serialize mt5.initialize() calls across all threads \u2014 MT5 Python API is not
# safe for concurrent initialize() calls from multiple threads simultaneously.
_mt5_init_lock = threading.Lock()


def _resolve_symbol_key(symbol_key, default_key='GOLD'):
    """Resolve a symbol key from SYMBOLS_CONFIG with safe fallback."""
    key = str(symbol_key or default_key).upper().strip()
    if key in SYMBOLS_CONFIG:
        return key
    return default_key if default_key in SYMBOLS_CONFIG else next(iter(SYMBOLS_CONFIG))


def _gold_symbol_runtime():
    """Return (symbol_key, symbol_name, symbol_cfg) for the 1-min bot."""
    symbol_key = _resolve_symbol_key(gold_state.get('config', {}).get('symbol_key', 'GOLD'), 'GOLD')
    sym_cfg = SYMBOLS_CONFIG.get(symbol_key, {})
    symbol_name = sym_cfg.get('symbol', symbol_key)
    return symbol_key, symbol_name, sym_cfg


def _daytrade_symbol_runtime():
    """Return (symbol_key, symbol_name, symbol_cfg) for the day-trade bot."""
    symbol_key = _resolve_symbol_key(daytrade_state.get('config', {}).get('symbol_key', 'GOLD'), 'GOLD')
    sym_cfg = SYMBOLS_CONFIG.get(symbol_key, {})
    symbol_name = sym_cfg.get('symbol', symbol_key)
    return symbol_key, symbol_name, sym_cfg
_symbol_alias_cache = {}
_symbol_alias_logged = set()
_symbol_alias_failed_logged = set()

def _mt5_ensure():
    """Thread-safe mt5.initialize() wrapper.  Returns True if connection is ready."""
    with _mt5_init_lock:
        return mt5.initialize()


def _normalize_symbol_name(name):
    """Normalize symbol for fuzzy matching across broker suffix/prefix variants."""
    return re.sub(r'[^A-Z0-9]', '', str(name).upper())


def _resolve_symbol(symbol_name):
    """
    Resolve configured symbol to an actual broker symbol.
    Handles cases like EURUSD -> EURUSD.a, GOLD -> XAUUSDm, BTCUSD -> XBTUSDm.
    """
    raw = str(symbol_name or '').strip()
    if not raw or not MT5_AVAILABLE:
        return raw

    cache_key = raw.upper()
    cached = _symbol_alias_cache.get(cache_key)
    if cached and mt5.symbol_info(cached):
        return cached

    exact = mt5.symbol_info(raw)
    if exact:
        if not exact.visible:
            mt5.symbol_select(raw, True)
        _symbol_alias_cache[cache_key] = raw
        return raw

    symbols = mt5.symbols_get()
    if not symbols:
        return raw

    raw_upper = raw.upper()
    raw_norm = _normalize_symbol_name(raw)

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

    # Keep order while dropping duplicates.
    alias_tokens = list(dict.fromkeys(alias_tokens))
    alias_norm_tokens = [tok for tok in {_normalize_symbol_name(t) for t in alias_tokens} if tok]

    best_name = None
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

        # Prefer cleaner matches with shorter suffix/prefix overhead.
        extra = max(0, len(name_norm) - len(raw_norm))
        score -= min(extra, 20)

        if score > best_score:
            best_score = score
            best_name = name

    if best_name and best_score >= 170:
        mt5.symbol_select(best_name, True)
        _symbol_alias_cache[cache_key] = best_name
        if best_name != raw and raw not in _symbol_alias_logged:
            add_log(f"Symbol mapped: {raw} -> {best_name}", "INFO")
            _symbol_alias_logged.add(raw)
        return best_name

    if raw not in _symbol_alias_failed_logged:
        add_log(f"No broker symbol match for {raw}. Check names with find_symbol.py", "WARNING")
        _symbol_alias_failed_logged.add(raw)

    return raw


def _is_btc_symbol_name(symbol_name):
    s = str(symbol_name or '').upper()
    return ('BTC' in s) or ('XBT' in s)


def _open_trade_loss_metrics():
    """Return combined open-trade PnL and loss-reach metrics across all MT5 positions."""
    try:
        positions = mt5.positions_get() or []
    except Exception:
        positions = []

    profits = []
    for pos in positions:
        try:
            profits.append(float(pos.profit))
        except Exception:
            continue

    combined_pnl = round(sum(profits), 2) if profits else 0.0
    loss_values = [p for p in profits if p < 0]
    combined_loss = round(sum(loss_values), 2) if loss_values else 0.0
    worst_loss = round(min(loss_values), 2) if loss_values else 0.0
    return {
        'open_positions': len(positions),
        'open_floating_pnl': combined_pnl,
        'open_loss_reach': combined_loss,
        'open_worst_loss': worst_loss,
    }


def _build_daily_loss_rows(trades, period='day'):
    """Group closed trades by period and calculate loss reach / drawdown analytics."""
    import collections
    import datetime as _dt

    period_mode = str(period or 'day').lower().strip()
    if period_mode not in ('day', 'week', 'month'):
        period_mode = 'day'

    def _trade_day(t):
        raw_date = t.get('date') or (t.get('close_time', '')[:10])
        if not raw_date:
            return None
        ds = str(raw_date)[:10]
        try:
            _dt.date.fromisoformat(ds)
            return ds
        except Exception:
            return None

    def _period_key(day_str):
        d = _dt.date.fromisoformat(day_str)
        if period_mode == 'week':
            iso = d.isocalendar()
            return f"{iso[0]}-W{iso[1]:02d}"
        if period_mode == 'month':
            return d.strftime('%Y-%m')
        return day_str

    grouped = collections.defaultdict(list)
    for t in trades:
        try:
            d = _trade_day(t)
            if not d:
                continue
            grouped[_period_key(d)].append(t)
        except Exception:
            continue

    rows = []
    all_trade_loss_reaches = []
    for key in sorted(grouped.keys(), reverse=True):
        day_trades = sorted(grouped[key], key=lambda x: str(x.get('close_time', '')))
        cum = 0.0
        peak = 0.0
        max_drawdown = 0.0
        losses = []
        wins = 0
        loss_count = 0
        win_count = 0
        loss_streak = 0
        max_loss_streak = 0
        equity_curve = []

        period_peak_trade_loss_reach = 0.0
        for t in day_trades:
            pnl = float(t.get('profit', 0) or 0)
            cum += pnl
            equity_curve.append(cum)
            if cum > peak:
                peak = cum
            drawdown = cum - peak
            if drawdown < max_drawdown:
                max_drawdown = drawdown

            if pnl > 0:
                wins += 1
                win_count += 1
                loss_streak = 0
            elif pnl < 0:
                losses.append(pnl)
                loss_count += 1
                loss_streak += 1
                if loss_streak > max_loss_streak:
                    max_loss_streak = loss_streak
            else:
                loss_streak = 0

            tr_loss_reach = t.get('max_loss_reach', None)
            if tr_loss_reach is None:
                tr_loss_reach = abs(min(0.0, pnl))
            else:
                tr_loss_reach = abs(float(tr_loss_reach or 0.0))
            all_trade_loss_reaches.append(tr_loss_reach)
            if tr_loss_reach > period_peak_trade_loss_reach:
                period_peak_trade_loss_reach = tr_loss_reach

        total = len(day_trades)
        total_pnl = round(sum(float(t.get('profit', 0) or 0) for t in day_trades), 2)
        loss_total = round(sum(losses), 2) if losses else 0.0
        worst_loss = round(min(losses), 2) if losses else 0.0
        recovery = round((total_pnl / abs(max_drawdown)) * 100, 1) if max_drawdown < 0 else 0.0
        loss_reach = round(abs(max_drawdown), 2)
        rows.append({
            'period': key,
            'trades': total,
            'wins': win_count,
            'losses': loss_count,
            'win_rate': round((win_count / total) * 100, 1) if total else 0.0,
            'total_pnl': total_pnl,
            'loss_total': loss_total,
            'worst_loss': worst_loss,
            'max_drawdown': round(max_drawdown, 2),
            'loss_reach': loss_reach,
            'peak_trade_loss_reach': round(period_peak_trade_loss_reach, 2),
            'max_loss_streak': max_loss_streak,
            'recovery_efficiency': recovery,
        })

    ever_high_loss_reach = round(max(all_trade_loss_reaches), 2) if all_trade_loss_reaches else 0.0
    risk_anchor = max(
        abs(round(min((r['max_drawdown'] for r in rows), default=0.0), 2)),
        abs(round(min((r['worst_loss'] for r in rows), default=0.0), 2)),
        ever_high_loss_reach,
    )
    # Capital guidance: size account so one worst observed adverse move is about 5% of equity.
    recommended_start_capital = round(max(100.0, risk_anchor / 0.05), 2) if risk_anchor > 0 else 0.0
    recommended_start_capital_safe = round(max(100.0, risk_anchor / 0.02), 2) if risk_anchor > 0 else 0.0

    summary = {
        'period_mode': period_mode,
        'periods': len(rows),
        'total_trades': sum(r['trades'] for r in rows),
        'total_pnl': round(sum(r['total_pnl'] for r in rows), 2),
        'total_loss_reach': round(sum(r['loss_reach'] for r in rows), 2),
        'worst_period_loss_reach': round(min((r['max_drawdown'] for r in rows), default=0.0), 2),
        'worst_single_loss': round(min((r['worst_loss'] for r in rows), default=0.0), 2),
        'ever_high_loss_reach': ever_high_loss_reach,
        'risk_anchor': round(risk_anchor, 2),
        'recommended_start_capital': recommended_start_capital,
        'recommended_start_capital_safe': recommended_start_capital_safe,
        'avg_recovery_efficiency': round(sum(r['recovery_efficiency'] for r in rows) / len(rows), 1) if rows else 0.0,
    }
    return rows, summary


def _append_to_archive(record: dict):
    """
    Append a single closed trade to the permanent cross-day archive.
    The archive is never cleared by a New-Day reset — it accumulates indefinitely
    and is used to generate daily / weekly / monthly performance reports.
    """
    import copy, threading
    rec = copy.copy(record)
    # Ensure a date key exists for fast grouping
    ct = rec.get('close_time', '')
    rec.setdefault('date', ct[:10] if ct else datetime.now().strftime('%Y-%m-%d'))
    with _archive_lock:
        try:
            if os.path.exists(_ARCHIVE_FILE):
                with open(_ARCHIVE_FILE) as f:
                    data = json.load(f)
            else:
                data = {'trades': []}
            # Deduplicate by ticket
            existing = {t['ticket'] for t in data['trades']}
            if rec.get('ticket') not in existing:
                data['trades'].append(rec)
                with open(_ARCHIVE_FILE, 'w') as f:
                    json.dump(data, f, default=str)
        except Exception:
            pass


def _email_cfg(name, default=None):
    try:
        return EMAIL_CONFIG.get(name, default)
    except Exception:
        return default


def _email_notifications_enabled():
    return bool(_email_cfg('enabled', False) and _email_cfg('smtp_host') and _email_cfg('to_email'))


def _send_email(subject, body):
    if not _email_notifications_enabled():
        return False

    host = str(_email_cfg('smtp_host', '') or '').strip()
    port = int(_email_cfg('smtp_port', 587) or 587)
    username = str(_email_cfg('username', '') or '').strip()
    password = str(_email_cfg('password', '') or '').strip()
    from_email = str(_email_cfg('from_email', '') or username).strip() or username
    to_email = str(_email_cfg('to_email', '') or '').strip()
    use_tls = bool(_email_cfg('use_tls', True))
    use_ssl = bool(_email_cfg('use_ssl', False))

    if not host or not to_email:
        return False

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = to_email
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                if use_tls:
                    smtp.starttls(context=context)
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        return True
    except Exception as exc:
        add_log(f"Email send failed: {exc}", 'WARNING')
        return False


def _queue_email(subject, body):
    if not _email_notifications_enabled():
        return

    def _runner():
        with _notification_lock:
            _send_email(subject, body)

    threading.Thread(target=_runner, daemon=True).start()


def _format_trade_close_email(record):
    pnl = float(record.get('profit', 0) or 0)
    lr = float(record.get('max_loss_reach', 0) or 0)
    bot = record.get('bot', 'SMC') or 'SMC'
    side = str(record.get('type', '')).upper()
    symbol = record.get('symbol', '')
    subject = f"Trade closed: {symbol} {side} {'+' if pnl >= 0 else ''}${pnl:.2f}"
    body = (
        f"Trade closed\n\n"
        f"Bot: {bot}\n"
        f"Symbol: {symbol}\n"
        f"Side: {side}\n"
        f"Volume: {record.get('volume', 0)}\n"
        f"Entry: {record.get('open_price', 0)}\n"
        f"Close: {record.get('close_price', 0)}\n"
        f"Profit: {pnl:+.2f}\n"
        f"Max loss reach: -${lr:.2f}\n"
        f"Result: {record.get('result', '')}\n"
        f"Open time: {record.get('open_time', '')}\n"
        f"Close time: {record.get('close_time', '')}\n"
    )
    return subject, body


def _notify_trade_close(record):
    subject, body = _format_trade_close_email(record)
    _queue_email(subject, body)
    pnl = float(record.get('profit', 0) or 0)
    symbol = str(record.get('symbol', '') or '')
    side = str(record.get('type', '') or '').upper()
    _queue_push(
        f"{symbol} {side} closed {pnl:+.2f} | LR -${float(record.get('max_loss_reach', 0) or 0):.2f}",
        title='Trade Closed',
    )


def _format_daily_summary_email(day_key, open_loss_metrics=None):
    trades = [t for t in bot_state.get('trade_history', []) if str(t.get('close_time', '')).startswith(day_key)]
    total = len(trades)
    wins = sum(1 for t in trades if float(t.get('profit', 0) or 0) > 0)
    losses = sum(1 for t in trades if float(t.get('profit', 0) or 0) < 0)
    pnl = round(sum(float(t.get('profit', 0) or 0) for t in trades), 2)
    worst_lr = round(max((float(t.get('max_loss_reach', 0) or 0) for t in trades), default=0.0), 2)
    open_loss = float((open_loss_metrics or {}).get('open_loss_reach', 0.0) or 0.0)
    subject = f"Daily summary {day_key}: {total} trades, {'+' if pnl >= 0 else ''}${pnl:.2f}"
    body = (
        f"Daily summary for {day_key}\n\n"
        f"Trades: {total}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Net P&L: {pnl:+.2f}\n"
        f"Worst trade max loss reach: ${worst_lr:.2f}\n"
        f"Open loss reach now: -${abs(open_loss):.2f}\n"
        f"Account: {bot_state.get('account', {})}\n"
    )
    return subject, body


def _maybe_send_daily_summary_email(open_loss_metrics=None):
    if not _email_notifications_enabled():
        return
    daily_cfg = str(_email_cfg('daily_summary_time_utc', '23:55') or '23:55')
    try:
        target_h, target_m = [int(x) for x in daily_cfg.split(':', 1)]
    except Exception:
        target_h, target_m = 23, 55
    now_utc = datetime.utcnow()
    day_key = now_utc.strftime('%Y-%m-%d')
    if now_utc.hour < target_h or (now_utc.hour == target_h and now_utc.minute < target_m):
        return
    if bot_state.get('notifications', {}).get('daily_summary_sent_for') == day_key:
        return
    subject, body = _format_daily_summary_email(day_key, open_loss_metrics)
    bot_state.setdefault('notifications', {})['daily_summary_sent_for'] = day_key
    _queue_email(subject, body)
    _queue_push(body, title=f'Daily Summary {day_key}')


def _build_daily_report_html(day_key):
    trades = [t for t in bot_state.get('trade_history', []) if str(t.get('close_time', '')).startswith(day_key)]
    stats = _performance_stats(trades)
    pnl_class = 'good' if stats['total_profit'] >= 0 else 'bad'
    rows = ''.join(
        f"<tr><td>{str(t.get('close_time', ''))[11:19]}</td><td>{t.get('symbol','')}</td><td>{str(t.get('type','')).upper()}</td><td>{t.get('volume',0)}</td><td>{float(t.get('profit',0) or 0):+.2f}</td><td>-${float(t.get('max_loss_reach',0) or 0):.2f}</td></tr>"
        for t in trades
    )
    if not rows:
        rows = '<tr><td colspan="6">No trades closed for this day.</td></tr>'
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Daily Report</title>'
        '<style>body{font-family:Segoe UI,Arial,sans-serif;background:#0d1117;color:#ddd;padding:16px}'
        'table{border-collapse:collapse;width:100%}th,td{border:1px solid #2a2f3a;padding:6px;text-align:left}'
        'th{background:#161b22;color:#9ecbff}.good{color:#2ecc71}.bad{color:#ff6b6b}</style></head><body>'
        f'<h2>Scalping Bot Daily Report - {day_key}</h2>'
        f'<p>Trades: <b>{stats["trades"]}</b> | Win Rate: <b>{stats["win_rate"]}%</b> | Total PnL: <b class="{pnl_class}">{stats["total_profit"]:+.2f}</b></p>'
        '<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Lots</th><th>PnL</th><th>Max Loss Reach</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></body></html>'
    )


def _maybe_send_daily_auto_report():
    if not _email_notifications_enabled():
        return
    daily_cfg = str(_email_cfg('daily_summary_time_utc', '23:55') or '23:55')
    try:
        target_h, target_m = [int(x) for x in daily_cfg.split(':', 1)]
    except Exception:
        target_h, target_m = 23, 55
    now_utc = datetime.utcnow()
    day_key = now_utc.strftime('%Y-%m-%d')
    if now_utc.hour < target_h or (now_utc.hour == target_h and now_utc.minute < target_m):
        return
    if bot_state.get('notifications', {}).get('daily_report_sent_for') == day_key:
        return
    html = _build_daily_report_html(day_key)
    text = f"Auto daily report generated for {day_key}. Open the dashboard report export for full table."
    subject = f"Auto Daily Report {day_key}"
    _queue_email(subject, text)
    _queue_push(text, title=subject)
    report_path = os.path.join(_DATA_DIR, f'daily_report_{day_key}.html')
    try:
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(html)
    except Exception:
        pass
    bot_state.setdefault('notifications', {})['daily_report_sent_for'] = day_key


def _maybe_send_risk_alert_email(open_loss_metrics):
    if not _email_notifications_enabled():
        return
    loss_reach = abs(float((open_loss_metrics or {}).get('open_loss_reach', 0.0) or 0.0))
    if loss_reach <= 0:
        return

    thresholds = _email_cfg('risk_alert_thresholds', [25.0, 50.0, 100.0]) or []
    try:
        thresholds = sorted({float(x) for x in thresholds if float(x) > 0})
    except Exception:
        thresholds = [25.0, 50.0, 100.0]

    sent_map = bot_state.setdefault('notifications', {}).setdefault('risk_alert_buckets_sent_for', {})
    day_key = datetime.utcnow().strftime('%Y-%m-%d')
    sent_today = sent_map.setdefault(day_key, [])

    for threshold in thresholds:
        if loss_reach < threshold:
            continue
        bucket = str(threshold)
        if bucket in sent_today:
            continue
        subject = f"Risk alert: open loss reach ${loss_reach:.2f}"
        body = (
            f"Open risk alert\n\n"
            f"Open loss reach: -${loss_reach:.2f}\n"
            f"Open positions: {open_loss_metrics.get('open_positions', 0) if open_loss_metrics else 0}\n"
            f"Open floating P&L: {float((open_loss_metrics or {}).get('open_floating_pnl', 0.0) or 0.0):+.2f}\n"
            f"Worst open loss: -${abs(float((open_loss_metrics or {}).get('open_worst_loss', 0.0) or 0.0)):.2f}\n"
            f"Threshold crossed: ${threshold:.2f}\n"
        )
        sent_today.append(bucket)
        _queue_email(subject, body)
        _queue_push(body, title=subject)


def _notification_watchdog():
    while True:
        try:
            metrics = _open_trade_loss_metrics()
            _maybe_send_risk_alert_email(metrics)
            _maybe_send_daily_summary_email(metrics)
            _maybe_send_daily_auto_report()
        except Exception:
            pass
        time.sleep(60)


def _start_notification_watchdog():
    global _notification_thread_started
    if _notification_thread_started:
        return
    _notification_thread_started = True
    threading.Thread(target=_notification_watchdog, daemon=True).start()


def _watchdog_status_snapshot():
    now = time.time()
    hb = bot_state.get('watchdogs', {}).get('heartbeat', {}) or {}
    stale_after = 120
    return {
        'smc': {
            'running_flag': bool(bot_state.get('running', False)),
            'last_heartbeat_age_sec': (now - hb.get('smc')) if hb.get('smc') else None,
            'stale': bool(bot_state.get('running', False) and hb.get('smc') and (now - hb.get('smc') > stale_after)),
            'thread_alive': bool(bot_thread and getattr(bot_thread, 'is_alive', lambda: False)()),
        },
        'gold': {
            'running_flag': bool(gold_state.get('running', False)),
            'last_heartbeat_age_sec': (now - hb.get('gold')) if hb.get('gold') else None,
            'stale': bool(gold_state.get('running', False) and hb.get('gold') and (now - hb.get('gold') > stale_after)),
            'thread_alive': bool(gold_thread and getattr(gold_thread, 'is_alive', lambda: False)()),
        },
        'daytrade': {
            'running_flag': bool(daytrade_state.get('running', False)),
            'last_heartbeat_age_sec': (now - hb.get('daytrade')) if hb.get('daytrade') else None,
            'stale': bool(daytrade_state.get('running', False) and hb.get('daytrade') and (now - hb.get('daytrade') > stale_after)),
            'thread_alive': bool(daytrade_thread and getattr(daytrade_thread, 'is_alive', lambda: False)()),
        },
    }


def _session_and_broker_watchdog():
    while True:
        try:
            wd = bot_state.setdefault('watchdogs', {})
            flags = wd.setdefault('session_alert_sent', {'smc': False, 'gold': False, 'daytrade': False})
            snap = _watchdog_status_snapshot()

            for key in ('smc', 'gold', 'daytrade'):
                is_problem = bool(snap[key].get('stale') or (snap[key].get('running_flag') and not snap[key].get('thread_alive')))
                if is_problem and not flags.get(key):
                    msg = (
                        f"{key.upper()} watchdog triggered. Running={snap[key]['running_flag']} "
                        f"ThreadAlive={snap[key]['thread_alive']} AgeSec={snap[key]['last_heartbeat_age_sec']}"
                    )
                    add_log(msg, 'WARNING')
                    _queue_email(f"Session watchdog alert: {key.upper()}", msg)
                    _queue_push(msg, title=f"Session Watchdog: {key.upper()}")
                    flags[key] = True
                elif not is_problem:
                    flags[key] = False

            broker_ok = False
            try:
                if _mt5_ensure() and mt5.account_info():
                    broker_ok = True
            except Exception:
                broker_ok = False
            wd['last_broker_ok'] = _now_iso() if broker_ok else wd.get('last_broker_ok')
            if (not broker_ok) and (not wd.get('broker_alert_sent', False)):
                msg = 'Broker connection watchdog: MT5 connection appears down.'
                add_log(msg, 'WARNING')
                _queue_email('Broker connection watchdog alert', msg)
                _queue_push(msg, title='Broker Watchdog')
                wd['broker_alert_sent'] = True
            if broker_ok:
                wd['broker_alert_sent'] = False
        except Exception:
            pass
        time.sleep(30)


def _start_session_watchdog():
    global _watchdog_thread_started
    if _watchdog_thread_started:
        return
    _watchdog_thread_started = True
    threading.Thread(target=_session_and_broker_watchdog, daemon=True).start()


def _load_history_from_disk(current_login=None):
    """Load today's trade history from disk (survives restarts)."""
    try:
        if not os.path.exists(_HISTORY_FILE):
            return
        with open(_HISTORY_FILE) as f:
            payload = json.load(f)
        # Reject if wrong day
        if payload.get('date') != datetime.now().strftime('%Y-%m-%d'):
            return
        # Reject if wrong account (once we know which account we're on)
        saved_login = payload.get('account')
        if current_login and saved_login and int(saved_login) != int(current_login):
            return  # stale — different account
        trades = payload.get('trades', [])
        existing_ids = {t['ticket'] for t in bot_state['trade_history']}
        added = 0
        for t in trades:
            if t['ticket'] not in existing_ids:
                # Honour the New-Day reset cutoff — skip trades closed before it
                if _reset_cutoff_time:
                    ct = t.get('close_time', '')
                    if ct:
                        try:
                            ct_dt = datetime.fromisoformat(str(ct)[:19])
                            if ct_dt < _reset_cutoff_time:
                                continue
                        except Exception:
                            pass
                bot_state['trade_history'].append(t)
                added += 1
        if added:
            bot_state['trade_history'].sort(key=lambda x: x.get('close_time', ''), reverse=True)
            bot_state['stats']['session_pnl'] = sum(
                t.get('profit', 0) for t in bot_state['trade_history']
            )
            saved_stats = payload.get('stats', {})
            disk_opened = saved_stats.get('trades_opened', 0)
            if disk_opened > bot_state['stats']['trades_opened']:
                bot_state['stats']['trades_opened'] = disk_opened
    except Exception:
        pass


def _save_daily_goal_to_disk():
    """Persist daily goal state so it survives restarts."""
    try:
        login = bot_state['account']['login'] if bot_state.get('account') else None
        payload = {
            'date':              datetime.now().strftime('%Y-%m-%d'),
            'account':           login,
            'daily_goal_state':  {
                k: str(v) if hasattr(v, 'isoformat') else v
                for k, v in daily_goal_state.items()
            },
            # Persist the New-Day reset cutoff so it survives process restarts
            'reset_cutoff_time': _reset_cutoff_time.isoformat() if _reset_cutoff_time else None,
        }
        with open(_DAILY_GOAL_FILE, 'w') as f:
            json.dump(payload, f, default=str)
    except Exception:
        pass


def _load_daily_goal_from_disk(current_login=None):
    """Restore daily goal state from disk if it's from today and same account."""
    global daily_goal_state, _reset_cutoff_time
    try:
        if not os.path.exists(_DAILY_GOAL_FILE):
            return
        with open(_DAILY_GOAL_FILE) as f:
            payload = json.load(f)
        if payload.get('date') != datetime.now().strftime('%Y-%m-%d'):
            return   # stale — new day
        saved_login = payload.get('account')
        if current_login and saved_login and int(saved_login) != int(current_login):
            return   # stale — different account
        # Restore the New-Day reset cutoff so pre-reset MT5 deals stay filtered
        # after a process restart
        cutoff_str = payload.get('reset_cutoff_time')
        if cutoff_str:
            try:
                _reset_cutoff_time = datetime.fromisoformat(str(cutoff_str))
            except Exception:
                pass
        saved = payload.get('daily_goal_state', {})
        if saved.get('start_balance'):
            daily_goal_state['start_balance'] = float(saved['start_balance'])
        if saved.get('closed_profit') is not None:
            daily_goal_state['closed_profit'] = float(saved['closed_profit'])
        daily_goal_state['goal_reached'] = bool(saved.get('goal_reached', False))
        import datetime as dt_mod
        daily_goal_state['start_date'] = datetime.now().date()
        bot_state['daily_goal']['goal_reached'] = daily_goal_state['goal_reached']
    except Exception:
        pass


def _validate_account_data(login):
    """
    Called immediately after MT5 login is confirmed.
    Clears any in-memory history / goal state that belongs to a different account,
    then re-loads fresh from disk (now that we know the correct login).
    """
    global daily_goal_state
    # Clear stale in-memory trades if they came from a different account
    existing_file_login = None
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE) as f:
                existing_file_login = json.load(f).get('account')
    except Exception:
        pass

    if existing_file_login and int(existing_file_login) != int(login):
        # Different account on disk — wipe everything and start clean
        bot_state['trade_history'] = []
        bot_state['stats']['session_pnl'] = 0
        bot_state['stats']['trades_opened'] = 0
        _open_ticket_map.clear()
        _all_positions_snapshot.clear()
        _trade_loss_tracker.clear()
        daily_goal_state['start_balance'] = None
        daily_goal_state['start_date']    = None
        daily_goal_state['goal_reached']  = False
        daily_goal_state['closed_profit'] = 0.0
        bot_state['daily_goal']['goal_reached'] = False
        try:
            os.remove(_HISTORY_FILE)
        except Exception:
            pass
        try:
            os.remove(_DAILY_GOAL_FILE)
        except Exception:
            pass
        add_log(f"Account changed to {login} — history cleared", 'INFO')

    # Now reload with the correct login
    _load_history_from_disk(current_login=login)
    _load_daily_goal_from_disk(current_login=login)


# Load persisted data at import time so it's ready before any route is called
_load_history_from_disk()
_load_daily_goal_from_disk()


def add_log(message, level='INFO'):
    """Add a log message"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    bot_state['logs'].insert(0, {
        'time': timestamp,
        'level': level,
        'message': message
    })
    bot_state['logs'] = bot_state['logs'][:100]


def _now_iso():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def _load_config_history_from_disk():
    try:
        if not os.path.exists(_CONFIG_HISTORY_FILE):
            return
        with open(_CONFIG_HISTORY_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f) or {}
        rows = payload.get('history', []) or []
        if isinstance(rows, list):
            bot_state['config_history'] = rows[:300]
    except Exception:
        pass


def _save_config_history_to_disk():
    try:
        with _config_history_lock:
            payload = {'history': bot_state.get('config_history', [])[:300]}
            with open(_CONFIG_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, default=str)
    except Exception:
        pass


def _load_notification_settings_from_disk():
    try:
        if not os.path.exists(_NOTIFY_SETTINGS_FILE):
            return
        with open(_NOTIFY_SETTINGS_FILE, 'r', encoding='utf-8') as f:
            payload = json.load(f) or {}

        email_cfg = payload.get('email_config', {}) or {}
        for k in (
            'enabled', 'smtp_host', 'smtp_port', 'use_tls', 'use_ssl',
            'username', 'password', 'from_email', 'to_email',
            'daily_summary_time_utc', 'risk_alert_thresholds',
        ):
            if k in email_cfg:
                EMAIL_CONFIG[k] = email_cfg[k]

        push_cfg = payload.get('push_config', {}) or {}
        bot_state.setdefault('push_config', {})
        for k in ('enabled', 'telegram_bot_token', 'telegram_chat_id', 'discord_webhook_url'):
            if k in push_cfg:
                bot_state['push_config'][k] = push_cfg[k]
    except Exception:
        pass


def _save_notification_settings_to_disk():
    try:
        payload = {
            'email_config': {
                'enabled': EMAIL_CONFIG.get('enabled', False),
                'smtp_host': EMAIL_CONFIG.get('smtp_host', ''),
                'smtp_port': EMAIL_CONFIG.get('smtp_port', 587),
                'use_tls': EMAIL_CONFIG.get('use_tls', True),
                'use_ssl': EMAIL_CONFIG.get('use_ssl', False),
                'username': EMAIL_CONFIG.get('username', ''),
                'password': EMAIL_CONFIG.get('password', ''),
                'from_email': EMAIL_CONFIG.get('from_email', ''),
                'to_email': EMAIL_CONFIG.get('to_email', ''),
                'daily_summary_time_utc': EMAIL_CONFIG.get('daily_summary_time_utc', '23:55'),
                'risk_alert_thresholds': EMAIL_CONFIG.get('risk_alert_thresholds', [25.0, 50.0, 100.0]),
            },
            'push_config': {
                'enabled': bot_state.get('push_config', {}).get('enabled', False),
                'telegram_bot_token': bot_state.get('push_config', {}).get('telegram_bot_token', ''),
                'telegram_chat_id': bot_state.get('push_config', {}).get('telegram_chat_id', ''),
                'discord_webhook_url': bot_state.get('push_config', {}).get('discord_webhook_url', ''),
            },
        }
        with open(_NOTIFY_SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(payload, f, default=str)
    except Exception:
        pass


def _record_config_change(area, before, after, actor='dashboard'):
    if before == after:
        return
    row = {
        'time_utc': _now_iso(),
        'area': str(area or 'unknown'),
        'actor': str(actor or 'dashboard'),
        'before': before,
        'after': after,
    }
    with _config_history_lock:
        bot_state.setdefault('config_history', []).insert(0, row)
        bot_state['config_history'] = bot_state['config_history'][:300]
    _save_config_history_to_disk()


def _watchdog_heartbeat(name):
    try:
        bot_state.setdefault('watchdogs', {}).setdefault('heartbeat', {})[name] = time.time()
    except Exception:
        pass


def _format_bot_label(bot_key):
    mapping = {
        '': '5-Min SMC',
        'Gold1M': 'Gold 1-Min',
        'GoldDay': 'Gold Day',
        'BTC': 'BTC',
    }
    return mapping.get(bot_key, bot_key or '5-Min SMC')


def _trade_bot_key(trade):
    sym = str(trade.get('symbol', '')).upper()
    if ('BTC' in sym) or ('XBT' in sym):
        return 'BTC'
    return trade.get('bot', '')


def _performance_stats(trades):
    profits = [float(t.get('profit', 0) or 0) for t in (trades or [])]
    n = len(profits)
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    total = round(sum(profits), 2)
    avg = (sum(profits) / n) if n else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (999.0 if wins else 0.0)

    # Per-trade Sharpe approximation (mean / stddev * sqrt(N))
    sharpe = 0.0
    if n >= 2:
        mean = sum(profits) / n
        var = sum((p - mean) ** 2 for p in profits) / max(1, n - 1)
        std = var ** 0.5
        if std > 0:
            sharpe = (mean / std) * (n ** 0.5)

    # Max drawdown from cumulative closed PnL
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in profits:
        eq += p
        if eq > peak:
            peak = eq
        dd = eq - peak
        if dd < max_dd:
            max_dd = dd

    return {
        'trades': n,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round((len(wins) / n) * 100, 1) if n else 0.0,
        'total_profit': round(total, 2),
        'avg_profit': round(avg, 2),
        'profit_factor': round(profit_factor, 2),
        'sharpe_est': round(sharpe, 2),
        'max_drawdown': round(max_dd, 2),
        'expectancy': round(avg, 2),
        'best': round(max(profits), 2) if profits else 0.0,
        'worst': round(min(profits), 2) if profits else 0.0,
    }


def _send_push(message, title='Scalping Bot Alert'):
    cfg = bot_state.get('push_config', {}) or {}
    if not cfg.get('enabled'):
        return False

    sent_any = False
    try:
        token = str(cfg.get('telegram_bot_token', '') or '').strip()
        chat_id = str(cfg.get('telegram_chat_id', '') or '').strip()
        if token and chat_id:
            payload = json.dumps({'chat_id': chat_id, 'text': f"{title}\n{message}"}).encode('utf-8')
            req = urllib.request.Request(
                url=f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=12):
                sent_any = True
    except Exception as exc:
        add_log(f"Telegram push failed: {exc}", 'WARNING')

    try:
        webhook = str(cfg.get('discord_webhook_url', '') or '').strip()
        if webhook:
            payload = json.dumps({'content': f"**{title}**\n{message}"}).encode('utf-8')
            req = urllib.request.Request(
                url=webhook,
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=12):
                sent_any = True
    except Exception as exc:
        add_log(f"Discord push failed: {exc}", 'WARNING')

    return sent_any


def _queue_push(message, title='Scalping Bot Alert'):
    cfg = bot_state.get('push_config', {}) or {}
    if not cfg.get('enabled'):
        return

    def _runner():
        _send_push(message, title=title)

    threading.Thread(target=_runner, daemon=True).start()


_load_config_history_from_disk()
_load_notification_settings_from_disk()


def get_mt5_data(symbol):
    """Get market data for a symbol"""
    if not _mt5_ensure():
        return None, None, None

    resolved_symbol = _resolve_symbol(symbol)
    if not resolved_symbol:
        return None, None, None

    sym_info = mt5.symbol_info(resolved_symbol)
    if sym_info is None:
        return None, None, None

    if not sym_info.visible:
        mt5.symbol_select(resolved_symbol, True)

    tick = mt5.symbol_info_tick(resolved_symbol)
    if tick is None:
        # Retry once after forcing selection.
        mt5.symbol_select(resolved_symbol, True)
        tick = mt5.symbol_info_tick(resolved_symbol)

    if tick is not None:
        bid = float(tick.bid)
        ask = float(tick.ask)
    else:
        # Fallback for brokers where tick stream lags right after symbol selection.
        bid = float(getattr(sym_info, 'bid', 0.0) or 0.0)
        ask = float(getattr(sym_info, 'ask', 0.0) or 0.0)

    if bid <= 0 and ask <= 0:
        return None, None, None
    if bid <= 0:
        bid = ask
    if ask <= 0:
        ask = bid

    spread = max(0.0, ask - bid)
    price_info = {
        'symbol': resolved_symbol,
        'bid': bid,
        'ask': ask,
        'spread': round(spread, 5),
        'spread_points': round(spread / sym_info.point) if sym_info and sym_info.point else 0,
    }

    rates = mt5.copy_rates_from_pos(resolved_symbol, mt5.TIMEFRAME_M5, 0, 200)
    if rates is None:
        return price_info, None, []
    
    import pandas as pd
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df.rename(columns={'tick_volume': 'volume'})
    
    positions = mt5.positions_get(symbol=resolved_symbol)
    pos_list = []
    if positions:
        for pos in positions:
            pos_list.append({
                'ticket': pos.ticket,
                'type': 'buy' if pos.type == mt5.ORDER_TYPE_BUY else 'sell',
                'volume': pos.volume,
                'open_price': pos.price_open,
                'current_price': pos.price_current,
                'profit': pos.profit,
                'sl': pos.sl,
                'tp': pos.tp,
                'symbol': pos.symbol,
            })
    
    return price_info, df, pos_list


def open_trade(symbol, order_type, volume, sl, tp):
    """Open a trade"""
    # Safety guard — never place an order if SMC bot is not running
    if not bot_state.get('running'):
        add_log("Order blocked: SMC bot is not running", "WARNING")
        return None
    # Global kill-switch
    if not trading_enabled:
        add_log("Order blocked: Trading is DISABLED (use the header toggle)", "WARNING")
        return None
    if not _mt5_ensure():
        return None

    symbol = _resolve_symbol(symbol)
    if not symbol:
        return None
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        add_log(f"Symbol {symbol} not found", "ERROR")
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
        add_log(f"Order blocked: no valid price for {symbol}", "WARNING")
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
        "comment": f"Scalper {symbol}",
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
        add_log(f"Order failed: {result.comment if result else 'Unknown'}", "ERROR")
        return None


def close_position(position):
    """Close a position"""
    tick = mt5.symbol_info_tick(position.symbol)
    if not tick:
        return False
    
    price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
        "position": position.ticket,
        "price": price,
        "deviation": 20,
        "magic": int(getattr(position, 'magic', 234000)),
        "comment": "Daily goal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    return result and result.retcode == mt5.TRADE_RETCODE_DONE


# ── In-memory store of open tickets so we can detect when they close ──
_open_ticket_map = {}   # ticket -> {symbol, type, volume, open_price, sl, tp, open_time}
_all_positions_snapshot = {}  # ticket -> full position info + last_profit (fallback history tracker)
_trade_loss_tracker = {}  # ticket -> {'min_profit': float}


def _update_trade_loss_reach(ticket, current_profit):
    """Track the worst floating loss seen for a live trade ticket."""
    try:
        t = int(ticket)
        p = float(current_profit)
    except Exception:
        return
    node = _trade_loss_tracker.get(t)
    if node is None:
        _trade_loss_tracker[t] = {'min_profit': p}
        return
    if p < float(node.get('min_profit', 0.0)):
        node['min_profit'] = p


def _consume_trade_loss_reach(ticket, snapshot_min_profit=None):
    """Return max loss reach (absolute $) for a closed ticket and remove tracker state."""
    try:
        t = int(ticket)
    except Exception:
        return 0.0
    tracked = _trade_loss_tracker.pop(t, None)
    min_profit = None
    if tracked is not None:
        try:
            min_profit = float(tracked.get('min_profit', 0.0))
        except Exception:
            min_profit = None
    if snapshot_min_profit is not None:
        try:
            snap_min = float(snapshot_min_profit)
            min_profit = snap_min if min_profit is None else min(min_profit, snap_min)
        except Exception:
            pass
    if min_profit is None:
        return 0.0
    return round(abs(min(0.0, min_profit)), 2)


def _current_trade_loss_reach(ticket):
    """Return the current max loss reach (absolute $) for an open ticket without consuming it."""
    try:
        t = int(ticket)
    except Exception:
        return 0.0

    min_profit = None
    tracked = _trade_loss_tracker.get(t)
    if tracked is not None:
        try:
            min_profit = float(tracked.get('min_profit', 0.0))
        except Exception:
            min_profit = None

    for bucket in (_all_positions_snapshot, _gold_positions, _day_positions):
        snap = bucket.get(t) if isinstance(bucket, dict) else None
        if not isinstance(snap, dict):
            continue
        try:
            snap_min = float(snap.get('min_profit_seen', 0.0))
        except Exception:
            continue
        min_profit = snap_min if min_profit is None else min(min_profit, snap_min)

    if min_profit is None:
        return 0.0
    return round(abs(min(0.0, min_profit)), 2)

def _record_open_trade(ticket, symbol, order_type, volume, price, sl, tp):
    """Track a newly opened trade ticket."""
    _update_trade_loss_reach(ticket, 0.0)
    _open_ticket_map[ticket] = {
        'ticket':     int(ticket),
        'symbol':     symbol,
        'type':       order_type,
        'volume':     float(volume),
        'open_price': float(price),
        'sl':         float(sl) if sl else 0.0,
        'tp':         float(tp) if tp else 0.0,
        'open_time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _build_deal_maps(from_date, to_date):
    """
    Fetch deals in [from_date, to_date] and return (ins, outs, inouts) dicts
    keyed by position_id.
      ins    – DEAL_ENTRY_IN  (entry=0)  : position opened
      outs   – DEAL_ENTRY_OUT (entry=1)  : position closed
      inouts – DEAL_ENTRY_INOUT (entry=2): open+close in one deal (netting / some XM configs)
    Falls back from group='*' to no-group if the wildcard returns nothing.
    """
    import datetime as dt_mod
    deals = None
    try:
        deals = mt5.history_deals_get(from_date, to_date, group='*')
    except Exception:
        pass
    if not deals:        # wildcard unsupported or truly empty — retry without group
        try:
            deals = mt5.history_deals_get(from_date, to_date)
        except Exception:
            pass
    if not deals:
        return {}, {}, {}

    ins, outs, inouts = {}, {}, {}
    for d in deals:
        if d.entry == 0:
            ins[d.position_id]    = d
        elif d.entry == 1:
            outs[d.position_id]   = d
        elif d.entry == 2:       # DEAL_ENTRY_INOUT — self-contained trade
            inouts[d.position_id] = d
    return ins, outs, inouts


def _infer_bot_from_deal(magic, out_deal=None, in_deal=None):
    """
    Map a deal to its bot label.
    Primary source: magic number.
    Fallback when magic=0: inspect the opening deal's comment.
      'GoldM1'   → 'Gold1M'
      'Scalper'  → '' (5-min SMC)
    """
    _MAGIC_BOT = {234000: '', 236000: 'Gold1M', 237000: 'GoldDay'}
    if magic in _MAGIC_BOT:
        return _MAGIC_BOT[magic]
    # Magic unknown (0 or broker-side wiped) — fall back to comment text
    for deal in (in_deal, out_deal):
        if deal is None:
            continue
        c = str(getattr(deal, 'comment', '') or '').strip()
        if 'GoldM1' in c:
            return 'Gold1M'
        if 'GoldDay' in c:
            return 'GoldDay'
        if 'Scalper' in c:
            return ''
    return ''   # treat as 5-min by default


def _deal_to_record(out_deal, in_deal=None, session_meta=None, bot=''):
    """Build a trade-history dict from closing deal + optional opening deal."""
    direction = 'buy' if out_deal.type == 1 else 'sell'  # type 1 = SELL deal closes a BUY
    return {
        'ticket':      int(out_deal.position_id),
        'symbol':      out_deal.symbol,
        'type':        direction,
        'volume':      float(out_deal.volume),
        'open_price':  float(in_deal.price) if in_deal
                       else (float(session_meta['open_price']) if session_meta else 0.0),
        'sl':          float(session_meta['sl']) if session_meta else 0.0,
        'tp':          float(session_meta['tp']) if session_meta else 0.0,
        'open_time':   datetime.fromtimestamp(in_deal.time).strftime('%Y-%m-%d %H:%M:%S')
                       if in_deal else '',
        'close_price': float(out_deal.price),
        'close_time':  datetime.fromtimestamp(out_deal.time).strftime('%Y-%m-%d %H:%M:%S'),
        'profit':      float(out_deal.profit),
        'max_loss_reach': float((session_meta or {}).get('max_loss_reach', 0.0) or 0.0),
        'result':      'WIN' if out_deal.profit > 0 else ('LOSS' if out_deal.profit < 0 else 'BE'),
        'bot':         bot,
    }


def _sync_trade_history():
    """
    Detect closed trades every 10s using TWO methods:

    Method 1 (preferred): MT5 history_deals_get — works on most brokers.
    Method 2 (fallback):  Position-snapshot comparison — works when the
                          broker's history API returns nothing (some XM configs).
                          Uses the position's last known floating profit as the
                          closed P&L (accurate when position hits TP/SL).
    """
    import datetime as dt_mod
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = today_start - dt_mod.timedelta(days=7)
    now         = datetime.now()

    existing_ids = {t['ticket'] for t in bot_state['trade_history']}
    new_records  = []

    # ── Method 1: MT5 deal history ──
    ins, outs, inouts = _build_deal_maps(week_start, now)

    for pid, out_deal in outs.items():
        if pid in existing_ids:
            continue
        if out_deal.volume == 0:
            continue
        magic = getattr(out_deal, 'magic', 0)
        in_deal = ins.get(pid)
        bot_label = _infer_bot_from_deal(magic, out_deal, in_deal)
        # Only 5-min bot trades belong in _sync_trade_history; gold/daytrade are
        # handled by their own monitor threads via _record_closed_by_sl_tp.
        if bot_label in ('Gold1M', 'GoldDay'):
            continue
        deal_dt = datetime.fromtimestamp(out_deal.time)
        if deal_dt < today_start:
            continue
        # Skip deals that closed before the last New-Day reset
        if _reset_cutoff_time and deal_dt < _reset_cutoff_time:
            continue
        session_meta = _open_ticket_map.pop(pid, None) or {}
        session_meta['max_loss_reach'] = _consume_trade_loss_reach(pid)
        new_records.append(_deal_to_record(out_deal, in_deal, session_meta, bot=''))
        existing_ids.add(pid)

    for pid, d in inouts.items():
        if pid in existing_ids:
            continue
        if d.volume == 0:
            continue
        magic = getattr(d, 'magic', 0)
        bot_label = _infer_bot_from_deal(magic, d, None)
        if bot_label in ('Gold1M', 'GoldDay'):
            continue
        d_dt = datetime.fromtimestamp(d.time)
        if d_dt < today_start:
            continue
        if _reset_cutoff_time and d_dt < _reset_cutoff_time:
            continue
        _open_ticket_map.pop(pid, None)
        sm = {'max_loss_reach': _consume_trade_loss_reach(pid)}
        new_records.append(_deal_to_record(d, session_meta=sm, bot=''))
        existing_ids.add(pid)

    # ── Method 2: Position snapshot comparison (fallback) ──
    # Any ticket that WAS in our snapshot but is no longer in open positions = closed
    current_tickets = {p.ticket for p in (mt5.positions_get() or [])}
    for ticket, snap in list(_all_positions_snapshot.items()):
        if ticket in current_tickets:
            continue  # still open — skip
        pid = ticket
        _all_positions_snapshot.pop(ticket, None)
        _open_ticket_map.pop(pid, None)
        if pid in existing_ids:
            continue  # already recorded by Method 1
        # Position closed — use last known floating profit
        profit     = snap.get('last_profit', 0.0)
        close_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        record = {
            'ticket':      int(pid),
            'symbol':      snap['symbol'],
            'type':        snap['type'],
            'volume':      snap['volume'],
            'open_price':  snap['open_price'],
            'sl':          snap['sl'],
            'tp':          snap['tp'],
            'open_time':   snap['open_time'],
            'close_price': 0.0,   # not available without history API
            'close_time':  close_time,
            'profit':      profit,
            'max_loss_reach': _consume_trade_loss_reach(pid, snap.get('min_profit_seen')),
            'result':      'WIN' if profit > 0 else ('LOSS' if profit < 0 else 'BE'),
            'bot':         '',    # _all_positions_snapshot is magic==234000 only → 5-min
        }
        new_records.append(record)
        existing_ids.add(pid)

    for record in new_records:
        bot_state['trade_history'].insert(0, record)
        _append_to_archive(record)
        _notify_trade_close(record)
        add_log(
            f"Closed {record['symbol']} {record['type'].upper()} "
            f"${record['profit']:+.2f} ({'WIN' if record['profit'] > 0 else 'LOSS'})",
            'SUCCESS' if record['profit'] > 0 else 'WARNING'
        )

    if len(bot_state['trade_history']) > 200:
        bot_state['trade_history'] = bot_state['trade_history'][:200]

    daily_goal_state['closed_profit'] = sum(
        t.get('profit', 0) for t in bot_state['trade_history']
    )
    bot_state['stats']['session_pnl'] = daily_goal_state['closed_profit']

    if new_records:
        _save_history_to_disk()
        _save_daily_goal_to_disk()


def _send_sl_modify(position, new_sl):
    """Modify the stop-loss of an open position via MT5.
    Guards against retcode 10016 (INVALID_STOPS) by:
      - skipping no-op moves (SL already at requested level)
      - validating the new SL clears the broker's stops_level distance
    """
    sym_info = mt5.symbol_info(position.symbol)
    if not sym_info:
        return False
    digits = sym_info.digits
    new_sl = round(new_sl, digits)

    # No-op: SL is already at the requested level — nothing to do
    if round(position.sl, digits) == new_sl:
        return True

    # Validate against broker minimum stop distance (prevents retcode 10016)
    tick        = mt5.symbol_info_tick(position.symbol)
    stops_level = getattr(sym_info, 'trade_stops_level', 0) or 0
    min_dist    = (stops_level + 1) * sym_info.point
    if tick:
        if position.type == mt5.ORDER_TYPE_BUY:
            if new_sl > tick.bid - min_dist:
                add_log(
                    f"SL move skipped #{position.ticket}: "
                    f"SL {new_sl:.{digits}f} within {stops_level+1} pts of bid {tick.bid:.{digits}f}",
                    'WARNING'
                )
                return False
        else:  # SELL
            if new_sl < tick.ask + min_dist:
                add_log(
                    f"SL move skipped #{position.ticket}: "
                    f"SL {new_sl:.{digits}f} within {stops_level+1} pts of ask {tick.ask:.{digits}f}",
                    'WARNING'
                )
                return False

    request = {
        'action':   mt5.TRADE_ACTION_SLTP,
        'symbol':   position.symbol,
        'position': int(position.ticket),
        'sl':       new_sl,
        'tp':       float(position.tp),
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return True
    rc = result.retcode if result else -1
    add_log(f"SL modify failed #{position.ticket} retcode={rc}", 'WARNING')
    return False


def _move_positions_to_breakeven():
    """
    trail_and_stop action — called every cycle after daily goal is hit.

    Goal already banked via closed trades.  For any REMAINING open positions:
      Profitable → close immediately to bank the profit now.
      Losing     → move SL to entry price (breakeven) so the worst outcome
                   is $0 extra loss on that trade; let it ride to TP.
    """
    PROFIT_LOCK_RATIO = 0.70   # for trailing: lock 70% of profit as SL moves up

    positions = mt5.positions_get()
    if not positions:
        return

    for pos in positions:
        tick     = mt5.symbol_info_tick(pos.symbol)
        sym_info = mt5.symbol_info(pos.symbol)
        if not tick or not sym_info:
            continue
        digits = sym_info.digits
        entry  = pos.price_open

        if pos.type == mt5.ORDER_TYPE_BUY:
            current     = tick.bid
            profit_dist = current - entry

            if profit_dist > sym_info.point * 5:      # profitable — close and bank it
                if close_position(pos):
                    add_log(
                        f"Banked profit: {pos.symbol} BUY #{pos.ticket} "
                        f"closed at {current:.{digits}f} (goal hit)",
                        'SUCCESS'
                    )
            else:                                     # losing — move SL to breakeven
                # For BUY, entry must be below bid-min_dist otherwise the BE SL
                # is above current price and MT5 will reject it.
                stops_level = getattr(sym_info, 'trade_stops_level', 0) or 0
                min_dist    = (stops_level + 1) * sym_info.point
                be_reachable = entry < current - min_dist
                be_sl = round(entry + sym_info.point, digits)  # 1 tick above entry
                if be_reachable and pos.sl < be_sl - sym_info.point:   # only move SL up, never down
                    if _send_sl_modify(pos, entry):
                        add_log(
                            f"BE protect: {pos.symbol} BUY #{pos.ticket} "
                            f"SL→{entry:.{digits}f} (worst case $0 loss)",
                            'INFO'
                        )

        elif pos.type == mt5.ORDER_TYPE_SELL:
            current     = tick.ask
            profit_dist = entry - current

            if profit_dist > sym_info.point * 5:      # profitable — close and bank it
                if close_position(pos):
                    add_log(
                        f"Banked profit: {pos.symbol} SELL #{pos.ticket} "
                        f"closed at {current:.{digits}f} (goal hit)",
                        'SUCCESS'
                    )
            else:                                     # losing — move SL to breakeven
                cur_sl = pos.sl if pos.sl > 0 else entry + 1
                # For SELL, entry must be above ask+min_dist otherwise the BE SL
                # is below current price and MT5 will reject it (generates spam WARNING).
                stops_level = getattr(sym_info, 'trade_stops_level', 0) or 0
                min_dist    = (stops_level + 1) * sym_info.point
                be_reachable = entry > current + min_dist
                if be_reachable and cur_sl > entry + sym_info.point:   # only move SL down on a sell
                    if _send_sl_modify(pos, entry):
                        add_log(
                            f"BE protect: {pos.symbol} SELL #{pos.ticket} "
                            f"SL→{entry:.{digits}f} (worst case $0 loss)",
                            'INFO'
                        )


def _load_today_history_from_mt5():
    """
    On bot start / reconnect: pull ALL of today's closed deals from MT5.
    Uses a 7-day look-back so IN deals for positions opened before midnight
    are found, and handles DEAL_ENTRY_INOUT (entry=2).
    """
    import datetime as dt_mod
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = today_start - dt_mod.timedelta(days=7)
    now         = datetime.now()

    ins, outs, inouts = _build_deal_maps(week_start, now)
    if not outs and not inouts:
        return

    existing_ids = {t['ticket'] for t in bot_state['trade_history']}

    _MAGIC_BOT = {234000: '', 236000: 'Gold1M', 237000: 'GoldDay'}

    # Normal OUT deals
    for pid, out_deal in outs.items():
        if pid in existing_ids:
            continue
        if out_deal.volume == 0:
            continue
        deal_dt = datetime.fromtimestamp(out_deal.time)
        if deal_dt < today_start:
            continue
        # Respect New-Day reset: don't re-add deals that closed before the reset
        if _reset_cutoff_time and deal_dt < _reset_cutoff_time:
            continue
        in_deal = ins.get(pid)
        magic   = getattr(out_deal, 'magic', 0) or (getattr(in_deal, 'magic', 0) if in_deal else 0)
        # Use comment as fallback when magic is 0 (some brokers wipe magic in history)
        bot_tag = _MAGIC_BOT.get(magic) if magic in _MAGIC_BOT else _infer_bot_from_deal(magic, out_deal, in_deal)
        record  = _deal_to_record(out_deal, in_deal, bot=bot_tag)
        bot_state['trade_history'].append(record)
        existing_ids.add(pid)

    # INOUT deals
    for pid, d in inouts.items():
        if pid in existing_ids:
            continue
        if d.volume == 0:
            continue
        d_dt = datetime.fromtimestamp(d.time)
        if d_dt < today_start:
            continue
        if _reset_cutoff_time and d_dt < _reset_cutoff_time:
            continue
        magic   = getattr(d, 'magic', 0)
        bot_tag = _MAGIC_BOT.get(magic) if magic in _MAGIC_BOT else _infer_bot_from_deal(magic, d, None)
        record  = _deal_to_record(d, bot=bot_tag)
        bot_state['trade_history'].append(record)
        existing_ids.add(pid)

    bot_state['trade_history'].sort(key=lambda x: x['close_time'], reverse=True)
    if bot_state['trade_history']:
        closed_profit = sum(t.get('profit', 0) for t in bot_state['trade_history'])
        bot_state['stats']['session_pnl'] = closed_profit
        daily_goal_state['closed_profit'] = closed_profit
    _save_history_to_disk()


def run_bot_thread():
    """Main bot loop"""
    global bot_state, daily_goal_state
    
    add_log("Advanced bot started (CRT + TBS M5)")
    strategy = CRTScalpingStrategy(**CRT_TBS_CONFIG)

    # Restore persisted data then fill any gaps from MT5 history
    try:
        if _mt5_ensure():
            acc_info = mt5.account_info()
            if acc_info:
                _validate_account_data(acc_info.login)  # clears stale data if account changed
                _load_today_history_from_mt5()
    except Exception:
        pass
    
    try:
        while bot_state['running']:
            _watchdog_heartbeat('smc')
            if not _mt5_ensure():
                add_log("MT5 connection lost", "ERROR")
                time.sleep(5)
                continue
            
            account = mt5.account_info()
            if account:
                bot_state['account'] = {
                    'login': int(account.login),
                    'server': str(account.server),
                    'balance': float(account.balance),
                    'equity': float(account.equity),
                    'profit': float(account.profit),
                }
                
                # Daily goal tracking
                if DAILY_GOAL_CONFIG.get('enabled', True):
                    today = datetime.now().date()
                    
                    # Check for day reset
                    if daily_goal_state['start_date'] != today:
                        # Load today's history first so we know any pre-existing P&L
                        _load_today_history_from_mt5()
                        closed_today = daily_goal_state.get('closed_profit', 0.0)
                        # Reconstruct midnight balance: remove today's already-closed P&L
                        # from current balance so equity-start_balance = true daily P&L
                        daily_goal_state['start_balance'] = float(account.balance) - closed_today
                        daily_goal_state['start_date']    = today
                        daily_goal_state['goal_reached']  = False
                        # closed_profit + trade_history preserved from the load above
                        bot_state['stats']['trades_opened'] = 0
                        bot_state['stats']['session_pnl']   = closed_today
                        _save_history_to_disk()
                        _save_daily_goal_to_disk()
                        add_log("New trading day - Daily tracker reset", "INFO")
                    
                    # Calculate daily P&L
                    # equity = balance (includes all closed P&L) + floating open P&L
                    # so equity - start_balance is the true total daily P&L
                    if daily_goal_state['start_balance']:
                        daily_pnl = account.equity - daily_goal_state['start_balance']
                        target = float(bot_state['daily_goal'].get('target', DAILY_GOAL_CONFIG.get('daily_target', 20.0)))
                        
                        bot_state['daily_goal']['current_pnl'] = daily_pnl
                        bot_state['daily_goal']['progress_pct'] = min(100, max(0, (daily_pnl / target) * 100))
                        
                        # Check if goal reached
                        if daily_pnl >= target and not daily_goal_state['goal_reached']:
                            daily_goal_state['goal_reached'] = True
                            bot_state['daily_goal']['goal_reached'] = True
                            action = DAILY_GOAL_CONFIG.get('action_on_goal', 'trail_and_stop')
                            add_log(f"DAILY GOAL REACHED! P&L: ${daily_pnl:.2f}", "SUCCESS")

                            if action == 'close_all':
                                add_log("Closing all positions...", "TRADE")
                                positions = mt5.positions_get()
                                if positions:
                                    for pos in positions:
                                        close_position(pos)
                                add_log("All positions closed - Done for today!", "SUCCESS")
                            elif action == 'trail_and_stop':
                                add_log("Goal hit — locking profitable positions at breakeven, closing losers. No new trades.", "INFO")
                                _move_positions_to_breakeven()
                            elif action == 'stop_trading':
                                add_log("Stopping new trades — open positions continue freely.", "INFO")
                            _save_daily_goal_to_disk()
                        
                        # Check loss limit
                        max_loss = DAILY_GOAL_CONFIG.get('max_daily_loss', -50.0)
                        if daily_pnl <= max_loss:
                            add_log(f"DAILY LOSS LIMIT HIT! P&L: ${daily_pnl:.2f}", "ERROR")
                            positions = mt5.positions_get()
                            if positions:
                                for pos in positions:
                                    close_position(pos)
                            daily_goal_state['goal_reached'] = True  # Stop trading
                            _save_daily_goal_to_disk()
                    
                    # Skip trading if goal reached
                    if daily_goal_state['goal_reached']:
                        # Still capture any trades closed after goal was hit
                        _sync_trade_history()
                        # Re-apply BE protection every cycle (safe to repeat;
                        # handles restart case + any position that slipped through)
                        action = DAILY_GOAL_CONFIG.get('action_on_goal', 'trail_and_stop')
                        if action == 'trail_and_stop':
                            _move_positions_to_breakeven()
                        time.sleep(10)
                        continue
            
            total_positions = 0
            
            for sym_key, sym_config in SYMBOLS_CONFIG.items():
                symbol = sym_config.get('symbol', sym_key)
                is_enabled = bot_state['symbols'].get(sym_key, {}).get('enabled', sym_config.get('enabled', True))
                
                try:
                    price_info, df, positions = get_mt5_data(symbol)
                    
                    if price_info is None:
                        continue
                    
                    bot_state['symbols'][sym_key]['symbol'] = price_info.get('symbol', symbol)
                    bot_state['symbols'][sym_key]['price'] = price_info
                    bot_state['symbols'][sym_key]['spread'] = price_info.get('spread_points', 0)
                    bot_state['symbols'][sym_key]['positions'] = positions
                    total_positions += len(positions)
                    
                    if df is None or len(df) < 50:
                        continue
                    
                    df = strategy.generate_signals(df)
                    signal_bar = df.iloc[-2]
                    
                    signal_strength = strategy.get_signal_strength(df.iloc[:-1])
                    bot_state['symbols'][sym_key]['signal_strength'] = signal_strength

                    # Store buy/sell scores for display
                    buy_score = int(signal_bar.get('buy_score', 0))
                    sell_score = int(signal_bar.get('sell_score', 0))
                    bot_state['symbols'][sym_key]['buy_score'] = buy_score
                    bot_state['symbols'][sym_key]['sell_score'] = sell_score
                    
                    # Get signal breakdown for analysis display
                    analysis = strategy.get_signal_breakdown(df.iloc[:-1])
                    bot_state['symbols'][sym_key]['analysis'] = analysis
                    
                    raw_signal = int(signal_bar.get('signal', 0))
                    entry_signal = raw_signal

                    # BTC fallback: if CRT signal is flat for long periods, allow
                    # score-bias entries when strength is decent and one side is dominant.
                    if raw_signal == 0 and sym_key == 'BTCUSD':
                        btc_fallback_min_strength = int(sym_config.get('btc_fallback_min_strength', 35))
                        btc_fallback_min_score_gap = int(sym_config.get('btc_fallback_min_score_gap', 2))
                        score_gap = abs(buy_score - sell_score)
                        if signal_strength >= btc_fallback_min_strength and score_gap >= btc_fallback_min_score_gap:
                            entry_signal = 1 if buy_score > sell_score else -1
                            side = 'BUY' if entry_signal == 1 else 'SELL'
                            bot_state['symbols'][sym_key]['signal'] = side + '*'
                            add_log(
                                f"BTC fallback signal -> {side} (strength={signal_strength}, buy={buy_score}, sell={sell_score})",
                                "INFO"
                            )
                        else:
                            bot_state['symbols'][sym_key]['signal'] = 'NONE'
                    elif raw_signal == 1:
                        bot_state['symbols'][sym_key]['signal'] = 'BUY'
                    elif raw_signal == -1:
                        bot_state['symbols'][sym_key]['signal'] = 'SELL'
                    else:
                        bot_state['symbols'][sym_key]['signal'] = 'NONE'
                    
                    # ── Skip trade entry if symbol is paused from dashboard ──
                    if not is_enabled:
                        continue
                    
                    last_signal_time = bot_state['symbols'][sym_key].get('last_signal_time')
                    if last_signal_time == signal_bar['time']:
                        continue
                    
                    max_per_symbol = bot_state['symbols'][sym_key].get('max_positions', RISK_CONFIG.get('max_positions_per_symbol', 1))
                    if len(positions) >= max_per_symbol:
                        continue
                    
                    # Get dynamic max positions based on account balance
                    temp_rm = RiskManager(**RISK_CONFIG)
                    if account:
                        temp_rm.account_balance = float(account.balance)
                    dynamic_max_pos = temp_rm.get_max_positions()
                    
                    if total_positions >= dynamic_max_pos:
                        continue
                    
                    symbol_for_trade = price_info.get('symbol', symbol)

                    max_spread = sym_config.get('max_spread', 50)
                    if price_info['spread_points'] > max_spread:
                        continue
                    
                    min_strength = sym_config.get('min_signal_strength', 50)
                    if sym_key == 'BTCUSD' and raw_signal == 0 and entry_signal != 0:
                        min_strength = min(min_strength, int(sym_config.get('btc_fallback_min_strength', 35)))
                    if signal_strength < min_strength:
                        continue
                    
                    if entry_signal != 0:
                        atr = signal_bar.get('ATR', 0.001)
                        if atr <= 0 or str(atr) == 'nan':
                            atr = 0.001 if 'USD' in symbol_for_trade else 2.0
                        
                        risk_manager = RiskManager(**RISK_CONFIG)
                        if account:
                            risk_manager.account_balance = account.balance
                        
                        fixed_lot = bot_state['symbols'][sym_key].get('lot_size', 0.0)
                        if fixed_lot and fixed_lot > 0:
                            position_size = float(fixed_lot)
                        else:
                            lot_mult = sym_config.get('lot_size_multiplier', 1.0)
                            position_size = risk_manager.calculate_position_size(atr, price_info['bid']) * lot_mult
                        
                        if entry_signal == 1:
                            entry = price_info['ask']
                            sl = risk_manager.calculate_stop_loss(entry, atr, 1, signal_bar)
                            tp = risk_manager.calculate_take_profit(entry, atr, 1, signal_bar, sl)
                            
                            sl_dist = round(entry - sl, 5)
                            tp_dist = round(tp - entry, 5)
                            rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
                            add_log(f"{sym_key}: BUY signal (Score:{buy_score}) SL:{sl_dist:.2f} TP:{tp_dist:.2f} RR:{rr}", "TRADE")
                            result = open_trade(symbol_for_trade, 'buy', position_size, sl, tp)
                            
                            if result:
                                bot_state['symbols'][sym_key]['last_signal_time'] = signal_bar['time']
                                bot_state['stats']['trades_opened'] += 1
                                add_log(f"{sym_key}: BUY #{result['ticket']}", "SUCCESS")
                                _record_open_trade(result['ticket'], symbol, 'buy', result['volume'], result['price'], sl, tp)
                        
                        elif entry_signal == -1:
                            entry = price_info['bid']
                            sl = risk_manager.calculate_stop_loss(entry, atr, -1, signal_bar)
                            tp = risk_manager.calculate_take_profit(entry, atr, -1, signal_bar, sl)
                            
                            sl_dist = round(sl - entry, 5)
                            tp_dist = round(entry - tp, 5)
                            rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0
                            add_log(f"{sym_key}: SELL signal (Score:{sell_score}) SL:{sl_dist:.2f} TP:{tp_dist:.2f} RR:{rr}", "TRADE")
                            result = open_trade(symbol_for_trade, 'sell', position_size, sl, tp)
                            
                            if result:
                                bot_state['symbols'][sym_key]['last_signal_time'] = signal_bar['time']
                                bot_state['stats']['trades_opened'] += 1
                                add_log(f"{sym_key}: SELL #{result['ticket']}", "SUCCESS")
                                _record_open_trade(result['ticket'], symbol, 'sell', result['volume'], result['price'], sl, tp)
                        
                        bot_state['stats']['signals_generated'] += 1
                
                except Exception as e:
                    add_log(f"{sym_key}: {str(e)}", "ERROR")
            
            bot_state['total_positions'] = total_positions

            # Update position snapshot so _sync_trade_history can detect closures
            # even when the broker's history API returns nothing.
            # Only track 5-min bot positions (magic=234000) to avoid picking up
            # Gold / DayTrade positions in the 5-min trade history.
            all_open = mt5.positions_get() or []
            for p in all_open:
                if p.magic != 234000:
                    continue  # Skip non-SMC positions
                if p.ticket not in _all_positions_snapshot:
                    _all_positions_snapshot[p.ticket] = {
                        'symbol':      p.symbol,
                        'type':        'buy' if p.type == mt5.ORDER_TYPE_BUY else 'sell',
                        'volume':      float(p.volume),
                        'open_price':  float(p.price_open),
                        'sl':          float(p.sl),
                        'tp':          float(p.tp),
                        'open_time':   datetime.fromtimestamp(p.time).strftime('%Y-%m-%d %H:%M:%S'),
                        'last_profit': float(p.profit),
                        'min_profit_seen': float(p.profit),
                    }
                else:
                    _all_positions_snapshot[p.ticket]['last_profit'] = float(p.profit)
                    _all_positions_snapshot[p.ticket]['min_profit_seen'] = min(
                        float(_all_positions_snapshot[p.ticket].get('min_profit_seen', 0.0)),
                        float(p.profit)
                    )
                _update_trade_loss_reach(p.ticket, p.profit)

            _sync_trade_history()       # detect any closed trades
            _save_daily_goal_to_disk()  # keep daily state fresh on disk
            time.sleep(10)
            
    except Exception as e:
        add_log(f"Bot error: {str(e)}", "ERROR")
    finally:
        bot_state['running'] = False
        add_log("Bot stopped")



# ══════════════════════════════════════════════════════════════
#  GOLD 1-MIN BOT — GOLD-SPECIFIC PARAMETERS
# ══════════════════════════════════════════════════════════════
def _record_closed_by_sl_tp(ticket, bot_label, snap=None, add_log_fn=None):
    """
    Called when a tracked position (by ticket) has disappeared from open positions.
    Two-pass approach:
      Pass 1 (preferred) — look up the closing deal in MT5 history for exact prices.
      Pass 2 (fallback) — build the record from the stored snapshot using last_profit
                          so we ALWAYS record something, even if the history API is slow.
    """
    import datetime as dt_mod
    global daily_goal_state

    existing_ids = {t['ticket'] for t in bot_state['trade_history']}
    if ticket in existing_ids:
        return  # already recorded (e.g. by recycle call) — no duplicate

    to_date   = datetime.now()
    from_date = to_date - dt_mod.timedelta(hours=24)

    in_deal = out_deal = None
    try:
        deals = mt5.history_deals_get(from_date, to_date) or []
        for d in deals:
            if d.position_id != ticket:
                continue
            if d.entry == 0:
                in_deal  = d
            elif d.entry == 1:
                out_deal = d
    except Exception:
        pass

    snap = snap or {}

    if out_deal:
        # ── Pass 1: exact deal data ──────────────────────────────────────────
        # Skip deals recorded before the last New-Day reset
        if _reset_cutoff_time and datetime.fromtimestamp(out_deal.time) < _reset_cutoff_time:
            return
        direction   = 'buy' if (in_deal and in_deal.type == 0) else (
                      'buy' if snap.get('direction', 1) == 1 else 'sell')
        pnl_val     = float(out_deal.profit)
        close_price = float(out_deal.price)
        close_time  = datetime.fromtimestamp(out_deal.time).strftime('%Y-%m-%d %H:%M:%S')
        open_price  = float(in_deal.price) if in_deal else snap.get('open_price', 0.0)
        open_time   = (datetime.fromtimestamp(in_deal.time).strftime('%Y-%m-%d %H:%M:%S')
                       if in_deal else snap.get('open_time', ''))
        volume      = float(out_deal.volume)
        symbol      = out_deal.symbol
    elif snap and snap.get('open_price'):
        # ── Pass 2: snapshot fallback (covers broker history-API delay) ──────
        pnl_val     = snap.get('last_profit', 0.0)
        close_price = 0.0          # exact price unknown without deal
        close_time  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        open_price  = snap.get('open_price', 0.0)
        open_time   = snap.get('open_time', '')
        volume      = snap.get('volume', 0.0)
        symbol      = snap.get('symbol', '')
        direction   = 'buy' if snap.get('direction', 1) == 1 else 'sell'
    else:
        return  # nothing to go on — skip

    if not symbol:
        return

    record = {
        'ticket':      ticket,
        'symbol':      symbol,
        'type':        direction,
        'volume':      volume,
        'open_price':  open_price,
        'close_price': close_price,
        'open_time':   open_time,
        'close_time':  close_time,
        'profit':      round(pnl_val, 2),
        'max_loss_reach': _consume_trade_loss_reach(ticket, snap.get('min_profit_seen')),
        'result':      'WIN' if pnl_val > 0 else ('LOSS' if pnl_val < 0 else 'BE'),
        'bot':         bot_label,
    }
    bot_state['trade_history'].insert(0, record)
    if len(bot_state['trade_history']) > 500:
        bot_state['trade_history'].pop()
    _append_to_archive(record)
    _notify_trade_close(record)
    if add_log_fn:
        src = 'deal' if out_deal else 'snapshot'
        add_log_fn(
            f"{symbol}: SL/TP closed ${pnl_val:+.2f} [{record['result']}] ({src})",
            'TRADE'
        )
    daily_goal_state['closed_profit'] = sum(
        t.get('profit', 0) for t in bot_state['trade_history']
    )
    _save_history_to_disk()


def add_gold_log(message, level="INFO"):
    entry = {'time': datetime.now().strftime('%H:%M:%S'), 'level': level.upper(), 'message': message}
    gold_state['logs'].insert(0, entry)
    if len(gold_state['logs']) > 80:
        gold_state['logs'].pop()


def _get_gold_session():
    """Return (in_session: bool, session_name: str) based on current UTC hour."""
    h = datetime.utcnow().hour
    if  7 <= h < 10: return True,  'London Open'
    if 10 <= h < 12: return True,  'London Mid'
    if 12 <= h < 15: return True,  'NY Open'
    if 15 <= h < 17: return True,  'London Close'
    return False, 'Off-Hours'


def _detect_gold_candle(df):
    """
    Detect last confirmed bar candle pattern.
    Returns (label: str, direction: 'buy'|'sell'|'', bonus: int)
    """
    if len(df) < 4:
        return '', '', 0
    c = df.iloc[-2]
    p = df.iloc[-3]
    body  = abs(float(c['close']) - float(c['open']))
    rng   = max(float(c['high']) - float(c['low']), 0.001)
    upper = float(c['high']) - max(float(c['close']), float(c['open']))
    lower = min(float(c['close']), float(c['open'])) - float(c['low'])
    c_bull = float(c['close']) > float(c['open'])
    p_bull = float(p['close']) > float(p['open'])
    p_body = abs(float(p['close']) - float(p['open']))
    # Bullish pin bar: long lower wick
    if lower >= 2 * max(body, 0.001) and lower >= 0.60 * rng:
        return 'BULL PIN', 'buy', 2
    # Bearish pin bar: long upper wick
    if upper >= 2 * max(body, 0.001) and upper >= 0.60 * rng:
        return 'BEAR PIN', 'sell', 2
    # Bullish engulfing
    if (c_bull and not p_bull and p_body > 0
            and float(c['close']) > float(p['open'])
            and float(c['open'])  < float(p['close'])):
        return 'BULL ENG', 'buy', 2
    # Bearish engulfing
    if (not c_bull and p_bull and p_body > 0
            and float(c['open'])  > float(p['close'])
            and float(c['close']) < float(p['open'])):
        return 'BEAR ENG', 'sell', 2
    # Doji
    if body < 0.18 * rng:
        return 'DOJI', '', 0
    return '', '', 0


def _open_gold_trade(order_type, volume, sl, tp):
    """Place a GOLD order tagged with GOLD_MAGIC."""
    # Safety guards — never place an order if bot is not running or MT5 is not ready
    if not gold_state.get('running'):
        add_gold_log("Order blocked: Gold bot is not running", "WARNING")
        return None
    # Global kill-switch
    if not trading_enabled:
        add_gold_log("Order blocked: Trading is DISABLED (use the header toggle)", "WARNING")
        return None
    if not _mt5_ensure():
        add_gold_log("Order blocked: MT5 not initialized", "WARNING")
        return None
    _, symbol, _ = _gold_symbol_runtime()
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return None
    if not sym_info.visible:
        mt5.symbol_select(symbol, True)
    volume = max(sym_info.volume_min, min(volume, sym_info.volume_max))
    volume = round(round(volume / sym_info.volume_step) * sym_info.volume_step, 2)
    tick   = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    price   = tick.ask if order_type == 'buy' else tick.bid
    digits  = sym_info.digits
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         mt5.ORDER_TYPE_BUY if order_type == 'buy' else mt5.ORDER_TYPE_SELL,
        "price":        price,
        "sl":           round(sl, digits) if sl else 0,
        "tp":           round(tp, digits) if tp else 0,
        "deviation":    30,
        "magic":        GOLD_MAGIC,
        "comment":      "GoldM1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return {'ticket': result.order, 'price': result.price, 'volume': result.volume}
    add_gold_log(f"Order failed [{order_type.upper()}]: {result.comment if result else 'None'}", "ERROR")
    return None


def _record_gold_close(pos, pnl_val):
    """Write closed gold trade to shared trade history and update daily P&L."""
    global daily_goal_state
    record = {
        'ticket':      int(pos.ticket),
        'symbol':      pos.symbol,
        'type':        'buy' if pos.type == mt5.ORDER_TYPE_BUY else 'sell',
        'volume':      float(pos.volume),
        'open_price':  float(pos.price_open),
        'close_price': float(pos.price_current),
        'open_time':   datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S'),
        'close_time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'profit':      round(float(pnl_val), 2),
        'max_loss_reach': _consume_trade_loss_reach(pos.ticket),
        'result':      'WIN' if pnl_val > 0 else ('LOSS' if pnl_val < 0 else 'BE'),
        'bot':         'Gold1M',
    }
    bot_state['trade_history'].insert(0, record)
    if len(bot_state['trade_history']) > 500:
        bot_state['trade_history'].pop()
    _append_to_archive(record)
    _notify_trade_close(record)
    if pnl_val > 0:
        gold_state['stats']['wins']   += 1
    else:
        gold_state['stats']['losses'] += 1
    daily_goal_state['closed_profit'] = sum(t.get('profit', 0) for t in bot_state['trade_history'])
    _save_history_to_disk()


def _monitor_gold_positions():
    """
    Close positions that have travelled >= recycle_pct of TP distance.
    Returns True if any position was recycled (triggers re-entry scan).
    """
    recycled   = False
    all_open   = mt5.positions_get() or []
    live_tkts  = {p.ticket for p in all_open if p.magic == GOLD_MAGIC}

    # ── Startup / reconnect recovery ─────────────────────────────────────────
    # Pick up any gold positions opened before the dashboard started
    # (e.g. after a restart while trades were still open).
    for pos in all_open:
        if pos.magic == GOLD_MAGIC and pos.ticket not in _gold_positions:
            _gold_positions[pos.ticket] = {
                'symbol':      pos.symbol,
                'direction':   1 if pos.type == mt5.ORDER_TYPE_BUY else -1,
                'type':        'buy' if pos.type == mt5.ORDER_TYPE_BUY else 'sell',
                'open_price':  float(pos.price_open),
                'volume':      float(pos.volume),
                'sl':          float(pos.sl),
                'tp':          float(pos.tp),
                'open_time':   datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S'),
                'last_profit': float(pos.profit),
                'min_profit_seen': float(pos.profit),
            }
            _update_trade_loss_reach(pos.ticket, pos.profit)

    # Keep last_profit fresh so snapshot fallback has accurate P&L
    for pos in all_open:
        if pos.magic == GOLD_MAGIC and pos.ticket in _gold_positions:
            snap = _gold_positions[pos.ticket]
            if isinstance(snap, dict):
                snap['last_profit'] = float(pos.profit)
                snap['min_profit_seen'] = min(float(snap.get('min_profit_seen', 0.0)), float(pos.profit))
            _update_trade_loss_reach(pos.ticket, pos.profit)

    for t in list(_gold_positions.keys()):
        if t not in live_tkts:
            # Wait CLOSE_CONFIRM_CYCLES consecutive missing cycles before recording close
            _missing_since_gold[t] = _missing_since_gold.get(t, 0) + 1
            if _missing_since_gold[t] < CLOSE_CONFIRM_CYCLES:
                continue   # not yet confirmed
            before_len = len(bot_state['trade_history'])
            snap = _gold_positions[t] if isinstance(_gold_positions[t], dict) else {}
            _record_closed_by_sl_tp(t, 'Gold1M', snap, add_gold_log)
            if len(bot_state['trade_history']) > before_len:
                # A new record was added — update gold wins/losses counter
                newest = bot_state['trade_history'][0]
                if newest.get('profit', 0) > 0:
                    gold_state['stats']['wins']   += 1
                else:
                    gold_state['stats']['losses'] += 1
            del _gold_positions[t]
            _missing_since_gold.pop(t, None)
        else:
            _missing_since_gold.pop(t, None)
    recycle_pct = gold_state['config'].get('recycle_pct', 0.50)
    for pos in all_open:
        if pos.magic != GOLD_MAGIC or pos.tp == 0:
            continue
        if pos.type == mt5.ORDER_TYPE_BUY:
            tp_dist      = pos.tp - pos.price_open
            price_travel = pos.price_current - pos.price_open
        else:
            tp_dist      = pos.price_open - pos.tp
            price_travel = pos.price_open - pos.price_current
        if tp_dist <= 0:
            continue
        if price_travel / tp_dist >= recycle_pct:
            pnl = pos.profit
            add_gold_log(f"GOLD: {recycle_pct*100:.0f}% TP hit (${pnl:.2f}) — recycling", "TRADE")
            if close_position(pos):
                _record_gold_close(pos, pnl)
                gold_state['stats']['recycled'] += 1
                recycled = True
    return recycled


def run_gold_bot_thread():
    """Gold-only 1-Min scalper with gold-tuned parameters. Loops every 3 seconds."""
    import pandas as pd
    global gold_state
    add_gold_log("Gold 1-Min bot started ⚡", "SUCCESS")
    _cooldown_bars   = 0    # bars remaining before next entry allowed
    _cooldown_period = 8    # wait at least 8 bars (8 min) after each trade
    try:
        while gold_state['running']:
            _watchdog_heartbeat('gold')
            # ── Maintain own MT5 connection — do NOT rely on SMC bot ─────────────
            if not _mt5_ensure():
                bot_state['connected'] = False
                add_gold_log("MT5 connection lost — retrying in 5s", "WARNING")
                time.sleep(5)
                continue
            try:
                _acct = mt5.account_info()
                if _acct:
                    bot_state['connected'] = True
                    bot_state['account'] = {
                        'login':   int(_acct.login),
                        'server':  str(_acct.server),
                        'balance': float(_acct.balance),
                        'equity':  float(_acct.equity),
                        'profit':  float(_acct.profit),
                    }
                else:
                    bot_state['connected'] = False
                    time.sleep(5)
                    continue
            except Exception:
                bot_state['connected'] = False
                time.sleep(5)
                continue
            cfg = gold_state['config']
            symbol_key, symbol, sym_cfg = _gold_symbol_runtime()

            # ── Session filter ──
            in_session, session_name = _get_gold_session()
            gold_state['live']['in_session']   = in_session
            gold_state['live']['session_name'] = session_name
            if cfg.get('session_filter', True) and not in_session:
                gold_state['live']['signal'] = 'WAIT'
                time.sleep(3)
                continue

            # ── Monitor open positions first ──
            recycled = _monitor_gold_positions()

            # Decrement cooldown each loop (~3 s ≈ one bar pass)
            if _cooldown_bars > 0:
                _cooldown_bars -= 1

            try:
                # ── Fetch M1 bars — need 300+ for HTF EMA-240 ──
                rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 350)
                if rates is None or len(rates) < 100:
                    time.sleep(3)
                    continue
                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                df.rename(columns={'tick_volume': 'volume'}, inplace=True)

                # ── ATR (14-bar True Range) ──
                tr  = pd.concat([
                    df['high'] - df['low'],
                    (df['high'] - df['close'].shift()).abs(),
                    (df['low']  - df['close'].shift()).abs(),
                ], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                if not atr or atr != atr:
                    atr = 2.0
                gold_state['live']['atr'] = round(atr, 3)

                # ── CRT Scalping Strategy: top-down Candle Range Theory ──
                min_score = cfg.get('confluence_score', 2)   # soft score (max 7)
                m1_strategy = CRTScalpingStrategy(
                    htf_h4_period=240, htf_h1_period=60, htf_m15_period=15,
                    atr_period=14, crt_lookback=4,
                    min_ref_size_atr=0.25, min_sweep_depth_atr=0.10,
                    min_score=min_score, require_d1=False,
                )
                df = m1_strategy.generate_signals(df)

                # ── Candle pattern (display only — strategy already gates candle dir) ──
                pattern, pat_dir, pat_bonus = '', '', 0
                if cfg.get('candle_patterns', True):
                    pattern, pat_dir, pat_bonus = _detect_gold_candle(df)
                gold_state['live']['pattern']     = pattern
                gold_state['live']['pattern_dir'] = pat_dir

                # ── Price / spread ──
                price_info, _, _ = get_mt5_data(symbol)
                if not price_info:
                    time.sleep(3)
                    continue
                gold_state['live']['price']  = price_info
                gold_state['live']['spread'] = price_info.get('spread_points', 0)

                # ── Read signal from last confirmed bar ──
                # The strategy already enforces all hard gates (HTF + setup + zone + candle)
                bar        = df.iloc[-2]
                eff_signal = int(bar.get('signal', 0))
                indic      = m1_strategy.get_indicator_snapshot(df)

                gold_state['live']['buy_score']  = int(bar.get('buy_score',  0))
                gold_state['live']['sell_score'] = int(bar.get('sell_score', 0))
                gold_state['live']['indicators'] = indic
                gold_state['live']['signal']     = (
                    'BUY'  if eff_signal ==  1 else
                    'SELL' if eff_signal == -1 else 'NONE'
                )

                # ── Open positions snapshot ──
                gold_pos = [p for p in (mt5.positions_get(symbol=symbol) or [])
                            if p.magic == GOLD_MAGIC]
                gold_state['live']['total_positions'] = len(gold_pos)
                gold_state['live']['positions'] = [
                    {
                        'ticket':        p.ticket,
                        'type':          'buy' if p.type == mt5.ORDER_TYPE_BUY else 'sell',
                        'volume':        float(p.volume),
                        'open_price':    float(p.price_open),
                        'price_current': float(p.price_current),
                        'tp':            float(p.tp),
                        'sl':            float(p.sl),
                        'profit':        float(p.profit),
                        'max_loss_reach': _current_trade_loss_reach(p.ticket),
                    }
                    for p in gold_pos
                ]

                # ── Entry checks ──
                spread   = price_info.get('spread_points', 0)
                max_sp   = cfg.get('max_spread', sym_cfg.get('max_spread', 80))
                max_pos  = cfg.get('max_positions', 3)
                has_room = len(gold_pos) < max_pos
                if spread > max_sp:
                    time.sleep(3)
                    continue
                if not has_room and not recycled:
                    time.sleep(3)
                    continue
                if _cooldown_bars > 0:
                    # Still in cooldown after last trade — skip entry
                    time.sleep(3)
                    continue
                if eff_signal == 0:
                    if recycled:
                        add_gold_log("Recycle: no signal on M1 — waiting", "INFO")
                    time.sleep(3)
                    continue

                # ── Position size ──
                account   = mt5.account_info()
                fixed_lot = cfg.get('lot_size', 0.0)
                if fixed_lot and fixed_lot > 0:
                    position_size = float(fixed_lot)
                else:
                    risk_mgr = RiskManager(**RISK_CONFIG)
                    if account:
                        risk_mgr.account_balance = float(account.balance)
                    position_size = (
                        risk_mgr.calculate_position_size(atr, price_info['bid'])
                        * sym_cfg.get('lot_size_multiplier', 1.0)
                    )

                # ── SL / TP using structural swing levels (ICT method) ──
                #  BUY : SL below the swept swing low (or supertrend support)
                #         TP at next swing high / BSL structural target
                #  SELL: SL above the swept swing high (or supertrend resist)
                #         TP at next swing low / SSL structural target
                # Fallback: ATR multiples when structural levels unavailable.
                _snap       = indic
                sl_atr_mult = cfg.get('sl_atr_mult', 1.5)
                tp_atr_mult = cfg.get('tp_atr_mult', 3.0)

                if eff_signal == 1:
                    entry   = price_info['ask']
                    # CRT structural SL/TP: SL below sweep wick; TP = top of reference candle
                    sw_low  = float(_snap.get('crt_sw_low',  0.0) or 0.0)
                    ref_hi  = float(_snap.get('crt_ref_high', 0.0) or 0.0)
                    if sw_low > 0 and sw_low < entry:
                        sl = sw_low - atr * 0.2
                        sl_dist = entry - sl
                        # Guard: keep SL distance within 0.2–6× ATR
                        if sl_dist < atr * 0.2 or sl_dist > atr * 6.0:
                            sl_dist = atr * sl_atr_mult
                            sl      = entry - sl_dist
                    else:
                        sl_dist = atr * sl_atr_mult
                        sl      = entry - sl_dist
                    if ref_hi > entry:
                        tp_dist = max(ref_hi - entry, atr * 0.5)
                    else:
                        tp_dist = sl_dist * tp_atr_mult
                    tp  = entry + tp_dist
                    res = _open_gold_trade('buy', position_size, sl, tp)
                    if res:
                        _cooldown_bars = _cooldown_period
                        _gold_positions[res['ticket']] = {
                            'symbol':      symbol,
                            'direction':   1,
                            'type':        'buy',
                            'open_price':  res['price'],
                            'volume':      res['volume'],
                            'sl':          sl,
                            'tp':          tp,
                            'open_time':   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'last_profit': 0.0,
                        }
                        gold_state['stats']['trades_opened'] += 1
                        add_gold_log(
                            f"BUY #{res['ticket']} @{res['price']:.2f}  "
                            f"SL={sl:.2f} ({sl_dist:.2f} pts)  TP={tp:.2f}  ATR={atr:.2f}" +
                            (f"  [{pattern}]" if pattern else ""),
                            "SUCCESS"
                        )
                elif eff_signal == -1:
                    entry   = price_info['bid']
                    sw_high = float(_snap.get('crt_sw_high', 0.0) or 0.0)
                    ref_lo  = float(_snap.get('crt_ref_low',  0.0) or 0.0)
                    if sw_high > entry:
                        sl = sw_high + atr * 0.2
                        sl_dist = sl - entry
                        if sl_dist < atr * 0.2 or sl_dist > atr * 6.0:
                            sl_dist = atr * sl_atr_mult
                            sl      = entry + sl_dist
                    else:
                        sl_dist = atr * sl_atr_mult
                        sl      = entry + sl_dist
                    if ref_lo > 0 and ref_lo < entry:
                        tp_dist = max(entry - ref_lo, atr * 0.5)
                    else:
                        tp_dist = sl_dist * tp_atr_mult
                    tp  = entry - tp_dist
                    res = _open_gold_trade('sell', position_size, sl, tp)
                    if res:
                        _cooldown_bars = _cooldown_period
                        _gold_positions[res['ticket']] = {
                            'symbol':      symbol,
                            'direction':   -1,
                            'type':        'sell',
                            'open_price':  res['price'],
                            'volume':      res['volume'],
                            'sl':          sl,
                            'tp':          tp,
                            'open_time':   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'last_profit': 0.0,
                        }
                        gold_state['stats']['trades_opened'] += 1
                        add_gold_log(
                            f"SELL #{res['ticket']} @{res['price']:.2f}  "
                            f"SL={sl:.2f} ({sl_dist:.2f} pts)  TP={tp:.2f}  ATR={atr:.2f}" +
                            (f"  [{pattern}]" if pattern else ""),
                            "SUCCESS"
                        )

            except Exception as e:
                add_gold_log(f"Loop error: {e}", "ERROR")

            time.sleep(3)

    except Exception as e:
        add_gold_log(f"Gold bot error: {e}", "ERROR")
    finally:
        gold_state['running'] = False
        add_gold_log("Gold 1-Min bot stopped")
        if not gold_state.get('stop_reason'):
            gold_state['stop_reason'] = 'Thread exited (crash or process restart).'


# ═══════════════════════════════════════════════════════════════
#  GOLD DAY TRADING BOT — SMC + ICT + Fibonacci on D1 / H4
# ═══════════════════════════════════════════════════════════════

_missing_since_day: dict = {}   # ticket -> consecutive-missing-cycle count


def add_daytrade_log(message, level="INFO"):
    entry = {'time': datetime.now().strftime('%H:%M:%S'), 'level': level.upper(), 'message': message}
    daytrade_state['logs'].insert(0, entry)
    if len(daytrade_state['logs']) > 80:
        daytrade_state['logs'].pop()


def _open_daytrade(order_type, volume, sl, tp):
    """Place a GOLD order tagged with DAYTRADE_MAGIC."""
    if not daytrade_state.get('running'):
        add_daytrade_log("Order blocked: Day Trade bot is not running", "WARNING")
        return None
    if not trading_enabled:
        add_daytrade_log("Order blocked: Trading is DISABLED (use the header toggle)", "WARNING")
        return None
    if not _mt5_ensure():
        add_daytrade_log("Order blocked: MT5 not initialized", "WARNING")
        return None
    _, symbol, _ = _daytrade_symbol_runtime()
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return None
    if not sym_info.visible:
        mt5.symbol_select(symbol, True)
    volume = max(sym_info.volume_min, min(volume, sym_info.volume_max))
    volume = round(round(volume / sym_info.volume_step) * sym_info.volume_step, 2)
    tick   = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    price   = tick.ask if order_type == 'buy' else tick.bid
    digits  = sym_info.digits
    request = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       volume,
        "type":         mt5.ORDER_TYPE_BUY if order_type == 'buy' else mt5.ORDER_TYPE_SELL,
        "price":        price,
        "sl":           round(sl, digits) if sl else 0,
        "tp":           round(tp, digits) if tp else 0,
        "deviation":    30,
        "magic":        DAYTRADE_MAGIC,
        "comment":      "GoldDay",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return {'ticket': result.order, 'price': result.price, 'volume': result.volume}
    add_daytrade_log(f"Order failed [{order_type.upper()}]: {result.comment if result else 'None'}", "ERROR")
    return None


def _record_daytrade_close(pos, pnl_val):
    """Write closed day-trade to shared trade history."""
    global daily_goal_state
    record = {
        'ticket':      int(pos.ticket),
        'symbol':      pos.symbol,
        'type':        'buy' if pos.type == mt5.ORDER_TYPE_BUY else 'sell',
        'volume':      float(pos.volume),
        'open_price':  float(pos.price_open),
        'close_price': float(pos.price_current),
        'open_time':   datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S'),
        'close_time':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'profit':      round(float(pnl_val), 2),
        'max_loss_reach': _consume_trade_loss_reach(pos.ticket),
        'result':      'WIN' if pnl_val > 0 else ('LOSS' if pnl_val < 0 else 'BE'),
        'bot':         'GoldDay',
    }
    bot_state['trade_history'].insert(0, record)
    if len(bot_state['trade_history']) > 500:
        bot_state['trade_history'].pop()
    _append_to_archive(record)
    _notify_trade_close(record)
    if pnl_val > 0:
        daytrade_state['stats']['wins']   += 1
    else:
        daytrade_state['stats']['losses'] += 1
    daily_goal_state['closed_profit'] = sum(t.get('profit', 0) for t in bot_state['trade_history'])
    _save_history_to_disk()


def _monitor_day_positions():
    """
    Monitor open day-trade positions. Close at recycle_pct of TP distance.
    Returns True if a position was recycled.
    """
    recycled   = False
    all_open   = mt5.positions_get() or []
    live_tkts  = {p.ticket for p in all_open if p.magic == DAYTRADE_MAGIC}

    # Pick up any positions reopened before dashboard restart
    for pos in all_open:
        if pos.magic == DAYTRADE_MAGIC and pos.ticket not in _day_positions:
            _day_positions[pos.ticket] = {
                'symbol':      pos.symbol,
                'direction':   1 if pos.type == mt5.ORDER_TYPE_BUY else -1,
                'type':        'buy' if pos.type == mt5.ORDER_TYPE_BUY else 'sell',
                'open_price':  float(pos.price_open),
                'volume':      float(pos.volume),
                'sl':          float(pos.sl),
                'tp':          float(pos.tp),
                'open_time':   datetime.fromtimestamp(pos.time).strftime('%Y-%m-%d %H:%M:%S'),
                'last_profit': float(pos.profit),
                'min_profit_seen': float(pos.profit),
            }
            _update_trade_loss_reach(pos.ticket, pos.profit)

    for pos in all_open:
        if pos.magic == DAYTRADE_MAGIC and pos.ticket in _day_positions:
            snap = _day_positions[pos.ticket]
            if isinstance(snap, dict):
                snap['last_profit'] = float(pos.profit)
                snap['min_profit_seen'] = min(float(snap.get('min_profit_seen', 0.0)), float(pos.profit))
            _update_trade_loss_reach(pos.ticket, pos.profit)

    for t in list(_day_positions.keys()):
        if t not in live_tkts:
            _missing_since_day[t] = _missing_since_day.get(t, 0) + 1
            if _missing_since_day[t] < CLOSE_CONFIRM_CYCLES:
                continue
            before_len = len(bot_state['trade_history'])
            snap = _day_positions[t] if isinstance(_day_positions[t], dict) else {}
            _record_closed_by_sl_tp(t, 'GoldDay', snap, add_daytrade_log)
            if len(bot_state['trade_history']) > before_len:
                newest = bot_state['trade_history'][0]
                if newest.get('profit', 0) > 0:
                    daytrade_state['stats']['wins']   += 1
                else:
                    daytrade_state['stats']['losses'] += 1
            del _day_positions[t]
            _missing_since_day.pop(t, None)
        else:
            _missing_since_day.pop(t, None)

    recycle_pct = daytrade_state['config'].get('recycle_pct', 0.60)
    for pos in all_open:
        if pos.magic != DAYTRADE_MAGIC or pos.tp == 0:
            continue
        if pos.type == mt5.ORDER_TYPE_BUY:
            tp_dist      = pos.tp   - pos.price_open
            price_travel = pos.price_current - pos.price_open
        else:
            tp_dist      = pos.price_open - pos.tp
            price_travel = pos.price_open - pos.price_current
        if tp_dist <= 0:
            continue
        if price_travel / tp_dist >= recycle_pct:
            pnl = pos.profit
            add_daytrade_log(f"GOLD DAY: {recycle_pct*100:.0f}% TP hit (${pnl:.2f}) — recycling", "TRADE")
            if close_position(pos):
                _record_daytrade_close(pos, pnl)
                daytrade_state['stats']['recycled'] += 1
                recycled = True
    return recycled


def run_daytrade_bot_thread():
    """
    Gold Day Trading bot. Uses the same SMC+ICT+Fibonacci strategy as the 5-min bot
    but on the H4 timeframe (200-bar history).  Loops every 30 seconds to reduce load.
    Session filter mirrors the Gold 1-Min bot (London + NY windows only).
    """
    import pandas as pd
    global daytrade_state
    add_daytrade_log("Day Trade bot started ⚡", "SUCCESS")
    # Re-use the same AdvancedScalpingStrategy that powers the 5-min bot
    strategy   = AdvancedScalpingStrategy(**STRATEGY_CONFIG)
    last_bar_time = None    # dedup: only act on a new confirmed bar

    try:
        while daytrade_state['running']:
            _watchdog_heartbeat('daytrade')
            if not _mt5_ensure():
                bot_state['connected'] = False
                add_daytrade_log("MT5 connection lost — retrying in 10s", "WARNING")
                time.sleep(10)
                continue
            try:
                _acct = mt5.account_info()
                if _acct:
                    bot_state['connected'] = True
                    bot_state['account'] = {
                        'login':   int(_acct.login),
                        'server':  str(_acct.server),
                        'balance': float(_acct.balance),
                        'equity':  float(_acct.equity),
                        'profit':  float(_acct.profit),
                    }
                else:
                    bot_state['connected'] = False
                    time.sleep(10)
                    continue
            except Exception:
                bot_state['connected'] = False
                time.sleep(10)
                continue

            cfg = daytrade_state['config']
            symbol_key, symbol, sym_cfg = _daytrade_symbol_runtime()

            # Session filter (same windows as Gold 1-Min)
            in_session, session_name = _get_gold_session()
            daytrade_state['live']['in_session']   = in_session
            daytrade_state['live']['session_name'] = session_name
            if cfg.get('session_filter', True) and not in_session:
                daytrade_state['live']['signal'] = 'WAIT'
                time.sleep(30)
                continue

            _monitor_day_positions()

            try:
                # H4 bars (200 bars = ~33 days of history — enough for all indicators)
                rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H4, 0, 200)
                if rates is None or len(rates) < 50:
                    time.sleep(30)
                    continue
                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s')
                df.rename(columns={'tick_volume': 'volume'}, inplace=True)

                # ATR
                tr  = pd.concat([
                    df['high'] - df['low'],
                    (df['high'] - df['close'].shift()).abs(),
                    (df['low']  - df['close'].shift()).abs(),
                ], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                if not atr or atr != atr:
                    atr = 8.0   # ~typical H4 ATR for gold
                daytrade_state['live']['atr'] = round(atr, 3)

                # Generate signals using full SMC+ICT+Fib strategy
                df = strategy.generate_signals(df)

                signal_bar      = df.iloc[-2]   # last confirmed bar
                signal_bar_time = signal_bar['time']
                eff_signal      = int(signal_bar.get('signal', 0))

                # Candle pattern detection
                pattern, pat_dir, _ = '', '', 0
                if cfg.get('candle_patterns', True):
                    pattern, pat_dir, _ = _detect_gold_candle(df)
                daytrade_state['live']['pattern']     = pattern
                daytrade_state['live']['pattern_dir'] = pat_dir

                # Price / spread
                price_info, _, _ = get_mt5_data(symbol)
                if not price_info:
                    time.sleep(30)
                    continue
                daytrade_state['live']['price']  = price_info
                daytrade_state['live']['spread'] = price_info.get('spread_points', 0)

                # Live scores
                daytrade_state['live']['buy_score']  = int(signal_bar.get('buy_score',  0))
                daytrade_state['live']['sell_score'] = int(signal_bar.get('sell_score', 0))
                daytrade_state['live']['signal']     = (
                    'BUY'  if eff_signal ==  1 else
                    'SELL' if eff_signal == -1 else 'NONE'
                )

                # Get indicator breakdown for the UI
                try:
                    analysis = strategy.get_signal_breakdown(df.iloc[:-1])
                    daytrade_state['live']['indicators'] = analysis
                except Exception:
                    pass

                # Open positions snapshot
                day_pos = [p for p in (mt5.positions_get(symbol=symbol) or [])
                           if p.magic == DAYTRADE_MAGIC]
                daytrade_state['live']['total_positions'] = len(day_pos)
                daytrade_state['live']['positions'] = [
                    {
                        'ticket':        p.ticket,
                        'type':          'buy' if p.type == mt5.ORDER_TYPE_BUY else 'sell',
                        'volume':        float(p.volume),
                        'open_price':    float(p.price_open),
                        'price_current': float(p.price_current),
                        'tp':            float(p.tp),
                        'sl':            float(p.sl),
                        'profit':        float(p.profit),
                        'max_loss_reach': _current_trade_loss_reach(p.ticket),
                    }
                    for p in day_pos
                ]

                # Entry guards
                spread   = price_info.get('spread_points', 0)
                max_sp   = cfg.get('max_spread', sym_cfg.get('max_spread', 80))
                max_pos  = cfg.get('max_positions', 2)
                min_sc   = cfg.get('confluence_score', 8)
                has_room = len(day_pos) < max_pos

                if spread > max_sp:
                    time.sleep(30)
                    continue
                if not has_room:
                    time.sleep(30)
                    continue
                if eff_signal == 0:
                    time.sleep(30)
                    continue

                # Score gate
                active_score = (daytrade_state['live']['buy_score']
                                if eff_signal == 1
                                else daytrade_state['live']['sell_score'])
                if active_score < min_sc:
                    time.sleep(30)
                    continue

                # De-dup: same bar as last entry
                if signal_bar_time == last_bar_time:
                    time.sleep(30)
                    continue

                # Position sizing
                account   = mt5.account_info()
                fixed_lot = cfg.get('lot_size', 0.0)
                if fixed_lot and fixed_lot > 0:
                    position_size = float(fixed_lot)
                else:
                    risk_mgr = RiskManager(**RISK_CONFIG)
                    if account:
                        risk_mgr.account_balance = float(account.balance)
                    position_size = (
                        risk_mgr.calculate_position_size(atr, price_info['bid'])
                        * sym_cfg.get('lot_size_multiplier', 1.0)
                    )

                sl_atr_mult = cfg.get('sl_atr_mult', 1.5)
                tp_atr_mult = cfg.get('tp_atr_mult', 3.0)

                if eff_signal == 1:
                    entry  = price_info['ask']
                    sl     = entry - atr * sl_atr_mult
                    tp     = entry + atr * tp_atr_mult
                    res    = _open_daytrade('buy', position_size, sl, tp)
                    if res:
                        last_bar_time = signal_bar_time
                        _day_positions[res['ticket']] = {
                            'symbol':      symbol,
                            'direction':   1,
                            'type':        'buy',
                            'open_price':  res['price'],
                            'volume':      res['volume'],
                            'sl':          sl,
                            'tp':          tp,
                            'open_time':   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'last_profit': 0.0,
                        }
                        daytrade_state['stats']['trades_opened'] += 1
                        add_daytrade_log(
                            f"BUY #{res['ticket']} @{res['price']:.2f}  "
                            f"SL={sl:.2f}  TP={tp:.2f}  ATR={atr:.2f}  Score={active_score}" +
                            (f"  [{pattern}]" if pattern else ""),
                            "SUCCESS"
                        )

                elif eff_signal == -1:
                    entry  = price_info['bid']
                    sl     = entry + atr * sl_atr_mult
                    tp     = entry - atr * tp_atr_mult
                    res    = _open_daytrade('sell', position_size, sl, tp)
                    if res:
                        last_bar_time = signal_bar_time
                        _day_positions[res['ticket']] = {
                            'symbol':      symbol,
                            'direction':   -1,
                            'type':        'sell',
                            'open_price':  res['price'],
                            'volume':      res['volume'],
                            'sl':          sl,
                            'tp':          tp,
                            'open_time':   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'last_profit': 0.0,
                        }
                        daytrade_state['stats']['trades_opened'] += 1
                        add_daytrade_log(
                            f"SELL #{res['ticket']} @{res['price']:.2f}  "
                            f"SL={sl:.2f}  TP={tp:.2f}  ATR={atr:.2f}  Score={active_score}" +
                            (f"  [{pattern}]" if pattern else ""),
                            "SUCCESS"
                        )

            except Exception as e:
                add_daytrade_log(f"Loop error: {e}", "ERROR")

            time.sleep(30)

    except Exception as e:
        add_daytrade_log(f"Day Trade bot error: {e}", "ERROR")
    finally:
        daytrade_state['running'] = False
        add_daytrade_log("Gold Day Trade bot stopped")
        if not daytrade_state.get('stop_reason'):
            daytrade_state['stop_reason'] = 'Thread exited.'


HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Multi-Symbol Scalper</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/antd/4.24.16/antd.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        :root {
            --bg-main: radial-gradient(circle at 10% 20%, #1b2a52 0%, #111827 45%, #0a1022 100%);
            --text-main: #fff;
            --text-muted: #888;
            --header-bg: rgba(16, 24, 44, 0.72);
            --header-border: rgba(255,255,255,0.14);
            --panel-bg: rgba(255,255,255,0.05);
            --panel-bg-strong: rgba(17, 24, 39, 0.74);
            --panel-border: rgba(255,255,255,0.12);
            --line-soft: rgba(255,255,255,0.10);
            --tab-bg: rgba(255,255,255,0.05);
            --tab-bg-active: rgba(255,255,255,0.12);
            --tab-hover: rgba(255,255,255,0.09);
            --tab-text: #888;
            --input-bg: #1a1a2e;
            --input-border: #444;
            --input-text: #fff;
            --shadow-soft: 0 8px 24px rgba(0,0,0,0.24);
        }
        body.light-theme {
            --bg-main: radial-gradient(circle at 8% 12%, #edf0f3 0%, #e4e8ed 46%, #d9dfe6 100%);
            --text-main: #252d38;
            --text-muted: #5c6675;
            --header-bg: rgba(233, 237, 242, 0.96);
            --header-border: rgba(71, 81, 95, 0.24);
            --panel-bg: rgba(236, 240, 245, 0.96);
            --panel-bg-strong: rgba(227, 233, 240, 0.98);
            --panel-border: rgba(82, 93, 108, 0.24);
            --line-soft: rgba(86, 98, 114, 0.20);
            --tab-bg: rgba(71, 81, 95, 0.08);
            --tab-bg-active: rgba(71, 81, 95, 0.20);
            --tab-hover: rgba(71, 81, 95, 0.14);
            --tab-text: #3f4a59;
            --input-bg: #e2e7ee;
            --input-border: #8a95a4;
            --input-text: #2a3340;
            --shadow-soft: 0 6px 16px rgba(48, 58, 72, 0.12);
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'Noto Sans', sans-serif;
            background: var(--bg-main);
            color: var(--text-main);
            min-height: 100vh;
            padding: 20px;
            transition: background 0.25s ease, color 0.25s ease;
        }
        .container {
            max-width: 1920px;
            margin: 0 auto;
            width: 100%;
        }
        .header {
            text-align: center;
            padding: 18px;
            background: var(--header-bg);
            border-radius: 14px;
            margin-bottom: 18px;
            border: 1px solid var(--header-border);
            box-shadow: 0 10px 30px rgba(0,0,0,0.28);
        }
        .header h1 { color: #ffd700; font-size: 1.8em; }
        .header p { color: var(--text-muted); font-size: 0.9em; }
        
        .controls { text-align: center; margin-bottom: 15px; }
        .btn {
            padding: 9px 20px;
            border: 1px solid transparent;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            margin: 5px;
            box-shadow: 0 2px 0 rgba(0,0,0,0.06);
            transition: all 0.2s ease;
        }
        .btn-start { background: #27ae60; color: white; }
        .btn-stop { background: #e74c3c; color: white; }
        .btn-connect { background: #3498db; color: white; }
        .btn:hover { transform: translateY(-1px); filter: brightness(1.04); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        
        .account-bar {
            display: flex;
            justify-content: center;
            gap: 30px;
            padding: 10px;
            background: var(--panel-bg);
            border-radius: 8px;
            margin-bottom: 15px;
            flex-wrap: wrap;
        }
        .account-item { text-align: center; }
        .account-label { color: var(--text-muted); font-size: 0.8em; }
        .account-value { font-size: 1.1em; font-weight: bold; color: #ffd700; }
        
        .symbols-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 15px;
            margin-bottom: 15px;
        }
        
        .symbol-card {
            background: var(--panel-bg);
            border-radius: 10px;
            padding: 15px;
            border: 1px solid var(--panel-border);
        }
        
        .symbol-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid var(--line-soft);
        }
        .symbol-name { font-size: 1.3em; font-weight: bold; }
        .symbol-name.gold { color: #ffd700; }
        .symbol-name.eur { color: #3498db; }
        .symbol-name.gbp { color: #e74c3c; }
        .symbol-name.btc { color: #f39c12; }
        
        .price-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .price-big { font-size: 1.8em; font-weight: bold; }
        .spread-info { color: var(--text-muted); font-size: 0.9em; }
        
        .signal-badge {
            padding: 5px 15px;
            border-radius: 15px;
            font-weight: bold;
        }
        .signal-buy { background: #27ae60; }
        .signal-sell { background: #e74c3c; }
        .signal-none { background: #555; color: #f4f6fa; border: 1px solid rgba(255,255,255,0.12); }
        
        .strength-bar {
            height: 6px;
            background: rgba(0,0,0,0.22);
            border-radius: 3px;
            overflow: hidden;
            margin-top: 5px;
        }
        .strength-fill {
            height: 100%;
            background: linear-gradient(90deg, #e74c3c, #f39c12, #27ae60);
        }
        
        .positions-mini { margin-top: 10px; font-size: 0.85em; }
        .position-row {
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            border-top: 1px solid var(--line-soft);
        }
        .profit-pos { color: #27ae60; }
        .profit-neg { color: #e74c3c; }
        
        .bottom-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        @media (max-width: 900px) { .bottom-grid { grid-template-columns: 1fr; } }
        
        .card {
            background: var(--panel-bg-strong);
            border-radius: 12px;
            padding: 15px;
            border: 1px solid var(--panel-border);
            box-shadow: var(--shadow-soft);
        }
        .card h3 {
            color: #ffd700;
            margin-bottom: 10px;
            font-size: 1em;
            border-bottom: 1px solid var(--line-soft);
            padding-bottom: 8px;
        }
        
        .logs { max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 0.8em; }
        .log-entry { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .log-time { color: var(--text-muted); margin-right: 8px; }
        .log-INFO { color: #3498db; }
        .log-WARNING { color: #f39c12; }
        .log-ERROR { color: #e74c3c; }
        .log-SUCCESS { color: #27ae60; }
        .log-TRADE { color: #ffd700; }
        
        .pos-table { width: 100%; border-collapse: collapse; font-size: 0.85em; }
        .pos-table th, .pos-table td {
            padding: 6px;
            text-align: left;
            border-bottom: 1px solid var(--line-soft);
        }
        .pos-table th { color: var(--text-muted); }
        
        .status-dot {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 5px;
        }
        .status-running { background: #27ae60; }
        .status-stopped { background: #888; }
        /* ── Tab navigation ── */
        .tab-nav {
            display: flex;
            gap: 3px;
            margin-bottom: 12px;
            border-bottom: 2px solid rgba(255,255,255,0.08);
        }
        .tab-btn {
            padding: 9px 24px;
            border: none;
            background: var(--tab-bg);
            color: var(--tab-text);
            cursor: pointer;
            font-size: 0.9em;
            font-weight: 600;
            border-radius: 8px 8px 0 0;
            transition: all 0.2s;
            border-bottom: 3px solid transparent;
            margin-bottom: -2px;
        }
        .tab-btn.active { background: var(--tab-bg-active); color: #ffd700; border-bottom-color: #ffd700; }
        .tab-btn:hover:not(.active) { background: var(--tab-hover); color: var(--text-main); }
        .tab-btn.gold-tab.active   { color: #ffd700; border-bottom-color: #ffd700; background: rgba(255,215,0,0.10); }
        .tab-btn.gold-tab:hover:not(.active) { color: #ffe066; }
        .tab-btn.daytrade-tab.active { color: #f39c12; border-bottom-color: #f39c12; background: rgba(243,156,18,0.10); }
        .tab-btn.daytrade-tab:hover:not(.active) { color: #f9ca24; }
        .tab-btn.btc-tab.active { color: #f39c12; border-bottom-color: #f39c12; background: rgba(243,156,18,0.16); }
        .tab-btn.btc-tab:hover:not(.active) { color: #ffb347; }
        .tab-btn.hist-tab.active  { color: #3498db; border-bottom-color: #3498db; background: rgba(52,152,219,0.10); }
        .tab-btn.hist-tab:hover:not(.active) { color: #5dade2; }
        /* Gold tab specific */
        .gold-price-big { font-size: 2.4em; font-weight: 800; color: #ffd700; letter-spacing: 1px; }
        .gold-stat-chip { background: rgba(255,215,0,0.08); border: 1px solid rgba(255,215,0,0.22); border-radius: 6px; padding: 4px 10px; font-size: 0.82em; }
        .pattern-chip { padding: 3px 8px; border-radius: 4px; font-size: 0.78em; font-weight: 700; }
        .pattern-bull { background: rgba(39,174,96,0.18); color: #2ecc71; border: 1px solid rgba(39,174,96,0.3); }
        .pattern-bear { background: rgba(231,76,60,0.18);  color: #e74c3c; border: 1px solid rgba(231,76,60,0.3); }
        .pattern-doji { background: rgba(255,255,255,0.07); color: #aaa;   border: 1px solid #444; }
        .session-badge-on  { background: rgba(39,174,96,0.2); color: #2ecc71; border: 1px solid rgba(39,174,96,0.4); border-radius: 6px; padding: 3px 10px; font-size: 0.82em; font-weight: 600; }
        .session-badge-off { background: rgba(255,255,255,0.05); color: #666; border: 1px solid #333; border-radius: 6px; padding: 3px 10px; font-size: 0.82em; }

        .theme-toggle {
            background: linear-gradient(135deg, #2c3e50, #34495e);
            color: #ecf0f1;
            border: 1px solid rgba(236,240,241,0.35);
            border-radius: 8px;
            padding: 8px 12px;
            font-size: 0.8em;
            font-weight: 700;
            cursor: pointer;
            letter-spacing: 0.3px;
            min-width: 104px;
            transition: all 0.2s ease;
        }
        .theme-toggle:hover { filter: brightness(1.08); transform: translateY(-1px); }

        .btn-restart {
            background: linear-gradient(135deg, #b7473a, #9f3a32);
            color: #fff;
            border: 1px solid rgba(255,255,255,0.14);
            border-radius: 8px;
            padding: 8px 18px;
            font-size: 0.85em;
            font-weight: 700;
            cursor: pointer;
            letter-spacing: 0.5px;
            box-shadow: 0 2px 8px rgba(159,58,50,0.35);
            transition: all 0.2s ease;
        }
        .btn-restart:hover { filter: brightness(1.08); transform: translateY(-1px); }

        body.light-theme .theme-toggle {
            background: linear-gradient(135deg, #9aa5b3, #8b97a6);
            color: #f6f8fb;
            border-color: rgba(73, 84, 100, 0.36);
        }

        body.light-theme .signal-none {
            background: #c2c9d3;
            color: #2f3a47;
            border-color: #9ea8b6;
        }

        body.light-theme .btn-restart {
            background: linear-gradient(135deg, #d7c4c4, #cbb5b5);
            color: #2a1f1f;
            border-color: rgba(102, 80, 80, 0.42);
            box-shadow: none;
        }

        body.light-theme .btn-restart:hover {
            background: linear-gradient(135deg, #cfb9b9, #c3acac);
            color: #201717;
            border-color: rgba(93, 72, 72, 0.46);
        }

        body.light-theme .btn-restart:disabled {
            background: #d2c7c7;
            color: #4a3c3c;
            border-color: #b7a7a7;
            cursor: not-allowed;
            opacity: 1;
        }

        body.light-theme #equityChart {
            background: var(--panel-bg-strong) !important;
            border: 1px solid var(--panel-border) !important;
        }

        body.light-theme .equity-note {
            color: var(--text-muted) !important;
        }

        body.light-theme .header h1,
        body.light-theme .account-value,
        body.light-theme .card h3 {
            color: #2f3a47;
        }

        body.light-theme .tab-btn.active {
            color: #2f3a47;
            border-bottom-color: #2f3a47;
            background: rgba(79, 93, 113, 0.22);
        }

        body.light-theme .tab-btn.gold-tab.active,
        body.light-theme .tab-btn.daytrade-tab.active,
        body.light-theme .tab-btn.btc-tab.active,
        body.light-theme .tab-btn.hist-tab.active {
            color: #2f3a47;
            border-bottom-color: #2f3a47;
            background: rgba(79, 93, 113, 0.22);
        }

        body.light-theme .tab-btn {
            color: #3f4a59;
            background: rgba(71,81,95,0.08);
        }

        body.light-theme .btn-start {
            background: #2f8f68 !important;
            color: #f5f8fb !important;
            border-color: #267554 !important;
        }

        body.light-theme .btn-stop {
            background: #b45a5a !important;
            color: #f8fafc !important;
            border-color: #964a4a !important;
        }

        body.light-theme .btn-connect {
            background: #4f6f95 !important;
            color: #f7f9fc !important;
            border-color: #425e80 !important;
        }

        body.light-theme button:not(.tab-btn):not(.btn-start):not(.btn-stop):not(.btn-connect):not(.theme-toggle):not(.btn-restart) {
            background: #6d7786 !important;
            color: #f8fafc !important;
            border-color: #5b6472 !important;
        }

        body.light-theme .btn,
        body.light-theme button,
        body.light-theme input,
        body.light-theme select,
        body.light-theme textarea {
            box-shadow: none;
        }
        body.light-theme input,
        body.light-theme select,
        body.light-theme textarea {
            background: var(--input-bg) !important;
            color: var(--input-text) !important;
            border-color: var(--input-border) !important;
        }
        body.light-theme .spread-info,
        body.light-theme .account-label,
        body.light-theme .log-time {
            color: var(--text-muted) !important;
        }

        /* Light-mode rescue layer for legacy inline dark styles */
        body.light-theme [style*="background:#111827"],
        body.light-theme [style*="background:#1a1a2e"],
        body.light-theme [style*="background:#161629"],
        body.light-theme [style*="background:#333"],
        body.light-theme [style*="background:#555"],
        body.light-theme [style*="background:rgba(0,0,0,0.35)"] {
            background: var(--panel-bg-strong) !important;
            color: var(--text-main) !important;
        }

        body.light-theme [style*="border:1px solid #333"],
        body.light-theme [style*="border:1px solid #444"],
        body.light-theme [style*="border:1px solid #555"],
        body.light-theme [style*="border:1px solid #2c3e50"] {
            border-color: var(--panel-border) !important;
        }

        body.light-theme [style*="color:#fff"],
        body.light-theme [style*="color:#aaa"],
        body.light-theme [style*="color:#888"],
        body.light-theme [style*="color:#666"],
        body.light-theme [style*="color:#bbb"] {
            color: var(--text-main) !important;
        }

        body.light-theme .strength-bar {
            background: rgba(31,41,55,0.14) !important;
        }

        body.light-theme .positions-mini .position-row {
            border-top-color: var(--line-soft) !important;
        }

        body.light-theme #tradingToggleWrap {
            background: rgba(120, 130, 145, 0.18) !important;
            border-color: var(--panel-border) !important;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header" style="position:relative">
            <h1>ADVANCED MULTI-SYMBOL SCALPER</h1>
            <p>LIVE BUILD 2026-03-27 20:52 | CRT + TBS | 5-Min: GOLD | EURUSD | GBPUSD</p>
            <p style="color:#f39c12;font-size:0.9em;font-weight:600;margin-top:4px;letter-spacing:0.5px">🎯 Managed by PAUL DE RIRE</p>
            <div id="liveHeartbeat" style="margin-top:6px;font-size:0.78em;color:#6dd5ff;letter-spacing:0.3px;">LIVE JS: starting...</div>
            <div style="position:absolute;top:10px;right:12px;display:flex;gap:8px;align-items:center">
                <button id="themeToggleBtn" class="theme-toggle" onclick="toggleTheme()" title="Switch night/light mode">☾ Night</button>
                <!-- Trading Enabled kill-switch -->
                <div id="tradingToggleWrap" style="display:flex;align-items:center;gap:6px;background:rgba(0,0,0,0.35);border:1px solid #555;border-radius:8px;padding:6px 14px;cursor:pointer"
                     onclick="toggleTrading()" title="Enable or disable ALL bots from opening new trades">
                    <span id="tradingDot" style="width:10px;height:10px;border-radius:50%;background:#27ae60;display:inline-block;box-shadow:0 0 6px #27ae60"></span>
                    <span id="tradingLabel" style="font-size:0.82em;font-weight:700;color:#27ae60">TRADING ON</span>
                </div>
                  <button id="restartAppBtn" class="btn-restart" onclick="restartApp()" title="Stop all bots and restart the dashboard process">&#9851; RESTART APP</button>
            </div>
        </div>
        
        <div class="account-bar">
            <div class="account-item"><div class="account-label">Trader</div><div class="account-value" style="color:#f39c12;font-size:1.1em;letter-spacing:0.3px">PAUL</div></div>
            <div class="account-item"><div class="account-label">Account</div><div class="account-value" id="accountId">-</div></div>
            <div class="account-item"><div class="account-label">Balance</div><div class="account-value" id="balance">$0.00</div></div>
            <div class="account-item"><div class="account-label">Equity</div><div class="account-value" id="equity">$0.00</div></div>
            <div class="account-item"><div class="account-label">Profit</div><div class="account-value" id="profit">$0.00</div></div>
            <div class="account-item"><div class="account-label">Open Loss Reach</div><div class="account-value" id="openLossReach" style="color:#e74c3c">$0.00</div></div>
            <div class="account-item"><div class="account-label">Positions</div><div class="account-value" id="totalPos">0</div></div>
            <div class="account-item"><div class="account-label">Trades</div><div class="account-value" id="trades">0</div></div>
            <div class="account-item" style="border-left:2px solid #ffd700;padding-left:15px">
                <div class="account-label">Daily Goal
                    <span onclick="openGoalEditor()" title="Edit target" style="cursor:pointer;margin-left:5px;font-size:0.85em;color:#888">&#9998;</span>
                </div>
                <!-- Display mode -->
                <div id="dailyGoalDisplay">
                    <div class="account-value" id="dailyGoal">$0/$20</div>
                    <div style="width:80px;height:6px;background:#333;border-radius:3px;margin-top:4px">
                        <div id="goalBar" style="height:100%;background:linear-gradient(90deg,#e74c3c,#f39c12,#27ae60);border-radius:3px;width:0%;transition:width 0.3s"></div>
                    </div>
                </div>
                <!-- Edit mode (hidden by default) -->
                <div id="goalEditor" style="display:none;margin-top:4px">
                    <div style="display:flex;align-items:center;gap:4px">
                        <span style="color:#aaa;font-size:0.8em">$</span>
                        <input id="goalInput" type="number" min="1" step="0.5"
                            style="width:65px;background:#1a1a2e;border:1px solid #ffd700;color:#fff;border-radius:4px;padding:2px 5px;font-size:0.85em"
                            onkeydown="if(event.key==='Enter')saveGoal();if(event.key==='Escape')closeGoalEditor()" />
                        <button onclick="saveGoal()" style="background:#27ae60;color:#fff;border:none;border-radius:3px;padding:2px 6px;cursor:pointer;font-size:0.8em">&#10003;</button>
                        <button onclick="closeGoalEditor()" style="background:#555;color:#fff;border:none;border-radius:3px;padding:2px 6px;cursor:pointer;font-size:0.8em">&#10005;</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Tab navigation -->
        <div class="tab-nav">
            <button class="tab-btn active" id="tabBtn5m"        onclick="switchTab('5min')">&#9889; 5-Min SMC Bot</button>
            <button class="tab-btn gold-tab"  id="tabBtnGold"    onclick="switchTab('gold')">&#127950; Gold 1-Min</button>
            <button class="tab-btn daytrade-tab" id="tabBtnDay"  onclick="switchTab('daytrade')">&#9728; Gold Day Trade</button>
            <button class="tab-btn btc-tab" id="tabBtnBtc"       onclick="switchTab('btc')">&#8383; BTC Standalone</button>
            <button class="tab-btn hist-tab" id="tabBtnHist"     onclick="switchTab('history')">&#128202; History</button>
            <button class="tab-btn" id="tabBtnReports" onclick="switchTab('reports')" style="background:#1a3a2a;color:#27ae60">&#128196; Reports</button>
            <button class="tab-btn" id="tabBtnLoss" onclick="switchTab('lossreach')" style="background:#3a1a1a;color:#ff8a80">&#128200; Loss Reach</button>
        </div>

        <!-- TAB 1: 5-Min SMC Bot -->
        <div id="tab-5min" class="tab-pane">
        <!-- 5-Min header bar — same style as Gold tabs -->
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px;padding:12px 16px;background:rgba(255,215,0,0.06);border-radius:10px;border:1px solid rgba(255,215,0,0.18)">
            <div>
                <span style="font-size:1.1em;font-weight:700;color:#ffd700">&#9889; 5-MIN SMC BOT</span>
                <span style="color:#888;font-size:0.82em;margin-left:12px">CRT + TBS &nbsp;|&nbsp; GOLD &bull; EURUSD &bull; GBPUSD &nbsp;|&nbsp; Loop: 10s</span>
            </div>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                <button class="btn btn-start" id="btnStart" onclick="startBot()" disabled>Start Bot</button>
                <button class="btn btn-stop"  id="btnStop"  onclick="stopBot()"  disabled>Stop Bot</button>
                <button class="btn btn-connect" id="btnConnect" onclick="manualConnect()">Connect MT5</button>
                <span>
                    <span class="status-dot" id="statusDot"></span>
                    <span id="botStatus" style="color:#888">Connecting...</span>
                </span>
                <span id="connHint" style="font-size:0.78em;color:#f39c12;max-width:360px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis"></span>
                <span style="background:#1a1a2e;border:1px solid #333;border-radius:6px;padding:4px 12px;font-size:0.82em">
                    Opened:&nbsp;<b id="smcOpened" style="color:#27ae60">0</b>
                    &nbsp;|&nbsp;P&amp;L:&nbsp;<b id="smcPnl" style="color:#ffd700">$0.00</b>
                </span>
            </div>
        </div>
        <div class="symbols-grid" id="symbolsGrid"></div>
        
        <!-- Bottom: 2-column row — positions + log -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px">

            <!-- Open Positions -->
            <div class="card">
                <h3>Open Positions</h3>
                <div style="max-height:300px;overflow-y:auto">
                    <table class="pos-table">
                        <thead><tr><th>Symbol</th><th>Type</th><th>Lots</th><th>Entry</th><th>P&amp;L</th></tr></thead>
                        <tbody id="posBody"><tr><td colspan="5" style="text-align:center;color:#888">No positions</td></tr></tbody>
                    </table>
                </div>
            </div>

            <!-- Activity Log -->
            <div class="card">
                <h3>Activity Log</h3>
                <div class="logs" id="logs" style="max-height:300px"></div>
            </div>

        </div>

        </div><!-- /tab-5min -->

        <!-- TAB 2: Gold 1-Min Scalper -->
        <div id="tab-gold" class="tab-pane" style="display:none">

            <!-- Header bar -->
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px;padding:12px 18px;background:rgba(255,215,0,0.05);border-radius:10px;border:1px solid rgba(255,215,0,0.22)">
                <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
                    <span style="font-size:1.15em;font-weight:800;color:#ffd700">&#127950; GOLD 1-MIN SCALPER</span>
                    <span style="font-size:0.72em;background:rgba(255,215,0,0.12);color:#ffd700;border:1px solid rgba(255,215,0,0.3);border-radius:4px;padding:2px 7px">HTF\u2193MS\u2193S&amp;D\u2193Liq\u2193ICT\u2193Momentum</span>
                    <span id="goldSessionBadge" class="session-badge-off">Off-Hours</span>
                    <span class="gold-stat-chip">ATR: <b id="goldAtrVal" style="color:#ffd700">--</b></span>
                    <span class="gold-stat-chip">Spread: <b id="goldSpreadVal">--</b> pts</span>
                </div>
                <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                    <button class="btn btn-start" id="btnGoldStart" onclick="startGold()" disabled>Start Gold</button>
                    <button class="btn btn-stop"  id="btnGoldStop"  onclick="stopGold()"  disabled>Stop Gold</button>
                    <span><span class="status-dot status-stopped" id="goldDot"></span><span id="goldStatus" style="color:#888">Idle</span></span>
                    <span style="background:#1a1a2e;border:1px solid #333;border-radius:6px;padding:4px 12px;font-size:0.82em">
                        Trades:&nbsp;<b id="goldOpened" style="color:#27ae60">0</b>
                        &nbsp;|&nbsp;Recycled:&nbsp;<b id="goldRecycled" style="color:#ffd700">0</b>
                        &nbsp;|&nbsp;W:&nbsp;<b id="goldWins" style="color:#27ae60">0</b>
                        &nbsp;L:&nbsp;<b id="goldLosses" style="color:#e74c3c">0</b>
                    </span>
                </div>
            </div>

            <!-- Main 2-col: live card + config -->
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:15px">

                <!-- Live GOLD signal card -->
                <div class="card" style="border:1px solid rgba(255,215,0,0.20);background:rgba(255,215,0,0.02)">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                        <span id="goldSymbolTitle" style="font-size:1.3em;font-weight:800;color:#ffd700">GOLD / XAU</span>
                        <span class="signal-badge signal-none" id="goldSignalBadge">NONE</span>
                    </div>
                    <div class="gold-price-big" id="goldPriceBig">2000.00</div>
                    <div style="display:flex;gap:12px;margin:10px 0;flex-wrap:wrap">
                        <span>BUY score: <b id="goldBuyScore" style="color:#27ae60">0</b></span>
                        <span>SELL score: <b id="goldSellScore" style="color:#e74c3c">0</b></span>
                        <span id="goldPatternChip" style="display:none" class="pattern-chip"></span>
                    </div>
                    <!-- CRT indicator bar (top-down) -->
                    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px;font-size:0.76em">
                        <span class="gold-stat-chip" title="Top-down HTF alignment (H4+H1+M15)">HTF: <b id="goldIndHtf" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip" title="CRT Bull/Bear pattern detected on signal bar">CRT: <b id="goldIndCrt" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip" title="Reference candle range Hi/Lo (used for TP)">Ref: <b id="goldIndRef" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip" title="Sweep low = BUY SL base (below liquidity raid)">Sw↓: <b id="goldIndSwL" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip" title="Sweep high = SELL SL base (above liquidity raid)">Sw↑: <b id="goldIndSwH" style="color:#aaa">--</b></span>
                    </div>
                    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;font-size:0.76em">
                        <span class="gold-stat-chip">Score: <b id="goldIndScore" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">RSI: <b id="goldIndRsi" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">BB%: <b id="goldIndBb" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">ATR: <b id="goldIndAtr" style="color:#aaa">--</b></span>
                    </div>
                    <div style="display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap">
                        <span style="color:#888;font-size:0.82em">Positions: <b id="goldPosCount" style="color:#fff">0</b></span>
                        <span style="color:#888;font-size:0.82em">P&amp;L: <b id="goldLivePnl" style="color:#fff">$0.00</b></span>
                    </div>
                    <div class="positions-mini" id="goldPosMini" style="max-height:160px;overflow-y:auto"></div>
                </div>

                <!-- Config panel -->
                <div class="card">
                    <h3 style="margin-bottom:14px">Gold Parameters <span style="color:#ffd700;font-size:0.8em">(live — saved on click)</span></h3>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 18px">
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Symbol</label>
                            <select id="gCfgSymbol"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px;font-size:0.9em">
                                <option value="GOLD">GOLD</option>
                            </select>
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Lot Size <span style="color:#666">(0=auto)</span></label>
                            <input id="gCfgLot" type="number" min="0" max="100" step="0.01" value="0"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Max Positions</label>
                            <input id="gCfgMaxPos" type="number" min="1" max="10" step="1" value="3"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Max Spread (pts)</label>
                            <input id="gCfgSpread" type="number" min="10" max="500" step="5" value="80"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Min Score <span style="color:#555">(max ~22)</span></label>
                            <input id="gCfgScore" type="number" min="1" max="13" step="1" value="4"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">SL fallback (xATR) <span style="color:#555">(struct preferred)</span></label>
                            <input id="gCfgSl" type="number" min="0.5" max="5" step="0.1" value="1.5"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">TP = R-multiple of SL dist</label>
                            <input id="gCfgTp" type="number" min="1" max="10" step="0.5" value="3.0"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Recycle at % TP</label>
                            <input id="gCfgRecycle" type="number" min="10" max="100" step="5" value="50"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div style="display:flex;flex-direction:column;justify-content:flex-end;gap:6px">
                            <label style="color:#888;font-size:0.8em;display:flex;align-items:center;gap:6px;cursor:pointer">
                                <input type="checkbox" id="gCfgSession" checked style="accent-color:#ffd700"> Session filter
                            </label>
                            <label style="color:#888;font-size:0.8em;display:flex;align-items:center;gap:6px;cursor:pointer">
                                <input type="checkbox" id="gCfgPatterns" checked style="accent-color:#ffd700"> Candle patterns
                            </label>
                        </div>
                    </div>
                    <div style="margin-top:14px;display:flex;align-items:center;gap:10px">
                        <button onclick="saveGoldConfig()" style="background:#b8860b;color:#fff;border:none;border-radius:6px;padding:7px 22px;font-weight:700;cursor:pointer;font-size:0.9em">&#128190; Save Config</button>
                        <span id="goldCfgSaved" style="color:#27ae60;font-size:0.82em;display:none">&#10003; Saved</span>
                    </div>
                </div>
            </div>

            <!-- Bottom 2-col: positions table + gold log -->
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px">
                <div class="card">
                    <h3>Gold Open Positions</h3>
                    <div style="max-height:280px;overflow-y:auto">
                        <table class="pos-table">
                            <thead><tr><th>Type</th><th>Lots</th><th>Entry</th><th>Current</th><th>SL</th><th>P&amp;L</th><th>Max LR</th><th>TP%</th></tr></thead>
                            <tbody id="goldPosBody"><tr><td colspan="8" style="text-align:center;color:#888">No gold positions</td></tr></tbody>
                        </table>
                    </div>
                </div>
                <div class="card">
                    <h3>Gold Log <span style="color:#ffd700;font-size:0.8em">[1-Min &bull; 3s loop]</span></h3>
                    <div class="logs" id="goldLogs" style="max-height:280px"></div>
                </div>
            </div>

        </div><!-- /tab-gold -->

        <!-- TAB 4: Gold Day Trading Bot -->
        <div id="tab-daytrade" class="tab-pane" style="display:none">

            <!-- Header bar -->
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px;padding:12px 18px;background:rgba(243,156,18,0.06);border-radius:10px;border:1px solid rgba(243,156,18,0.26)">
                <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
                    <span style="font-size:1.15em;font-weight:800;color:#f39c12">&#9728; GOLD DAY TRADE BOT</span>
                    <span style="font-size:0.72em;background:rgba(243,156,18,0.12);color:#f39c12;border:1px solid rgba(243,156,18,0.3);border-radius:4px;padding:2px 7px">SMC&#8203;&#43;&#8203;ICT&#8203;&#43;&#8203;Fib &#8226; H4 Candles &#8226; Loop: 30s</span>
                    <span id="daySessionBadge" class="session-badge-off">Off-Hours</span>
                    <span class="gold-stat-chip">ATR: <b id="dayAtrVal" style="color:#f39c12">--</b></span>
                    <span class="gold-stat-chip">Spread: <b id="daySpreadVal">--</b> pts</span>
                </div>
                <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                    <button class="btn btn-start" id="btnDayStart" onclick="startDayTrade()" disabled>Start Day Trade</button>
                    <button class="btn btn-stop"  id="btnDayStop"  onclick="stopDayTrade()"  disabled>Stop</button>
                    <span><span class="status-dot status-stopped" id="dayDot"></span><span id="dayStatus" style="color:#888">Idle</span></span>
                    <span style="background:#1a1a2e;border:1px solid #333;border-radius:6px;padding:4px 12px;font-size:0.82em">
                        Trades:&nbsp;<b id="dayOpened" style="color:#27ae60">0</b>
                        &nbsp;|&nbsp;Recycled:&nbsp;<b id="dayRecycled" style="color:#f39c12">0</b>
                        &nbsp;|&nbsp;W:&nbsp;<b id="dayWins" style="color:#27ae60">0</b>
                        &nbsp;L:&nbsp;<b id="dayLosses" style="color:#e74c3c">0</b>
                        &nbsp;|&nbsp;P&amp;L:&nbsp;<b id="dayLivePnl" style="color:#fff">$0.00</b>
                    </span>
                </div>
            </div>

            <!-- Main 2-col: live card + config -->
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:15px">

                <!-- Live signal card -->
                <div class="card" style="border:1px solid rgba(243,156,18,0.20);background:rgba(243,156,18,0.02)">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
                        <span id="daySymbolTitle" style="font-size:1.3em;font-weight:800;color:#f39c12">GOLD / XAU &mdash; H4</span>
                        <span class="signal-badge signal-none" id="daySignalBadge">NONE</span>
                    </div>
                    <div class="gold-price-big" id="dayPriceBig" style="color:#f39c12">2000.00</div>
                    <div style="display:flex;gap:12px;margin:10px 0;flex-wrap:wrap">
                        <span>BUY score: <b id="dayBuyScore" style="color:#27ae60">0</b></span>
                        <span>SELL score: <b id="daySellScore" style="color:#e74c3c">0</b></span>
                        <span id="dayPatternChip" style="display:none" class="pattern-chip"></span>
                    </div>
                    <!-- Indicator bar -->
                    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;font-size:0.76em">
                        <span class="gold-stat-chip">EMA: <b id="dayIndEma" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">RSI: <b id="dayIndRsi" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">BB%: <b id="dayIndBb" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">Score: <b id="dayIndScore" style="color:#aaa">--</b></span>
                        <span class="gold-stat-chip">ATR: <b id="dayIndAtr" style="color:#aaa">--</b></span>
                    </div>
                    <div style="display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap">
                        <span style="color:#888;font-size:0.82em">Positions: <b id="dayPosCount" style="color:#fff">0</b></span>
                    </div>
                    <div class="positions-mini" id="dayPosMini" style="max-height:160px;overflow-y:auto"></div>
                </div>

                <!-- Config panel -->
                <div class="card">
                    <h3 style="margin-bottom:14px">Day Trade Parameters <span style="color:#f39c12;font-size:0.8em">(live — saved on click)</span></h3>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px 18px">
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Symbol</label>
                            <select id="dCfgSymbol"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px;font-size:0.9em">
                                <option value="GOLD">GOLD</option>
                            </select>
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Lot Size <span style="color:#666">(0=auto)</span></label>
                            <input id="dCfgLot" type="number" min="0" max="100" step="0.01" value="0"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Max Positions</label>
                            <input id="dCfgMaxPos" type="number" min="1" max="10" step="1" value="2"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Max Spread (pts)</label>
                            <input id="dCfgSpread" type="number" min="10" max="500" step="5" value="80"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Min Confluence Score</label>
                            <input id="dCfgScore" type="number" min="1" max="25" step="1" value="8"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">SL (xATR)</label>
                            <input id="dCfgSl" type="number" min="0.5" max="5" step="0.1" value="1.5"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">TP = R-multiple of SL</label>
                            <input id="dCfgTp" type="number" min="1" max="10" step="0.5" value="3.0"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div>
                            <label style="color:#888;font-size:0.8em;display:block;margin-bottom:3px">Recycle at % TP</label>
                            <input id="dCfgRecycle" type="number" min="10" max="100" step="5" value="60"
                                style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px;font-size:0.9em">
                        </div>
                        <div style="display:flex;flex-direction:column;justify-content:flex-end;gap:6px">
                            <label style="color:#888;font-size:0.8em;display:flex;align-items:center;gap:6px;cursor:pointer">
                                <input type="checkbox" id="dCfgSession" checked style="accent-color:#f39c12"> Session filter
                            </label>
                            <label style="color:#888;font-size:0.8em;display:flex;align-items:center;gap:6px;cursor:pointer">
                                <input type="checkbox" id="dCfgPatterns" checked style="accent-color:#f39c12"> Candle patterns
                            </label>
                        </div>
                    </div>
                    <div style="margin-top:14px;display:flex;align-items:center;gap:10px">
                        <button onclick="saveDayConfig()" style="background:#b86c00;color:#fff;border:none;border-radius:6px;padding:7px 22px;font-weight:700;cursor:pointer;font-size:0.9em">&#128190; Save Config</button>
                        <span id="dayCfgSaved" style="color:#27ae60;font-size:0.82em;display:none">&#10003; Saved</span>
                    </div>
                </div>
            </div>

            <!-- Bottom 2-col: positions table + log -->
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px">
                <div class="card">
                    <h3>Day Trade Positions</h3>
                    <div style="max-height:280px;overflow-y:auto">
                        <table class="pos-table">
                            <thead><tr><th>Type</th><th>Lots</th><th>Entry</th><th>Current</th><th>SL</th><th>P&amp;L</th><th>Max LR</th><th>TP%</th></tr></thead>
                            <tbody id="dayPosBody"><tr><td colspan="8" style="text-align:center;color:#888">No day trade positions</td></tr></tbody>
                        </table>
                    </div>
                </div>
                <div class="card">
                    <h3>Day Trade Log <span style="color:#f39c12;font-size:0.8em">[H4 &bull; 30s loop]</span></h3>
                    <div class="logs" id="dayLogs" style="max-height:280px"></div>
                </div>
            </div>

        </div><!-- /tab-daytrade -->

        <!-- TAB: BTC Standalone -->
        <div id="tab-btc" class="tab-pane" style="display:none">
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px;padding:12px 18px;background:rgba(243,156,18,0.08);border-radius:10px;border:1px solid rgba(243,156,18,0.28)">
                <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
                    <span style="font-size:1.15em;font-weight:800;color:#f39c12">&#8383; BTCUSD STANDALONE BOT</span>
                    <span style="font-size:0.72em;background:rgba(243,156,18,0.12);color:#f39c12;border:1px solid rgba(243,156,18,0.3);border-radius:4px;padding:2px 7px">Independent controls per timeframe</span>
                    <span style="font-size:0.68em;background:rgba(39,174,96,0.12);color:#27ae60;border:1px solid rgba(39,174,96,0.35);border-radius:4px;padding:2px 7px">independent-state build 2026-03-29</span>
                </div>
                <div id="btcConnState" style="font-size:0.84em;color:#aaa">Checking connection...</div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;margin-bottom:14px">
                <div class="symbol-card" style="border:1px solid rgba(243,156,18,0.25)">
                    <div class="symbol-header">
                        <span class="symbol-name btc">BTC 1-Min Scalper</span>
                        <span class="signal-badge signal-none" id="btc1mSignalBadge">NONE</span>
                    </div>
                    <div class="price-row">
                        <span class="price-big" id="btc1mPrice">--.-----</span>
                        <span class="spread-info">Spread: <span id="btc1mSpreadPts">--</span> pts</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin:8px 0">
                        <span>BUY Score: <b id="btc1mBuyScore" style="color:#27ae60">0</b></span>
                        <span>SELL Score: <b id="btc1mSellScore" style="color:#e74c3c">0</b></span>
                    </div>
                    <div>Strength: <span id="btc1mStrength">0</span>%</div>
                    <div class="strength-bar"><div class="strength-fill" id="btc1mBar" style="width:0%"></div></div>
                    <div id="btc1mAnalysis" style="font-size:0.75em;color:#888;margin-top:8px">No signals detected</div>
                    <div class="positions-mini" id="btc1mPosMini"><div style="color:#666;font-size:0.9em;margin-top:5px;">No positions</div></div>
                    <div style="display:flex;align-items:center;gap:6px;margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.08)">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">Lot</label>
                        <input id="btc1mLot" type="number" min="0" max="100" step="0.01" value="0" style="width:62px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em" onkeydown="if(event.key==='Enter')saveBtcCardConfig('1m')">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">MaxPos</label>
                        <input id="btc1mMaxPos" type="number" min="1" max="10" step="1" value="3" style="width:46px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em" onkeydown="if(event.key==='Enter')saveBtcCardConfig('1m')">
                        <button onclick="saveBtcCardConfig('1m')" style="background:#2980b9;color:#fff;border:none;border-radius:4px;padding:3px 9px;font-size:0.78em;cursor:pointer;white-space:nowrap">Save</button>
                        <span id="btc1mSaved" style="color:#27ae60;font-size:0.75em;display:none">&#10003;</span>
                        <span id="btc1mStatus" style="margin-left:auto;font-weight:700;color:#888;font-size:0.82em">Idle</span>
                    </div>
                    <div style="display:flex;gap:10px;margin-top:10px">
                        <button id="btc1mStart" onclick="btcControl('1m', true)" class="btn btn-start">Start 1m</button>
                        <button id="btc1mStop" onclick="btcControl('1m', false)" class="btn btn-stop">Stop</button>
                    </div>
                    <div style="margin:10px 0 0;padding:8px 10px;background:#111827;border:1px solid #2c3e50;border-radius:6px;font-size:0.82em;line-height:1.35">
                        <span style="color:#888">Why no trade?</span>
                        <div id="btc1mReason" style="color:#aaa;margin-top:4px">Checking...</div>
                    </div>
                </div>

                <div class="symbol-card" style="border:1px solid rgba(243,156,18,0.25)">
                    <div class="symbol-header">
                        <span class="symbol-name btc">BTC 5-Min Bot</span>
                        <span class="signal-badge signal-none" id="btc5mSignalBadge">NONE</span>
                    </div>
                    <div class="price-row">
                        <span class="price-big" id="btc5mPrice">--.-----</span>
                        <span class="spread-info">Spread: <span id="btc5mSpreadPts">--</span> pts</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin:8px 0">
                        <span>BUY Score: <b id="btc5mBuyScore" style="color:#27ae60">0</b></span>
                        <span>SELL Score: <b id="btc5mSellScore" style="color:#e74c3c">0</b></span>
                    </div>
                    <div>Strength: <span id="btc5mStrength">0</span>%</div>
                    <div class="strength-bar"><div class="strength-fill" id="btc5mBar" style="width:0%"></div></div>
                    <div id="btc5mAnalysis" style="font-size:0.75em;color:#888;margin-top:8px">No signals detected</div>
                    <div class="positions-mini" id="btc5mPosMini"><div style="color:#666;font-size:0.9em;margin-top:5px;">No positions</div></div>
                    <div style="display:flex;align-items:center;gap:6px;margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.08)">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">Lot</label>
                        <input id="btc5mLot" type="number" min="0" max="100" step="0.01" value="0" style="width:62px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em" onkeydown="if(event.key==='Enter')saveBtcCardConfig('5m')">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">MaxPos</label>
                        <input id="btc5mMaxPos" type="number" min="1" max="10" step="1" value="1" style="width:46px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em" onkeydown="if(event.key==='Enter')saveBtcCardConfig('5m')">
                        <button onclick="saveBtcCardConfig('5m')" style="background:#2980b9;color:#fff;border:none;border-radius:4px;padding:3px 9px;font-size:0.78em;cursor:pointer;white-space:nowrap">Save</button>
                        <span id="btc5mSaved" style="color:#27ae60;font-size:0.75em;display:none">&#10003;</span>
                        <span id="btc5mStatus" style="margin-left:auto;font-weight:700;color:#888;font-size:0.82em">Idle</span>
                    </div>
                    <div style="display:flex;gap:10px;margin-top:10px;align-items:center;flex-wrap:wrap">
                        <button id="btc5mStart" onclick="btcControl('5m', true)" class="btn btn-start">Start 5m</button>
                        <button id="btc5mStop" onclick="btcControl('5m', false)" class="btn btn-stop">Stop</button>
                        <span id="btc5mMode" style="color:#aaa;font-size:0.82em">Independent standby</span>
                        <span id="btc5mSignal" style="color:#aaa;font-size:0.82em">0/0</span>
                        <span id="btc5mSpread" style="color:#aaa;font-size:0.82em">0/0</span>
                        <span id="btc5mPos" style="color:#aaa;font-size:0.82em">0/0</span>
                    </div>
                    <div style="margin:10px 0 0;padding:8px 10px;background:#111827;border:1px solid #2c3e50;border-radius:6px;font-size:0.82em;line-height:1.35">
                        <span style="color:#888">Why no trade?</span>
                        <div id="btc5mReason" style="color:#aaa;margin-top:4px">Checking...</div>
                    </div>
                </div>

                <div class="symbol-card" style="border:1px solid rgba(243,156,18,0.25)">
                    <div class="symbol-header">
                        <span class="symbol-name btc">BTC Day Trade (H4)</span>
                        <span class="signal-badge signal-none" id="btc4hSignalBadge">NONE</span>
                    </div>
                    <div class="price-row">
                        <span class="price-big" id="btc4hPrice">--.-----</span>
                        <span class="spread-info">Spread: <span id="btc4hSpreadPts">--</span> pts</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin:8px 0">
                        <span>BUY Score: <b id="btc4hBuyScore" style="color:#27ae60">0</b></span>
                        <span>SELL Score: <b id="btc4hSellScore" style="color:#e74c3c">0</b></span>
                    </div>
                    <div>Strength: <span id="btc4hStrength">0</span>%</div>
                    <div class="strength-bar"><div class="strength-fill" id="btc4hBar" style="width:0%"></div></div>
                    <div id="btc4hAnalysis" style="font-size:0.75em;color:#888;margin-top:8px">No signals detected</div>
                    <div class="positions-mini" id="btc4hPosMini"><div style="color:#666;font-size:0.9em;margin-top:5px;">No positions</div></div>
                    <div style="display:flex;align-items:center;gap:6px;margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.08)">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">Lot</label>
                        <input id="btc4hLot" type="number" min="0" max="100" step="0.01" value="0" style="width:62px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em" onkeydown="if(event.key==='Enter')saveBtcCardConfig('4h')">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">MaxPos</label>
                        <input id="btc4hMaxPos" type="number" min="1" max="10" step="1" value="2" style="width:46px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em" onkeydown="if(event.key==='Enter')saveBtcCardConfig('4h')">
                        <button onclick="saveBtcCardConfig('4h')" style="background:#2980b9;color:#fff;border:none;border-radius:4px;padding:3px 9px;font-size:0.78em;cursor:pointer;white-space:nowrap">Save</button>
                        <span id="btc4hSaved" style="color:#27ae60;font-size:0.75em;display:none">&#10003;</span>
                        <span id="btc4hStatus" style="margin-left:auto;font-weight:700;color:#888;font-size:0.82em">Idle</span>
                    </div>
                    <div style="display:flex;gap:10px;margin-top:10px;align-items:center;flex-wrap:wrap">
                        <button id="btc4hStart" onclick="btcControl('4h', true)" class="btn btn-start">Start 4h</button>
                        <button id="btc4hStop" onclick="btcControl('4h', false)" class="btn btn-stop">Stop</button>
                        <span id="btc4hSession" style="color:#aaa;font-size:0.82em">Session: --</span>
                    </div>
                    <div style="margin:10px 0 0;padding:8px 10px;background:#111827;border:1px solid #2c3e50;border-radius:6px;font-size:0.82em;line-height:1.35">
                        <span style="color:#888">Why no trade?</span>
                        <div id="btc4hReason" style="color:#aaa;margin-top:4px">Checking...</div>
                    </div>
                </div>
            </div>

            <div class="card" style="border:1px solid rgba(243,156,18,0.22)">
                <h3 style="color:#f39c12">BTC Mode Notes</h3>
                <div style="color:#aaa;font-size:0.9em;line-height:1.5">
                    1m mode uses the fast scalper engine, 5m mode runs the CRT/TBS bot independently (without BTC-only locking), and 4h mode uses the day-trade engine on H4 bars. Each mode is controlled independently from this tab.
                </div>
            </div>

            <div class="card" style="margin-top:14px;border:1px solid rgba(243,156,18,0.35)">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:10px">
                    <span style="font-size:1.0em;font-weight:800;color:#f39c12">&#8383; BTC CONFIG + TUNING</span>
                    <span style="font-size:0.8em;color:#aaa">Presets + walk-forward + runtime settings</span>
                </div>

                <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
                    <span style="color:#888;font-size:0.85em">Preset:</span>
                    <button onclick="applyBtcPreset('conservative')" style="background:#2c3e50;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer">Conservative</button>
                    <button onclick="applyBtcPreset('balanced')" style="background:#34495e;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer">Balanced</button>
                    <button onclick="applyBtcPreset('aggressive')" style="background:#7f2a1d;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer">Aggressive</button>
                    <span id="btcPresetMsg" style="font-size:0.82em;color:#27ae60"></span>
                </div>

                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-bottom:10px">
                    <div style="border:1px solid #333;border-radius:8px;padding:10px;background:#161629">
                        <h3 style="margin:0 0 10px;color:#f39c12">BTC 1m Config</h3>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 12px">
                            <label style="color:#888;font-size:0.8em">Lot Size<input id="btcCfg1mLot" type="number" min="0" max="100" step="0.01" value="0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Max Positions<input id="btcCfg1mMaxPos" type="number" min="1" max="10" step="1" value="3" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Max Spread<input id="btcCfg1mSpread" type="number" min="5" max="1000" step="5" value="80" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Min Score<input id="btcCfg1mScore" type="number" min="1" max="25" step="1" value="4" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">SL xATR<input id="btcCfg1mSl" type="number" min="0.1" max="10" step="0.1" value="1.5" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">TP xATR<input id="btcCfg1mTp" type="number" min="0.1" max="10" step="0.1" value="3.0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                        </div>
                    </div>
                    <div style="border:1px solid #333;border-radius:8px;padding:10px;background:#161629">
                        <h3 style="margin:0 0 10px;color:#f39c12">BTC 5m Config</h3>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 12px">
                            <label style="color:#888;font-size:0.8em">Lot Size<input id="btcCfg5mLot" type="number" min="0" max="100" step="0.01" value="0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Max Positions<input id="btcCfg5mMaxPos" type="number" min="1" max="10" step="1" value="1" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Max Spread<input id="btcCfg5mSpread" type="number" min="5" max="2000" step="5" value="120" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Min Signal<input id="btcCfg5mMinSignal" type="number" min="0" max="100" step="1" value="0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Lot Multiplier<input id="btcCfg5mLotMul" type="number" min="0.1" max="10" step="0.1" value="1.0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em;display:flex;align-items:center;gap:8px;margin-top:18px">
                                <input id="btcCfg5mEnabled" type="checkbox" checked style="accent-color:#f39c12"> Enabled
                            </label>
                        </div>
                    </div>
                    <div style="border:1px solid #333;border-radius:8px;padding:10px;background:#161629">
                        <h3 style="margin:0 0 10px;color:#f39c12">BTC 4h Config</h3>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 12px">
                            <label style="color:#888;font-size:0.8em">Lot Size<input id="btcCfg4hLot" type="number" min="0" max="100" step="0.01" value="0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Max Positions<input id="btcCfg4hMaxPos" type="number" min="1" max="10" step="1" value="2" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Max Spread<input id="btcCfg4hSpread" type="number" min="5" max="1000" step="5" value="80" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">Min Score<input id="btcCfg4hScore" type="number" min="1" max="25" step="1" value="8" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">SL xATR<input id="btcCfg4hSl" type="number" min="0.1" max="10" step="0.1" value="1.5" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                            <label style="color:#888;font-size:0.8em">TP xATR<input id="btcCfg4hTp" type="number" min="0.1" max="10" step="0.1" value="3.0" style="width:100%;margin-top:3px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:5px 8px"></label>
                        </div>
                    </div>
                </div>
                <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
                    <button onclick="saveBtcConfig()" style="background:#b8860b;color:#fff;border:none;border-radius:6px;padding:7px 18px;cursor:pointer;font-weight:700">&#128190; Save BTC Config</button>
                    <span id="btcCfgSaved" style="color:#27ae60;font-size:0.82em;display:none">&#10003; Saved</span>
                </div>

                <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
                    <span style="color:#888;font-size:0.85em">Walk-forward:</span>
                    <select id="btcWfProfile" style="background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:5px;padding:5px 8px">
                        <option value="conservative">Conservative</option>
                        <option value="balanced" selected>Balanced</option>
                        <option value="aggressive">Aggressive</option>
                    </select>
                    <label style="color:#aaa;font-size:0.82em">Train(days)
                        <input id="btcWfTrain" type="number" min="7" max="120" value="21" style="width:64px;margin-left:4px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 6px">
                    </label>
                    <label style="color:#aaa;font-size:0.82em">Test(days)
                        <input id="btcWfTest" type="number" min="3" max="60" value="7" style="width:58px;margin-left:4px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 6px">
                    </label>
                    <label style="color:#aaa;font-size:0.82em">Folds
                        <input id="btcWfFolds" type="number" min="1" max="10" value="4" style="width:46px;margin-left:4px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 6px">
                    </label>
                    <button onclick="runBtcWalkforward()" style="background:#f39c12;color:#111;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;font-weight:700">Run WF</button>
                </div>

                <div id="btcWfSummary" style="font-size:0.85em;color:#bbb;margin-bottom:8px">No walk-forward run yet.</div>
                <div style="max-height:220px;overflow-y:auto">
                    <table class="pos-table">
                        <thead><tr><th>Fold</th><th>Train P&amp;L</th><th>Train WR%</th><th>Test P&amp;L</th><th>Test WR%</th><th>Test DD%</th></tr></thead>
                        <tbody id="btcWfBody"><tr><td colspan="6" style="text-align:center;color:#777;padding:12px">Run walk-forward to populate</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div><!-- /tab-btc -->

        <!-- TAB 5: Trade History & Analytics -->
        <div id="tab-history" class="tab-pane" style="display:none">

            <!-- Header -->
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:16px;padding:12px 18px;background:rgba(52,152,219,0.07);border-radius:10px;border:1px solid rgba(52,152,219,0.22)">
                <span style="font-size:1.1em;font-weight:800;color:#3498db">&#128202; TRADE HISTORY &amp; ANALYTICS</span>
                <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
                    <button onclick="refreshAllHistory()" style="background:#2980b9;color:#fff;border:none;border-radius:5px;padding:5px 14px;cursor:pointer;font-size:0.85em">&#8635; Refresh</button>
                    <button onclick="clearHistory('',function(){refreshAllHistory()})" style="background:#555;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer;font-size:0.8em" title="Clear 5-Min history">&#128465; 5-Min</button>
                    <button onclick="clearHistory('Gold1M',function(){refreshAllHistory()})" style="background:#555;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer;font-size:0.8em" title="Clear Gold 1-Min history">&#128465; Gold1M</button>
                    <button onclick="clearHistory('GoldDay',function(){refreshAllHistory()})" style="background:#555;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer;font-size:0.8em" title="Clear Gold Day Trade history">&#128465; GoldDay</button>
                    <button onclick="clearHistory('BTC',function(){refreshAllHistory()})" style="background:#555;color:#fff;border:none;border-radius:5px;padding:5px 10px;cursor:pointer;font-size:0.8em" title="Clear BTC history">&#128465; BTC</button>
                    <button onclick="resetNewDay()" style="background:linear-gradient(135deg,#e67e22,#d35400);color:#fff;border:none;border-radius:5px;padding:5px 13px;cursor:pointer;font-size:0.85em;font-weight:700" title="Reset all counters, PnL, and history for a fresh new trading day">&#127774; New Day</button>
                </div>
            </div>

            <!-- Summary stat cards -->
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-bottom:16px">
                <div class="card" style="border:1px solid rgba(255,215,0,0.2)">
                    <div style="font-size:1em;font-weight:700;color:#ffd700;margin-bottom:10px">&#9889; 5-Min SMC</div>
                    <div style="display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:0.82em">
                        <span style="color:#888">Trades</span><b id="hist5_total" style="color:#fff">0</b>
                        <span style="color:#888">W / L</span><span><b id="hist5_wins" style="color:#27ae60">0</b> / <b id="hist5_losses" style="color:#e74c3c">0</b></span>
                        <span style="color:#888">Win Rate</span><b id="hist5_wr" style="color:#ffd700">0%</b>
                        <span style="color:#888">P&amp;L</span><b id="hist5_pnl" style="color:#fff">$0.00</b>
                        <span style="color:#888">Avg/trade</span><b id="hist5_avg" style="color:#aaa">$0.00</b>
                        <span style="color:#888">Best</span><b id="hist5_best" style="color:#27ae60">$0</b>
                        <span style="color:#888">Worst</span><b id="hist5_worst" style="color:#e74c3c">$0</b>
                    </div>
                    <div id="wb5" style="margin-top:10px"></div>
                </div>
                <div class="card" style="border:1px solid rgba(184,134,11,0.3)">
                    <div style="font-size:1em;font-weight:700;color:#ffd700;margin-bottom:10px">&#127950; Gold 1-Min</div>
                    <div style="display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:0.82em">
                        <span style="color:#888">Trades</span><b id="histG_total" style="color:#fff">0</b>
                        <span style="color:#888">W / L</span><span><b id="histG_wins" style="color:#27ae60">0</b> / <b id="histG_losses" style="color:#e74c3c">0</b></span>
                        <span style="color:#888">Win Rate</span><b id="histG_wr" style="color:#ffd700">0%</b>
                        <span style="color:#888">P&amp;L</span><b id="histG_pnl" style="color:#fff">$0.00</b>
                        <span style="color:#888">Avg/trade</span><b id="histG_avg" style="color:#aaa">$0.00</b>
                        <span style="color:#888">Best</span><b id="histG_best" style="color:#27ae60">$0</b>
                        <span style="color:#888">Worst</span><b id="histG_worst" style="color:#e74c3c">$0</b>
                    </div>
                    <div id="wbG" style="margin-top:10px"></div>
                </div>
                <div class="card" style="border:1px solid rgba(243,156,18,0.3)">
                    <div style="font-size:1em;font-weight:700;color:#f39c12;margin-bottom:10px">&#9728; Gold Day Trade</div>
                    <div style="display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:0.82em">
                        <span style="color:#888">Trades</span><b id="histD_total" style="color:#fff">0</b>
                        <span style="color:#888">W / L</span><span><b id="histD_wins" style="color:#27ae60">0</b> / <b id="histD_losses" style="color:#e74c3c">0</b></span>
                        <span style="color:#888">Win Rate</span><b id="histD_wr" style="color:#ffd700">0%</b>
                        <span style="color:#888">P&amp;L</span><b id="histD_pnl" style="color:#fff">$0.00</b>
                        <span style="color:#888">Avg/trade</span><b id="histD_avg" style="color:#aaa">$0.00</b>
                        <span style="color:#888">Best</span><b id="histD_best" style="color:#27ae60">$0</b>
                        <span style="color:#888">Worst</span><b id="histD_worst" style="color:#e74c3c">$0</b>
                    </div>
                    <div id="wbD" style="margin-top:10px"></div>
                </div>
                <div class="card" style="border:1px solid rgba(243,156,18,0.45)">
                    <div style="font-size:1em;font-weight:700;color:#f39c12;margin-bottom:10px">&#8383; BTC</div>
                    <div style="display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:0.82em">
                        <span style="color:#888">Trades</span><b id="histB_total" style="color:#fff">0</b>
                        <span style="color:#888">W / L</span><span><b id="histB_wins" style="color:#27ae60">0</b> / <b id="histB_losses" style="color:#e74c3c">0</b></span>
                        <span style="color:#888">Win Rate</span><b id="histB_wr" style="color:#ffd700">0%</b>
                        <span style="color:#888">P&amp;L</span><b id="histB_pnl" style="color:#fff">$0.00</b>
                        <span style="color:#888">Avg/trade</span><b id="histB_avg" style="color:#aaa">$0.00</b>
                        <span style="color:#888">Best</span><b id="histB_best" style="color:#27ae60">$0</b>
                        <span style="color:#888">Worst</span><b id="histB_worst" style="color:#e74c3c">$0</b>
                    </div>
                    <div id="wbB" style="margin-top:10px"></div>
                </div>
            </div>

            <!-- Cumulative P&L equity curve -->
            <div class="card" style="margin-bottom:16px">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <h3 style="margin:0;border:none;padding:0">Cumulative P&amp;L Curve</h3>
                    <span class="equity-note" style="color:#555;font-size:0.75em">per closed trade &bull; all bots</span>
                </div>
                <canvas id="equityChart" height="200" style="width:100%;display:block;border-radius:6px;background:#0d0d1a;border:1px solid rgba(255,255,255,0.08)"></canvas>
                <div id="equityLegend" style="display:flex;gap:20px;margin-top:8px;font-size:0.82em;flex-wrap:wrap;justify-content:center"></div>
            </div>

            <!-- Trade log with bot filter -->
            <div class="card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
                    <h3 style="margin:0;border:none;padding:0">Trade Log <span id="allHistTotal" style="color:#555;font-size:0.75em;font-weight:400"></span></h3>
                    <div style="display:flex;gap:5px;flex-wrap:wrap">
                        <button id="histFilter_all"     onclick="setHistFilter('all')"     style="background:#2980b9;color:#fff;border:none;border-radius:4px;padding:3px 13px;cursor:pointer;font-size:0.82em">All</button>
                        <button id="histFilter_5min"    onclick="setHistFilter('5min')"    style="background:#1a1a2e;color:#ffd700;border:1px solid rgba(255,215,0,0.3);border-radius:4px;padding:3px 13px;cursor:pointer;font-size:0.82em">&#9889; 5-Min</button>
                        <button id="histFilter_gold"    onclick="setHistFilter('gold')"    style="background:#1a1a2e;color:#ffd700;border:1px solid rgba(184,134,11,0.3);border-radius:4px;padding:3px 13px;cursor:pointer;font-size:0.82em">&#127950; Gold1M</button>
                        <button id="histFilter_daytrade" onclick="setHistFilter('daytrade')" style="background:#1a1a2e;color:#f39c12;border:1px solid rgba(243,156,18,0.3);border-radius:4px;padding:3px 13px;cursor:pointer;font-size:0.82em">&#9728; GoldDay</button>
                        <button id="histFilter_btc" onclick="setHistFilter('btc')" style="background:#1a1a2e;color:#f39c12;border:1px solid rgba(243,156,18,0.45);border-radius:4px;padding:3px 13px;cursor:pointer;font-size:0.82em">&#8383; BTC</button>
                    </div>
                </div>
                <div style="max-height:420px;overflow-y:auto">
                    <table class="pos-table">
                        <thead><tr><th>Time</th><th>Bot</th><th>Symbol</th><th>Dir</th><th>Lots</th><th>Entry</th><th>Close</th><th>P&amp;L</th><th>Max Loss Reach</th><th>&#9654;</th></tr></thead>
                        <tbody id="allHistBody"><tr><td colspan="10" style="text-align:center;color:#888;padding:24px">No closed trades yet &mdash; history appears here as positions close</td></tr></tbody>
                    </table>
                </div>
            </div>

        </div><!-- /tab-history -->

        <!-- TAB 6: Performance Reports -->
        <div id="tab-reports" class="tab-pane" style="display:none">
            <div class="card" style="margin-bottom:14px">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
                    <span style="font-size:1.1em;font-weight:800;color:#27ae60">&#128196; PERFORMANCE REPORTS</span>
                    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                        <select id="reportPeriod" onchange="loadReports()" style="background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:6px;padding:5px 10px;cursor:pointer">
                            <option value="daily">Daily</option>
                            <option value="weekly">Weekly</option>
                            <option value="monthly">Monthly</option>
                        </select>
                        <button onclick="loadReports()" style="background:#27ae60;color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer">&#8635; Refresh</button>
                        <button onclick="exportReportHtml()" style="background:#2980b9;color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer">&#8659; Export HTML</button>
                        <button onclick="showDeleteHistoryMode()" style="background:#e74c3c;color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer">&#128465; Delete History</button>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-bottom:14px;border:1px solid rgba(52,152,219,0.35)">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
                    <span style="font-size:1.0em;font-weight:800;color:#3498db">&#9993; EMAIL ALERT SETTINGS</span>
                    <span style="font-size:0.8em;color:#999">Trade close + daily summary + risk alerts</span>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 14px">
                    <label style="color:#888;font-size:0.82em">Enabled
                        <select id="emailEnabled" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                            <option value="false">Disabled</option>
                            <option value="true">Enabled</option>
                        </select>
                    </label>
                    <label style="color:#888;font-size:0.82em">SMTP Host
                        <input id="emailSmtpHost" type="text" placeholder="smtp.gmail.com" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">SMTP Port
                        <input id="emailSmtpPort" type="number" min="1" max="65535" value="587" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">Username
                        <input id="emailUsername" type="text" placeholder="you@example.com" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">Password / App Password
                        <input id="emailPassword" type="password" placeholder="********" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">From Email
                        <input id="emailFrom" type="text" placeholder="optional" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">To Email
                        <input id="emailTo" type="text" placeholder="alerts@yourmail.com" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">Daily Summary Time (UTC)
                        <input id="emailDailyTime" type="text" placeholder="23:55" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">Risk Alert Buckets ($)
                        <input id="emailRiskBuckets" type="text" placeholder="25,50,100" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                </div>
                <div style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                    <label style="color:#888;font-size:0.82em;display:flex;align-items:center;gap:6px"><input id="emailUseTls" type="checkbox" checked style="accent-color:#3498db"> TLS</label>
                    <label style="color:#888;font-size:0.82em;display:flex;align-items:center;gap:6px"><input id="emailUseSsl" type="checkbox" style="accent-color:#3498db"> SSL</label>
                    <button onclick="saveEmailSettings()" style="background:#2980b9;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Save Email Settings</button>
                    <button onclick="sendTestEmail()" style="background:#16a085;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Send Test Email</button>
                    <span id="emailCfgSaved" style="font-size:0.82em;color:#27ae60;display:none">&#10003; Saved</span>
                </div>
                <div id="emailCfgMsg" style="margin-top:8px;font-size:0.82em;color:#999">Load/save from dashboard. Password is only updated when provided.</div>
            </div>

            <div class="card" style="margin-bottom:14px;border:1px solid rgba(155,89,182,0.35)">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
                    <span style="font-size:1.0em;font-weight:800;color:#9b59b6">&#128276; PUSH + WATCHDOG + SAFETY</span>
                    <span id="advancedOpsMsg" style="font-size:0.8em;color:#999">Ready</span>
                </div>
                <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 14px;margin-bottom:8px">
                    <label style="color:#888;font-size:0.82em">Push Enabled
                        <select id="pushEnabled" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                            <option value="false">Disabled</option>
                            <option value="true">Enabled</option>
                        </select>
                    </label>
                    <label style="color:#888;font-size:0.82em">Telegram Bot Token
                        <input id="pushTelegramToken" type="password" placeholder="optional" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">Telegram Chat ID
                        <input id="pushTelegramChat" type="text" placeholder="optional" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                    <label style="color:#888;font-size:0.82em">Discord Webhook URL
                        <input id="pushDiscordWebhook" type="text" placeholder="optional" style="width:100%;margin-top:4px;background:#1a1a2e;color:#fff;border:1px solid #555;border-radius:5px;padding:6px 8px">
                    </label>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
                    <button onclick="savePushSettings()" style="background:#8e44ad;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Save Push Settings</button>
                    <button onclick="sendPushTest()" style="background:#6c5ce7;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Send Push Test</button>
                    <button onclick="activateEmergencyPause()" style="background:#c0392b;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Activate Emergency Pause</button>
                    <button onclick="releaseEmergencyPause()" style="background:#16a085;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Release Emergency Pause</button>
                    <button onclick="refreshWatchdogs()" style="background:#2c3e50;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Refresh Watchdogs</button>
                </div>
                <div id="watchdogView" style="font-size:0.82em;color:#bbb;line-height:1.5;background:#111827;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:10px;white-space:pre-wrap">Watchdog status will appear here.</div>
            </div>

            <div class="card" style="margin-bottom:14px;border:1px solid rgba(46,204,113,0.35)">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
                    <span style="font-size:1.0em;font-weight:800;color:#2ecc71">&#128202; HEALTH + RISK + REPLAY + JOURNAL</span>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
                    <button onclick="loadStrategyHealth()" style="background:#27ae60;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Strategy Health</button>
                    <button onclick="loadPerformanceSplits()" style="background:#1abc9c;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Per-Bot Splits</button>
                    <button onclick="loadOpenRiskDashboard()" style="background:#e67e22;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Open Risk Dashboard</button>
                    <button onclick="loadTradeReplay()" style="background:#3498db;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Trade Replay</button>
                    <button onclick="window.location.href='/api/export_journal?format=csv'" style="background:#34495e;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Export Journal CSV</button>
                    <button onclick="window.location.href='/api/export_journal?format=html'" style="background:#2d3436;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Export Journal HTML</button>
                    <button onclick="loadConfigHistory()" style="background:#7f8c8d;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer">Config History</button>
                </div>
                <div id="strategyHealthView" style="font-size:0.82em;color:#bbb;line-height:1.5;background:#0f172a;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:10px;white-space:pre-wrap;margin-bottom:8px">Strategy health data will appear here.</div>
                <div id="openRiskView" style="font-size:0.82em;color:#bbb;line-height:1.5;background:#0f172a;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:10px;white-space:pre-wrap;margin-bottom:8px">Open-trade risk dashboard will appear here.</div>
                <div id="tradeReplayView" style="font-size:0.82em;color:#bbb;line-height:1.5;background:#0f172a;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:10px;white-space:pre-wrap">Trade replay will appear here.</div>
            </div>

            <div id="deleteHistoryPanel" style="display:none;padding:16px;background:rgba(231,76,60,0.1);border:1px solid rgba(231,76,60,0.3);border-radius:8px;margin-bottom:14px">
                <div style="color:#e74c3c;font-weight:700;margin-bottom:12px">Delete Trade History</div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
                    <button onclick="clearHistory('',function(){loadReports()})" style="background:#e74c3c;color:#fff;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:0.9em;font-weight:600">Clear 5-Min</button>
                    <button onclick="clearHistory('Gold1M',function(){loadReports()})" style="background:#e74c3c;color:#fff;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:0.9em;font-weight:600">Clear Gold 1m</button>
                    <button onclick="clearHistory('GoldDay',function(){loadReports()})" style="background:#e74c3c;color:#fff;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:0.9em;font-weight:600">Clear Day Trade</button>
                    <button onclick="clearHistory('BTC',function(){loadReports()})" style="background:#e74c3c;color:#fff;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:0.9em;font-weight:600">Clear BTC</button>
                    <button onclick="clearHistory('ALL',function(){loadReports()})" style="background:#8b0000;color:#fff;border:none;border-radius:5px;padding:6px 12px;cursor:pointer;font-size:0.9em;font-weight:600;margin-left:auto">Clear ALL</button>
                </div>
                <button onclick="hideDeleteHistoryMode()" style="background:#555;color:#fff;border:none;border-radius:5px;padding:5px 12px;cursor:pointer;font-size:0.85em">Cancel</button>
            </div>

            <!-- Totals summary cards -->
            <div id="reportTotalsRow" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px"></div>
            <!-- Period breakdown table -->
            <div class="card">
                <div style="max-height:520px;overflow-y:auto">
                    <table class="pos-table" id="reportTable">
                        <thead id="reportThead"></thead>
                        <tbody id="reportBody"><tr><td colspan="9" style="text-align:center;color:#888;padding:24px">Click Refresh to load reports</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div><!-- /tab-reports -->

        <!-- TAB 7: Daily Loss Reach -->
        <div id="tab-lossreach" class="tab-pane" style="display:none">
            <div class="card" style="margin-bottom:14px">
                <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
                    <span style="font-size:1.1em;font-weight:800;color:#ff8a80">&#128200; DAILY LOSS REACH TRACKER</span>
                    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                        <select id="lossReachPeriod" onchange="loadLossReach()" style="background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:6px;padding:5px 10px;cursor:pointer">
                            <option value="day">Day</option>
                            <option value="week">Week</option>
                            <option value="month">Month</option>
                        </select>
                        <button onclick="loadLossReach()" style="background:#c0392b;color:#fff;border:none;border-radius:6px;padding:6px 14px;cursor:pointer">&#8635; Refresh</button>
                    </div>
                </div>
                <div id="lossReachSummary" style="margin-top:10px;font-size:0.88em;color:#bbb">Loading...</div>
            </div>

            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:14px">
                <div class="card" style="border:1px solid rgba(231,76,60,0.35)">
                    <div style="font-size:0.84em;color:#999">Ever High Loss Reach Recorded</div>
                    <div id="lossReachEverHigh" style="font-size:1.4em;font-weight:800;color:#e74c3c;margin-top:6px">$0.00</div>
                    <div style="font-size:0.78em;color:#888;margin-top:4px">Worst adverse move seen on any single trade (MAE)</div>
                </div>
                <div class="card" style="border:1px solid rgba(241,196,15,0.35)">
                    <div style="font-size:0.84em;color:#999">Recommended Starting Capital</div>
                    <div id="lossReachCapital" style="font-size:1.4em;font-weight:800;color:#f1c40f;margin-top:6px">$0.00</div>
                    <div id="lossReachCapitalSafe" style="font-size:0.82em;color:#bbb;margin-top:4px">Safe mode: $0.00</div>
                    <div style="font-size:0.78em;color:#888;margin-top:4px">Based on worst observed loss reach to reduce blow-up risk</div>
                </div>
            </div>

            <div class="card">
                <div style="max-height:520px;overflow-y:auto">
                    <table class="pos-table" id="lossReachTable">
                        <thead>
                            <tr>
                                <th id="lossReachPeriodHdr">Date</th><th>Trades</th><th>W/L</th><th>Win%</th><th>Total P&amp;L</th>
                                <th>Loss Total</th><th>Worst Loss</th><th>Peak Trade LR</th><th>Loss Reach (DD)</th><th>Max L Streak</th><th>Recovery%</th>
                            </tr>
                        </thead>
                        <tbody id="lossReachBody"><tr><td colspan="11" style="text-align:center;color:#888;padding:24px">Loading...</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div><!-- /tab-lossreach -->
    </div>
    
    <script>
        const SYMBOLS = ['GOLD', 'EURUSD', 'GBPUSD'];
        const COLORS = {'GOLD': 'gold', 'EURUSD': 'eur', 'GBPUSD': 'gbp'};

        function _setThemeButton(theme) {
            const btn = document.getElementById('themeToggleBtn');
            if (!btn) return;
            if (theme === 'light') {
                btn.textContent = '☀ Light';
                btn.title = 'Switch to night mode';
            } else {
                btn.textContent = '☾ Night';
                btn.title = 'Switch to light mode';
            }
        }

        function applyTheme(theme) {
            const t = (theme === 'light') ? 'light' : 'night';
            const body = document.body;
            if (!body) return;
            body.classList.toggle('light-theme', t === 'light');
            _setThemeButton(t);
            try { localStorage.setItem('dashboardTheme', t); } catch(e) {}
        }

        function toggleTheme() {
            const isLight = document.body && document.body.classList.contains('light-theme');
            applyTheme(isLight ? 'night' : 'light');
        }

        function sanitizeNonBtcSymbolKey(symbolKey) {
            return (symbolKey && symbolKey.toUpperCase() === 'BTCUSD') ? 'GOLD' : (symbolKey || 'GOLD');
        }
        
        function init() {
            document.getElementById('symbolsGrid').innerHTML = SYMBOLS.map(s => `
                <div class="symbol-card" id="card-${s}">
                    <div class="symbol-header">
                        <span class="symbol-name ${COLORS[s]}">${s}</span>
                        <span class="signal-badge signal-none" id="signal-${s}">NONE</span>
                    </div>
                    <div class="price-row">
                        <span class="price-big" id="price-${s}">--.-----</span>
                        <span class="spread-info">Spread: <span id="spread-${s}">--</span> pts</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin:8px 0">
                        <span>BUY Score: <b id="buy-${s}" style="color:#27ae60">0</b></span>
                        <span>SELL Score: <b id="sell-${s}" style="color:#e74c3c">0</b></span>
                    </div>
                    <div>Strength: <span id="strength-${s}">0</span>%</div>
                    <div class="strength-bar"><div class="strength-fill" id="bar-${s}" style="width:0%"></div></div>
                    <div id="analysis-${s}" style="font-size:0.75em;color:#888;margin-top:8px"></div>
                    <div class="positions-mini" id="pos-${s}"></div>
                    <div style="display:flex;align-items:center;gap:6px;margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.08)">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">Lot</label>
                        <input id="lot-${s}" type="number" min="0" max="100" step="0.01" value="0"
                            style="width:62px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em"
                            title="0 = auto (risk-based)"
                            onkeydown="if(event.key==='Enter')saveSymbolConfig('${s}')">
                        <label style="color:#888;font-size:0.78em;white-space:nowrap">MaxPos</label>
                        <input id="mp-${s}" type="number" min="1" max="10" step="1" value="1"
                            style="width:46px;background:#1a1a2e;color:#fff;border:1px solid #444;border-radius:4px;padding:3px 5px;font-size:0.82em"
                            onkeydown="if(event.key==='Enter')saveSymbolConfig('${s}')">
                        <button onclick="saveSymbolConfig('${s}')"
                            style="background:#2980b9;color:#fff;border:none;border-radius:4px;padding:3px 9px;font-size:0.78em;cursor:pointer;white-space:nowrap">Save</button>
                        <span id="cfg-saved-${s}" style="color:#27ae60;font-size:0.75em;display:none">&#10003;</span>
                        <button id="btn-en-${s}" onclick="toggleSymbolEnabled('${s}')" title="Pause / resume trading this symbol"
                            style="margin-left:auto;background:#27ae60;color:#fff;border:none;border-radius:4px;padding:3px 10px;font-size:0.78em;cursor:pointer;white-space:nowrap;min-width:68px">
                            &#10003; Active
                        </button>
                    </div>
                </div>
            `).join('');
        }
        
        var _reconnectTimer = null;   // prevent duplicate reconnect loops
        var _lastConnectError = '';
        function update(d) {
            if (!d) return;
            const dot = document.getElementById('statusDot');
            const txt = document.getElementById('botStatus');
            const btnConnect = document.getElementById('btnConnect');
            const connHint = document.getElementById('connHint');
            if (!dot || !txt) return;
            
            if (d.running) {
                dot.className = 'status-dot status-running';
                txt.textContent = 'Running';
                document.getElementById('btnStart').disabled = true;
                document.getElementById('btnStop').disabled = false;
                if (btnConnect) { btnConnect.disabled = false; btnConnect.textContent = 'Reconnect MT5'; }
                if (connHint) connHint.textContent = '';
                if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
            } else if (d.connected) {
                dot.className = 'status-dot status-stopped';
                txt.textContent = 'Ready';
                document.getElementById('btnStart').disabled = false;
                document.getElementById('btnStop').disabled = true;
                if (btnConnect) { btnConnect.disabled = false; btnConnect.textContent = 'Reconnect MT5'; }
                if (connHint) connHint.textContent = '';
                if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
            } else {
                dot.className = 'status-dot status-stopped';
                txt.textContent = 'Connecting...';
                document.getElementById('btnStart').disabled = true;
                document.getElementById('btnStop').disabled = true;
                if (btnConnect) { btnConnect.disabled = false; btnConnect.textContent = 'Connect MT5'; }
                if (connHint) connHint.textContent = _lastConnectError ? ('MT5: ' + _lastConnectError) : 'MT5 not connected. Open MetaTrader and log in, then press Connect MT5.';
                // Auto-reconnect whenever status shows disconnected
                if (!_reconnectTimer) {
                    _reconnectTimer = setTimeout(function() {
                        _reconnectTimer = null;
                        autoConnect();
                    }, 3000);
                }
            }
            
            if (d.account) {
                document.getElementById('accountId').textContent = d.account.login;
                document.getElementById('balance').textContent = '$' + parseFloat(d.account.balance || 0).toFixed(2);
                document.getElementById('equity').textContent = '$' + parseFloat(d.account.equity || 0).toFixed(2);
                const p = parseFloat(d.account.profit || 0);
                const pe = document.getElementById('profit');
                pe.textContent = '$' + p.toFixed(2);
                pe.style.color = p >= 0 ? '#27ae60' : '#e74c3c';
            } else {
                document.getElementById('accountId').textContent = 'Not Connected';
            }
            
            document.getElementById('totalPos').textContent = d.total_positions || 0;
            document.getElementById('trades').textContent = (d.stats && d.stats.trades_opened) || 0;
            const olm = d.open_loss_metrics || {};
            const openLossReachVal = parseFloat(olm.open_loss_reach || 0);
            const olrEl = document.getElementById('openLossReach');
            if (olrEl) {
                const absVal = Math.abs(openLossReachVal).toFixed(2);
                olrEl.textContent = '$' + absVal;
                olrEl.style.color = openLossReachVal < 0 ? '#e74c3c' : '#27ae60';
            }
            // 5-min header bar stat chips
            const smcO = document.getElementById('smcOpened');
            const smcP = document.getElementById('smcPnl');
            if (smcO) smcO.textContent = (d.stats && d.stats.trades_opened) || 0;
            if (smcP) {
                // Use live_pnl (closed + floating) so it moves in real-time
                const spnl = parseFloat((d.smc_live_pnl != null) ? d.smc_live_pnl
                           : ((d.stats && d.stats.session_pnl) || 0));
                smcP.textContent = '$' + spnl.toFixed(2);
                smcP.style.color = spnl >= 0 ? '#27ae60' : '#e74c3c';
            }
            
            // Daily goal display
            if (d.daily_goal) {
                const dg = d.daily_goal;
                const pnl = parseFloat(dg.current_pnl || 0);
                const target = parseFloat(dg.target || 20);
                const progress = Math.min(100, Math.max(0, parseFloat(dg.progress_pct || 0)));
                
                const dgEl = document.getElementById('dailyGoal');
                if (dgEl) {
                    dgEl.textContent = '$' + pnl.toFixed(2) + '/$' + target.toFixed(0);
                    dgEl.style.color = pnl >= 0 ? (dg.goal_reached ? '#27ae60' : '#ffd700') : '#e74c3c';
                }
                const gbEl = document.getElementById('goalBar');
                if (gbEl) {
                    gbEl.style.width = progress + '%';
                    if (dg.goal_reached) gbEl.style.background = '#27ae60';
                }
            }
            
            let allPos = [];
            for (const s of SYMBOLS) {
                const sd = (d.symbols || {})[s];
                if (!sd) continue;
                
                if (sd.price) {
                    const dec = s === 'GOLD' ? 2 : 5;
                    document.getElementById(`price-${s}`).textContent = sd.price.bid.toFixed(dec);
                    document.getElementById(`spread-${s}`).textContent = sd.spread || '--';
                }
                
                const sig = document.getElementById(`signal-${s}`);
                sig.textContent = sd.signal || 'NONE';
                sig.className = 'signal-badge signal-' + (sd.signal || 'none').toLowerCase();
                
                document.getElementById(`buy-${s}`).textContent = sd.buy_score || 0;
                document.getElementById(`sell-${s}`).textContent = sd.sell_score || 0;
                document.getElementById(`strength-${s}`).textContent = sd.signal_strength || 0;
                document.getElementById(`bar-${s}`).style.width = (sd.signal_strength || 0) + '%';
                
                // Show analysis breakdown
                const an = sd.analysis || {};
                let analysisHtml = '';
                // ── SMC / ICT ──
                if (an.in_order_block)       analysisHtml += '<span style="color:#f39c12">[OB]</span> ';
                if (an.has_fvg)             analysisHtml += '<span style="color:#9b59b6">[FVG]</span> ';
                if (an.in_ote_zone)         analysisHtml += '<span style="color:#3498db">[OTE]</span> ';
                if (an.in_kill_zone)        analysisHtml += '<span style="color:#e74c3c">[KILL]</span> ';
                if (an.high_volume)         analysisHtml += '<span style="color:#27ae60">[VOL]</span> ';
                if (an.fib_confluence > 0)  analysisHtml += '<span style="color:#ffd700">[FIB:' + an.fib_confluence + ']</span> ';
                // ── LIQUIDITY ──
                if (an.liquidity_sweep) {
                    const sw = an.sweep_strength > 0 ? '(' + an.sweep_strength.toFixed(2) + ')' : '';
                    analysisHtml += '<span style="color:#ff6600">[SWEEP' + sw + ']</span> ';
                }
                if (an.buyside_liquidity)    analysisHtml += '<span style="color:#ff4488">[BSL]</span> ';
                if (an.sellside_liquidity)   analysisHtml += '<span style="color:#44aaff">[SSL]</span> ';
                if (an.near_bsl)             analysisHtml += '<span style="color:#ff88bb">[@BSL]</span> ';
                if (an.near_ssl)             analysisHtml += '<span style="color:#88ccff">[@SSL]</span> ';
                if (an.price_at_pdh)         analysisHtml += '<span style="color:#ffcc00">[PDH]</span> ';
                if (an.price_at_pdl)         analysisHtml += '<span style="color:#aaffaa">[PDL]</span> ';
                if (an.liq_run_bull)         analysisHtml += '<span style="color:#00ff88">[LIQ↑]</span> ';
                if (an.liq_run_bear)         analysisHtml += '<span style="color:#ff4444">[LIQ↓]</span> ';
                if (an.inducement_bull)      analysisHtml += '<span style="color:#ccffcc">[IND↑]</span> ';
                if (an.inducement_bear)      analysisHtml += '<span style="color:#ffcccc">[IND↓]</span> ';
                // ── Traditional indicators ──
                if (an.rsi_oversold)  analysisHtml += '<span style="color:#00ff00">[RSI↓]</span> ';
                else if (an.rsi_overbought) analysisHtml += '<span style="color:#ff4444">[RSI↑]</span> ';
                if (an.macd_crossover)      analysisHtml += '<span style="color:#00ffff">[MACD✕]</span> ';
                else if (an.macd_bullish)   analysisHtml += '<span style="color:#44ff44">[MACD+]</span> ';
                else                       analysisHtml += '<span style="color:#ff6666">[MACD-]</span> ';
                if (an.ema_crossover)       analysisHtml += '<span style="color:#ff00ff">[EMA✕]</span> ';
                else if (an.ema_bullish)    analysisHtml += '<span style="color:#66ff66">[EMA↑]</span> ';
                else                       analysisHtml += '<span style="color:#ff8888">[EMA↓]</span> ';
                if (an.bb_lower_touch)      analysisHtml += '<span style="color:#00ff88">[BB↓]</span> ';
                else if (an.bb_upper_touch) analysisHtml += '<span style="color:#ff8800">[BB↑]</span> ';
                document.getElementById(`analysis-${s}`).innerHTML = analysisHtml || 'No signals detected';

                // Populate lot/maxpos inputs only when they're not focused (avoid overwriting mid-edit)
                const lotEl = document.getElementById(`lot-${s}`);
                const mpEl  = document.getElementById(`mp-${s}`);
                if (lotEl && document.activeElement !== lotEl)
                    lotEl.value = (sd.lot_size != null ? sd.lot_size : 0);
                if (mpEl  && document.activeElement !== mpEl)
                    mpEl.value  = (sd.max_positions != null ? sd.max_positions : 1);

                // Enabled / paused state
                const enBtn = document.getElementById(`btn-en-${s}`);
                const card  = document.getElementById(`card-${s}`);
                const isEnabled = sd.enabled !== false;
                if (enBtn) {
                    enBtn.textContent  = isEnabled ? '\u2713 Active' : '\u23F8 Paused';
                    enBtn.style.background = isEnabled ? '#27ae60' : '#c0392b';
                }
                if (card) card.style.opacity = isEnabled ? '1' : '0.45';

                const pd = document.getElementById(`pos-${s}`);
                if (sd.positions && sd.positions.length > 0) {
                    pd.innerHTML = sd.positions.map(p => `
                        <div class="position-row">
                            <span>${p.type.toUpperCase()} ${p.volume}</span>
                            <span class="${p.profit >= 0 ? 'profit-pos' : 'profit-neg'}">$${p.profit.toFixed(2)}</span>
                        </div>
                    `).join('');
                    allPos = allPos.concat(sd.positions);
                } else {
                    pd.innerHTML = '<div style="color:#666;font-size:0.9em;margin-top:5px;">No positions</div>';
                }
            }
            
            const pb = document.getElementById('posBody');
            if (allPos.length > 0) {
                pb.innerHTML = allPos.map(p => `
                    <tr>
                        <td>${p.symbol}</td>
                        <td>${p.type.toUpperCase()}</td>
                        <td>${p.volume}</td>
                        <td>${p.open_price.toFixed(p.symbol.includes('USD') && !p.symbol.includes('XAU') && !p.symbol.includes('GOLD') ? 5 : 2)}</td>
                        <td class="${p.profit >= 0 ? 'profit-pos' : 'profit-neg'}">$${p.profit.toFixed(2)}</td>
                    </tr>
                `).join('');
            } else {
                pb.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#888">No positions</td></tr>';
            }
            
            const logs = document.getElementById('logs');
            if (d.logs && d.logs.length > 0) {
                logs.innerHTML = d.logs.map(l => `
                    <div class="log-entry">
                        <span class="log-time">${l.time}</span>
                        <span class="log-${l.level}">[${l.level}]</span> ${l.message}
                    </div>
                `).join('');
            }
            // keep trading toggle in sync with every status poll
            if (d.trading_enabled != null) _applyTradingEnabled(d.trading_enabled);
        }
        
        // ── History tab functions ──
        var _histFilter = 'all';
        var _histData   = { '5min': [], 'gold': [], 'daytrade': [], 'btc': [] };

        function setHistFilter(f) {
            _histFilter = f;
            var keys = ['all','5min','gold','daytrade','btc'];
            keys.forEach(function(k) {
                var el = document.getElementById('histFilter_' + k);
                if (!el) return;
                if (k === f) {
                    el.style.background = '#2980b9'; el.style.color = '#fff'; el.style.border = 'none';
                } else if (k === '5min') {
                    el.style.background = '#1a1a2e'; el.style.color = '#ffd700'; el.style.border = '1px solid rgba(255,215,0,0.3)';
                } else if (k === 'gold') {
                    el.style.background = '#1a1a2e'; el.style.color = '#ffd700'; el.style.border = '1px solid rgba(184,134,11,0.3)';
                } else if (k === 'daytrade') {
                    el.style.background = '#1a1a2e'; el.style.color = '#f39c12'; el.style.border = '1px solid rgba(243,156,18,0.3)';
                } else if (k === 'btc') {
                    el.style.background = '#1a1a2e'; el.style.color = '#f39c12'; el.style.border = '1px solid rgba(243,156,18,0.45)';
                } else {
                    el.style.background = '#1a1a2e'; el.style.color = '#aaa'; el.style.border = '1px solid #333';
                }
            });
            _renderHistTable();
        }

        function refreshAllHistory() {
            Promise.all([
                fetch('/api/trade_history?bot=5min').then(function(r){return r.json();}),
                fetch('/api/trade_history?bot=' + encodeURIComponent('Gold1M')).then(function(r){return r.json();}),
                fetch('/api/trade_history?bot=' + encodeURIComponent('GoldDay')).then(function(r){return r.json();}),
                fetch('/api/trade_history?bot=BTC').then(function(r){return r.json();}),
            ]).then(function(results) {
                var d5 = results[0], dG = results[1], dD = results[2], dB = results[3];
                _histData['5min']     = d5.trades || [];
                _histData['gold']     = dG.trades || [];
                _histData['daytrade'] = dD.trades || [];
                _histData['btc']      = dB.trades || [];
                _renderHistStats('hist5', d5.summary || {});
                _renderHistStats('histG', dG.summary || {});
                _renderHistStats('histD', dD.summary || {});
                _renderHistStats('histB', dB.summary || {});
                _renderWinBars();
                _drawEquityChart();
                _renderHistTable();
            }).catch(console.error);
        }

        function _renderHistStats(prefix, s) {
            var set = function(id, v) { var e = document.getElementById(prefix + '_' + id); if (e) e.textContent = v; };
            var setColor = function(id, c) { var e = document.getElementById(prefix + '_' + id); if (e) e.style.color = c; };
            var pnl = s.total_pnl || 0;
            set('total',  s.total    || 0);
            set('wins',   s.wins     || 0);
            set('losses', s.losses   || 0);
            set('wr',     (s.win_rate || 0) + '%');
            set('pnl',    (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2));
            set('avg',    (s.total ? ((pnl >= 0 ? '+$' : '-$') + Math.abs(pnl / s.total).toFixed(2)) : '$0.00'));
            set('best',   '+$' + (s.best  || 0).toFixed(2));
            set('worst',  '-$' + Math.abs(s.worst || 0).toFixed(2));
            setColor('pnl', pnl >= 0 ? '#27ae60' : '#e74c3c');
            var wr = s.win_rate || 0;
            setColor('wr', wr >= 50 ? '#27ae60' : wr >= 35 ? '#ffd700' : '#e74c3c');
        }

        function _renderWinBars() {
            var defs = [
                {key: '5min',     id: 'wb5'},
                {key: 'gold',     id: 'wbG'},
                {key: 'daytrade', id: 'wbD'},
                {key: 'btc',      id: 'wbB'},
            ];
            defs.forEach(function(d) {
                var trades = _histData[d.key] || [];
                var wins   = trades.filter(function(t){return t.result==='WIN';}).length;
                var losses = trades.filter(function(t){return t.result==='LOSS';}).length;
                var total  = wins + losses;
                var wr     = total > 0 ? Math.round(wins / total * 100) : 0;
                var wPct   = total > 0 ? Math.round(wins   / total * 100) : 0;
                var lPct   = total > 0 ? Math.round(losses / total * 100) : 0;
                var el = document.getElementById(d.id);
                if (!el) return;
                el.innerHTML = '<div style="font-size:0.75em;color:#888;margin-bottom:4px">W: ' + wins + ' &nbsp; L: ' + losses
                    + ' &nbsp; WR: <b style="color:' + (wr>=50?'#27ae60':wr>=35?'#ffd700':'#e74c3c') + '">' + wr + '%</b></div>'
                    + '<div style="border-radius:4px;overflow:hidden;height:12px;display:flex;background:#111">'
                    + (wPct > 0 ? '<div style="width:' + wPct + '%;background:#27ae60"></div>' : '')
                    + (lPct > 0 ? '<div style="width:' + lPct + '%;background:#e74c3c"></div>' : '')
                    + '</div>';
            });
        }

        function _drawEquityChart() {
            var canvas = document.getElementById('equityChart');
            if (!canvas) return;
            var ctx = canvas.getContext('2d');
            var W = canvas.clientWidth || 900;
            canvas.width  = W;
            canvas.height = 200;
            ctx.clearRect(0, 0, W, 200);
            var PAD = {t:16, r:20, b:32, l:56};
            var cW = W - PAD.l - PAD.r, cH = 200 - PAD.t - PAD.b;
            var defs = [
                {key:'5min',     label:'5-Min SMC',  color:'#ffd700'},
                {key:'gold',     label:'Gold 1M',    color:'#40c0f0'},
                {key:'daytrade', label:'Gold Day',   color:'#f39c12'},
                {key:'btc',      label:'BTC',        color:'#ff9f43'},
            ];
            var series = defs.map(function(def) {
                var trades = (_histData[def.key]||[]).slice().sort(function(a,b){
                    return (a.close_time||'').localeCompare(b.close_time||'');
                });
                var cum = 0, pts = [0];
                trades.forEach(function(t){cum += t.profit||0; pts.push(cum);});
                return {label:def.label, color:def.color, pts:pts, key:def.key};
            });
            var allVals = series.reduce(function(a,s){return a.concat(s.pts);}, []);
            var yMin = Math.min.apply(null, [0].concat(allVals));
            var yMax = Math.max.apply(null, [0].concat(allVals));
            if (yMax === yMin) { yMin -= 1; yMax += 1; }
            var yRange = yMax - yMin;
            // Grid lines
            ctx.strokeStyle = 'rgba(255,255,255,0.05)'; ctx.lineWidth = 1;
            for (var i = 0; i <= 4; i++) {
                var gy = PAD.t + cH * (1 - i/4);
                ctx.beginPath(); ctx.moveTo(PAD.l, gy); ctx.lineTo(PAD.l+cW, gy); ctx.stroke();
                var gv = yMin + yRange*(i/4);
                ctx.fillStyle='#555'; ctx.font='10px monospace'; ctx.textAlign='right';
                ctx.fillText((gv>=0?'+':'')+'$'+gv.toFixed(1), PAD.l-4, gy+4);
            }
            // Zero line
            var zy = PAD.t + cH * (1 - (0-yMin)/yRange);
            ctx.strokeStyle = 'rgba(255,255,255,0.18)'; ctx.lineWidth = 1;
            ctx.setLineDash([4,4]);
            ctx.beginPath(); ctx.moveTo(PAD.l, zy); ctx.lineTo(PAD.l+cW, zy); ctx.stroke();
            ctx.setLineDash([]);
            // Series lines
            series.forEach(function(s) {
                if (s.pts.length < 2) return;
                var n = s.pts.length;
                ctx.strokeStyle = s.color; ctx.lineWidth = 2;
                ctx.beginPath();
                s.pts.forEach(function(v,i){
                    var x = PAD.l + (i/(n-1))*cW;
                    var y = PAD.t + cH*(1-(v-yMin)/yRange);
                    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
                });
                ctx.stroke();
                // Fill area
                ctx.globalAlpha = 0.08;
                ctx.fillStyle = s.color;
                ctx.beginPath();
                s.pts.forEach(function(v,i){
                    var x = PAD.l + (i/(n-1))*cW;
                    var y = PAD.t + cH*(1-(v-yMin)/yRange);
                    if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
                });
                ctx.lineTo(PAD.l+cW, zy); ctx.lineTo(PAD.l, zy); ctx.closePath(); ctx.fill();
                ctx.globalAlpha = 1;
                // Last point dot
                var lx = PAD.l+cW, lv = s.pts[n-1];
                var ly = PAD.t + cH*(1-(lv-yMin)/yRange);
                ctx.fillStyle = s.color;
                ctx.beginPath(); ctx.arc(lx,ly,4,0,Math.PI*2); ctx.fill();
            });
            // Axis border
            ctx.strokeStyle = 'rgba(255,255,255,0.1)'; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(PAD.l,PAD.t); ctx.lineTo(PAD.l,PAD.t+cH); ctx.lineTo(PAD.l+cW,PAD.t+cH); ctx.stroke();
            // Legend
            var legEl = document.getElementById('equityLegend');
            if (legEl) {
                legEl.innerHTML = defs.map(function(def) {
                    var pnl = (_histData[def.key]||[]).reduce(function(a,t){return a+(t.profit||0);}, 0);
                    return '<span style="display:flex;align-items:center;gap:5px">'
                        + '<span style="width:18px;height:3px;background:'+def.color+';display:inline-block;border-radius:2px"></span>'
                        + '<span style="color:'+def.color+'">'+def.label+'</span>'
                        + '<span style="color:'+(pnl>=0?'#27ae60':'#e74c3c')+'">'+(pnl>=0?'+':'')+'$'+pnl.toFixed(2)+'</span>'
                        + '</span>';
                }).join('');
            }
        }

        function _renderHistTable() {
            var trades = [];
            if (_histFilter === 'all') {
                trades = (_histData['5min']||[]).concat(_histData['gold']||[]).concat(_histData['daytrade']||[]);
            } else if (_histFilter === '5min')     { trades = _histData['5min']     || []; }
            else if  (_histFilter === 'gold')      { trades = _histData['gold']     || []; }
            else if  (_histFilter === 'daytrade')  { trades = _histData['daytrade'] || []; }
            else if  (_histFilter === 'btc')       { trades = _histData['btc']      || []; }
            trades = trades.slice().sort(function(a,b){
                return (b.close_time||'').localeCompare(a.close_time||'');
            });
            var totEl = document.getElementById('allHistTotal');
            if (totEl) totEl.textContent = '(' + trades.length + ' trades)';
            var tbody = document.getElementById('allHistBody');
            if (!tbody) return;
            if (trades.length === 0) {
                tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#888;padding:24px">No closed trades in this view</td></tr>';
                return;
            }
            var isG = function(sym) { return sym && (sym.includes('XAU')||sym.includes('GOLD')); };
            var isBtc = function(sym) { return sym && (sym.includes('BTC') || sym.includes('XBT')); };
            var botBadge = function(t) {
                if (isBtc((t.symbol || '').toUpperCase())) return '<span style="background:#f39c12;color:#111;padding:1px 5px;border-radius:3px;font-size:0.73em;font-weight:700;white-space:nowrap">BTC</span>';
                if (t.bot === 'Gold1M')   return '<span style="background:#b8860b;color:#fff;padding:1px 5px;border-radius:3px;font-size:0.73em;white-space:nowrap">GOLD</span>';
                if (t.bot === 'GoldDay')  return '<span style="background:#f39c12;color:#fff;padding:1px 5px;border-radius:3px;font-size:0.73em;white-space:nowrap">DAY</span>';
                return '<span style="background:#1f618d;color:#fff;padding:1px 5px;border-radius:3px;font-size:0.73em;white-space:nowrap">5MIN</span>';
            };
            tbody.innerHTML = trades.map(function(t) {
                var dec  = isG(t.symbol) ? 2 : 5;
                var clr  = t.result==='WIN' ? '#27ae60' : t.result==='LOSS' ? '#e74c3c' : '#888';
                var badge = t.result==='WIN'
                    ? '<span style="background:#27ae60;color:#fff;padding:2px 7px;border-radius:4px;font-size:0.78em;font-weight:bold">WIN</span>'
                    : t.result==='LOSS'
                    ? '<span style="background:#e74c3c;color:#fff;padding:2px 7px;border-radius:4px;font-size:0.78em;font-weight:bold">LOSS</span>'
                    : '<span style="background:#555;color:#fff;padding:2px 7px;border-radius:4px;font-size:0.78em">BE</span>';
                var pnl = t.profit || 0;
                var mae = Number(t.max_loss_reach || 0);
                return '<tr>'
                    + '<td style="font-size:0.8em;color:#aaa">'+(t.close_time||'').slice(5,16)+'</td>'
                    + '<td>'+botBadge(t)+'</td>'
                    + '<td style="font-weight:bold">'+t.symbol+'</td>'
                    + '<td style="color:'+(t.type==='buy'?'#27ae60':'#e74c3c')+';font-weight:bold">'+(t.type||'').toUpperCase()+'</td>'
                    + '<td>'+t.volume+'</td>'
                    + '<td>'+(t.open_price||0).toFixed(dec)+'</td>'
                    + '<td>'+(t.close_price||0).toFixed(dec)+'</td>'
                    + '<td style="color:'+clr+';font-weight:bold">'+(pnl>=0?'+':'')+' $'+pnl.toFixed(2)+'</td>'
                    + '<td style="color:#e74c3c;font-weight:bold">-$'+mae.toFixed(2)+'</td>'
                    + '<td>'+badge+'</td>'
                    + '</tr>';
            }).join('');
        }

        // Legacy history refresh aliases
        function refreshHistory()       { refreshAllHistory(); }
        function refreshGoldHistory()   { refreshAllHistory(); }

        function manualConnect() {
            const btn = document.getElementById('btnConnect');
            if (btn) { btn.disabled = true; btn.textContent = 'Connecting...'; }
            fetch('/api/connect', {method:'POST'})
                .then(r=>r.json())
                .then(cd=>{
                    if (cd.success) {
                        _lastConnectError = '';
                        refresh();
                        refreshGold();
                        refreshDayTrade();
                    } else {
                        _lastConnectError = (cd.error || 'Connection failed');
                        const hint = document.getElementById('connHint');
                        if (hint) hint.textContent = 'MT5: ' + _lastConnectError;
                        refresh();
                    }
                })
                .catch(()=>{
                    _lastConnectError = 'Could not reach dashboard backend';
                    const hint = document.getElementById('connHint');
                    if (hint) hint.textContent = 'MT5: ' + _lastConnectError;
                })
                .finally(()=>{
                    if (btn) btn.disabled = false;
                });
        }

        function autoConnect() {
            // First check current status — if already connected, no need to POST /connect
            fetch('/api/status')
                .then(r=>r.json())
                .then(d=>{
                    if (d.connected) {
                        // Already connected — just update UI immediately
                        _lastConnectError = '';
                        update(d);
                    } else {
                        // Not connected — do explicit connect POST
                        fetch('/api/connect', {method:'POST'})
                            .then(r=>r.json())
                            .then(cd=>{
                                if (cd.success) {
                                    _lastConnectError = '';
                                    refresh();
                                    refreshGold();
                                    refreshDayTrade();
                                } else {
                                    _lastConnectError = (cd.error || 'MT5 offline');
                                    document.getElementById('botStatus').textContent = 'MT5 Offline - retrying...';
                                    setTimeout(autoConnect, 5000);
                                }
                            })
                            .catch(()=>{
                                _lastConnectError = 'Could not reach /api/connect';
                                setTimeout(autoConnect, 5000);
                            });
                    }
                })
                .catch(()=>{
                    document.getElementById('botStatus').textContent = 'Connecting...';
                    setTimeout(autoConnect, 5000);
                });
        }
        function _coreStatusFallback(d) {
            if (!d) return;
            const st = document.getElementById('botStatus');
            const bStart = document.getElementById('btnStart');
            const bStop = document.getElementById('btnStop');
            const dot = document.getElementById('statusDot');
            if (st) st.textContent = d.running ? 'Running' : (d.connected ? 'Ready' : 'Connecting...');
            if (bStart) bStart.disabled = !!d.running || !d.connected;
            if (bStop) bStop.disabled = !d.running;
            if (dot) dot.className = d.running ? 'status-dot status-running' : 'status-dot status-stopped';
            if (d.account) {
                const id = document.getElementById('accountId');
                const bal = document.getElementById('balance');
                const eq = document.getElementById('equity');
                const p = document.getElementById('profit');
                if (id) id.textContent = d.account.login;
                if (bal) bal.textContent = '$' + Number(d.account.balance || 0).toFixed(2);
                if (eq) eq.textContent = '$' + Number(d.account.equity || 0).toFixed(2);
                if (p) {
                    const pv = Number(d.account.profit || 0);
                    p.textContent = '$' + pv.toFixed(2);
                    p.style.color = pv >= 0 ? '#27ae60' : '#e74c3c';
                }
            }
        }

        function startBot() {
            fetch('/api/start', {method:'POST'})
                .then(r=>r.json())
                .then(d=>{
                    if (!d.success) alert(d.error || 'Start failed');
                    refresh();
                })
                .catch(console.error);
        }
        function stopBot() {
            fetch('/api/stop', {method:'POST'})
                .then(r=>r.json())
                .then(d=>{
                    if (!d.success) alert(d.error || 'Stop failed');
                    refresh();
                })
                .catch(console.error);
        }
        function refresh() {
            fetch('/api/status')
                .then(r=>r.json())
                .then(d=>{
                    try {
                        update(d);
                    } catch (e) {
                        console.error('update() failed; applying core fallback status', e);
                        _coreStatusFallback(d);
                    }
                })
                .catch(console.error);
        }
        function restartApp() {
            if (!confirm('RESTART APP?\\n\\nThis will:\\n  \u2022 Stop ALL bots immediately\\n  \u2022 Close all connections\\n  \u2022 Restart the dashboard process\\n\\nMT5 positions remain open.\\n\\nConfirm?')) return;
            const btn = document.querySelector('button[onclick="restartApp()"]');
            if (btn) { btn.textContent = '\u23F3 Restarting...'; btn.disabled = true; }
            fetch('/api/restart_app', {method:'POST'})
                .then(r => r.json())
                .then(d => {
                    if (d.success) {
                        document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0a0a1a;color:#fff;font-family:monospace;flex-direction:column;gap:16px"><div style="font-size:2em">&#9851;</div><div style="font-size:1.2em;color:#ffd700">Restarting dashboard...</div><div style="color:#888">Page will reload automatically</div></div>';
                        setTimeout(()=>location.reload(), 4000);
                    } else {
                        alert('Restart failed: ' + (d.error || 'unknown'));
                        if (btn) { btn.textContent = '\u9851 RESTART APP'; btn.disabled = false; }
                    }
                })
                .catch(()=>{
                    // Network gone — process restarting; wait then reload
                    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0a0a1a;color:#fff;font-family:monospace;flex-direction:column;gap:16px"><div style="font-size:2em">&#9851;</div><div style="font-size:1.2em;color:#ffd700">Restarting dashboard...</div><div style="color:#888">Page will reload automatically</div></div>';
                    setTimeout(()=>location.reload(), 4500);
                });
        }

        // ── Tab switching ──
        function switchTab(tab) {
            ['5min','gold','daytrade','btc','history','reports','lossreach'].forEach(function(t) {
                const pane = document.getElementById('tab-' + t);
                if (pane) pane.style.display = tab === t ? '' : 'none';
            });
            const b5 = document.getElementById('tabBtn5m');
            const bg = document.getElementById('tabBtnGold');
            const bd = document.getElementById('tabBtnDay');
            const bb = document.getElementById('tabBtnBtc');
            const bh = document.getElementById('tabBtnHist');
            const br = document.getElementById('tabBtnReports');
            const bl = document.getElementById('tabBtnLoss');
            if (b5) b5.classList.toggle('active', tab === '5min');
            if (bg) bg.classList.toggle('active', tab === 'gold');
            if (bd) bd.classList.toggle('active', tab === 'daytrade');
            if (bb) bb.classList.toggle('active', tab === 'btc');
            if (bh) bh.classList.toggle('active', tab === 'history');
            if (br) br.classList.toggle('active', tab === 'reports');
            if (bl) bl.classList.toggle('active', tab === 'lossreach');
            if (tab === 'btc')       { refreshBtcStatus(); loadBtcConfig(); }
            if (tab === 'history')   refreshAllHistory();
            if (tab === 'reports')   {
                loadReports();
                loadEmailSettings();
                loadPushSettings();
                refreshWatchdogs();
            }
            if (tab === 'lossreach') loadLossReach();
            if (tab === 'daytrade')  refreshDayTrade();
            try { localStorage.setItem('activeTab', tab); } catch(e) {}
        }

        function loadLossReach() {
            const body = document.getElementById('lossReachBody');
            const summary = document.getElementById('lossReachSummary');
            const periodSel = document.getElementById('lossReachPeriod');
            const period = periodSel ? (periodSel.value || 'day') : 'day';
            const periodHdr = document.getElementById('lossReachPeriodHdr');
            const periodLabel = period === 'week' ? 'Week' : (period === 'month' ? 'Month' : 'Date');
            if (periodHdr) periodHdr.textContent = periodLabel;
            if (body) body.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#888;padding:24px">Loading...</td></tr>';
            fetch('/api/loss_report?period=' + encodeURIComponent(period))
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        if (summary) summary.textContent = 'Error: ' + data.error;
                        if (body) body.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#e74c3c;padding:24px">Failed to load loss reach report</td></tr>';
                        return;
                    }
                    const rows = data.days || [];
                    const s = data.summary || {};
                    const everHighEl = document.getElementById('lossReachEverHigh');
                    const capEl = document.getElementById('lossReachCapital');
                    const capSafeEl = document.getElementById('lossReachCapitalSafe');
                    if (everHighEl) everHighEl.textContent = '$' + Number(s.ever_high_loss_reach || 0).toFixed(2);
                    if (capEl) capEl.textContent = '$' + Number(s.recommended_start_capital || 0).toFixed(2);
                    if (capSafeEl) capSafeEl.textContent = 'Safe mode: $' + Number(s.recommended_start_capital_safe || 0).toFixed(2);
                    if (summary) {
                        summary.innerHTML =
                            'Periods: <b>' + (s.periods || 0) + '</b> &nbsp;|&nbsp; ' +
                            'Trades: <b>' + (s.total_trades || 0) + '</b> &nbsp;|&nbsp; ' +
                            'Total P&amp;L: <b style="color:' + ((s.total_pnl || 0) >= 0 ? '#27ae60' : '#e74c3c') + '">' + (((s.total_pnl || 0) >= 0 ? '+' : '') + '$' + Number(s.total_pnl || 0).toFixed(2)) + '</b> &nbsp;|&nbsp; ' +
                            'Total Loss Reach: <b style="color:#e74c3c">$' + Number(s.total_loss_reach || 0).toFixed(2) + '</b> &nbsp;|&nbsp; ' +
                            'Worst Period DD: <b style="color:#e74c3c">$' + Math.abs(Number(s.worst_period_loss_reach || 0)).toFixed(2) + '</b>';
                    }
                    if (!rows.length) {
                        if (body) body.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#888;padding:24px">No loss reach data yet</td></tr>';
                        return;
                    }
                    if (body) {
                        body.innerHTML = rows.map(r => {
                            const pnl = Number(r.total_pnl || 0);
                            const lossTotal = Math.abs(Number(r.loss_total || 0));
                            const worstLoss = Math.abs(Number(r.worst_loss || 0));
                            const dd = Math.abs(Number(r.max_drawdown || 0));
                            const peakTradeLR = Math.abs(Number(r.peak_trade_loss_reach || 0));
                            const rec = Number(r.recovery_efficiency || 0);
                            return '<tr>' +
                                '<td>' + (r.period || r.date || '') + '</td>' +
                                '<td>' + (r.trades || 0) + '</td>' +
                                '<td>' + (r.wins || 0) + ' / ' + (r.losses || 0) + '</td>' +
                                '<td style="color:' + ((r.win_rate || 0) >= 50 ? '#27ae60' : '#f39c12') + '">' + Number(r.win_rate || 0).toFixed(1) + '%</td>' +
                                '<td style="color:' + (pnl >= 0 ? '#27ae60' : '#e74c3c') + '">' + (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) + '</td>' +
                                '<td style="color:#e74c3c">-$' + lossTotal.toFixed(2) + '</td>' +
                                '<td style="color:#e74c3c">-$' + worstLoss.toFixed(2) + '</td>' +
                                '<td style="color:#ff7675">$' + peakTradeLR.toFixed(2) + '</td>' +
                                '<td style="color:#e74c3c"><b>$' + dd.toFixed(2) + '</b></td>' +
                                '<td>' + (r.max_loss_streak || 0) + '</td>' +
                                '<td style="color:' + (rec >= 100 ? '#27ae60' : '#f39c12') + '">' + rec.toFixed(1) + '%</td>' +
                            '</tr>';
                        }).join('');
                    }
                })
                .catch(() => {
                    if (summary) summary.textContent = 'Failed to load loss reach report';
                    if (body) body.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#e74c3c;padding:24px">Request failed</td></tr>';
                });
        }

        function _setBtnState(startId, stopId, running) {
            const s = document.getElementById(startId);
            const x = document.getElementById(stopId);
            if (s) s.disabled = !!running;
            if (x) x.disabled = !running;
        }

        function _setTxt(id, text, color) {
            const el = document.getElementById(id);
            if (!el) return;
            el.textContent = text;
            if (color) el.style.color = color;
        }

        function _btcAnalysisHtml(an) {
            an = an || {};
            let html = '';
            if (an.macd_crossover) html += '<span style="color:#00ffff">[MACD✕]</span> ';
            else if (an.macd_bullish) html += '<span style="color:#44ff44">[MACD+]</span> ';
            else if (an.macd_bearish != null) html += '<span style="color:#ff6666">[MACD-]</span> ';
            if (an.ema_crossover) html += '<span style="color:#ff00ff">[EMA✕]</span> ';
            else if (an.ema_bullish) html += '<span style="color:#66ff66">[EMA↑]</span> ';
            else if (an.ema_bearish != null) html += '<span style="color:#ff8888">[EMA↓]</span> ';
            if (an.rsi_oversold) html += '<span style="color:#00ff00">[RSI↓]</span> ';
            else if (an.rsi_overbought) html += '<span style="color:#ff4444">[RSI↑]</span> ';
            if (an.in_order_block) html += '<span style="color:#f39c12">[OB]</span> ';
            if (an.has_fvg) html += '<span style="color:#9b59b6">[FVG]</span> ';
            return html || 'No signals detected';
        }

        function _btcSetSignalBadge(id, signal) {
            const el = document.getElementById(id);
            if (!el) return;
            const s = (signal || 'NONE').toUpperCase();
            el.textContent = s;
            el.className = 'signal-badge signal-' + s.toLowerCase();
        }

        function _btcRenderPositions(id, positions) {
            const el = document.getElementById(id);
            if (!el) return;
            const rows = positions || [];
            if (!rows.length) {
                el.innerHTML = '<div style="color:#666;font-size:0.9em;margin-top:5px;">No positions</div>';
                return;
            }
            el.innerHTML = rows.map(function(p) {
                const pnl = Number(p.profit || 0);
                const lr = Number(p.max_loss_reach || 0);
                return '<div class="position-row">'
                    + '<span>' + String((p.type || '').toUpperCase()) + ' ' + (p.volume || 0) + ' <span style="color:#e74c3c">LR -$' + lr.toFixed(2) + '</span></span>'
                    + '<span class="' + (pnl >= 0 ? 'profit-pos' : 'profit-neg') + '">$' + pnl.toFixed(2) + '</span>'
                    + '</div>';
            }).join('');
        }

        function updateBtcStatus(d) {
            if (!d) return;
            _setTxt('btcConnState', d.connected ? 'MT5 Connected' : 'MT5 Not Connected', d.connected ? '#27ae60' : '#e74c3c');

            const m1 = d.m1 || {};
            const m5 = d.m5 || {};
            const h4 = d.h4 || {};

            _setTxt('btc1mStatus', m1.running ? 'Running' : 'Idle', m1.running ? '#27ae60' : '#888');
            _setTxt('btc1mSession', 'Session: ' + (m1.session_filter ? 'On' : 'Off'), m1.session_filter ? '#f39c12' : '#27ae60');
            _btcSetSignalBadge('btc1mSignalBadge', m1.signal || 'NONE');
            _setTxt('btc1mPrice', (m1.price_bid || 0) > 0 ? Number(m1.price_bid).toFixed(2) : '--.-----');
            _setTxt('btc1mSpreadPts', (m1.spread_points || 0) > 0 ? String(m1.spread_points) : '--');
            _setTxt('btc1mBuyScore', String(m1.buy_score || 0));
            _setTxt('btc1mSellScore', String(m1.sell_score || 0));
            _setTxt('btc1mStrength', String(m1.signal_strength || 0));
            var b1 = document.getElementById('btc1mBar'); if (b1) b1.style.width = String(m1.signal_strength || 0) + '%';
            var a1 = document.getElementById('btc1mAnalysis'); if (a1) a1.innerHTML = _btcAnalysisHtml(m1.analysis);
            _btcRenderPositions('btc1mPosMini', m1.positions || []);
            var reason1m = '';
            var reason1mColor = '#27ae60';
            if (!d.connected) {
                reason1m = 'Blocked: MT5 not connected.';
                reason1mColor = '#e74c3c';
            } else if (!d.trading_enabled) {
                reason1m = 'Blocked: global trading switch is OFF.';
                reason1mColor = '#e74c3c';
            } else if (!m1.running) {
                reason1m = 'Idle: BTC 1m bot is not running.';
                reason1mColor = '#aaa';
            } else if (!m1.engine_running) {
                reason1m = 'Blocked: 1m engine is not running.';
                reason1mColor = '#e67e22';
            } else if ((m1.symbol_key || '').toUpperCase() !== 'BTCUSD') {
                reason1m = 'Blocked: wrong symbol loaded for 1m mode.';
                reason1mColor = '#e67e22';
            } else if (m1.session_filter) {
                reason1m = 'Blocked: session filter is ON for crypto.';
                reason1mColor = '#f39c12';
            } else {
                reason1m = 'Ready: 1m gates passed, waiting for valid setup.';
                reason1mColor = '#27ae60';
            }
            _setTxt('btc1mReason', reason1m, reason1mColor);
            _setBtnState('btc1mStart', 'btc1mStop', m1.running);

            _setTxt('btc5mStatus', m5.running ? 'Running' : 'Idle', m5.running ? '#27ae60' : '#888');
            var m5Independent = !!(m5.independent_mode || m5.running);
            _setTxt('btc5mMode', m5Independent ? 'Independent active' : 'Independent standby', m5Independent ? '#27ae60' : '#aaa');
            _setTxt('btc5mSignal', (m5.signal_strength || 0) + '/' + (m5.min_signal_strength || 0), (m5.signal_strength || 0) >= (m5.min_signal_strength || 0) ? '#27ae60' : '#f39c12');
            _setTxt('btc5mSpread', (m5.spread_points || 0) + '/' + (m5.max_spread || 0), (m5.spread_points || 0) <= (m5.max_spread || 0) ? '#27ae60' : '#e74c3c');
            _setTxt('btc5mPos', (m5.positions || 0) + '/' + (m5.max_positions || 1), (m5.positions || 0) < (m5.max_positions || 1) ? '#27ae60' : '#f39c12');
            _btcSetSignalBadge('btc5mSignalBadge', m5.signal || 'NONE');
            _setTxt('btc5mPrice', (m5.price_bid || 0) > 0 ? Number(m5.price_bid).toFixed(2) : '--.-----');
            _setTxt('btc5mSpreadPts', (m5.spread_points || 0) > 0 ? String(m5.spread_points) : '--');
            _setTxt('btc5mBuyScore', String(m5.buy_score || 0));
            _setTxt('btc5mSellScore', String(m5.sell_score || 0));
            _setTxt('btc5mStrength', String(m5.signal_strength || 0));
            var b5 = document.getElementById('btc5mBar'); if (b5) b5.style.width = String(m5.signal_strength || 0) + '%';
            var a5 = document.getElementById('btc5mAnalysis'); if (a5) a5.innerHTML = _btcAnalysisHtml(m5.analysis);
            _btcRenderPositions('btc5mPosMini', m5.positions_list || []);

            var reason = '';
            var reasonColor = '#27ae60';
            if (!d.connected) {
                reason = 'Blocked: MT5 not connected.';
                reasonColor = '#e74c3c';
            } else if (!d.trading_enabled) {
                reason = 'Blocked: global trading switch is OFF.';
                reasonColor = '#e74c3c';
            } else if (!m5.running) {
                reason = 'Idle: BTC 5m bot is not running.';
                reasonColor = '#aaa';
            } else if (!m5.engine_running) {
                reason = 'Blocked: 5m engine is not running.';
                reasonColor = '#e67e22';
            } else if ((m5.positions || 0) >= (m5.max_positions || 1)) {
                reason = 'Blocked: max BTC positions reached.';
                reasonColor = '#f39c12';
            } else if ((m5.spread_points || 0) > (m5.max_spread || 0)) {
                reason = 'Blocked: spread too high (' + (m5.spread_points || 0) + ' > ' + (m5.max_spread || 0) + ').';
                reasonColor = '#e74c3c';
            } else if ((m5.signal_strength || 0) < (m5.min_signal_strength || 0)) {
                reason = 'Waiting: signal below threshold (' + (m5.signal_strength || 0) + ' < ' + (m5.min_signal_strength || 0) + ').';
                reasonColor = '#f39c12';
            } else {
                reason = 'Ready: all gates passed, waiting for valid entry execution.';
                reasonColor = '#27ae60';
            }
            _setTxt('btc5mReason', reason, reasonColor);
            _setBtnState('btc5mStart', 'btc5mStop', m5.running);

            _setTxt('btc4hStatus', h4.running ? 'Running' : 'Idle', h4.running ? '#27ae60' : '#888');
            _setTxt('btc4hSession', 'Session: ' + (h4.session_filter ? 'On' : 'Off'), h4.session_filter ? '#f39c12' : '#27ae60');
            _btcSetSignalBadge('btc4hSignalBadge', h4.signal || 'NONE');
            _setTxt('btc4hPrice', (h4.price_bid || 0) > 0 ? Number(h4.price_bid).toFixed(2) : '--.-----');
            _setTxt('btc4hSpreadPts', (h4.spread_points || 0) > 0 ? String(h4.spread_points) : '--');
            _setTxt('btc4hBuyScore', String(h4.buy_score || 0));
            _setTxt('btc4hSellScore', String(h4.sell_score || 0));
            _setTxt('btc4hStrength', String(h4.signal_strength || 0));
            var b4 = document.getElementById('btc4hBar'); if (b4) b4.style.width = String(h4.signal_strength || 0) + '%';
            var a4 = document.getElementById('btc4hAnalysis'); if (a4) a4.innerHTML = _btcAnalysisHtml(h4.analysis);
            _btcRenderPositions('btc4hPosMini', h4.positions || []);
            var reason4h = '';
            var reason4hColor = '#27ae60';
            if (!d.connected) {
                reason4h = 'Blocked: MT5 not connected.';
                reason4hColor = '#e74c3c';
            } else if (!d.trading_enabled) {
                reason4h = 'Blocked: global trading switch is OFF.';
                reason4hColor = '#e74c3c';
            } else if (!h4.running) {
                reason4h = 'Idle: BTC 4h bot is not running.';
                reason4hColor = '#aaa';
            } else if (!h4.engine_running) {
                reason4h = 'Blocked: 4h engine is not running.';
                reason4hColor = '#e67e22';
            } else if ((h4.symbol_key || '').toUpperCase() !== 'BTCUSD') {
                reason4h = 'Blocked: wrong symbol loaded for 4h mode.';
                reason4hColor = '#e67e22';
            } else if (h4.session_filter) {
                reason4h = 'Blocked: session filter is ON for crypto.';
                reason4hColor = '#f39c12';
            } else {
                reason4h = 'Ready: 4h gates passed, waiting for valid setup.';
                reason4hColor = '#27ae60';
            }
            _setTxt('btc4hReason', reason4h, reason4hColor);
            _setBtnState('btc4hStart', 'btc4hStop', h4.running);
        }

        function refreshBtcStatus() {
            fetch('/api/btc/status')
                .then(r => r.json())
                .then(updateBtcStatus)
                .catch(() => {});
        }

        function saveBtcCardConfig(mode) {
            var payload = {};
            if (mode === '1m') {
                payload.m1 = {
                    lot_size: parseFloat(document.getElementById('btc1mLot').value) || 0,
                    max_positions: parseInt(document.getElementById('btc1mMaxPos').value) || 3,
                };
            } else if (mode === '5m') {
                payload.m5 = {
                    lot_size: parseFloat(document.getElementById('btc5mLot').value) || 0,
                    max_positions: parseInt(document.getElementById('btc5mMaxPos').value) || 1,
                };
            } else if (mode === '4h') {
                payload.h4 = {
                    lot_size: parseFloat(document.getElementById('btc4hLot').value) || 0,
                    max_positions: parseInt(document.getElementById('btc4hMaxPos').value) || 2,
                };
            }
            fetch('/api/btc/config', {
                method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
            }).then(r => r.json()).then(function() {
                var id = mode === '1m' ? 'btc1mSaved' : (mode === '5m' ? 'btc5mSaved' : 'btc4hSaved');
                var el = document.getElementById(id);
                if (el) {
                    el.style.display = 'inline';
                    setTimeout(function(){ el.style.display = 'none'; }, 1800);
                }
                loadBtcConfig();
                refreshBtcStatus();
            }).catch(function() {
                alert('Failed to save BTC card config');
            });
        }

        function btcControl(mode, start) {
            fetch('/api/btc/control', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode: mode, start: !!start})
            })
            .then(r => r.json())
            .then(d => {
                if (!d.success) {
                    alert(d.error || 'BTC control failed');
                    return;
                }
                if (d.status) updateBtcStatus(d.status);
                refresh();
                refreshGold();
                refreshDayTrade();
            })
            .catch(() => alert('BTC control request failed'));
        }

        // ── Performance Reports ──────────────────────────────────────────────
        function loadReports() {
            const tbody = document.getElementById('reportBody');
            if (tbody) tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#888;padding:24px">Loading\u2026</td></tr>';
            fetch('/api/reports')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        if (tbody) tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:#e74c3c;padding:24px">Error: ${data.error}</td></tr>`;
                        return;
                    }
                    window._lastReportData = data;  // store for export
                    const period = document.getElementById('reportPeriod').value;
                    const rows   = data[period] || [];

                    // Totals summary cards
                    const totals = data.totals || {};
                    const totDiv = document.getElementById('reportTotalsRow');
                    const totCards = Object.entries(totals).map(([bot, s]) => {
                        if (!s || !s.trades) return '';
                        const wr = s.win_rate || 0;
                        const cl = wr >= 55 ? '#27ae60' : (wr >= 45 ? '#f39c12' : '#e74c3c');
                        return `<div class="card" style="flex:1;min-width:200px;padding:12px">
                            <div style="font-weight:700;color:#aaa;margin-bottom:8px">${bot}</div>
                            <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:0.85em">
                                <span style="color:#888">All-time trades:</span><b>${s.trades}</b>
                                <span style="color:#888">Win rate:</span><b style="color:${cl}">${wr}%</b>
                                <span style="color:#888">Total P&amp;L:</span>
                                <b style="color:${s.total_profit>=0?'#27ae60':'#e74c3c'}">${(s.total_profit>=0?'+':'')}$${s.total_profit.toFixed(2)}</b>
                                <span style="color:#888">Best trade:</span><b style="color:#27ae60">+$${s.max_win.toFixed(2)}</b>
                                <span style="color:#888">Worst trade:</span><b style="color:#e74c3c">-$${Math.abs(s.max_loss).toFixed(2)}</b>
                            </div>
                        </div>`;
                    }).join('');
                    totDiv.innerHTML = totCards || '<span style="color:#666;font-size:0.88em">No all-time trade data yet</span>';

                    // Filtered to selected period
                    const thead = document.getElementById('reportThead');
                    thead.innerHTML = `<tr><th>${period.charAt(0).toUpperCase()+period.slice(1)}</th>
                        <th>Bot</th><th>Trades</th><th>Win%</th>
                        <th>W/L</th><th>Total P&amp;L</th><th>Avg</th>
                        <th>Best</th><th>Worst</th></tr>`;
                    if (!rows.length) {
                        tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#888;padding:24px">No data yet &mdash; trades will appear here as they close</td></tr>';
                        return;
                    }
                    tbody.innerHTML = rows.map(r => {
                        const wr = r.win_rate || 0;
                        const cl = wr >= 55 ? '#27ae60' : (wr >= 45 ? '#f39c12' : '#e74c3c');
                        const pc = r.total_profit >= 0 ? '#27ae60' : '#e74c3c';
                        const pf = (r.total_profit >= 0 ? '+' : '') + '$' + r.total_profit.toFixed(2);
                        const allRow = r.bot_key === 'ALL';
                        const border = allRow ? 'border-top:2px solid #333;font-weight:600;' : '';
                        const botColor = allRow ? '#aaa' : (r.bot_key==='' ? '#3498db' : (r.bot_key==='Gold1M' ? '#ffd700' : (r.bot_key==='BTC' ? '#ff9f43' : '#f39c12')));
                        return `<tr style="${border}">
                            <td>${r.period}</td>
                            <td style="color:${botColor}">${r.bot}</td>
                            <td>${r.trades}</td>
                            <td style="color:${cl}">${wr}%</td>
                            <td>${r.wins} / ${r.losses}</td>
                            <td style="color:${pc}">${pf}</td>
                            <td style="color:${pc}">${(r.avg_profit>=0?'+':'')+'$'+r.avg_profit.toFixed(2)}</td>
                            <td style="color:#27ae60">+$${r.max_win.toFixed(2)}</td>
                            <td style="color:#e74c3c">-$${Math.abs(r.max_loss).toFixed(2)}</td>
                        </tr>`;
                    }).join('');
                })
                .catch(e => {
                    console.error('Reports load error:', e);
                    if (tbody) tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#e74c3c;padding:24px">Failed to load reports &mdash; check console</td></tr>';
                });
        }

        function exportReportHtml() {
            const period = document.getElementById('reportPeriod').value;
            window.location.href = '/api/export_report?period=' + encodeURIComponent(period);
        }

        function loadEmailSettings() {
            fetch('/api/email_config')
                .then(r => r.json())
                .then(d => {
                    if (d.error) return;
                    const cfg = d.config || {};
                    const setVal = (id, v) => { const el = document.getElementById(id); if (el && document.activeElement !== el) el.value = v == null ? '' : String(v); };
                    const setChk = (id, v) => { const el = document.getElementById(id); if (el && document.activeElement !== el) el.checked = !!v; };
                    setVal('emailEnabled', cfg.enabled ? 'true' : 'false');
                    setVal('emailSmtpHost', cfg.smtp_host || '');
                    setVal('emailSmtpPort', cfg.smtp_port || 587);
                    setVal('emailUsername', cfg.username || '');
                    setVal('emailPassword', '');
                    setVal('emailFrom', cfg.from_email || '');
                    setVal('emailTo', cfg.to_email || '');
                    setVal('emailDailyTime', cfg.daily_summary_time_utc || '23:55');
                    setVal('emailRiskBuckets', (cfg.risk_alert_thresholds || []).join(','));
                    setChk('emailUseTls', cfg.use_tls !== false);
                    setChk('emailUseSsl', !!cfg.use_ssl);
                    const msg = document.getElementById('emailCfgMsg');
                    if (msg) msg.textContent = 'Email settings loaded.';
                })
                .catch(() => {
                    const msg = document.getElementById('emailCfgMsg');
                    if (msg) msg.textContent = 'Failed to load email settings';
                });
        }

        function saveEmailSettings() {
            const payload = {
                enabled: document.getElementById('emailEnabled').value === 'true',
                smtp_host: (document.getElementById('emailSmtpHost').value || '').trim(),
                smtp_port: parseInt(document.getElementById('emailSmtpPort').value || '587', 10),
                username: (document.getElementById('emailUsername').value || '').trim(),
                password: (document.getElementById('emailPassword').value || ''),
                from_email: (document.getElementById('emailFrom').value || '').trim(),
                to_email: (document.getElementById('emailTo').value || '').trim(),
                daily_summary_time_utc: (document.getElementById('emailDailyTime').value || '').trim(),
                risk_alert_thresholds: (document.getElementById('emailRiskBuckets').value || '').trim(),
                use_tls: !!document.getElementById('emailUseTls').checked,
                use_ssl: !!document.getElementById('emailUseSsl').checked,
            };
            fetch('/api/email_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(d => {
                const msg = document.getElementById('emailCfgMsg');
                if (d.success) {
                    const ok = document.getElementById('emailCfgSaved');
                    if (ok) { ok.style.display = 'inline'; setTimeout(() => ok.style.display = 'none', 1800); }
                    if (msg) msg.textContent = 'Email settings saved.';
                    const pwd = document.getElementById('emailPassword');
                    if (pwd) pwd.value = '';
                } else {
                    if (msg) msg.textContent = 'Save failed: ' + (d.error || 'unknown');
                }
            }).catch(() => {
                const msg = document.getElementById('emailCfgMsg');
                if (msg) msg.textContent = 'Save failed: request error';
            });
        }

        function sendTestEmail() {
            const msg = document.getElementById('emailCfgMsg');
            if (msg) msg.textContent = 'Sending test email...';
            fetch('/api/email_test', {method: 'POST'})
                .then(r => r.json())
                .then(d => {
                    if (msg) msg.textContent = d.success ? 'Test email sent.' : ('Test failed: ' + (d.error || 'unknown'));
                })
                .catch(() => {
                    if (msg) msg.textContent = 'Test failed: request error';
                });
        }

        function _advMsg(text, ok) {
            const el = document.getElementById('advancedOpsMsg');
            if (!el) return;
            el.textContent = text;
            el.style.color = ok ? '#2ecc71' : '#e74c3c';
        }

        function loadPushSettings() {
            fetch('/api/push_config')
                .then(r => r.json())
                .then(d => {
                    const c = d.config || {};
                    const setVal = (id, v) => { const el = document.getElementById(id); if (el && document.activeElement !== el) el.value = (v == null ? '' : String(v)); };
                    setVal('pushEnabled', c.enabled ? 'true' : 'false');
                    setVal('pushTelegramToken', '');
                    setVal('pushTelegramChat', c.telegram_chat_id || '');
                    setVal('pushDiscordWebhook', c.discord_webhook_url || '');
                })
                .catch(() => _advMsg('Failed to load push settings', false));
        }

        function savePushSettings() {
            const payload = {
                enabled: document.getElementById('pushEnabled').value === 'true',
                telegram_bot_token: (document.getElementById('pushTelegramToken').value || '').trim(),
                telegram_chat_id: (document.getElementById('pushTelegramChat').value || '').trim(),
                discord_webhook_url: (document.getElementById('pushDiscordWebhook').value || '').trim(),
            };
            fetch('/api/push_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    _advMsg('Push settings saved', true);
                    const token = document.getElementById('pushTelegramToken');
                    if (token) token.value = '';
                } else {
                    _advMsg('Push save failed', false);
                }
            }).catch(() => _advMsg('Push save failed', false));
        }

        function sendPushTest() {
            fetch('/api/push_test', {method: 'POST'})
                .then(r => r.json())
                .then(d => _advMsg(d.success ? 'Push test sent' : 'Push test failed', !!d.success))
                .catch(() => _advMsg('Push test failed', false));
        }

        function activateEmergencyPause() {
            const reason = prompt('Emergency pause reason:', 'Manual safety pause') || 'Manual safety pause';
            const closePositions = confirm('Close all open positions now? OK = Yes, Cancel = No');
            fetch('/api/emergency_pause', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'activate', reason: reason, close_positions: closePositions})
            }).then(r => r.json()).then(d => {
                _advMsg(d.success ? 'Emergency pause activated' : ('Emergency pause failed: ' + (d.error || 'unknown')), !!d.success);
                refresh(); refreshGold(); refreshDayTrade(); refreshWatchdogs();
            }).catch(() => _advMsg('Emergency pause request failed', false));
        }

        function releaseEmergencyPause() {
            fetch('/api/emergency_pause', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'release'})
            }).then(r => r.json()).then(d => {
                _advMsg(d.success ? 'Emergency pause released' : ('Release failed: ' + (d.error || 'unknown')), !!d.success);
                refreshWatchdogs();
            }).catch(() => _advMsg('Emergency pause release failed', false));
        }

        function refreshWatchdogs() {
            fetch('/api/watchdogs').then(r => r.json()).then(d => {
                const el = document.getElementById('watchdogView');
                if (!el) return;
                const snap = d.snapshot || {};
                const state = d.state || {};
                el.textContent = JSON.stringify({snapshot: snap, broker_last_ok: state.last_broker_ok, emergency_pause: (window.__INITIAL_STATE__||{}).emergency_pause}, null, 2);
            }).catch(() => {
                const el = document.getElementById('watchdogView');
                if (el) el.textContent = 'Failed to load watchdog data';
            });
        }

        function loadPerformanceSplits() {
            fetch('/api/performance_splits').then(r => r.json()).then(d => {
                const el = document.getElementById('strategyHealthView');
                if (!el) return;
                el.textContent = 'Per-bot performance splits:\\n' + JSON.stringify(d, null, 2);
            }).catch(() => {
                const el = document.getElementById('strategyHealthView');
                if (el) el.textContent = 'Failed to load performance splits';
            });
        }

        function loadStrategyHealth() {
            fetch('/api/strategy_health').then(r => r.json()).then(d => {
                const el = document.getElementById('strategyHealthView');
                if (!el) return;
                el.textContent = 'Strategy health panel:\\n' + JSON.stringify(d, null, 2);
            }).catch(() => {
                const el = document.getElementById('strategyHealthView');
                if (el) el.textContent = 'Failed to load strategy health';
            });
        }

        function loadOpenRiskDashboard() {
            fetch('/api/open_risk_dashboard').then(r => r.json()).then(d => {
                const el = document.getElementById('openRiskView');
                if (!el) return;
                el.textContent = JSON.stringify(d, null, 2);
            }).catch(() => {
                const el = document.getElementById('openRiskView');
                if (el) el.textContent = 'Failed to load open risk dashboard';
            });
        }

        function loadTradeReplay() {
            fetch('/api/trade_replay?bot=all').then(r => r.json()).then(d => {
                const el = document.getElementById('tradeReplayView');
                if (!el) return;
                el.textContent = JSON.stringify(d, null, 2);
            }).catch(() => {
                const el = document.getElementById('tradeReplayView');
                if (el) el.textContent = 'Failed to load trade replay';
            });
        }

        function loadConfigHistory() {
            fetch('/api/config_history?limit=100').then(r => r.json()).then(d => {
                const el = document.getElementById('tradeReplayView');
                if (!el) return;
                el.textContent = 'Config change history:\\n' + JSON.stringify(d, null, 2);
            }).catch(() => {
                const el = document.getElementById('tradeReplayView');
                if (el) el.textContent = 'Failed to load config history';
            });
        }

        function applyBtcPreset(profile) {
            fetch('/api/btc/preset', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({profile: profile, target: 'all'})
            })
            .then(r => r.json())
            .then(d => {
                const el = document.getElementById('btcPresetMsg');
                if (!d.success) {
                    el.style.color = '#e74c3c';
                    el.textContent = d.error || 'Preset apply failed';
                    return;
                }
                el.style.color = '#27ae60';
                el.textContent = 'Applied ' + profile + ' preset to BTC bots';
                setTimeout(() => { el.textContent = ''; }, 2500);
                refresh();
                refreshGold();
                refreshDayTrade();
            })
            .catch(() => {
                const el = document.getElementById('btcPresetMsg');
                el.style.color = '#e74c3c';
                el.textContent = 'Preset apply failed';
            });
        }

        function runBtcWalkforward() {
            var profile = document.getElementById('btcWfProfile').value || 'balanced';
            var train_days = parseInt(document.getElementById('btcWfTrain').value, 10) || 21;
            var test_days = parseInt(document.getElementById('btcWfTest').value, 10) || 7;
            var folds = parseInt(document.getElementById('btcWfFolds').value, 10) || 4;

            var summaryEl = document.getElementById('btcWfSummary');
            var bodyEl = document.getElementById('btcWfBody');
            if (!summaryEl || !bodyEl) return;

            summaryEl.style.color = '#bbb';
            summaryEl.textContent = 'Running walk-forward...';
            bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;padding:12px">Working...</td></tr>';

            fetch('/api/btc/walkforward', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({profile: profile, train_days: train_days, test_days: test_days, folds: folds})
            })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d || !d.success) {
                    summaryEl.style.color = '#e74c3c';
                    summaryEl.textContent = (d && d.error) ? d.error : 'Walk-forward failed';
                    bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#e74c3c;padding:12px">No results</td></tr>';
                    return;
                }

                var s = d.summary || {};
                var avgPnl = (s.avg_test_pnl != null) ? ('$' + Number(s.avg_test_pnl).toFixed(2)) : '--';
                var avgWr = (s.avg_test_win_rate != null) ? (Number(s.avg_test_win_rate).toFixed(1) + '%') : '--';
                var robustness = (s.robustness != null) ? (Math.round(Number(s.robustness) * 100) + '%') : '--';

                summaryEl.style.color = '#bbb';
                summaryEl.textContent =
                    'Profile=' + (d.profile || profile) +
                    ' | Folds=' + (s.folds || 0) +
                    ' | Avg Test PnL=' + avgPnl +
                    ' | Avg Test WR=' + avgWr +
                    ' | Robustness=' + robustness;

                var rows = d.folds || [];
                if (!rows.length) {
                    bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#888;padding:12px">No fold rows</td></tr>';
                    return;
                }

                bodyEl.innerHTML = rows.map(function(f) {
                    var train = f.train || {};
                    var test = f.test || {};
                    var trainPnl = Number(train.pnl || 0);
                    var trainWr = Number(train.win_rate || 0);
                    var testPnl = Number(test.pnl || 0);
                    var testWr = Number(test.win_rate || 0);
                    var testDd = Number(test.max_drawdown || 0);
                    var testPnlColor = testPnl >= 0 ? '#27ae60' : '#e74c3c';
                    var testWrColor = testWr >= 50 ? '#27ae60' : (testWr >= 40 ? '#f39c12' : '#e74c3c');

                    return '<tr>' +
                        '<td>' + (f.fold || '') + '</td>' +
                        '<td>' + (trainPnl >= 0 ? '+' : '') + '$' + trainPnl.toFixed(2) + '</td>' +
                        '<td>' + trainWr.toFixed(1) + '%</td>' +
                        '<td style="color:' + testPnlColor + '">' + (testPnl >= 0 ? '+' : '') + '$' + testPnl.toFixed(2) + '</td>' +
                        '<td style="color:' + testWrColor + '">' + testWr.toFixed(1) + '%</td>' +
                        '<td>' + testDd.toFixed(2) + '%</td>' +
                        '</tr>';
                }).join('');
            })
            .catch(function() {
                summaryEl.style.color = '#e74c3c';
                summaryEl.textContent = 'Walk-forward request failed';
                bodyEl.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#e74c3c;padding:12px">Error</td></tr>';
            });
        }

        function loadBtcConfig() {
            fetch('/api/btc/config').then(r => r.json()).then(function(cfgData) {
                var g = (cfgData && cfgData.m1) || {};
                var d = (cfgData && cfgData.h4) || {};
                var m5 = (cfgData && cfgData.m5) || {};

                var setIf = function(id, val) {
                    var el = document.getElementById(id);
                    if (el && document.activeElement !== el && val != null) el.value = val;
                };

                setIf('btcCfg1mLot', g.lot_size);
                setIf('btcCfg1mMaxPos', g.max_positions);
                setIf('btcCfg1mSpread', g.max_spread);
                setIf('btcCfg1mScore', g.confluence_score);
                setIf('btcCfg1mSl', g.sl_atr_mult);
                setIf('btcCfg1mTp', g.tp_atr_mult);

                setIf('btcCfg4hLot', d.lot_size);
                setIf('btcCfg4hMaxPos', d.max_positions);
                setIf('btcCfg4hSpread', d.max_spread);
                setIf('btcCfg4hScore', d.confluence_score);
                setIf('btcCfg4hSl', d.sl_atr_mult);
                setIf('btcCfg4hTp', d.tp_atr_mult);

                setIf('btcCfg5mLot', m5.lot_size);
                setIf('btcCfg5mMaxPos', m5.max_positions);
                setIf('btcCfg5mSpread', m5.max_spread);
                setIf('btcCfg5mMinSignal', m5.min_signal_strength);
                setIf('btcCfg5mLotMul', m5.lot_size_multiplier);
                var enEl = document.getElementById('btcCfg5mEnabled');
                if (enEl && document.activeElement !== enEl && m5.enabled != null) enEl.checked = !!m5.enabled;

                // Mirror compact card controls
                setIf('btc1mLot', g.lot_size);
                setIf('btc1mMaxPos', g.max_positions);
                setIf('btc5mLot', m5.lot_size);
                setIf('btc5mMaxPos', m5.max_positions);
                setIf('btc4hLot', d.lot_size);
                setIf('btc4hMaxPos', d.max_positions);
            }).catch(function() {});
        }

        function saveBtcConfig() {
            var payload = {
                m1: {
                    lot_size: parseFloat(document.getElementById('btcCfg1mLot').value) || 0,
                    max_positions: parseInt(document.getElementById('btcCfg1mMaxPos').value) || 3,
                    max_spread: parseInt(document.getElementById('btcCfg1mSpread').value) || 80,
                    confluence_score: parseInt(document.getElementById('btcCfg1mScore').value) || 4,
                    sl_atr_mult: parseFloat(document.getElementById('btcCfg1mSl').value) || 1.5,
                    tp_atr_mult: parseFloat(document.getElementById('btcCfg1mTp').value) || 3.0,
                },
                h4: {
                    lot_size: parseFloat(document.getElementById('btcCfg4hLot').value) || 0,
                    max_positions: parseInt(document.getElementById('btcCfg4hMaxPos').value) || 2,
                    max_spread: parseInt(document.getElementById('btcCfg4hSpread').value) || 80,
                    confluence_score: parseInt(document.getElementById('btcCfg4hScore').value) || 8,
                    sl_atr_mult: parseFloat(document.getElementById('btcCfg4hSl').value) || 1.5,
                    tp_atr_mult: parseFloat(document.getElementById('btcCfg4hTp').value) || 3.0,
                },
                m5: {
                    lot_size: parseFloat(document.getElementById('btcCfg5mLot').value) || 0,
                    max_positions: parseInt(document.getElementById('btcCfg5mMaxPos').value) || 1,
                    max_spread: parseInt(document.getElementById('btcCfg5mSpread').value) || 120,
                    min_signal_strength: parseInt(document.getElementById('btcCfg5mMinSignal').value) || 0,
                    lot_size_multiplier: parseFloat(document.getElementById('btcCfg5mLotMul').value) || 1.0,
                    enabled: !!document.getElementById('btcCfg5mEnabled').checked,
                }
            };

            fetch('/api/btc/config', {
                method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
            }).then(r => r.json()).then(function() {
                var el = document.getElementById('btcCfgSaved');
                if (el) {
                    el.style.display = 'inline';
                    setTimeout(function(){ el.style.display = 'none'; }, 2200);
                }
                refreshBtcStatus();
                refreshGold();
                refreshDayTrade();
                refresh();
            }).catch(function() {
                alert('Failed to save BTC config');
            });
        }

        // ── Gold 1-Min Bot ──
        function startGold() {
            fetch('/api/gold/start', {method:'POST'})
                .then(r=>r.json())
                .then(d=>{ if(!d.success) alert(d.error||'Error'); refreshGold(); })
                .catch(console.error);
        }
        function stopGold() {
            fetch('/api/gold/stop', {method:'POST'})
                .then(r=>r.json())
                .then(()=>refreshGold())
                .catch(console.error);
        }
        function refreshGold() {
            fetch('/api/gold/status')
                .then(r=>r.json())
                .then(d=>{ try { goldUpdate(d); } catch (e) { console.error('goldUpdate failed', e); } })
                .catch(console.error);
        }

        // ── Gold Day Trade Bot ──
        function startDayTrade() {
            fetch('/api/daytrade/start', {method:'POST'})
                .then(r=>r.json())
                .then(d=>{ if(!d.success) alert(d.error||'Error'); refreshDayTrade(); })
                .catch(console.error);
        }
        function stopDayTrade() {
            fetch('/api/daytrade/stop', {method:'POST'})
                .then(r=>r.json())
                .then(()=>refreshDayTrade())
                .catch(console.error);
        }
        function refreshDayTrade() {
            fetch('/api/daytrade/status')
                .then(r=>r.json())
                .then(d=>{ try { dayTradeUpdate(d); } catch (e) { console.error('dayTradeUpdate failed', e); } })
                .catch(console.error);
        }

        function saveDayConfig() {
            const cfg = {
                symbol_key:       sanitizeNonBtcSymbolKey(document.getElementById('dCfgSymbol').value || 'GOLD'),
                lot_size:         parseFloat(document.getElementById('dCfgLot').value)     || 0,
                max_positions:    parseInt(document.getElementById('dCfgMaxPos').value)    || 2,
                max_spread:       parseInt(document.getElementById('dCfgSpread').value)    || 80,
                confluence_score: parseInt(document.getElementById('dCfgScore').value)     || 8,
                sl_atr_mult:      parseFloat(document.getElementById('dCfgSl').value)      || 1.5,
                tp_atr_mult:      parseFloat(document.getElementById('dCfgTp').value)      || 3.0,
                recycle_pct:      (parseFloat(document.getElementById('dCfgRecycle').value) || 60) / 100,
                session_filter:   document.getElementById('dCfgSession').checked,
                candle_patterns:  document.getElementById('dCfgPatterns').checked,
            };
            fetch('/api/daytrade/config', {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify(cfg)
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    const el = document.getElementById('dayCfgSaved');
                    el.style.display = 'inline';
                    setTimeout(() => el.style.display = 'none', 2000);
                }
            });
        }

        function dayTradeUpdate(d) {
            if (!d) return;
            const running = d.running;
            document.getElementById('dayDot').className = running ? 'status-dot status-running' : 'status-dot status-stopped';
            let statusText = 'Running';
            if (!running) {
                const gr = d.goal_reached;
                const reason = d.stop_reason || '';
                statusText = gr ? 'Idle — Daily goal reached' : (reason ? 'Idle — ' + reason : 'Idle');
            }
            document.getElementById('dayStatus').textContent = statusText;
            document.getElementById('btnDayStart').disabled = running || !d.connected;
            document.getElementById('btnDayStop').disabled  = !running;

            const st = d.stats || {};
            document.getElementById('dayOpened').textContent   = st.trades_opened || 0;
            document.getElementById('dayRecycled').textContent = st.recycled       || 0;
            document.getElementById('dayWins').textContent     = st.wins           || 0;
            document.getElementById('dayLosses').textContent   = st.losses         || 0;

            const live = d.live || {};
            const dayTitle = document.getElementById('daySymbolTitle');
            if (dayTitle) dayTitle.textContent = 'GOLD / XAU — H4';

            // Session badge
            const sb = document.getElementById('daySessionBadge');
            if (live.in_session) {
                sb.className = 'session-badge-on';
                sb.textContent = '\u25CF ' + (live.session_name || 'Active');
            } else {
                sb.className = 'session-badge-off';
                sb.textContent = live.session_name || 'Off-Hours';
            }

            document.getElementById('dayAtrVal').textContent    = live.atr    != null ? parseFloat(live.atr).toFixed(2) : '--';
            document.getElementById('daySpreadVal').textContent = live.spread != null ? live.spread : '--';

            if (live.price && live.price.bid != null)
                document.getElementById('dayPriceBig').textContent = parseFloat(live.price.bid).toFixed(2);

            const sig = document.getElementById('daySignalBadge');
            sig.textContent = live.signal || 'NONE';
            sig.className   = 'signal-badge signal-' + (live.signal || 'none').toLowerCase();

            document.getElementById('dayBuyScore').textContent  = live.buy_score  || 0;
            document.getElementById('daySellScore').textContent = live.sell_score || 0;

            // Indicator chips
            const ind = live.indicators || {};
            const emaEl = document.getElementById('dayIndEma');
            if (emaEl) {
                const ema_fast = ind.ema_fast, ema_slow = ind.ema_slow;
                if (ema_fast != null && ema_slow != null) {
                    emaEl.textContent = ema_fast > ema_slow ? '\u25B2 BULL' : '\u25BC BEAR';
                    emaEl.style.color = ema_fast > ema_slow ? '#27ae60' : '#e74c3c';
                } else { emaEl.textContent = '--'; emaEl.style.color = '#aaa'; }
            }
            const rsiEl = document.getElementById('dayIndRsi');
            if (rsiEl && ind.rsi != null) {
                rsiEl.textContent = parseFloat(ind.rsi).toFixed(1);
                rsiEl.style.color = ind.rsi < 35 ? '#27ae60' : ind.rsi > 65 ? '#e74c3c' : '#aaa';
            }
            const bbEl = document.getElementById('dayIndBb');
            if (bbEl && ind.bb_pct != null) {
                bbEl.textContent = (ind.bb_pct * 100).toFixed(0) + '%';
                bbEl.style.color = ind.bb_pct < 0.2 ? '#27ae60' : ind.bb_pct > 0.8 ? '#e74c3c' : '#aaa';
            }
            const atrEl = document.getElementById('dayIndAtr');
            if (atrEl) { atrEl.textContent = live.atr != null ? parseFloat(live.atr).toFixed(2) : '--'; }
            const scoreEl = document.getElementById('dayIndScore');
            if (scoreEl) {
                const sc = live.signal === 'BUY' ? (live.buy_score || 0) : (live.sell_score || 0);
                scoreEl.textContent = sc;
                scoreEl.style.color = sc >= 8 ? '#27ae60' : sc >= 5 ? '#ffd700' : '#aaa';
            }

            // Pattern chip
            const pc = document.getElementById('dayPatternChip');
            if (live.pattern) {
                pc.style.display = 'inline';
                pc.textContent   = live.pattern;
                pc.className     = 'pattern-chip ' + (
                    live.pattern_dir === 'buy'  ? 'pattern-bull' :
                    live.pattern_dir === 'sell' ? 'pattern-bear' : 'pattern-doji'
                );
            } else { pc.style.display = 'none'; }

            // P&L
            const dayPnlEl = document.getElementById('dayLivePnl');
            if (dayPnlEl) {
                const tp = d.live_pnl != null ? d.live_pnl : 0;
                dayPnlEl.textContent = '$' + tp.toFixed(2);
                dayPnlEl.style.color = tp >= 0 ? '#27ae60' : '#e74c3c';
            }

            // Positions
            const positions = live.positions || [];
            document.getElementById('dayPosCount').textContent = positions.length;

            const mini = document.getElementById('dayPosMini');
            mini.innerHTML = positions.length > 0 ? positions.map(p => `
                <div class="position-row">
                    <span>${p.type.toUpperCase()} ${p.volume} @${p.open_price.toFixed(2)} <span style="color:#e74c3c">LR -$${Number(p.max_loss_reach || 0).toFixed(2)}</span></span>
                    <span class="${p.profit >= 0 ? 'profit-pos' : 'profit-neg'}">$${p.profit.toFixed(2)}</span>
                </div>`).join('') : '<div style="color:#666;font-size:0.9em">No positions</div>';

            const pb = document.getElementById('dayPosBody');
            if (positions.length > 0) {
                pb.innerHTML = positions.map(p => {
                    let tpPct = '\u2014';
                    if (p.tp && p.open_price) {
                        const tpD  = p.type === 'buy' ? p.tp - p.open_price : p.open_price - p.tp;
                        const trav = p.type === 'buy' ? p.price_current - p.open_price : p.open_price - p.price_current;
                        if (tpD > 0) tpPct = Math.round(trav / tpD * 100) + '%';
                    }
                    const clr = p.profit >= 0 ? 'profit-pos' : 'profit-neg';
                    const lr = Number(p.max_loss_reach || 0);
                    return `<tr>
                        <td>${p.type.toUpperCase()}</td><td>${p.volume}</td>
                        <td>${p.open_price.toFixed(2)}</td><td>${p.price_current.toFixed(2)}</td>
                        <td style="color:#e74c3c">${p.sl ? p.sl.toFixed(2) : '--'}</td>
                        <td class="${clr}">$${p.profit.toFixed(2)}</td>
                        <td style="color:#e74c3c;font-weight:bold">-$${lr.toFixed(2)}</td>
                        <td style="color:#ffd700">${tpPct}</td>
                    </tr>`;
                }).join('');
            } else {
                pb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#888">No day-trade positions</td></tr>';
            }

            // Log
            const logs = document.getElementById('dayLogs');
            if (d.logs && d.logs.length > 0)
                logs.innerHTML = d.logs.map(l =>
                    `<div class="log-entry"><span class="log-time">${l.time}</span> <span class="log-${l.level}">[${l.level}]</span> ${l.message}</div>`
                ).join('');

            // Sync config inputs
            const cfg = d.config || {};
            const fields = [
                ['dCfgLot',     cfg.lot_size,         'float'],
                ['dCfgMaxPos',  cfg.max_positions,    'int'],
                ['dCfgSpread',  cfg.max_spread,       'int'],
                ['dCfgScore',   cfg.confluence_score, 'int'],
                ['dCfgSl',      cfg.sl_atr_mult,      'float'],
                ['dCfgTp',      cfg.tp_atr_mult,      'float'],
                ['dCfgRecycle', cfg.recycle_pct != null ? Math.round(cfg.recycle_pct * 100) : null, 'int'],
            ];
            fields.forEach(([id, val, type]) => {
                const el = document.getElementById(id);
                if (el && document.activeElement !== el && val != null)
                    el.value = type === 'float' ? val : parseInt(val);
            });
            const sesEl = document.getElementById('dCfgSession');
            const patEl = document.getElementById('dCfgPatterns');
            const symEl = document.getElementById('dCfgSymbol');
            if (sesEl && document.activeElement !== sesEl && cfg.session_filter  != null) sesEl.checked = cfg.session_filter;
            if (patEl && document.activeElement !== patEl && cfg.candle_patterns != null) patEl.checked = cfg.candle_patterns;
            if (symEl && document.activeElement !== symEl && cfg.symbol_key) symEl.value = sanitizeNonBtcSymbolKey(cfg.symbol_key);

            // Update trading_enabled toggle if status contains it
            if (d.trading_enabled !== undefined) _applyTradingEnabled(d.trading_enabled);
        }

        // ── Trading Enabled kill-switch ──
        let _tradingEnabled = true;
        function _applyTradingEnabled(val) {
            _tradingEnabled = val;
            const dot   = document.getElementById('tradingDot');
            const label = document.getElementById('tradingLabel');
            const wrap  = document.getElementById('tradingToggleWrap');
            const isLight = document.body.classList.contains('light-theme');
            const onColor  = isLight ? '#1f6b4b' : '#27ae60';
            const offColor = isLight ? '#9f3a3a' : '#e74c3c';
            const wrapBgOn  = isLight ? 'rgba(31,107,75,0.10)' : 'rgba(0,0,0,0.35)';
            const wrapBgOff = isLight ? 'rgba(159,58,58,0.10)' : 'rgba(0,0,0,0.35)';
            if (val) {
                dot.style.background   = onColor;
                dot.style.boxShadow    = isLight ? 'none' : '0 0 6px ' + onColor;
                label.textContent      = 'TRADING ON';
                label.style.color      = onColor;
                wrap.style.borderColor = onColor;
                wrap.style.background  = wrapBgOn;
            } else {
                dot.style.background   = offColor;
                dot.style.boxShadow    = isLight ? 'none' : '0 0 6px ' + offColor;
                label.textContent      = 'TRADING OFF';
                label.style.color      = offColor;
                wrap.style.borderColor = offColor;
                wrap.style.background  = wrapBgOff;
            }
        }
        function toggleTrading() {
            fetch('/api/trading_enabled', {
                method: 'POST', headers: {'Content-Type':'application/json'},
                body: JSON.stringify({enabled: !_tradingEnabled})
            }).then(r => r.json()).then(d => {
                if (d.success !== false) _applyTradingEnabled(d.trading_enabled);
            }).catch(console.error);
        }
        // Poll trading_enabled state so all browser tabs stay in sync
        function refreshTradingEnabled() {
            fetch('/api/trading_enabled').then(r=>r.json()).then(d => _applyTradingEnabled(d.trading_enabled)).catch(()=>{});
        }

        function saveGoldConfig() {
            const cfg = {
                symbol_key:       sanitizeNonBtcSymbolKey(document.getElementById('gCfgSymbol').value || 'GOLD'),
                lot_size:         parseFloat(document.getElementById('gCfgLot').value)     || 0,
                max_positions:    parseInt(document.getElementById('gCfgMaxPos').value)    || 3,
                max_spread:       parseInt(document.getElementById('gCfgSpread').value)    || 80,
                confluence_score: parseInt(document.getElementById('gCfgScore').value)     || 4,
                sl_atr_mult:      parseFloat(document.getElementById('gCfgSl').value)      || 1.5,
                tp_atr_mult:      parseFloat(document.getElementById('gCfgTp').value)      || 3.0,
                recycle_pct:      (parseFloat(document.getElementById('gCfgRecycle').value) || 50) / 100,
                session_filter:   document.getElementById('gCfgSession').checked,
                candle_patterns:  document.getElementById('gCfgPatterns').checked,
            };
            fetch('/api/gold/config', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(cfg)
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    const el = document.getElementById('goldCfgSaved');
                    el.style.display = 'inline';
                    setTimeout(() => el.style.display = 'none', 2000);
                }
            });
        }

        function goldUpdate(d) {
            if (!d) return;
            const running = d.running;
            document.getElementById('goldDot').className = running ? 'status-dot status-running' : 'status-dot status-stopped';
            // Status text: Running / reason for idle
            let statusText = 'Running';
            if (!running) {
                const gr = d.goal_reached;
                const reason = d.stop_reason || '';
                if (gr) {
                    statusText = 'Idle — Daily goal reached';
                } else if (reason) {
                    statusText = 'Idle — ' + reason;
                } else {
                    statusText = 'Idle';
                }
            }
            document.getElementById('goldStatus').textContent = statusText;
            document.getElementById('btnGoldStart').disabled = running || !d.connected;
            document.getElementById('btnGoldStop').disabled  = !running;

            const st = d.stats || {};
            document.getElementById('goldOpened').textContent   = st.trades_opened || 0;
            document.getElementById('goldRecycled').textContent = st.recycled       || 0;
            document.getElementById('goldWins').textContent     = st.wins           || 0;
            document.getElementById('goldLosses').textContent   = st.losses         || 0;

            const live = d.live || {};
            const goldTitle = document.getElementById('goldSymbolTitle');
            if (goldTitle) goldTitle.textContent = 'GOLD / XAU';

            // Session badge
            const sb = document.getElementById('goldSessionBadge');
            if (live.in_session) {
                sb.className = 'session-badge-on';
                sb.textContent = '\u25CF ' + (live.session_name || 'Active');
            } else {
                sb.className = 'session-badge-off';
                sb.textContent = live.session_name || 'Off-Hours';
            }

            // ATR + spread
            document.getElementById('goldAtrVal').textContent    = live.atr    != null ? live.atr.toFixed(2)    : '--';
            document.getElementById('goldSpreadVal').textContent = live.spread != null ? live.spread             : '--';

            // Price
            if (live.price && live.price.bid != null) {
                document.getElementById('goldPriceBig').textContent = parseFloat(live.price.bid).toFixed(2);
            }

            // Signal badge
            const sig = document.getElementById('goldSignalBadge');
            sig.textContent = live.signal || 'NONE';
            sig.className   = 'signal-badge signal-' + (live.signal || 'none').toLowerCase();

            // Scores
            document.getElementById('goldBuyScore').textContent  = live.buy_score  || 0;
            document.getElementById('goldSellScore').textContent = live.sell_score || 0;

            // CRT indicator bar — top-down analysis
            const ind = live.indicators || {};

            // HTF bias (H4+H1+M15 simulated on M1)
            const htfEl = document.getElementById('goldIndHtf');
            if (htfEl) {
                const bias = ind.htf_bias, h4 = ind.htf_h4, h1 = ind.htf_h1;
                if (bias === 1)       { htfEl.textContent = '\u25B2 BULL'; htfEl.style.color = '#27ae60'; }
                else if (bias === -1) { htfEl.textContent = '\u25BC BEAR'; htfEl.style.color = '#e74c3c'; }
                else if (h4 != null)  { htfEl.textContent = (h4 ? 'H4\u25B2' : 'H4\u25BC') + (h1 ? ' H1\u25B2' : ' H1\u25BC'); htfEl.style.color = '#ffd700'; }
                else                  { htfEl.textContent = '--'; htfEl.style.color = '#aaa'; }
            }
            // CRT pattern
            const crtEl = document.getElementById('goldIndCrt');
            if (crtEl) {
                const cb = ind.crt_bull, cs = ind.crt_bear;
                if (cb)      { crtEl.textContent = 'BULL'; crtEl.style.color = '#27ae60'; }
                else if (cs) { crtEl.textContent = 'BEAR'; crtEl.style.color = '#e74c3c'; }
                else         { crtEl.textContent = '--';   crtEl.style.color = '#aaa'; }
            }
            // CRT reference candle range
            const refEl = document.getElementById('goldIndRef');
            if (refEl) {
                const rh = ind.crt_ref_high, rl = ind.crt_ref_low;
                if (rh && rl && rh > 0) { refEl.textContent = rl.toFixed(1) + '\u2013' + rh.toFixed(1); refEl.style.color = '#ffd700'; }
                else                    { refEl.textContent = '--'; refEl.style.color = '#aaa'; }
            }
            // CRT sweep low (BUY SL anchor)
            const swlEl = document.getElementById('goldIndSwL');
            if (swlEl) {
                const sl = ind.crt_sw_low;
                if (sl && sl > 0) { swlEl.textContent = sl.toFixed(2); swlEl.style.color = '#27ae60'; }
                else              { swlEl.textContent = '--'; swlEl.style.color = '#aaa'; }
            }
            // CRT sweep high (SELL SL anchor)
            const swhEl = document.getElementById('goldIndSwH');
            if (swhEl) {
                const sh = ind.crt_sw_high;
                if (sh && sh > 0) { swhEl.textContent = sh.toFixed(2); swhEl.style.color = '#e74c3c'; }
                else              { swhEl.textContent = '--'; swhEl.style.color = '#aaa'; }
            }
            // Score
            const scoreEl = document.getElementById('goldIndScore');
            if (scoreEl) {
                const sig = live.signal;
                const sc = (sig === 'BUY') ? (ind.buy_score || 0) : (ind.sell_score || 0);
                scoreEl.textContent = sc + '/7';
                scoreEl.style.color = sc >= 4 ? '#27ae60' : sc >= 2 ? '#ffd700' : '#aaa';
            }
            const rsiEl = document.getElementById('goldIndRsi');
            if (rsiEl && ind.rsi != null) {
                rsiEl.textContent = ind.rsi.toFixed(1);
                rsiEl.style.color = ind.rsi < 35 ? '#27ae60' : ind.rsi > 65 ? '#e74c3c' : '#aaa';
            }
            const bbEl = document.getElementById('goldIndBb');
            if (bbEl && ind.bb_pct != null) {
                bbEl.textContent = (ind.bb_pct * 100).toFixed(0) + '%';
                bbEl.style.color = ind.bb_pct < 0.2 ? '#27ae60' : ind.bb_pct > 0.8 ? '#e74c3c' : '#aaa';
            }
            const atrEl = document.getElementById('goldIndAtr');
            if (atrEl && ind.atr != null) {
                atrEl.textContent = ind.atr.toFixed(2);
                atrEl.style.color = '#aaa';
            }

            // Candle pattern chip
            const pc = document.getElementById('goldPatternChip');
            if (live.pattern) {
                pc.style.display = 'inline';
                pc.textContent   = live.pattern;
                pc.className     = 'pattern-chip ' + (
                    live.pattern_dir === 'buy'  ? 'pattern-bull' :
                    live.pattern_dir === 'sell' ? 'pattern-bear' : 'pattern-doji'
                );
            } else {
                pc.style.display = 'none';
            }

            // Live positions
            const positions = live.positions || [];
            document.getElementById('goldPosCount').textContent = positions.length;
            // Use live_pnl (closed + floating) for a full real-time P&L
            const totalPnl = d.live_pnl != null ? d.live_pnl
                           : positions.reduce((s, p) => s + p.profit, 0);
            const pnlEl = document.getElementById('goldLivePnl');
            pnlEl.textContent = '$' + totalPnl.toFixed(2);
            pnlEl.className   = totalPnl >= 0 ? 'profit-pos' : 'profit-neg';

            // Mini positions
            const mini = document.getElementById('goldPosMini');
            mini.innerHTML = positions.length > 0 ? positions.map(p => `
                <div class="position-row">
                    <span>${p.type.toUpperCase()} ${p.volume} @${p.open_price.toFixed(2)} <span style="color:#e74c3c">LR -$${Number(p.max_loss_reach || 0).toFixed(2)}</span></span>
                    <span class="${p.profit >= 0 ? 'profit-pos' : 'profit-neg'}">$${p.profit.toFixed(2)}</span>
                </div>`).join('') : '<div style="color:#666;font-size:0.9em">No positions</div>';

            // Full positions table
            const pb = document.getElementById('goldPosBody');
            if (positions.length > 0) {
                pb.innerHTML = positions.map(p => {
                    let tpPct = '\u2014';
                    if (p.tp && p.open_price) {
                        const tpD  = p.type === 'buy' ? p.tp - p.open_price : p.open_price - p.tp;
                        const trav = p.type === 'buy' ? p.price_current - p.open_price : p.open_price - p.price_current;
                        if (tpD > 0) tpPct = Math.round(trav / tpD * 100) + '%';
                    }
                    const clr = p.profit >= 0 ? 'profit-pos' : 'profit-neg';
                    const lr = Number(p.max_loss_reach || 0);
                    return `<tr>
                        <td>${p.type.toUpperCase()}</td>
                        <td>${p.volume}</td>
                        <td>${p.open_price.toFixed(2)}</td>
                        <td>${p.price_current.toFixed(2)}</td>
                        <td style="color:#e74c3c">${p.sl ? p.sl.toFixed(2) : '--'}</td>
                        <td class="${clr}">$${p.profit.toFixed(2)}</td>
                        <td style="color:#e74c3c;font-weight:bold">-$${lr.toFixed(2)}</td>
                        <td style="color:#ffd700">${tpPct}</td>
                    </tr>`;
                }).join('');
            } else {
                pb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#888">No gold positions</td></tr>';
            }

            // Log
            const logs = document.getElementById('goldLogs');
            if (d.logs && d.logs.length > 0) {
                logs.innerHTML = d.logs.map(l =>
                    `<div class="log-entry"><span class="log-time">${l.time}</span> <span class="log-${l.level}">[${l.level}]</span> ${l.message}</div>`
                ).join('');
            }

            // Sync config inputs (only when not focused)
            const cfg = d.config || {};
            const fields = [
                ['gCfgLot',     cfg.lot_size,         'float'],
                ['gCfgMaxPos',  cfg.max_positions,    'int'],
                ['gCfgSpread',  cfg.max_spread,       'int'],
                ['gCfgScore',   cfg.confluence_score, 'int'],
                ['gCfgSl',      cfg.sl_atr_mult,      'float'],
                ['gCfgTp',      cfg.tp_atr_mult,      'float'],
                ['gCfgRecycle', cfg.recycle_pct != null ? Math.round(cfg.recycle_pct * 100) : null, 'int'],
            ];
            fields.forEach(([id, val, type]) => {
                const el = document.getElementById(id);
                if (el && document.activeElement !== el && val != null)
                    el.value = type === 'float' ? val : parseInt(val);
            });
            const sesEl = document.getElementById('gCfgSession');
            const patEl = document.getElementById('gCfgPatterns');
            const symEl = document.getElementById('gCfgSymbol');
            if (sesEl && document.activeElement !== sesEl && cfg.session_filter  != null) sesEl.checked = cfg.session_filter;
            if (patEl && document.activeElement !== patEl && cfg.candle_patterns != null) patEl.checked = cfg.candle_patterns;
            if (symEl && document.activeElement !== symEl && cfg.symbol_key) symEl.value = sanitizeNonBtcSymbolKey(cfg.symbol_key);
        }

        function refreshGoldHistory()  { _refreshBotHistory('Gold1M',    'goldHistBody',  'ghTotal', 'ghWins', 'ghLosses', 'ghWinRate', 'ghPnl', 'ghBest', 'ghWorst', 'goldHistCount',  'No closed gold trades yet'); }

        function resetNewDay() {
            if (!confirm(
                'Reset to a New Day?\\n\\n' +
                'This will:\\n' +
                '  \u2022 Clear ALL trade history (all 3 bots)\\n' +
                '  \u2022 Reset daily PnL, goal tracker, and stat counters\\n' +
                '  \u2022 Delete saved session files from disk\\n\\n' +
                'Active bots keep running. MT5 positions are NOT affected.\\n\\n' +
                'Confirm?'
            )) return;
            fetch('/api/reset_new_day', { method: 'POST' })
                .then(r => r.json())
                .then(d => {
                    if (d.success) {
                        alert('\u2705 ' + d.message);
                        refreshAllHistory();
                    } else {
                        alert('Error: ' + (d.error || 'unknown'));
                    }
                })
                .catch(e => alert('Request failed: ' + e));
        }

        function showDeleteHistoryMode() {
            const panel = document.getElementById('deleteHistoryPanel');
            if (panel) panel.style.display = 'block';
        }

        function hideDeleteHistoryMode() {
            const panel = document.getElementById('deleteHistoryPanel');
            if (panel) panel.style.display = 'none';
        }

        function clearHistory(botKey, refreshFn) {
            const labels = {'': '5-Min SMC', 'Gold1M': 'Gold 1-Min', 'GoldDay': 'Gold Day Trade', 'BTC': 'BTC', 'ALL': 'ALL bots'};
            const label = labels[botKey] || botKey;
            if (!confirm('Delete ' + label + ' trade history? This cannot be undone.')) return;
            const url = '/api/clear_history' + (botKey ? '?bot=' + encodeURIComponent(botKey) : '');
            fetch(url, {method:'POST'}).then(r => r.json()).then(d => {
                if (d.success) { 
                    hideDeleteHistoryMode();
                    alert('✓ ' + label + ' history cleared');
                    if (refreshFn) refreshFn(); 
                }
                else alert('Clear failed: ' + (d.error || 'unknown'));
            }).catch(e => alert('Error: ' + e));
        }

        function _refreshBotHistory(botKey, tbodyId, idTotal, idWins, idLosses, idWR, idPnl, idBest, idWorst, idBadge, emptyMsg) {
            fetch('/api/trade_history?bot=' + encodeURIComponent(botKey))
                .then(r => r.json())
                .then(data => {
                    const s = data.summary || {};
                    document.getElementById(idTotal).textContent   = s.total    || 0;
                    document.getElementById(idWins).textContent    = s.wins     || 0;
                    document.getElementById(idLosses).textContent  = s.losses   || 0;
                    document.getElementById(idWR).textContent      = (s.win_rate || 0) + '%';
                    const pnl = s.total_pnl || 0;
                    const pnlEl = document.getElementById(idPnl);
                    pnlEl.textContent = '$' + pnl.toFixed(2);
                    pnlEl.style.color = pnl >= 0 ? '#27ae60' : '#e74c3c';
                    document.getElementById(idBest).textContent  = '$' + (s.best  || 0).toFixed(2);
                    document.getElementById(idWorst).textContent = '$' + (s.worst || 0).toFixed(2);
                    const cnt = s.total || 0;
                    const badge = document.getElementById(idBadge);
                    badge.textContent = cnt; badge.style.display = cnt > 0 ? 'inline' : 'none';
                    const tbody = document.getElementById(tbodyId);
                    const trades = data.trades || [];
                    if (trades.length === 0) {
                        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:#888;padding:16px">${emptyMsg}</td></tr>`;
                        return;
                    }
                    const isG = sym => sym && (sym.includes('XAU') || sym.includes('GOLD'));
                    tbody.innerHTML = trades.map(t => {
                        const dec = isG(t.symbol) ? 2 : 5;
                        const clr = t.result === 'WIN' ? '#27ae60' : (t.result === 'LOSS' ? '#e74c3c' : '#888');
                        const badge = t.result === 'WIN'
                            ? '<span style="background:#27ae60;color:#fff;padding:2px 8px;border-radius:4px;font-size:0.8em;font-weight:bold">WIN</span>'
                            : t.result === 'LOSS'
                            ? '<span style="background:#e74c3c;color:#fff;padding:2px 8px;border-radius:4px;font-size:0.8em;font-weight:bold">LOSS</span>'
                            : '<span style="background:#555;color:#fff;padding:2px 8px;border-radius:4px;font-size:0.8em">BE</span>';
                        return `<tr>
                            <td style="font-size:0.8em;color:#aaa">${(t.close_time||'').slice(11,16)}</td>
                            <td style="font-weight:bold">${t.symbol}</td>
                            <td style="color:${t.type==='buy'?'#27ae60':'#e74c3c'};font-weight:bold">${(t.type||'').toUpperCase()}</td>
                            <td>${t.volume}</td>
                            <td>${(t.open_price||0).toFixed(dec)}</td>
                            <td>${(t.close_price||0).toFixed(dec)}</td>
                            <td style="color:${clr};font-weight:bold">$${(t.profit||0).toFixed(2)}</td>
                            <td>${badge}</td>
                        </tr>`;
                    }).join('');
                }).catch(console.error);
        }

        // ── Per-symbol lot / max-positions config ──
        function toggleSymbolEnabled(sym) {
            const btn = document.getElementById('btn-en-' + sym);
            const isActive = btn && btn.textContent.includes('Active');
            fetch('/api/symbol_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({symbol: sym, enabled: !isActive})
            }).then(r => r.json()).then(d => {
                if (d.success && btn) {
                    const en = d.enabled;
                    btn.textContent    = en ? '\u2713 Active' : '\u23F8 Paused';
                    btn.style.background = en ? '#27ae60' : '#c0392b';
                    const card = document.getElementById('card-' + sym);
                    if (card) card.style.opacity = en ? '1' : '0.45';
                }
            }).catch(console.error);
        }

        function saveSymbolConfig(sym) {
            const lotInput = document.getElementById('lot-' + sym);
            const mpInput  = document.getElementById('mp-'  + sym);
            const lot = parseFloat(lotInput.value) || 0;
            const mp  = parseInt(mpInput.value)    || 1;
            fetch('/api/symbol_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({symbol: sym, lot_size: lot, max_positions: mp})
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    const tick = document.getElementById('cfg-saved-' + sym);
                    tick.style.display = 'inline';
                    setTimeout(() => tick.style.display = 'none', 1800);
                    lotInput.style.borderColor = '#27ae60';
                    mpInput.style.borderColor  = '#27ae60';
                    setTimeout(() => {
                        lotInput.style.borderColor = '#444';
                        mpInput.style.borderColor  = '#444';
                    }, 1800);
                }
            }).catch(console.error);
        }

        // ── Daily goal editor ──
        function openGoalEditor() {
            const txt = document.getElementById('dailyGoal').textContent || '';
            const parts = txt.split('/');
            const current = parseFloat((parts[1] || '20').replace('$','')) || 20;
            document.getElementById('goalInput').value = current;
            document.getElementById('dailyGoalDisplay').style.display = 'none';
            document.getElementById('goalEditor').style.display = 'block';
            setTimeout(() => document.getElementById('goalInput').focus(), 50);
        }
        function closeGoalEditor() {
            document.getElementById('goalEditor').style.display = 'none';
            document.getElementById('dailyGoalDisplay').style.display = 'block';
        }
        function saveGoal() {
            const val = parseFloat(document.getElementById('goalInput').value);
            if (!val || val <= 0) { closeGoalEditor(); return; }
            fetch('/api/daily_goal', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({target: val})
            })
            .then(r => r.json())
            .then(d => {
                closeGoalEditor();
                if (d.daily_goal) {
                    const t = d.daily_goal.target || val;
                    const pnl = d.daily_goal.current_pnl || 0;
                    document.getElementById('dailyGoal').textContent = '$' + pnl.toFixed(2) + '/$' + t.toFixed(0);
                }
            })
            .catch(() => closeGoalEditor());
        }

        function updateLiveHeartbeat() {
            const el = document.getElementById('liveHeartbeat');
            if (!el) return;
            const now = new Date();
            const hh = String(now.getHours()).padStart(2, '0');
            const mm = String(now.getMinutes()).padStart(2, '0');
            const ss = String(now.getSeconds()).padStart(2, '0');
            el.textContent = `LIVE JS: ${hh}:${mm}:${ss}`;
        }

        try {
            var _savedTheme = localStorage.getItem('dashboardTheme');
            if (_savedTheme) applyTheme(_savedTheme); else applyTheme('night');
        } catch(e) {
            applyTheme('night');
        }

        init();
        updateLiveHeartbeat();
        // Restore the last active tab so page refresh doesn't reset tab selection
        try { var _savedTab = localStorage.getItem('activeTab'); if (_savedTab) switchTab(_savedTab); } catch(e) {}
        autoConnect();
        setInterval(updateLiveHeartbeat, 1000);
        setInterval(refresh, 2000);
        setInterval(refreshGold,           2000);
        setInterval(refreshDayTrade,       5000);
        setInterval(refreshBtcStatus,      5000);
        setInterval(refreshAllHistory,     10000);
        setInterval(refreshTradingEnabled, 15000);
        refresh();
        refreshGold();
        refreshDayTrade();
        refreshBtcStatus();
        loadBtcConfig();
        refreshAllHistory();
        refreshTradingEnabled();
    </script>
    <script>
        // Fallback bootstrap: keeps core status UI alive if the main script fails.
        (function () {
            function setText(id, value) {
                var el = document.getElementById(id);
                if (el) el.textContent = value;
            }

            function fallbackSwitchTab(tab) {
                var tabs = ['5min', 'gold', 'daytrade', 'btc', 'history', 'reports', 'lossreach'];
                for (var i = 0; i < tabs.length; i++) {
                    var t = tabs[i];
                    var pane = document.getElementById('tab-' + t);
                    if (pane) pane.style.display = (t === tab) ? '' : 'none';
                }

                var btnByTab = {
                    '5min': 'tabBtn5m',
                    'gold': 'tabBtnGold',
                    'daytrade': 'tabBtnDay',
                    'btc': 'tabBtnBtc',
                    'history': 'tabBtnHist',
                    'reports': 'tabBtnReports',
                    'lossreach': 'tabBtnLoss'
                };

                var keys = Object.keys(btnByTab);
                for (var j = 0; j < keys.length; j++) {
                    var key = keys[j];
                    var btn = document.getElementById(btnByTab[key]);
                    if (btn) btn.classList.toggle('active', key === tab);
                }

                try { localStorage.setItem('activeTab', tab); } catch (e) {}
            }

            function bindFallbackTabs() {
                var pairs = [
                    ['tabBtn5m', '5min'],
                    ['tabBtnGold', 'gold'],
                    ['tabBtnDay', 'daytrade'],
                    ['tabBtnBtc', 'btc'],
                    ['tabBtnHist', 'history'],
                    ['tabBtnReports', 'reports'],
                    ['tabBtnLoss', 'lossreach']
                ];

                for (var i = 0; i < pairs.length; i++) {
                    var id = pairs[i][0];
                    var tab = pairs[i][1];
                    var btn = document.getElementById(id);
                    if (!btn || btn.dataset.fallbackTabBound) continue;
                    btn.dataset.fallbackTabBound = '1';
                    btn.addEventListener('click', function (ev) {
                        var m = this.id === 'tabBtn5m' ? '5min'
                              : this.id === 'tabBtnGold' ? 'gold'
                              : this.id === 'tabBtnDay' ? 'daytrade'
                              : this.id === 'tabBtnBtc' ? 'btc'
                              : this.id === 'tabBtnHist' ? 'history'
                              : this.id === 'tabBtnReports' ? 'reports'
                              : this.id === 'tabBtnLoss' ? 'lossreach'
                              : '5min';
                        fallbackSwitchTab(m);
                        ev.preventDefault();
                    });
                }
            }

            function setDisabled(id, value) {
                var el = document.getElementById(id);
                if (el) el.disabled = !!value;
            }

            function heartbeat() {
                var el = document.getElementById('liveHeartbeat');
                if (!el) return;
                var now = new Date();
                var hh = String(now.getHours()).padStart(2, '0');
                var mm = String(now.getMinutes()).padStart(2, '0');
                var ss = String(now.getSeconds()).padStart(2, '0');
                el.textContent = 'LIVE JS: ' + hh + ':' + mm + ':' + ss;
            }

            function refreshStatus() {
                fetch('/api/status')
                    .then(function (r) { return r.json(); })
                    .then(function (d) {
                        if (!d) return;
                        setText('botStatus', d.running ? 'Running' : (d.connected ? 'Ready' : 'Connecting...'));
                        setDisabled('btnStart', !!d.running || !d.connected);
                        setDisabled('btnStop', !d.running);

                        if (d.account) {
                            setText('accountId', String(d.account.login || 'Not Connected'));
                            setText('balance', '$' + Number(d.account.balance || 0).toFixed(2));
                            setText('equity', '$' + Number(d.account.equity || 0).toFixed(2));
                            setText('profit', '$' + Number(d.account.profit || 0).toFixed(2));
                        }

                        setText('totalPos', String(d.total_positions || 0));
                        setText('trades', String((d.stats && d.stats.trades_opened) || 0));

                        if (!d.connected) {
                            fetch('/api/connect', { method: 'POST' }).catch(function () {});
                        }
                    })
                    .catch(function () {});
            }

            function bindCoreButtons() {
                var c = document.getElementById('btnConnect');
                var s = document.getElementById('btnStart');
                var x = document.getElementById('btnStop');
                if (c && !c.dataset.fallbackBound) {
                    c.dataset.fallbackBound = '1';
                    c.addEventListener('click', function () {
                        fetch('/api/connect', { method: 'POST' }).then(function () { refreshStatus(); }).catch(function () {});
                    });
                }
                if (s && !s.dataset.fallbackBound) {
                    s.dataset.fallbackBound = '1';
                    s.addEventListener('click', function () {
                        fetch('/api/start', { method: 'POST' }).then(function () { refreshStatus(); }).catch(function () {});
                    });
                }
                if (x && !x.dataset.fallbackBound) {
                    x.dataset.fallbackBound = '1';
                    x.addEventListener('click', function () {
                        fetch('/api/stop', { method: 'POST' }).then(function () { refreshStatus(); }).catch(function () {});
                    });
                }
            }

            // Only provide a tab-switch implementation when main JS failed to define one.
            if (typeof window.switchTab !== 'function') {
                window.switchTab = fallbackSwitchTab;
            }

            heartbeat();
            bindCoreButtons();
            bindFallbackTabs();
            refreshStatus();
            try {
                var savedTab = localStorage.getItem('activeTab');
                if (savedTab) fallbackSwitchTab(savedTab);
            } catch (e) {
                fallbackSwitchTab('5min');
            }
            setInterval(heartbeat, 1000);
            setInterval(refreshStatus, 3000);
            setInterval(bindCoreButtons, 5000);
            setInterval(bindFallbackTabs, 5000);
        })();
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    # Build a lightweight initial-state JSON blob so the page renders
    # correctly on FIRST PAINT — no round-trip fetch required.
    import json as _json
    try:
        # Refresh account right before serving the page
        if not bot_state.get('connected'):
            if _mt5_ensure():
                _acct = mt5.account_info()
                if _acct:
                    bot_state['connected'] = True
                    bot_state['account'] = {
                        'login':   int(_acct.login),
                        'server':  str(_acct.server),
                        'balance': float(_acct.balance),
                        'equity':  float(_acct.equity),
                        'profit':  float(_acct.profit),
                    }
        _init_state = _json.dumps({
            'connected': bot_state.get('connected', False),
            'running':   bot_state.get('running', False),
            'account':   bot_state.get('account'),
            'total_positions': int(bot_state.get('total_positions', 0)),
            'stats':     bot_state.get('stats', {}),
            'daily_goal': bot_state.get('daily_goal', {}),
            'smc_live_pnl': 0,
            'symbols':   bot_state.get('symbols', {}),
        }, default=str)
    except Exception as _e:
        import traceback; print(f"index() init state error: {_e}\n{traceback.format_exc()}")
        # Fall back to current bot_state — never show "Connecting..." due to a build error
        try:
            _init_state = _json.dumps({
                'connected': bot_state.get('connected', False),
                'running':   bot_state.get('running', False),
                'account':   bot_state.get('account'),
                'total_positions': int(bot_state.get('total_positions', 0)),
                'stats':     bot_state.get('stats', {}),
                'daily_goal': bot_state.get('daily_goal', {}),
                'smc_live_pnl': 0,
                'symbols':   {},
            }, default=str)
        except Exception:
            _init_state = '{"connected":false,"running":false}'

    # Inject INITIAL_STATE before </body> so JS can paint status immediately
    _inject = (
        f'<script>window.__INITIAL_STATE__={_init_state};\n'
        '// Apply initial state immediately \u2014 no fetch latency\n'
        'try{update(window.__INITIAL_STATE__);}catch(e){}\n'
        '</script>\n'
    )
    html = HTML_TEMPLATE.replace('</body>', _inject + '</body>', 1)
    resp = make_response(html)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/api/status')
def get_status():
    try:
        # ── Always refresh account from MT5 so Balance/Equity are live ──────────
        # NOTE: do NOT call mt5.initialize() here from multiple threads
        # simultaneously — use _mt5_ensure() with its lock if re-init is needed.
        try:
            _acct = mt5.account_info()
            if _acct:
                bot_state['account'] = {
                    'login':   int(_acct.login),
                    'server':  str(_acct.server),
                    'balance': float(_acct.balance),
                    'equity':  float(_acct.equity),
                    'profit':  float(_acct.profit),
                }
                bot_state['connected'] = True
            elif not bot_state.get('connected'):
                # account_info() returned None and we're not connected yet—
                # try re-initializing MT5 (e.g. after cold start race condition)
                if _mt5_ensure():
                    _acct2 = mt5.account_info()
                    if _acct2:
                        bot_state['account'] = {
                            'login':   int(_acct2.login),
                            'server':  str(_acct2.server),
                            'balance': float(_acct2.balance),
                            'equity':  float(_acct2.equity),
                            'profit':  float(_acct2.profit),
                        }
                        bot_state['connected'] = True
        except Exception:
            pass

        # ── Daily goal: always compute from live equity (covers all 3 bots) ─────
        if DAILY_GOAL_CONFIG.get('enabled', True) and bot_state.get('connected'):
            try:
                acct_eq  = (bot_state['account'] or {}).get('equity')
                acct_bal = (bot_state['account'] or {}).get('balance')
                if acct_eq is not None and acct_bal is not None:
                    today = datetime.now().date()

                    # Initialise start_balance whenever it is missing or is a new day.
                    # Use  balance − today's closed P&L  so the baseline = midnight balance.
                    is_new_day = daily_goal_state.get('start_date') != today
                    if daily_goal_state['start_balance'] is None or is_new_day:
                        closed_today = sum(t.get('profit', 0)
                                          for t in bot_state['trade_history'])
                        daily_goal_state['start_balance'] = float(acct_bal) - closed_today
                        daily_goal_state['start_date']    = today
                        if is_new_day:
                            # New calendar day — reset goal flag
                            daily_goal_state['goal_reached'] = False
                            bot_state['daily_goal']['goal_reached'] = False
                        _save_daily_goal_to_disk()

                    # daily_pnl = closed P&L + floating P&L = equity − midnight_balance
                    daily_pnl = float(acct_eq) - daily_goal_state['start_balance']
                    target    = float(bot_state['daily_goal'].get(
                                    'target', DAILY_GOAL_CONFIG.get('daily_target', 20.0)))
                    bot_state['daily_goal']['current_pnl']  = round(daily_pnl, 2)
                    bot_state['daily_goal']['progress_pct'] = round(
                        min(100, max(0, (daily_pnl / target) * 100)) if target else 0, 1)

                    # Goal reached?
                    if daily_pnl >= target and not daily_goal_state['goal_reached']:
                        daily_goal_state['goal_reached']        = True
                        bot_state['daily_goal']['goal_reached'] = True
                        add_log(f"DAILY GOAL REACHED! P&L: ${daily_pnl:.2f}", "SUCCESS")
                        add_log("Closing profitable positions and locking losers at breakeven...", "TRADE")
                        try:
                            _move_positions_to_breakeven()
                        except Exception as _ge:
                            add_log(f"Goal action error: {_ge}", "WARNING")
                        _save_daily_goal_to_disk()

                    # While goal is active: re-apply protection every status poll (every 2s)
                    # so any position opened by a still-running bot is immediately handled
                    elif daily_goal_state['goal_reached']:
                        try:
                            _move_positions_to_breakeven()
                        except Exception:
                            pass
            except Exception:
                pass

        # Compute live SMC floating P&L from open positions
        try:
            _open_pos = mt5.positions_get() or []
            _smc_floating = sum(float(p.profit) for p in _open_pos if getattr(p, 'magic', 0) == 234000)
            _smc_closed   = sum(t.get('profit', 0) for t in bot_state['trade_history']
                                if t.get('bot', '') not in ('Gold1M', 'GoldDay'))
            smc_live_pnl = round(_smc_floating + _smc_closed, 2)
        except Exception:
            smc_live_pnl = round(bot_state.get('stats', {}).get('session_pnl', 0), 2)

        open_loss_metrics = _open_trade_loss_metrics()

        # Ensure all required fields exist
        state = {
            'connected': bot_state.get('connected', False),
            'running': bot_state.get('running', False),
            'account': bot_state.get('account'),
            'symbols': bot_state.get('symbols', {}),
            'logs': bot_state.get('logs', []),
            'stats': bot_state.get('stats', {'signals_generated': 0, 'trades_opened': 0, 'session_pnl': 0}),
            'total_positions': int(bot_state.get('total_positions', 0)),
            'strategy_name': bot_state.get('strategy_name', ''),
            'daily_goal': bot_state.get('daily_goal', {'enabled': False, 'current_pnl': 0, 'target': 20, 'progress_pct': 0, 'goal_reached': False}),
            'smc_live_pnl': smc_live_pnl,
            'open_loss_metrics': open_loss_metrics,
            'trading_enabled': trading_enabled,
            'emergency_pause': bot_state.get('emergency_pause', {}),
            'watchdogs': _watchdog_status_snapshot(),
        }
        return jsonify(state)
    except Exception as e:
        import traceback
        print(f"Status error: {e}\n{traceback.format_exc()}")
        # Preserve the actual connected/account state — never show "Connecting..." due to
        # a transient internal error (e.g. daily-goal calc or MT5 positions blip).
        return jsonify({
            'connected': bot_state.get('connected', False),
            'running':   bot_state.get('running', False),
            'account':   bot_state.get('account'),
            'symbols':   bot_state.get('symbols', {}),
            'logs':      bot_state.get('logs', []),
            'stats':     bot_state.get('stats', {'signals_generated': 0, 'trades_opened': 0, 'session_pnl': 0}),
            'total_positions': int(bot_state.get('total_positions', 0)),
            'daily_goal': bot_state.get('daily_goal', {'enabled': False, 'current_pnl': 0, 'target': 20, 'progress_pct': 0, 'goal_reached': False}),
            'smc_live_pnl': 0,
            'open_loss_metrics': _open_trade_loss_metrics(),
            'emergency_pause': bot_state.get('emergency_pause', {}),
            'error': str(e),
        })


@app.route('/api/connect', methods=['POST'])
def connect():
    global bot_state
    
    try:
        if not _mt5_ensure():
            try:
                last = mt5.last_error()
                err = f"Failed to initialize MT5 ({last})"
            except Exception:
                err = 'Failed to initialize MT5'
            return jsonify({'success': False, 'error': err})
        
        account = mt5.account_info()
        if account:
            bot_state['connected'] = True
            bot_state['account'] = {
                'login': int(account.login),
                'server': str(account.server),
                'balance': float(account.balance),
                'equity': float(account.equity),
                'profit': float(account.profit),
            }
            
            for sym_key, sym_config in SYMBOLS_CONFIG.items():
                symbol = _resolve_symbol(sym_config.get('symbol', sym_key))
                if symbol:
                    mt5.symbol_select(symbol, True)

            # Load today's closed trades from MT5 immediately on connect
            try:
                _validate_account_data(account.login)   # clears stale data if account changed
                _load_today_history_from_mt5()          # fill in any gaps from MT5
                _load_daily_goal_from_disk(current_login=account.login)
            except Exception:
                pass

            add_log(f"Connected - Account: {account.login} | History loaded: {len(bot_state['trade_history'])} trades", "SUCCESS")
            return jsonify({'success': True, 'history_count': len(bot_state['trade_history'])})
        else:
            try:
                last = mt5.last_error()
                err = f"No account info ({last})"
            except Exception:
                err = 'No account info'
            return jsonify({'success': False, 'error': err})
    except Exception as e:
        try:
            last = mt5.last_error()
            return jsonify({'success': False, 'error': f"{e} | MT5 last_error={last}"})
        except Exception:
            return jsonify({'success': False, 'error': str(e)})


@app.route('/api/start', methods=['POST'])
def start_bot():
    global bot_thread, bot_state
    
    if not bot_state['connected']:
        return jsonify({'success': False, 'error': 'Not connected'})
    if bot_state.get('emergency_pause', {}).get('active'):
        return jsonify({'success': False, 'error': 'Emergency pause is active. Release it first.'}), 400
    
    if bot_state['running']:
        return jsonify({'success': True})  # Already running, return success
    
    bot_state['running'] = True
    bot_thread = threading.Thread(target=run_bot_thread, daemon=True)
    bot_thread.start()
    
    return jsonify({'success': True})


@app.route('/api/stop', methods=['POST'])
def stop_bot():
    global bot_state
    bot_state['running'] = False
    add_log("Stopping bot...")
    return jsonify({'success': True})


@app.route('/api/daily_goal', methods=['POST'])
def update_daily_goal():
    """Update daily goal settings (target, action, reset)"""
    global daily_goal_state, bot_state
    
    data = request.get_json() or {}
    before_cfg = {
        'target': bot_state.get('daily_goal', {}).get('target'),
        'action': bot_state.get('daily_goal', {}).get('action'),
    }
    
    if 'target' in data:
        try:
            new_target = float(data['target'])
            if new_target > 0:
                DAILY_GOAL_CONFIG['daily_target'] = new_target
                bot_state['daily_goal']['target'] = new_target
                # If goal was already reached at old target, re‑evaluate
                if daily_goal_state.get('closed_profit', 0) < new_target:
                    daily_goal_state['goal_reached'] = False
                    bot_state['daily_goal']['goal_reached'] = False
                _save_daily_goal_to_disk()
                add_log(f"Daily profit target updated to ${new_target:.2f}", "INFO")
        except (ValueError, TypeError):
            pass

    if 'action' in data:
        DAILY_GOAL_CONFIG['action_on_goal'] = data['action']
        bot_state['daily_goal']['action'] = data['action']
        add_log(f"Daily goal action changed to: {data['action']}", "INFO")
    
    if 'reset' in data and data['reset']:
        daily_goal_state['goal_reached'] = False
        bot_state['daily_goal']['goal_reached'] = False
        account = mt5.account_info()
        if account:
            # Subtract today's already-closed P&L so baseline = midnight balance
            closed_today = sum(t.get('profit', 0) for t in bot_state['trade_history'])
            daily_goal_state['start_balance'] = float(account.balance) - closed_today
            daily_goal_state['start_date'] = datetime.now().date()
        _save_daily_goal_to_disk()
        add_log("Daily tracker manually reset", "INFO")
    
    after_cfg = {
        'target': bot_state.get('daily_goal', {}).get('target'),
        'action': bot_state.get('daily_goal', {}).get('action'),
    }
    _record_config_change('daily_goal', before_cfg, after_cfg)
    return jsonify({'success': True, 'daily_goal': bot_state['daily_goal']})


@app.route('/api/symbol_config', methods=['POST'])
def update_symbol_config():
    """Update per-symbol lot size, max positions, and enabled flag."""
    global bot_state
    data = request.get_json() or {}
    sym = data.get('symbol', '').upper()
    if sym not in bot_state['symbols']:
        return jsonify({'success': False, 'error': f'Unknown symbol: {sym}'}), 400

    before_cfg = {
        'enabled': bot_state['symbols'][sym].get('enabled', True),
        'lot_size': bot_state['symbols'][sym].get('lot_size', 0.0),
        'max_positions': bot_state['symbols'][sym].get('max_positions', 1),
    }

    if 'enabled' in data:
        bot_state['symbols'][sym]['enabled'] = bool(data['enabled'])
        state_str = 'ENABLED' if bot_state['symbols'][sym]['enabled'] else 'PAUSED'
        add_log(f"{sym} trading {state_str} via dashboard toggle", 'INFO')

    if 'lot_size' in data:
        try:
            lot = float(data['lot_size'])
            bot_state['symbols'][sym]['lot_size'] = max(0.0, round(lot, 2))
        except (ValueError, TypeError):
            pass

    if 'max_positions' in data:
        try:
            mp = int(data['max_positions'])
            bot_state['symbols'][sym]['max_positions'] = max(1, min(mp, 10))
        except (ValueError, TypeError):
            pass

    ls  = bot_state['symbols'][sym]['lot_size']
    mp  = bot_state['symbols'][sym]['max_positions']
    en  = bot_state['symbols'][sym]['enabled']
    if 'lot_size' in data or 'max_positions' in data:
        lot_label = f"{ls:.2f} lots" if ls > 0 else "auto"
        add_log(f"{sym} config: lot={lot_label}, max_pos={mp}", "INFO")
    _record_config_change(f'symbol:{sym}', before_cfg, {
        'enabled': en,
        'lot_size': ls,
        'max_positions': mp,
    })
    return jsonify({'success': True, 'symbol': sym,
                    'lot_size': ls, 'max_positions': mp, 'enabled': en})


@app.route('/api/close_all', methods=['POST'])
def close_all_positions():
    """Manually close all positions"""
    positions = mt5.positions_get()
    if not positions:
        return jsonify({'success': True, 'closed': 0})
    
    closed = 0
    for pos in positions:
        if close_position(pos):
            closed += 1
    
    add_log(f"Manually closed {closed} positions", "TRADE")
    return jsonify({'success': True, 'closed': closed})


@app.route('/api/trade_history')
def get_trade_history():
    """Return completed trade history filtered by bot.
    ?bot=5min     → only 5-min SMC bot trades (bot='')
    ?bot=Gold1M   → only Gold 1-Min trades
    ?bot=GoldDay  → only Gold Day Trade trades
    (no param)    → all trades (for debugging)
    """
    bot_filter = request.args.get('bot', '').strip()
    history = bot_state.get('trade_history', [])
    def _is_btc_trade(t):
        sym = str(t.get('symbol', '')).upper()
        return ('BTC' in sym) or ('XBT' in sym)
    if bot_filter == '5min':
        # Exact match: only 5-min SMC bot records (bot field is '' or missing)
        history = [t for t in history if t.get('bot', '') == '' and not _is_btc_trade(t)]
    elif bot_filter.upper() == 'BTC':
        history = [t for t in history if _is_btc_trade(t)]
    elif bot_filter:
        history = [t for t in history if t.get('bot', '') == bot_filter and not _is_btc_trade(t)]
    else:
        # No filter — return everything (kept for debugging / future use)
        pass
    total   = len(history)
    wins    = sum(1 for t in history if t.get('result') == 'WIN')
    losses  = sum(1 for t in history if t.get('result') == 'LOSS')
    pnl     = sum(t.get('profit', 0) for t in history)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0
    return jsonify({
        'trades':   history,
        'summary': {
            'total':    total,
            'wins':     wins,
            'losses':   losses,
            'breakeven': total - wins - losses,
            'win_rate': win_rate,
            'total_pnl': round(pnl, 2),
            'best':  max((t.get('profit', 0) for t in history), default=0),
            'worst': min((t.get('profit', 0) for t in history), default=0),
        }
    })


@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    """Clear trade history. Optional ?bot= to clear only one bot's records.
    bot values: 'Gold1M', 'GoldDay', or omit/empty for 5-min bot only.
    Pass bot='ALL' to wipe everything.
    """
    global bot_state
    bot_filter = request.args.get('bot', '').strip()
    def _is_btc_trade(t):
        sym = str(t.get('symbol', '')).upper()
        return ('BTC' in sym) or ('XBT' in sym)
    if bot_filter == 'ALL':
        bot_state['trade_history'].clear()
        # Also clear archive and history files
        try:
            if os.path.exists(_HISTORY_FILE):
                os.remove(_HISTORY_FILE)
            if os.path.exists(_ARCHIVE_FILE):
                os.remove(_ARCHIVE_FILE)
        except Exception:
            pass
    elif bot_filter in ('Gold1M', 'GoldDay'):
        bot_state['trade_history'] = [
            t for t in bot_state['trade_history']
            if (t.get('bot', '') != bot_filter) or _is_btc_trade(t)
        ]
        _save_history_to_disk()
    elif bot_filter.upper() == 'BTC':
        bot_state['trade_history'] = [
            t for t in bot_state['trade_history']
            if not _is_btc_trade(t)
        ]
        _save_history_to_disk()
    else:
        # Default: clear non-BTC 5-min bot records only
        bot_state['trade_history'] = [
            t for t in bot_state['trade_history']
            if (t.get('bot', '') in ('Gold1M', 'GoldDay')) or _is_btc_trade(t)
        ]
        _save_history_to_disk()
    return jsonify({'success': True, 'cleared': bot_filter or '5min'})


def _btc_status_payload():
    symbols = bot_state.get('symbols', {})
    btc_runtime = symbols.get('BTCUSD', {})
    btc_price = btc_runtime.get('price') or {}
    btc_positions = btc_runtime.get('positions') or []
    m1_live = gold_state.get('live', {}) or {}
    h4_live = daytrade_state.get('live', {}) or {}
    btc_enabled = bool(symbols.get('BTCUSD', {}).get('enabled', False))
    btc_mode = bot_state.get('btc_mode', {})
    m1_active = bool(btc_mode.get('m1', False))
    m5_active = bool(btc_mode.get('m5', False))
    h4_active = bool(btc_mode.get('h4', False))
    btc_symbol_cfg = SYMBOLS_CONFIG.get('BTCUSD', {})
    btc_symbol_name = btc_symbol_cfg.get('symbol', 'BTCUSD')
    btc_tick_price = {}
    if bot_state.get('connected'):
        pinfo, _, _ = get_mt5_data(btc_symbol_name)
        if pinfo:
            btc_tick_price = pinfo

    m1_positions = [p for p in (m1_live.get('positions') or []) if _is_btc_symbol_name(p.get('symbol', ''))]
    h4_positions = [p for p in (h4_live.get('positions') or []) if _is_btc_symbol_name(p.get('symbol', ''))]

    m1_price_obj = m1_live.get('price') or {}
    h4_price_obj = h4_live.get('price') or {}
    m1_live_is_btc = _is_btc_symbol_name(m1_price_obj.get('symbol', ''))
    h4_live_is_btc = _is_btc_symbol_name(h4_price_obj.get('symbol', ''))

    m1_bid = float((m1_price_obj.get('bid') if m1_live_is_btc else btc_tick_price.get('bid', 0)) or 0)
    m1_spread = int((m1_price_obj.get('spread_points') if m1_live_is_btc else btc_tick_price.get('spread_points', 0)) or 0)
    h4_bid = float((h4_price_obj.get('bid') if h4_live_is_btc else btc_tick_price.get('bid', 0)) or 0)
    h4_spread = int((h4_price_obj.get('spread_points') if h4_live_is_btc else btc_tick_price.get('spread_points', 0)) or 0)

    m1_buy = int(m1_live.get('buy_score', 0) or 0) if (m1_active and m1_live_is_btc) else 0
    m1_sell = int(m1_live.get('sell_score', 0) or 0) if (m1_active and m1_live_is_btc) else 0
    h4_buy = int(h4_live.get('buy_score', 0) or 0) if (h4_active and h4_live_is_btc) else 0
    h4_sell = int(h4_live.get('sell_score', 0) or 0) if (h4_active and h4_live_is_btc) else 0

    return {
        'connected': bool(bot_state.get('connected', False)),
        'trading_enabled': bool(trading_enabled),
        'mode_lock': {
            'm1': m1_active,
            'm5': m5_active,
            'h4': h4_active,
        },
        'm1': {
            'running': m1_active,
            'engine_running': bool(gold_state.get('running', False)),
            'symbol_key': 'BTCUSD',
            'session_filter': False,
            'signal': str((m1_live.get('signal', 'NONE') if (m1_active and m1_live_is_btc) else 'NONE') or 'NONE'),
            'price_bid': m1_bid,
            'spread_points': m1_spread,
            'buy_score': m1_buy,
            'sell_score': m1_sell,
            'signal_strength': int(max(m1_buy, m1_sell)),
            'analysis': (m1_live.get('indicators') or {}) if (m1_active and m1_live_is_btc) else {},
            'positions': m1_positions if m1_active else [],
            'lot_size': float(gold_state.get('config', {}).get('lot_size', 0) or 0),
            'max_positions': int(gold_state.get('config', {}).get('max_positions', 3) or 3),
        },
        'm5': {
            'running': m5_active,
            'engine_running': bool(bot_state.get('running', False)),
            'independent_mode': bool(m5_active),
            'btc_enabled': bool(btc_enabled),
            'signal': str(btc_runtime.get('signal', 'NONE') or 'NONE'),
            'price_bid': float(btc_price.get('bid', 0) or 0),
            'signal_strength': int(btc_runtime.get('signal_strength', 0) or 0),
            'buy_score': int(btc_runtime.get('buy_score', 0) or 0),
            'sell_score': int(btc_runtime.get('sell_score', 0) or 0),
            'analysis': btc_runtime.get('analysis') or {},
            'positions_list': btc_positions,
            'min_signal_strength': int(SYMBOLS_CONFIG.get('BTCUSD', {}).get('min_signal_strength', 0) or 0),
            'spread_points': int(btc_price.get('spread_points', btc_runtime.get('spread', 0)) or 0),
            'max_spread': int(SYMBOLS_CONFIG.get('BTCUSD', {}).get('max_spread', 0) or 0),
            'positions': int(len(btc_positions)),
            'lot_size': float(btc_runtime.get('lot_size', 0) or 0),
            'max_positions': int(btc_runtime.get('max_positions', 1) or 1),
        },
        'h4': {
            'running': h4_active,
            'engine_running': bool(daytrade_state.get('running', False)),
            'symbol_key': 'BTCUSD',
            'session_filter': False,
            'signal': str((h4_live.get('signal', 'NONE') if (h4_active and h4_live_is_btc) else 'NONE') or 'NONE'),
            'price_bid': h4_bid,
            'spread_points': h4_spread,
            'buy_score': h4_buy,
            'sell_score': h4_sell,
            'signal_strength': int(max(h4_buy, h4_sell)),
            'analysis': (h4_live.get('indicators') or {}) if (h4_active and h4_live_is_btc) else {},
            'positions': h4_positions if h4_active else [],
            'lot_size': float(daytrade_state.get('config', {}).get('lot_size', 0) or 0),
            'max_positions': int(daytrade_state.get('config', {}).get('max_positions', 2) or 2),
        },
    }


@app.route('/api/btc/status', methods=['GET'])
def api_btc_status():
    return jsonify(_btc_status_payload())


@app.route('/api/btc/config', methods=['GET', 'POST'])
def api_btc_config():
    def _coerce_float(v, lo, hi, default):
        try:
            x = float(v)
            return max(lo, min(x, hi))
        except (ValueError, TypeError):
            return default

    def _coerce_int(v, lo, hi, default):
        try:
            x = int(v)
            return max(lo, min(x, hi))
        except (ValueError, TypeError):
            return default

    btc_symbol_cfg = SYMBOLS_CONFIG.get('BTCUSD', {})
    btc_runtime = bot_state.get('symbols', {}).get('BTCUSD', {})

    if request.method == 'POST':
        before_cfg = {
            'm1': copy.deepcopy(gold_state.get('config', {})),
            'h4': copy.deepcopy(daytrade_state.get('config', {})),
            'm5': {
                'enabled': bot_state['symbols']['BTCUSD'].get('enabled', True),
                'lot_size': bot_state['symbols']['BTCUSD'].get('lot_size', 0),
                'max_positions': bot_state['symbols']['BTCUSD'].get('max_positions', 1),
                'max_spread': SYMBOLS_CONFIG.get('BTCUSD', {}).get('max_spread', 120),
                'min_signal_strength': SYMBOLS_CONFIG.get('BTCUSD', {}).get('min_signal_strength', 0),
                'lot_size_multiplier': SYMBOLS_CONFIG.get('BTCUSD', {}).get('lot_size_multiplier', 1.0),
            },
        }
        data = request.get_json() or {}
        m1 = data.get('m1', {}) or {}
        h4 = data.get('h4', {}) or {}
        m5 = data.get('m5', {}) or {}

        # 1m BTC config
        gcfg = gold_state.get('config', {})
        if bot_state.get('btc_mode', {}).get('m1', False):
            gcfg['symbol_key'] = 'BTCUSD'
            gcfg['session_filter'] = False
        if 'lot_size' in m1:
            gcfg['lot_size'] = round(_coerce_float(m1.get('lot_size'), 0.0, 100.0, gcfg.get('lot_size', 0.0)), 2)
        if 'max_positions' in m1:
            gcfg['max_positions'] = _coerce_int(m1.get('max_positions'), 1, 10, gcfg.get('max_positions', 3))
        if 'max_spread' in m1:
            gcfg['max_spread'] = _coerce_int(m1.get('max_spread'), 5, 2000, gcfg.get('max_spread', 80))
        if 'confluence_score' in m1:
            gcfg['confluence_score'] = _coerce_int(m1.get('confluence_score'), 1, 25, gcfg.get('confluence_score', 4))
        if 'sl_atr_mult' in m1:
            gcfg['sl_atr_mult'] = round(_coerce_float(m1.get('sl_atr_mult'), 0.1, 10.0, gcfg.get('sl_atr_mult', 1.5)), 2)
        if 'tp_atr_mult' in m1:
            gcfg['tp_atr_mult'] = round(_coerce_float(m1.get('tp_atr_mult'), 0.1, 10.0, gcfg.get('tp_atr_mult', 3.0)), 2)

        # 4h BTC config
        dcfg = daytrade_state.get('config', {})
        if bot_state.get('btc_mode', {}).get('h4', False):
            dcfg['symbol_key'] = 'BTCUSD'
            dcfg['session_filter'] = False
        if 'lot_size' in h4:
            dcfg['lot_size'] = round(_coerce_float(h4.get('lot_size'), 0.0, 100.0, dcfg.get('lot_size', 0.0)), 2)
        if 'max_positions' in h4:
            dcfg['max_positions'] = _coerce_int(h4.get('max_positions'), 1, 10, dcfg.get('max_positions', 2))
        if 'max_spread' in h4:
            dcfg['max_spread'] = _coerce_int(h4.get('max_spread'), 5, 2000, dcfg.get('max_spread', 80))
        if 'confluence_score' in h4:
            dcfg['confluence_score'] = _coerce_int(h4.get('confluence_score'), 1, 25, dcfg.get('confluence_score', 8))
        if 'sl_atr_mult' in h4:
            dcfg['sl_atr_mult'] = round(_coerce_float(h4.get('sl_atr_mult'), 0.1, 10.0, dcfg.get('sl_atr_mult', 1.5)), 2)
        if 'tp_atr_mult' in h4:
            dcfg['tp_atr_mult'] = round(_coerce_float(h4.get('tp_atr_mult'), 0.1, 10.0, dcfg.get('tp_atr_mult', 3.0)), 2)

        # 5m BTC config
        if 'enabled' in m5:
            bot_state['symbols']['BTCUSD']['enabled'] = bool(m5.get('enabled'))
        if 'lot_size' in m5:
            bot_state['symbols']['BTCUSD']['lot_size'] = round(_coerce_float(m5.get('lot_size'), 0.0, 100.0, bot_state['symbols']['BTCUSD'].get('lot_size', 0.0)), 2)
        if 'max_positions' in m5:
            bot_state['symbols']['BTCUSD']['max_positions'] = _coerce_int(m5.get('max_positions'), 1, 10, bot_state['symbols']['BTCUSD'].get('max_positions', 1))
        if 'max_spread' in m5:
            SYMBOLS_CONFIG['BTCUSD']['max_spread'] = _coerce_int(m5.get('max_spread'), 5, 2000, btc_symbol_cfg.get('max_spread', 120))
        if 'min_signal_strength' in m5:
            SYMBOLS_CONFIG['BTCUSD']['min_signal_strength'] = _coerce_int(m5.get('min_signal_strength'), 0, 100, btc_symbol_cfg.get('min_signal_strength', 0))
        if 'lot_size_multiplier' in m5:
            SYMBOLS_CONFIG['BTCUSD']['lot_size_multiplier'] = round(_coerce_float(m5.get('lot_size_multiplier'), 0.1, 10.0, btc_symbol_cfg.get('lot_size_multiplier', 1.0)), 2)

        _record_config_change('btc_config', before_cfg, {
            'm1': copy.deepcopy(gold_state.get('config', {})),
            'h4': copy.deepcopy(daytrade_state.get('config', {})),
            'm5': {
                'enabled': bot_state['symbols']['BTCUSD'].get('enabled', True),
                'lot_size': bot_state['symbols']['BTCUSD'].get('lot_size', 0),
                'max_positions': bot_state['symbols']['BTCUSD'].get('max_positions', 1),
                'max_spread': SYMBOLS_CONFIG.get('BTCUSD', {}).get('max_spread', 120),
                'min_signal_strength': SYMBOLS_CONFIG.get('BTCUSD', {}).get('min_signal_strength', 0),
                'lot_size_multiplier': SYMBOLS_CONFIG.get('BTCUSD', {}).get('lot_size_multiplier', 1.0),
            },
        })

    # Always return the merged config state
    return jsonify({
        'm1': {
            'symbol_key': 'BTCUSD',
            'session_filter': False,
            'lot_size': gold_state.get('config', {}).get('lot_size', 0),
            'max_positions': gold_state.get('config', {}).get('max_positions', 3),
            'max_spread': gold_state.get('config', {}).get('max_spread', 80),
            'confluence_score': gold_state.get('config', {}).get('confluence_score', 4),
            'sl_atr_mult': gold_state.get('config', {}).get('sl_atr_mult', 1.5),
            'tp_atr_mult': gold_state.get('config', {}).get('tp_atr_mult', 3.0),
        },
        'm5': {
            'enabled': bool(bot_state.get('symbols', {}).get('BTCUSD', {}).get('enabled', True)),
            'lot_size': bot_state.get('symbols', {}).get('BTCUSD', {}).get('lot_size', 0),
            'max_positions': bot_state.get('symbols', {}).get('BTCUSD', {}).get('max_positions', 1),
            'max_spread': SYMBOLS_CONFIG.get('BTCUSD', {}).get('max_spread', 120),
            'min_signal_strength': SYMBOLS_CONFIG.get('BTCUSD', {}).get('min_signal_strength', 0),
            'lot_size_multiplier': SYMBOLS_CONFIG.get('BTCUSD', {}).get('lot_size_multiplier', 1.0),
        },
        'h4': {
            'symbol_key': 'BTCUSD',
            'session_filter': False,
            'lot_size': daytrade_state.get('config', {}).get('lot_size', 0),
            'max_positions': daytrade_state.get('config', {}).get('max_positions', 2),
            'max_spread': daytrade_state.get('config', {}).get('max_spread', 80),
            'confluence_score': daytrade_state.get('config', {}).get('confluence_score', 8),
            'sl_atr_mult': daytrade_state.get('config', {}).get('sl_atr_mult', 1.5),
            'tp_atr_mult': daytrade_state.get('config', {}).get('tp_atr_mult', 3.0),
        },
    })


@app.route('/api/btc/control', methods=['POST'])
def api_btc_control():
    global bot_thread, gold_thread, daytrade_thread
    data = request.get_json() or {}
    mode = str(data.get('mode', '')).lower().strip()
    start = bool(data.get('start', True))

    if mode not in ('1m', '5m', '4h'):
        return jsonify({'success': False, 'error': 'mode must be one of: 1m, 5m, 4h'}), 400

    if not bot_state.get('connected'):
        return jsonify({'success': False, 'error': 'Not connected to MT5'}), 400
    if start and bot_state.get('emergency_pause', {}).get('active'):
        return jsonify({'success': False, 'error': 'Emergency pause is active. Release it first.'}), 400

    if mode == '1m':
        if start:
            if not bot_state['btc_mode'].get('m1', False):
                bot_state['btc_backup']['m1'] = {
                    'running': bool(gold_state.get('running', False)),
                    'manual_running': bool(gold_state.get('manual_running', False)),
                    'config': copy.deepcopy(gold_state.get('config', {})),
                }
            bot_state['btc_mode']['m1'] = True
            gold_state['config']['symbol_key'] = 'BTCUSD'
            gold_state['config']['session_filter'] = False
            if not gold_state['running']:
                gold_state['running'] = True
                gold_state['manual_running'] = False
                gold_state['stop_reason'] = ''
                gold_thread = threading.Thread(target=run_gold_bot_thread, daemon=True)
                gold_thread.start()
                add_log('BTC standalone 1m started', 'SUCCESS')
        else:
            bot_state['btc_mode']['m1'] = False
            snap = bot_state.get('btc_backup', {}).get('m1')
            if isinstance(snap, dict):
                was_manual = bool(snap.get('manual_running', False))
                was_running = bool(snap.get('running', False))
                gold_state['config'] = copy.deepcopy(snap.get('config', gold_state.get('config', {})))
                gold_state['manual_running'] = was_manual
                if was_manual:
                    gold_state['running'] = was_running
                    gold_state['stop_reason'] = ''
                else:
                    gold_state['running'] = False
                    gold_state['stop_reason'] = 'Stopped from BTC standalone tab.'
            else:
                gold_state['running'] = False
                gold_state['manual_running'] = False
                gold_state['stop_reason'] = 'Stopped from BTC standalone tab.'
            if not gold_state.get('manual_running', False):
                gold_state['config']['symbol_key'] = 'GOLD'
                gold_state['config']['session_filter'] = True
            bot_state['btc_backup']['m1'] = None
            add_log('BTC standalone 1m stopped', 'INFO')

    elif mode == '4h':
        if start:
            if not bot_state['btc_mode'].get('h4', False):
                bot_state['btc_backup']['h4'] = {
                    'running': bool(daytrade_state.get('running', False)),
                    'manual_running': bool(daytrade_state.get('manual_running', False)),
                    'config': copy.deepcopy(daytrade_state.get('config', {})),
                }
            bot_state['btc_mode']['h4'] = True
            daytrade_state['config']['symbol_key'] = 'BTCUSD'
            daytrade_state['config']['session_filter'] = False
            if not daytrade_state['running']:
                daytrade_state['running'] = True
                daytrade_state['manual_running'] = False
                daytrade_state['stop_reason'] = ''
                daytrade_thread = threading.Thread(target=run_daytrade_bot_thread, daemon=True)
                daytrade_thread.start()
                add_log('BTC standalone 4h started', 'SUCCESS')
        else:
            bot_state['btc_mode']['h4'] = False
            snap = bot_state.get('btc_backup', {}).get('h4')
            if isinstance(snap, dict):
                was_manual = bool(snap.get('manual_running', False))
                was_running = bool(snap.get('running', False))
                daytrade_state['config'] = copy.deepcopy(snap.get('config', daytrade_state.get('config', {})))
                daytrade_state['manual_running'] = was_manual
                if was_manual:
                    daytrade_state['running'] = was_running
                    daytrade_state['stop_reason'] = ''
                else:
                    daytrade_state['running'] = False
                    daytrade_state['stop_reason'] = 'Stopped from BTC standalone tab.'
            else:
                daytrade_state['running'] = False
                daytrade_state['manual_running'] = False
                daytrade_state['stop_reason'] = 'Stopped from BTC standalone tab.'
            if not daytrade_state.get('manual_running', False):
                daytrade_state['config']['symbol_key'] = 'GOLD'
                daytrade_state['config']['session_filter'] = True
            bot_state['btc_backup']['h4'] = None
            add_log('BTC standalone 4h stopped', 'INFO')

    elif mode == '5m':
        if start:
            if not bot_state['btc_mode'].get('m5', False):
                bot_state['btc_backup']['m5'] = {
                    'running': bool(bot_state.get('running', False)),
                    'btc_max_spread': float(SYMBOLS_CONFIG.get('BTCUSD', {}).get('max_spread', 0) or 0),
                    'btc_min_signal_strength': int(SYMBOLS_CONFIG.get('BTCUSD', {}).get('min_signal_strength', 0) or 0),
                }
            bot_state['btc_mode']['m5'] = True
            # BTC feeds on many brokers have large point spreads; avoid accidental hard-blocks.
            if float(SYMBOLS_CONFIG.get('BTCUSD', {}).get('max_spread', 0) or 0) < 5000:
                SYMBOLS_CONFIG['BTCUSD']['max_spread'] = 6000
            # Keep BTC signal threshold practical; very high values can starve entries.
            if int(SYMBOLS_CONFIG.get('BTCUSD', {}).get('min_signal_strength', 0) or 0) > 50:
                SYMBOLS_CONFIG['BTCUSD']['min_signal_strength'] = 50
            # Ensure BTC fallback knobs exist even when running from older config.py.
            SYMBOLS_CONFIG['BTCUSD'].setdefault('btc_fallback_min_strength', 35)
            SYMBOLS_CONFIG['BTCUSD'].setdefault('btc_fallback_min_score_gap', 2)
            bot_state['symbols']['BTCUSD']['enabled'] = True
            if not bot_state['running']:
                bot_state['running'] = True
                bot_thread = threading.Thread(target=run_bot_thread, daemon=True)
                bot_thread.start()
                add_log('BTC standalone 5m started (independent mode)', 'SUCCESS')
            else:
                add_log('BTC standalone 5m mode enabled (independent mode)', 'INFO')
        else:
            bot_state['btc_mode']['m5'] = False
            snap = bot_state.get('btc_backup', {}).get('m5')
            if isinstance(snap, dict):
                if 'BTCUSD' in SYMBOLS_CONFIG:
                    SYMBOLS_CONFIG['BTCUSD']['max_spread'] = float(snap.get('btc_max_spread', SYMBOLS_CONFIG['BTCUSD'].get('max_spread', 120)))
                    SYMBOLS_CONFIG['BTCUSD']['min_signal_strength'] = int(snap.get('btc_min_signal_strength', SYMBOLS_CONFIG['BTCUSD'].get('min_signal_strength', 0)))
                bot_state['running'] = bool(snap.get('running', False))
            else:
                bot_state['running'] = False
            bot_state['btc_backup']['m5'] = None
            add_log('BTC standalone 5m stopped', 'INFO')

    return jsonify({'success': True, 'status': _btc_status_payload()})


# ── Performance Reports ────────────────────────────────────────────────────────

@app.route('/api/btc/preset', methods=['POST'])
def api_btc_preset():
    """Apply a BTC profile preset to bot runtime configs."""
    data = request.get_json() or {}
    profile = str(data.get('profile', 'balanced')).lower().strip()
    target = str(data.get('target', 'all')).lower().strip()

    if profile not in BTC_PROFILE_PRESETS:
        return jsonify({'success': False, 'error': f'Unknown profile: {profile}'}), 400

    p = BTC_PROFILE_PRESETS[profile]

    # Always ensure BTC symbol config reflects preset.
    if 'BTCUSD' in SYMBOLS_CONFIG:
        SYMBOLS_CONFIG['BTCUSD']['max_spread'] = float(p['max_spread'])
        SYMBOLS_CONFIG['BTCUSD']['min_signal_strength'] = int(p['min_signal_strength'])
        SYMBOLS_CONFIG['BTCUSD']['lot_size_multiplier'] = float(p['lot_size_multiplier'])

    # 5-min bot BTC runtime card config (for immediate next entries).
    btc_state = bot_state.get('symbols', {}).get('BTCUSD')
    if btc_state:
        btc_state['enabled'] = True

    # Gold 1-min bot (can now trade BTC through symbol_key)
    if target in ('all', 'gold'):
        gold_state['config']['symbol_key'] = 'BTCUSD'
        gold_state['config']['max_spread'] = int(p['max_spread'])
        gold_state['config']['confluence_score'] = int(p['min_confluence_score'])
        gold_state['config']['sl_atr_mult'] = float(p['stop_loss_atr_multiplier'])
        gold_state['config']['tp_atr_mult'] = float(p['take_profit_atr_multiplier'])

    # Day-trade bot (can now trade BTC through symbol_key)
    if target in ('all', 'daytrade'):
        daytrade_state['config']['symbol_key'] = 'BTCUSD'
        daytrade_state['config']['max_spread'] = int(p['max_spread'])
        daytrade_state['config']['confluence_score'] = int(p['min_confluence_score'])
        daytrade_state['config']['sl_atr_mult'] = float(p['stop_loss_atr_multiplier'])
        daytrade_state['config']['tp_atr_mult'] = float(p['take_profit_atr_multiplier'])

    add_log(f"BTC preset applied: {profile}", "INFO")
    add_gold_log(f"BTC preset applied: {profile}", "INFO")
    add_daytrade_log(f"BTC preset applied: {profile}", "INFO")

    return jsonify({
        'success': True,
        'profile': profile,
        'target': target,
        'applied': {
            'btc_symbol': SYMBOLS_CONFIG.get('BTCUSD', {}),
            'gold': gold_state.get('config', {}),
            'daytrade': daytrade_state.get('config', {}),
        },
    })


@app.route('/api/btc/walkforward', methods=['POST'])
def api_btc_walkforward():
    """Run quick BTC walk-forward validation from dashboard."""
    if not MT5_AVAILABLE:
        return jsonify({'success': False, 'error': 'MetaTrader5 package not installed'}), 400

    data = request.get_json() or {}
    profile = str(data.get('profile', 'balanced')).lower().strip()
    train_days = int(data.get('train_days', 21))
    test_days = int(data.get('test_days', 7))
    folds = int(data.get('folds', 4))

    train_days = max(7, min(train_days, 180))
    test_days = max(3, min(test_days, 90))
    folds = max(1, min(folds, 10))

    if profile not in BTC_PROFILE_PRESETS:
        return jsonify({'success': False, 'error': f'Unknown profile: {profile}'}), 400

    if not _mt5_ensure():
        return jsonify({'success': False, 'error': 'MT5 not connected'}), 400

    try:
        from btc_walkforward import run_walkforward

        symbol = _resolve_symbol('BTCUSD')
        out = run_walkforward(
            mt5,
            symbol=symbol,
            profile=profile,
            days=max(90, (train_days + test_days) * folds + 10),
            train_days=train_days,
            test_days=test_days,
            folds=folds,
        )
        return jsonify(out)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/reports')
def get_reports():
    """
    Generate daily / weekly / monthly performance reports from the permanent archive.
    Returns per-bot stats for each calendar period found in the archive.
    """
    import collections, itertools
    try:
        if not os.path.exists(_ARCHIVE_FILE):
            return jsonify({'daily': [], 'weekly': [], 'monthly': [], 'totals': {}})
        with open(_ARCHIVE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        trades = data.get('trades', [])
        if not trades:
            return jsonify({'daily': [], 'weekly': [], 'monthly': [], 'totals': {}})

        BOT_LABELS = ['', 'Gold1M', 'GoldDay', 'BTC']
        BOT_NAMES  = {'': '5-Min SMC', 'Gold1M': 'Gold 1-Min', 'GoldDay': 'Gold Day', 'BTC': 'BTC Standalone'}

        def _report_bot_key(t):
            sym = str(t.get('symbol', '')).upper()
            if ('BTC' in sym) or ('XBT' in sym):
                return 'BTC'
            return t.get('bot', '')

        def _stats(group):
            wins   = [t['profit'] for t in group if t.get('profit', 0) > 0]
            losses = [t['profit'] for t in group if t.get('profit', 0) < 0]
            total  = sum(t.get('profit', 0) for t in group)
            n      = len(group)
            return {
                'trades':       n,
                'wins':         len(wins),
                'losses':       len(losses),
                'win_rate':     round(len(wins) / n * 100, 1) if n else 0.0,
                'total_profit': round(total, 2),
                'avg_profit':   round(total / n, 2) if n else 0.0,
                'max_win':      round(max(wins),   2) if wins   else 0.0,
                'max_loss':     round(min(losses), 2) if losses else 0.0,
            }

        def _period_rows(keyfn, label_fn):
            rows = []
            keyed = collections.defaultdict(list)
            for t in trades:
                try:
                    keyed[keyfn(t)].append(t)
                except Exception:
                    pass  # skip trades with malformed dates
            for period in sorted(keyed.keys(), reverse=True):
                by_bot = collections.defaultdict(list)
                for t in keyed[period]:
                    by_bot[_report_bot_key(t)].append(t)
                for bot in BOT_LABELS:
                    grp = by_bot.get(bot, [])
                    if not grp:
                        continue
                    rows.append({
                        'period':    label_fn(period),
                        'bot':       BOT_NAMES.get(bot, bot or '5-Min SMC'),
                        'bot_key':   bot,
                        **_stats(grp),
                    })
                # All-bots combined row
                all_grp = keyed[period]
                rows.append({
                    'period':    label_fn(period),
                    'bot':       'ALL',
                    'bot_key':   'ALL',
                    **_stats(all_grp),
                })
            return rows

        import datetime as _dt
        def _week(t):
            raw = t.get('date') or t.get('close_time', '')[:10]
            if not raw or len(raw) < 10:
                raise ValueError('bad date')
            d = _dt.date.fromisoformat(raw[:10])
            return d.isocalendar()[0] * 100 + d.isocalendar()[1]  # YYYYWW int for sorting

        def _week_label(w):
            yr, wk = divmod(w, 100)
            return f'{yr}-W{wk:02d}'

        daily   = _period_rows(lambda t: t.get('date', t.get('close_time','')[:10]),
                               lambda p: p)
        weekly  = _period_rows(lambda t: _week(t), lambda p: _week_label(p))
        monthly = _period_rows(lambda t: t.get('date', t.get('close_time','')[:10])[:7],
                               lambda p: p)

        # Overall totals per bot across all time
        totals = {}
        by_bot_all = collections.defaultdict(list)
        for t in trades:
            by_bot_all[_report_bot_key(t)].append(t)
        for bot in BOT_LABELS:
            grp = by_bot_all.get(bot, [])
            totals[BOT_NAMES.get(bot, bot or '5-Min SMC')] = _stats(grp) if grp else {}

        return jsonify({'daily': daily, 'weekly': weekly, 'monthly': monthly, 'totals': totals})
    except Exception as e:
        return jsonify({'error': str(e), 'daily': [], 'weekly': [], 'monthly': [], 'totals': {}})


@app.route('/api/loss_report')
def get_loss_report():
    """Return daily loss-reach/drawdown analytics grouped by trading date."""
    try:
        period = request.args.get('period', 'day')
        if not os.path.exists(_ARCHIVE_FILE):
            return jsonify({'days': [], 'summary': {'period_mode': period}})
        with open(_ARCHIVE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        trades = data.get('trades', []) or []
        rows, summary = _build_daily_loss_rows(trades, period=period)
        return jsonify({'days': rows, 'summary': summary})
    except Exception as e:
        return jsonify({'error': str(e), 'days': [], 'summary': {}})


@app.route('/api/export_report')
def export_report():
    """Generate and return a standalone HTML performance report as a file download."""
    import collections, datetime as _dt
    period = request.args.get('period', 'daily')
    try:
        if not os.path.exists(_ARCHIVE_FILE):
            return 'No trade data yet.', 404
        with open(_ARCHIVE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        trades = data.get('trades', [])

        BOT_LABELS = ['', 'Gold1M', 'GoldDay', 'BTC']
        BOT_NAMES  = {'': '5-Min SMC', 'Gold1M': 'Gold 1-Min', 'GoldDay': 'Gold Day', 'BTC': 'BTC Standalone'}
        BOT_COLORS = {'': '#3498db', 'Gold1M': '#ffd700', 'GoldDay': '#f39c12', 'BTC': '#ff9f43', 'ALL': '#aaa'}

        def _report_bot_key(t):
            sym = str(t.get('symbol', '')).upper()
            if ('BTC' in sym) or ('XBT' in sym):
                return 'BTC'
            return t.get('bot', '')

        def _stats(group):
            wins   = [t['profit'] for t in group if t.get('profit', 0) > 0]
            losses = [t['profit'] for t in group if t.get('profit', 0) < 0]
            total  = sum(t.get('profit', 0) for t in group)
            n      = len(group)
            return {
                'trades': n, 'wins': len(wins), 'losses': len(losses),
                'win_rate': round(len(wins)/n*100, 1) if n else 0.0,
                'total_profit': round(total, 2),
                'avg_profit': round(total/n, 2) if n else 0.0,
                'max_win': round(max(wins), 2) if wins else 0.0,
                'max_loss': round(min(losses), 2) if losses else 0.0,
            }

        def _key(t):
            raw = t.get('date', t.get('close_time', '')[:10])
            if period == 'weekly':
                d = _dt.date.fromisoformat(raw[:10])
                iso = d.isocalendar()
                return f'{iso[0]}-W{iso[1]:02d}'
            if period == 'monthly': return raw[:7] if raw else ''
            return raw[:10] if raw else ''

        keyed = collections.defaultdict(list)
        for t in trades:
            try: keyed[_key(t)].append(t)
            except Exception: pass

        rows_html = ''
        for per in sorted(keyed.keys(), reverse=True):
            by_bot = collections.defaultdict(list)
            for t in keyed[per]:
                by_bot[_report_bot_key(t)].append(t)
            for bot in BOT_LABELS:
                grp = by_bot.get(bot, [])
                if not grp: continue
                s = _stats(grp)
                clr = '#27ae60' if s['win_rate'] >= 55 else ('#f39c12' if s['win_rate'] >= 45 else '#e74c3c')
                pclr = '#27ae60' if s['total_profit'] >= 0 else '#e74c3c'
                bc = BOT_COLORS.get(bot, '#ccc')
                sign = '+' if s['total_profit'] >= 0 else ''
                asign = '+' if s['avg_profit'] >= 0 else ''
                rows_html += (
                    f'<tr><td>{per}</td>'
                    f'<td style="color:{bc}">{BOT_NAMES.get(bot, bot)}</td>'
                    f'<td>{s["trades"]}</td>'
                    f'<td style="color:{clr}">{s["win_rate"]}%</td>'
                    f'<td>{s["wins"]}/{s["losses"]}</td>'
                    f'<td style="color:{pclr}">{sign}${s["total_profit"]:.2f}</td>'
                    f'<td style="color:{pclr}">{asign}${s["avg_profit"]:.2f}</td>'
                    f'<td style="color:#27ae60">+${s["max_win"]:.2f}</td>'
                    f'<td style="color:#e74c3c">-${abs(s["max_loss"]):.2f}</td></tr>'
                )
            all_grp = keyed[per]
            s = _stats(all_grp)
            pclr = '#27ae60' if s['total_profit'] >= 0 else '#e74c3c'
            sign = '+' if s['total_profit'] >= 0 else ''
            asign = '+' if s['avg_profit'] >= 0 else ''
            rows_html += (
                f'<tr style="border-top:2px solid #444;font-weight:600">'
                f'<td>{per}</td><td style="color:#aaa">ALL</td>'
                f'<td>{s["trades"]}</td><td>{s["win_rate"]}%</td>'
                f'<td>{s["wins"]}/{s["losses"]}</td>'
                f'<td style="color:{pclr}">{sign}${s["total_profit"]:.2f}</td>'
                f'<td style="color:{pclr}">{asign}${s["avg_profit"]:.2f}</td>'
                f'<td style="color:#27ae60">+${s["max_win"]:.2f}</td>'
                f'<td style="color:#e74c3c">-${abs(s["max_loss"]):.2f}</td></tr>'
            )

        by_bot_all = collections.defaultdict(list)
        for t in trades:
            by_bot_all[_report_bot_key(t)].append(t)
        totals_html = ''
        for bot in BOT_LABELS:
            grp = by_bot_all.get(bot, [])
            if not grp: continue
            s = _stats(grp)
            clr = '#27ae60' if s['win_rate'] >= 55 else ('#f39c12' if s['win_rate'] >= 45 else '#e74c3c')
            pclr = '#27ae60' if s['total_profit'] >= 0 else '#e74c3c'
            sign = '+' if s['total_profit'] >= 0 else ''
            totals_html += (
                f'<div class="tot-card"><h3>{BOT_NAMES.get(bot, bot)}</h3><table>'
                f'<tr><td>Trades</td><td><b>{s["trades"]}</b></td></tr>'
                f'<tr><td>Win Rate</td><td><b style="color:{clr}">{s["win_rate"]}%</b></td></tr>'
                f'<tr><td>Total P&amp;L</td><td><b style="color:{pclr}">{sign}${s["total_profit"]:.2f}</b></td></tr>'
                f'<tr><td>Best</td><td><b style="color:#27ae60">+${s["max_win"]:.2f}</b></td></tr>'
                f'<tr><td>Worst</td><td><b style="color:#e74c3c">-${abs(s["max_loss"]):.2f}</b></td></tr>'
                f'</table></div>'
            )

        now = _dt.datetime.now().strftime('%Y-%m-%d %H:%M')
        pt = period.capitalize()
        html_out = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<title>Trading Report &mdash; {now}</title>'
            '<style>'
            'body{font-family:Arial,sans-serif;background:#0d0d1a;color:#ccc;margin:20px}'
            'h1{color:#27ae60}h2{color:#ffd700;margin-top:24px}'
            '.tot-wrap{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:20px}'
            '.tot-card{background:#1a1a2e;border-radius:8px;padding:14px;min-width:180px}'
            '.tot-card h3{margin:0 0 8px;color:#aaa}'
            '.tot-card table td{padding:2px 8px}'
            'table.rep{width:100%;border-collapse:collapse;background:#1a1a2e}'
            'table.rep th{background:#111;padding:8px;text-align:left;color:#ffd700}'
            'table.rep td{padding:6px 8px;border-bottom:1px solid #222}'
            '</style></head><body>'
            '<h1>&#128196; Trading Performance Report</h1>'
            f'<p style="color:#666">Generated: {now}</p>'
            '<h2>All-Time Totals</h2>'
            f'<div class="tot-wrap">{totals_html}</div>'
            f'<h2>{pt} Breakdown</h2>'
            '<table class="rep"><thead><tr>'
            f'<th>{pt}</th><th>Bot</th><th>Trades</th><th>Win%</th>'
            '<th>W/L</th><th>Total P&amp;L</th><th>Avg</th><th>Best</th><th>Worst</th>'
            '</tr></thead>'
            f'<tbody>{rows_html}</tbody></table>'
            '</body></html>'
        )
        filename = f'trading_report_{period}_{_dt.date.today()}.html'
        resp = make_response(html_out)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return resp
    except Exception as e:
        import traceback
        return f'Export error: {e}\n{traceback.format_exc()}', 500


# ── New-Day Reset ─────────────────────────────────────────────────────────────

@app.route('/api/reset_new_day', methods=['POST'])
def reset_new_day():
    """
    Reset ALL session state as if starting a brand-new trading day.
    Clears:
      - Trade history (in-memory + disk files)
      - Daily-goal state (goal_reached, closed_profit, etc.)
      - Session PnL & stat counters for all 3 bots
      - Position tracking dicts
    The bots keep running but start counting from zero.
    """
    global daily_goal_state, _gold_positions, _reset_cutoff_time

    # ── Stamp the reset time — deals before this are ignored on any reload ───
    _reset_cutoff_time = datetime.now()

    # ── Clear trade history ──────────────────────────────────────
    bot_state['trade_history'].clear()
    bot_state['stats']['session_pnl']       = 0.0
    bot_state['stats']['trades_opened']     = 0
    bot_state['stats']['signals_generated'] = 0

    # ── Reset daily goal tracker ─────────────────────────────────
    daily_goal_state['start_balance']  = None
    daily_goal_state['start_date']     = None
    daily_goal_state['current_pnl']    = 0.0
    daily_goal_state['closed_profit']  = 0.0
    daily_goal_state['goal_reached']   = False
    bot_state['daily_goal']['goal_reached']  = False
    bot_state['daily_goal']['current_pnl']   = 0.0
    bot_state['daily_goal']['progress_pct']  = 0.0

    # ── Reset Gold stats ─────────────────────────────────────────
    gold_state['stats']  = {'trades_opened': 0, 'recycled': 0, 'wins': 0, 'losses': 0}
    gold_state['live']['buy_score']  = 0
    gold_state['live']['sell_score'] = 0
    gold_state['stop_reason']        = 'New-Day reset — click \u25B6 Start to begin.'

    # ── Wipe position-tracking dicts (MT5 positions are the ground truth) ──
    _gold_positions.clear()
    _day_positions.clear()
    _open_ticket_map.clear()
    _all_positions_snapshot.clear()
    _trade_loss_tracker.clear()

    # ── Reset Day Trade stats ────────────────────────────────────
    daytrade_state['stats'] = {'trades_opened': 0, 'recycled': 0, 'wins': 0, 'losses': 0}
    daytrade_state['live']['buy_score']  = 0
    daytrade_state['live']['sell_score'] = 0
    daytrade_state['stop_reason']        = 'New-Day reset — click ▶ Start to begin.'

    # ── Delete history file; save goal file with the cutoff time persisted ──
    try:
        if os.path.exists(_HISTORY_FILE):
            os.remove(_HISTORY_FILE)
    except Exception:
        pass
    # Save goal file immediately so _reset_cutoff_time survives a process restart
    _save_daily_goal_to_disk()

    add_log('New-Day reset performed — all session state cleared.', 'INFO')
    add_gold_log('New-Day reset: session state cleared.', 'INFO')
    add_daytrade_log('New-Day reset: session state cleared.', 'INFO')
    return jsonify({'success': True, 'message': 'New-Day reset complete — all counters cleared.'})


# ── Trading-Enabled kill-switch ────────────────────────────────

@app.route('/api/trading_enabled', methods=['GET', 'POST'])
def api_trading_enabled():
    global trading_enabled
    if request.method == 'POST':
        data = request.get_json() or {}
        val  = data.get('enabled')
        if val is None:
            return jsonify({'success': False, 'error': 'Missing "enabled" field'}), 400
        before_val = trading_enabled
        trading_enabled = bool(val)
        state_str = 'ENABLED' if trading_enabled else 'DISABLED'
        add_log(f"Trading {state_str} via dashboard toggle", 'WARNING' if not trading_enabled else 'INFO')
        add_gold_log(f"Trading {state_str}", 'WARNING' if not trading_enabled else 'INFO')
        add_daytrade_log(f"Trading {state_str}", 'WARNING' if not trading_enabled else 'INFO')
        _record_config_change('trading_enabled', {'enabled': before_val}, {'enabled': trading_enabled})
        return jsonify({'success': True, 'trading_enabled': trading_enabled})
    return jsonify({'trading_enabled': trading_enabled})


@app.route('/api/email_config', methods=['GET', 'POST'])
def api_email_config():
    if request.method == 'GET':
        return jsonify({
            'config': {
                'enabled': bool(_email_cfg('enabled', False)),
                'smtp_host': str(_email_cfg('smtp_host', '') or ''),
                'smtp_port': int(_email_cfg('smtp_port', 587) or 587),
                'use_tls': bool(_email_cfg('use_tls', True)),
                'use_ssl': bool(_email_cfg('use_ssl', False)),
                'username': str(_email_cfg('username', '') or ''),
                'from_email': str(_email_cfg('from_email', '') or ''),
                'to_email': str(_email_cfg('to_email', '') or ''),
                'daily_summary_time_utc': str(_email_cfg('daily_summary_time_utc', '23:55') or '23:55'),
                'risk_alert_thresholds': list(_email_cfg('risk_alert_thresholds', [25.0, 50.0, 100.0]) or []),
            }
        })

    data = request.get_json() or {}
    before_cfg = {
        'enabled': EMAIL_CONFIG.get('enabled', False),
        'smtp_host': EMAIL_CONFIG.get('smtp_host', ''),
        'smtp_port': EMAIL_CONFIG.get('smtp_port', 587),
        'use_tls': EMAIL_CONFIG.get('use_tls', True),
        'use_ssl': EMAIL_CONFIG.get('use_ssl', False),
        'username': EMAIL_CONFIG.get('username', ''),
        'from_email': EMAIL_CONFIG.get('from_email', ''),
        'to_email': EMAIL_CONFIG.get('to_email', ''),
        'daily_summary_time_utc': EMAIL_CONFIG.get('daily_summary_time_utc', '23:55'),
        'risk_alert_thresholds': EMAIL_CONFIG.get('risk_alert_thresholds', [25.0, 50.0, 100.0]),
    }
    try:
        EMAIL_CONFIG['enabled'] = bool(data.get('enabled', EMAIL_CONFIG.get('enabled', False)))
        EMAIL_CONFIG['smtp_host'] = str(data.get('smtp_host', EMAIL_CONFIG.get('smtp_host', '')) or '').strip()
        EMAIL_CONFIG['smtp_port'] = int(data.get('smtp_port', EMAIL_CONFIG.get('smtp_port', 587)) or 587)
        EMAIL_CONFIG['use_tls'] = bool(data.get('use_tls', EMAIL_CONFIG.get('use_tls', True)))
        EMAIL_CONFIG['use_ssl'] = bool(data.get('use_ssl', EMAIL_CONFIG.get('use_ssl', False)))
        EMAIL_CONFIG['username'] = str(data.get('username', EMAIL_CONFIG.get('username', '')) or '').strip()
        if 'password' in data and str(data.get('password', '')).strip():
            EMAIL_CONFIG['password'] = str(data.get('password', '')).strip()
        EMAIL_CONFIG['from_email'] = str(data.get('from_email', EMAIL_CONFIG.get('from_email', '')) or '').strip()
        EMAIL_CONFIG['to_email'] = str(data.get('to_email', EMAIL_CONFIG.get('to_email', '')) or '').strip()
        daily_time = str(data.get('daily_summary_time_utc', EMAIL_CONFIG.get('daily_summary_time_utc', '23:55')) or '23:55').strip()
        if not re.match(r'^\d{2}:\d{2}$', daily_time):
            return jsonify({'success': False, 'error': 'daily_summary_time_utc must be HH:MM'}), 400
        EMAIL_CONFIG['daily_summary_time_utc'] = daily_time

        raw_thresholds = data.get('risk_alert_thresholds', EMAIL_CONFIG.get('risk_alert_thresholds', [25.0, 50.0, 100.0]))
        if isinstance(raw_thresholds, str):
            vals = [x.strip() for x in raw_thresholds.split(',') if x.strip()]
        else:
            vals = list(raw_thresholds or [])
        thresholds = sorted({float(v) for v in vals if float(v) > 0})
        EMAIL_CONFIG['risk_alert_thresholds'] = thresholds or [25.0, 50.0, 100.0]

        # Reset dedupe state after config change.
        bot_state.setdefault('notifications', {})['daily_summary_sent_for'] = None
        bot_state.setdefault('notifications', {})['risk_alert_buckets_sent_for'] = {}
        bot_state.setdefault('notifications', {})['daily_report_sent_for'] = None
        _save_notification_settings_to_disk()
        add_log('Email notification settings updated via dashboard', 'INFO')
        _record_config_change('email_config', before_cfg, {
            'enabled': EMAIL_CONFIG.get('enabled', False),
            'smtp_host': EMAIL_CONFIG.get('smtp_host', ''),
            'smtp_port': EMAIL_CONFIG.get('smtp_port', 587),
            'use_tls': EMAIL_CONFIG.get('use_tls', True),
            'use_ssl': EMAIL_CONFIG.get('use_ssl', False),
            'username': EMAIL_CONFIG.get('username', ''),
            'from_email': EMAIL_CONFIG.get('from_email', ''),
            'to_email': EMAIL_CONFIG.get('to_email', ''),
            'daily_summary_time_utc': EMAIL_CONFIG.get('daily_summary_time_utc', '23:55'),
            'risk_alert_thresholds': EMAIL_CONFIG.get('risk_alert_thresholds', [25.0, 50.0, 100.0]),
        })
        return jsonify({'success': True})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


@app.route('/api/email_test', methods=['POST'])
def api_email_test():
    if not _email_notifications_enabled():
        return jsonify({'success': False, 'error': 'Email notifications are disabled or incomplete config'}), 400
    account = bot_state.get('account') or {}
    subject = 'Scalping Bot test email'
    body = (
        'This is a test email from the Scalping Bot dashboard.\n\n'
        f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Account: {account.get('login', 'N/A')}\n"
        f"Server: {account.get('server', 'N/A')}\n"
    )
    ok = _send_email(subject, body)
    if not ok:
        return jsonify({'success': False, 'error': 'SMTP send failed; check host/credentials'}), 400
    return jsonify({'success': True})


@app.route('/api/email_test_trade_close', methods=['POST'])
def api_email_test_trade_close():
    """Send a simulated trade-close email using the live close-notification formatter."""
    if not _email_notifications_enabled():
        return jsonify({'success': False, 'error': 'Email notifications are disabled or incomplete config'}), 400

    data = request.get_json(silent=True) or {}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    side = str(data.get('type', 'buy') or 'buy').lower()
    if side not in ('buy', 'sell'):
        side = 'buy'
    record = {
        'ticket': int(data.get('ticket', int(time.time()) % 1000000000)),
        'symbol': str(data.get('symbol', 'BTCUSD')),
        'type': side,
        'volume': float(data.get('volume', 0.01) or 0.01),
        'open_price': float(data.get('open_price', 70000.0) or 70000.0),
        'close_price': float(data.get('close_price', 70025.0) or 70025.0),
        'open_time': str(data.get('open_time', now)),
        'close_time': str(data.get('close_time', now)),
        'profit': float(data.get('profit', 5.25) or 5.25),
        'max_loss_reach': float(data.get('max_loss_reach', 3.10) or 3.10),
        'result': str(data.get('result', 'WIN')),
        'bot': str(data.get('bot', 'BTC-SIM')),
    }
    subject, body = _format_trade_close_email(record)
    ok = _send_email('[SIMULATED] ' + subject, body)
    if not ok:
        return jsonify({'success': False, 'error': 'SMTP send failed; check host/credentials'}), 400
    return jsonify({'success': True, 'subject': '[SIMULATED] ' + subject})


# ── Gold Day Trading Bot API routes ────────────────────────────

@app.route('/api/daytrade/start', methods=['POST'])
def daytrade_start():
    global daytrade_thread, daytrade_state
    if not bot_state.get('connected'):
        return jsonify({'success': False, 'error': 'Not connected to MT5'})
    if bot_state.get('emergency_pause', {}).get('active'):
        return jsonify({'success': False, 'error': 'Emergency pause is active. Release it first.'}), 400
    if daytrade_state['running']:
        return jsonify({'success': True})  # Already running, return success
    daytrade_state['manual_running'] = True
    daytrade_state['running']     = True
    daytrade_state['stop_reason'] = ''
    daytrade_thread = threading.Thread(target=run_daytrade_bot_thread, daemon=True)
    daytrade_thread.start()
    return jsonify({'success': True})


@app.route('/api/daytrade/stop', methods=['POST'])
def daytrade_stop():
    daytrade_state['manual_running'] = False
    daytrade_state['running']     = False
    daytrade_state['stop_reason'] = 'Stopped manually.'
    add_daytrade_log("Stopping Gold Day Trade bot...")
    return jsonify({'success': True})


@app.route('/api/daytrade/status', methods=['GET'])
def daytrade_status():
    try:
        _open_pos   = mt5.positions_get() or []
        _day_float  = sum(float(p.profit) for p in _open_pos if getattr(p, 'magic', 0) == DAYTRADE_MAGIC)
        _day_closed = sum(t.get('profit', 0) for t in bot_state['trade_history']
                          if t.get('bot') == 'GoldDay')
        day_live_pnl = round(_day_float + _day_closed, 2)
    except Exception:
        day_live_pnl = 0.0
    effective_running = bool(daytrade_state.get('running', False) and daytrade_state.get('manual_running', False))
    live_payload = daytrade_state['live']
    if not effective_running:
        live_payload = dict(daytrade_state.get('live', {}) or {})
        live_payload['price'] = None
        live_payload['spread'] = 0
        live_payload['signal'] = 'NONE'
        live_payload['buy_score'] = 0
        live_payload['sell_score'] = 0
        live_payload['positions'] = []
        live_payload['total_positions'] = 0
    return jsonify({
        'running':          effective_running,
        'engine_running':   bool(daytrade_state.get('running', False)),
        'stop_reason':      daytrade_state.get('stop_reason', ''),
        'connected':        bot_state.get('connected', False),
        'goal_reached':     daily_goal_state.get('goal_reached', False),
        'trading_enabled':  trading_enabled,
        'config':           daytrade_state['config'],
        'live':             live_payload,
        'stats':            daytrade_state['stats'],
        'logs':             daytrade_state['logs'][:50],
        'live_pnl':         day_live_pnl,
    })


@app.route('/api/daytrade/config', methods=['POST'])
def daytrade_config():
    data = request.get_json() or {}
    cfg  = daytrade_state['config']
    before_cfg = copy.deepcopy(cfg)
    allow_btc = bool(data.get('allow_btc', False))
    if 'symbol_key' in data:
        cfg['symbol_key'] = _resolve_symbol_key(data.get('symbol_key'), 'GOLD')
        if (not allow_btc) and cfg['symbol_key'] == 'BTCUSD':
            cfg['symbol_key'] = 'GOLD'
    mapping = {
        'lot_size':         lambda v: max(0.0, round(float(v), 2)),
        'max_positions':    lambda v: max(1, min(int(v), 10)),
        'max_spread':       lambda v: max(5, min(int(v), 1000)),
        'sl_atr_mult':      lambda v: max(0.1, round(float(v), 2)),
        'tp_atr_mult':      lambda v: max(0.1, round(float(v), 2)),
        'confluence_score': lambda v: max(1, min(int(v), 25)),
        'session_filter':   lambda v: bool(v),
        'candle_patterns':  lambda v: bool(v),
        'recycle_pct':      lambda v: max(0.10, min(round(float(v), 2), 1.0)),
    }
    for key, coerce in mapping.items():
        if key in data:
            try:
                cfg[key] = coerce(data[key])
            except (ValueError, TypeError):
                pass
    add_daytrade_log(
        f"Config updated: symbol={cfg.get('symbol_key','GOLD')}, "
        f"lot={'auto' if cfg['lot_size']==0 else cfg['lot_size']}, "
        f"maxpos={cfg['max_positions']}, score={cfg['confluence_score']}, "
        f"SL={cfg['sl_atr_mult']}xATR, TP={cfg['tp_atr_mult']}xATR"
    )
    _record_config_change('daytrade_config', before_cfg, copy.deepcopy(cfg))
    return jsonify({'success': True, 'config': cfg})


# ── Gold 1-Min Bot API routes ───────────────────────────────────

@app.route('/api/gold/start', methods=['POST'])
def gold_start():
    global gold_thread, gold_state
    if not bot_state.get('connected'):
        return jsonify({'success': False, 'error': 'Not connected to MT5'})
    if bot_state.get('emergency_pause', {}).get('active'):
        return jsonify({'success': False, 'error': 'Emergency pause is active. Release it first.'}), 400
    if gold_state['running']:
        return jsonify({'success': True})  # Already running, return success
    gold_state['manual_running'] = True
    gold_state['running'] = True
    gold_state['stop_reason'] = ''
    gold_thread = threading.Thread(target=run_gold_bot_thread, daemon=True)
    gold_thread.start()
    return jsonify({'success': True})


@app.route('/api/gold/stop', methods=['POST'])
def gold_stop():
    gold_state['manual_running'] = False
    gold_state['running']     = False
    gold_state['stop_reason'] = 'Stopped manually.'
    add_gold_log("Stopping Gold 1-Min bot...")
    return jsonify({'success': True})


@app.route('/api/gold/status', methods=['GET'])
def gold_status():
    try:
        _open_pos = mt5.positions_get() or []
        _gold_float  = sum(float(p.profit) for p in _open_pos if getattr(p, 'magic', 0) == GOLD_MAGIC)
        _gold_closed = sum(t.get('profit', 0) for t in bot_state['trade_history']
                           if t.get('bot') == 'Gold1M')
        gold_live_pnl = round(_gold_float + _gold_closed, 2)
    except Exception:
        gold_live_pnl = 0.0
    effective_running = bool(gold_state.get('running', False) and gold_state.get('manual_running', False))
    live_payload = gold_state['live']
    if not effective_running:
        live_payload = dict(gold_state.get('live', {}) or {})
        live_payload['price'] = None
        live_payload['spread'] = 0
        live_payload['signal'] = 'NONE'
        live_payload['buy_score'] = 0
        live_payload['sell_score'] = 0
        live_payload['positions'] = []
        live_payload['total_positions'] = 0
    return jsonify({
        'running':     effective_running,
        'engine_running': bool(gold_state.get('running', False)),
        'stop_reason': gold_state.get('stop_reason', ''),
        'connected':   bot_state.get('connected', False),
        'goal_reached': daily_goal_state.get('goal_reached', False),
        'config':      gold_state['config'],
        'live':        live_payload,
        'stats':       gold_state['stats'],
        'logs':        gold_state['logs'][:50],
        'live_pnl':    gold_live_pnl,
    })


@app.route('/api/gold/config', methods=['POST'])
def gold_config():
    data = request.get_json() or {}
    cfg  = gold_state['config']
    before_cfg = copy.deepcopy(cfg)
    allow_btc = bool(data.get('allow_btc', False))
    if 'symbol_key' in data:
        cfg['symbol_key'] = _resolve_symbol_key(data.get('symbol_key'), 'GOLD')
        if (not allow_btc) and cfg['symbol_key'] == 'BTCUSD':
            cfg['symbol_key'] = 'GOLD'
    mapping = {
        'lot_size':         lambda v: max(0.0, round(float(v), 2)),
        'max_positions':    lambda v: max(1, min(int(v), 10)),
        'max_spread':       lambda v: max(5, min(int(v), 1000)),
        'sl_atr_mult':      lambda v: max(0.1, round(float(v), 2)),
        'tp_atr_mult':      lambda v: max(0.1, round(float(v), 2)),
        'confluence_score': lambda v: max(1, min(int(v), 25)),
        'session_filter':   lambda v: bool(v),
        'candle_patterns':  lambda v: bool(v),
        'recycle_pct':      lambda v: max(0.10, min(round(float(v), 2), 1.0)),
    }
    for key, coerce in mapping.items():
        if key in data:
            try:
                cfg[key] = coerce(data[key])
            except (ValueError, TypeError):
                pass
    add_gold_log(
        f"Config updated: symbol={cfg.get('symbol_key','GOLD')}, "
        f"lot={'auto' if cfg['lot_size']==0 else cfg['lot_size']}, "
        f"maxpos={cfg['max_positions']}, score={cfg['confluence_score']}, "
        f"SL={cfg['sl_atr_mult']}xATR, TP={cfg['tp_atr_mult']}xATR, "
        f"recycle={int(cfg['recycle_pct']*100)}%"
    )
    _record_config_change('gold_config', before_cfg, copy.deepcopy(cfg))
    return jsonify({'success': True, 'config': cfg})


def _all_closed_trades():
    merged = []
    seen = set()
    for t in bot_state.get('trade_history', []) or []:
        tid = t.get('ticket')
        if tid in seen:
            continue
        merged.append(t)
        seen.add(tid)
    try:
        if os.path.exists(_ARCHIVE_FILE):
            with open(_ARCHIVE_FILE, encoding='utf-8') as f:
                data = json.load(f) or {}
            for t in data.get('trades', []) or []:
                tid = t.get('ticket')
                if tid in seen:
                    continue
                merged.append(t)
                seen.add(tid)
    except Exception:
        pass
    return merged


@app.route('/api/push_config', methods=['GET', 'POST'])
def api_push_config():
    cfg = bot_state.setdefault('push_config', {
        'enabled': False,
        'telegram_bot_token': '',
        'telegram_chat_id': '',
        'discord_webhook_url': '',
    })
    if request.method == 'GET':
        return jsonify({'config': {
            'enabled': bool(cfg.get('enabled', False)),
            'telegram_chat_id': str(cfg.get('telegram_chat_id', '') or ''),
            'discord_webhook_url': str(cfg.get('discord_webhook_url', '') or ''),
            'has_telegram_token': bool(str(cfg.get('telegram_bot_token', '') or '').strip()),
        }})

    data = request.get_json() or {}
    before = dict(cfg)
    cfg['enabled'] = bool(data.get('enabled', cfg.get('enabled', False)))
    if 'telegram_bot_token' in data and str(data.get('telegram_bot_token', '')).strip():
        cfg['telegram_bot_token'] = str(data.get('telegram_bot_token', '')).strip()
    cfg['telegram_chat_id'] = str(data.get('telegram_chat_id', cfg.get('telegram_chat_id', '')) or '').strip()
    cfg['discord_webhook_url'] = str(data.get('discord_webhook_url', cfg.get('discord_webhook_url', '')) or '').strip()
    _save_notification_settings_to_disk()
    _record_config_change('push_config', {
        'enabled': before.get('enabled', False),
        'telegram_chat_id': before.get('telegram_chat_id', ''),
        'discord_webhook_url': before.get('discord_webhook_url', ''),
    }, {
        'enabled': cfg.get('enabled', False),
        'telegram_chat_id': cfg.get('telegram_chat_id', ''),
        'discord_webhook_url': cfg.get('discord_webhook_url', ''),
    })
    return jsonify({'success': True})


@app.route('/api/push_test', methods=['POST'])
def api_push_test():
    msg = f"Push test from Scalping Bot at {_now_iso()}"
    ok = _send_push(msg, title='Scalping Bot Push Test')
    return jsonify({'success': bool(ok), 'message': msg})


@app.route('/api/performance_splits')
def api_performance_splits():
    trades = _all_closed_trades()
    by_bot = {'': [], 'Gold1M': [], 'GoldDay': [], 'BTC': []}
    for t in trades:
        by_bot.setdefault(_trade_bot_key(t), []).append(t)
    return jsonify({
        'all': _performance_stats(trades),
        'bots': { _format_bot_label(k): _performance_stats(v) for k, v in by_bot.items() },
    })


@app.route('/api/strategy_health')
def api_strategy_health():
    trades = _all_closed_trades()
    by_bot = {'': [], 'Gold1M': [], 'GoldDay': [], 'BTC': []}
    for t in trades:
        by_bot.setdefault(_trade_bot_key(t), []).append(t)
    health = {}
    for k, arr in by_bot.items():
        s = _performance_stats(arr)
        score = 50.0
        score += min(20.0, max(-20.0, (s['win_rate'] - 50.0) * 0.6))
        score += min(15.0, max(-15.0, s['profit_factor'] - 1.0))
        score += min(15.0, max(-15.0, s['sharpe_est'] * 2.5))
        score += max(-20.0, min(5.0, s['max_drawdown'] / 20.0))
        health[_format_bot_label(k)] = {
            **s,
            'health_score': round(max(0.0, min(100.0, score)), 1),
            'status': 'HEALTHY' if score >= 65 else ('WATCH' if score >= 45 else 'RISK'),
        }
    return jsonify({'generated_at_utc': _now_iso(), 'health': health})


@app.route('/api/open_risk_dashboard')
def api_open_risk_dashboard():
    try:
        positions = mt5.positions_get() or []
    except Exception:
        positions = []
    by_symbol = {}
    total_float = 0.0
    total_lots = 0.0
    for p in positions:
        sym = str(getattr(p, 'symbol', 'UNKNOWN'))
        pnl = float(getattr(p, 'profit', 0.0) or 0.0)
        vol = float(getattr(p, 'volume', 0.0) or 0.0)
        tid = int(getattr(p, 'ticket', 0) or 0)
        lr = float((_trade_loss_tracker.get(tid, {}) or {}).get('max_loss_reach', 0.0) or 0.0)
        row = by_symbol.setdefault(sym, {
            'symbol': sym,
            'positions': 0,
            'lots': 0.0,
            'floating_pnl': 0.0,
            'max_loss_reach_open': 0.0,
        })
        row['positions'] += 1
        row['lots'] += vol
        row['floating_pnl'] += pnl
        row['max_loss_reach_open'] += abs(lr)
        total_float += pnl
        total_lots += vol
    rows = sorted(by_symbol.values(), key=lambda x: abs(x['floating_pnl']), reverse=True)
    return jsonify({
        'summary': {
            'open_positions': len(positions),
            'total_lots': round(total_lots, 2),
            'floating_pnl': round(total_float, 2),
            'open_loss_metrics': _open_trade_loss_metrics(),
        },
        'symbols': rows,
    })


@app.route('/api/equity_curve')
def api_equity_curve():
    trades = sorted(_all_closed_trades(), key=lambda t: str(t.get('close_time', '')))
    points_all = []
    cum_all = 0.0
    by_bot_cum = {'': 0.0, 'Gold1M': 0.0, 'GoldDay': 0.0, 'BTC': 0.0}
    points_by_bot = {'5-Min SMC': [], 'Gold 1-Min': [], 'Gold Day': [], 'BTC': []}
    for t in trades:
        pnl = float(t.get('profit', 0) or 0)
        key = _trade_bot_key(t)
        cum_all += pnl
        by_bot_cum[key] = by_bot_cum.get(key, 0.0) + pnl
        ts = str(t.get('close_time', '') or t.get('date', ''))
        points_all.append({'time': ts, 'equity': round(cum_all, 2), 'ticket': t.get('ticket')})
        points_by_bot.setdefault(_format_bot_label(key), []).append({'time': ts, 'equity': round(by_bot_cum[key], 2), 'ticket': t.get('ticket')})
    return jsonify({'all': points_all, 'by_bot': points_by_bot})


@app.route('/api/trade_replay')
def api_trade_replay():
    bot_filter = str(request.args.get('bot', 'all') or 'all').strip().lower()
    trades = sorted(_all_closed_trades(), key=lambda t: str(t.get('close_time', '')))
    if bot_filter != 'all':
        if bot_filter == '5min':
            trades = [t for t in trades if _trade_bot_key(t) == '']
        elif bot_filter == 'gold1m':
            trades = [t for t in trades if _trade_bot_key(t) == 'Gold1M']
        elif bot_filter == 'goldday':
            trades = [t for t in trades if _trade_bot_key(t) == 'GoldDay']
        elif bot_filter == 'btc':
            trades = [t for t in trades if _trade_bot_key(t) == 'BTC']

    replay = []
    cum = 0.0
    for idx, t in enumerate(trades, start=1):
        pnl = float(t.get('profit', 0) or 0)
        cum += pnl
        duration_min = None
        try:
            ot = datetime.fromisoformat(str(t.get('open_time', ''))[:19])
            ct = datetime.fromisoformat(str(t.get('close_time', ''))[:19])
            duration_min = round((ct - ot).total_seconds() / 60.0, 2)
        except Exception:
            pass
        replay.append({
            'step': idx,
            'ticket': t.get('ticket'),
            'bot': _format_bot_label(_trade_bot_key(t)),
            'symbol': t.get('symbol', ''),
            'side': str(t.get('type', '')).upper(),
            'open_time': t.get('open_time', ''),
            'close_time': t.get('close_time', ''),
            'duration_min': duration_min,
            'entry': float(t.get('open_price', 0) or 0),
            'exit': float(t.get('close_price', 0) or 0),
            'profit': round(pnl, 2),
            'max_loss_reach': round(float(t.get('max_loss_reach', 0) or 0), 2),
            'cumulative_pnl': round(cum, 2),
            'result': t.get('result', ''),
        })
    return jsonify({'count': len(replay), 'replay': replay})


@app.route('/api/export_journal')
def api_export_journal():
    fmt = str(request.args.get('format', 'csv') or 'csv').lower().strip()
    trades = sorted(_all_closed_trades(), key=lambda t: str(t.get('close_time', '')), reverse=True)
    fields = ['ticket', 'bot', 'symbol', 'type', 'volume', 'open_price', 'close_price', 'profit', 'max_loss_reach', 'result', 'open_time', 'close_time']

    if fmt == 'json':
        return jsonify({'trades': trades, 'count': len(trades)})

    if fmt == 'html':
        rows = ''.join(
            '<tr>' + ''.join(f'<td>{t.get(k, "")}</td>' for k in fields) + '</tr>'
            for t in trades
        )
        html = (
            '<!doctype html><html><head><meta charset="utf-8"><title>Trade Journal</title>'
            '<style>body{font-family:Segoe UI,Arial,sans-serif;background:#0d1117;color:#ddd;padding:16px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #2a2f3a;padding:6px;text-align:left}th{background:#161b22;color:#9ecbff}</style>'
            '</head><body><h2>Trade Journal Export</h2><table><thead><tr>'
            + ''.join(f'<th>{f}</th>' for f in fields) + '</tr></thead><tbody>' + rows + '</tbody></table></body></html>'
        )
        resp = make_response(html)
        resp.headers['Content-Type'] = 'text/html; charset=utf-8'
        resp.headers['Content-Disposition'] = f'attachment; filename="trade_journal_{datetime.now().strftime("%Y%m%d")}.html"'
        return resp

    # Default CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for t in trades:
        row = dict(t)
        row['bot'] = _format_bot_label(_trade_bot_key(t))
        writer.writerow({k: row.get(k, '') for k in fields})
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = f'attachment; filename="trade_journal_{datetime.now().strftime("%Y%m%d")}.csv"'
    return resp


@app.route('/api/config_history')
def api_config_history():
    limit = max(1, min(int(request.args.get('limit', 100) or 100), 300))
    rows = bot_state.get('config_history', [])[:limit]
    return jsonify({'count': len(rows), 'history': rows})


@app.route('/api/watchdogs')
def api_watchdogs():
    return jsonify({
        'snapshot': _watchdog_status_snapshot(),
        'state': bot_state.get('watchdogs', {}),
    })


@app.route('/api/emergency_pause', methods=['GET', 'POST'])
def api_emergency_pause():
    global trading_enabled
    if request.method == 'GET':
        return jsonify(bot_state.get('emergency_pause', {}))

    data = request.get_json() or {}
    action = str(data.get('action', 'activate') or 'activate').lower().strip()
    reason = str(data.get('reason', '') or '').strip()
    close_positions = bool(data.get('close_positions', False))

    if action not in ('activate', 'release'):
        return jsonify({'success': False, 'error': 'action must be activate or release'}), 400

    before = copy.deepcopy(bot_state.get('emergency_pause', {}))
    if action == 'activate':
        trading_enabled = False
        bot_state['running'] = False
        gold_state['running'] = False
        gold_state['manual_running'] = False
        daytrade_state['running'] = False
        daytrade_state['manual_running'] = False
        bot_state['emergency_pause'] = {
            'active': True,
            'reason': reason or 'Manual emergency pause',
            'triggered_at': _now_iso(),
            'close_positions': close_positions,
        }
        if close_positions:
            try:
                positions = mt5.positions_get() or []
                for pos in positions:
                    close_position(pos)
            except Exception:
                pass
        msg = f"Emergency pause activated. Close positions={close_positions}. Reason={bot_state['emergency_pause']['reason']}"
        add_log(msg, 'ERROR')
        _queue_email('Emergency pause activated', msg)
        _queue_push(msg, title='Emergency Pause')
    else:
        bot_state['emergency_pause'] = {
            'active': False,
            'reason': '',
            'triggered_at': None,
            'close_positions': False,
        }
        msg = 'Emergency pause released. Trading can be re-enabled manually.'
        add_log(msg, 'INFO')
        _queue_email('Emergency pause released', msg)
        _queue_push(msg, title='Emergency Pause')

    _record_config_change('emergency_pause', before, copy.deepcopy(bot_state.get('emergency_pause', {})))
    return jsonify({'success': True, 'state': bot_state.get('emergency_pause', {}), 'trading_enabled': trading_enabled})


# ── App Restart ───────────────────────────────────────────────────────────────

@app.route('/api/restart_app', methods=['POST'])
def restart_app():
    """
    Stop all bots gracefully then restart the entire dashboard process.
    Uses a 1-second timer so the HTTP response is sent before the process exits.
    The new process is launched with the same interpreter + script arguments.
    """
    import sys, subprocess, threading

    def _do_restart():
        import os
        import signal
        # Stop all bots cleanly
        bot_state['running']     = False
        gold_state['running']    = False
        daytrade_state['running'] = False
        add_log('App restart triggered by user.', 'WARNING')
        time.sleep(0.8)   # let response reach browser
        script_path = os.path.abspath(__file__)
        cwd = os.path.dirname(script_path)

        # Ensure only one dashboard instance survives after restart.
        try:
            me = os.getpid()
            if os.name == 'nt':
                cmd = (
                    "Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue "
                    "| Select-Object -ExpandProperty OwningProcess -Unique"
                )
                out = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", cmd],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                for raw in out.splitlines():
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        pid = int(raw)
                    except ValueError:
                        continue
                    if pid != me:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except Exception:
                            pass
        except Exception:
            pass

        # Launch replacement process from absolute path with cache disabled.
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        args = [sys.executable, '-B', script_path]
        creationflags = 0
        startupinfo = None
        if os.name == 'nt':
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            creationflags=creationflags,
            startupinfo=startupinfo,
            close_fds=True,
        )
        os._exit(0)      # hard-exit this process (daemon threads die with it)

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({'success': True, 'message': 'Restarting — page will reload in ~4s'})


if __name__ == '__main__':
    print("\n" + "=" * 65)
    print("ADVANCED MULTI-SYMBOL SCALPER - WEB DASHBOARD")
    print("Strategy: CRT + TBS (M5)")
    print("Symbols: GOLD | EURUSD | GBPUSD | BTCUSD")
    print("=" * 65)
    print("\nOpen http://localhost:5000 in your browser")
    print("Press Ctrl+C to stop\n")

    # ── Pre-initialize MT5 at startup so browser sees connected=True immediately ──
    try:
        if _mt5_ensure():
            _acct = mt5.account_info()
            if _acct:
                bot_state['connected'] = True
                bot_state['account'] = {
                    'login':   int(_acct.login),
                    'server':  str(_acct.server),
                    'balance': float(_acct.balance),
                    'equity':  float(_acct.equity),
                    'profit':  float(_acct.profit),
                }
                for _sk, _sc in SYMBOLS_CONFIG.items():
                    _resolved = _resolve_symbol(_sc.get('symbol', _sk))
                    if _resolved:
                        mt5.symbol_select(_resolved, True)
                try:
                    _validate_account_data(_acct.login)
                    _load_today_history_from_mt5()
                    _load_daily_goal_from_disk(current_login=_acct.login)
                except Exception:
                    pass
                print(f"MT5 connected at startup: Account {_acct.login}")
            else:
                print("MT5 initialized but no account — open MetaTrader 5 and log in.")
        else:
            print("MT5 not available at startup — browser will connect on first load.")
    except Exception as _e:
        print(f"Startup MT5 init skipped: {_e}")

    _start_notification_watchdog()
    _start_session_watchdog()
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
