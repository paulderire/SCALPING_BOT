"""
Scalping Bot - System Tray Desktop App
=======================================
Runs the bot and dashboard silently in the background.
Right-click the tray icon to control everything.

No terminal needed!
"""

import sys
import os
import time
import threading
import subprocess
import webbrowser
import requests
from pathlib import Path
from datetime import datetime

# ── Ensure we're in the right directory ──
BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)

# ── Add venv to path if needed ──
venv_scripts = BASE_DIR / ".venv" / "Scripts"
if venv_scripts.exists():
    os.environ["PATH"] = str(venv_scripts) + os.pathsep + os.environ.get("PATH", "")

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pystray", "pillow", "-q"])
    import pystray
    from PIL import Image, ImageDraw, ImageFont

# ── Global state ──
bot_process     = None
dashboard_process = None
status = {
    "bot_running":       False,
    "dashboard_running": False,
    "balance":           "–",
    "daily_pnl":         "–",
    "positions":         0,
    "trades":            0,
    "last_update":       "Never",
}
tray_icon = None

DASHBOARD_URL = "http://localhost:5000"
PYTHON = sys.executable
if (BASE_DIR / ".venv" / "Scripts" / "pythonw.exe").exists():
    PYTHON = str(BASE_DIR / ".venv" / "Scripts" / "pythonw.exe")
elif (BASE_DIR / ".venv" / "Scripts" / "python.exe").exists():
    PYTHON = str(BASE_DIR / ".venv" / "Scripts" / "python.exe")


# ══════════════════════════════════════════
#  ICON GENERATION (drawn on canvas)
# ══════════════════════════════════════════

