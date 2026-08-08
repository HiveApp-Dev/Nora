#!/usr/bin/env python3
"""
PnL Calendar for Alpaca (via Alpaca's official REST API)
----------------------------------------------------------------------
Pulls your day-by-day portfolio equity from Alpaca's OFFICIAL,
documented "Portfolio History" endpoint and serves the same
interactive calendar as the SnapTrade/Robinhood versions.

Why this is better than the Robinhood version:
  - Alpaca publishes and supports this API. There's no ToS risk, no
    mimicking app traffic, and no fragile guessing at undocumented
    field names -- the portfolio-history response shape is stable.
  - Auth is a scoped API key/secret pair (from your Alpaca dashboard),
    not your actual account username/password.
  - Alpaca's endpoint natively returns daily (or intraday) equity
    marks for the exact date range you ask for, which is generally
    more complete/reliable calendar data than reverse-engineered
    historicals.

IMPORTANT -- read before using:
  - You need an Alpaca account and an API key/secret pair, generated
    from your Alpaca dashboard (Paper or Live -- your choice, see
    --paper below). Never share these keys; treat them like a
    password.
  - This script reads your key/secret from environment variables
    (preferred) or prompts you for them. Neither is ever written to
    disk by this script.
  - This only covers ONE Alpaca account per run (whichever key/secret
    you provide). If you have both a paper and a live account, or
    multiple live accounts, run the script once per account and note
    that only one account's data feeds the calendar at a time --
    there's no built-in multi-account merge here (unlike the
    SnapTrade version, which merges brokerage-linked accounts).
  - Alpaca's portfolio history is only populated once there's been
    at least one day of activity/equity in the account.

Get API keys:
    https://app.alpaca.markets/paper/dashboard/overview  (paper trading keys)
    https://app.alpaca.markets/live/dashboard/overview    (live trading keys)

Install:
    pip install requests

Usage:
    python pnl_calendar_alpaca.py
        Launches straight into a numbered menu where you can switch
        Live <-> Paper, period, timeframe, date range,
        browser, and port before it connects to anything.

    python pnl_calendar_alpaca.py --no-menu --paper
        Skips the menu and runs immediately with whatever flags you
        pass (useful for scripting) -- flags below match the menu
        settings 1:1 and are used as the starting values if you DO
        use the menu.
    python pnl_calendar_alpaca.py --no-menu --period 1A --timeframe 1D
    python pnl_calendar_alpaca.py --no-menu --start-date 2026-01-01 --end-date 2026-08-08

Env vars (skip the interactive prompts):
    ALPACA_API_KEY_ID=...
    ALPACA_API_SECRET_KEY=...
"""

import argparse
import csv
import datetime as dt
import getpass
import http.server
import importlib.metadata
import json
import os
import socketserver
import subprocess
import sys
import threading
import traceback
import webbrowser
import zoneinfo


REQUIRED_PACKAGES = {
    "requests": "requests",
    # zoneinfo (stdlib) has no bundled tz database on Windows -- it falls
    # back to this pip package automatically when present. Harmless to
    # install on Linux/Mac too, where the system database is used instead.
    "tzdata": "tzdata",
}


def _installed_version(import_name):
    try:
        return importlib.metadata.version(import_name)
    except importlib.metadata.PackageNotFoundError:
        return None


def ensure_dependencies():
    missing = [pip_name for import_name, pip_name in REQUIRED_PACKAGES.items()
               if _installed_version(import_name) is None]
    if not missing:
        return
    print("=" * 60)
    print(f"Missing required package(s): {', '.join(missing)}")
    print("Installing now with pip...")
    print("=" * 60)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
    except subprocess.CalledProcessError as e:
        print(f"[!] Failed to auto-install {missing}: {e}")
        print(f"    Try running manually:  {sys.executable} -m pip install {' '.join(missing)}")
        pause_before_exit()
        sys.exit(1)
    try:
        __import__("requests")
    except ImportError:
        print("[!] Still couldn't import requests after installing.")
        pause_before_exit()
        sys.exit(1)
    print("[+] Dependencies installed successfully.\n")


def pause_before_exit():
    try:
        input("\nPress Enter to close...")
    except (EOFError, KeyboardInterrupt):
        pass


PAGE_TITLE = "Nora"

# Alpaca's daily-bar timestamps land near market close (4pm ET), which is
# close enough to the UTC day boundary that converting to a date in UTC
# can push a trading day's data onto the *next* calendar date. Extracting
# the date in the exchange's own timezone (US Eastern) avoids that shift.
# Looked up lazily (not at import time) so a missing tz database can't
# crash the script before ensure_dependencies() gets a chance to fix it.
_exchange_tz_cache = None


def get_exchange_tz():
    global _exchange_tz_cache
    if _exchange_tz_cache is None:
        try:
            _exchange_tz_cache = zoneinfo.ZoneInfo("America/New_York")
        except zoneinfo.ZoneInfoNotFoundError:
            print("[!] Couldn't load the America/New_York timezone database.")
            print("    Try:  pip install tzdata")
            pause_before_exit()
            sys.exit(1)
    return _exchange_tz_cache

LIVE_BASE_URL = "https://api.alpaca.markets"
PAPER_BASE_URL = "https://paper-api.alpaca.markets"


# --------------------------------------------------------------------------
# Alpaca auth (official REST API -- scoped API key/secret, not your login)
# --------------------------------------------------------------------------

class AlpacaSession:
    def __init__(self, key_id, secret_key, base_url):
        import requests
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        })

    def get(self, path, params=None):
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        return resp


def verify_alpaca_credentials(key_id, secret_key, use_paper):
    """Verifies a key/secret pair against Alpaca's /v2/account endpoint.
    Returns (session, error_message) -- exactly one of the two is None."""
    base_url = PAPER_BASE_URL if use_paper else LIVE_BASE_URL
    session = AlpacaSession(key_id, secret_key, base_url)
    try:
        resp = session.get("/v2/account")
    except Exception as e:
        return None, f"Could not reach Alpaca: {e}"

    if resp.status_code in (401, 403):
        return None, (f"Authentication failed ({resp.status_code}). Double-check the key/secret, "
                       f"and that they match the {'paper' if use_paper else 'live'} environment.")
    if not resp.ok:
        return None, f"Unexpected response verifying account ({resp.status_code}): {resp.text[:300]}"

    account = resp.json()
    label = account.get("account_number") or account.get("id") or "Alpaca account"
    print(f"[+] Logged in to Alpaca ({'paper' if use_paper else 'live'}) -- account {label}.")
    return session, None


