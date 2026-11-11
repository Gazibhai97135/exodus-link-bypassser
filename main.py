"""
EXODUS DZHQ BYPASS API — exodus-PRIMARY RESOLVER + ADMIN PANEL
Developer: @Exodus_OWN3R
Version: 3.1  (+ Telegram bot, force-join, referral API keys, DB export/import)

Resolver: @exodusbypassbot via your logged-in Telegram account is the PRIMARY
resolver for ALL links. exodus HTTP API races in parallel. DZHQ is a silent
fallback only. Nick is OFF (human verification puzzle = unreliable).
"""

# ══════════════════════════════════════════════════════════════
#  SETUP — loads .env automatically
# ══════════════════════════════════════════════════════════════
import sys, os, subprocess

def _load_dotenv(path=".env"):
    if not os.path.exists(path):
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
            if k and k not in os.environ:
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

# Group 1 — DZHQ (silent fallback only)
DZHQ_BOT        = "@DZHQ_BypassBot"
DZHQ_GROUP      = -1003644908415

# Group 2 — Nick (DISABLED — human verification puzzle)
NICK_BOT        = "@Nick_Bypass_Bot"

# Group 3 — exodus (PRIMARY resolver)
exodus_BOT        = "@exodusbypassbot"

DEVELOPER       = "@Exodus_OWN3R"
PORT            = int(os.environ.get('PORT', 5000))
SECRET_KEY      = (
    os.environ.get("SECRET_KEY")
    or os.environ.get("SESSION_SECRET")
    or secrets.token_hex(32)
)
ACCOUNTS_FILE   = "accounts_data.json"

# exodus is now the primary resolver for EVERY link.
exodus_BYPASS_API = os.environ.get(
    "exodus_BYPASS_API",
    "https://exodusbypassapi.up.railway.app/bypass?url=",
).strip()
exodus_TIMEOUT_SEC = float(os.environ.get("exodus_TIMEOUT_SEC", "20"))
exodus_ALLOWED_HOST_PARTS = ()   # empty = match all
exodus_BOT_TIMEOUT_SEC = float(os.environ.get("exodus_BOT_TIMEOUT_SEC", "75"))
BYPASS_IDLE_TIMEOUT_SEC = float(os.environ.get("BYPASS_IDLE_TIMEOUT_SEC", "30"))
exodus_RACE_MAX_TIMEOUT_SEC = max(
    30.0, float(os.environ.get("exodus_RACE_MAX_TIMEOUT_SEC", "90"))
)
MAX_BYPASS_TIMEOUT_SEC = max(
    30.0, float(os.environ.get("MAX_BYPASS_TIMEOUT_SEC", "120"))
)
TRACE_BOTS = os.environ.get("TRACE_BOTS", "0").lower() in ("1", "true", "yes", "on")

def _trace(label, message):
    if TRACE_BOTS:
        print(f"[TRACE][{label}] {message}", flush=True)

# ── Bot toggle settings ────────────────────────────────────────────────────────
# Nick is default OFF — human puzzle makes it unreliable. exodus is primary.
bot_settings        = {"dzhq": True, "nick": False, "dzhq_first": False,
                       "nick_first": False, "random_mode": False, "exodus_bot": True}

RANDOM_BATCH_MIN = 3
RANDOM_BATCH_MAX = 10
_random_state = {"bot": None, "left": 0, "batch": 0}
_random_lock  = threading.Lock()

def _random_pick_bot():
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
    for m in ("dzhq_first", "nick_first", "random_mode"):
        bot_settings[m] = (m == mode)

_jobs = {}
_jobs_lock = threading.Lock()
_MAX_JOBS = 1000


def _flow_update(job_id, status, message, **extra):
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
        if len(job["flow"]) > 50:
            del job["flow"][:-50]

logging.basicConfig(level=logging.WARNING)
logging.getLogger('telethon').setLevel(logging.WARNING)


