"""
botsys.py — EXODUS Telegram front-end for the Bypass API
Developer: @exodus_own3r

What this adds on top of main.py
  • Telegram bot: user sends a short link -> bot calls the local /bypass API -> returns the main link
  • Footer "Developer: @exodus_own3r" on every user-facing message
  • Force-join: admin-managed channels, re-checked on EVERY request (new channels apply instantly)
  • Referral system -> API keys   (5 refs = 7 days | 10 refs = 15 days | 20 refs = 1 month)
  • API key gate on /bypass and /bypass/async  (key via ?key=, X-API-Key header or JSON body)
  • "Buy / contact" button everywhere
  • Premium in-bot admin panel with coloured buttons (Bot API button `style`)

Only stdlib + requests (already a Flask dependency chain) are needed.
"""
import os
import re
import json
import time
import html
import sqlite3
import secrets
import threading
import copy
from concurrent.futures import ThreadPoolExecutor

import requests

# ═══════════════════════════ CONFIG ═══════════════════════════
DB_PATH          = os.environ.get("BOT_DB", "exodus_bot.db")
DEV_TAG          = "@exodus_own3r"
DEV_URL          = "https://t.me/exodus_own3r"
CONTACT_USERNAME = os.environ.get("CONTACT_USERNAME", "exodus_own3r").lstrip("@")
CONTACT_URL      = f"https://t.me/{CONTACT_USERNAME}"
DEFAULT_DAILY    = int(os.environ.get("API_DAILY_LIMIT", "500"))

# (referrals needed, key days, label)
PLANS = [(5, 7, "7 Days"), (10, 15, "15 Days"), (20, 30, "1 Month")]

# Shared secret so the bot can call the local API without a user key.
INTERNAL_TOKEN = secrets.token_hex(24)

FOOTER = f"\n\n━━━━━━━━━━━━━━━━━━\n👨‍💻 <b>Developer:</b> {DEV_TAG}"

BOT_TOKEN    = ""
BOT_ID       = 0
BOT_USERNAME = ""
BASE_URL     = "https://exodus-link-bypassser.onrender.com/"
LOCAL_PORT   = 5000
ADMIN_IDS    = set()

E = html.escape


def _require_key_default():
    v = os.environ.get("REQUIRE_API_KEY")
    if v is None:
        return bool(os.environ.get("BOT_TOKEN", "").strip())
    return v.lower() in ("1", "true", "yes", "on")


# ═══════════════════════════ DATABASE ═══════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at INTEGER,
  referred_by INTEGER, ref_counted INTEGER DEFAULT 0,
  ref_points INTEGER DEFAULT 0, total_refs INTEGER DEFAULT 0,
  banned INTEGER DEFAULT 0, bypass_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT UNIQUE, title TEXT, link TEXT, added_at INTEGER);
CREATE TABLE IF NOT EXISTS keys(
  key TEXT PRIMARY KEY, user_id INTEGER, created_at INTEGER, expires_at INTEGER,
  revoked INTEGER DEFAULT 0, daily_limit INTEGER, plan TEXT);
CREATE TABLE IF NOT EXISTS usage(
  key TEXT, day TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(key, day));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

_db = None
_dblock = threading.RLock()


def _db_init():
    global _db
    _db = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _db.row_factory = sqlite3.Row
    try:
        _db.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    _db.executescript(SCHEMA)


def q(sql, args=(), one=False, many=False):
    with _dblock:
        if _db is None:
            _db_init()
        cur = _db.execute(sql, args)
        if one:
            return cur.fetchone()
        if many:
            return cur.fetchall()
        return cur.rowcount


def get_setting(k, default=None):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else default


def set_setting(k, v):
    q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def daily_limit():
    try:
        return int(get_setting("daily_limit", DEFAULT_DAILY))
    except Exception:
        return DEFAULT_DAILY


def force_join_on():
    return get_setting("force_join", "1") == "1"


# ── users ───────────────────────────────────────────────────────
def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def upsert_user(frm, referred_by=None):
    uid = frm["id"]
    uname = frm.get("username") or ""
    fname = frm.get("first_name") or ""
    row = get_user(uid)
    if row is None:
        rb = None
        if referred_by and referred_by != uid and get_user(referred_by):
            rb = referred_by
        q("INSERT INTO users(id,username,first_name,joined_at,referred_by) VALUES(?,?,?,?,?)",
          (uid, uname, fname, int(time.time()), rb))
        return get_user(uid), True
    q("UPDATE users SET username=?, first_name=? WHERE id=?", (uname, fname, uid))
    return row, False


# ── keys ────────────────────────────────────────────────────────
def _new_key():
    h = secrets.token_hex(12).upper()
    return f"EXO-{h[:8]}-{h[8:16]}-{h[16:]}"


def active_key(uid):
    return q("SELECT * FROM keys WHERE user_id=? AND revoked=0 AND expires_at>? "
             "ORDER BY expires_at DESC LIMIT 1", (uid, int(time.time())), one=True)


def grant_key(uid, days, label=""):
    """Create a key, or extend the user's active one. Returns (key, expires_at)."""
    now = int(time.time())
    row = active_key(uid)
    if row:
        exp = max(now, row["expires_at"]) + days * 86400
        q("UPDATE keys SET expires_at=? WHERE key=?", (exp, row["key"]))
        return row["key"], exp
    key, exp = _new_key(), now + days * 86400
    q("INSERT INTO keys(key,user_id,created_at,expires_at,plan) VALUES(?,?,?,?,?)",
      (key, uid, now, exp, label))
    return key, exp


def claim_plan(uid, idx):
    cost, days, label = PLANS[idx]
    # atomic: only deducts when the user really has enough referral points
    n = q("UPDATE users SET ref_points=ref_points-? WHERE id=? AND ref_points>=?", (cost, uid, cost))
    if not n:
        return None
    return grant_key(uid, days, label)


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def key_usage_today(key):
    r = q("SELECT count FROM usage WHERE key=? AND day=?", (key, _today()), one=True)
    return r["count"] if r else 0