def alpaca_login_interactive(use_paper):
    """Prompts for a key/secret (or reads env vars) and verifies them.
    Used as a one-off fallback when no saved account is selected."""
    key_id = os.environ.get("ALPACA_API_KEY_ID") or input("Alpaca API Key ID: ").strip()
    secret_key = os.environ.get("ALPACA_API_SECRET_KEY") or getpass.getpass("Alpaca API Secret Key (hidden input): ")

    if not key_id or not secret_key:
        print("[!] API Key ID and Secret Key are both required. Exiting.")
        pause_before_exit()
        sys.exit(1)

    print(f"[+] Verifying Alpaca credentials against {'paper' if use_paper else 'live'} ...")
    session, error = verify_alpaca_credentials(key_id, secret_key, use_paper)
    if error:
        print(f"[!] {error}")
        pause_before_exit()
        sys.exit(1)
    return session, key_id, secret_key


def alpaca_login_with_account(account):
    """Logs in using a saved account dict ({label, paper, key_id, secret_key})."""
    print(f"[+] Verifying saved account '{account['label']}' "
          f"({'paper' if account['paper'] else 'live'}) ...")
    session, error = verify_alpaca_credentials(account["key_id"], account["secret_key"], account["paper"])
    if error:
        print(f"[!] {error}")
        print(f"    The saved account '{account['label']}' may have a revoked or incorrect key. "
              f"Use the Account menu to remove or re-add it.")
        pause_before_exit()
        sys.exit(1)
    return session


# --------------------------------------------------------------------------
# Saved account storage (remembers API key/secret pairs locally)
# --------------------------------------------------------------------------

CREDENTIALS_FILE = os.path.join(os.path.expanduser("~"), ".nora_alpaca_accounts.json")


def load_accounts():
    """Returns (accounts_list, last_selected_label). Tolerates a missing
    or corrupt file by returning an empty list rather than crashing."""
    if not os.path.exists(CREDENTIALS_FILE):
        return [], None
    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = data.get("accounts", [])
        accounts = [a for a in accounts if isinstance(a, dict) and "key_id" in a and "secret_key" in a]
        return accounts, data.get("last_selected")
    except (OSError, json.JSONDecodeError):
        print(f"[!] Could not read saved accounts from {CREDENTIALS_FILE} -- ignoring it.")
        return [], None


def save_accounts(accounts, last_selected):
    """Writes accounts to disk. Note: keys/secrets are stored in plain
    JSON in your home directory (permissions restricted to your user
    where the OS supports it) -- this is meant for a single-user
    machine, not a shared one."""
    try:
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts, "last_selected": last_selected}, f, indent=2)
        try:
            os.chmod(CREDENTIALS_FILE, 0o600)
        except OSError:
            pass  # best-effort on platforms that don't support chmod (e.g. Windows)
    except OSError as e:
        print(f"[!] Could not save accounts to {CREDENTIALS_FILE}: {e}")




def fetch_daily_equity_history(session, period="1A", timeframe="1D"):
    """Pulls equity history via Alpaca's official
    GET /v2/account/portfolio/history endpoint. Returns
    (series, raw_sample, base_value) where series is
    {"YYYY-MM-DD": float}, raw_sample is the raw JSON response (kept
    for debugging), and base_value is Alpaca's own baseline equity
    figure for the period (the value right before the first returned
    day) -- used to anchor the day-over-day change calc for the very
    first day in the series, which otherwise has nothing to diff
    against.
    """
    params = {
        "period": period,
        "timeframe": timeframe,
        "extended_hours": "false",
    }
    try:
        resp = session.get("/v2/account/portfolio/history", params=params)
    except Exception as e:
        print(f"[!] Failed to fetch portfolio history: {e}")
        return {}, None, None

    if not resp.ok:
        print(f"[!] Alpaca returned an error fetching portfolio history "
              f"({resp.status_code}): {resp.text[:300]}")
        return {}, None, None

    data = resp.json()
    timestamps = data.get("timestamp") or []
    equity = data.get("equity") or []
    base_value = data.get("base_value")

    if not timestamps or not equity:
        print("[!] Alpaca returned no portfolio history data. This is normal for a")
        print("    brand-new account with no trading activity yet.")
        return {}, data, base_value

    series = {}
    unmatched = 0
    for ts, eq in zip(timestamps, equity):
        if eq is None:
            unmatched += 1
            continue
        try:
            # Alpaca timestamps are Unix seconds (UTC), marking end-of-bar.
            date_str = dt.datetime.fromtimestamp(ts, tz=get_exchange_tz()).date().isoformat()
            series[date_str] = float(eq)
        except (TypeError, ValueError, OSError):
            unmatched += 1
            continue

    if unmatched:
        print(f"[!] Parsed {len(series)} day(s); skipped {unmatched} record(s) with no equity value "
              f"(e.g. days the market was closed with no prior mark).")

    return series, data, base_value


def fill_calendar_gaps(values):
    """Forward-fills interior gaps in a {date: value} series so every
    calendar day between the first and last known date has a value."""
    if not values:
        return {}, {}
    dates = sorted(values.keys())
    start = dt.date.fromisoformat(dates[0])
    end = dt.date.fromisoformat(dates[-1])

    filled_values, filled_from = {}, {}
    last_val, last_real_date = None, None
    cur = start
    one_day = dt.timedelta(days=1)
    while cur <= end:
        key = cur.isoformat()
        if key in values:
            last_val = values[key]
            last_real_date = key
            filled_from[key] = None
        else:
            filled_from[key] = last_real_date
        filled_values[key] = last_val
        cur += one_day
    return filled_values, filled_from


def compute_daily_portfolio_change(values_by_date, start_date=None, end_date=None, filled_from=None):
    filled_from = filled_from or {}
    all_dates = sorted(values_by_date.keys())
    daily = {}
    prev_value = None
    for date in all_dates:
        value = values_by_date[date]
        if prev_value is not None:
            change = value - prev_value
            pct = (change / prev_value * 100.0) if prev_value else 0.0
            in_range = (not start_date or date >= start_date) and (not end_date or date <= end_date)
            if in_range:
                daily[date] = {
                    "value": round(value, 2),
                    "change": round(change, 2),
                    "pct": round(pct, 2),
                    "filled_from": filled_from.get(date),
                }
        prev_value = value
    return daily


