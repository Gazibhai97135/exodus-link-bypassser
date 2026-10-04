"""
EXODUS DZHQ BYPASS API — MULTI-ACCOUNT + ADMIN PANEL + NICK BOT + ALEX API
Developer: @Exodus_OWN3R
Version: 3.0  (+ Telegram bot, force-join, referral API keys)

Features:
- Multi-account round-robin bypass
- /admin panel (password: Exodus_OWN3R)
- Add/Remove accounts
- Update session string (no restart needed)
- Pause/Resume per account or all
- Live stats per account
- NEVER CRASHES — even if default session fails!
- PERSISTENCE FIX — accounts survive server restarts!
- Nick_Bypass_Bot DM support (2-way race)
- Alex Bypass API used as the primary URL resolver
- Bot toggles (DZHQ / Nick / Alex on-off from admin panel)
- DZHQ First Mode — DZHQ runs first; Nick only activates if DZHQ fails

HOW TO RUN:
  python bot.py
  (packages install automatically on first run)

SETUP:
  Open /admin to add Telegram accounts if Telegram fallback is needed.

BYPASS:
  GET /bypass?link=YOUR_LINK
"""

# ══════════════════════════════════════════════════════════════
#  SETUP — loads .env automatically
# ══════════════════════════════════════════════════════════════
import sys, os, subprocess

def _load_dotenv(path=".env"):
    """Load key=value pairs from .env file into os.environ."""
    if not os.path.exists(path):
        # Create .env.example for the user
        example = ".env.example"
        if not os.path.exists(example):
            with open(example, "w") as f:
                f.write(
                    "# Admin panel password (default: Exodus_OWN3R)\n"
                    "ADMIN_PASSWORD=Exodus_OWN3R\n\n"
                    "# Flask secret key\n"
                    "SECRET_KEY=change_this_to_random_string\n"
                )
            print(f"ℹ️  Created {example} — copy it to .env and fill your values\n")
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:   # don't override existing env vars
                os.environ[k] = v
    print(f"✅ Loaded {path}\n")

_load_dotenv()
# ══════════════════════════════════════════════════════════════

from flask import Flask, request, jsonify, session as flask_session, redirect, render_template_string, send_file
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
import asyncio
import threading
import time
import re
import logging
import os
import secrets
import json as _json
import uuid
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import wraps

# ==================== CONFIG ====================
ADMIN_PASSWORD  = 'Exodus_OWN3R'

# Group 1 — DZHQ
DZHQ_BOT        = "@DZHQ_BypassBot"
DZHQ_GROUP      = -1003644908415

# Group 2 — Nick (DM mode)
NICK_BOT        = "@Nick_Bypass_Bot"

# Group 3 — Alex Bypass Bot (DM mode) — races against the Alex HTTP API
ALEX_BOT        = "@alexbypassbot"

DEVELOPER       = "@Exodus_OWN3R"
PORT            = int(os.environ.get('PORT', 5000))
SECRET_KEY      = (
    os.environ.get("SECRET_KEY")
    or os.environ.get("SESSION_SECRET")
    or secrets.token_hex(32)
)
ACCOUNTS_FILE   = "accounts_data.json"

# Alex API is only used for the two supported short-link domains.
ALEX_BYPASS_API = os.environ.get(
    "ALEX_BYPASS_API",
    "https://alexbypassapi.up.railway.app/bypass?url=",
).strip()
ALEX_TIMEOUT_SEC = float(os.environ.get("ALEX_TIMEOUT_SEC", "20"))
ALEX_ALLOWED_HOST_PARTS = ("urlking", "monteolympus")
ALEX_BOT_TIMEOUT_SEC = float(os.environ.get("ALEX_BOT_TIMEOUT_SEC", "75"))
# Only silence triggers this timeout. Progress/loading messages refresh it.
BYPASS_IDLE_TIMEOUT_SEC = float(os.environ.get("BYPASS_IDLE_TIMEOUT_SEC", "30"))
# A non-positive per-bot timeout must never turn an HTTP request into an
# unbounded wait. These caps are only used when a bot/API does not answer.
ALEX_RACE_MAX_TIMEOUT_SEC = max(
    30.0, float(os.environ.get("ALEX_RACE_MAX_TIMEOUT_SEC", "90"))
)
MAX_BYPASS_TIMEOUT_SEC = max(
    30.0, float(os.environ.get("MAX_BYPASS_TIMEOUT_SEC", "120"))
)
TRACE_BOTS = os.environ.get("TRACE_BOTS", "0").lower() in ("1", "true", "yes", "on")

def _trace(label, message):
    if TRACE_BOTS:
        print(f"[TRACE][{label}] {message}", flush=True)

# ── Bot toggle settings ────────────────────────────────────────────────────────
# Set to False to disable a bot entirely (it won't be sent to or waited on)
bot_settings        = {"dzhq": True, "nick": True, "dzhq_first": False,
                       "nick_first": False, "random_mode": False, "alex_bot": True}

# ── Random Mode state ──────────────────────────────────────────────────────────
# Random Mode: ek bot par lagataar N requests (random 3-10), phir doosre bot par
# switch. Dono bots aapas me baari-baari kaam karte hain.
RANDOM_BATCH_MIN = 3
RANDOM_BATCH_MAX = 10
_random_state = {"bot": None, "left": 0, "batch": 0}
_random_lock  = threading.Lock()

def _random_pick_bot():
    """Return the bot ('dzhq'/'nick') that should handle this request in Random Mode."""
    import random as _rnd
    with _random_lock:
        if _random_state["bot"] is None or _random_state["left"] <= 0:
            prev = _random_state["bot"]
            nxt  = "nick" if prev == "dzhq" else ("dzhq" if prev == "nick" else _rnd.choice(["dzhq", "nick"]))
            size = _rnd.randint(RANDOM_BATCH_MIN, RANDOM_BATCH_MAX)
            _random_state["bot"]   = nxt
            _random_state["left"]  = size
            _random_state["batch"] = size
            print(f"[RandomMode] switched to {nxt.upper()} for next {size} link(s)", flush=True)
        _random_state["left"] -= 1
        return _random_state["bot"]

def _random_status():
    with _random_lock:
        return {"current": _random_state["bot"], "remaining": max(0, _random_state["left"]),
                "batch": _random_state["batch"]}

def _set_exclusive_mode(mode):
    """Only one of dzhq_first / nick_first / random_mode can be ON at a time."""
    for m in ("dzhq_first", "nick_first", "random_mode"):
        bot_settings[m] = (m == mode)

# Async requests are kept separate from the synchronous /bypass endpoint.  This
# prevents a slow resolver (especially a Telegram bot) from making the caller's
# HTTP request time out before the final Telegram message arrives.
_jobs = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 1000


def _flow_update(job_id, status, message, **extra):
    """Append a small, client-friendly status message to an async job."""
    if not job_id:
        return
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["status"] = status
        job["message"] = message
        job["updated_at"] = time.time()
        if "result" in extra:
            job["result"] = extra["result"]
        event = {"status": status, "message": message, "at": job["updated_at"]}
        event.update(extra)
        job["flow"].append(event)
        # Keep memory bounded even when a client never polls the job.
        if len(job["flow"]) > 50:
            del job["flow"][:-50]

logging.basicConfig(level=logging.WARNING)
logging.getLogger('telethon').setLevel(logging.WARNING)