def validate_and_count(key):
    """-> (ok, http_status, message). Counts one request when ok."""
    if not key:
        return False, 401, "API key required. Get a free key from the bot (refer friends) or buy one."
    row = q("SELECT k.*, u.banned AS banned FROM keys k LEFT JOIN users u ON u.id=k.user_id "
            "WHERE k.key=?", (key,), one=True)
    if not row:
        return False, 401, "Invalid API key."
    if row["revoked"]:
        return False, 403, "This API key has been revoked."
    if row["banned"]:
        return False, 403, "This account is banned."
    if row["expires_at"] <= int(time.time()):
        return False, 401, "API key expired. Refer more friends or buy a plan to renew."
    limit = row["daily_limit"] or daily_limit()
    day = _today()
    q("INSERT OR IGNORE INTO usage(key,day,count) VALUES(?,?,0)", (key, day))
    if not q("UPDATE usage SET count=count+1 WHERE key=? AND day=? AND count<?", (key, day, limit)):
        return False, 429, f"Daily limit reached ({limit}/day). Buy a higher plan."
    return True, 200, "ok"


# ── channels ────────────────────────────────────────────────────
def list_channels():
    return q("SELECT * FROM channels ORDER BY id", many=True)


# ═══════════════════════════ TELEGRAM LAYER ═══════════════════════════
class TGError(Exception):
    def __init__(self, desc, code=0, retry_after=0):
        super().__init__(desc)
        self.desc, self.code, self.retry_after = desc, code, retry_after


_tl = threading.local()


def tg(method, _t=30, **params):
    s = getattr(_tl, "s", None)
    if s is None:
        s = _tl.s = requests.Session()
    try:
        r = s.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params, timeout=_t)
        data = r.json()
    except Exception as e:
        raise TGError(f"network: {e}")
    if not data.get("ok"):
        raise TGError(data.get("description", "error"), data.get("error_code", 0),
                      (data.get("parameters") or {}).get("retry_after", 0))
    return data["result"]


def _strip_style(markup):
    m = copy.deepcopy(markup)
    for row in m.get("inline_keyboard", []):
        for b in row:
            b.pop("style", None)
    return m


def _call_markup(method, **params):
    """Call a method that carries reply_markup; if Telegram rejects coloured buttons, retry plain."""
    try:
        return tg(method, **params)
    except TGError as e:
        if "message is not modified" in e.desc:
            return None
        if e.code == 400 and params.get("reply_markup"):
            low = e.desc.lower()
            if "style" in low or "button" in low or "unsupported" in low:
                params["reply_markup"] = _strip_style(params["reply_markup"])
                try:
                    return tg(method, **params)
                except TGError as e2:
                    if "message is not modified" in e2.desc:
                        return None
                    raise
        raise


def send(cid, text, markup=None):
    p = dict(chat_id=cid, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True})
    if markup:
        p["reply_markup"] = markup
    return _call_markup("sendMessage", **p)


def edit(cid, mid, text, markup=None):
    p = dict(chat_id=cid, message_id=mid, text=text, parse_mode="HTML",
             link_preview_options={"is_disabled": True})
    if markup:
        p["reply_markup"] = markup
    return _call_markup("editMessageText", **p)


def show(cid, mid, text, markup=None):
    """Edit in place when we have a message id, otherwise send a new message."""
    if mid:
        try:
            return edit(cid, mid, text, markup)
        except TGError:
            pass
    return send(cid, text, markup)


def B(text, cb=None, url=None, style=None, copy_text=None):
    b = {"text": text}
    if url:
        b["url"] = url
    elif copy_text:
        b["copy_text"] = {"text": copy_text}
    else:
        b["callback_data"] = cb or "noop"
    if style:
        b["style"] = style          # primary (blue) | success (green) | danger (red)
    return b


def KB(*rows):
    return {"inline_keyboard": [list(r) for r in rows if r]}


# ═══════════════════════════ FORCE JOIN ═══════════════════════════
_member_cache = {}          # (uid, chat_id) -> ts of last confirmed membership
_CACHE_TTL = 60


def _is_member(uid, chat_id):
    ck = (uid, chat_id)
    if time.time() - _member_cache.get(ck, 0) < _CACHE_TTL:
        return True
    try:
        m = tg("getChatMember", chat_id=chat_id, user_id=uid)
    except TGError as e:
        # Bot lost admin rights / channel deleted: don't lock every user out.
        print(f"[ForceJoin] cannot verify {chat_id}: {e.desc}", flush=True)
        return True
    st = m.get("status")
    ok = st in ("creator", "administrator", "member") or (st == "restricted" and m.get("is_member", False))
    if ok:
        _member_cache[ck] = time.time()
    return ok


def missing_channels(uid):
    if uid in ADMIN_IDS or not force_join_on():
        return []
    return [c for c in list_channels() if not _is_member(uid, c["chat_id"])]


_pending_link = {}          # uid -> link waiting for the user to finish joining


def join_prompt(cid, mid, miss):
    lines = "\n".join(f"  📢 <b>{E(c['title'] or 'Channel')}</b>" for c in miss)
    text = ("<b>🔒 JOIN REQUIRED</b>\n━━━━━━━━━━━━━━━━━━\n"
            "To use this bot you must join:\n\n"
            f"{lines}\n\n"
            "<blockquote>After joining, tap <b>✅ Verify</b> — your link will be processed automatically.</blockquote>"
            + FOOTER)
    rows, styles = [], ["primary", "success"]
    for i, c in enumerate(miss):
        if c["link"]:
            rows.append([B(f"📢 Join {c['title'] or 'Channel'}", url=c["link"], style=styles[i % 2])])
    rows.append([B("✅ Verify", cb="verify", style="success")])
    rows.append([B("💎 Buy Premium", url=CONTACT_URL, style="danger")])
    show(cid, mid, text, KB(*rows))


def gate(uid, cid, mid=None, pending=None):
    miss = missing_channels(uid)
    if not miss:
        return True
    if pending:
        _pending_link[uid] = pending
    join_prompt(cid, mid, miss)
    return False