def write_debug_csv(directory, raw_series, daily_change):
    path = os.path.join(directory, "debug_alpaca_daily.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "portfolio_value", "dollar_change", "pct_change"])
        for date in sorted(raw_series.keys()):
            entry = daily_change.get(date)
            w.writerow([date, raw_series[date],
                        entry["change"] if entry else "",
                        entry["pct"] if entry else ""])
    return path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #0f1117; --panel: rgba(22,25,35,0.72); --panel-2: rgba(29,33,48,0.85); --border: rgba(255,255,255,0.08);
    --text: #e7e9ee; --muted: #9096a8;
    --green: #16a34a; --green-bg: rgba(22,163,74,0.18);
    --red: #dc2626; --red-bg: rgba(220,38,38,0.18);
    --accent: #6366f1; --dim: 0.72;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg); color:var(--text); min-height:100vh; padding:18px; }
  #bgLayer { position:fixed; inset:0; z-index:-2; background-size:cover; background-position:center; background-repeat:no-repeat; }
  #bgVideoLayer { position:fixed; inset:0; z-index:-2; overflow:hidden; display:none; background:#000; }
  #bgVideoLayer iframe { position:absolute; top:50%; left:50%; width:100vw; height:56.25vw;
    min-height:100vh; min-width:177.78vh; transform:translate(-50%,-50%); pointer-events:none; border:0; }
  #bgOverlay { position:fixed; inset:0; z-index:-1; background: rgba(10,11,16, var(--dim)); transition: background .2s ease; }
  .wrap { max-width: 840px; margin: 0 auto; }
  header { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; flex-wrap:wrap; gap:10px; }
  h1 { font-size:18px; margin:0; font-weight:700; letter-spacing:.01em; }
  .sub { color: var(--muted); font-size: 11px; margin-top: 1px; }
  .nav { display:flex; align-items:center; gap:6px; }
  .nav button, .icon-btn { background:var(--panel-2); border:1px solid var(--border); color:var(--text);
    border-radius:7px; padding:6px 10px; cursor:pointer; font-size:13px; backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); }
  .nav button:hover, .icon-btn:hover { background:var(--accent); border-color:var(--accent); }
  .month-label { font-size:14px; font-weight:600; min-width:128px; text-align:center; }
  .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px; margin-bottom:14px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:9px; padding:10px 12px;
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); }
  .card .label { color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.04em; }
  .card .value { font-size:17px; font-weight:700; margin-top:2px; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  .grid { display:grid; grid-template-columns:repeat(7,1fr); gap:6px; }
  .dow { text-align:center; color:var(--muted); font-size:11px; padding-bottom:3px; }
  .day { background:var(--panel); border:1px solid var(--border); border-radius:8px; min-height:58px;
    padding:6px; cursor:pointer; display:flex; flex-direction:column; justify-content:space-between;
    transition:transform .06s ease, border-color .15s ease; position:relative;
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); }
  .day:hover { border-color:var(--accent); transform:translateY(-1px); }
  .day.empty { background:transparent; border:none; cursor:default; backdrop-filter:none; }
  .day.empty:hover { transform:none; }
  .day .num { font-size:11px; color:var(--muted); }
  .day .pnl { font-size:12px; font-weight:700; align-self:flex-end; line-height:1.1; }
  .day .pnl.muted { color: var(--muted); font-weight:500; }
  .day .pnl.flat { color: var(--muted); font-weight:600; }
  .day.win { background:var(--green-bg); border-color:rgba(22,163,74,0.4); }
  .day.loss { background:var(--red-bg); border-color:rgba(220,38,38,0.4); }
  .day.filled { position: relative; }
  .day.filled::after { content:""; position:absolute; top:5px; right:5px; width:5px; height:5px; border-radius:50%; background:var(--muted); opacity:0.55; }
  .day.today .num { color:var(--accent); font-weight:700; }
  .modal-backdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6);
    align-items:center; justify-content:center; z-index:50; padding:16px; }
  .modal-backdrop.open { display:flex; }
  .modal { background:var(--panel-2); border:1px solid var(--border); border-radius:12px; padding:20px; width:320px;
    max-height:80vh; overflow-y:auto; backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px); }
  .modal h3 { margin:0 0 4px 0; font-size:15px; }
  .modal .src { color:var(--muted); font-size:12px; margin-bottom:12px; }
  .modal input[type="text"], .modal input[type="url"] { width:100%; padding:9px 11px; border-radius:8px; border:1px solid var(--border);
    background:var(--bg); color:var(--text); font-size:13px; margin-bottom:10px; }
  .modal label.field-label { display:block; font-size:11px; color:var(--muted); margin-bottom:5px; text-transform:uppercase; letter-spacing:.03em; }
  .modal .row { display:flex; gap:8px; margin-top:4px; }
  .modal button { flex:1; padding:9px; border-radius:8px; border:1px solid var(--border);
    cursor:pointer; font-size:13px; background:var(--panel); color:var(--text); }
  .modal button.primary { background:var(--accent); border-color:var(--accent); color:white; }
  .modal button.danger { background:transparent; color:var(--red); border-color:var(--red); }
  .day-pnl-line { font-size:22px; font-weight:700; margin: 4px 0 14px 0; }
  .trade-row { display:flex; justify-content:space-between; align-items:center; padding:7px 0;
    border-bottom:1px solid var(--border); font-size:13px; }
  .trade-row:last-child { border-bottom:none; }
  .empty-state { color:var(--muted); font-size:13px; padding:8px 0; }
  .acct-row { display:flex; align-items:center; gap:9px; padding:8px 2px; border-bottom:1px solid var(--border); font-size:13px; }
  .acct-row:last-child { border-bottom:none; }
  .acct-row input { width:16px; height:16px; accent-color: var(--accent); flex-shrink:0; }
  .acct-row label { cursor:pointer; flex:1; }
  footer { margin-top:18px; color:var(--muted); font-size:11px; text-align:center; }
  input[type="range"] { width:100%; accent-color: var(--accent); margin-bottom:12px; }
</style>
</head>
<body>
<div id="bgLayer"></div>
<div id="bgVideoLayer"><div id="ytPlayer"></div></div>
<div id="bgOverlay"></div>
<div class="wrap">
  <header>
    <div>
      <h1>__TITLE__</h1>
    </div>
    <div class="nav">
      <button id="prevBtn">&#8592;</button>
      <div class="month-label" id="monthLabel"></div>
      <button id="nextBtn">&#8594;</button>
      <button id="todayBtn">Today</button>
      <button class="icon-btn" id="accountsBtn" title="Accounts">&#128101;</button>
      <button class="icon-btn" id="bgSoundBtn" title="Toggle background sound" style="display:none;">&#128264;</button>
      <button class="icon-btn" id="settingsBtn" title="Settings">&#9881;</button>
    </div>
  </header>

  <div class="summary">
    <div class="card"><div class="label">Month Change</div><div class="value" id="monthTotal">$0.00</div></div>
    <div class="card"><div class="label">Win Days</div><div class="value" id="winDays">0</div></div>
    <div class="card"><div class="label">Loss Days</div><div class="value" id="lossDays">0</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value" id="winRate">0%</div></div>
  </div>

  <div class="grid" id="dowRow"></div>
  <div class="grid" id="calendarGrid" style="margin-top:6px;"></div>
</div>