# ==================== exodus API ====================
def exodus_bypass(link):
    if not exodus_BYPASS_API:
        return None, "exodus API is not configured"

    encoded_link = urllib.parse.quote(link, safe="")
    if exodus_BYPASS_API.endswith("url="):
        target = f"{exodus_BYPASS_API}{encoded_link}"
    else:
        separator = "&" if "?" in exodus_BYPASS_API else "?"
        target = f"{exodus_BYPASS_API}{separator}url={encoded_link}"
    req = urllib.request.Request(
        target,
        headers={
            "Accept": "application/json",
            "User-Agent": "Exodus_bypass-API/3.1",
        },
        method="GET",
    )

    try:
        api_timeout = (
            exodus_TIMEOUT_SEC
            if exodus_TIMEOUT_SEC > 0
            else exodus_RACE_MAX_TIMEOUT_SEC
        )
        with urllib.request.urlopen(req, timeout=api_timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
            _trace("exodus_API", f"response for {link}: {str(payload)[:500]}")
    except urllib.error.HTTPError as exc:
        return None, f"exodusAPI HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"exodusAPI unavailable: {exc.reason}"
    except TimeoutError:
        return None, "exodusAPI request timed out"
    except (ValueError, json.JSONDecodeError):
        return None, "exodusAPI returned invalid JSON"
    except Exception as exc:
        return None, f"exodusAPI error: {exc}"

    if not isinstance(payload, dict):
        return None, "exodusAPI returned an unexpected response"
    if str(payload.get("status", "")).lower() not in ("success", "ok", "true", "1"):
        return None, str(payload.get("message") or payload.get("error") or "exodusAPI could not bypass this URL")

    result_url = payload.get("url") or payload.get("bypassed") or payload.get("result")
    if isinstance(result_url, list):
        result_url = result_url[0] if result_url else None
    if not isinstance(result_url, str) or not result_url.startswith(("http://", "https://")):
        return None, "exodus API response did not contain a valid result URL"

    return {
        "status": "ok",
        "original": link,
        "bypassed": result_url,
        "raw_type": payload.get("module", "exodus"),
        "exodus_response": payload,
    }, None


def _should_use_exodus(link):
    """exodus is now the primary resolver for every link."""
    return True


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
        "exodus_bot_id":    None,
        "pending":        {},
        "nick_dm_queue":  [],
        "nick_dm_lock":   threading.Lock(),
        "exodus_dm_queue":  [],
        "exodus_dm_lock":   threading.Lock(),
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


# ==================== NICK BOT PARSER (DISABLED) ====================
def parse_nick_message(text, entities, sent_link):
    """Nick is disabled. This function is kept for compatibility only."""
    return None


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
    """Nick handler — kept but effectively unused (Nick disabled)."""
    async def on_dm(event):
        return
    return on_dm


# ==================== exodus BOT PARSER ====================
_SMALLCAPS_MAP = {
    'ᴀ':'a','ʙ':'b','ᴄ':'c','ᴅ':'d','ᴇ':'e','ꜰ':'f','ɢ':'g','ʜ':'h','ɪ':'i','ᴊ':'j',
    'ᴋ':'k','ʟ':'l','ᴍ':'m','ɴ':'n','ᴏ':'o','ᴩ':'p','ᴘ':'p','q':'q','ǫ':'q','ʀ':'r',
    'ꜱ':'s','ѕ':'s','ᴛ':'t','ᴜ':'u','ᴠ':'v','ᴡ':'w','x':'x','ʏ':'y','ᴢ':'z','ɀ':'z',
}
def _fold(s):
    return ''.join(_SMALLCAPS_MAP.get(ch, ch) for ch in (s or ''))

_RX_exodus_BYPASSED = re.compile(r'bypass(?:ed)?\s*link[^:\n]*:?-?\s*\**\s*\n?\s*(https?://\S+)', re.I)
_RX_exodus_ORIGINAL = re.compile(r'original\s*link[^:\n]*:?-?\s*\**\s*\n?\s*(https?://\S+)', re.I)
_RX_exodus_PROGRESS = re.compile(
    r'initialis|initializ|fetching|scanning|bypassing\s*security|cracking|decoding|solving|'
    r'processing|please\s*wait|\d{1,3}\s*%|[▰▱]', re.I)
_RX_exodus_FAIL = re.compile(
    r'bypass\s*(?:failed|error)|invalid\s*link|not\s*support(?:ed)?|unsupported|'
    r'no\s*script|not\s*found|unable|unable\s+to|cannot|can[\'’]t|'
    r'could\s*not|couldn[\'’]t|not\s*possible|does\s*not\s*support|'
    r'doesn[\'’]t\s*support|failed|error', re.I)
_exodus_SKIP_PARTS  = ("t.me/+", "t.me/exodusmodz", "update", "join", "channel", "apkamitrr")


def parse_exodus_message(text, entities, sent_link):
    if not text:
        return None
    stripped = text.strip()
    folded   = _fold(stripped)

    if (
        _RX_exodus_PROGRESS.search(folded)
        and not _RX_exodus_BYPASSED.search(folded)
        and not _RX_exodus_FAIL.search(folded)
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
    if _RX_exodus_FAIL.search(folded) and not _RX_exodus_BYPASSED.search(folded):
        return {
            "status": "failed",
            "original": sent_link,
            "error": stripped[:300],
            "raw": stripped[:300],
        }

    m = _RX_exodus_BYPASSED.search(folded)
    if m:
        u = _clean(m.group(1))
        mo = _RX_exodus_ORIGINAL.search(folded)
        orig = _clean(mo.group(1)) if mo else sent_link
        same_as_input = (sent_link and (sent_link in u or u in sent_link)) or (orig and u == orig)
        if u.startswith(("http://", "https://")) and not same_as_input \
           and not any(k in u.lower() for k in _exodus_SKIP_PARTS):
            return {"status": "ok", "original": orig or sent_link, "bypassed": u, "raw": stripped[:300]}

    # Fallback: plain URL regex
    for u in re.findall(r'https?://[^\s\n\)\]>"\']+', text):
        u = _clean(u)
        if not u.startswith(("http://", "https://")):
            continue
        if sent_link and (sent_link in u or u in sent_link):
            continue
        if any(k in u.lower() for k in _exodus_SKIP_PARTS):
            continue
        return {"status": "ok", "original": sent_link, "bypassed": u, "raw": stripped[:300]}
    return None


def _same_link(left, right):
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


def make_exodus_dm_handler(acc_id):
    async def on_dm(event):
        with accounts_lock:
            state = accounts.get(acc_id)
        if not state:
            return

        exodus_bot_id = state.get("exodus_bot_id")
        if exodus_bot_id:
            if event.sender_id != exodus_bot_id:
                return
        else:
            try:
                sndr = await event.get_sender()
                u = (getattr(sndr, 'username', '') or '').lower()
                if 'exodus' not in u:
                    return
            except Exception:
                return

        msg  = event.message
        text = (msg.text or msg.caption or '').strip()
        if not text:
            return

        exodus_lock = state.get("exodus_dm_lock")
        if not exodus_lock:
            return

        with exodus_lock:
            queue = state.get("exodus_dm_queue", [])
            if not queue:
                return
            req_id = queue[0]

        with accounts_lock:
            req = state.get("pending", {}).get(req_id)
        if not req or req.get('done'):
            with exodus_lock:
                if queue and queue[0] == req_id:
                    queue.pop(0)
            return

        incoming_id = getattr(msg, "id", None)
        sent_id = req.get("exodus_sent_id")
        if sent_id and incoming_id and incoming_id <= sent_id:
            _trace(
                "exodus_BOT",
                f"ignored stale message id={incoming_id} for {req['link']} "
                f"(request message id={sent_id})",
            )
            return

        r = parse_exodus_message(text, msg.entities or [], req['link'])
        if r and r.get("original") and not _same_link(r["original"], req["link"]):
            _trace(
                "exodus_BOT",
                f"ignored mismatched result for {req['link']}: "
                f"message original={r['original']}",
            )
            return
        _trace("exodus_BOT", f"received for {req['link']}: text={text[:500]!r} parsed={r}")
        req["last_exodus_ts"] = time.time()
        if r is None:
            folded_text = _fold(text)
            is_known_failure = _RX_ERR.search(folded_text) and not _RX_exodus_BYPASSED.search(folded_text)
            if (TRACE_BOTS or is_known_failure) \
                    and not _RX_exodus_PROGRESS.search(folded_text) \
                    and not _RX_RATE.search(folded_text):
                with exodus_lock:
                    if queue and queue[0] == req_id:
                        queue.pop(0)
                req['exodus_bot_result'] = {
                    "status": "failed",
                    "error": "exodusparser found no bypassed URL",
                    "raw": text[:500],
                }
                req['exodus_bot_event'].set()
            return

        with exodus_lock:
            if queue and queue[0] == req_id:
                queue.pop(0)

        req['exodus_bot_result'] = r
        req['exodus_bot_event'].set()
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

        try:
            ent = await tg.get_entity(DZHQ_BOT)
            with accounts_lock:
                accounts[acc_id]['dzhq_bot_id'] = ent.id
        except:
            pass

        try:
            ent3 = await tg.get_entity(NICK_BOT)
            with accounts_lock:
                accounts[acc_id]['nick_bot_id'] = ent3.id
            nick_id = ent3.id
        except:
            nick_id = None

        try:
            ent4 = await tg.get_entity(exodus_BOT)
            with accounts_lock:
                accounts[acc_id]['exodus_bot_id'] = ent4.id
            exodus_id = ent4.id
        except Exception:
            exodus_id = None
            print(f"[Acc {acc_id}] ⚠️ Could not resolve {exodus_BOT}")

        dzhq_h = make_dzhq_handler(acc_id)
        tg.add_event_handler(dzhq_h, events.NewMessage(chats=DZHQ_GROUP))
        tg.add_event_handler(dzhq_h, events.MessageEdited(chats=DZHQ_GROUP))

        exodus_h = make_exodus_dm_handler(acc_id)
        if exodus_id:
            tg.add_event_handler(exodus_h, events.NewMessage(from_users=exodus_id, incoming=True, func=lambda e: e.is_private))
            tg.add_event_handler(exodus_h, events.MessageEdited(from_users=exodus_id, incoming=True, func=lambda e: e.is_private))
        else:
            tg.add_event_handler(exodus_h, events.NewMessage(incoming=True, func=lambda e: e.is_private))
            tg.add_event_handler(exodus_h, events.MessageEdited(incoming=True, func=lambda e: e.is_private))

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


# ==================== exodus API + exodus BOT RACE ====================
def exodus_race(link):
    """
    Race the exodus HTTP API against @exodusbypassbot (Telegram DM).
    Whichever answers first wins.
    """
    use_bot = bot_settings.get("exodus_bot", True)

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

    def _api_racer():
        res, err = exodus_bypass(link)
        if res:
            _declare(
                "exodus",
                res["bypassed"],
                {
                    "module": res.get("raw_type", "exodus_api"),
                    "account": "exodus_api",
                },
            )
        else:
            _record_error("api", err)

    threads = [threading.Thread(target=_api_racer, daemon=True)]

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
                "exodus_sent_id":    None,
                "last_exodus_ts":    None,
                "exodus_bot_event":  threading.Event(),
                "exodus_bot_result": None,
            }
            with accounts_lock:
                accounts[acc_id]["pending"][req_id] = req_entry
                alk = accounts[acc_id].get("exodus_dm_lock")
                if alk:
                    with alk:
                        accounts[acc_id]["exodus_dm_queue"].append(req_id)

            loop = acc_state.get("loop")

            async def _send_exodus():
                sent = await acc_state["client"].send_message(exodus_BOT, link)
                req_entry["exodus_sent_id"] = getattr(sent, "id", None)
                req_entry["last_exodus_ts"] = time.time()

            def _bot_racer():
                try:
                    asyncio.run_coroutine_threadsafe(_send_exodus(), loop).result(timeout=12)
                except Exception as e:
                    _record_error("bot", f"exodus bot send error: {e}")
                    return
                ev = req_entry["exodus_bot_event"]
                while not race_event.is_set():
                    idle_timeout = (
                        BYPASS_IDLE_TIMEOUT_SEC
                        if BYPASS_IDLE_TIMEOUT_SEC > 0
                        else exodus_BOT_TIMEOUT_SEC
                    )
                    last_seen = req_entry.get("last_exodus_ts") or time.time()
                    rem = None if idle_timeout <= 0 else last_seen + idle_timeout - time.time()
                    if rem is not None and rem <= 0:
                        _record_error("bot", "exodus bot idle timeout (no new message)")
                        break
                    if ev.wait(timeout=0.3 if rem is None else min(0.3, rem)):
                        r = req_entry.get("exodus_bot_result")
                        if r and r.get("bypassed"):
                            _declare(
                                "exodus_bot",
                                r["bypassed"],
                                {
                                    "module": "exodus_bot",
                                    "account": acc_state.get("name") or acc_id,
                                },
                            )
                        else:
                            _record_error(
                                "bot",
                                (r or {}).get("error") or "exodus bot no result",
                            )
                        break

            threads.append(threading.Thread(target=_bot_racer, daemon=True))
        else:
            errors['bot'] = "No active Telegram account for exodus bot"

    for t in threads:
        t.start()

    configured = [
        value
        for value in (exodus_TIMEOUT_SEC, exodus_BOT_TIMEOUT_SEC, BYPASS_IDLE_TIMEOUT_SEC)
        if value > 0
    ]
    race_budget = min(
        exodus_RACE_MAX_TIMEOUT_SEC,
        max(configured, default=30.0) + 5.0,
    )
    if not race_event.wait(timeout=race_budget):
        race_event.set()
        with errors_lock:
            errors.setdefault("race", "exodus resolver timed out")

    if acc_id and req_id:
        with accounts_lock:
            if acc_id in accounts:
                accounts[acc_id]["pending"].pop(req_id, None)
                alk = accounts[acc_id].get("exodus_dm_lock")
                if alk:
                    with alk:
                        q = accounts[acc_id].get("exodus_dm_queue", [])
                        if req_id in q:
                            q.remove(req_id)

    if winner.get('url'):
        return winner, None

    msg = " | ".join(f"{k}: {v}" for k, v in errors.items() if v) or "exodus API and exodus bot both failed"
    return None, msg


# ==================== FLASK APP ====================
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.json.sort_keys = False

# ── EXODUS bot system: API-key gate + DB export/import + user list ──
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
  .pill { display:inline-block; padding:.2rem .55rem; border-radius:999px; font-size:.68rem; font-weight:700; font-family:'JetBrains Mono',monospace; letter-spacing:.05em; }
  .pill-violet { background:rgba(139,92,246,.18); color:#a78bfa; }
  .pill-cyan { background:rgba(6,182,212,.18); color:#22d3ee; }
  .pill-green { background:rgba(34,197,94,.18); color:#4ade80; }
  .pill-red { background:rgba(239,68,68,.18); color:#f87171; }
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
      <button class="nav-item" onclick="showPage('users'); loadUsers()" id="nav-users"><span class="nav-icon">👥</span><span>Users</span></button>
      <button class="nav-item" onclick="showPage('database')" id="nav-database"><span class="nav-icon">💾</span><span>Database</span></button>
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
        <div class="page-sub">exodus IS THE PRIMARY RESOLVER — NICK IS DISABLED (HUMAN PUZZLE)</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-bottom:1.5rem;">

        <!-- exodus Bot Card -->
        <div class="glass" style="padding:1.5rem;grid-column:1/-1;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(236,72,153,.15);border:1px solid rgba(236,72,153,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🅰️</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">exodus — PRIMARY resolver</div>
              <div class="text-xs text-muted text-mono">@exodusbypassbot · DM via your account + HTTP API race</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="exodusBotLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">exodus API and @exodusbypassbot run in parallel</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="exodusBotToggle" onchange="toggleBot('exodus_bot', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>

        <!-- DZHQ Card -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(139,92,246,.15);border:1px solid rgba(139,92,246,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">🏆</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">DZHQ — Fallback only</div>
              <div class="text-xs text-muted text-mono">@DZHQ_BypassBot · Group chat</div>
            </div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between;padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);">
            <div>
              <div style="font-size:.82rem;font-weight:600;" id="dzhqLabel">Enabled</div>
              <div class="text-xs text-muted text-mono">Only tried if exodus fails</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="dzhqToggle" onchange="toggleBot('dzhq', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>

        <!-- Nick Card (disabled) -->
        <div class="glass" style="padding:1.5rem;opacity:.6;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(107,114,128,.15);border:1px solid rgba(107,114,128,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">⚡</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Nick — DISABLED</div>
              <div class="text-xs text-muted text-mono">@Nick_Bypass_Bot · Human verification puzzle</div>
            </div>
          </div>
          <div style="padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);color:var(--muted);font-size:.75rem;">
            Nick is disabled globally because it requires a human verification puzzle that blocks automation. exodus replaces it.
          </div>
        </div>
      </div>

      <!-- Race mode info banner -->
      <div class="glass" style="padding:1.25rem 1.5rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
        <div style="font-size:1.5rem;">🏁</div>
        <div>
          <div style="font-weight:700;font-size:.9rem;margin-bottom:.2rem;" id="raceModeText">exodus Primary Resolver</div>
          <div class="text-xs text-muted text-mono" id="raceModeDesc">exodus API and @exodusbypassbot run in parallel — first valid response wins. DZHQ is a silent fallback.</div>
        </div>
        <div id="raceBadge" class="badge badge-active" style="margin-left:auto;">exodus</div>
      </div>
    </div>

    <!-- Users -->
    <div class="page" id="page-users">
      <div class="page-header">
        <div class="page-title">Users</div>
        <div class="page-sub">TELEGRAM USERS OF @Exodus_link_bypass_bot</div>
      </div>
      <div class="section-header">
        <div class="actions-bar">
          <input type="text" id="userSearch" placeholder="Search by ID or @username..." style="background:rgba(0,0,0,.4);border:1px solid rgba(255,255,255,.1);border-radius:.5rem;padding:.5rem .75rem;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.78rem;outline:none;min-width:240px;">
          <button class="btn btn-cyan" onclick="loadUsers()">🔄 Refresh</button>
        </div>
        <div class="text-xs text-muted text-mono" id="usersCount"></div>
      </div>
      <div class="glass" style="margin-top:1rem">
        <div class="table-wrap">
          <table>
            <thead><tr><th>User ID</th><th>Username</th><th>First Name</th><th>Refs</th><th>Credits</th><th>Bypasses</th><th>Banned</th><th>Joined</th></tr></thead>
            <tbody id="usersTable"><tr><td colspan="8" class="empty-state">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:1rem;">
        <button class="btn btn-violet" onclick="usersPrev()" id="usersPrevBtn" disabled>⬅ Prev</button>
        <span class="text-xs text-muted text-mono" id="usersPage">Page 1</span>
        <button class="btn btn-violet" onclick="usersNext()" id="usersNextBtn">Next ➡</button>
      </div>
    </div>

    <!-- Database -->
    <div class="page" id="page-database">
      <div class="page-header">
        <div class="page-title">Database</div>
        <div class="page-sub">EXPORT / IMPORT BOT DATABASE</div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem;">
        <!-- Export -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">💾</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Export Database</div>
              <div class="text-xs text-muted text-mono">Download a snapshot of the bot DB</div>
            </div>
          </div>
          <div style="padding:.875rem 1rem;background:rgba(0,0,0,.25);border-radius:.625rem;border:1px solid rgba(255,255,255,.06);font-size:.75rem;color:var(--muted);line-height:1.7;margin-bottom:1rem;">
            Downloads <code>.db</code> (SQLite) and <code>.csv</code> (users) files. Safe — takes a consistent snapshot even while the bot is running.
          </div>
          <button class="btn btn-green btn-full" onclick="exportDb()">⬇ Download Database (.db)</button>
          <button class="btn btn-cyan btn-full" style="margin-top:.5rem;" onclick="exportCsv()">⬇ Download Users (.csv)</button>
        </div>

        <!-- Import -->
        <div class="glass" style="padding:1.5rem;">
          <div style="display:flex;align-items:center;gap:.875rem;margin-bottom:1rem;">
            <div style="width:44px;height:44px;border-radius:.75rem;background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);display:flex;align-items:center;justify-content:center;font-size:1.25rem;">📥</div>
            <div>
              <div style="font-weight:700;font-size:.95rem;">Import Database</div>
              <div class="text-xs text-muted text-mono">Replace the current DB</div>
            </div>
          </div>
          <div style="padding:.875rem 1rem;background:rgba(239,68,68,.08);border-radius:.625rem;border:1px solid rgba(239,68,68,.25);font-size:.75rem;color:#fca5a5;line-height:1.7;margin-bottom:1rem;">
            ⚠️ <b>Warning:</b> This will overwrite ALL current users, keys, channels, and settings. The current DB is backed up to <code>.db.bak</code> before overwriting.
          </div>
          <input type="file" id="dbFileInput" accept=".db" style="display:none" onchange="importDb()">
          <button class="btn btn-red btn-full" onclick="document.getElementById('dbFileInput').click()">📁 Choose .db File</button>
          <div class="text-xs text-muted text-mono" style="margin-top:.75rem;text-align:center;" id="importStatus"></div>
        </div>
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

// Users state
let usersOffset = 0;
const USERS_PAGE_SIZE = 25;
let usersTotal = 0;

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

async function apiCall(method, path, body, isForm) {
  const opts = { method, headers: { 'X-Admin-Token': token } };
  if (body && !isForm) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  } else if (body && isForm) {
    opts.body = body;
  }
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
  const nav = document.getElementById('nav-'+name);
  if (nav) nav.classList.add('active');
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
  const abEl   = document.getElementById('exodusBotToggle');
  const abLbl  = document.getElementById('exodusBotLabel');
  const dLbl   = document.getElementById('dzhqLabel');
  if (dEl)  dEl.checked  = d.dzhq;
  if (abEl) abEl.checked = (d.exodus_bot !== false);
  if (abLbl) { const on = (d.exodus_bot !== false); abLbl.textContent = on ? 'Enabled' : 'Disabled'; abLbl.style.color = on ? 'var(--green)' : 'var(--red)'; }
  if (dLbl)  { dLbl.textContent  = d.dzhq       ? 'Enabled'               : 'Disabled'; dLbl.style.color  = d.dzhq       ? 'var(--green)' : 'var(--red)'; }
}

async function toggleBot(bot, enabled) {
  try {
    const d = await apiCall('POST', '/admin/api/bots/toggle', {bot, enabled});
    if (!d.success) { toast(d.error || 'Cannot toggle', 'error'); loadBotsStatus(); return; }
    _applyBotState(d);
    const labels = { dzhq: 'DZHQ', exodus_bot: 'expdus resolver' };
    toast(`${labels[bot] || bot} ${enabled ? 'enabled' : 'disabled'}`, 'success');
  } catch(e) { toast(e.message, 'error'); loadBotsStatus(); }
}

// ── Users page ────────────────────────────────────────────────────────
async function loadUsers(offset) {
  if (offset !== undefined) usersOffset = offset;
  try {
    const data = await apiCall('GET', `/admin/api/users?offset=${usersOffset}&limit=${USERS_PAGE_SIZE}`);
    usersTotal = data.total;
    renderUsers(data.users);
    document.getElementById('usersCount').textContent = `Total: ${data.total} users`;
    const page = Math.floor(usersOffset / USERS_PAGE_SIZE) + 1;
    const totalPages = Math.max(1, Math.ceil(usersTotal / USERS_PAGE_SIZE));
    document.getElementById('usersPage').textContent = `Page ${page} / ${totalPages}`;
    document.getElementById('usersPrevBtn').disabled = usersOffset === 0;
    document.getElementById('usersNextBtn').disabled = (usersOffset + USERS_PAGE_SIZE) >= usersTotal;
  } catch(e) { toast('Could not load users', 'error'); }
}

function renderUsers(users) {
  const q = (document.getElementById('userSearch').value || '').toLowerCase().trim();
  const filtered = q ? users.filter(u => 
    String(u.id).includes(q) || 
    (u.username && u.username.toLowerCase().includes(q)) ||
    (u.first_name && u.first_name.toLowerCase().includes(q))
  ) : users;
  
  document.getElementById('usersTable').innerHTML = filtered.length ? filtered.map(u => {
    const uname = u.username ? `<a href="https://t.me/${esc(u.username)}" target="_blank" style="color:var(--cyan);text-decoration:none;">@${esc(u.username)}</a>` : '<span class="text-muted">—</span>';
    const joined = u.joined_at ? timeAgo(new Date(u.joined_at * 1000).toISOString()) : '—';
    return `<tr>
      <td class="text-mono"><code>${u.id}</code></td>
      <td>${uname}</td>
      <td>${esc(u.first_name || '—')}</td>
      <td class="text-mono">${u.total_refs || 0}</td>
      <td class="text-mono"><span class="pill pill-violet">${u.credits || 0}</span></td>
      <td class="text-mono">${u.bypass_count || 0}</td>
      <td>${u.banned ? '<span class="pill pill-red">BANNED</span>' : '<span class="pill pill-green">Active</span>'}</td>
      <td class="text-xs text-muted">${joined}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="8" class="empty-state">No users found</td></tr>';
}

function usersPrev() { if (usersOffset > 0) loadUsers(Math.max(0, usersOffset - USERS_PAGE_SIZE)); }
function usersNext() { if (usersOffset + USERS_PAGE_SIZE < usersTotal) loadUsers(usersOffset + USERS_PAGE_SIZE); }
document.getElementById('userSearch').addEventListener('input', () => loadUsers(usersOffset));

// ── Database export / import ──────────────────────────────────────────
async function exportDb() {
  try {
    const r = await fetch('/admin/api/db/export', { headers: { 'X-Admin-Token': token } });
    if (!r.ok) throw new Error('Export failed');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `exodus_db_${new Date().toISOString().slice(0,10)}.db`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Database downloaded', 'success');
  } catch(e) { toast(e.message || 'Export failed', 'error'); }
}

async function exportCsv() {
  try {
    const r = await fetch('/admin/api/db/export-csv', { headers: { 'X-Admin-Token': token } });
    if (!r.ok) throw new Error('Export failed');
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `exodus_users_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    toast('Users CSV downloaded', 'success');
  } catch(e) { toast(e.message || 'Export failed', 'error'); }
}

async function importDb() {
  const input = document.getElementById('dbFileInput');
  const file = input.files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.db')) {
    toast('Please select a .db file', 'error');
    input.value = '';
    return;
  }
  if (!confirm(`⚠️ REPLACE the current database with "${file.name}"?\n\nAll current users, keys, channels and settings will be overwritten.\nA .bak backup will be saved.`)) {
    input.value = '';
    return;
  }
  const status = document.getElementById('importStatus');
  status.textContent = 'Uploading...';
  try {
    const form = new FormData();
    form.append('file', file);
    const r = await fetch('/admin/api/db/import', {
      method: 'POST',
      headers: { 'X-Admin-Token': token },
      body: form,
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Import failed');
    status.textContent = '✅ Imported successfully';
    toast('Database imported!', 'success');
    setTimeout(() => { status.textContent = ''; }, 4000);
  } catch(e) {
    status.textContent = '❌ ' + (e.message || 'Import failed');
    toast(e.message || 'Import failed', 'error');
  } finally {
    input.value = '';
  }
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
            'exodus_bot_id': None,
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
        "exodus_bot":    bot_settings.get("exodus_bot", True),
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
    if bot not in ("dzhq", "nick", "dzhq_first", "nick_first", "random_mode", "exodus_bot"):
        return jsonify({"success": False, "error": "Invalid bot name"}), 400
    if bot == "exodus_bot":
        bot_settings["exodus_bot"] = enab
        return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))
    if bot in ("dzhq_first", "nick_first", "random_mode"):
        _set_exclusive_mode(bot if enab else None)
        if bot == "random_mode":
            with _random_lock:
                _random_state["bot"] = None
                _random_state["left"] = 0
                _random_state["batch"] = 0
        return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))
    other = "nick" if bot == "dzhq" else "dzhq"
    if not enab and not bot_settings[other]:
        return jsonify({"success": False,
                        "error": "Cannot disable both bots — keep at least one enabled"}), 400
    bot_settings[bot] = enab
    return jsonify(_bots_state({"success": True, "bot": bot, "enabled": enab}))


# ── Users list (from bot DB) ──────────────────────────────────────────
@app.route('/admin/api/users', methods=['GET'])
@require_auth
def admin_users_list():
    try:
        offset = int(request.args.get('offset', 0))
        limit  = int(request.args.get('limit', 25))
        limit  = max(1, min(limit, 100))
        rows, total = botsys.list_users_paginated(offset, limit)
        return jsonify({"users": rows, "total": total, "offset": offset, "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e), "users": [], "total": 0}), 500


# ── DB Export / Import ────────────────────────────────────────────────
@app.route('/admin/api/db/export', methods=['GET'])
@require_auth
def admin_db_export():
    try:
        data = botsys.export_db_bytes()
        return send_file(
            __import__('io').BytesIO(data),
            mimetype="application/x-sqlite3",
            as_attachment=True,
            download_name=f"exodus_db_{time.strftime('%Y%m%d_%H%M%S')}.db"
        )
    except Exception as e:
        return jsonify({"error": f"Export failed: {e}"}), 500

@app.route('/admin/api/db/export-csv', methods=['GET'])
@require_auth
def admin_db_export_csv():
    try:
        data = botsys.export_users_csv()
        return send_file(
            __import__('io').BytesIO(data),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"exodus_users_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        )
    except Exception as e:
        return jsonify({"error": f"CSV export failed: {e}"}), 500

@app.route('/admin/api/db/import', methods=['POST'])
@require_auth
def admin_db_import():
    try:
        f = request.files.get('file')
        if not f:
            return jsonify({"error": "No file uploaded"}), 400
        data = f.read()
        if len(data) > 200 * 1024 * 1024:
            return jsonify({"error": "File too large (max 200 MB)"}), 400
        botsys.import_db_bytes(data)
        return jsonify({"success": True, "size": len(data), "message": "Database replaced. Backup saved as .db.bak"})
    except Exception as e:
        return jsonify({"error": f"Import failed: {e}"}), 500


# ==================== PUBLIC HOME PAGE ====================
HOME_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Exodus Bypass — Lightning Fast Link Resolver</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #030712; --bg2: #0a0f1d; --bg3: #111827;
    --violet: #8b5cf6; --cyan: #06b6d4; --pink: #ec4899;
    --green: #22c55e; --amber: #f59e0b; --red: #ef4444; --blue: #3b82f6;
    --text: #e2e8f0; --muted: #94a3b8; --border: #1e293b;
    --card: rgba(255,255,255,0.03); --card-border: rgba(255,255,255,0.08);
  }
  html { scroll-behavior: smooth; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; overflow-x: hidden; line-height: 1.6; }
  
  /* Background bubbles */
  .bubbles { position: fixed; inset: 0; overflow: hidden; pointer-events: none; z-index: 0; }
  .bubble { position: absolute; border-radius: 50%; opacity: 0.15; animation: float linear infinite; }
  @keyframes float { 
    0%{transform:translateY(100vh) scale(0);opacity:0} 
    10%{opacity:0.18} 
    90%{opacity:0.1} 
    100%{transform:translateY(-100px) scale(1);opacity:0} 
  }
  
  /* Nav */
  nav {
    position: sticky; top: 0; z-index: 100;
    padding: 1rem 2rem;
    background: rgba(3,7,18,0.85);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; flex-wrap: wrap;
  }
  .logo { font-size: 1.15rem; font-weight: 900; letter-spacing: -.02em; background: linear-gradient(90deg, var(--violet), var(--cyan)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; display: flex; align-items: center; gap: .5rem; }
  .logo-icon { font-size: 1.5rem; -webkit-text-fill-color: initial; }
  .nav-links { display: flex; gap: 1.5rem; align-items: center; flex-wrap: wrap; }
  .nav-links a { color: var(--muted); text-decoration: none; font-size: .9rem; font-weight: 500; transition: color .2s; }
  .nav-links a:hover { color: var(--cyan); }
  .nav-cta { padding: .55rem 1.1rem; background: linear-gradient(135deg, var(--violet), var(--cyan)); border-radius: .5rem; color: white !important; font-weight: 700; font-size: .85rem; box-shadow: 0 4px 16px rgba(139,92,246,0.4); }
  
  /* Hero */
  .hero {
    position: relative; z-index: 5;
    padding: 5rem 2rem 4rem;
    text-align: center;
    max-width: 1100px; margin: 0 auto;
  }
  .hero-badge {
    display: inline-flex; align-items: center; gap: .5rem;
    padding: .45rem 1rem;
    background: rgba(34,197,94,0.1);
    border: 1px solid rgba(34,197,94,0.3);
    border-radius: 999px;
    font-size: .78rem; font-weight: 600;
    color: var(--green);
    font-family: 'JetBrains Mono', monospace;
    letter-spacing: .05em;
    margin-bottom: 1.5rem;
    animation: fadeInUp .6s ease;
  }
  .hero-badge .live-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
    animation: pulse 1.5s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .hero h1 {
    font-size: clamp(2.2rem, 5vw, 4rem);
    font-weight: 900; line-height: 1.1;
    letter-spacing: -.03em;
    margin-bottom: 1.25rem;
    background: linear-gradient(135deg, #fff 0%, var(--violet) 40%, var(--cyan) 70%, var(--pink) 100%);
    background-size: 200% 200%;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    animation: gradientShift 8s ease infinite, fadeInUp .7s ease;
  }
  @keyframes gradientShift { 0%,100%{background-position:0% 50%} 50%{background-position:100% 50%} }
  @keyframes fadeInUp { from{opacity:0;transform:translateY(24px)} to{opacity:1;transform:none} }
  .hero p.lead {
    font-size: clamp(1rem, 2vw, 1.2rem);
    color: var(--muted);
    max-width: 640px; margin: 0 auto 2rem;
    animation: fadeInUp .8s ease .1s backwards;
  }
  .hero-buttons {
    display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap;
    animation: fadeInUp .9s ease .2s backwards;
  }
  .btn-cta {
    display: inline-flex; align-items: center; gap: .6rem;
    padding: 1rem 1.75rem;
    background: linear-gradient(135deg, var(--violet), var(--cyan));
    border: none; border-radius: .75rem;
    color: white; font-weight: 700; font-size: .95rem;
    text-decoration: none; cursor: pointer;
    box-shadow: 0 8px 32px rgba(139,92,246,0.4);
    transition: transform .15s, box-shadow .15s;
    font-family: 'Inter', sans-serif;
    letter-spacing: .01em;
  }
  .btn-cta:hover { transform: translateY(-2px); box-shadow: 0 12px 40px rgba(139,92,246,0.5); }
  .btn-cta.secondary {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.15);
    box-shadow: none;
    backdrop-filter: blur(10px);
  }
  .btn-cta.secondary:hover { background: rgba(255,255,255,0.1); }
  
  /* Stats strip */
  .stats-strip {
    position: relative; z-index: 5;
    max-width: 1100px; margin: 0 auto 4rem; padding: 0 2rem;
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 1rem;
  }
  .stat {
    padding: 1.25rem 1rem;
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 1rem;
    text-align: center;
    backdrop-filter: blur(12px);
  }
  .stat-value {
    font-size: 1.9rem; font-weight: 900;
    background: linear-gradient(135deg, var(--violet), var(--cyan));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    font-family: 'JetBrains Mono', monospace;
    line-height: 1;
    margin-bottom: .4rem;
  }
  .stat-label {
    font-size: .72rem; color: var(--muted);
    text-transform: uppercase; letter-spacing: .12em;
    font-weight: 600;
  }
  
  /* Sections */
  section.block {
    position: relative; z-index: 5;
    max-width: 1100px; margin: 0 auto; padding: 4rem 2rem;
  }
  .section-header { text-align: center; margin-bottom: 3rem; }
  .section-tag {
    display: inline-block;
    padding: .35rem .9rem;
    background: rgba(139,92,246,0.12);
    border: 1px solid rgba(139,92,246,0.3);
    border-radius: 999px;
    font-size: .72rem; font-weight: 700;
    color: var(--violet);
    text-transform: uppercase; letter-spacing: .15em;
    font-family: 'JetBrains Mono', monospace;
    margin-bottom: 1rem;
  }
  .section-header h2 {
    font-size: clamp(1.75rem, 3.5vw, 2.5rem);
    font-weight: 900; letter-spacing: -.02em;
    margin-bottom: .75rem;
    background: linear-gradient(90deg, #fff, var(--cyan));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .section-header p { color: var(--muted); max-width: 560px; margin: 0 auto; font-size: 1.05rem; }
  
  /* Features */
  .features {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1.25rem;
  }
  .feature {
    padding: 1.75rem;
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 1.25rem;
    transition: transform .2s, border-color .2s;
    backdrop-filter: blur(12px);
  }
  .feature:hover { transform: translateY(-4px); border-color: rgba(139,92,246,0.4); }
  .feature-icon {
    width: 52px; height: 52px;
    border-radius: 1rem;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.5rem;
    margin-bottom: 1rem;
    background: linear-gradient(135deg, rgba(139,92,246,0.2), rgba(6,182,212,0.2));
    border: 1px solid rgba(139,92,246,0.3);
  }
  .feature h3 { font-size: 1.1rem; font-weight: 700; margin-bottom: .5rem; }
  .feature p { font-size: .9rem; color: var(--muted); line-height: 1.6; }
  
  /* How to get key */
  .steps {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1.5rem;
    margin-top: 1rem;
  }
  .step {
    position: relative;
    padding: 2rem 1.5rem 1.5rem;
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 1.25rem;
    text-align: center;
    backdrop-filter: blur(12px);
  }
  .step-num {
    position: absolute; top: -20px; left: 50%; transform: translateX(-50%);
    width: 40px; height: 40px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--violet), var(--cyan));
    display: flex; align-items: center; justify-content: center;
    font-weight: 900; font-size: 1rem;
    color: white;
    box-shadow: 0 4px 16px rgba(139,92,246,0.5);
  }
  .step h3 { font-size: 1rem; font-weight: 700; margin-bottom: .5rem; }
  .step p { font-size: .85rem; color: var(--muted); line-height: 1.5; }
  
  /* Free tier card */
  .free-card {
    max-width: 640px; margin: 0 auto;
    padding: 2.5rem 2rem;
    background: linear-gradient(135deg, rgba(139,92,246,0.1), rgba(6,182,212,0.1));
    border: 1px solid rgba(139,92,246,0.3);
    border-radius: 1.5rem;
    text-align: center;
    box-shadow: 0 20px 60px rgba(139,92,246,0.15);
    position: relative;
    overflow: hidden;
  }
  .free-card::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(90deg, var(--violet), var(--cyan), var(--pink));
  }
  .free-card h3 { font-size: 1.5rem; font-weight: 900; margin-bottom: .75rem; }
  .free-card .big-number {
    font-size: 3.5rem; font-weight: 900;
    background: linear-gradient(135deg, var(--green), var(--cyan));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
    font-family: 'JetBrains Mono', monospace;
    line-height: 1;
    margin: .5rem 0;
  }
  .free-card p { color: var(--muted); margin-bottom: 1.5rem; }
  
  /* API preview */
  .api-preview {
    max-width: 720px; margin: 0 auto;
    background: #0a0f1d;
    border: 1px solid var(--card-border);
    border-radius: 1rem;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  .api-preview-header {
    display: flex; align-items: center; gap: .5rem;
    padding: .75rem 1rem;
    background: rgba(255,255,255,0.03);
    border-bottom: 1px solid var(--border);
  }
  .api-dot { width: 10px; height: 10px; border-radius: 50%; }
  .api-preview-title {
    margin-left: auto;
    font-family: 'JetBrains Mono', monospace;
    font-size: .72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .1em;
  }
  .api-preview pre {
    padding: 1.25rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: .82rem;
    line-height: 1.7;
    color: var(--text);
    overflow-x: auto;
  }
  .api-preview .kw { color: var(--pink); }
  .api-preview .str { color: var(--green); }
  .api-preview .url { color: var(--cyan); }
  .api-preview .comment { color: var(--muted); }
  
  /* Footer */
  footer {
    position: relative; z-index: 5;
    padding: 3rem 2rem 2rem;
    text-align: center;
    border-top: 1px solid var(--border);
    background: var(--bg2);
    margin-top: 4rem;
  }
  footer p { color: var(--muted); font-size: .85rem; margin-bottom: .5rem; }
  footer .dev {
    display: inline-flex; align-items: center; gap: .5rem;
    padding: .5rem 1rem;
    background: rgba(139,92,246,0.1);
    border: 1px solid rgba(139,92,246,0.3);
    border-radius: 999px;
    color: var(--violet);
    text-decoration: none;
    font-weight: 600; font-size: .85rem;
    margin-top: .75rem;
    transition: background .2s;
  }
  footer .dev:hover { background: rgba(139,92,246,0.2); }
  
  /* Responsive */
  @media (max-width: 640px) {
    nav { padding: .75rem 1rem; }
    .nav-links { gap: 1rem; }
    .nav-links a:not(.nav-cta) { display: none; }
    .hero { padding: 3rem 1.25rem 2rem; }
    section.block { padding: 3rem 1.25rem; }
  }
</style>
</head>
<body>
<div class="bubbles" id="bubbles"></div>

<nav>
  <div class="logo"><span class="logo-icon">⚡</span> Exodus Bypass</div>
  <div class="nav-links">
    <a href="#features">Features</a>
    <a href="#get-key">Get API Key</a>
    <a href="#api">API Docs</a>
    <a href="/admin" style="color:var(--muted);font-size:.8rem;">Admin</a>
    <a href="https://t.me/Exodus_link_bypass_bot" target="_blank" class="nav-cta">🤖 Open Bot</a>
  </div>
</nav>

<!-- Hero -->
<div class="hero">
  <div class="hero-badge">
    <span class="live-dot"></span>
    <span id="liveStatus">API ONLINE</span>
  </div>
  <h1>Bypass Any Short Link<br>In Seconds ⚡</h1>
  <p class="lead">Powered by <b>@mainexodus</b> via real Telegram accounts. Get instant access to the real destination behind URL shorteners, ad-gateways, and file hosts.</p>
  <div class="hero-buttons">
    <a href="https://t.me/Exodus_link_bypass_bot" target="_blank" class="btn-cta">🚀 Open Telegram Bot</a>
    <a href="#get-key" class="btn-cta secondary">🔑 Get Free API Key</a>
  </div>
</div>

<!-- Stats strip -->
<div class="stats-strip" id="statsStrip">
  <div class="stat">
    <div class="stat-value" id="statAccounts">—</div>
    <div class="stat-label">Accounts Online</div>
  </div>
  <div class="stat">
    <div class="stat-value" id="statBypasses">—</div>
    <div class="stat-label">Bypasses Served</div>
  </div>
  <div class="stat">
    <div class="stat-value" id="statSuccess">—</div>
    <div class="stat-label">Success Rate</div>
  </div>
  <div class="stat">
    <div class="stat-value">10</div>
    <div class="stat-label">Free / 24h</div>
  </div>
</div>

<!-- Features -->
<section class="block" id="features">
  <div class="section-header">
    <div class="section-tag">⚡ Features</div>
    <h2>What This Bot Does</h2>
    <p>Everything you need to bypass links — from a single powerful Telegram bot.</p>
  </div>
  <div class="features">
    <div class="feature">
      <div class="feature-icon">🔗</div>
      <h3>Instant Link Bypass</h3>
      <p>Paste any short link → get the main destination in seconds. Powered by @mainexodus.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">⚡</div>
      <h3>Lightning Fast</h3>
      <p>Parallel race between the exodus HTTP API and your Telegram accounts — first valid response wins.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">🎁</div>
      <h3>Free Daily Bypasses</h3>
      <p>Every user gets 10 free bypasses every 24 hours. No signup, no strings attached.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">🔑</div>
      <h3>API Keys via Referrals</h3>
      <p>Refer 5 friends → 7-day API key. Refer 10 → 15 days. Refer 20 → 1 full month.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">💳</div>
      <h3>Credits System</h3>
      <p>Out of free bypasses? 1 referral = 10 extra credits. Redeem anytime from the bot.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">📢</div>
      <h3>Force-Join Channels</h3>
      <p>Users must join your channels first — growing your community automatically.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">🌐</div>
      <h3>Public REST API</h3>
      <p>Full HTTP API with API keys, daily limits, and JSON responses. Perfect for bots and apps.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">👨‍💻</div>
      <h3>Active Development</h3>
      <p>Continuously updated and maintained by <b>@exodus_own3r</b>. New features added regularly.</p>
    </div>
  </div>
</section>

<!-- How to get API key -->
<section class="block" id="get-key">
  <div class="section-header">
    <div class="section-tag">🔑 Get API Key</div>
    <h2>Three Simple Ways</h2>
    <p>Get a free API key in under a minute — no registration required.</p>
  </div>
  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <h3>Refer Friends</h3>
      <p>Share your referral link. When friends join the required channels, you earn points that convert to API keys.</p>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <h3>Redeem Credits</h3>
      <p>Use 1 referral point to get 10 bypass credits. Perfect for quick single-use links.</p>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <h3>Buy Premium</h3>
      <p>Need more? Message @exodus_own3r for custom plans, higher limits, and instant keys.</p>
    </div>
  </div>
  <div style="text-align:center; margin-top:2.5rem;">
    <a href="https://t.me/Exodus_link_bypass_bot" target="_blank" class="btn-cta">🤖 Get Key from Bot</a>
  </div>
</section>

<!-- Free tier highlight -->
<section class="block">
  <div class="free-card">
    <h3>🎁 Try It Free Today</h3>
    <div class="big-number">10</div>
    <p><b>Free bypasses every 24 hours</b> — no account, no API key required.</p>
    <a href="https://t.me/Exodus_link_bypass_bot" target="_blank" class="btn-cta">Start Free Now →</a>
  </div>
</section>

<!-- API preview -->
<section class="block" id="api">
  <div class="section-header">
    <div class="section-tag">🌐 API</div>
    <h2>Developer-Friendly REST API</h2>
    <p>Simple JSON API. Get your key from the bot, then call <code style="color:var(--cyan);background:rgba(6,182,212,0.1);padding:.1rem .4rem;border-radius:.3rem;">/bypass</code>.</p>
  </div>
  <div class="api-preview">
    <div class="api-preview-header">
      <div class="api-dot" style="background:#ef4444;"></div>
      <div class="api-dot" style="background:#f59e0b;"></div>
      <div class="api-dot" style="background:#22c55e;"></div>
      <div class="api-preview-title">Example Request</div>
    </div>
    <pre><span class="comment"># GET request with your API key</span>
<span class="kw">curl</span> -H <span class="str">"X-API-Key: EXO-XXXXXXXX-XXXXXXXX-XXXXXXXX"</span> \
     <span class="str">"<span class="url" id="apiUrl">https://exodus-link-bypassser.onrender.com</span>/bypass?link=SHORT_LINK"</span>

<span class="comment"># Response</span>
{
  <span class="str">"status"</span>: <span class="kw">true</span>,
  <span class="str">"url"</span>: <span class="str">"https://real-destination.com/file.mp4"</span>,
  <span class="str">"links"</span>: {
    <span class="str">"original"</span>: <span class="str">"https://shorte.st/abc123"</span>,
    <span class="str">"bypassed"</span>: <span class="str">"https://real-destination.com/file.mp4"</span>
  },
  <span class="str">"response_ms"</span>: <span class="str">"1240ms"</span>
}</pre>
  </div>
</section>

<!-- Footer -->
<footer>
  <p>⚡ <b>Exodus Bypass</b> — Lightning-fast link resolver</p>
  <p style="font-size:.78rem;">Powered by @mainexodus· Free tier: 10 bypasses / 24h</p>
  <a href="https://t.me/exodus_own3r" target="_blank" class="dev">
    👨‍💻 Developer: @exodus_own3r
  </a>
</footer>

<script>
// Bubbles
(function() {
  const colors = ['#8b5cf6','#06b6d4','#ec4899','#3b82f6','#22c55e','#f59e0b'];
  const wrap = document.getElementById('bubbles');
  for (let i = 0; i < 22; i++) {
    const b = document.createElement('div');
    b.className = 'bubble';
    const size = Math.random()*70+25;
    b.style.cssText = `width:${size}px;height:${size}px;left:${Math.random()*100}%;background:${colors[Math.floor(Math.random()*colors.length)]};animation-duration:${Math.random()*18+12}s;animation-delay:${Math.random()*-22}s;filter:blur(${Math.random()*4+2}px);`;
    wrap.appendChild(b);
  }
})();

// Live stats
async function loadStats() {
  try {
    const d = await (await fetch('/status')).json();
    const accs = d.accounts || [];
    const online = accs.filter(a => a.online).length;
    const totalBypasses = accs.reduce((s,a)=>s+(a.bypasses||0),0);
    const succ = accs.reduce((s,a)=>s+(a.success||0),0);
    const rate = totalBypasses > 0 ? ((succ/totalBypasses)*100).toFixed(1)+'%' : '100%';
    
    document.getElementById('statAccounts').textContent = online;
    document.getElementById('statBypasses').textContent = totalBypasses.toLocaleString();
    document.getElementById('statSuccess').textContent = rate;
    document.getElementById('liveStatus').textContent = d.api_online ? 'API ONLINE' : 'API OFFLINE';
    document.getElementById('liveStatus').style.color = d.api_online ? 'var(--green)' : 'var(--red)';
  } catch(e) {}
}

// Set API URL from current origin
document.getElementById('apiUrl').textContent = window.location.origin;

loadStats();
setInterval(loadStats, 8000);
</script>
</body>
</html>"""


@app.route('/', methods=['GET'])
def home():
    return render_template_string(HOME_HTML)


# ==================== BYPASS ROUTE ====================

def _single_bypass_payload(original, bypassed, source, response_ms, account, module):
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
    try:
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

    # ── STEP 1: exodus is the PRIMARY resolver for EVERY link ───────────
    # @exodusbypassbot via your logged-in Telegram account + exodus HTTP API race.
    # No other bot is tried here — Nick's human puzzle is disabled globally.
    t0 = time.time()
    _flow_update(job_id, "processing", "Calling exodus resolver")
    exodus_winner, exodus_error = exodus_race(link)
    if exodus_winner:
        result_url = exodus_winner["url"]
        extra = exodus_winner.get("extra", {})
        _flow_update(job_id, "success", "Bypass successful", source=exodus_winner["source"], url=result_url)
        return jsonify(_single_bypass_payload(
            original=link,
            bypassed=result_url,
            source=exodus_winner["source"],
            response_ms=f"{int((time.time() - t0) * 1000)}ms",
            account=extra.get("account", "exodus_api"),
            module=extra.get("module", "exodus_bot"),
        ))
    _flow_update(job_id, "processing",
                 f"exodus did not respond ({exodus_error}); trying DZHQ fallback")
    print(f"[exodus] {exodus_error} — trying DZHQ fallback")

    # ── STEP 2: DZHQ silent fallback (exodus failed) ────────────────────
    if not bot_settings.get("dzhq", True):
        _flow_update(job_id, "failed", "exodus failed and DZHQ fallback is disabled")
        return jsonify({
            "status":      False,
            "developer":   DEVELOPER,
            "message":     f"exodus resolver failed ({exodus_error}). DZHQ fallback is disabled in admin panel.",
            "response_ms": f"{int((time.time() - t0) * 1000)}ms",
        }), 422

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

    req_entry = {
        "link":         link,
        "ts":           t0,
        "done":         False,
        "dzhq_event":   threading.Event(),
        "dzhq_result":  None,
        "dzhq_sent_id": None,
        "last_dzhq_ts": None,
    }
    with accounts_lock:
        accounts[acc_id]["pending"][req_id] = req_entry

    TIMEOUT      = BYPASS_IDLE_TIMEOUT_SEC
    DZHQ_TIMEOUT = 20.0

    async def _send_dzhq(tg):
        try:
            s1 = await tg.send_message(DZHQ_GROUP, f"/b {link}")
            req_entry['dzhq_sent_id'] = s1.id
            req_entry["last_dzhq_ts"] = time.time()
            _trace("DZHQ", f"sent /b for {link}, message_id={s1.id}")
        except Exception as e:
            req_entry['dzhq_fail'] = f"DZHQ send error: {e}"
            _trace("DZHQ", f"send failed for {link}: {e}")

    try:
        asyncio.run_coroutine_threadsafe(_send_dzhq(acc_state["client"]), loop).result(timeout=12)
    except Exception as e:
        with accounts_lock:
            accounts[acc_id]["pending"].pop(req_id, None)
        return jsonify({
            "status":    False,
            "developer": DEVELOPER,
            "message":   f"Failed to send to DZHQ: {e}",
        }), 500

    race_event  = threading.Event()
    race_winner = {}
    race_lock   = threading.Lock()

    def _declare_winner(source, data):
        with race_lock:
            if race_event.is_set():
                return False
            race_winner['source'] = source
            race_winner['data']   = data
            race_event.set()
            _trace(source.upper(), f"winner for {link}: {data}")
            return True

    request_budget = (
        TIMEOUT * 2 if TIMEOUT > 0 else MAX_BYPASS_TIMEOUT_SEC
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

    t1 = threading.Thread(target=_watch_dzhq, daemon=True)
    t1.start()
    race_event.wait(timeout=max(0.0, request_deadline - time.monotonic()))

    async def _cleanup():
        tg = acc_state["client"]
        mid = req_entry.get('dzhq_sent_id')
        if mid:
            try:
                await tg.delete_messages(DZHQ_GROUP, [mid])
            except:
                pass
    try:
        asyncio.run_coroutine_threadsafe(_cleanup(), loop)
    except:
        pass

    with accounts_lock:
        accounts[acc_id]["pending"].pop(req_id, None)

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
        _flow_update(job_id, "failed", "exodus and DZHQ fallback both failed", response_ms=f"{ms}ms")
        return jsonify({
            "status":      False,
            "developer":   DEVELOPER,
            "message":     "exodus resolver and DZHQ fallback both failed",
            "exodus":        exodus_error,
            "dzhq":        req_entry.get('dzhq_fail', 'no response'),
            "response_ms": f"{ms}ms",
        }), 422

    data = race_winner['data']
    acct = acc_state.get("name", "unknown")
    _flow_update(job_id, "success", "Bypass successful (DZHQ fallback)", source="dzhq")

    r = data[0]
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


# ==================== PUBLIC STATUS ====================
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
        "api_online":     bool(exodus_BYPASS_API) or len(online) > 0,
        "total_accounts": len(accs),
        "online_accounts": len(online),
        "exodus_api":       bool(exodus_BYPASS_API),
        "bots":           {"dzhq": bot_settings.get("dzhq", True),
                           "nick": bot_settings.get("nick", False),
                           "exodus_bot": bot_settings.get("exodus_bot", True)},
        "accounts":       accs,
    }


@app.route('/status', methods=['GET'])
def public_status():
    return jsonify(_public_status())


# ==================== FLASK SERVER ====================
def run_flask():
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False, threaded=True)


# ==================== STARTUP ====================
if __name__ == '__main__':
    try:
        load_accounts_from_file()
    except Exception as e:
        print(f"⚠️ Could not load saved accounts: {e}")

    if accounts:
        print(f"ℹ️ Saved accounts loaded: {len(accounts)}")
    else:
        print("ℹ️ No Telegram accounts configured. API/Admin Panel remain available; add one via /admin.")

    threading.Thread(target=run_flask, daemon=True).start()
    botsys.start_bot(PORT)

    print(f"""
{'='*58}
  ✅ Exodus BYPASS API STARTED
  ⚡ Bypass:   http://localhost:{PORT}/bypass?link=URL
  📊 Admin:    http://localhost:{PORT}/admin
  🔑 Password: {ADMIN_PASSWORD}
  🅰️ Primary:  @exodus_bypass_bot (via your account + API race)
  🏆 Fallback: DZHQ (silent)
  ⚡ Nick:     DISABLED (human puzzle)
  💾 Accounts: {len(accounts)} total
{'='*58}
""")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")