# ═══════════════════════════ REFERRALS ═══════════════════════════
def try_count_referral(uid):
    """A referral only counts once the invited user has passed force-join."""
    row = get_user(uid)
    if not row or not row["referred_by"] or row["ref_counted"]:
        return
    if missing_channels(uid):
        return
    if not q("UPDATE users SET ref_counted=1 WHERE id=? AND ref_counted=0", (uid,)):
        return
    ref = row["referred_by"]
    q("UPDATE users SET ref_points=ref_points+1, total_refs=total_refs+1 WHERE id=?", (ref,))
    r = get_user(ref)
    if r:
        name = E(row["first_name"] or "Someone")
        try:
            send(ref, f"<b>🎉 NEW REFERRAL!</b>\n<blockquote>{name} joined using your link.\n"
                      f"🎁 Referral points: <b>{r['ref_points']}</b></blockquote>" + FOOTER,
                 KB([B("🎁 Refer & Earn", cb="refer", style="success")]))
        except TGError:
            pass


# ═══════════════════════════ SCREENS ═══════════════════════════
def fmt_exp(ts):
    left = ts - int(time.time())
    d, h = left // 86400, (left % 86400) // 3600
    return f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts))} UTC  ({d}d {h}h left)"


def menu_markup(uid):
    rows = [
        [B("🔗 How to Bypass", cb="howto", style="primary"), B("🎁 Refer & Earn", cb="refer", style="success")],
        [B("🔑 My API Key", cb="mykey", style="primary"), B("📖 API Guide", cb="docs", style="primary")],
        [B("💎 Buy Premium", url=CONTACT_URL, style="danger"), B("👨‍💻 Developer", url=DEV_URL, style="success")],
    ]
    if uid in ADMIN_IDS:
        rows.append([B("🛡️ Admin Panel", cb="adm:home", style="danger")])
    return KB(*rows)


def screen_menu(uid, name):
    text = (f"<b>⚡ EXODUS BYPASS BOT</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Hey <b>{E(name or 'there')}</b> 👋\n\n"
            "Send me any <b>short link</b> and I'll return the <b>main link</b> instantly.\n\n"
            "<blockquote>🔗 Paste a link → ⚡ get the real URL\n"
            "🎁 Refer friends → 🔑 free API key\n"
            f"💎 Need more? Message {E('@' + CONTACT_USERNAME)}</blockquote>" + FOOTER)
    return text, menu_markup(uid)


def screen_howto(uid):
    text = ("<b>🔗 HOW TO BYPASS</b>\n━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Copy a short link\n2️⃣ Paste it here\n3️⃣ Get the main link in seconds\n\n"
            "<blockquote>Join all required channels first, otherwise the bot will ask you to.</blockquote>" + FOOTER)
    return text, KB([B("🔙 Back", cb="menu", style="primary")])


def screen_refer(uid):
    u = get_user(uid)
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    pts = u["ref_points"]
    tiers = []
    for cost, days, label in PLANS:
        mark = "✅" if pts >= cost else "🔒"
        tiers.append(f"{mark} <b>{cost}</b> referrals → <b>{label}</b> API key")
    text = ("<b>🎁 REFER & EARN</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Invite friends — when they join the required channels you earn <b>1 point</b>.\n\n"
            f"🔗 <b>Your link</b>\n<code>{E(link)}</code>\n\n"
            f"👥 Total referrals: <b>{u['total_refs']}</b>\n"
            f"⭐ Available points: <b>{pts}</b>\n\n"
            "<b>🏆 Rewards</b>\n" + "\n".join(tiers) +
            "\n\n<blockquote>Claiming a reward spends the points. If you already have a key, "
            "the days are added to it.</blockquote>" + FOOTER)
    rows = []
    for i, (cost, days, label) in enumerate(PLANS):
        if pts >= cost:
            rows.append([B(f"🔑 Claim {label}  (−{cost} pts)", cb=f"claim:{i}", style="success")])
    share = ("https://t.me/share/url?url=" + requests.utils.quote(link, safe="") +
             "&text=" + requests.utils.quote("⚡ Bypass short links instantly!", safe=""))
    rows.append([B("📤 Share My Link", url=share, style="primary"), B("📋 Copy Link", copy_text=link, style="primary")])
    rows.append([B("🔙 Back", cb="menu", style="danger")])
    return text, KB(*rows)


def api_guide(key=None):
    k = key or "YOUR_API_KEY"
    base = BASE_URL
    get_url = f"{base}/bypass?key={k}&link=SHORT_LINK"
    curl = f'curl -H "X-API-Key: {k}" "{base}/bypass?link=SHORT_LINK"'
    py = ("import requests\n"
          f'r = requests.get("{base}/bypass",\n'
          f'                 params={{"key": "{k}", "link": "SHORT_LINK"}})\n'
          'print(r.json()["url"])')
    return ("<b>📖 API GUIDE</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>Main URL</b>\n<code>{E(base)}/bypass</code>\n\n"
            f"1️⃣ <b>Browser / GET</b>\n<pre>{E(get_url)}</pre>\n"
            f"2️⃣ <b>cURL (header)</b>\n<pre>{E(curl)}</pre>\n"
            f"3️⃣ <b>Python</b>\n<pre>{E(py)}</pre>\n"
            "📦 <b>Response</b>\n"
            '<pre>{"status": true, "url": "MAIN_LINK",\n "links": {"original": "...", "bypassed": "..."}}</pre>\n'
            "⚠️ <b>Errors</b>: <code>401</code> invalid/expired key • <code>429</code> daily limit"
            + FOOTER)


def screen_docs(uid):
    row = active_key(uid)
    return api_guide(row["key"] if row else None), KB(
        [B("🔑 My API Key", cb="mykey", style="primary"), B("🎁 Get Key", cb="refer", style="success")],
        [B("🔙 Back", cb="menu", style="danger")])