<div class="modal-backdrop" id="accountsModalBackdrop">
  <div class="modal">
    <h3>Accounts</h3>
    <div class="src">Choose which connected accounts count toward the calendar. Changes apply instantly &mdash; no need to re-run the script.</div>
    <div id="accountsList"></div>
    <div class="row">
      <button id="accountsAllBtn">Select all</button>
      <button class="primary" id="accountsDoneBtn">Done</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="dayModalBackdrop">
  <div class="modal">
    <h3 id="dayModalDate"></h3>
    <div class="day-pnl-line" id="dayModalPnl"></div>
    <div id="dayModalDetail"></div>
    <div class="row"><button id="dayModalClose">Close</button></div>
  </div>
</div>

<div class="modal-backdrop" id="settingsModalBackdrop">
  <div class="modal">
    <h3>Settings</h3>
    <div class="src">Custom background &mdash; paste any image or GIF URL (e.g. right-click a Giphy GIF and "Copy image address").</div>
    <label class="field-label" for="bgUrlInput">Background image / GIF URL</label>
    <input type="url" id="bgUrlInput" placeholder="https://media.giphy.com/media/…/giphy.gif">
    <div class="src">Or loop a YouTube video behind the calendar instead. It plays muted by default &mdash; use the speaker button in the toolbar to turn sound on.</div>
    <label class="field-label" for="bgYoutubeInput">YouTube background video URL</label>
    <input type="url" id="bgYoutubeInput" placeholder="https://www.youtube.com/watch?v=…">
    <label class="field-label" for="bgDimInput">Overlay darkness</label>
    <input type="range" id="bgDimInput" min="0" max="95" value="72">
    <div class="row">
      <button class="danger" id="bgClearBtn">Clear</button>
      <button id="settingsCancelBtn">Cancel</button>
      <button class="primary" id="settingsSaveBtn">Save</button>
    </div>
  </div>
</div>

<script src="https://www.youtube.com/iframe_api" async></script>
<script>
const SETTINGS_KEY = "nora_settings_v1";
const dows = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];

// Data computed server-side from your real Alpaca portfolio history.
// ACCOUNTS: [{id, label}, ...] for every fetched account.
// RAW: {account_id: {"YYYY-MM-DD": total_value}} -- outlier-cleaned per-account series.
// Merging, gap-filling, and day-over-day change are all computed here in
// JS (mirroring the Python functions of the same name) so toggling
// accounts in the Accounts menu recomputes instantly, no re-run needed.
const ACCOUNTS = __ACCOUNTS_JSON__;
const RAW = __RAW_HISTORY_JSON__;
const START_DATE = __START_DATE_JSON__;
const END_DATE = __END_DATE_JSON__;
const ACCOUNTS_KEY = "nora_accounts_v1";

let settings = {};
try { settings = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"); } catch (e) { settings = {}; }

function loadSelectedAccountIds() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(ACCOUNTS_KEY) || "null"); } catch (e) { saved = null; }
  if (!Array.isArray(saved)) return ACCOUNTS.map(a => a.id); // default: all selected
  // Keep only ids that still exist (accounts can be added/removed between runs).
  const validIds = new Set(ACCOUNTS.map(a => a.id));
  const filtered = saved.filter(id => validIds.has(id));
  return filtered.length ? filtered : ACCOUNTS.map(a => a.id);
}
function saveSelectedAccountIds(ids) {
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(ids));
}
let selectedAccountIds = loadSelectedAccountIds();

// --- Merge / fill / compute (mirrors the Python functions of the same name) ---
function mergeAccountHistories(rawByAccount, accountIds) {
  const idSet = new Set(accountIds);
  const relevant = accountIds.filter(id => rawByAccount[id]);
  if (!relevant.length) return {};
  const allDates = new Set();
  relevant.forEach(id => Object.keys(rawByAccount[id]).forEach(d => allDates.add(d)));
  const sortedDates = Array.from(allDates).sort();
  const lastKnown = {};
  relevant.forEach(id => lastKnown[id] = null);
  const merged = {};
  for (const date of sortedDates) {
    let total = 0, haveAny = false;
    for (const id of relevant) {
      const series = rawByAccount[id];
      if (series[date] !== undefined) lastKnown[id] = series[date];
      if (lastKnown[id] !== null) { total += lastKnown[id]; haveAny = true; }
    }
    if (haveAny) merged[date] = total;
  }
  return merged;
}

function fillCalendarGaps(mergedValues) {
  const dates = Object.keys(mergedValues).sort();
  if (!dates.length) return { filled: {}, filledFrom: {} };
  const start = new Date(dates[0] + "T00:00:00");
  const end = new Date(dates[dates.length - 1] + "T00:00:00");
  const filled = {}, filledFrom = {};
  let lastVal = null, lastRealDate = null;
  for (let cur = new Date(start); cur <= end; cur.setDate(cur.getDate() + 1)) {
    const key = cur.toISOString().slice(0, 10);
    if (mergedValues[key] !== undefined) {
      lastVal = mergedValues[key];
      lastRealDate = key;
      filledFrom[key] = null;
    } else {
      filledFrom[key] = lastRealDate;
    }
    filled[key] = lastVal;
  }
  return { filled, filledFrom };
}

function computeDailyChange(valuesByDate, startDate, endDate, filledFrom) {
  const dates = Object.keys(valuesByDate).sort();
  const daily = {};
  let prevValue = null;
  for (const date of dates) {
    const value = valuesByDate[date];
    if (prevValue !== null) {
      const change = value - prevValue;
      const pct = prevValue ? (change / prevValue * 100.0) : 0.0;
      const inRange = (!startDate || date >= startDate) && (!endDate || date <= endDate);
      if (inRange) {
        daily[date] = {
          value: Math.round(value * 100) / 100,
          change: Math.round(change * 100) / 100,
          pct: Math.round(pct * 100) / 100,
          filled_from: filledFrom[date] || null,
        };
      }
    }
    prevValue = value;
  }
  return daily;
}

function recomputeData() {
  const merged = mergeAccountHistories(RAW, selectedAccountIds);
  const { filled, filledFrom } = fillCalendarGaps(merged);
  return computeDailyChange(filled, START_DATE, END_DATE, filledFrom);
}

let DATA = recomputeData();

// --- YouTube background video (muted-loop, with a manual sound toggle) ---
let ytApiReady = false;
let ytPlayer = null;
let pendingYtId = null;
let ytSoundOn = false;

