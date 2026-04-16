#!/usr/bin/env python3
"""
Scalping Bot Desktop App
Runs dashboard.py in the background and opens a native desktop window.
"""

import os
import sys
import time
import subprocess
import urllib.request
import urllib.error
from shutil import which


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_PATH = os.path.join(BASE_DIR, 'dashboard.py')
SERVER_URL = 'http://127.0.0.1:5000'
STATUS_URL = SERVER_URL + '/api/status'
CONNECT_URL = SERVER_URL + '/api/connect'
EDGE_EXE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


def _python_exe():
    venv_py = os.path.join(BASE_DIR, '.venv', 'Scripts', 'python.exe')
    if os.path.exists(venv_py):
        return venv_py
    return sys.executable


def _http_get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode('utf-8', errors='ignore')


def _http_post(url, timeout=5):
    req = urllib.request.Request(url=url, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode('utf-8', errors='ignore')


def is_server_up():
    try:
        status, _ = _http_get(STATUS_URL, timeout=3)
        return status == 200
    except Exception:
        return False


def wait_for_server(seconds=45):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if is_server_up():
            return True
        time.sleep(0.5)
    return False


def start_dashboard_server():
    if is_server_up():
        return None

    if not os.path.exists(DASHBOARD_PATH):
        raise FileNotFoundError('dashboard.py not found in project folder')

    flags = 0
    startupinfo = None
    if os.name == 'nt':
        flags = subprocess.CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    proc = subprocess.Popen(
        [_python_exe(), '-B', DASHBOARD_PATH],
        cwd=BASE_DIR,
        creationflags=flags,
        startupinfo=startupinfo,
    )

    if not wait_for_server(seconds=60):
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError('Dashboard server failed to start in time')

    return proc


def try_connect_mt5():
    try:
        _http_post(CONNECT_URL, timeout=10)
    except Exception:
        # Non-fatal: user can still connect manually from the desktop UI.
        pass


def find_edge_exe():
    if os.path.exists(EDGE_EXE):
        return EDGE_EXE
    discovered = which('msedge.exe') or which('msedge')
    return discovered


def launch_edge_app_mode():
    edge = find_edge_exe()
    if not edge:
        return None

    profile_dir = os.path.join(BASE_DIR, '.desktop_profile')
    os.makedirs(profile_dir, exist_ok=True)

    args = [
        edge,
        f'--app={SERVER_URL}',
        '--new-window',
        '--window-size=1440,900',
        '--window-position=40,40',
        f'--user-data-dir={profile_dir}',
        '--no-first-run',
        '--disable-features=TranslateUI',
    ]

    startupinfo = None
    creationflags = 0
    if os.name == 'nt':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    return subprocess.Popen(
        args,
        cwd=BASE_DIR,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def run_desktop_app():
    server_proc = start_dashboard_server()
    try_connect_mt5()

    edge_proc = launch_edge_app_mode()
    if edge_proc is None:
        try:
            import webview
        except Exception:
            print('Missing native browser runtime. Install Microsoft Edge or pywebview.')
            sys.exit(1)

        window = webview.create_window(
            title='Scalping Bot Desktop',
            url=SERVER_URL,
            width=1400,
            height=900,
            min_size=(1100, 700),
            text_select=True,
        )
        webview.start()
    else:
        try:
            edge_proc.wait()
        except KeyboardInterrupt:
            pass

    if server_proc is not None:
        try:
            server_proc.terminate()
        except Exception:
            pass


if __name__ == '__main__':
    run_desktop_app()