def screen_key(uid):
    row = active_key(uid)
    if not row:
        text = ("<b>🔑 MY API KEY</b>\n━━━━━━━━━━━━━━━━━━\n"
                "You don't have an active key yet.\n\n"
                "<blockquote>Refer <b>5</b> friends → 7 days\nRefer <b>10</b> friends → 15 days\n"
                "Refer <b>20</b> friends → 1 month</blockquote>" + FOOTER)
        return text, KB([B("🎁 Refer & Earn", cb="refer", style="success")],
                        [B("💎 Buy Premium", url=CONTACT_URL, style="danger")],
                        [B("🔙 Back", cb="menu", style="primary")])
    lim = row["daily_limit"] or daily_limit()
    text = ("<b>🔑 MY API KEY</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"<code>{row['key']}</code>\n\n"
            f"⏳ Expires: <b>{fmt_exp(row['expires_at'])}</b>\n"
            f"📊 Today: <b>{key_usage_today(row['key'])}/{lim}</b>\n\n"
            + api_guide(row["key"]).split("━━━━━━━━━━━━━━━━━━\n", 1)[1])
    return text, KB(
        [B("📋 Copy Key", copy_text=row["key"], style="success"), B("📖 Full Guide", cb="docs", style="primary")],
        [B("💎 Upgrade", url=CONTACT_URL, style="danger"), B("🔙 Back", cb="menu", style="primary")])


def key_card(key, exp, label=""):
    text = ("<b>🎉 API KEY READY!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"<code>{key}</code>\n\n"
            f"🏷 Plan: <b>{E(label or 'Custom')}</b>\n"
            f"⏳ Expires: <b>{fmt_exp(exp)}</b>\n\n"
            + api_guide(key).split("━━━━━━━━━━━━━━━━━━\n", 1)[1])
    return text, KB([B("📋 Copy Key", copy_text=key, style="success")],
                    [B("📖 API Guide", cb="docs", style="primary"), B("🔙 Menu", cb="menu", style="danger")])


# ═══════════════════════════ BYPASS FLOW ═══════════════════════════
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_inflight = set()
_inflight_lock = threading.Lock()


def call_bypass(link):
    try:
        r = requests.get(f"http://127.0.0.1:{LOCAL_PORT}/bypass", params={"link": link},
                         headers={"X-Internal-Token": INTERNAL_TOKEN}, timeout=150)
        return r.json(), None
    except Exception as e:
        return None, f"API unreachable ({type(e).__name__})"


def handle_link(uid, cid, link):
    if not gate(uid, cid, pending=link):
        return
    with _inflight_lock:
        if uid in _inflight:
            send(cid, "⏳ <b>Please wait</b> — your previous link is still processing." + FOOTER)
            return
        _inflight.add(uid)
    try:
        wait = send(cid, "<b>⏳ BYPASSING…</b>\n<blockquote>Hang tight, resolving your link ⚡</blockquote>" + FOOTER)
        mid = wait["message_id"] if wait else None
        data, err = call_bypass(link)
        buy_row = [B("💎 Buy Premium", url=CONTACT_URL, style="danger"), B("👨‍💻 Developer", url=DEV_URL, style="primary")]
        ok = bool(data) and data.get("status") is True
        if not ok:
            reason = err or (data or {}).get("message") or "All bypass bots failed"
            text = (f"<b>❌ BYPASS FAILED</b>\n━━━━━━━━━━━━━━━━━━\n<blockquote>{E(str(reason))}</blockquote>\n"
                    "Try again in a moment or send a different link." + FOOTER)
            show(cid, mid, text, KB(buy_row))
            return
        links = data.get("links") or {}
        main = data.get("url") or links.get("bypassed") or ""
        extras = []
        labels = {"instant_dl": "⚡ Instant DL", "telegram": "✈️ Telegram", "direct": "📥 Direct"}
        for k2, lab in labels.items():
            if links.get(k2):
                extras.append(f"{lab}\n<code>{E(str(links[k2]))}</code>")
        f = data.get("file") or {}
        fileline = ""
        if f.get("name") or f.get("size"):
            fileline = f"\n📁 <b>{E(str(f.get('name', '')))}</b> {E(str(f.get('size', '')))}"
        text = ("<b>✅ BYPASS SUCCESSFUL</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🔗 <b>Original</b>\n<code>{E(link)}</code>\n\n"
                f"🎯 <b>Main Link</b>\n<code>{E(main)}</code>{fileline}\n"
                + ("\n" + "\n\n".join(extras) + "\n" if extras else "") +
                f"\n⚡ <i>Resolved in {E(str(data.get('response_ms', '—')))}</i>" + FOOTER)
        rows = []
        if main.startswith(("http://", "https://")) and len(main) < 2000:
            r1 = [B("🌐 Open Link", url=main, style="success")]
            if len(main) <= 256:
                r1.append(B("📋 Copy", copy_text=main, style="primary"))
            rows.append(r1)
        rows.append(buy_row)
        show(cid, mid, text, KB(*rows))
        q("UPDATE users SET bypass_count=bypass_count+1 WHERE id=?", (uid,))
    finally:
        with _inflight_lock:
            _inflight.discard(uid)


# ═══════════════════════════ ADMIN PANEL ═══════════════════════════
admin_state = {}            # admin uid -> {"mode": str, ...}


def _stats():
    now = int(time.time())
    one = lambda s, a=(): q(s, a, one=True)[0]
    day0 = now - 86400
    return {
        "users": one("SELECT COUNT(*) FROM users"),
        "new24": one("SELECT COUNT(*) FROM users WHERE joined_at>?", (day0,)),
        "banned": one("SELECT COUNT(*) FROM users WHERE banned=1"),
        "keys": one("SELECT COUNT(*) FROM keys WHERE revoked=0 AND expires_at>?", (now,)),
        "chans": one("SELECT COUNT(*) FROM channels"),
        "bypass": one("SELECT COALESCE(SUM(bypass_count),0) FROM users"),
        "refs": one("SELECT COALESCE(SUM(total_refs),0) FROM users"),
        "api_today": one("SELECT COALESCE(SUM(count),0) FROM usage WHERE day=?", (_today(),)),
    }