function extractYoutubeId(url) {
  if (!url) return null;
  try {
    const u = new URL(url.trim());
    const host = u.hostname.replace(/^www\\./, "");
    if (host === "youtu.be") return u.pathname.slice(1).split("/")[0] || null;
    if (host === "youtube.com" || host === "m.youtube.com" || host === "music.youtube.com") {
      if (u.searchParams.get("v")) return u.searchParams.get("v");
      const embedMatch = u.pathname.match(/\\/embed\\/([^/?]+)/);
      if (embedMatch) return embedMatch[1];
      const shortsMatch = u.pathname.match(/\\/shorts\\/([^/?]+)/);
      if (shortsMatch) return shortsMatch[1];
      const liveMatch = u.pathname.match(/\\/live\\/([^/?]+)/);
      if (liveMatch) return liveMatch[1];
    }
  } catch (e) { /* not a valid URL */ }
  return null;
}

function updateSoundBtn() {
  const btn = document.getElementById("bgSoundBtn");
  btn.innerHTML = ytSoundOn ? "&#128266;" : "&#128264;";
  btn.title = ytSoundOn ? "Mute background video" : "Unmute background video";
}

function createYtPlayer(id) {
  document.getElementById("bgVideoLayer").style.display = "block";
  document.getElementById("bgSoundBtn").style.display = "";
  ytSoundOn = false;
  updateSoundBtn();
  if (ytPlayer && ytPlayer.loadVideoById) {
    ytPlayer.mute();
    ytPlayer.loadVideoById(id);
    return;
  }
  ytPlayer = new YT.Player("ytPlayer", {
    videoId: id,
    playerVars: {
      autoplay: 1, mute: 1, loop: 1, playlist: id, controls: 0, showinfo: 0,
      modestbranding: 1, disablekb: 1, fs: 0, iv_load_policy: 3, playsinline: 1, rel: 0
    },
    events: {
      onReady: (e) => { e.target.mute(); e.target.playVideo(); },
    }
  });
}

function destroyYoutubeBackground() {
  document.getElementById("bgVideoLayer").style.display = "none";
  document.getElementById("bgSoundBtn").style.display = "none";
  if (ytPlayer && ytPlayer.stopVideo) { try { ytPlayer.stopVideo(); } catch (e) {} }
}

function showYoutubeBackground(id) {
  if (ytApiReady && window.YT && YT.Player) createYtPlayer(id);
  else pendingYtId = id;
}

window.onYouTubeIframeAPIReady = function () {
  ytApiReady = true;
  if (pendingYtId) { createYtPlayer(pendingYtId); pendingYtId = null; }
};

document.getElementById("bgSoundBtn").addEventListener("click", () => {
  if (!ytPlayer || !ytPlayer.unMute) return;
  ytSoundOn = !ytSoundOn;
  if (ytSoundOn) { ytPlayer.unMute(); ytPlayer.setVolume(100); } else { ytPlayer.mute(); }
  updateSoundBtn();
});

function applyBackground() {
  const layer = document.getElementById("bgLayer");
  const dim = (typeof settings.dim === "number" ? settings.dim : 72) / 100;
  document.documentElement.style.setProperty("--dim", dim);
  const ytId = extractYoutubeId(settings.ytUrl);
  if (ytId) {
    layer.style.backgroundImage = "none";
    showYoutubeBackground(ytId);
  } else {
    destroyYoutubeBackground();
    layer.style.backgroundImage = settings.bgUrl ? `url("${settings.bgUrl}")` : "none";
  }
}
applyBackground();

let viewDate = new Date();
viewDate.setDate(1);

const dowRow = document.getElementById("dowRow");
dows.forEach(d => { const el = document.createElement("div"); el.className = "dow"; el.textContent = d; dowRow.appendChild(el); });

const monthLabel = document.getElementById("monthLabel");
const grid = document.getElementById("calendarGrid");

function keyFor(y, m, d) { return `${y}-${String(m+1).padStart(2,"0")}-${String(d).padStart(2,"0")}`; }
function fmtMoney(n) { const sign = n < 0 ? "-" : ""; return sign + "$" + Math.abs(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}); }
function fmtPct(n) { const sign = n >= 0 ? "+" : ""; return sign + n.toFixed(2) + "%"; }

function render() {
  const y = viewDate.getFullYear(), m = viewDate.getMonth();
  monthLabel.textContent = viewDate.toLocaleString(undefined, {month:"long", year:"numeric"});
  const firstDow = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m+1, 0).getDate();
  const today = new Date();
  grid.innerHTML = "";
  for (let i = 0; i < firstDow; i++) { const el = document.createElement("div"); el.className = "day empty"; grid.appendChild(el); }

  let total = 0, wins = 0, losses = 0;
  for (let d = 1; d <= daysInMonth; d++) {
    const k = keyFor(y, m, d);
    const entry = DATA[k];
    const val = entry ? entry.pct : undefined;
    const isFilled = !!(entry && entry.filled_from);
    const el = document.createElement("div");
    el.className = "day";
    if (y === today.getFullYear() && m === today.getMonth() && d === today.getDate()) el.classList.add("today");
    if (typeof val === "number") {
      total += entry.change;
      if (isFilled) {
        el.classList.add("filled");
      } else if (val > 0) { el.classList.add("win"); wins++; }
      else if (val < 0) { el.classList.add("loss"); losses++; }
    }
    // Days with no fresh sync from any account now render like any other
    // day (using their carried-forward value, usually 0.00%) instead of a
    // blank "gap" -- a small dot in the corner is the only visual cue,
    // and clicking the day still explains it was carried forward.
    const pnlHtml = typeof val !== "number" ? "" :
      `<div class="pnl ${val > 0 ? "pos" : val < 0 ? "neg" : "flat"}">${fmtPct(val)}</div>`;
    el.innerHTML = `<div class="num">${d}</div>` + pnlHtml;
    el.addEventListener("click", () => openDayInfo(y, m, d, entry));
    grid.appendChild(el);
  }
  document.getElementById("monthTotal").textContent = fmtMoney(total);
  document.getElementById("monthTotal").className = "value " + (total >= 0 ? "pos" : "neg");
  document.getElementById("winDays").textContent = wins;
  document.getElementById("lossDays").textContent = losses;
  document.getElementById("winRate").textContent = ((wins+losses) > 0 ? Math.round((wins/(wins+losses))*100) : 0) + "%";
}

// --- Day info (read-only) ---
const dayModalBackdrop = document.getElementById("dayModalBackdrop");
const dayModalDate = document.getElementById("dayModalDate");
const dayModalPnl = document.getElementById("dayModalPnl");
const dayModalDetail = document.getElementById("dayModalDetail");