def make_icon(color="#00cc44", letter="B"):
    """Create a 64×64 tray icon with a coloured circle + letter."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Background circle
    draw.ellipse([2, 2, size - 2, size - 2], fill=color, outline="white", width=2)
    # Centre letter
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 2), letter, fill="white", font=font)
    return img


def get_icon():
    """Return icon matching current state."""
    if status["bot_running"] and status["dashboard_running"]:
        return make_icon("#00cc44", "▶")   # green  – both running
    elif status["bot_running"]:
        return make_icon("#ff9900", "B")   # orange – bot only
    elif status["dashboard_running"]:
        return make_icon("#3399ff", "D")   # blue   – dash only
    else:
        return make_icon("#cc2200", "■")   # red    – stopped


# ══════════════════════════════════════════
#  PROCESS MANAGEMENT
# ══════════════════════════════════════════

def _run_script(script_name):
    """Start a python script as a hidden subprocess and return it."""
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    return subprocess.Popen(
        [PYTHON, "-B", str(BASE_DIR / script_name)],
        cwd=str(BASE_DIR),
        env=env,
        startupinfo=startupinfo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_bot(icon=None, item=None):
    global bot_process
    if status["bot_running"]:
        return
    bot_process = _run_script("headless_bot.py")
    status["bot_running"] = True
    _refresh_icon()
    _notify("Bot started", "Headless trading bot is now running.")


def stop_bot(icon=None, item=None):
    global bot_process
    if bot_process and bot_process.poll() is None:
        bot_process.terminate()
        bot_process.wait(timeout=5)
    bot_process = None
    status["bot_running"] = False
    _refresh_icon()
    _notify("Bot stopped", "Headless trading bot was stopped.")


def start_dashboard(icon=None, item=None):
    global dashboard_process
    if status["dashboard_running"]:
        webbrowser.open(DASHBOARD_URL)
        return
    dashboard_process = _run_script("dashboard.py")
    status["dashboard_running"] = True
    _refresh_icon()
    # Give Flask a moment then open browser
    threading.Thread(target=_open_browser_delayed, daemon=True).start()


def _open_browser_delayed():
    time.sleep(3)
    webbrowser.open(DASHBOARD_URL)


def stop_dashboard(icon=None, item=None):
    global dashboard_process
    if dashboard_process and dashboard_process.poll() is None:
        dashboard_process.terminate()
        dashboard_process.wait(timeout=5)
    dashboard_process = None
    status["dashboard_running"] = False
    _refresh_icon()


def open_dashboard_browser(icon=None, item=None):
    webbrowser.open(DASHBOARD_URL)


def start_all(icon=None, item=None):
    start_bot()
    start_dashboard()


def stop_all(icon=None, item=None):
    stop_bot()
    stop_dashboard()


def quit_app(icon=None, item=None):
    stop_all()
    if tray_icon:
        tray_icon.stop()


# ══════════════════════════════════════════
#  STATUS POLLING
# ══════════════════════════════════════════

def _poll_status():
    """Background thread – poll dashboard API every 10 s."""
    while True:
        try:
            if status["dashboard_running"]:
                r = requests.get(f"{DASHBOARD_URL}/api/status", timeout=3)
                if r.status_code == 200:
                    data = r.json()
                    acct = data.get("account") or {}
                    status["balance"]   = f"${float(acct.get('balance', 0)):.2f}" if acct else "–"
                    status["positions"] = int(data.get("total_positions", 0))
                    stats = data.get("stats") or {}
                    status["trades"]    = int(stats.get("trades_opened", 0))
                    daily = data.get("daily_goal") or {}
                    pnl   = daily.get("current_pnl", None)
                    status["daily_pnl"] = f"${float(pnl):.2f}" if pnl is not None else "–"
                    status["last_update"] = datetime.now().strftime("%H:%M:%S")
        except Exception:
            pass

        # Check processes are still alive
        if bot_process and bot_process.poll() is not None:
            status["bot_running"] = False
            _refresh_icon()
        if dashboard_process and dashboard_process.poll() is not None:
            status["dashboard_running"] = False
            _refresh_icon()

        time.sleep(10)


def _refresh_icon():
    if tray_icon:
        tray_icon.icon  = get_icon()
        tray_icon.title = _build_tooltip()
        tray_icon.update_menu()


def _build_tooltip():
    bot_s  = "Running" if status["bot_running"]       else "Stopped"
    dash_s = "Running" if status["dashboard_running"] else "Stopped"
    lines = [
        "Scalping Bot",
        f"Bot: {bot_s}  |  Dashboard: {dash_s}",
        f"Balance: {status['balance']}  Pos: {status['positions']}",
        f"Trades: {status['trades']}  P&L: {status['daily_pnl']}",
        f"Updated: {status['last_update']}",
    ]
    return "\n".join(lines)


def _notify(title, msg):
    """Show a tray notification if supported."""
    try:
        if tray_icon:
            tray_icon.notify(msg, title)
    except Exception:
        pass


# ══════════════════════════════════════════
#  TRAY MENU
# ══════════════════════════════════════════

def _checked_bot(item):
    return status["bot_running"]


def _checked_dash(item):
    return status["dashboard_running"]


def build_menu():
    return pystray.Menu(
        pystray.MenuItem("── Scalping Bot ──", None, enabled=False),
        pystray.Menu.SEPARATOR,

        pystray.MenuItem(
            "▶  Start Bot",
            start_bot,
            enabled=lambda item: not status["bot_running"],
        ),
        pystray.MenuItem(
            "⏹  Stop Bot",
            stop_bot,
            enabled=lambda item: status["bot_running"],
        ),
        pystray.Menu.SEPARATOR,

        pystray.MenuItem(
            "🌐  Start Dashboard",
            start_dashboard,
            enabled=lambda item: not status["dashboard_running"],
        ),
        pystray.MenuItem(
            "🔗  Open in Browser",
            open_dashboard_browser,
            enabled=lambda item: status["dashboard_running"],
        ),
        pystray.MenuItem(
            "⏹  Stop Dashboard",
            stop_dashboard,
            enabled=lambda item: status["dashboard_running"],
        ),
        pystray.Menu.SEPARATOR,

        pystray.MenuItem("▶▶  Start Both",  start_all),
        pystray.MenuItem("⏹⏹  Stop Both",  stop_all),
        pystray.Menu.SEPARATOR,

        pystray.MenuItem(
            lambda text: (
                f"Balance: {status['balance']}  |  "
                f"Pos: {status['positions']}  |  "
                f"P&L: {status['daily_pnl']}"
            ),
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌  Quit", quit_app),
    )


# ══════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════

def main():
    global tray_icon

    # Start status poller
    threading.Thread(target=_poll_status, daemon=True).start()

    # Build tray
    tray_icon = pystray.Icon(
        name="ScalpingBot",
        icon=get_icon(),
        title=_build_tooltip(),
        menu=build_menu(),
    )

    # Auto-start dashboard only — bot must be started manually via tray menu
    threading.Thread(target=start_dashboard, daemon=True).start()

    tray_icon.run()


if __name__ == "__main__":
    main()