def adm_home():
    s = _stats()
    text = ("<b>🛡️ ADMIN CONTROL PANEL</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 Users: <b>{s['users']}</b>   🆕 24h: <b>{s['new24']}</b>\n"
            f"🔑 Active keys: <b>{s['keys']}</b>   📢 Channels: <b>{s['chans']}</b>\n"
            f"⚡ Bypasses: <b>{s['bypass']}</b>   🚫 Banned: <b>{s['banned']}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<i>Force-join: {'ON ✅' if force_join_on() else 'OFF ❌'}</i>" + FOOTER)
    web = BASE_URL + "/admin"
    return text, KB(
        [B("📊 Statistics", cb="adm:stats", style="primary"), B("👥 Users", cb="adm:users", style="primary")],
        [B("📢 Channels", cb="adm:ch", style="success"), B("📣 Broadcast", cb="adm:bc", style="success")],
        [B("🔑 API Keys", cb="adm:keys", style="primary"), B("⚙️ Settings", cb="adm:set", style="primary")],
        [B("🌐 Web Panel", url=web, style="success"), B("💎 Contact", url=CONTACT_URL, style="danger")],
        [B("🔙 Close", cb="menu", style="danger")])


def adm_stats():
    s = _stats()
    text = ("<b>📊 STATISTICS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total users: <b>{s['users']}</b>\n🆕 Joined (24h): <b>{s['new24']}</b>\n"
            f"🚫 Banned: <b>{s['banned']}</b>\n⚡ Total bypasses: <b>{s['bypass']}</b>\n"
            f"🎁 Total referrals: <b>{s['refs']}</b>\n🔑 Active keys: <b>{s['keys']}</b>\n"
            f"🌐 API requests today: <b>{s['api_today']}</b>\n📢 Channels: <b>{s['chans']}</b>")
    return text, KB([B("🔄 Refresh", cb="adm:stats", style="success"), B("🔙 Back", cb="adm:home", style="danger")])


def adm_channels():
    chans = list_channels()
    lines = [f"{i}. <b>{E(c['title'] or c['chat_id'])}</b>  <code>{E(c['chat_id'])}</code>" for i, c in enumerate(chans, 1)]
    text = ("<b>📢 FORCE-JOIN CHANNELS</b>\n━━━━━━━━━━━━━━━━━━\n" +
            ("\n".join(lines) if lines else "<i>No channels yet.</i>") +
            "\n\n<blockquote>The bot must be an <b>admin</b> in every channel. "
            "New channels apply to all users immediately.</blockquote>")
    rows = [[B(f"❌ Remove {c['title'] or c['chat_id']}"[:60], cb=f"adm:chdel:{c['id']}", style="danger")] for c in chans]
    rows.append([B("➕ Add Channel", cb="adm:chadd", style="success")])
    rows.append([B("🔙 Back", cb="adm:home", style="primary")])
    return text, KB(*rows)


def adm_add_channel(text):
    parts = text.split()
    if not parts:
        raise ValueError("Send a channel @username or numeric ID.")
    ref, link_in = parts[0], (parts[1] if len(parts) > 1 else None)
    m = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})/?$", ref)
    if m:
        ref = "@" + m.group(1)
    if re.fullmatch(r"-?\d+", ref):
        chat_ref = int(ref)
    elif ref.startswith("@"):
        chat_ref = ref
    else:
        raise ValueError("Use <code>@username</code> or <code>-100123…</code> (optionally followed by an invite link).")
    try:
        chat = tg("getChat", chat_id=chat_ref)
    except TGError as e:
        raise ValueError(f"Cannot access that chat: {E(e.desc)}")
    try:
        me = tg("getChatMember", chat_id=chat["id"], user_id=BOT_ID)
    except TGError as e:
        raise ValueError(f"Bot is not in that chat: {E(e.desc)}")
    if me.get("status") not in ("administrator", "creator"):
        raise ValueError("Make the bot an <b>admin</b> of that channel first, then add it again.")
    if chat.get("username"):
        link = f"https://t.me/{chat['username']}"
    elif link_in and link_in.startswith("http"):
        link = link_in
    else:
        try:
            link = tg("createChatInviteLink", chat_id=chat["id"], name="Exodus Bot")["invite_link"]
        except TGError:
            raise ValueError("Private channel: send <code>-100ID https://t.me/+invite</code> together.")
    title = chat.get("title") or chat.get("username") or str(chat["id"])
    n = q("INSERT OR IGNORE INTO channels(chat_id,title,link,added_at) VALUES(?,?,?,?)",
          (str(chat["id"]), title, link, int(time.time())))
    if not n:
        raise ValueError("That channel is already added.")
    return q("SELECT * FROM channels WHERE chat_id=?", (str(chat["id"]),), one=True)


def adm_users():
    text = ("<b>👥 USER MANAGER</b>\n━━━━━━━━━━━━━━━━━━\nFind a user by ID to ban, unban or give a key.")
    return text, KB([B("🔍 Find User", cb="adm:ufind", style="success")],
                    [B("🔙 Back", cb="adm:home", style="primary")])


def adm_user_card(uid):
    u = get_user(uid)
    if not u:
        return "<b>❌ User not found.</b>", KB([B("🔙 Back", cb="adm:users", style="primary")])
    k = active_key(uid)
    text = ("<b>👤 USER</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <code>{uid}</code>\n📛 {E(u['first_name'] or '-')}  "
            f"{('@' + E(u['username'])) if u['username'] else ''}\n"
            f"📅 Joined: {time.strftime('%Y-%m-%d', time.gmtime(u['joined_at']))}\n"
            f"🎁 Refs: <b>{u['total_refs']}</b> • points <b>{u['ref_points']}</b>\n"
            f"⚡ Bypasses: <b>{u['bypass_count']}</b>\n"
            f"🚫 Banned: <b>{'Yes' if u['banned'] else 'No'}</b>\n"
            f"🔑 Key: {('<code>' + k['key'] + '</code> — ' + fmt_exp(k['expires_at'])) if k else '<i>none</i>'}")
    ban_btn = (B("✅ Unban", cb=f"adm:uunban:{uid}", style="success") if u["banned"]
               else B("🚫 Ban", cb=f"adm:uban:{uid}", style="danger"))
    return text, KB(
        [ban_btn, B("🗑 Revoke Key", cb=f"adm:urev:{uid}", style="danger")],
        [B("🔑 +7d", cb=f"adm:ukey:{uid}:7", style="primary"), B("🔑 +15d", cb=f"adm:ukey:{uid}:15", style="primary"),
         B("🔑 +30d", cb=f"adm:ukey:{uid}:30", style="primary")],
        [B("🔙 Back", cb="adm:users", style="success")])