function openDayInfo(y, m, d, entry) {
  const dateObj = new Date(y, m, d);
  dayModalDate.textContent = dateObj.toLocaleDateString(undefined, {weekday:"long", month:"long", day:"numeric", year:"numeric"});

  dayModalDetail.innerHTML = "";

  if (entry && typeof entry.pct === "number") {
    const isFilled = !!entry.filled_from;
    if (isFilled) {
      dayModalPnl.textContent = "No new sync";
      dayModalPnl.className = "day-pnl-line";
      dayModalPnl.style.color = "var(--muted)";
    } else {
      dayModalPnl.textContent = fmtPct(entry.pct);
      dayModalPnl.className = "day-pnl-line " + (entry.pct >= 0 ? "pos" : "neg");
    }

    const rows = [
      ["$ Change", fmtMoney(entry.change), entry.change >= 0 ? "pos" : "neg"],
      ["Portfolio Value", fmtMoney(entry.value), ""],
    ];
    rows.forEach(([label, value, cls]) => {
      const row = document.createElement("div");
      row.className = "trade-row";
      row.innerHTML = `<span>${label}</span><span class="${cls}">${value}</span>`;
      dayModalDetail.appendChild(row);
    });

    if (isFilled) {
      const note = document.createElement("div");
      note.className = "empty-state";
      note.textContent = `None of your accounts synced a fresh value on this day, so it's carried forward from ${entry.filled_from}.`;
      dayModalDetail.appendChild(note);
    }
  } else {
    dayModalPnl.textContent = "No data";
    dayModalPnl.className = "day-pnl-line";
    dayModalPnl.style.color = "var(--muted)";

    const div = document.createElement("div");
    div.className = "empty-state";
    div.textContent = "No portfolio value recorded for this day.";
    dayModalDetail.appendChild(div);
  }

  dayModalBackdrop.classList.add("open");
}
function closeDayInfo() { dayModalBackdrop.classList.remove("open"); }
document.getElementById("dayModalClose").addEventListener("click", closeDayInfo);
dayModalBackdrop.addEventListener("click", (e) => { if (e.target === dayModalBackdrop) closeDayInfo(); });

// --- Settings ---
const settingsModalBackdrop = document.getElementById("settingsModalBackdrop");
const bgUrlInput = document.getElementById("bgUrlInput");
const bgYoutubeInput = document.getElementById("bgYoutubeInput");
const bgDimInput = document.getElementById("bgDimInput");

document.getElementById("settingsBtn").addEventListener("click", () => {
  bgUrlInput.value = settings.bgUrl || "";
  bgYoutubeInput.value = settings.ytUrl || "";
  bgDimInput.value = (typeof settings.dim === "number" ? settings.dim : 72);
  settingsModalBackdrop.classList.add("open");
});
function closeSettings() { settingsModalBackdrop.classList.remove("open"); }
document.getElementById("settingsCancelBtn").addEventListener("click", closeSettings);
settingsModalBackdrop.addEventListener("click", (e) => { if (e.target === settingsModalBackdrop) closeSettings(); });

document.getElementById("settingsSaveBtn").addEventListener("click", () => {
  const ytUrl = bgYoutubeInput.value.trim();
  if (ytUrl && !extractYoutubeId(ytUrl)) {
    alert("That doesn't look like a valid YouTube URL. Try a link like https://www.youtube.com/watch?v=VIDEOID");
    return;
  }
  settings.bgUrl = bgUrlInput.value.trim();
  settings.ytUrl = ytUrl;
  settings.dim = parseInt(bgDimInput.value, 10);
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  applyBackground();
  closeSettings();
});
document.getElementById("bgClearBtn").addEventListener("click", () => {
  settings = { dim: settings.dim };
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  bgUrlInput.value = "";
  bgYoutubeInput.value = "";
  applyBackground();
});

// --- Accounts menu ---
const accountsModalBackdrop = document.getElementById("accountsModalBackdrop");
const accountsList = document.getElementById("accountsList");

function renderAccountsList() {
  accountsList.innerHTML = "";
  if (!ACCOUNTS.length) {
    const div = document.createElement("div");
    div.className = "empty-state";
    div.textContent = "No connected accounts found.";
    accountsList.appendChild(div);
    return;
  }
  ACCOUNTS.forEach((acct, i) => {
    const row = document.createElement("div");
    row.className = "acct-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = "acct_" + i;
    cb.checked = selectedAccountIds.includes(acct.id);
    cb.addEventListener("change", () => {
      if (cb.checked) {
        if (!selectedAccountIds.includes(acct.id)) selectedAccountIds.push(acct.id);
      } else {
        selectedAccountIds = selectedAccountIds.filter(id => id !== acct.id);
      }
      saveSelectedAccountIds(selectedAccountIds);
      DATA = recomputeData();
      render();
    });
    const label = document.createElement("label");
    label.setAttribute("for", cb.id);
    label.textContent = acct.label;
    row.appendChild(cb);
    row.appendChild(label);
    accountsList.appendChild(row);
  });
}

document.getElementById("accountsBtn").addEventListener("click", () => {
  renderAccountsList();
  accountsModalBackdrop.classList.add("open");
});
document.getElementById("accountsDoneBtn").addEventListener("click", () => accountsModalBackdrop.classList.remove("open"));
accountsModalBackdrop.addEventListener("click", (e) => { if (e.target === accountsModalBackdrop) accountsModalBackdrop.classList.remove("open"); });
document.getElementById("accountsAllBtn").addEventListener("click", () => {
  selectedAccountIds = ACCOUNTS.map(a => a.id);
  saveSelectedAccountIds(selectedAccountIds);
  DATA = recomputeData();
  render();
  renderAccountsList();
});

document.getElementById("prevBtn").addEventListener("click", () => { viewDate.setMonth(viewDate.getMonth()-1); render(); });
document.getElementById("nextBtn").addEventListener("click", () => { viewDate.setMonth(viewDate.getMonth()+1); render(); });
document.getElementById("todayBtn").addEventListener("click", () => { viewDate = new Date(); viewDate.setDate(1); render(); });

