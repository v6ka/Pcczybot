# language: Python 3.10+, file: scraper.py
# Mobile-API based Instagram scraper + Telegram bot. Brand @vampslol.

from __future__ import annotations
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
import httpx

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.filters import CommandStart, Command
    from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram.exceptions import TelegramBadRequest
except ImportError:
    print("pip install aiogram", file=sys.stderr)
    sys.exit(1)

# ================= BRANDING =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7762493835:AAEQAYd4YB5RhLuW2iwn8TkjAel7myFSepY").strip()
BRAND = "pcczy"
INSTA_URL = f"https://instagram.com/{BRAND}"
TG_DEV = "@vampslol"
TG_DEV_URL = "https://t.me/vampslol"
TG_CHANNEL = "@Shelikecarti"
TG_CHANNEL_URL = "https://t.me/Shelikecarti"

# ================= CONFIG =================
GLOBAL_RPS = 5.0
CLIENT_TIMEOUT = 15.0
IG_APP_ID = "567067343352427"
IG_USER_AGENT = "Instagram 269.0.0.18.75 Android (33/13; 420dpi; 1080x2400; samsung; SM-G991B; o1s; exynos2100; en_US; 458229237)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("pcczy")

@dataclass
class Profile:
    username: str
    user_id: Optional[str] = None
    full_name: Optional[str] = None
    bio: Optional[str] = None
    followers: Optional[int] = None
    following: Optional[int] = None
    posts_count: Optional[int] = None
    profile_pic_url: Optional[str] = None
    external_url: Optional[str] = None
    is_verified: bool = False
    is_private: bool = False
    is_business: bool = False
    category: Optional[str] = None
    source: Optional[str] = None
    error: Optional[str] = None
    error_detail: Optional[str] = None

class TokenBucket:
    def __init__(self, rps):
        self.rps = rps
        self.capacity = max(1.0, rps)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self):
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rps)
            self.last = now
            if self.tokens < 1.0:
                await asyncio.sleep((1.0 - self.tokens) / self.rps)
                self.tokens = 1.0
            self.tokens -= 1.0

BUCKET = TokenBucket(GLOBAL_RPS)