def adm_keys():
    rows = q("SELECT * FROM keys ORDER BY created_at DESC LIMIT 10", many=True)
    now = int(time.time())
    lines = []
    for r in rows:
        st = "🚫" if r["revoked"] else ("✅" if r["expires_at"] > now else "⌛")
        lines.append(f"{st} <code>{r['key']}</code> • <code>{r['user_id']}</code>")
    text = ("<b>🔑 API KEYS</b>\n━━━━━━━━━━━━━━━━━━\n" +
            ("\n".join(lines) if lines else "<i>No keys yet.</i>") +
            "\n\n<i>Showing the 10 most recent.</i>")
    return text, KB(
        [B("➕ Generate Key", cb="adm:kgen", style="success"), B("🗑 Revoke Key", cb="adm:krev", style="danger")],
        [B("🔙 Back", cb="adm:home", style="primary")])


def adm_settings():
    text = ("<b>⚙️ SETTINGS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🔒 Force-join: <b>{'ON ✅' if force_join_on() else 'OFF ❌'}</b>\n"
            f"📊 Daily API limit / key: <b>{daily_limit()}</b>\n"
            f"💬 Contact: <b>@{E(CONTACT_USERNAME)}</b>\n"
            f"🌐 Base URL: <code>{E(BASE_URL)}</code>\n\n"
            "<b>🏆 Referral plans</b>\n" +
            "\n".join(f"• {c} refs → {l}" for c, d, l in PLANS))
    return text, KB(
        [B("🔒 Toggle Force-Join", cb="adm:settog", style="primary"), B("📊 Set Daily Limit", cb="adm:setlim", style="success")],
        [B("🔙 Back", cb="adm:home", style="danger")])


def _broadcast(admin_cid, text):
    users = q("SELECT id FROM users WHERE banned=0", many=True)
    ok = bad = 0
    for r in users:
        for _ in range(2):
            try:
                send(r["id"], text + FOOTER)
                ok += 1
                break
            except TGError as e:
                if e.retry_after:
                    time.sleep(e.retry_after + 1)
                    continue
                bad += 1
                break
        time.sleep(0.05)
    send(admin_cid, f"<b>📣 BROADCAST DONE</b>\n✅ Sent: <b>{ok}</b>\n❌ Failed: <b>{bad}</b>",
         KB([B("🔙 Admin Panel", cb="adm:home", style="primary")]))


def on_admin_callback(uid, cid, mid, data, ack):
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else "home"
    prev_state = admin_state.pop(uid, None)

    if act == "home":
        return show(cid, mid, *adm_home())
    if act == "stats":
        return show(cid, mid, *adm_stats())
    if act == "ch":
        return show(cid, mid, *adm_channels())
    if act == "chadd":
        admin_state[uid] = {"mode": "add_channel"}
        return show(cid, mid, "<b>➕ ADD CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\nSend the channel <code>@username</code> or "
                    "<code>-100…</code> ID.\nPrivate channel? Send <code>-100ID https://t.me/+invite</code>.\n\n"
                    "<i>The bot must already be an admin there.</i>",
                    KB([B("❌ Cancel", cb="adm:ch", style="danger")]))
    if act == "chdel":
        q("DELETE FROM channels WHERE id=?", (int(parts[2]),))
        ack("Channel removed")
        return show(cid, mid, *adm_channels())
    if act == "notify":
        c = q("SELECT * FROM channels WHERE id=?", (int(parts[2]),), one=True)
        if not c:
            return ack("Channel not found", True)
        ack("Notifying users…")
        txt = (f"<b>📢 NEW CHANNEL ADDED</b>\n<blockquote>Join <b>{E(c['title'] or 'our channel')}</b> to keep "
               "using the bot.</blockquote>")
        threading.Thread(target=_broadcast_with_button, args=(cid, txt, c), daemon=True).start()
        return None
    if act == "bc":
        admin_state[uid] = {"mode": "broadcast"}
        return show(cid, mid, "<b>📣 BROADCAST</b>\n━━━━━━━━━━━━━━━━━━\nSend the message to broadcast "
                    "(HTML allowed: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;a href&gt;</code>).",
                    KB([B("❌ Cancel", cb="adm:home", style="danger")]))
    if act == "bcgo":
        txt = (prev_state or {}).get("text")
        if not txt:
            return ack("Nothing to send", True)
        ack("Broadcast started")
        threading.Thread(target=_broadcast, args=(cid, txt), daemon=True).start()
        return show(cid, mid, "<b>📣 Broadcasting…</b> you'll get a report when finished.")
    if act == "users":
        return show(cid, mid, *adm_users())
    if act == "ufind":
        admin_state[uid] = {"mode": "find_user"}
        return show(cid, mid, "<b>🔍 FIND USER</b>\nSend the user's numeric Telegram ID.",
                    KB([B("❌ Cancel", cb="adm:users", style="danger")]))
    if act in ("uban", "uunban"):
        t = int(parts[2])
        q("UPDATE users SET banned=? WHERE id=?", (1 if act == "uban" else 0, t))
        ack("Done")
        return show(cid, mid, *adm_user_card(t))
    if act == "urev":
        t = int(parts[2])
        q("UPDATE keys SET revoked=1 WHERE user_id=?", (t,))
        ack("Keys revoked")
        return show(cid, mid, *adm_user_card(t))
    if act == "ukey":
        t, days = int(parts[2]), int(parts[3])
        grant_key(t, days, f"Admin {days}d")
        ack("Key granted")
        return show(cid, mid, *adm_user_card(t))
    if act == "keys":
        return show(cid, mid, *adm_keys())
    if act == "kgen":
        admin_state[uid] = {"mode": "gen_key"}
        return show(cid, mid, "<b>➕ GENERATE KEY</b>\nSend: <code>USER_ID DAYS</code>  (e.g. <code>123456789 30</code>)",
                    KB([B("❌ Cancel", cb="adm:keys", style="danger")]))
    if act == "krev":
        admin_state[uid] = {"mode": "revoke_key"}
        return show(cid, mid, "<b>🗑 REVOKE KEY</b>\nSend the full key to revoke.",
                    KB([B("❌ Cancel", cb="adm:keys", style="danger")]))
    if act == "set":
        return show(cid, mid, *adm_settings())
    if act == "settog":
        set_setting("force_join", "0" if force_join_on() else "1")
        return show(cid, mid, *adm_settings())
    if act == "setlim":
        admin_state[uid] = {"mode": "set_limit"}
        return show(cid, mid, "<b>📊 DAILY LIMIT</b>\nSend a number (requests per key per day).",
                    KB([B("❌ Cancel", cb="adm:set", style="danger")]))
    return ack()