render();
</script>
</body>
</html>
"""


def write_html(directory, accounts_meta, raw_history, start_date, end_date):
    path = os.path.join(directory, "index.html")
    html = HTML_TEMPLATE.replace("__TITLE__", PAGE_TITLE)
    html = html.replace("__ACCOUNTS_JSON__", json.dumps(accounts_meta))
    html = html.replace("__RAW_HISTORY_JSON__", json.dumps(raw_history))
    html = html.replace("__START_DATE_JSON__", json.dumps(start_date))
    html = html.replace("__END_DATE_JSON__", json.dumps(end_date))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# --------------------------------------------------------------------------
# Local server + browser launch
# --------------------------------------------------------------------------

BROWSER_ALIASES = {
    "chrome": ["google-chrome", "chrome", "chromium", "chromium-browser"],
    "firefox": ["firefox"],
    "safari": ["safari"],
    "edge": ["microsoft-edge", "msedge", "edge"],
    "brave": ["brave-browser", "brave"],
    "default": [],
}


def get_browser_controller(choice):
    choice = (choice or "default").lower()
    if choice in ("none", "default", ""):
        return None
    candidates = BROWSER_ALIASES.get(choice, [choice])
    for name in candidates:
        try:
            return webbrowser.get(name)
        except webbrowser.Error:
            continue
    print(f"[!] Could not find a registered browser for '{choice}'. Falling back to system default.")
    return None


def serve(directory, port):
    os.chdir(directory)
    handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    httpd.allow_reuse_address = True
    print(f"[+] Serving {PAGE_TITLE} at http://127.0.0.1:{port}/")
    print("[+] Press Ctrl+C to stop the server.")
    httpd.serve_forever()


# --------------------------------------------------------------------------
# Interactive numbered menu
# --------------------------------------------------------------------------

BROWSER_CHOICES = ["default", "chrome", "firefox", "safari", "edge", "brave", "none"]


def _prompt_choice(label, choices, current):
    """Numbered sub-menu for picking one value from a fixed list."""
    print(f"\n{label} (currently: {current})")
    for i, c in enumerate(choices, 1):
        marker = " *" if c == current else ""
        print(f"  {i}) {c}{marker}")
    raw = input(f"Pick 1-{len(choices)} (Enter to keep current): ").strip()
    if not raw:
        return current
    try:
        idx = int(raw)
        if 1 <= idx <= len(choices):
            return choices[idx - 1]
    except ValueError:
        pass
    print("[!] Not a valid choice -- keeping current value.")
    return current


def _prompt_date(label, current):
    raw = input(f"{label} as YYYY-MM-DD (currently: {current or 'earliest/today default'}; "
                f"Enter to keep, 'clear' to reset to default): ").strip()
    if not raw:
        return current
    if raw.lower() == "clear":
        return None
    try:
        dt.date.fromisoformat(raw)
        return raw
    except ValueError:
        print("[!] Not a valid YYYY-MM-DD date -- keeping current value.")
        return current


def _prompt_float(label, current):
    raw = input(f"{label} (currently: {current}; Enter to keep): ").strip()
    if not raw:
        return current
    try:
        return float(raw)
    except ValueError:
        print("[!] Not a valid number -- keeping current value.")
        return current


def _prompt_int(label, current):
    raw = input(f"{label} (currently: {current}; Enter to keep): ").strip()
    if not raw:
        return current
    try:
        return int(raw)
    except ValueError:
        print("[!] Not a valid integer -- keeping current value.")
        return current


def _account_label(acct):
    return f"{acct['label']} ({'Paper' if acct['paper'] else 'Live'})"


def _add_account(accounts):
    """Prompts for a new account's details, verifies the credentials
    against Alpaca before saving, and returns the updated list plus
    the label of the newly-added account (or None if cancelled/failed)."""
    print("\nAdd a new account")
    label = input("  Label for this account (e.g. 'Main Live', 'Paper Test'): ").strip()
    if not label:
        print("[!] A label is required -- cancelled.")
        return accounts, None
    if any(a["label"] == label for a in accounts):
        print(f"[!] An account named '{label}' already exists -- cancelled.")
        return accounts, None

    print("  1) Live trading")
    print("  2) Paper trading")
    sub = input("  Pick 1-2: ").strip()
    paper = sub == "2"
    if sub not in ("1", "2"):
        print("[!] Not a valid choice -- cancelled.")
        return accounts, None

    key_id = input("  Alpaca API Key ID: ").strip()
    secret_key = getpass.getpass("  Alpaca API Secret Key (hidden input): ")
    if not key_id or not secret_key:
        print("[!] Key ID and Secret Key are both required -- cancelled.")
        return accounts, None

    print(f"  Verifying against Alpaca ({'paper' if paper else 'live'}) ...")
    _, error = verify_alpaca_credentials(key_id, secret_key, paper)
    if error:
        print(f"[!] {error}")
        print("    Not saved.")
        return accounts, None

    accounts = accounts + [{"label": label, "paper": paper, "key_id": key_id, "secret_key": secret_key}]
    print(f"[+] Saved account '{label}'.")
    return accounts, label


def _remove_account(accounts, selected_label):
    """Numbered picker for deleting a saved account. Returns the
    updated list plus the (possibly cleared) selected label."""
    if not accounts:
        print("\n[!] No saved accounts to remove.")
        return accounts, selected_label
    print("\nRemove which account?")
    for i, a in enumerate(accounts, 1):
        print(f"  {i}) {_account_label(a)}")
    print("  0) Cancel")
    raw = input(f"Pick 0-{len(accounts)}: ").strip()
    try:
        idx = int(raw)
    except ValueError:
        print("[!] Not a valid choice -- cancelled.")
        return accounts, selected_label
    if idx == 0:
        return accounts, selected_label
    if not (1 <= idx <= len(accounts)):
        print("[!] Not a valid choice -- cancelled.")
        return accounts, selected_label

    target = accounts[idx - 1]
    confirm = input(f"Remove '{_account_label(target)}'? This deletes its saved key/secret. (y/N): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return accounts, selected_label

    accounts = [a for a in accounts if a is not target]
    if selected_label == target["label"]:
        selected_label = None
    print(f"[+] Removed '{target['label']}'.")
    return accounts, selected_label


def manage_accounts_menu(accounts, selected_label):
    """Sub-menu for selecting, adding, or removing saved Alpaca
    accounts. Returns (accounts, selected_label)."""
    while True:
        print("\n" + "-" * 60)
        print("  Accounts")
        print("-" * 60)
        for i, a in enumerate(accounts, 1):
            marker = " *" if a["label"] == selected_label else ""
            print(f"  {i}) {_account_label(a)}{marker}")
        add_idx = len(accounts) + 1
        remove_idx = len(accounts) + 2
        print(f"  {add_idx}) Add new account")
        print(f"  {remove_idx}) Remove an account")
        print("  0) Back")
        raw = input("Select an option: ").strip()
        try:
            idx = int(raw)
        except ValueError:
            print("[!] Not a valid option.")
            continue

        if idx == 0:
            return accounts, selected_label
        elif 1 <= idx <= len(accounts):
            selected_label = accounts[idx - 1]["label"]
            save_accounts(accounts, selected_label)
        elif idx == add_idx:
            accounts, new_label = _add_account(accounts)
            if new_label:
                selected_label = new_label
            save_accounts(accounts, selected_label)
        elif idx == remove_idx:
            accounts, selected_label = _remove_account(accounts, selected_label)
            save_accounts(accounts, selected_label)
        else:
            print("[!] Not a valid option.")


def run_menu(settings, accounts, selected_label):
    """Full numbered menu loop for switching accounts and every other
    run setting before actually connecting to Alpaca. Returns
    (settings, accounts, selected_label) once the user picks 'Start'."""
    while True:
        selected = next((a for a in accounts if a["label"] == selected_label), None)
        account_display = _account_label(selected) if selected else "(none saved -- will prompt at Start)"
        print("\n" + "=" * 60)
        print(f"  {PAGE_TITLE} -- Alpaca PnL Calendar")
        print("=" * 60)
        print(f"  1) Account .................. {account_display}")
        print(f"  2) Start date ................ {settings['start_date'] or '(earliest available)'}")
        print(f"  3) End date .................. {settings['end_date']}")
        print(f"  4) Browser ................... {settings['browser']}")
        print(f"  5) Local port ................ {settings['port']}")
        print("-" * 60)
        print("  6) Start")
        print("  0) Quit")
        choice = input("Select an option: ").strip()

        if choice == "1":
            accounts, selected_label = manage_accounts_menu(accounts, selected_label)
        elif choice == "2":
            settings["start_date"] = _prompt_date("Start date", settings["start_date"])
        elif choice == "3":
            new_end = _prompt_date("End date", settings["end_date"])
            settings["end_date"] = new_end or dt.date.today().isoformat()
        elif choice == "4":
            settings["browser"] = _prompt_choice("Browser", BROWSER_CHOICES, settings["browser"])
        elif choice == "5":
            settings["port"] = _prompt_int("Local port", settings["port"])
        elif choice == "6":
            return settings, accounts, selected_label
        elif choice == "0":
            print("Bye.")
            sys.exit(0)
        else:
            print("[!] Not a valid option -- pick a number from the menu.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ensure_dependencies()

    parser = argparse.ArgumentParser(description="Alpaca-connected PnL calendar (official REST API).")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser", default="default", help="chrome, firefox, safari, edge, brave, default, or none")
    parser.add_argument("--dir", default=os.path.join(os.getcwd(), "pnl_calendar_site_alpaca"))
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD (default: earliest data returned)")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--paper", action="store_true",
                         help="Use Alpaca's paper-trading environment/API instead of live "
                              "(only used when no saved account is selected)")
    parser.add_argument("--period", default="1A",
                         help="Alpaca period, e.g. 1D, 1W, 1M, 3M, 1A, all (default: 1A)")
    parser.add_argument("--timeframe", default="1D",
                         help="Granularity of points within the period (default: 1D)")
    parser.add_argument("--no-menu", action="store_true",
                         help="Skip the interactive numbered menu and run immediately with the flags above "
                              "(useful for scripting/automation)")
    args = parser.parse_args()

    today = dt.date.today()

    settings = {
        "period": args.period,
        "timeframe": args.timeframe,
        "start_date": args.start_date,
        "end_date": args.end_date or today.isoformat(),
        "browser": args.browser,
        "port": args.port,
    }

    accounts, selected_label = load_accounts()

    if not args.no_menu:
        settings, accounts, selected_label = run_menu(settings, accounts, selected_label)

    start_date = settings["start_date"]
    end_date = settings["end_date"]

    selected_account = next((a for a in accounts if a["label"] == selected_label), None)
    if selected_account:
        session = alpaca_login_with_account(selected_account)
        account_display_label = selected_account["label"]
        use_paper = selected_account["paper"]
    else:
        session, key_id, secret_key = alpaca_login_interactive(args.paper)
        account_display_label = "Alpaca (Paper)" if args.paper else "Alpaca"
        use_paper = args.paper
        if not args.no_menu:
            save = input("Save this key/secret for next time? (y/N): ").strip().lower()
            if save == "y":
                label = input("  Label for this account (e.g. 'Main Live'): ").strip() or account_display_label
                accounts = accounts + [{"label": label, "paper": use_paper, "key_id": key_id, "secret_key": secret_key}]
                save_accounts(accounts, label)
                account_display_label = label
                print(f"[+] Saved as '{label}'.")

    print("[+] Fetching daily portfolio equity history...")
    raw_series, raw_sample, base_value = fetch_daily_equity_history(
        session, period=settings["period"], timeframe=settings["timeframe"])
    if not raw_series:
        print("[!] No usable data returned -- see messages above.")
        pause_before_exit()
        sys.exit(1)
    print(f"[+] Got {len(raw_series)} day(s) of portfolio equity.")

    # The earliest day Alpaca returns has nothing before it to diff
    # against, so day-over-day change can't be computed for it and it
    # would otherwise silently disappear from the calendar. Anchor it
    # using Alpaca's own base_value (the account's baseline equity
    # right before the period starts) as a synthetic prior day.
    if base_value is not None:
        try:
            earliest = min(raw_series.keys())
            anchor_date = (dt.date.fromisoformat(earliest) - dt.timedelta(days=1)).isoformat()
            if anchor_date not in raw_series:
                raw_series[anchor_date] = float(base_value)
                print(f"[+] Anchored {earliest} against Alpaca's base_value so its change can be computed.")
        except (TypeError, ValueError):
            pass

    cleaned = raw_series
    filled_values, filled_from = fill_calendar_gaps(cleaned)
    filled_count = sum(1 for v in filled_from.values() if v is not None)
    if filled_count:
        print(f"[+] Forward-filled {filled_count} day(s) with no fresh data point "
              f"(non-trading days, or gaps in what Alpaca returned).")

    daily_change = compute_daily_portfolio_change(filled_values, start_date, end_date, filled_from)
    print(f"[+] Computed portfolio change for {len(daily_change)} day(s).")

    os.makedirs(args.dir, exist_ok=True)
    debug_csv = write_debug_csv(args.dir, cleaned, daily_change)
    print(f"[+] Wrote daily values to {debug_csv}")
    print("    Open it (e.g. in Excel) and compare against the Alpaca dashboard if the calendar looks off.")

    accounts_meta = [{"id": "alpaca", "label": account_display_label}]
    raw_history = {"alpaca": cleaned}
    write_html(args.dir, accounts_meta, raw_history, start_date, end_date)

    port = settings["port"]
    url = f"http://127.0.0.1:{port}/"
    if settings["browser"].lower() != "none":
        controller = get_browser_controller(settings["browser"])

        def open_browser():
            (controller.open if controller else webbrowser.open)(url)

        threading.Timer(0.7, open_browser).start()
    else:
        print(f"[i] Skipping auto-open. Visit {url} manually.")

    try:
        serve(args.dir, port)
    except KeyboardInterrupt:
        print("\n[+] Server stopped.")
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[+] Stopped.")
    except SystemExit as e:
        if e.code not in (0, None):
            pass
        raise
    except Exception:
        print("\n" + "=" * 60)
        print("[!] The script hit an unexpected error:")
        print("=" * 60)
        traceback.print_exc()
        pause_before_exit()
        sys.exit(1)