# ==================== ALEX API ====================
def alex_bypass(link):
    """Resolve a URL through the user-configured Alex Bypass API."""
    if not ALEX_BYPASS_API:
        return None, "Alex API is not configured"

    encoded_link = urllib.parse.quote(link, safe="")
    if ALEX_BYPASS_API.endswith("url="):
        target = f"{ALEX_BYPASS_API}{encoded_link}"
    else:
        separator = "&" if "?" in ALEX_BYPASS_API else "?"
        target = f"{ALEX_BYPASS_API}{separator}url={encoded_link}"
    req = urllib.request.Request(
        target,
        headers={
            "Accept": "application/json",
            "User-Agent": "Exodus_bypass-API/3.0",
        },
        method="GET",
    )

    try:
        # Never pass None here: urllib treats it as an unlimited network wait.
        api_timeout = (
            ALEX_TIMEOUT_SEC
            if ALEX_TIMEOUT_SEC > 0
            else ALEX_RACE_MAX_TIMEOUT_SEC
        )
        with urllib.request.urlopen(req, timeout=api_timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            _trace("ALEX_API", f"response for {link}: {str(payload)[:500]}")
    except urllib.error.HTTPError as exc:
        return None, f"Alex API HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"Alex API unavailable: {exc.reason}"
    except TimeoutError:
        return None, "Alex API request timed out"
    except (ValueError, json.JSONDecodeError):
        return None, "Alex API returned invalid JSON"
    except Exception as exc:
        return None, f"Alex API error: {exc}"

    if not isinstance(payload, dict):
        return None, "Alex API returned an unexpected response"
    if str(payload.get("status", "")).lower() not in ("success", "ok", "true", "1"):
        return None, str(payload.get("message") or payload.get("error") or "Alex API could not bypass this URL")

    result_url = payload.get("url") or payload.get("bypassed") or payload.get("result")
    if isinstance(result_url, list):
        result_url = result_url[0] if result_url else None
    if not isinstance(result_url, str) or not result_url.startswith(("http://", "https://")):
        return None, "Alex API response did not contain a valid result URL"

    return {
        "status": "ok",
        "original": link,
        "bypassed": result_url,
        "raw_type": payload.get("module", "alex"),
        "alex_response": payload,
    }, None


def _should_use_alex(link):
    """Return True only for URLKing and MonteOlympus links."""
    try:
        hostname = (urllib.parse.urlsplit(link).hostname or "").lower()
    except ValueError:
        return False
    return any(part in hostname for part in ALEX_ALLOWED_HOST_PARTS)


# ==================== ACCOUNTS STORE ====================
accounts        = {}
accounts_lock   = threading.Lock()
round_robin_idx = 0
rr_lock         = threading.Lock()
admin_tokens    = set()


# ==================== PERSISTENCE ====================
def save_accounts_to_file():
    try:
        data = []
        with accounts_lock:
            for acc in accounts.values():
                data.append({
                    "id":             acc["id"],
                    "name":           acc["name"],
                    "api_id":         acc["api_id"],
                    "api_hash":       acc["api_hash"],
                    "session_string": acc["session_string"],
                })
        with open(ACCOUNTS_FILE, "w") as f:
            _json.dump(data, f, indent=2)
        print(f"💾 Saved {len(data)} accounts to {ACCOUNTS_FILE}")
    except Exception as e:
        print(f"⚠️ Could not save accounts: {e}")

def load_accounts_from_file():
    if not os.path.exists(ACCOUNTS_FILE):
        print(f"ℹ️ No saved accounts file found ({ACCOUNTS_FILE})")
        return
    try:
        with open(ACCOUNTS_FILE, "r") as f:
            data = _json.load(f)
        count = 0
        for entry in data:
            try:
                acc_id = entry["id"]
                with accounts_lock:
                    accounts[acc_id] = _make_acc_dict(
                        acc_id, entry["name"],
                        int(entry["api_id"]), entry["api_hash"], entry["session_string"]
                    )
                _start_account_thread(acc_id)
                count += 1
                print(f"✅ Loaded saved account: {entry['name']} ({acc_id})")
            except Exception as e:
                print(f"⚠️ Could not load account {entry.get('name', '?')}: {e}")
        print(f"💾 Loaded {count} accounts from {ACCOUNTS_FILE}")
    except Exception as e:
        print(f"⚠️ Could not load accounts file: {e}")


def _make_acc_dict(acc_id, name, api_id, api_hash, session_string):
    return {
        "id":             acc_id,
        "name":           name,
        "api_id":         int(api_id),
        "api_hash":       api_hash,
        "session_string": session_string,
        "status":         "connecting",
        "bypass_count":   0,
        "success_count":  0,
        "fail_count":     0,
        "last_used":      None,
        "error_message":  None,
        "client":         None,
        "loop":           None,
        "thread":         None,
        "dzhq_bot_id":    None,
        "nick_bot_id":    None,
        "alex_bot_id":    None,
        "pending":        {},
        "nick_dm_queue":  [],   # FIFO queue for Nick DM requests
        "nick_dm_lock":   threading.Lock(),
        "alex_dm_queue":  [],   # FIFO queue for Alex bot DM requests
        "alex_dm_lock":   threading.Lock(),
        "username":       None,
        "first_name":     None,
    }


# ==================== DEFAULT ACCOUNT ====================
_default_loaded = False

def load_default_account():
    global _default_loaded
    if _default_loaded:
        return
    _default_loaded = True
    try:
        API_ID = int(os.environ.get("TELEGRAM_API_ID", "30605082"))
        API_HASH = os.environ.get("TELEGRAM_API_HASH", "8c8ea983caab6da3ca4ce1e901dba229").strip()
        SESSION_STR = os.environ.get("TELEGRAM_SESSION", "1BVtsOIEBuzC73J_y33yll45vlL7EAqjQQh2PKd3CzYTywGTtZLrkzLHbjg5g-ZYnj1NCf09NsSk1DyATDJJTsZfE90atSwwJ0A1GNfrjGnJopL7gpS_2X71wwfiMDtMnl14CrcPQ1kgrDfkgiWl00VuJ65d7ovNYVGF5AoizmYOLbGOn7i6qs3nP7csi2U7T6dwDenqmofQz30RBc_qu_FC6XkNhbHDm2VmVChhyaRNmiOSnON0DQw6KjOvDaVo8i1JAWbmjs1aczRgMKhTOruthlhw7S2EuCwaZFgJVz8W8iHdrBT88mjinrft18h8AcZCrzS_FU7XPOPmY31jPBQa5Zho7Blo=").strip()
        if SESSION_STR and API_ID and API_HASH:
            try:
                acc_id = _add_account_record("Johan", API_ID, API_HASH, SESSION_STR)
                print(f"✅ Default account loaded: {acc_id}")
            except Exception as e:
                print(f"⚠️ Default account failed: {e}")
    except Exception as e:
        print(f"⚠️ Error loading default account: {e}")

def _add_account_record(name, api_id, api_hash, session_string):
    acc_id = str(uuid.uuid4())[:8]
    with accounts_lock:
        accounts[acc_id] = _make_acc_dict(acc_id, name, api_id, api_hash, session_string)
    _start_account_thread(acc_id)
    return acc_id


# ==================== REGEX ====================
_RX_IN  = re.compile(r'In\s+Link[^:\n]*?:-\s*\*{0,4}\s*(https?://[^\s*\n]+)', re.I)
_RX_GOT = re.compile(r'Got\s+Result[^:\n]*?:-\s*\*{0,4}\s*(https?://[^\s*\n]+)', re.I)
_RICH_LABELS = {
    'instant_dl':      re.compile(r'instant\s*dl|10gbps',  re.I),
    'telegram_link':   re.compile(r'telegram\s*link|tgcdn', re.I),
    'direct_download': re.compile(r'direct\s*download',     re.I),
    'got_result':      re.compile(r'got\s*result',          re.I),
}
_RX_ERR          = re.compile(
    r'invalid\s*link|not\s*support|unsupported|no\s*script|not\s*found|'
    r'error|failed|cannot|wrong|sorry|got\s+error',
    re.I,
)
_RX_RATE         = re.compile(r'rate\s*limit|flood|wait\s*\d+|try\s*again', re.I)
_RX_INTERMEDIATE = re.compile(
    r'^[\*\s]*bypass(?:ing)?\.{0,6}[\*\s]*$'
    r'|^[\*\s]*processing\.{0,6}[\*\s]*$'
    r'|^[\*\s]*please\s*wait[\*\s]*$'
    r'|^[\*\s]*fetching[\*\s]*$'
    r'|^[\*\s]*checking[\*\s]*$',
    re.I | re.M)
_RX_GOT_ERR = re.compile(r'Got\s+Error[^:\n]*?:-\s*[`*\s]*(.*?)\s*[`*]*\s*$', re.I | re.M)
_RX_JUNK    = re.compile(r'[\*`\'\"✔️✅]+')
_RX_FNAME   = re.compile(r'File\s+Name\s*:-\s*[`*\s]*(.+?)\s*[`*]*\s*$', re.I | re.M)
_RX_FSIZE   = re.compile(r'File\s+Size\s*:-\s*[`*\s]*(.+?)\s*[`*]*\s*$', re.I | re.M)
_RX_SEP     = re.compile(r'━{3,}.*?✦.*?━{3,}')

def _clean(u):
    return _RX_JUNK.sub('', u).rstrip('.,;:!?)>]\'"').strip()

def parse_dzhq_message(text, entities, sent_link):
    if not text:
        return [{"status": "empty"}]
    stripped = text.strip()
    lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    if len(lines) <= 2 and _RX_INTERMEDIATE.search(stripped):
        return [{"status": "intermediate", "raw": stripped[:80]}]
    if _RX_RATE.search(text):
        return [{"status": "rate_limit", "error": "Rate limited by DZHQ bot"}]
    if _RX_ERR.search(text):
        err_m = _RX_GOT_ERR.search(text)
        clean_err = re.sub(r'[\*`]+', '', _clean(err_m.group(1))).strip() if err_m else stripped[:120]
        return [{"status": "failed", "error": clean_err}]

    results, ent_urls = [], []
    if entities:
        for ent in entities:
            off = getattr(ent, 'offset', None)
            lng = getattr(ent, 'length', None)
            url = getattr(ent, 'url', None)
            if not url:
                if isinstance(ent, MessageEntityUrl) and off is not None and lng:
                    url = text[off:off + lng]
                else:
                    continue
            url = _clean(url)
            if not url.startswith(('http://', 'https://')):
                continue
            if off is not None:
                ls = text.rfind('\n', 0, off) + 1
                le = text.find('\n', off)
                le = le if le != -1 else len(text)
                anchor = _clean(text[off:off + (lng or 0)]) if lng else ''
                line   = _clean(text[ls:le])
                # Prefer the anchor text ("FSL Server", "Pixeldrain"…) so that
                # multi-link lines don't all inherit the same line label.
                if anchor and not anchor.startswith(('http://', 'https://')) \
                   and not re.fullmatch(r'(?i)[\s\*`\[\]\-–—:•]*((link|click|here|download|open|dl)[\s\*`\[\]\-–—:•]*)+', anchor):
                    label = anchor
                else:
                    label = line
                ent_urls.append((label, url))
            else:
                ent_urls.append((url, url))


    blocks = _RX_SEP.split(text)
    blocks = [b.strip() for b in blocks if b.strip()]
    for block in blocks:
        if 'Powered By' in block and 'DZHQBypass' in block and len(block) < 80:
            continue
        r = _parse_block(block, ent_urls, sent_link)
        if r:
            results.append(r)
    if not results:
        r = _parse_block(text, ent_urls, sent_link)
        if r:
            results.append(r)
    if not results:
        return [{"status": "no_link", "error": "No bypassed link found", "raw": text[:300]}]
    return results


# ==================== NICK BOT PARSER (DM mode) ====================
def parse_nick_message(text, entities, sent_link):
    """Parse DM reply from Nick_Bypass_Bot"""
    if not text:
        return None
    stripped = text.strip()
    # Skip intermediate messages
    if _RX_INTERMEDIATE.search(stripped) and len(stripped) < 100:
        return None
    if _RX_RATE.search(text):
        return {
            "status": "rate_limit",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }
    if _RX_ERR.search(text):
        return {
            "status": "failed",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }

    urls_found = []
    # Extract from entities first (most reliable)
    if entities:
        for ent in entities:
            off = getattr(ent, 'offset', None)
            lng = getattr(ent, 'length', None)
            url = getattr(ent, 'url', None)
            if not url:
                if isinstance(ent, MessageEntityUrl) and off is not None and lng:
                    url = text[off:off + lng]
                else:
                    continue
            url = _clean(url)
            if not url.startswith(('http://', 'https://')):
                continue
            if sent_link and (sent_link in url or url in sent_link):
                continue
            if url not in urls_found:
                urls_found.append(url)

    # Fallback: plain text URL regex
    if not urls_found:
        for u in re.findall(r'https?://[^\s\n\)\]>"\']+', text):
            u = _clean(u)
            if not u.startswith(('http://', 'https://')):
                continue
            if sent_link and (sent_link in u or u in sent_link):
                continue
            if u not in urls_found:
                urls_found.append(u)

    if not urls_found:
        return None

    def _flatten(lst):
        if not lst:       return None
        if len(lst) == 1: return lst[0]
        return lst

    return {
        "status":   "ok",
        "original": sent_link,
        "bypassed": _flatten(urls_found),
        "raw":      stripped[:300],
    }


def _parse_block(block, ent_urls, sent_link):
    original      = None
    bypassed      = []
    instant_dl    = []
    telegram_link = []
    direct_dl     = []
    file_name = file_size = None
    raw_type = "unknown"

    m = _RX_FNAME.search(block)
    if m: file_name = _clean(m.group(1))
    m = _RX_FSIZE.search(block)
    if m: file_size = _clean(m.group(1))
    m = _RX_IN.search(block)
    if m: original = _clean(m.group(1))
    m = _RX_GOT.search(block)
    if m:
        u = _clean(m.group(1))
        if u not in bypassed: bypassed.append(u)
        raw_type = "got_result"

    for label, url in ent_urls:
        ll, ul = label.lower(), url.lower()
        if original and (original in url or url in original):
            continue
        if _RICH_LABELS['instant_dl'].search(ll):
            if url not in instant_dl: instant_dl.append(url)
            raw_type = "rich"
        elif _RICH_LABELS['telegram_link'].search(ll) or 'tgcdn_bot' in ul or 'telegram.dog' in ul:
            if url not in telegram_link: telegram_link.append(url)
            raw_type = "rich"
        elif _RICH_LABELS['direct_download'].search(ll):
            if url not in direct_dl: direct_dl.append(url)
            raw_type = "rich"
        elif _RICH_LABELS['got_result'].search(ll):
            if url not in bypassed: bypassed.append(url)
            raw_type = "entity_got_result"
        else:
            if url not in bypassed: bypassed.append(url)
            if raw_type == "unknown": raw_type = "entity_generic"

    if not bypassed:
        urls = [_clean(u) for u in re.findall(r'https?://[^\s\*\n\)\]>]+', block)]
        for u in urls:
            if original and (original in u or u in original):
                continue
            if u not in bypassed: bypassed.append(u)
            raw_type = "plain_url"

    if not original:
        original = sent_link
    if not (bypassed or instant_dl or telegram_link or direct_dl):
        return None

    def _flatten(lst):
        if not lst:   return None
        if len(lst) == 1: return lst[0]
        return lst

    out = {"status": "ok", "original": original,
           "bypassed": _flatten(bypassed),
           "raw_type": raw_type, "file_name": file_name, "file_size": file_size}
    if instant_dl:    out["instant_dl"]   = _flatten(instant_dl)
    if telegram_link: out["telegram"]     = _flatten(telegram_link)
    if direct_dl:     out["direct"]       = _flatten(direct_dl)
    return out


# ==================== PER-ACCOUNT TELEGRAM CLIENT ====================
def make_dzhq_handler(acc_id):
    async def on_reply(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return
        bot_id = state.get("dzhq_bot_id")
        if bot_id:
            if event.sender_id != bot_id:
                return
        else:
            try:
                s = await event.get_sender()
                u = (getattr(s, 'username', '') or '').lower()
                if 'dzhq' not in u:
                    return
            except:
                return

        msg  = event.message
        text = msg.text or ''
        if not text:
            return

        rt_obj      = getattr(msg, 'reply_to', None)
        reply_to_id = getattr(rt_obj, 'reply_to_msg_id', None) if rt_obj else None

        with accounts_lock:
            pending = state.get("pending", {})
        sent_id_map = {
            req['dzhq_sent_id']: rid
            for rid, req in pending.items()
            if not req.get('done') and req.get('dzhq_sent_id')
        }
        matched_id = sent_id_map.get(reply_to_id)
        if not matched_id or matched_id not in pending:
            return

        req     = pending[matched_id]
        ents    = msg.entities or []
        results = parse_dzhq_message(text, ents, req['link'])
        status  = results[0].get('status') if results else 'empty'
        _trace("DZHQ", f"received for {req['link']}: status={status} text={text[:500]!r} parsed={results}")
        req['last_dzhq_ts'] = time.time()
        if status in ('failed', 'rate_limit'):
            req['dzhq_fail'] = (
                results[0].get('error')
                or ("DZHQ rate limited" if status == "rate_limit" else "DZHQ rejected the link")
            )

        tg = state.get("client")
        if status not in ('intermediate', 'rate_limit'):
            if tg:
                async def _auto_click_delete(bot_msg, req_ref, acc):
                    """Click the Delete inline button on DZHQ bot's result message.
                    Only clicks if button is present — never deletes messages manually."""
                    try:
                        buttons = getattr(bot_msg, 'buttons', None)
                        clicked = False
                        if buttons:
                            flat = [btn for row in buttons for btn in row]
                            for btn in flat:
                                btn_text = (getattr(btn, 'text', '') or '').strip().lower()
                                if 'delete' in btn_text or '🗑' in btn_text or '❌' in btn_text:
                                    try:
                                        await btn.click()
                                        print(f"[DZHQ/{acc}] ✅ Delete button clicked")
                                        clicked = True
                                        break
                                    except Exception as e:
                                        print(f"[DZHQ/{acc}] ⚠️ btn.click error: {e}")
                        if not clicked:
                            print(f"[DZHQ/{acc}] ℹ️ No Delete button found — skipping")
                        req_ref['dzhq_sent_id'] = None
                    except Exception as e:
                        print(f"[DZHQ/{acc}] Delete error: {e}")

                asyncio.create_task(_auto_click_delete(msg, req, acc_id))

        if status in ('intermediate', 'empty'):
            return

        req['dzhq_result'] = results
        req['dzhq_event'].set()
    return on_reply


def make_nick_dm_handler(acc_id):
    """
    Handler for Nick_Bypass_Bot private DMs.
    - Nick is used as a Telegram fallback only; no captcha solver is used.
    - If Nick sends the bypass result → signal the waiting thread
    """
    async def on_dm(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return

        # Never treat Alex bot DMs as Nick replies
        alex_bot_id_guard = state.get("alex_bot_id")
        if alex_bot_id_guard and event.sender_id == alex_bot_id_guard:
            return

        # Only handle DMs from Nick bot
        nick_bot_id = state.get("nick_bot_id")
        if nick_bot_id:
            if event.sender_id != nick_bot_id:
                return
        else:
            try:
                s = await event.get_sender()
                u = (getattr(s, 'username', '') or '').lower()
                n = (getattr(s, 'first_name', '') or '').lower()
                if 'nick' not in u and 'nick' not in n and 'bypass' not in u:
                    return
            except:
                return

        msg  = event.message
        text = (msg.text or msg.caption or '').strip()
        tg   = state.get("client")

        # Need at least some text to parse a bypass result
        if not text:
            return

        # FIFO: pop oldest pending Nick DM request
        nick_lock = state.get("nick_dm_lock")
        if not nick_lock:
            return

        with nick_lock:
            queue = state.get("nick_dm_queue", [])
            if not queue:
                return
            req_id = queue[0]   # peek

        with accounts_lock:
            pending = state.get("pending", {})
            req     = pending.get(req_id)
        if not req or req.get('done'):
            with nick_lock:
                if queue and queue[0] == req_id:
                    queue.pop(0)
            return

        ents = msg.entities or []
        r    = parse_nick_message(text, ents, req['link'])
        _trace("NICK", f"received for {req['link']}: text={text[:500]!r} parsed={r}")
        req['last_nick_ts'] = time.time()
        if r is None:
            if TRACE_BOTS and not _RX_INTERMEDIATE.search(text) and not _RX_RATE.search(text):
                with nick_lock:
                    if queue and queue[0] == req_id:
                        queue.pop(0)
                req['nick_result'] = {
                    "status": "failed",
                    "error": "Nick parser found no bypassed URL",
                    "raw": text[:500],
                }
                req['nick_event'].set()
            return   # intermediate/rate_limit — keep waiting

        # Got a real bypass result — pop from queue and signal
        with nick_lock:
            if queue and queue[0] == req_id:
                queue.pop(0)

        req['nick_result'] = r
        req['nick_event'].set()
    return on_dm



# ==================== ALEX BOT PARSER (DM mode) ====================
# Alex bot writes everything in small-caps unicode ("ʙʏᴩᴀꜱꜱᴇᴅ ʟɪɴᴋ"), so all
# matching is done on a folded ASCII copy of the text.
_SMALLCAPS_MAP = {
    'ᴀ':'a','ʙ':'b','ᴄ':'c','ᴅ':'d','ᴇ':'e','ꜰ':'f','ɢ':'g','ʜ':'h','ɪ':'i','ᴊ':'j',
    'ᴋ':'k','ʟ':'l','ᴍ':'m','ɴ':'n','ᴏ':'o','ᴩ':'p','ᴘ':'p','q':'q','ǫ':'q','ʀ':'r',
    'ꜱ':'s','ѕ':'s','ᴛ':'t','ᴜ':'u','ᴠ':'v','ᴡ':'w','x':'x','ʏ':'y','ᴢ':'z','ɀ':'z',
}
def _fold(s):
    return ''.join(_SMALLCAPS_MAP.get(ch, ch) for ch in (s or ''))

_RX_ALEX_BYPASSED = re.compile(r'bypass(?:ed)?\s*link[^:\n]*:?-?\s*\**\s*\n?\s*(https?://\S+)', re.I)
_RX_ALEX_ORIGINAL = re.compile(r'original\s*link[^:\n]*:?-?\s*\**\s*\n?\s*(https?://\S+)', re.I)
_RX_ALEX_PROGRESS = re.compile(
    r'initialis|initializ|fetching|scanning|bypassing\s*security|cracking|decoding|solving|'
    r'processing|please\s*wait|\d{1,3}\s*%|[▰▱]', re.I)
_RX_ALEX_FAIL = re.compile(
    r'bypass\s*(?:failed|error)|invalid\s*link|not\s*support(?:ed)?|unsupported|'
    r'no\s*script|not\s*found|unable|unable\s+to|cannot|can[\'’]t|'
    r'could\s*not|couldn[\'’]t|not\s*possible|does\s*not\s*support|'
    r'doesn[\'’]t\s*support|failed|error', re.I)
_ALEX_SKIP_PARTS  = ("t.me/+", "t.me/alexmodz", "update", "join", "channel", "apkamitrr")


def parse_alex_message(text, entities, sent_link):
    """Parse DM reply from the Alex Bypass Bot (new + edited messages)."""
    if not text:
        return None
    stripped = text.strip()
    folded   = _fold(stripped)

    # Progress / animation frames — keep waiting
    if (
        _RX_ALEX_PROGRESS.search(folded)
        and not _RX_ALEX_BYPASSED.search(folded)
        and not _RX_ALEX_FAIL.search(folded)
    ):
        return None
    if _RX_INTERMEDIATE.search(folded) and len(folded) < 120:
        return None
    if _RX_RATE.search(folded):
        return {
            "status": "rate_limit",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }
    if _RX_ALEX_FAIL.search(folded) and not _RX_ALEX_BYPASSED.search(folded):
        return {
            "status": "failed",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }

    # 1) Preferred: the explicit "BYPASSED LINK" section
    m = _RX_ALEX_BYPASSED.search(folded)
    if m:
        u = _clean(m.group(1))
        mo = _RX_ALEX_ORIGINAL.search(folded)
        orig = _clean(mo.group(1)) if mo else sent_link
        same_as_input = (sent_link and (sent_link in u or u in sent_link)) or (orig and u == orig)
        if u.startswith(("http://", "https://")) and not same_as_input \
           and not any(k in u.lower() for k in _ALEX_SKIP_PARTS):
            return {"status": "ok", "original": orig or sent_link, "bypassed": u, "raw": stripped[:300]}


    # 2) Fallback: reuse the generic DM parser (entities + plain URLs)
    r = parse_nick_message(text, entities, sent_link)
    if not r:
        return None
    if r.get("status") != "ok":
        return r
    b = r.get("bypassed")
    if isinstance(b, list):
        b = next((x for x in b if not any(k in x.lower() for k in _ALEX_SKIP_PARTS)), b[0] if b else None)
    if not b:
        return None
    r["bypassed"] = b
    return r


def _same_link(left, right):
    """Compare links without treating a trailing slash as a different request."""
    if not left or not right:
        return False
    try:
        a = urllib.parse.urlsplit(str(left).strip())
        b = urllib.parse.urlsplit(str(right).strip())
        return (
            a.scheme.lower(),
            (a.hostname or "").lower(),
            a.port,
            a.path.rstrip("/") or "/",
            a.query,
            a.fragment,
        ) == (
            b.scheme.lower(),
            (b.hostname or "").lower(),
            b.port,
            b.path.rstrip("/") or "/",
            b.query,
            b.fragment,
        )
    except ValueError:
        return str(left).strip().rstrip("/") == str(right).strip().rstrip("/")


def make_alex_dm_handler(acc_id):
    """Handler for @alexbypassbot private DMs."""
    async def on_dm(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return

        alex_bot_id = state.get("alex_bot_id")
        if alex_bot_id:
            if event.sender_id != alex_bot_id:
                return
        else:
            try:
                sndr = await event.get_sender()
                u = (getattr(sndr, 'username', '') or '').lower()
                if 'alex' not in u:
                    return
            except Exception:
                return

        msg  = event.message
        text = (msg.text or msg.caption or '').strip()
        if not text:
            return

        alex_lock = state.get("alex_dm_lock")
        if not alex_lock:
            return

        with alex_lock:
            queue = state.get("alex_dm_queue", [])
            if not queue:
                return
            req_id = queue[0]

        with accounts_lock:
            req = state.get("pending", {}).get(req_id)
        if not req or req.get('done'):
            with alex_lock:
                if queue and queue[0] == req_id:
                    queue.pop(0)
            return

        # Alex edits its progress/result messages in the same DM. Ignore a
        # message from an earlier request, otherwise a stale result can be
        # accepted as the result for the next queued link.
        incoming_id = getattr(msg, "id", None)
        sent_id = req.get("alex_sent_id")
        if sent_id and incoming_id and incoming_id <= sent_id:
            _trace(
                "ALEX_BOT",
                f"ignored stale message id={incoming_id} for {req['link']} "
                f"(request message id={sent_id})",
            )
            return

        r = parse_alex_message(text, msg.entities or [], req['link'])
        if r and r.get("original") and not _same_link(r["original"], req["link"]):
            _trace(
                "ALEX_BOT",
                f"ignored mismatched result for {req['link']}: "
                f"message original={r['original']}",
            )
            return
        _trace("ALEX_BOT", f"received for {req['link']}: text={text[:500]!r} parsed={r}")
        req["last_alex_ts"] = time.time()
        if r is None:
            folded_text = _fold(text)
            is_known_failure = _RX_ERR.search(folded_text) and not _RX_ALEX_BYPASSED.search(folded_text)
            if (TRACE_BOTS or is_known_failure) \
                    and not _RX_ALEX_PROGRESS.search(folded_text) \
                    and not _RX_RATE.search(folded_text):
                with alex_lock:
                    if queue and queue[0] == req_id:
                        queue.pop(0)
                req['alex_bot_result'] = {
                    "status": "failed",
                    "error": "Alex parser found no bypassed URL",
                    "raw": text[:500],
                }
                req['alex_bot_event'].set()
            return   # intermediate message — keep waiting

        with alex_lock:
            if queue and queue[0] == req_id:
                queue.pop(0)

        req['alex_bot_result'] = r
        req['alex_bot_event'].set()
    return on_dm


async def _run_account(acc_id):
    with accounts_lock:
        state = accounts.get(acc_id)
    if not state:
        return

    api_id   = state['api_id']
    api_hash = state['api_hash']
    sess     = state['session_string']

    loop = asyncio.get_event_loop()
    tg = TelegramClient(
        StringSession(sess), api_id, api_hash,
        connection=ConnectionTcpAbridged,
        auto_reconnect=True, receive_updates=True, loop=loop,
    )
    with accounts_lock:
        accounts[acc_id]['client'] = tg
        accounts[acc_id]['loop']   = loop

    try:
        await tg.start()
        if not await tg.is_user_authorized():
            raise Exception("Session invalid / expired")

        # Resolve DZHQ bot ID
        try:
            ent = await tg.get_entity(DZHQ_BOT)
            with accounts_lock:
                accounts[acc_id]['dzhq_bot_id'] = ent.id
        except:
            pass

        # Resolve Nick bot ID
        try:
            ent3 = await tg.get_entity(NICK_BOT)
            with accounts_lock:
                accounts[acc_id]['nick_bot_id'] = ent3.id
            nick_id = ent3.id
        except:
            nick_id = None
            print(f"[Acc {acc_id}] ⚠️ Could not resolve {NICK_BOT}")

        # Resolve Alex bot ID
        try:
            ent4 = await tg.get_entity(ALEX_BOT)
            with accounts_lock:
                accounts[acc_id]['alex_bot_id'] = ent4.id
            alex_id = ent4.id
        except Exception:
            alex_id = None
            print(f"[Acc {acc_id}] ⚠️ Could not resolve {ALEX_BOT}")

        # Group handlers (DZHQ edits its message too)
        dzhq_h = make_dzhq_handler(acc_id)
        tg.add_event_handler(dzhq_h, events.NewMessage(chats=DZHQ_GROUP))
        tg.add_event_handler(dzhq_h, events.MessageEdited(chats=DZHQ_GROUP))

        # Alex bot DM handler — Alex EDITS the progress message into the result,
        # so MessageEdited must be handled as well.
        alex_h = make_alex_dm_handler(acc_id)
        if alex_id:
            tg.add_event_handler(alex_h, events.NewMessage(from_users=alex_id, incoming=True, func=lambda e: e.is_private))
            tg.add_event_handler(alex_h, events.MessageEdited(from_users=alex_id, incoming=True, func=lambda e: e.is_private))
        else:
            tg.add_event_handler(alex_h, events.NewMessage(incoming=True, func=lambda e: e.is_private))
            tg.add_event_handler(alex_h, events.MessageEdited(incoming=True, func=lambda e: e.is_private))


        # Nick DM handler — listen for private messages from Nick bot
        if nick_id:
            nick_h = make_nick_dm_handler(acc_id)
            # Nick can first send a loading message and then edit that same
            # message into the final result/error.  Listening only for
            # NewMessage leaves the request waiting until it times out.
            tg.add_event_handler(
                nick_h,
                events.NewMessage(from_users=nick_id, incoming=True, func=lambda e: e.is_private)
            )
            tg.add_event_handler(
                nick_h,
                events.MessageEdited(from_users=nick_id, incoming=True, func=lambda e: e.is_private)
            )
        else:
            # Fallback: listen to all private incoming and filter by username
            nick_h = make_nick_dm_handler(acc_id)
            tg.add_event_handler(
                nick_h,
                events.NewMessage(incoming=True, func=lambda e: e.is_private)
            )
            tg.add_event_handler(
                nick_h,
                events.MessageEdited(incoming=True, func=lambda e: e.is_private)
            )

        me = await tg.get_me()
        with accounts_lock:
            accounts[acc_id].update({
                "status":     "active",
                "username":   getattr(me, 'username', None),
                "first_name": getattr(me, 'first_name', None),
                "error_message": None,
            })
        print(f"[Acc {acc_id}] ✅ {me.first_name} (@{me.username})")
        await tg.run_until_disconnected()

    except Exception as e:
        err = str(e)
        print(f"[Acc {acc_id}] ❌ {err}")
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id].update({"status": "error", "error_message": err})

def _thread_target(acc_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_account(acc_id))
    except Exception as e:
        print(f"[Acc {acc_id}] Thread crash: {e}")
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id]['status'] = 'error'

def _start_account_thread(acc_id):
    t = threading.Thread(target=_thread_target, args=(acc_id,), daemon=True)
    t.start()
    with accounts_lock:
        if acc_id in accounts:
            accounts[acc_id]['thread'] = t

def _stop_account(acc_id):
    with accounts_lock:
        state = accounts.get(acc_id)
    if not state:
        return
    tg   = state.get("client")
    loop = state.get("loop")
    if tg and loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(tg.disconnect(), loop).result(timeout=5)
        except:
            pass


# ==================== ACCOUNT SELECTION ====================
def get_next_active():
    global round_robin_idx
    with accounts_lock:
        active = [(k, v) for k, v in accounts.items() if v.get("status") == "active"]
    if not active:
        return None, None
    with rr_lock:
        idx = round_robin_idx % len(active)
        round_robin_idx = (round_robin_idx + 1) % len(active)
    return active[idx]



# ==================== ALEX API + ALEX BOT RACE ====================
def alex_race(link):
    """
    Race the Alex HTTP API against @alexbypassbot (Telegram DM).
    Whichever answers first wins. Returns (winner_dict|None, error_str|None).
    """
    use_bot = bot_settings.get("alex_bot", True)

    race_event  = threading.Event()
    winner      = {}
    win_lock    = threading.Lock()
    errors      = {}
    errors_lock = threading.Lock()
    error_goal  = 1

    def _record_error(source, message):
        nonlocal error_goal
        with errors_lock:
            errors[source] = message
            should_stop = len(errors) >= error_goal
        if should_stop:
            race_event.set()

    def _declare(source, url, extra=None):
        with win_lock:
            if race_event.is_set():
                return
            winner['source'] = source
            winner['url']    = url
            winner['extra']  = extra or {}
            race_event.set()

    # ── Racer 1: Alex HTTP API ────────────────────────────────────────
    def _api_racer():
        res, err = alex_bypass(link)
        if res:
            _declare(
                "alex",
                res["bypassed"],
                {
                    "module": res.get("raw_type", "alex_api"),
                    "account": "alex_api",
                },
            )
        else:
            _record_error("api", err)

    threads = [threading.Thread(target=_api_racer, daemon=True)]

    # ── Racer 2: @alexbypassbot DM ────────────────────────────────────
    acc_id = None
    req_id = None
    req_entry = None
    acc_state = None

    if use_bot:
        acc_id, acc_state = get_next_active()
        error_goal = 1 + int(bool(acc_state))
        if acc_state:
            req_id    = secrets.token_hex(8)
            req_entry = {
                "link":            link,
                "ts":              time.time(),
                "done":            False,
                "alex_sent_id":    None,
                "last_alex_ts":    None,
                "alex_bot_event":  threading.Event(),
                "alex_bot_result": None,
            }
            with accounts_lock:
                accounts[acc_id]["pending"][req_id] = req_entry
                alk = accounts[acc_id].get("alex_dm_lock")
                if alk:
                    with alk:
                        accounts[acc_id]["alex_dm_queue"].append(req_id)

            loop = acc_state.get("loop")

            async def _send_alex():
                sent = await acc_state["client"].send_message(ALEX_BOT, link)
                req_entry["alex_sent_id"] = getattr(sent, "id", None)
                req_entry["last_alex_ts"] = time.time()

            def _bot_racer():
                try:
                    asyncio.run_coroutine_threadsafe(_send_alex(), loop).result(timeout=12)
                except Exception as e:
                    _record_error("bot", f"Alex bot send error: {e}")
                    return
                ev = req_entry["alex_bot_event"]
                while not race_event.is_set():
                    idle_timeout = (
                        BYPASS_IDLE_TIMEOUT_SEC
                        if BYPASS_IDLE_TIMEOUT_SEC > 0
                        else ALEX_BOT_TIMEOUT_SEC
                    )
                    last_seen = req_entry.get("last_alex_ts") or time.time()
                    rem = None if idle_timeout <= 0 else last_seen + idle_timeout - time.time()
                    if rem is not None and rem <= 0:
                        _record_error("bot", "Alex bot idle timeout (no new message)")
                        break
                    if ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                        r = req_entry.get("alex_bot_result")
                        if r and r.get("bypassed"):
                            _declare(
                                "alex_bot",
                                r["bypassed"],
                                {
                                    "module": "alex_bot",
                                    "account": acc_state.get("name") or acc_id,
                                },
                            )
                        else:
                            _record_error(
                                "bot",
                                (r or {}).get("error") or "Alex bot no result",
                            )
                        break

            threads.append(threading.Thread(target=_bot_racer, daemon=True))
        else:
            errors['bot'] = "No active Telegram account for Alex bot"

    for t in threads:
        t.start()

    # Racers use idle deadlines, but the caller still needs an absolute safety
    # cap if a worker crashes before it records an error.
    configured = [
        value
        for value in (ALEX_TIMEOUT_SEC, ALEX_BOT_TIMEOUT_SEC, BYPASS_IDLE_TIMEOUT_SEC)
        if value > 0
    ]
    race_budget = min(
        ALEX_RACE_MAX_TIMEOUT_SEC,
        max(configured, default=30.0) + 5.0,
    )
    if not race_event.wait(timeout=race_budget):
        race_event.set()
        with errors_lock:
            errors.setdefault("race", "Alex resolver timed out")

    # Cleanup the Alex DM queue entry
    if acc_id and req_id:
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id]["pending"].pop(req_id, None)
                alk = accounts[acc_id].get("alex_dm_lock")
                if alk:
                    with alk:
                        q = accounts[acc_id].get("alex_dm_queue", [])
                        if req_id in q:
                            q.remove(req_id)

    if winner.get('url'):
        return winner, None

    msg = " | ".join(f"{k}: {v}" for k, v in errors.items() if v) or "Alex API and Alex bot both failed"
    return None, msg


# ==================== FLASK APP ====================
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.json.sort_keys = False

# ── EXODUS bot system: API-key gate for /bypass (keys come from the Telegram bot) ──
import botsys
botsys.install_api_gate(
    app, DEVELOPER,
    is_admin=lambda: (request.headers.get('X-Admin-Token') or flask_session.get('admin_token')) in admin_tokens,
)


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('X-Admin-Token') or flask_session.get('admin_token')
        if not token or token not in admin_tokens:
            if request.path.startswith('/admin/api/'):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return wrapper


# ==================== ADMIN HTML ====================
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Exodus Admin Panel</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #030712; --bg2: #0d1117; --bg3: #111827;
    --violet: #8b5cf6; --cyan: #06b6d4; --pink: #ec4899;
    --green: #22c55e; --amber: #f59e0b; --red: #ef4444; --blue: #3b82f6;
    --text: #e2e8f0; --muted: #64748b; --border: #1e293b;
    --card: rgba(255,255,255,0.03); --card-border: rgba(255,255,255,0.07);
  }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; overflow-x: hidden; }
  .bubbles { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
  .bubble { position: absolute; border-radius: 50%; opacity: 0.15; animation: float linear infinite; }
  @keyframes float { 0%{transform:translateY(100vh) scale(0);opacity:0} 10%{opacity:0.15} 90%{opacity:0.1} 100%{transform:translateY(-100px) scale(1);opacity:0} }
  .login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; position: relative; z-index: 10; padding: 1rem; }
  .login-card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); backdrop-filter: blur(24px); border-radius: 1.25rem; padding: 2.5rem; width: 100%; max-width: 400px; position: relative; overflow: hidden; box-shadow: 0 25px 50px rgba(0,0,0,0.8); animation: slideUp 0.5s ease; }
  @keyframes slideUp { from{opacity:0;transform:translateY(24px) scale(0.97)} to{opacity:1;transform:none} }
  .login-card::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:linear-gradient(90deg,var(--violet),var(--cyan),var(--pink)); }
  .login-icon { width:64px; height:64px; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); border-radius:1rem; display:flex; align-items:center; justify-content:center; margin:0 auto 1.5rem; font-size:2rem; }
  .login-title { text-align:center; font-size:1.75rem; font-weight:900; text-transform:uppercase; letter-spacing:.1em; background:linear-gradient(90deg,var(--violet),var(--cyan),var(--pink)); background-size:200%; -webkit-background-clip:text; -webkit-text-fill-color:transparent; animation:grad 6s ease infinite; background-clip:text; margin-bottom:.25rem; }
  @keyframes grad { 0%,100%{background-position:0% 50%} 50%{background-position:100% 50%} }
  .login-sub { text-align:center; color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:.75rem; letter-spacing:.15em; margin-bottom:2rem; }
  .form-group { margin-bottom:1rem; }
  .form-input { width:100%; background:rgba(0,0,0,0.4); border:1px solid rgba(255,255,255,0.1); border-radius:.75rem; padding:1rem; color:var(--text); font-family:'JetBrains Mono',monospace; font-size:.9rem; letter-spacing:.2em; text-align:center; outline:none; transition:border-color .2s; }
  .form-input:focus { border-color:var(--violet); box-shadow:0 0 0 2px rgba(139,92,246,0.2); }
  .btn-primary { width:100%; padding:1rem; background:linear-gradient(135deg,var(--violet),var(--cyan)); border:none; border-radius:.75rem; color:white; font-weight:700; font-size:.85rem; letter-spacing:.15em; text-transform:uppercase; cursor:pointer; transition:opacity .2s,transform .1s; box-shadow:0 0 20px rgba(139,92,246,0.4); }
  .btn-primary:hover { opacity:.9; }
  .btn-primary:active { transform:scale(.98); }
  .btn-primary:disabled { opacity:.5; cursor:not-allowed; }
  .err-msg { color:var(--red); font-size:.8rem; margin-top:.75rem; text-align:center; font-family:'JetBrains Mono',monospace; }
  .app { display:none; min-height:100vh; }
  .app.visible { display:flex; }
  .sidebar { width:240px; min-height:100vh; background:var(--bg2); border-right:1px solid var(--border); display:flex; flex-direction:column; position:sticky; top:0; height:100vh; overflow-y:auto; z-index:10; }
  .sidebar-logo { padding:1.5rem 1.25rem; border-bottom:1px solid var(--border); }
  .sidebar-logo-text { font-weight:900; font-size:1rem; text-transform:uppercase; letter-spacing:.1em; background:linear-gradient(90deg,var(--violet),var(--cyan)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; animation:grad 6s ease infinite; background-size:200%; }
  .sidebar-sub { font-size:.65rem; color:var(--muted); font-family:'JetBrains Mono',monospace; letter-spacing:.1em; margin-top:.2rem; }
  .sidebar-nav { flex:1; padding:.75rem; }
  .nav-item { display:flex; align-items:center; gap:.75rem; padding:.65rem .875rem; border-radius:.625rem; color:var(--muted); font-size:.85rem; font-weight:500; cursor:pointer; transition:all .15s; margin-bottom:.25rem; border:none; background:none; width:100%; text-align:left; }
  .nav-item:hover { background:rgba(255,255,255,0.04); color:var(--text); }
  .nav-item.active { background:rgba(139,92,246,0.15); color:var(--violet); border:1px solid rgba(139,92,246,0.2); }
  .nav-icon { font-size:1rem; width:1.25rem; text-align:center; }
  .sidebar-footer { padding:.75rem; border-top:1px solid var(--border); }
  .btn-logout { display:flex; align-items:center; gap:.75rem; padding:.65rem .875rem; border-radius:.625rem; color:var(--red); font-size:.85rem; font-weight:500; cursor:pointer; transition:all .15s; border:none; background:none; width:100%; }
  .btn-logout:hover { background:rgba(239,68,68,0.1); }
  .main { flex:1; overflow-y:auto; background:var(--bg); position:relative; z-index:5; }
  .page { display:none; padding:2rem; max-width:1200px; }
  .page.active { display:block; }
  .page-header { margin-bottom:2rem; }
  .page-title { font-size:1.75rem; font-weight:900; text-transform:uppercase; letter-spacing:.08em; background:linear-gradient(90deg,var(--violet),var(--cyan),var(--pink)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; animation:grad 6s ease infinite; background-size:200%; }
  .page-sub { color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:.75rem; letter-spacing:.1em; margin-top:.25rem; }
  .glass { background:var(--card); border:1px solid var(--card-border); backdrop-filter:blur(12px); border-radius:1rem; }
  .stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:1rem; margin-bottom:2rem; }
  .stat-card { padding:1.25rem; position:relative; overflow:hidden; }
  .stat-card-icon { font-size:1.5rem; margin-bottom:.75rem; }
  .stat-card-value { font-size:2rem; font-weight:900; margin-bottom:.25rem; }
  .stat-card-label { font-size:.7rem; color:var(--muted); font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:.1em; }
  .badge { display:inline-flex; align-items:center; gap:.35rem; padding:.25rem .625rem; border-radius:.375rem; font-size:.7rem; font-weight:700; font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:.08em; border:1px solid transparent; }
  .badge-active { background:rgba(34,197,94,.12); color:var(--green); border-color:rgba(34,197,94,.25); }
  .badge-paused { background:rgba(245,158,11,.12); color:var(--amber); border-color:rgba(245,158,11,.25); }
  .badge-error { background:rgba(239,68,68,.12); color:var(--red); border-color:rgba(239,68,68,.25); }
  .badge-connecting { background:rgba(59,130,246,.12); color:var(--blue); border-color:rgba(59,130,246,.25); }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .dot-active { background:var(--green); box-shadow:0 0 6px var(--green); animation:pulse 2s infinite; }
  .dot-paused { background:var(--amber); }
  .dot-error { background:var(--red); box-shadow:0 0 6px var(--red); animation:pulse 1s infinite; }
  .dot-connecting { background:var(--blue); animation:spin 1s linear infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  @keyframes spin { to{transform:rotate(360deg)} }
  .table-wrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; }
  th { padding:.875rem 1.25rem; text-align:left; font-size:.7rem; font-weight:600; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; font-family:'JetBrains Mono',monospace; border-bottom:1px solid var(--border); background:rgba(255,255,255,.02); }
  td { padding:.875rem 1.25rem; font-size:.85rem; border-bottom:1px solid rgba(255,255,255,.04); vertical-align:middle; }
  tr:last-child td { border-bottom:none; }
  tr:hover td { background:rgba(255,255,255,.02); }
  .btn { display:inline-flex; align-items:center; gap:.4rem; padding:.4rem .75rem; border-radius:.5rem; font-size:.78rem; font-weight:600; cursor:pointer; transition:all .15s; border:1px solid transparent; font-family:inherit; }
  .btn-sm { padding:.3rem .6rem; font-size:.72rem; }
  .btn-violet { background:rgba(139,92,246,.15); color:var(--violet); border-color:rgba(139,92,246,.3); }
  .btn-violet:hover { background:rgba(139,92,246,.25); }
  .btn-cyan { background:rgba(6,182,212,.15); color:var(--cyan); border-color:rgba(6,182,212,.3); }
  .btn-cyan:hover { background:rgba(6,182,212,.25); }
  .btn-green { background:rgba(34,197,94,.15); color:var(--green); border-color:rgba(34,197,94,.3); }
  .btn-green:hover { background:rgba(34,197,94,.25); }
  .btn-amber { background:rgba(245,158,11,.15); color:var(--amber); border-color:rgba(245,158,11,.25); }
  .btn-amber:hover { background:rgba(245,158,11,.25); }
  .btn-red { background:rgba(239,68,68,.15); color:var(--red); border-color:rgba(239,68,68,.3); }
  .btn-red:hover { background:rgba(239,68,68,.25); }
  .btn-full { width:100%; justify-content:center; padding:.75rem; font-size:.85rem; }
  .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); backdrop-filter:blur(8px); z-index:1000; align-items:center; justify-content:center; padding:1rem; }
  .modal-overlay.open { display:flex; }
  .modal { background:var(--bg2); border:1px solid rgba(255,255,255,.1); border-radius:1.25rem; width:100%; max-width:480px; overflow:hidden; box-shadow:0 25px 50px rgba(0,0,0,.8); animation:slideUp .3s ease; }
  .modal-header { padding:1.25rem 1.5rem; border-bottom:1px solid var(--border); background:rgba(255,255,255,.02); }
  .modal-title { font-weight:800; text-transform:uppercase; letter-spacing:.08em; font-size:1rem; background:linear-gradient(90deg,var(--violet),var(--cyan)); -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text; }
  .modal-body { padding:1.5rem; }
  .modal-footer { padding:1rem 1.5rem; border-top:1px solid var(--border); display:flex; gap:.75rem; justify-content:flex-end; }
  .field { margin-bottom:1rem; }
  .field label { display:block; font-size:.72rem; color:var(--muted); font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:.1em; margin-bottom:.4rem; }
  .field input, .field textarea { width:100%; background:rgba(0,0,0,.4); border:1px solid rgba(255,255,255,.1); border-radius:.625rem; padding:.65rem .875rem; color:var(--text); font-family:'JetBrains Mono',monospace; font-size:.8rem; outline:none; transition:border-color .2s; resize:vertical; }
  .field input:focus, .field textarea:focus { border-color:var(--violet); box-shadow:0 0 0 2px rgba(139,92,246,.15); }
  .field-row { display:grid; grid-template-columns:1fr 1fr; gap:.75rem; }
  .section-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem; flex-wrap:wrap; gap:.75rem; }
  .actions-bar { display:flex; gap:.5rem; flex-wrap:wrap; }
  .text-mono { font-family:'JetBrains Mono',monospace; }
  .text-muted { color:var(--muted); }
  .text-xs { font-size:.72rem; }
  .empty-state { padding:3rem; text-align:center; color:var(--muted); font-family:'JetBrains Mono',monospace; font-size:.75rem; text-transform:uppercase; letter-spacing:.1em; }
  .toast-container { position:fixed; bottom:1.5rem; right:1.5rem; z-index:9999; display:flex; flex-direction:column; gap:.5rem; }
  .toast { padding:.75rem 1.25rem; border-radius:.625rem; font-size:.82rem; font-weight:600; animation:slideUp .3s ease; max-width:300px; border:1px solid transparent; }
  .toast-success { background:rgba(34,197,94,.15); color:var(--green); border-color:rgba(34,197,94,.3); }
  .toast-error { background:rgba(239,68,68,.15); color:var(--red); border-color:rgba(239,68,68,.3); }
  .refresh-indicator { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--cyan); animation:pulse 1s infinite; margin-left:.5rem; }
  .toggle-switch { position:relative; width:44px; height:24px; }
  .toggle-switch input { opacity:0; width:0; height:0; }
  .toggle-slider { position:absolute; cursor:pointer; inset:0; background:#374151; border-radius:24px; transition:.3s; }
  .toggle-slider::before { position:absolute; content:""; height:18px; width:18px; left:3px; bottom:3px; background:#fff; border-radius:50%; transition:.3s; }
  input:checked + .toggle-slider { background:var(--green); }
  input:checked + .toggle-slider::before { transform:translateX(20px); }
  @media (max-width:640px) { .sidebar{width:56px} .sidebar-logo-text,.sidebar-sub,.nav-item span{display:none} .nav-item{justify-content:center} .page{padding:1rem} }
</style>
</head>
<body>
<div class="bubbles" id="bubbles"></div>

<!-- Login -->
<div class="login-wrap" id="loginScreen">
  <div class="login-card">
    <div class="login-icon">🔐</div>
    <div class="login-title">Exodus Admin</div>
    <div class="login-sub">SECURE TERMINAL ACCESS</div>
    <form id="loginForm">
      <div class="form-group">
        <input class="form-input" type="password" id="passwordInput" placeholder="Enter access code" autocomplete="off">
      </div>
      <button class="btn-primary" type="submit" id="loginBtn">INITIALIZE</button>
      <div class="err-msg" id="loginErr" style="display:none"></div>
    </form>
  </div>
</div>

<!-- Main App -->
<div class="app" id="mainApp">
  <nav class="sidebar">
    <div class="sidebar-logo">
      <div class="sidebar-logo-text">Exodus Admin</div>
      <div class="sidebar-sub">BYPASS CONTROL</div>
    </div>
    <div class="sidebar-nav">
      <button class="nav-item active" onclick="showPage('dashboard')" id="nav-dashboard"><span class="nav-icon">📊</span><span>Dashboard</span></button>
      <button class="nav-item" onclick="showPage('accounts')" id="nav-accounts"><span class="nav-icon">👤</span><span>Accounts</span></button>
      <button class="nav-item" onclick="showPage('bots')" id="nav-bots"><span class="nav-icon">⚡</span><span>Bots</span></button>
    </div>
    <div class="sidebar-footer">
      <button class="btn-logout" onclick="logout()"><span class="nav-icon">🚪</span><span>Logout</span></button>
    </div>
  </nav>

  <main class="main">
    <!-- Dashboard -->
    <div class="page active" id="page-dashboard">
      <div class="page-header">
        <div class="page-title">System Overview</div>
        <div class="page-sub">LIVE STATUS <span class="refresh-indicator"></span></div>
      </div>
      <div class="stat-grid" id="statGrid"></div>
      <div class="section-header">
        <div style="font-weight:700;font-size:.9rem;">Quick Actions</div>
        <div class="actions-bar">
          <button class="btn btn-green" onclick="apiCall('POST','/admin/api/resume-all').then(()=>{toast('All resumed','success');refresh()})">▶ Resume All</button>
          <button class="btn btn-amber" onclick="apiCall('POST','/admin/api/pause-all').then(()=>{toast('All paused','success');refresh()})">⏸ Pause All</button>
        </div>
      </div>
      <div class="glass" style="margin-top:1rem">
        <table>
          <thead><tr><th>Account</th><th>Status</th><th>Bypasses</th><th>Win Rate</th><th>Last Used</th></tr></thead>
          <tbody id="dashTable"><tr><td colspan="5" class="empty-state">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- Accounts -->
    <div class="page" id="page-accounts">
      <div class="page-header">
        <div class="page-title">Accounts</div>
        <div class="page-sub">MANAGE TELEGRAM SESSIONS</div>
      </div>
      <div class="section-header">
        <div class="actions-bar">
          <button class="btn btn-violet" onclick="openAddModal()">➕ Add Account</button>
          <button class="btn btn-green" onclick="apiCall('POST','/admin/api/resume-all').then(()=>{toast('Resumed','success');refresh()})">▶ Resume All</button>
          <button class="btn btn-amber" onclick="apiCall('POST','/admin/api/pause-all').then(()=>{toast('Paused','success');refresh()})">⏸ Pause All</button>
        </div>
      </div>
      <div class="glass" style="margin-top:1rem">
        <div class="table-wrap">
          <table>
            <thead><tr><th>Account</th><th>Status</th><th>Stats</th><th>Error</th><th>Actions</th></tr></thead>
            <tbody id="accTable"><tr><td colspan="5" class="empty-state">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Bot Toggles -->
    <div class="page" id="page-bots">
      <div class="page-header">
        <div class="page-title">Bot Control</div>
        <div class="page-sub">ENABLE / DISABLE RACE BOTS — AT LEAST ONE MUST STAY ON</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-bottom:1.5rem;">

        <!-- DZHQ Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(139,92,246,.15);border:1px solid rgba(139,92,246,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🏆</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">DZHQ Group Bot</div>
              <div class="text-xs text-muted text-mono">@DZHQ_BypassBot · Group chat</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="dzhqLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Sends /b link to DZHQ group</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="dzhqToggle" onchange="toggleBot('dzhq', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;font-size:.75rem;color:var(--muted);line-height:1.6;">
            Races in parallel with Nick. Whichever replies first wins. If disabled, only Nick runs.
          </div>
        </div>

        <!-- Nick Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(6,182,212,.15);border:1px solid rgba(6,182,212,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚡</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Nick DM Bot</div>
              <div class="text-xs text-muted text-mono">@Nick_Bypass_Bot · Direct message</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="nickLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Sends link via private DM</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="nickToggle" onchange="toggleBot('nick', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;font-size:.75rem;color:var(--muted);line-height:1.6;">
            Races in parallel with DZHQ. If disabled, only DZHQ runs.
          </div>
        </div>

        <!-- Alex Bot Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(236,72,153,.15);border:1px solid rgba(236,72,153,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🅰️</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Alex DM Bot</div>
              <div class="text-xs text-muted text-mono">@alexbypassbot · Direct message</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="alexBotLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Races the Alex API on URLKing / MonteOlympus links</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="alexBotToggle" onchange="toggleBot('alex_bot', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;font-size:.75rem;color:var(--muted);line-height:1.6;">
            On = Alex API aur @alexbypassbot dono ek saath try karte hain — jo pehle jawab de wohi jeetta hai. Off = sirf Alex API.
          </div>
        </div>
      </div>


        <!-- DZHQ First Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚙️</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">DZHQ First Mode</div>
              <div class="text-xs text-muted text-mono">DZHQ pehle koshish karta hai — Nick sirf tab kaam karta hai jab DZHQ fail ho</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="dzhqFirstLabel">Disabled (Parallel Race)</div>
              <div class="text-xs text-muted text-mono">Off = Both run together &nbsp;|&nbsp; On = DZHQ first, Nick as fallback</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="dzhqFirstToggle" onchange="toggleBot('dzhq_first', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div style="margin-top:.875rem;display:grid;grid-template-columns:1fr 1fr;gap:.75rem;">
            <div style="padding:.75rem 1rem;background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.2);border-radius:.5rem;font-size:.75rem;color:var(--muted);line-height:1.7;">
              <div style="color:var(--text);font-weight:600;margin-bottom:.25rem;">🔴 OFF — Parallel Race</div>
              DZHQ + Nick dono ek saath chaltay hain. Jo pehle jawab de woh jeet jaata hai.
            </div>
            <div style="padding:.75rem 1rem;background:rgba(251,191,36,.06);border:1px solid rgba(251,191,36,.2);border-radius:.5rem;font-size:.75rem;color:var(--muted);line-height:1.7;">
              <div style="color:var(--text);font-weight:600;margin-bottom:.25rem;">🟡 ON — DZHQ First</div>
              DZHQ pehle koshish karta hai. Agar DZHQ succeed kare to Nick ko message hi nahi jaata. DZHQ fail ho to Nick fallback karta hai.
            </div>
          </div>
        </div>

        <!-- Nick First Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(56,189,248,.12);border:1px solid rgba(56,189,248,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚡</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Nick First Mode</div>
              <div class="text-xs text-muted text-mono">Nick pehle koshish karta hai — DZHQ sirf tab kaam karta hai jab Nick fail ho</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="nickFirstLabel">Disabled (Parallel Race)</div>
              <div class="text-xs text-muted text-mono">Off = Both run together &nbsp;|&nbsp; On = Nick first, DZHQ as fallback</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="nickFirstToggle" onchange="toggleBot('nick_first', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>

        <!-- Random Mode Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🎲</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Random Mode</div>
              <div class="text-xs text-muted text-mono">Kabhi 5 link Nick pe, kabhi 10 link DZHQ pe — dono bots baari-baari switch hote rehte hain</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="randomModeLabel">Disabled</div>
              <div class="text-xs text-muted text-mono" id="randomModeInfo">Har switch par 3-10 links ka random batch ek hi bot ko jaata hai</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="randomModeToggle" onchange="toggleBot('random_mode', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>

      <!-- Race mode info banner -->
      <div class="glass" style="padding:1.25rem 1.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
        <div style="font-size:1.5rem;">🏁</div>
        <div>
          <div style="font-weight:700;font-size:.9rem;margin-bottom:.2rem;" id="raceModeText">2-Way Race Active</div>
          <div class="text-xs text-muted text-mono" id="raceModeDesc">Both bots receive the link simultaneously — first valid response wins and is returned to the caller.</div>
        </div>
        <div id="raceBadge" class="badge badge-active" style="margin-left:auto;">RACING</div>
      </div>
    </div>

  </main>
</div>

<!-- Add Account Modal -->
<div class="modal-overlay" id="addModal">
  <div class="modal">
    <div class="modal-header"><div class="modal-title">Initialize Account</div></div>
    <div class="modal-body">
      <div class="field"><label>Account Name / Label</label><input type="text" id="newName" placeholder="e.g. Worker-01"></div>
      <div class="field-row">
        <div class="field"><label>API ID</label><input type="number" id="newApiId" placeholder="12345678"></div>
        <div class="field"><label>API Hash</label><input type="text" id="newApiHash" placeholder="abcdef..."></div>
      </div>
      <div class="field"><label>Session String</label><textarea id="newSession" rows="5" placeholder="Paste Telethon session string..."></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('addModal')">Cancel</button>
      <button class="btn btn-violet" onclick="submitAddAccount()">Deploy</button>
    </div>
  </div>
</div>

<!-- Update Session Modal -->
<div class="modal-overlay" id="sessionModal">
  <div class="modal">
    <div class="modal-header"><div class="modal-title" id="sessionModalTitle">Update Session</div></div>
    <div class="modal-body">
      <div class="field"><label>New Session String</label><textarea id="newSessionStr" rows="6" placeholder="Paste new Telethon session string here..."></textarea></div>
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('sessionModal')">Cancel</button>
      <button class="btn btn-cyan" onclick="submitUpdateSession()">Update Session</button>
    </div>
  </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<script>
const TOKEN_KEY = 'Exodus_admin_token';
let token = localStorage.getItem(TOKEN_KEY) || '';
let currentSessionAccId = null;
let refreshTimer = null;

// Bubbles
(function() {
  const colors = ['#8b5cf6','#06b6d4','#ec4899','#3b82f6','#22c55e','#f59e0b'];
  const wrap = document.getElementById('bubbles');
  for (let i = 0; i < 28; i++) {
    const b = document.createElement('div');
    b.className = 'bubble';
    const size = Math.random()*60+20;
    b.style.cssText = `width:${size}px;height:${size}px;left:${Math.random()*100}%;background:${colors[Math.floor(Math.random()*colors.length)]};animation-duration:${Math.random()*15+10}s;animation-delay:${Math.random()*-20}s;filter:blur(${Math.random()*3+1}px);`;
    wrap.appendChild(b);
  }
})();

async function apiCall(method, path, body) {
  const opts = { method, headers: { 'X-Admin-Token': token, 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401) { logout(); throw new Error('Unauthorized'); }
  return r.json();
}

function toast(msg, type='success') {
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.textContent = msg;
  document.getElementById('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

if (token) showApp();

document.getElementById('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const pw = document.getElementById('passwordInput').value;
  const btn = document.getElementById('loginBtn');
  btn.disabled = true; btn.textContent = 'AUTHENTICATING...';
  document.getElementById('loginErr').style.display = 'none';
  try {
    const r = await fetch('/admin/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pw}) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Access denied');
    token = d.token;
    localStorage.setItem(TOKEN_KEY, token);
    showApp();
  } catch(e) {
    const el = document.getElementById('loginErr');
    el.textContent = e.message; el.style.display = 'block';
    document.getElementById('passwordInput').value = '';
  } finally { btn.disabled = false; btn.textContent = 'INITIALIZE'; }
});

function showApp() {
  document.getElementById('loginScreen').style.display = 'none';
  document.getElementById('mainApp').classList.add('visible');
  startRefresh();
  loadBotsStatus();
}

function logout() {
  localStorage.removeItem(TOKEN_KEY); token = '';
  stopRefresh();
  document.getElementById('mainApp').classList.remove('visible');
  document.getElementById('loginScreen').style.display = 'flex';
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  document.getElementById('nav-'+name).classList.add('active');
}

function startRefresh() { refresh(); refreshTimer = setInterval(refresh, 5000); }
function stopRefresh() { if (refreshTimer) clearInterval(refreshTimer); }

async function refresh() {
  try {
    const data = await apiCall('GET', '/admin/api/accounts');
    renderDashboard(data);
    renderAccounts(data);
  } catch(e) {}
}

function renderDashboard(accs) {
  const total    = accs.length;
  const active   = accs.filter(a=>a.status==='active').length;
  const paused   = accs.filter(a=>a.status==='paused').length;
  const errored  = accs.filter(a=>a.status==='error').length;
  const bypasses = accs.reduce((s,a)=>s+a.bypass_count,0);
  const succ     = accs.reduce((s,a)=>s+a.success_count,0);
  const rate     = bypasses>0 ? ((succ/bypasses)*100).toFixed(1) : 0;
  const stats = [
    {label:'Total Accounts', value:total,    color:'var(--violet)', icon:'👥'},
    {label:'Active',         value:active,   color:'var(--green)',  icon:'✅'},
    {label:'Paused',         value:paused,   color:'var(--amber)',  icon:'⏸'},
    {label:'Error',          value:errored,  color:'var(--red)',    icon:'⚠️'},
    {label:'Total Bypasses', value:bypasses.toLocaleString(), color:'var(--cyan)', icon:'⚡'},
    {label:'Success Rate',   value:rate+'%', color:'var(--pink)',   icon:'🎯'},
  ];
  document.getElementById('statGrid').innerHTML = stats.map(s=>`<div class="glass stat-card"><div class="stat-card-icon">${s.icon}</div><div class="stat-card-value" style="color:${s.color}">${s.value}</div><div class="stat-card-label">${s.label}</div></div>`).join('');
  document.getElementById('dashTable').innerHTML = accs.length ? accs.map(a=>`<tr><td><b>${esc(a.name)}</b>${a.username?`<div class="text-xs text-muted text-mono">@${esc(a.username)}</div>`:''}</td><td>${statusBadge(a.status)}</td><td class="text-mono text-xs">${a.bypass_count.toLocaleString()}</td><td class="text-mono" style="color:var(--cyan)">${a.bypass_count>0?((a.success_count/a.bypass_count)*100).toFixed(1)+'%':'-'}</td><td class="text-xs text-muted">${a.last_used?timeAgo(a.last_used):'Never'}</td></tr>`).join('') : '<tr><td colspan="5" class="empty-state">No accounts</td></tr>';
}

function renderAccounts(accs) {
  document.getElementById('accTable').innerHTML = accs.length ? accs.map(a=>`<tr><td><b>${esc(a.name)}</b>${a.first_name||a.username?`<div class="text-xs text-muted text-mono">${a.first_name?esc(a.first_name):''}${a.username?' @'+esc(a.username):''}</div>`:''}<div class="text-xs text-muted text-mono">ID: ${a.api_id}</div></td><td>${statusBadge(a.status)}</td><td class="text-mono text-xs"><div>Bypasses: <b>${a.bypass_count}</b></div><div style="color:var(--green)">Success: ${a.success_count}</div><div style="color:var(--red)">Failed: ${a.fail_count}</div></td><td class="text-xs" style="color:var(--red);max-width:180px;word-break:break-word">${a.error_message?esc(a.error_message):'-'}</td><td><div style="display:flex;gap:.375rem;flex-wrap:wrap">${a.status==='paused'||a.status==='error'?`<button class="btn btn-sm btn-green" onclick="toggleAcc('${a.id}','resume')">▶ Resume</button>`:`<button class="btn btn-sm btn-amber" onclick="toggleAcc('${a.id}','pause')">⏸ Pause</button>`}<button class="btn btn-sm btn-cyan" onclick="openSessionModal('${a.id}','${esc(a.name)}')">🔑 Session</button><button class="btn btn-sm btn-red" onclick="deleteAcc('${a.id}')">🗑</button></div></td></tr>`).join('') : '<tr><td colspan="5" class="empty-state">No accounts. Click "Add Account" to start.</td></tr>';
}

function statusBadge(s) {
  const cls = {active:'badge-active',paused:'badge-paused',error:'badge-error',connecting:'badge-connecting'};
  const dotCls = {active:'dot-active',paused:'dot-paused',error:'dot-error',connecting:'dot-connecting'};
  return `<span class="badge ${cls[s]||'badge-connecting'}"><span class="dot ${dotCls[s]||'dot-connecting'}"></span>${s}</span>`;
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function timeAgo(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff/60000);
  if (m < 1)  return 'just now';
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m/60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h/24) + 'd ago';
}

async function toggleAcc(id, action) {
  try { await apiCall('POST', `/admin/api/accounts/${id}/${action}`); toast(`Account ${action}d`,'success'); refresh(); }
  catch(e) { toast(e.message,'error'); }
}
async function deleteAcc(id) {
  if (!confirm('Delete this account?')) return;
  try { await apiCall('DELETE', `/admin/api/accounts/${id}`); toast('Account deleted','success'); refresh(); }
  catch(e) { toast(e.message,'error'); }
}

function openAddModal() {
  ['newName','newApiId','newApiHash','newSession'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('addModal').classList.add('open');
}
async function submitAddAccount() {
  const name=document.getElementById('newName').value.trim();
  const apiId=document.getElementById('newApiId').value.trim();
  const apiHash=document.getElementById('newApiHash').value.trim();
  const sess=document.getElementById('newSession').value.trim();
  if (!name||!apiId||!apiHash||!sess){toast('All fields required','error');return;}
  try { await apiCall('POST','/admin/api/accounts',{name,api_id:parseInt(apiId),api_hash:apiHash,session_string:sess}); toast('Account added!','success'); closeModal('addModal'); refresh(); }
  catch(e){toast(e.message,'error');}
}

function openSessionModal(id,name) {
  currentSessionAccId=id;
  document.getElementById('sessionModalTitle').textContent=`Update Session: ${name}`;
  document.getElementById('newSessionStr').value='';
  document.getElementById('sessionModal').classList.add('open');
}
async function submitUpdateSession() {
  const sess=document.getElementById('newSessionStr').value.trim();
  if (!sess){toast('Session string required','error');return;}
  try { await apiCall('PUT',`/admin/api/accounts/${currentSessionAccId}/session`,{session_string:sess}); toast('Session updated! Reconnecting...','success'); closeModal('sessionModal'); refresh(); }
  catch(e){toast(e.message,'error');}
}

function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(el=>{ el.addEventListener('click',e=>{if(e.target===el)el.classList.remove('open');}); });

// ── Bot Toggles ───────────────────────────────────────────────────────
async function loadBotsStatus() {
  try {
    const d = await apiCall('GET', '/admin/api/bots/status');
    _applyBotState(d);
  } catch(e) {}
}

function _applyBotState(d) {
  const dEl    = document.getElementById('dzhqToggle');
  const nEl    = document.getElementById('nickToggle');
  const dfEl   = document.getElementById('dzhqFirstToggle');
  const nfEl   = document.getElementById('nickFirstToggle');
  const rmEl   = document.getElementById('randomModeToggle');
  const nfLbl  = document.getElementById('nickFirstLabel');
  const rmLbl  = document.getElementById('randomModeLabel');
  const rmInfo = document.getElementById('randomModeInfo');
  const abEl   = document.getElementById('alexBotToggle');
  const abLbl  = document.getElementById('alexBotLabel');
  const dLbl   = document.getElementById('dzhqLabel');
  const nLbl   = document.getElementById('nickLabel');
  const dfLbl  = document.getElementById('dzhqFirstLabel');
  const raceText  = document.getElementById('raceModeText');
  const raceDesc  = document.getElementById('raceModeDesc');
  const raceBadge = document.getElementById('raceBadge');

  if (dEl)  dEl.checked  = d.dzhq;
  if (nEl)  nEl.checked  = d.nick;
  if (dfEl) dfEl.checked = !!d.dzhq_first;
  if (nfEl) nfEl.checked = !!d.nick_first;
  if (rmEl) rmEl.checked = !!d.random_mode;
  if (nfLbl) { nfLbl.textContent = d.nick_first ? 'ON — Nick First Mode' : 'Disabled (Parallel Race)'; nfLbl.style.color = d.nick_first ? 'var(--cyan)' : 'var(--muted)'; }
  if (rmLbl) { rmLbl.textContent = d.random_mode ? 'ON — Random Switching' : 'Disabled'; rmLbl.style.color = d.random_mode ? 'var(--green)' : 'var(--muted)'; }
  if (rmInfo) {
    if (d.random_mode && d.random && d.random.current) {
      rmInfo.textContent = `Abhi ${d.random.current.toUpperCase()} chal raha hai — ${d.random.remaining}/${d.random.batch} links baaki, phir doosre bot par switch`;
    } else {
      rmInfo.textContent = 'Har switch par 3-10 links ka random batch ek hi bot ko jaata hai';
    }
  }
  if (abEl) abEl.checked = (d.alex_bot !== false);
  if (abLbl) { const on = (d.alex_bot !== false); abLbl.textContent = on ? 'Enabled' : 'Disabled'; abLbl.style.color = on ? 'var(--green)' : 'var(--red)'; }

  if (dLbl)  { dLbl.textContent  = d.dzhq       ? 'Enabled'               : 'Disabled'; dLbl.style.color  = d.dzhq       ? 'var(--green)' : 'var(--red)'; }
  if (nLbl)  { nLbl.textContent  = d.nick       ? 'Enabled'               : 'Disabled'; nLbl.style.color  = d.nick       ? 'var(--green)' : 'var(--red)'; }
  if (dfLbl) { dfLbl.textContent = d.dzhq_first ? 'ON — DZHQ First Mode'  : 'Disabled (Parallel Race)'; dfLbl.style.color = d.dzhq_first ? 'var(--amber)' : 'var(--muted)'; }

  if (raceText && raceBadge) {
    if (d.dzhq && d.nick && d.random_mode) {
      raceText.textContent = 'Random Mode — Auto Switching';
      if (raceDesc) raceDesc.textContent = 'Ek bot ko 3-10 links ka batch jaata hai, phir doosre bot par switch ho jaata hai. Fail hone par doosra bot fallback karta hai.';
      raceBadge.className = 'badge badge-active';
      raceBadge.textContent = 'RANDOM';
      raceBadge.style.background = 'rgba(139,92,246,.2)';
      raceBadge.style.color = '#a78bfa';
    } else if (d.dzhq && d.nick && d.nick_first) {
      raceText.textContent = 'Nick First Mode — DZHQ as Fallback';
      if (raceDesc) raceDesc.textContent = 'Nick pehle koshish karta hai. Nick succeed ho to DZHQ ko message nahi jaata. Nick fail ho to DZHQ try karta hai.';
      raceBadge.className = 'badge badge-active';
      raceBadge.textContent = 'NICK FIRST';
      raceBadge.style.background = 'rgba(56,189,248,.2)';
      raceBadge.style.color = '#38bdf8';
    } else if (d.dzhq && d.nick && d.dzhq_first) {
      raceText.textContent = 'DZHQ First Mode — Nick as Fallback';
      if (raceDesc) raceDesc.textContent = 'DZHQ pehle koshish karta hai. DZHQ succeed ho to Nick ko message nahi jaata. DZHQ fail ho to Nick try karta hai.';
      raceBadge.className = 'badge badge-amber';
      raceBadge.textContent = 'DZHQ FIRST';
      raceBadge.style.background = 'rgba(251,191,36,.2)';
      raceBadge.style.color = 'var(--amber)';
    } else if (d.dzhq && d.nick) {
      raceText.textContent = '2-Way Race Active — DZHQ + Nick';
      if (raceDesc) raceDesc.textContent = 'Both bots receive the link simultaneously — first valid response wins.';
      raceBadge.className = 'badge badge-active';
      raceBadge.textContent = 'RACING';
      raceBadge.style.background = '';
      raceBadge.style.color = '';
    } else if (d.dzhq) {
      raceText.textContent = 'Solo Mode — DZHQ Only';
      if (raceDesc) raceDesc.textContent = 'Only DZHQ group bot is active.';
      raceBadge.className = 'badge badge-paused';
      raceBadge.textContent = 'SOLO';
      raceBadge.style.background = '';
      raceBadge.style.color = '';
    } else {
      raceText.textContent = 'Solo Mode — Nick Only';
      if (raceDesc) raceDesc.textContent = 'Only Nick DM bot is active.';
      raceBadge.className = 'badge badge-paused';
      raceBadge.textContent = 'SOLO';
      raceBadge.style.background = '';
      raceBadge.style.color = '';
    }
  }
}

async function toggleBot(bot, enabled) {
  try {
    const d = await apiCall('POST', '/admin/api/bots/toggle', {bot, enabled});
    if (!d.success) { toast(d.error || 'Cannot disable both bots', 'error'); loadBotsStatus(); return; }
    _applyBotState(d);
    const labels = { dzhq: 'DZHQ', nick: 'Nick', dzhq_first: 'DZHQ First Mode', nick_first: 'Nick First Mode', random_mode: 'Random Mode', alex_bot: 'Alex DM Bot' };
    toast(`${labels[bot] || bot} ${enabled ? 'enabled' : 'disabled'}`, 'success');
  } catch(e) { toast(e.message, 'error'); loadBotsStatus(); }
}
</script>
</body>
</html>"""


# ==================== ADMIN API ROUTES ====================
@app.route('/admin')
@app.route('/admin/')
def admin_index():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/login')
def admin_login_page():
    return render_template_string(ADMIN_HTML)

@app.route('/admin/api/login', methods=['POST'])
def admin_api_login():
    d = request.get_json(silent=True) or {}
    if d.get('password') != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    token = secrets.token_hex(32)
    admin_tokens.add(token)
    threading.Timer(86400, lambda: admin_tokens.discard(token)).start()
    return jsonify({"token": token, "message": "Login successful"})

@app.route('/admin/api/accounts', methods=['GET'])
@require_auth
def admin_list_accounts():
    with accounts_lock:
        result = [{
            "id":            a["id"],
            "name":          a["name"],
            "api_id":        a["api_id"],
            "status":        a["status"],
            "bypass_count":  a["bypass_count"],
            "success_count": a["success_count"],
            "fail_count":    a["fail_count"],
            "last_used":     a["last_used"],
            "error_message": a["error_message"],
            "username":      a["username"],
            "first_name":    a["first_name"],
        } for a in accounts.values()]
    return jsonify(result)

@app.route('/admin/api/accounts', methods=['POST'])
@require_auth
def admin_add_account():
    d = request.get_json(silent=True) or {}
    name    = d.get('name', '').strip()
    api_id  = d.get('api_id')
    api_hash= d.get('api_hash', '').strip()
    sess    = d.get('session_string', '').strip()
    if not (name and api_id and api_hash and sess):
        return jsonify({"error": "Missing required fields"}), 400
    acc_id = _add_account_record(name, api_id, api_hash, sess)
    save_accounts_to_file()
    return jsonify({"id": acc_id, "message": "Account added, connecting..."})

@app.route('/admin/api/accounts/<acc_id>', methods=['DELETE'])
@require_auth
def admin_delete_account(acc_id):
    _stop_account(acc_id)
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
        del accounts[acc_id]
    save_accounts_to_file()
    return jsonify({"success": True})

@app.route('/admin/api/accounts/<acc_id>/pause', methods=['POST'])
@require_auth
def admin_pause_account(acc_id):
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
        accounts[acc_id]['status'] = 'paused'
    return jsonify({"success": True})

@app.route('/admin/api/accounts/<acc_id>/resume', methods=['POST'])
@require_auth
def admin_resume_account(acc_id):
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
        acc = accounts[acc_id]
        if acc['status'] == 'paused':
            if acc.get('client') and acc.get('loop'):
                acc['status'] = 'active'
            else:
                acc['status'] = 'connecting'
                threading.Thread(target=_thread_target, args=(acc_id,), daemon=True).start()
        elif acc['status'] == 'error':
            acc['status'] = 'connecting'
            acc['error_message'] = None
            threading.Thread(target=_thread_target, args=(acc_id,), daemon=True).start()
    return jsonify({"success": True})

@app.route('/admin/api/accounts/<acc_id>/session', methods=['PUT'])
@require_auth
def admin_update_session(acc_id):
    d = request.get_json(silent=True) or {}
    new_sess = d.get('session_string', '').strip()
    if not new_sess:
        return jsonify({"error": "session_string required"}), 400
    with accounts_lock:
        if acc_id not in accounts:
            return jsonify({"error": "Account not found"}), 404
    _stop_account(acc_id)
    with accounts_lock:
        accounts[acc_id].update({
            'session_string': new_sess,
            'status': 'connecting',
            'error_message': None,
            'client': None,
            'loop': None,
            'dzhq_bot_id': None,
            'nick_bot_id': None,
            'alex_bot_id': None,
            'pending': {},
        })
    _start_account_thread(acc_id)
    save_accounts_to_file()
    return jsonify({"success": True, "message": "Session updated, reconnecting..."})

@app.route('/admin/api/pause-all', methods=['POST'])
@require_auth
def admin_pause_all():
    with accounts_lock:
        for a in accounts.values():
            a['status'] = 'paused'
    return jsonify({"success": True})

@app.route('/admin/api/resume-all', methods=['POST'])
@require_auth
def admin_resume_all():
    with accounts_lock:
        for acc_id, a in accounts.items():
            if a['status'] == 'paused':
                if a.get('client') and a.get('loop'):
                    a['status'] = 'active'
                else:
                    a['status'] = 'connecting'
    with accounts_lock:
        to_restart = [acc_id for acc_id, a in accounts.items() if a['status'] == 'connecting']
    for acc_id in to_restart:
        threading.Thread(target=_thread_target, args=(acc_id,), daemon=True).start()
    return jsonify({"success": True})

# ── Bot toggle endpoints ───────────────────────────────────────────────────────
def _bots_state(extra=None):
    st = {
        "dzhq":        bot_settings["dzhq"],
        "nick":        bot_settings["nick"],
        "dzhq_first":  bot_settings.get("dzhq_first", False),
        "nick_first":  bot_settings.get("nick_first", False),
        "random_mode": bot_settings.get("random_mode", False),
        "alex_bot":    bot_settings.get("alex_bot", True),
        "random":      _random_status(),
    }
    if extra:
        st.update(extra)
    return st

@app.route('/admin/api/bots/status', methods=['GET'])
@require_auth
def admin_bots_status():
    return jsonify(_bots_state())

@app.route('/admin/api/bots/toggle', methods=['POST'])
@require_auth
def admin_bots_toggle():
    d = request.get_json(silent=True) or {}
    bot  = d.get("bot")
    enab = bool(d.get("enabled", True))
    if bot not in ("dzhq", "nick", "dzhq_first", "nick_first", "random_mode", "alex_bot"):
        return jsonify({"success": False, "error": "Invalid bot name"}), 400
    # Alex bot toggle — independent of the DZHQ/Nick race
    if bot == "alex_bot":
        bot_settings["alex_bot"] = enab
        return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))
    # Mode toggles — only one mode can be active at a time
    if bot in ("dzhq_first", "nick_first", "random_mode"):
        _set_exclusive_mode(bot if enab else None)
        if bot == "random_mode":
            with _random_lock:
                _random_state["bot"] = None
                _random_state["left"] = 0
                _random_state["batch"] = 0
        return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))
    # At least one of dzhq/nick must remain enabled
    other = "nick" if bot == "dzhq" else "dzhq"
    if not enab and not bot_settings[other]:
        return jsonify({"success": False,
                        "error": "Cannot disable both bots — keep at least one enabled"}), 400
    bot_settings[bot] = enab
    return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))


# ==================== PUBLIC HOME / STATUS ====================
def _public_status():
    with accounts_lock:
        accs = [{
            "name":       a["name"],
            "username":   ("@" + a["username"]) if a.get("username") else None,
            "first_name": a.get("first_name"),
            "status":     a["status"],
            "online":     a["status"] == "active",
            "bypasses":   a["bypass_count"],
            "success":    a["success_count"],
            "failed":     a["fail_count"],
            "last_used":  a["last_used"],
        } for a in accounts.values()]
    online = [a for a in accs if a["online"]]
    return {
        "status":         True,
        "developer":      DEVELOPER,
        "api_online":     bool(ALEX_BYPASS_API) or len(online) > 0,
        "total_accounts": len(accs),
        "online_accounts": len(online),
        "alex_api":       bool(ALEX_BYPASS_API),
        "bots":           {"dzhq": bot_settings.get("dzhq", True),
                           "nick": bot_settings.get("nick", True),
                           "dzhq_first": bot_settings.get("dzhq_first", False),
                           "nick_first": bot_settings.get("nick_first", False),
                           "random_mode": bot_settings.get("random_mode", False),
                           "alex_bot": bot_settings.get("alex_bot", True)},
        "accounts":       accs,
    }


@app.route('/status', methods=['GET'])
def public_status():
    return jsonify(_public_status())


HOME_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Exodus Bypass API — Status</title>
<style>
:root{--bg:#0b0f17;--card:#131a26;--line:#1f2a3a;--txt:#e6edf7;--mut:#8b9bb4;--grn:#22c55e;--red:#ef4444;--amb:#f59e0b;--cyn:#22d3ee}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px 16px}
.wrap{max-width:820px;margin:0 auto}h1{font-size:1.45rem;margin:0 0 4px}.mut{color:var(--mut);font-size:.85rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.big{font-size:1.6rem;font-weight:700}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.on{background:var(--grn);box-shadow:0 0 8px var(--grn)}.off{background:var(--red)}.pau{background:var(--amb)}
.acc{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.acc:last-child{border-bottom:0}.tag{font-size:.72rem;color:var(--mut);font-family:ui-monospace,monospace}
code{background:#0f1621;border:1px solid var(--line);padding:2px 6px;border-radius:6px;color:var(--cyn)}
</style></head><body><div class="wrap">
<h1>⚡ Exodus Bypass API</h1><div class="mut" id="sub">Loading status…</div>
<div class="grid">
 <div class="card"><div class="mut">API</div><div class="big" id="api">—</div></div>
 <div class="card"><div class="mut">Online accounts</div><div class="big" id="on">—</div></div>
 <div class="card"><div class="mut">Total accounts</div><div class="big" id="tot">—</div></div>
 <div class="card"><div class="mut">Alex resolver</div><div class="big" id="alex">—</div></div>
</div>
<div class="card" style="padding:0"><div id="list"></div></div>
<p class="mut" style="margin-top:18px">Usage: <code>/bypass?link=YOUR_LINK</code> · JSON: <code>/status</code> · Panel: <code>/admin</code></p>
</div><script>
const S=s=>document.getElementById(s);
async function load(){
 try{const d=await (await fetch('/status')).json();
  S('sub').textContent='Developer '+d.developer+' · updated '+new Date().toLocaleTimeString();
  S('api').innerHTML='<span class="dot '+(d.api_online?'on':'off')+'"></span>'+(d.api_online?'Online':'Offline');
   S('on').textContent=d.online_accounts;S('tot').textContent=d.total_accounts;
   S('alex').textContent=d.alex_api?'Ready':'Off';
  S('list').innerHTML=d.accounts.length?d.accounts.map(a=>{
   const c=a.status==='active'?'on':(a.status==='paused'||a.status==='connecting'?'pau':'off');
   return '<div class="acc"><div><b><span class="dot '+c+'"></span>'+a.name+'</b>'+
    '<div class="tag">'+[a.first_name,a.username].filter(Boolean).join(' · ')+'</div></div>'+
    '<div class="tag">'+a.status.toUpperCase()+' · ✅'+a.success+' ❌'+a.failed+'</div></div>';
  }).join(''):'<div class="acc"><span class="mut">No accounts connected yet.</span></div>';
 }catch(e){S('sub').textContent='Status unavailable';}
}
load();setInterval(load,5000);
</script></body></html>"""


@app.route('/', methods=['GET'])
def home():
    return render_template_string(HOME_HTML)


# ==================== BYPASS ROUTE ====================

def _single_bypass_payload(original, bypassed, source, response_ms, account, module):
    """Return the shared response shape for exactly one bypassed URL."""
    return {
        "status":      True,
        "developer":   DEVELOPER,
        "response_ms": response_ms,
        "source":      source,
        "module":      module,
        "account":     account,
        "url":         bypassed,
        "links":       {
            "original": original,
            "bypassed": bypassed,
        },
    }


def _run_async_bypass(job_id, link):
    """Run the existing resolver in its own request context and save its reply."""
    try:
        # The normal resolver remains the single source of truth.  The header
        # lets it publish the same flow updates while running in the worker.
        with app.test_request_context(
            "/bypass",
            method="GET",
            headers={"X-Bypass-Job-Id": job_id},
        ):
            response = bypass(link_override=link)
            response_obj = app.make_response(response)
            payload = response_obj.get_json(silent=True)
            code = response_obj.status_code

        if isinstance(payload, dict):
            final_status = "success" if payload.get("status") is True else "failed"
        else:
            final_status = "failed"
            payload = {"status": False, "message": "Resolver returned an invalid response"}
        _flow_update(
            job_id,
            final_status,
            payload.get("message") or (
                "Bypass successful" if final_status == "success"
                else "Bypass failed"
            ),
            result=payload,
            http_status=code,
        )
    except Exception as exc:
        logging.exception("Async bypass job failed")
        _flow_update(job_id, "failed", f"Bypass flow crashed: {exc}")


@app.route('/bypass/async', methods=['GET', 'POST'])
def bypass_async():
    """Start a non-blocking bypass and return a job id for status polling."""
    if request.method == 'GET':
        link = (request.args.get('link') or request.args.get('url') or '').strip()
    else:
        data = request.get_json(silent=True) or {}
        link = (data.get('link') or data.get('url') or '').strip()
    if not link:
        return jsonify({
            "status": False,
            "developer": DEVELOPER,
            "message": "Missing 'link' parameter",
        }), 400
    if not link.startswith(("http://", "https://")):
        link = "https://" + link

    job_id = secrets.token_urlsafe(12)
    with _jobs_lock:
        if len(_jobs) >= _MAX_JOBS:
            # Remove oldest finished jobs first; never interrupt active work.
            finished = [
                (key, value.get("updated_at", 0))
                for key, value in _jobs.items()
                if value.get("status") in ("success", "failed")
            ]
            for key, _ in sorted(finished, key=lambda item: item[1])[:100]:
                _jobs.pop(key, None)
        _jobs[job_id] = {
            "status": "queued",
            "message": "Bypass queued",
            "link": link,
            "created_at": time.time(),
            "updated_at": time.time(),
            "flow": [{
                "status": "queued",
                "message": "Bypass queued",
                "at": time.time(),
            }],
        }

    threading.Thread(
        target=_run_async_bypass,
        args=(job_id, link),
        daemon=True,
        name=f"bypass-{job_id}",
    ).start()
    return jsonify({
        "status": "processing",
        "developer": DEVELOPER,
        "job_id": job_id,
        "message": "Bypass started; poll /bypass/status/<job_id> for flow updates",
        "status_url": f"/bypass/status/{job_id}",
    }), 202


@app.route('/bypass/status/<job_id>', methods=['GET'])
def bypass_status(job_id):
    """Return all status messages received so far for an async request."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({
                "status": False,
                "developer": DEVELOPER,
                "message": "Unknown or expired job id",
            }), 404
        snapshot = dict(job)
        snapshot["flow"] = list(job["flow"])
    snapshot["developer"] = DEVELOPER
    return jsonify(snapshot)


@app.route('/bypass', methods=['GET', 'POST'])
def bypass(link_override=None):
    if request.method == 'GET':
        link = (request.args.get('link') or request.args.get('url') or '').strip()
    else:
        d    = request.get_json(silent=True) or {}
        link = (d.get('link') or d.get('url') or '').strip()
    if link_override is not None:
        link = link_override.strip()

    if not link:
        return jsonify({"status": False, "developer": DEVELOPER, "message": "Missing 'link' parameter"}), 400
    if not link.startswith(('http://', 'https://')):
        link = 'https://' + link
    job_id = request.headers.get("X-Bypass-Job-Id")
    _flow_update(job_id, "processing", "Link received; starting bypass flow", link=link)
    _trace("REQUEST", f"start {link}")

    # ── Alex API only for URLKing / MonteOlympus links ───────────────
    # If Alex is offline or returns an error, continue with the original
    # Telegram race instead of failing the request.
    t0 = time.time()
    if _should_use_alex(link):
        _flow_update(job_id, "processing", "Checking Alex resolver")
        alex_winner, alex_error = alex_race(link)
        if alex_winner:
            result_url = alex_winner["url"]
            extra = alex_winner.get("extra", {})
            _flow_update(job_id, "success", "Bypass successful", source=alex_winner["source"], url=result_url)
            return jsonify(_single_bypass_payload(
                original=link,
                bypassed=result_url,
                source=alex_winner["source"],
                response_ms=f"{int((time.time() - t0) * 1000)}ms",
                account=extra.get("account", "alex_api"),
                module=extra.get("module", "alex_bot"),
            ))
        if alex_error:
            _flow_update(job_id, "processing", f"Alex resolver did not complete: {alex_error}; trying Telegram")
            print(f"[Alex] {alex_error} — trying Telegram fallback")

    # ── Account selection ─────────────────────────────────────────────
    acc_id, acc_state = get_next_active()
    if not acc_state:
        _flow_update(job_id, "failed", "No active Telegram account is available")
        return jsonify({
            "status":    False,
            "developer": DEVELOPER,
            "message":   "No active Telegram accounts. Add via /admin.",
        }), 503

    req_id = secrets.token_hex(8)
    loop   = acc_state.get("loop")

    # ── Respect bot toggles ───────────────────────────────────────────
    use_dzhq    = bot_settings.get("dzhq", True)
    use_nick    = bot_settings.get("nick", True)

    # ── Mode resolution: primary bot runs first, other one is fallback ──
    primary = None
    if use_dzhq and use_nick:
        if bot_settings.get("random_mode", False):
            primary = _random_pick_bot()
        elif bot_settings.get("dzhq_first", False):
            primary = "dzhq"
        elif bot_settings.get("nick_first", False):
            primary = "nick"
    seq_mode   = primary is not None
    dzhq_first = primary == "dzhq"
    nick_first = primary == "nick"

    if not use_dzhq and not use_nick:
        _flow_update(job_id, "failed", "All bypass bots are disabled")
        return jsonify({
            "status":    False,
            "developer": DEVELOPER,
            "message":   "All bots are disabled — enable at least one in /admin",
        }), 503

    req_entry = {
        "link":         link,
        "ts":           t0,
        "done":         False,
        # DZHQ group
        "dzhq_event":   threading.Event(),
        "dzhq_result":  None,
        "dzhq_sent_id": None,
        "last_dzhq_ts": None,
        # Nick DM
        "nick_event":   threading.Event(),
        "nick_result":  None,
        "last_nick_ts": None,
    }
    with accounts_lock:
        accounts[acc_id]["pending"][req_id] = req_entry
        # Register in Nick DM queue (FIFO) only if Nick is enabled
        # In DZHQ First mode, Nick queue registration is deferred until DZHQ fails
        if use_nick and primary != "dzhq":
            nick_lock = accounts[acc_id].get("nick_dm_lock")
            if nick_lock:
                with nick_lock:
                    accounts[acc_id]["nick_dm_queue"].append(req_id)

    TIMEOUT      = BYPASS_IDLE_TIMEOUT_SEC
    DZHQ_TIMEOUT = 20.0   # How long to wait for DZHQ before falling back to Nick

    # ── Helper: send to Nick (used in both modes) ─────────────────────
    async def _send_nick(tg):
        try:
            await tg.send_message(NICK_BOT, link)
            req_entry["last_nick_ts"] = time.time()
            _trace("NICK", f"sent {link}")
        except Exception as e:
            req_entry['nick_fail'] = f"Nick DM send error: {e}"
            _trace("NICK", f"send failed for {link}: {e}")
            with accounts_lock:
                nlk = accounts[acc_id].get("nick_dm_lock")
                if nlk:
                    with nlk:
                        q = accounts[acc_id].get("nick_dm_queue", [])
                        if req_id in q:
                            q.remove(req_id)

    # ── Send to DZHQ ──────────────────────────────────────────────────
    async def _send_dzhq(tg):
        try:
            s1 = await tg.send_message(DZHQ_GROUP, f"/b {link}")
            req_entry['dzhq_sent_id'] = s1.id
            req_entry["last_dzhq_ts"] = time.time()
            _trace("DZHQ", f"sent /b for {link}, message_id={s1.id}")
        except Exception as e:
            req_entry['dzhq_fail'] = f"DZHQ send error: {e}"
            _trace("DZHQ", f"send failed for {link}: {e}")

    # ── MODE A: Sequential (DZHQ First / Nick First / Random) ─────────
    if seq_mode:
        _flow_update(job_id, "processing", f"Sent link to {primary.upper()}; waiting for its reply")
        async def _send_primary():
            if primary == "dzhq":
                await _send_dzhq(acc_state["client"])
            else:
                await _send_nick(acc_state["client"])
        try:
            asyncio.run_coroutine_threadsafe(_send_primary(), loop).result(timeout=12)
        except Exception as e:
            with accounts_lock:
                accounts[acc_id]["pending"].pop(req_id, None)
            return jsonify({
                "status":    False,
                "developer": DEVELOPER,
                "message":   f"Failed to send to {primary}: {e}",
            }), 500

    # ── MODE B: Parallel — send to both simultaneously ────────────────
    else:
        _flow_update(job_id, "processing", "Sent link to enabled bypass bots; waiting for the first reply")
        async def _send_all():
            tg = acc_state["client"]
            sends = []
            if use_dzhq:
                sends.append(_send_dzhq(tg))
            if use_nick:
                sends.append(_send_nick(tg))
            if sends:
                await asyncio.gather(*sends)
        try:
            asyncio.run_coroutine_threadsafe(_send_all(), loop).result(timeout=12)
        except Exception as e:
            with accounts_lock:
                accounts[acc_id]["pending"].pop(req_id, None)
            return jsonify({
                "status":    False,
                "developer": DEVELOPER,
                "message":   f"Failed to send: {e}",
            }), 500

    # ── Race / watcher setup ──────────────────────────────────────────
    active_bots = sum([use_dzhq, use_nick])

    race_event  = threading.Event()
    race_winner = {}
    race_lock   = threading.Lock()
    done_count  = [0]
    done_lock   = threading.Lock()
    # DZHQ First uses one watcher for both sequential stages. Counting both
    # bots here would leave the request waiting forever after both fail.
    watcher_goal = 1 if seq_mode else active_bots

    def _declare_winner(source, data):
        with race_lock:
            if race_event.is_set():
                return False
            race_winner['source'] = source
            race_winner['data']   = data
            req_entry['race_done'] = True   # signal Nick handler: race already won
            race_event.set()
            _trace(source.upper(), f"winner for {link}: {data}")
            return True

    def _watcher_done():
        with done_lock:
            done_count[0] += 1
            if done_count[0] >= watcher_goal:
                race_event.set()   # all active bots timed out / failed

    request_budget = (
        TIMEOUT * (2 if seq_mode else 1)
        if TIMEOUT > 0
        else MAX_BYPASS_TIMEOUT_SEC
    )
    request_budget = min(MAX_BYPASS_TIMEOUT_SEC, max(1.0, request_budget))
    request_deadline = time.monotonic() + request_budget

    def _remaining(last_seen):
        hard_remaining = request_deadline - time.monotonic()
        if TIMEOUT <= 0:
            return hard_remaining
        idle_remaining = last_seen + TIMEOUT - time.time()
        return min(idle_remaining, hard_remaining)

    def _watch_dzhq():
        ev = req_entry['dzhq_event']
        while not race_event.is_set():
            last_seen = req_entry.get("last_dzhq_ts") or t0
            remaining = _remaining(last_seen)
            if remaining is not None and remaining <= 0: break
            if ev.wait(timeout=0.3 if remaining is None else min(0.3, remaining)):
                res = req_entry.get('dzhq_result')
                if res and res[0].get('status') == 'ok':
                    _declare_winner("dzhq", res)
                break

        # ── DZHQ First fallback: DZHQ failed/timed out → now send Nick ─
        if dzhq_first and not race_event.is_set():
            print(f"[Bypass] DZHQ First: DZHQ failed/timeout — sending to Nick now")
            # Register Nick in queue
            with accounts_lock:
                nlk = accounts[acc_id].get("nick_dm_lock")
                if nlk:
                    with nlk:
                        accounts[acc_id]["nick_dm_queue"].append(req_id)
            # Send to Nick
            async def _fallback_nick():
                await _send_nick(acc_state["client"])
            try:
                asyncio.run_coroutine_threadsafe(_fallback_nick(), loop).result(timeout=10)
            except Exception as e:
                print(f"[Bypass] DZHQ First fallback Nick send error: {e}")
            # Now watch Nick with remaining time
            nick_ev   = req_entry['nick_event']
            while not race_event.is_set():
                last_seen = req_entry.get("last_nick_ts") or time.time()
                rem = _remaining(last_seen)
                if rem is not None and rem <= 0: break
                if nick_ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                    r = req_entry.get('nick_result')
                    if r and r.get('status') == 'ok':
                        _declare_winner("nick", r)
                    break

        _watcher_done()

    def _watch_nick():
        ev = req_entry['nick_event']
        while not race_event.is_set():
            last_seen = req_entry.get("last_nick_ts") or t0
            remaining = _remaining(last_seen)
            if remaining is not None and remaining <= 0: break
            if ev.wait(timeout=0.3 if remaining is None else min(0.3, remaining)):
                r = req_entry.get('nick_result')
                if r and r.get('status') == 'ok':
                    _declare_winner("nick", r)
                break

        # ── Nick First fallback: Nick failed/timed out → now send DZHQ ─
        if nick_first and not race_event.is_set():
            print("[Bypass] Nick First: Nick failed/timeout — sending to DZHQ now")
            async def _fallback_dzhq():
                await _send_dzhq(acc_state["client"])
            try:
                asyncio.run_coroutine_threadsafe(_fallback_dzhq(), loop).result(timeout=10)
            except Exception as e:
                print(f"[Bypass] Nick First fallback DZHQ send error: {e}")
            dz_ev = req_entry['dzhq_event']
            while not race_event.is_set():
                last_seen = req_entry.get("last_dzhq_ts") or time.time()
                rem = _remaining(last_seen)
                if rem is not None and rem <= 0: break
                if dz_ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                    res = req_entry.get('dzhq_result')
                    if res and res[0].get('status') == 'ok':
                        _declare_winner("dzhq", res)
                    break

        _watcher_done()

    threads = []
    # In sequential modes only the primary watcher runs; it handles the
    # fallback bot itself. In parallel mode both watchers run together.
    if (use_dzhq and not seq_mode) or dzhq_first:
        t1 = threading.Thread(target=_watch_dzhq, daemon=True)
        t1.start(); threads.append(t1)
    if (use_nick and not seq_mode) or nick_first:
        t3 = threading.Thread(target=_watch_nick, daemon=True)
        t3.start(); threads.append(t3)
    # Even if a watcher is unexpectedly lost, never hold the HTTP request open
    # forever.
    race_event.wait(timeout=max(0.0, request_deadline - time.monotonic()))

    # Cleanup
    async def _cleanup():
        tg = acc_state["client"]
        for gid, key in ((DZHQ_GROUP, 'dzhq_sent_id'),):
            mid = req_entry.get(key)
            if mid:
                try:
                    await tg.delete_messages(gid, [mid])
                except:
                    pass
    try:
        asyncio.run_coroutine_threadsafe(_cleanup(), loop)
    except:
        pass

    # Remove from nick queue if still there
    with accounts_lock:
        accounts[acc_id]["pending"].pop(req_id, None)
        nlk = accounts[acc_id].get("nick_dm_lock")
        if nlk:
            with nlk:
                q = accounts[acc_id].get("nick_dm_queue", [])
                if req_id in q:
                    q.remove(req_id)

    ms  = int((time.time() - t0) * 1000)
    won = race_winner.get('source') is not None

    with accounts_lock:
        if acc_id in accounts:
            accounts[acc_id]['bypass_count'] += 1
            accounts[acc_id]['last_used'] = datetime.now(timezone.utc).isoformat()
            if won:
                accounts[acc_id]['success_count'] += 1
            else:
                accounts[acc_id]['fail_count'] += 1

    if not won:
        _flow_update(job_id, "failed", "All bypass bots failed or timed out", response_ms=f"{ms}ms")
        _trace("RESULT", f"failed for {link}: dzhq={req_entry.get('dzhq_fail', 'no response')} nick={req_entry.get('nick_fail', 'no response')}")
        return jsonify({
            "status":      False,
            "developer":   DEVELOPER,
            "message":     "All bots failed to bypass",
            "dzhq":        req_entry.get('dzhq_fail', 'no response'),
            "nick":        req_entry.get('nick_fail',  'no response'),
            "response_ms": f"{ms}ms",
        }), 422

    source = race_winner['source']
    data   = race_winner['data']
    acct   = acc_state.get("name", "unknown")
    _flow_update(job_id, "success", "Bypass successful", source=source)
    _trace("RESULT", f"success for {link}: source={source} data={data}")

    # Build unified response
    if source == "nick":
        if isinstance(data.get("bypassed"), str) and data.get("bypassed"):
            return jsonify(_single_bypass_payload(
                original=data.get("original") or link,
                bypassed=data["bypassed"],
                source="nick",
                response_ms=f"{ms}ms",
                account=acct,
                module="nick_bot",
            ))

        links = {}
        if data.get('original'): links["original"] = data['original']
        if data.get('bypassed'): links["bypassed"]  = data['bypassed']
        return jsonify({
            "status":      True,
            "developer":   DEVELOPER,
            "response_ms": f"{ms}ms",
            "source":      source,
            "account":     acct,
            "links":       links,
        })

    # source == "dzhq"
    r     = data[0]
    if (
        len(data) == 1
        and isinstance(r.get("bypassed"), str)
        and r.get("bypassed")
        and not any(r.get(key) for key in ("instant_dl", "telegram", "direct"))
    ):
        return jsonify(_single_bypass_payload(
            original=r.get("original") or link,
            bypassed=r["bypassed"],
            source="dzhq",
            response_ms=f"{ms}ms",
            account=acct,
            module="dzhq_bot",
        ))

    links = {}
    if r.get('original'):    links["original"]   = r['original']
    if r.get('bypassed'):    links["bypassed"]   = r['bypassed']
    if r.get('instant_dl'):  links["instant_dl"] = r['instant_dl']
    if r.get('telegram'):    links["telegram"]   = r['telegram']
    if r.get('direct'):      links["direct"]     = r['direct']

    resp = {
        "status":      True,
        "developer":   DEVELOPER,
        "response_ms": f"{ms}ms",
        "source":      "dzhq",
        "account":     acct,
    }
    if r.get('file_name') or r.get('file_size'):
        resp["file"] = {}
        if r.get('file_name'): resp["file"]["name"] = r['file_name']
        if r.get('file_size'): resp["file"]["size"] = r['file_size']
    resp["links"] = links
    if len(data) > 1:
        resp["batch"] = data
    return jsonify(resp)



# ==================== FLASK SERVER ====================
def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)


# ==================== STARTUP ====================
if __name__ == '__main__':
    try:
        load_accounts_from_file()
    except Exception as e:
        print(f"⚠️ Could not load saved accounts: {e}")

    # Do not auto-load a default Telegram session. The API and Admin Panel
    # must remain available even when there is no connected account; accounts
    # can be added or updated securely from /admin.
    if accounts:
        print(f"ℹ️ Saved accounts loaded: {len(accounts)}")
    else:
        print("ℹ️ No Telegram accounts configured. API/Admin Panel remain available; add one via /admin.")

    threading.Thread(target=run_flask, daemon=True).start()
    botsys.start_bot(PORT)   # Telegram front-end (needs BOT_TOKEN in .env)

    print(f"""
{'='*58}
  ✅ Exodus BYPASS API STARTED
  ⚡ Bypass:   http://localhost:{PORT}/bypass?link=URL
  📊 Admin:    http://localhost:{PORT}/admin
  🔑 Password: {ADMIN_PASSWORD}
  🤖 Bots:     DZHQ Group + Nick DM (2-way race)
  🅰️ Alex Bot: {'✅ ON — races Alex API' if bot_settings.get('alex_bot') else '❌ OFF'} ({ALEX_BOT})
  🔗 Alex API:  {'✅ Primary resolver enabled' if ALEX_BYPASS_API else '⚠️  Not configured'}
  💾 Accounts: {len(accounts)} total
{'='*58}
""")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