def _broadcast_with_button(admin_cid, text, chan):
    users = q("SELECT id FROM users WHERE banned=0", many=True)
    kb = KB([B(f"📢 Join {chan['title'] or 'Channel'}", url=chan["link"], style="primary")],
            [B("✅ Verify", cb="verify", style="success")])
    ok = bad = 0
    for r in users:
        try:
            send(r["id"], text + FOOTER, kb)
            ok += 1
        except TGError as e:
            if e.retry_after:
                time.sleep(e.retry_after + 1)
            bad += 1
        time.sleep(0.05)
    send(admin_cid, f"<b>📣 NOTIFY DONE</b>\n✅ Sent: <b>{ok}</b>\n❌ Failed: <b>{bad}</b>",
         KB([B("🔙 Admin Panel", cb="adm:home", style="primary")]))


def on_admin_input(uid, cid, text):
    st = admin_state.get(uid) or {}
    mode = st.get("mode")
    back = lambda cb: KB([B("🔙 Back", cb=cb, style="primary")])
    try:
        if mode == "add_channel":
            c = adm_add_channel(text)
            admin_state.pop(uid, None)
            send(cid, f"<b>✅ CHANNEL ADDED</b>\n<b>{E(c['title'])}</b>\n\n"
                      "Existing users will be asked to join the next time they use the bot.",
                 KB([B("📣 Notify All Users", cb=f"adm:notify:{c['id']}", style="success")],
                    [B("🔙 Channels", cb="adm:ch", style="primary")]))
        elif mode == "broadcast":
            try:
                send(cid, "<b>👀 PREVIEW</b>\n\n" + text + FOOTER)
            except TGError as e:
                send(cid, f"❌ Invalid HTML: {E(e.desc)}\nSend the message again.")
                return
            st["text"] = text
            send(cid, "Send this to <b>all users</b>?",
                 KB([B("✅ Send Now", cb="adm:bcgo", style="success"), B("❌ Cancel", cb="adm:home", style="danger")]))
            st["mode"] = "confirm"
        elif mode == "find_user":
            if not text.strip().isdigit():
                return send(cid, "❌ Send a numeric user ID.")
            admin_state.pop(uid, None)
            send(cid, *adm_user_card(int(text.strip())))
        elif mode == "gen_key":
            p = text.split()
            if len(p) != 2 or not (p[0].isdigit() and p[1].isdigit()) or not (1 <= int(p[1]) <= 3650):
                return send(cid, "❌ Format: <code>USER_ID DAYS</code>")
            if not get_user(int(p[0])):
                return send(cid, "❌ That user has never started the bot.")
            key, exp = grant_key(int(p[0]), int(p[1]), f"Admin {p[1]}d")
            admin_state.pop(uid, None)
            send(cid, f"<b>✅ KEY READY</b>\n<code>{key}</code>\n⏳ {fmt_exp(exp)}", back("adm:keys"))
            try:
                kt, km = key_card(key, exp, f"{p[1]} Days")
                send(int(p[0]), kt, km)
            except TGError:
                pass
        elif mode == "revoke_key":
            n = q("UPDATE keys SET revoked=1 WHERE key=?", (text.strip(),))
            admin_state.pop(uid, None)
            send(cid, "✅ Key revoked." if n else "❌ Key not found.", back("adm:keys"))
        elif mode == "set_limit":
            if not text.strip().isdigit() or int(text) < 1:
                return send(cid, "❌ Send a positive number.")
            set_setting("daily_limit", int(text))
            admin_state.pop(uid, None)
            send(cid, f"✅ Daily limit set to <b>{int(text)}</b>.", back("adm:set"))
    except ValueError as e:
        send(cid, f"❌ {e}", KB([B("🔙 Cancel", cb="adm:ch" if mode == "add_channel" else "adm:home", style="danger")]))


# ═══════════════════════════ UPDATE ROUTER ═══════════════════════════
def on_message(m):
    chat = m.get("chat") or {}
    if chat.get("type") != "private" or not m.get("from"):
        return
    frm, cid = m["from"], chat["id"]
    uid = frm["id"]
    text = (m.get("text") or "").strip()
    if not text:
        return

    ref = None
    if text.startswith("/start"):
        p = text.split(maxsplit=1)
        if len(p) > 1 and p[1].startswith("ref_") and p[1][4:].isdigit():
            ref = int(p[1][4:])
    row, _new = upsert_user(frm, ref)
    if row["banned"] and uid not in ADMIN_IDS:
        return send(cid, "🚫 <b>You are banned from using this bot.</b>\n"
                         f"Contact {E('@' + CONTACT_USERNAME)}." + FOOTER)

    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd == "/cancel":
            admin_state.pop(uid, None)
            return send(cid, "✅ Cancelled.")
        if cmd == "/admin":
            if uid not in ADMIN_IDS:
                return
            admin_state.pop(uid, None)
            return send(cid, *adm_home())
        if cmd == "/buy":
            return send(cid, "<b>💎 BUY PREMIUM</b>\n━━━━━━━━━━━━━━━━━━\nHigher limits, instant keys or custom plans?\n"
                        f"📩 Message <b>{E('@' + CONTACT_USERNAME)}</b> to buy." + FOOTER,
                        KB([B("💬 Message to Buy", url=CONTACT_URL, style="success")]))
        if not gate(uid, cid):
            return
        if cmd in ("/start", "/menu", "/help"):
            try_count_referral(uid)
            return send(cid, *screen_menu(uid, frm.get("first_name")))
        if cmd == "/refer":
            return send(cid, *screen_refer(uid))
        if cmd in ("/key", "/mykey"):
            return send(cid, *screen_key(uid))
        if cmd in ("/api", "/docs"):
            return send(cid, *screen_docs(uid))
        return send(cid, *screen_menu(uid, frm.get("first_name")))

    if uid in ADMIN_IDS and uid in admin_state:
        return on_admin_input(uid, cid, text)

    mt = URL_RE.search(text)
    if mt:
        return handle_link(uid, cid, mt.group(0).rstrip(").,]"))
    if not gate(uid, cid):
        return
    send(cid, "<b>🔗 Send me a short link</b> to bypass.\n<i>Example:</i> <code>https://example.com/abc</code>" + FOOTER,
         menu_markup(uid))