# ================= FETCH LOGIC =================
async def _fetch_mobile_profile(username: str) -> Profile:
    """Hits the i.instagram.com mobile endpoint which bypasses the web login wall."""
    await BUCKET.take()
    
    headers = {
        "User-Agent": IG_USER_AGENT,
        "x-ig-app-id": IG_APP_ID,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    url = f"https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"
    
    async with httpx.AsyncClient(http2=True, timeout=CLIENT_TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=headers)
        except httpx.TransportError as e:
            return Profile(username=username, error="Network error", error_detail=f"transport: {type(e).__name__}")
            
        if r.status_code == 404:
            return Profile(username=username, error="User not found")
        if r.status_code == 429:
            return Profile(username=username, error="Rate limited", error_detail="Instagram is blocking requests, try again later.")
        if r.status_code == 401 or r.status_code == 403:
            return Profile(username=username, error="Blocked by Instagram", error_detail=f"HTTP {r.status_code} - API requires auth or IP is flagged.")
        if r.status_code != 200:
            return Profile(username=username, error=f"HTTP {r.status_code}", error_detail="Unknown API response.")
            
        try:
            data = r.json()
        except Exception:
            return Profile(username=username, error="Parse error", error_detail="Response was not valid JSON (login wall hit).")
            
        user = (data.get("data") or {}).get("user")
        if not user:
            return Profile(username=username, error="User data missing", error_detail="API returned empty user object.")
            
        p = Profile(username=username, source="mobile_api")
        p.user_id = user.get("id")
        p.full_name = user.get("full_name")
        p.bio = user.get("biography")
        p.external_url = user.get("external_url")
        p.profile_pic_url = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
        p.is_verified = bool(user.get("is_verified"))
        p.is_private = bool(user.get("is_private"))
        p.is_business = bool(user.get("is_business_account"))
        p.category = user.get("category_name") or user.get("business_category_name")
        
        f = user.get("edge_followed_by") or {}
        p.followers = f.get("count") if isinstance(f, dict) else None
        
        f = user.get("edge_follow") or {}
        p.following = f.get("count") if isinstance(f, dict) else None
        
        media = user.get("edge_owner_to_timeline_media") or {}
        p.posts_count = media.get("count") if isinstance(media, dict) else None
        
        return p

async def scrape_one(username: str) -> Profile:
    username = username.strip().lstrip("@").lower()
    await asyncio.sleep(random.uniform(0.1, 0.3)) # slight jitter
    return await _fetch_mobile_profile(username)

# ================= TELEGRAM UI =================
if not BOT_TOKEN:
    print("BOT_TOKEN empty", file=sys.stderr)
    sys.exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def _clean_username(raw):
    if not raw:
        return None
    s = raw.strip().lstrip("@")
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", s)
    if m:
        s = m.group(1)
    s = s.split("?")[0].split("/")[0].strip().rstrip(".")
    return s if re.match(r"^[A-Za-z0-9._]{1,30}$", s) else None

def _fmt_int(n):
    if n is None:
        return "—"
    if n >= 1000000:
        return f"{n/1000000:.1f}M".replace(".0M","M")
    if n >= 1000:
        return f"{n/1000:.1f}K".replace(".0K","K")
    return str(n)

def _safe(text, limit=400):
    if not text:
        return "—"
    t = text.strip()
    if len(t) > limit:
        t = t[:limit-1] + "…"
    return html.escape(t, quote=False)

def _profile_caption(p: Profile):
    if p.error:
        detail = f"\n<i>{html.escape(p.error_detail)}</i>" if p.error_detail else ""
        return f"❌ <b>Lookup Failed</b>\n\n<b>User:</b> @{html.escape(p.username)}\n<b>Reason:</b> {html.escape(p.error)}{detail}"

    name_line = _safe(p.full_name, 80) if p.full_name and p.full_name.lower() != p.username.lower() else f"@{html.escape(p.username)}"
    
    badges = []
    if p.is_verified:
        badges.append("✅ Verified")
    badges.append("🔒 Private" if p.is_private else "🌐 Public")
    if p.category:
        badges.append(f"🏷️ {_safe(p.category, 40)}")
    badge_str = "  ·  ".join(badges)

    lines = [
        f"<b>{name_line}</b>",
        f"<a href=\"https://instagram.com/{html.escape(p.username)}\">@{html.escape(p.username)}</a>",
        "",
        badge_str,
        "",
        f"👥 <b>Followers:</b> {_fmt_int(p.followers)}",
        f"➡️ <b>Following:</b> {_fmt_int(p.following)}",
        f"🖼️ <b>Posts:</b> {_fmt_int(p.posts_count)}",
        "",
        f"📝 <i>{_safe(p.bio, 320)}</i>",
    ]
    if p.external_url:
        safe_url = html.escape(p.external_url, quote=True)
        ext_name = html.escape(re.sub(r"^https?://", "", p.external_url)[:48], quote=False)
        lines.append(f"\n🔗 <a href=\"{safe_url}\">{ext_name}</a>")
    return "\n".join(lines)

def _profile_kb(p: Profile):
    rows = []
    if not p.error:
        rows.append([InlineKeyboardButton(text="📸 Open on Instagram", url=f"https://instagram.com/{p.username}")])
        
    rows.append([
        InlineKeyboardButton(text=f"Insta · {BRAND}", url=INSTA_URL),
        InlineKeyboardButton(text=f"Dev · {TG_DEV}", url=TG_DEV_URL),
    ])
    rows.append([InlineKeyboardButton(text=f"Channel · {TG_CHANNEL}", url=TG_CHANNEL_URL)])
    
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _send_profile(target: Message, p: Profile):
    caption = _profile_caption(p)
    kb = _profile_kb(p)
    
    try:
        if p.profile_pic_url and not p.error:
            try:
                await target.answer_photo(photo=p.profile_pic_url, caption=caption[:1024], reply_markup=kb)
                if len(caption) > 1024:
                    await target.answer(caption[1024:], disable_web_page_preview=True)
                return
            except TelegramBadRequest as e:
                log.warning("photo failed: %s", e)
                
        await target.answer(caption, reply_markup=kb, disable_web_page_preview=True)
        
    except TelegramBadRequest as e:
        log.warning("html failed: %s", e)
        try:
            plain = re.sub(r"<[^>]+>", "", caption)
            await target.answer(plain[:4000], reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
    except Exception:
        log.exception("send hard fail")
        try:
            await target.answer("⚠️ Error sending message to Telegram.", disable_web_page_preview=True)
        except Exception:
            pass

WELCOME = (
    f"<b>🔍 Instagram Public Scraper</b>\n"
    f"━━━━━━━━━━━━━━━━━━\n"
    f"Send me an Instagram username to fetch public profile data.\n\n"
    f"<b>Examples</b>\n"
    f"• <code>natgeo</code>\n"
    f"• <code>@nasa</code>\n\n"
    f"<b>Links</b>\n"
    f"Insta · <a href=\"{INSTA_URL}\">{BRAND}</a>\n"
    f"Dev · <a href=\"{TG_DEV_URL}\">{TG_DEV}</a>\n"
    f"Channel · <a href=\"{TG_CHANNEL_URL}\">{TG_CHANNEL}</a>"
)

HELP = (
    f"<b>How to use</b>\n"
    f"Send any Instagram username (with or without <code>@</code>).\n\n"
    f"<b>Limits</b>\n"
    f"• Public accounts only.\n"
    f"• Instagram rate limits may cause temporary failures.\n\n"
    f"<i>Insta · {BRAND}</i>"
)

@dp.message(CommandStart())
async def cmd_start(m: Message):
    await m.answer(WELCOME, disable_web_page_preview=True)

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP, disable_web_page_preview=True)

@dp.message(F.text)
async def on_text(m: Message):
    raw = (m.text or "").strip()
    if raw.startswith("/"):
        return
        
    username = _clean_username(raw)
    if not username:
        await m.answer("⚠️ That doesn't look like an Instagram username.\nSend <code>natgeo</code> or <code>instagram.com/natgeo</code>.", disable_web_page_preview=True)
        return
        
    ack = await m.answer(f"🔎 Looking up <b>@{html.escape(username)}</b>…")
    
    try:
        p = await scrape_one(username)
    except Exception as e:
        log.exception("scrape_one raised")
        p = Profile(username=username, error="Internal Error", error_detail=str(e))
        
    try:
        await ack.delete()
    except Exception:
        pass
        
    await _send_profile(m, p)

async def _run_bot():
    me = await bot.get_me()
    log.info("bot up as @%s", me.username)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(_run_bot())
    except (KeyboardInterrupt, SystemExit):
        pass