def on_callback(cq):
    uid = cq["from"]["id"]
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    cid = (msg.get("chat") or {}).get("id")
    mid = msg.get("message_id")
    done = {"v": False}

    def ack(text=None, alert=False):
        if done["v"]:
            return
        done["v"] = True
        p = {"callback_query_id": cq["id"]}
        if text:
            p.update(text=text, show_alert=alert)
        try:
            tg("answerCallbackQuery", **p)
        except TGError:
            pass

    if not cid:
        return ack()
    row, _ = upsert_user(cq["from"])
    if row["banned"] and uid not in ADMIN_IDS:
        return ack("🚫 You are banned.", True)

    if data.startswith("adm:"):
        if uid not in ADMIN_IDS:
            return ack("Admins only", True)
        r = on_admin_callback(uid, cid, mid, data, ack)
        return ack()

    if data == "verify":
        _member_cache.clear()
        miss = missing_channels(uid)
        if miss:
            ack("❌ You haven't joined all channels yet!", True)
            return join_prompt(cid, mid, miss)
        ack("✅ Verified!")
        try_count_referral(uid)
        link = _pending_link.pop(uid, None)
        if link:
            show(cid, mid, "<b>✅ VERIFIED</b>\nProcessing your link…" + FOOTER)
            return handle_link(uid, cid, link)
        return show(cid, mid, *screen_menu(uid, cq["from"].get("first_name")))

    if not data.startswith("claim:"):
        ack()
    if data == "noop":
        return ack()
    if not gate(uid, cid, mid):
        return ack()
    if data == "menu":
        return show(cid, mid, *screen_menu(uid, cq["from"].get("first_name")))
    if data == "howto":
        return show(cid, mid, *screen_howto(uid))
    if data == "refer":
        return show(cid, mid, *screen_refer(uid))
    if data == "mykey":
        return show(cid, mid, *screen_key(uid))
    if data == "docs":
        return show(cid, mid, *screen_docs(uid))
    if data.startswith("claim:"):
        try:
            idx = int(data.split(":")[1])
            if not 0 <= idx < len(PLANS):
                return ack()
            res = claim_plan(uid, idx)
        except (ValueError, IndexError):
            return ack()
        if not res:
            ack("Not enough referral points", True)
            return show(cid, mid, *screen_refer(uid))
        ack("🎉 Key unlocked!")
        key, exp = res
        return show(cid, mid, *key_card(key, exp, PLANS[idx][2]))


def _safe(fn, obj):
    try:
        fn(obj)
    except Exception as e:                       # never let one update kill the bot
        print(f"[Bot] handler error: {type(e).__name__}: {e}", flush=True)


def _poll_loop():
    pool = ThreadPoolExecutor(max_workers=24, thread_name_prefix="tgbot")
    offset = None
    try:
        tg("deleteWebhook", drop_pending_updates=False)
    except TGError:
        pass
    print(f"[Bot] @{BOT_USERNAME} polling…", flush=True)
    while True:
        try:
            params = dict(timeout=50, allowed_updates=["message", "callback_query"])
            if offset is not None:
                params["offset"] = offset
            for u in tg("getUpdates", _t=70, **params):
                offset = u["update_id"] + 1
                if "message" in u:
                    pool.submit(_safe, on_message, u["message"])
                elif "callback_query" in u:
                    pool.submit(_safe, on_callback, u["callback_query"])
        except TGError as e:
            print(f"[Bot] poll error: {e.desc}", flush=True)
            time.sleep(max(3, e.retry_after))
        except Exception as e:
            print(f"[Bot] poll crash: {e}", flush=True)
            time.sleep(5)


def start_bot(port):
    """Called from main.py. Safe to call without a token (bot simply stays off)."""
    global BOT_TOKEN, BOT_ID, BOT_USERNAME, BASE_URL, LOCAL_PORT, ADMIN_IDS
    LOCAL_PORT = port
    BASE_URL = os.environ.get("BASE_URL", f"http://localhost:{port}").strip().rstrip("/")
    ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_IDS", ""))}
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
    if not BOT_TOKEN:
        print("⚠️  BOT_TOKEN not set — Telegram bot disabled (API stays up).", flush=True)
        return False
    try:
        me = tg("getMe")
    except TGError as e:
        print(f"⚠️  Bot token rejected: {e.desc}", flush=True)
        return False
    BOT_ID, BOT_USERNAME = me["id"], me.get("username", "")
    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS empty — nobody can open the bot admin panel.", flush=True)
    threading.Thread(target=_poll_loop, daemon=True, name="tg-poll").start()
    return True


# ═══════════════════════════ API KEY GATE (Flask) ═══════════════════════════
def install_api_gate(app, developer, is_admin=None):
    """Protect /bypass and /bypass/async with API keys. Bot + web-admin bypass the gate."""
    from flask import request, jsonify

    @app.before_request
    def _api_key_gate():
        if not _require_key_default():
            return None
        if request.path.rstrip("/") not in ("/bypass", "/bypass/async"):
            return None
        if request.headers.get("X-Internal-Token") == INTERNAL_TOKEN:
            return None
        if is_admin and is_admin():
            return None
        key = (request.headers.get("X-API-Key") or request.args.get("key")
               or request.args.get("api_key") or "")
        if not key and request.is_json:
            body = request.get_json(silent=True) or {}
            key = body.get("key") or body.get("api_key") or ""
        ok, code, msg = validate_and_count(str(key).strip())
        if ok:
            return None
        return jsonify({
            "status": False, "developer": developer, "message": msg,
            "get_key": f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else None,
            "buy": CONTACT_URL,
        }), code
