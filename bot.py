#!/usr/bin/env python3
"""
Angel Downloader bot v3 — HexDL-style downloader with:
- 5 languages (fa/en/ru/zh/ar), per-user, stored in DB
- Forced channel join (@ange1Vpn by default)
- Free tier limits (daily count + file size); Premium = unlimited
- Premium sales: Telegram Stars (auto), TON crypto & card-to-card (admin approval)
- In-bot ADMIN PANEL (/admin, Persian): stats, broadcast, premium, bans, payments, settings
- Admin channel posting (/post or panel button): text/photo post with inline URL buttons, preview + confirm, to FORCE_CHANNELS[0]
- Contact-admin support inbox with two-way replies
- Optional LOCAL Bot API server (env LOCAL_BOT_API) -> raises the send cap from 50 MB to 2000 MB
Config via env: BOT_TOKEN, FORCE_CHANNELS, FORCE_JOIN_URLS, ADMIN_IDS, LOCAL_BOT_API, DB_PATH.
Long polling — no public port needed (except the local API server's own localhost port).
"""
import asyncio
import logging
import os
import sqlite3
import tempfile
import time
from datetime import date, datetime

import yt_dlp

try:
    import imageio_ffmpeg
    FFMPEG_LOCATION = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_LOCATION = None
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      KeyboardButton, LabeledPrice, ReplyKeyboardMarkup, Update)
from telegram.constants import ChatMemberStatus
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler,
                          PreCheckoutQueryHandler, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dlbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
FORCE_CHANNELS = [c.strip() for c in os.environ.get("FORCE_CHANNELS", "@ange1Vpn").split(",") if c.strip()]
_url_list = [u.strip() for u in os.environ.get("FORCE_JOIN_URLS", "").split(",") if u.strip()]
FORCE_JOIN_URLS = {ch: _url_list[i] for i, ch in enumerate(FORCE_CHANNELS) if i < len(_url_list)}
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db"))
LOCAL_API = os.environ.get("LOCAL_BOT_API", "").rstrip("/")
MB = 1024 * 1024
API_MAX = 2000 * MB if LOCAL_API else 49 * MB   # local Bot API server raises the cap
API_MAX_MB = API_MAX // MB

# ---------------- admin channel posting (/post) ----------------
# Per-admin in-memory drafts: admin_id -> {"stage": "content"|"buttons"|"preview",
# "text": str, "photo_file_id": str|None, "buttons": [(label, url), ...]}
POST_DRAFTS: dict[int, dict] = {}
POST_MAX_BUTTONS = 10
POST_MAX_TEXT = 4096        # Telegram message limit
POST_MAX_CAPTION = 1024     # Telegram photo-caption limit
POST_BUTTON_LABEL_MAX = 64  # Telegram inline-button text limit

DEFAULT_SETTINGS = {
    "free_daily_limit": "5",
    "free_max_mb": "20",
    "premium_days": "30",
    "price_stars": "150",
    "price_toman": "30000",
    "price_ton": "0.5",
    "card_number": "",
    "ton_wallet": os.environ.get("TON_WALLET", "UQB7oE1ngb6P-C-ApduI2rFToVHCFfhB1F0fyqngmIrMYXWX"),
    "sales_enabled": "0",
}

# ---------------- DB ----------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    lang TEXT DEFAULT 'fa', joined_at INTEGER, banned INTEGER DEFAULT 0, downloads_total INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS daily(user_id INTEGER, day TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS premium(user_id INTEGER PRIMARY KEY, until INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    method TEXT, status TEXT DEFAULT 'pending', created INTEGER);
CREATE TABLE IF NOT EXISTS support_map(admin_id INTEGER, msg_id INTEGER, user_id INTEGER,
    PRIMARY KEY(admin_id, msg_id));
""")
for k, v in DEFAULT_SETTINGS.items():
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
db.commit()


def setting(key: str) -> str:
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    db.commit()


def register_user(u) -> None:
    db.execute("INSERT OR IGNORE INTO users(user_id, username, first_name, joined_at) VALUES(?,?,?,?)",
               (u.id, u.username, u.first_name, int(time.time())))
    db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (u.username, u.first_name, u.id))
    db.commit()


def is_banned(uid: int) -> bool:
    r = db.execute("SELECT banned FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["banned"])


def premium_until(uid: int) -> int:
    r = db.execute("SELECT until FROM premium WHERE user_id=?", (uid,)).fetchone()
    return r["until"] if r else 0


def is_premium(uid: int) -> bool:
    return uid in ADMIN_IDS or premium_until(uid) > int(time.time())


def grant_premium(uid: int, days: int) -> int:
    until = max(premium_until(uid), int(time.time())) + days * 86400
    db.execute("INSERT OR REPLACE INTO premium(user_id, until) VALUES(?,?)", (uid, until))
    db.commit()
    return until


def today_count(uid: int) -> int:
    r = db.execute("SELECT count FROM daily WHERE user_id=? AND day=?",
                   (uid, date.today().isoformat())).fetchone()
    return r["count"] if r else 0


def bump_daily(uid: int):
    d = date.today().isoformat()
    db.execute("INSERT INTO daily(user_id, day, count) VALUES(?,?,1) "
               "ON CONFLICT(user_id, day) DO UPDATE SET count=count+1", (uid, d))
    db.execute("UPDATE users SET downloads_total=downloads_total+1 WHERE user_id=?", (uid,))
    db.commit()


# ---------------- i18n ----------------
TEXTS = {
    "fa": {
        "welcome": "سلام! 👋\nلینک بفرست، فایلش رو برات می‌فرستم 📥\nاز اینستاگرام، تیک‌تاک، یوتیوب، ایکس (توییتر)، پینترست، ساندکلاد، اسپاتیفای و صدها سایت و شبکه‌ی اجتماعی دیگه دانلود می‌کنم.\nفقط لینک رو بفرست 👇",
        "help": "چطور دانلود کنم؟ 🤔\nلینک رو بفرست؛ فایل رو بگیر. 😎\nاینستاگرام، یوتیوب، توییتر، تیکتاک، پینترست، ساندکلاد و اسپاتیفای پشتیبانی می‌شن.",
        "join": "🔒 عضویت اجباری\nبرای دریافت فایل، در کانال‌های زیر عضو شوید. بعد دکمه‌ی «عضو شدم، بررسی کن» را بزنید.",
        "check_btn": "☑️ عضو شدم، بررسی کن",
        "joined_ok": "✅ عضویتت تأیید شد.",
        "not_member": "هنوز عضو نشدی! اول عضو شو، بعد بررسی کن 🙏",
        "need_link": "فقط لینک بفرست 📥",
        "downloading": "⏬ در حال دانلود...",
        "sending": "📤 در حال ارسال...",
        "failed": "❌ دانلود نشد. لینک رو چک کن و دوباره بفرست.",
        "too_big": "❌ فایل از سقف {max} مگابایتی بزرگ‌تره و نمی‌تونم بفرستمش.",
        "banned": "🚫 حساب شما مسدود شده است.",
        "btn_help": "راهنما", "btn_lang": "زبان", "btn_premium": "⭐ پریمیوم", "btn_contact": "📩 پیام به مدیر",
        "limit_reached": "🚫 سقف دانلود رایگان امروزت ({n} تا) تموم شد!\nبا پریمیوم نامحدود دانلود کن ⭐",
        "prem_active": "⭐ شما پریمیوم هستید (تا {date}) — دانلود نامحدود!",
        "prem_free": "حساب شما رایگان است: روزانه {n} دانلود، حداکثر {mb} مگابایت.\n⭐ پریمیوم = دانلود نامحدود + فایل تا {maxmb} مگابایت",
        "prem_offer": "💎 اشتراک {days} روزه پریمیوم:\n⭐ {stars} ستاره تلگرام\n💎 {ton} TON\n💳 {toman} تومان (کارت به کارت)",
        "pay_stars": "⭐ پرداخت با ستاره تلگرام", "pay_crypto": "💎 پرداخت با ارز (TON)", "pay_card": "💳 کارت به کارت",
        "sales_soon": "⭐ فروش اشتراک پریمیوم به‌زودی فعال می‌شود. فعلاً از دانلود رایگان لذت ببرید!",
        "invoice_title": "اشتراک پریمیوم {days} روزه",
        "invoice_desc": "دانلود نامحدود + فایل تا {maxmb} مگابایت",
        "crypto_inst": "💎 مبلغ {amount} TON را به این آدرس بفرستید:\n`{wallet}`\n\nبعد از پرداخت، دکمه‌ی زیر را بزنید و رسید/هش تراکنش را همین‌جا بفرستید تا مدیر تأیید کند.",
        "card_inst": "💳 مبلغ {amount} تومان را به این کارت واریز کنید:\n`{card}`\n\nبعد از واریز، دکمه‌ی زیر را بزنید و عکس رسید را همین‌جا بفرستید تا مدیر تأیید کند.",
        "card_not_set": "💳 شماره کارت هنوز ثبت نشده است؛ به‌زودی فعال می‌شود.",
        "paid_btn": "✅ پرداخت کردم",
        "send_receipt": "📎 رسید پرداخت را بفرستید (عکس رسید یا هش تراکنش به صورت متن).",
        "receipt_sent": "✅ رسید شما برای مدیر ارسال شد؛ بعد از تأیید، پریمیوم فعال می‌شود.",
        "pay_approved": "🎉 پرداخت شما تأیید شد! اشتراک پریمیوم تا {date} فعال است. دانلود نامحدود!",
        "pay_rejected": "❌ رسید پرداخت شما تأیید نشد. اگر اشتباهی شده با پشتیبانی در تماس باشید.",
        "stars_success": "🎉 پرداخت با ستاره موفق بود! اشتراک پریمیوم شما تا {date} فعال شد. دانلود نامحدود!",
        "contact_prompt": "📩 پیامت رو همین‌جا بنویس و بفرست (متن، عکس یا ویدیو).\nمستقیم برای مدیر ارسال می‌شه و جوابش هم همین‌جا میاد. برای لغو، /start بزن.",
        "contact_sent": "✅ پیامت برای مدیر ارسال شد. جوابش همین‌جا میاد.",
        "admin_reply": "📩 پاسخ مدیر:",
        "lang_changed": "✅ زبان تنظیم شد.",
    },
    "en": {
        "welcome": "Hi! 👋\nSend me a link and I'll send you the file 📥\nI download from Instagram, TikTok, YouTube, X (Twitter), Pinterest, SoundCloud, Spotify and hundreds of other sites and social networks.\nJust send the link 👇",
        "help": "How to download? 🤔\nSend a link; get the file. 😎\nInstagram, YouTube, Twitter, TikTok, Pinterest, SoundCloud and Spotify are supported.",
        "join": "🔒 Forced join\nTo receive files, join the channel(s) below. Then tap “I joined, check”.",
        "check_btn": "☑️ I joined, check",
        "joined_ok": "✅ Membership confirmed.",
        "not_member": "You haven't joined yet! Join first, then check 🙏",
        "need_link": "Just send a link 📥",
        "downloading": "⏬ Downloading...",
        "sending": "📤 Sending...",
        "failed": "❌ Download failed. Check the link and try again.",
        "too_big": "❌ The file is larger than the {max} MB limit and I can't send it.",
        "banned": "🚫 Your account is blocked.",
        "btn_help": "Help", "btn_lang": "Language", "btn_premium": "⭐ Premium", "btn_contact": "📩 Contact admin",
        "limit_reached": "🚫 You've hit today's free download limit ({n}).\nGo Premium for unlimited downloads ⭐",
        "prem_active": "⭐ You are Premium (until {date}) — unlimited downloads!",
        "prem_free": "Your account is free: {n} downloads per day, up to {mb} MB each.\n⭐ Premium = unlimited downloads + files up to {maxmb} MB",
        "prem_offer": "💎 {days}-day Premium plan:\n⭐ {stars} Telegram Stars\n💎 {ton} TON\n💳 {toman} (card transfer)",
        "pay_stars": "⭐ Pay with Telegram Stars", "pay_crypto": "💎 Pay with crypto (TON)", "pay_card": "💳 Card to card",
        "sales_soon": "⭐ Premium sales open soon. Enjoy free downloads for now!",
        "invoice_title": "{days}-day Premium subscription",
        "invoice_desc": "Unlimited downloads + files up to {maxmb} MB",
        "crypto_inst": "💎 Send {amount} TON to this address:\n`{wallet}`\n\nAfter paying, tap the button below and send the receipt / transaction hash here for admin approval.",
        "card_inst": "💳 Transfer {amount} to this card:\n`{card}`\n\nAfter paying, tap the button below and send the receipt photo here for admin approval.",
        "card_not_set": "💳 Card number is not set yet; coming soon.",
        "paid_btn": "✅ I paid",
        "send_receipt": "📎 Send your payment receipt (photo or transaction hash as text).",
        "receipt_sent": "✅ Your receipt was sent to the admin; Premium activates after approval.",
        "pay_approved": "🎉 Your payment was approved! Premium is active until {date}. Unlimited downloads!",
        "pay_rejected": "❌ Your receipt was not approved. If this is a mistake, contact support.",
        "stars_success": "🎉 Stars payment successful! Your Premium is active until {date}. Unlimited downloads!",
        "contact_prompt": "📩 Write your message here (text, photo or video).\nIt goes straight to the admin and the reply comes back here. Send /start to cancel.",
        "contact_sent": "✅ Your message was sent to the admin. The reply will arrive here.",
        "admin_reply": "📩 Admin reply:",
        "lang_changed": "✅ Language changed.",
    },
}
TEXTS.update({
    "ru": {
        "welcome": "Привет! 👋\nПришли ссылку — я отправлю файл 📥\nСкачиваю из Instagram, TikTok, YouTube, X (Twitter), Pinterest, SoundCloud, Spotify и сотен других сайтов и соцсетей.\nПросто пришли ссылку 👇",
        "help": "Как скачать? 🤔\nПришли ссылку — получи файл. 😎\nПоддерживаются Instagram, YouTube, Twitter, TikTok, Pinterest, SoundCloud и Spotify.",
        "join": "🔒 Обязательная подписка\nЧтобы получать файлы, подпишись на канал(ы) ниже. Затем нажми «Я подписался, проверить».",
        "check_btn": "☑️ Я подписался, проверить",
        "joined_ok": "✅ Подписка подтверждена.",
        "not_member": "Ты ещё не подписался! Сначала подпишись, потом проверяй 🙏",
        "need_link": "Просто пришли ссылку 📥",
        "downloading": "⏬ Скачиваю...",
        "sending": "📤 Отправляю...",
        "failed": "❌ Не удалось скачать. Проверь ссылку и попробуй снова.",
        "too_big": "❌ Файл больше лимита {max} МБ, я не могу его отправить.",
        "banned": "🚫 Твой аккаунт заблокирован.",
        "btn_help": "Помощь", "btn_lang": "Язык", "btn_premium": "⭐ Премиум", "btn_contact": "📩 Написать админу",
        "limit_reached": "🚫 Дневной бесплатный лимит ({n}) исчерпан.\nПремиум — безлимитные загрузки ⭐",
        "prem_active": "⭐ У тебя Премиум (до {date}) — безлимитные загрузки!",
        "prem_free": "Бесплатный аккаунт: {n} загрузок в день, до {mb} МБ каждая.\n⭐ Премиум = безлимит + файлы до {maxmb} МБ",
        "prem_offer": "💎 Премиум на {days} дней:\n⭐ {stars} звёзд Telegram\n💎 {ton} TON\n💳 {toman} (перевод на карту)",
        "pay_stars": "⭐ Оплата звёздами Telegram", "pay_crypto": "💎 Оплата криптовалютой (TON)", "pay_card": "💳 Перевод на карту",
        "sales_soon": "⭐ Продажа Премиума скоро откроется. Пока пользуйся бесплатными загрузками!",
        "invoice_title": "Премиум-подписка на {days} дней",
        "invoice_desc": "Безлимитные загрузки + файлы до {maxmb} МБ",
        "crypto_inst": "💎 Отправь {amount} TON на этот адрес:\n`{wallet}`\n\nПосле оплаты нажми кнопку ниже и пришли чек / хэш транзакции сюда для подтверждения админом.",
        "card_inst": "💳 Переведи {amount} на эту карту:\n`{card}`\n\nПосле оплаты нажми кнопку ниже и пришли фото чека сюда для подтверждения админом.",
        "card_not_set": "💳 Номер карты ещё не указан; скоро появится.",
        "paid_btn": "✅ Я оплатил",
        "send_receipt": "📎 Пришли чек оплаты (фото или хэш транзакции текстом).",
        "receipt_sent": "✅ Чек отправлен админу; Премиум активируется после подтверждения.",
        "pay_approved": "🎉 Оплата подтверждена! Премиум активен до {date}. Безлимитные загрузки!",
        "pay_rejected": "❌ Чек не подтверждён. Если это ошибка, напиши в поддержку.",
        "stars_success": "🎉 Оплата звёздами прошла! Премиум активен до {date}. Безлимитные загрузки!",
        "contact_prompt": "📩 Напиши сообщение здесь (текст, фото или видео).\nОно уйдёт напрямую админу, ответ придёт сюда же. Отмена — /start.",
        "contact_sent": "✅ Сообщение отправлено админу. Ответ придёт сюда.",
        "admin_reply": "📩 Ответ админа:",
        "lang_changed": "✅ Язык изменён.",
    },
    "zh": {
        "welcome": "你好!👋\n发送链接，我把文件发给你 📥\n支持 Instagram、TikTok、YouTube、X（Twitter）、Pinterest、SoundCloud、Spotify 以及数百个其他网站和社交平台。\n直接发送链接即可 👇",
        "help": "如何下载?🤔\n发送链接，获取文件。😎\n支持 Instagram、YouTube、Twitter、TikTok、Pinterest、SoundCloud 和 Spotify。",
        "join": "🔒 强制加入\n要接收文件，请先加入下方频道，然后点击“已加入，检查”。",
        "check_btn": "☑️ 已加入，检查",
        "joined_ok": "✅ 已确认加入。",
        "not_member": "你还没有加入!请先加入，然后再检查 🙏",
        "need_link": "请发送链接 📥",
        "downloading": "⏬ 正在下载...",
        "sending": "📤 正在发送...",
        "failed": "❌ 下载失败。请检查链接后重试。",
        "too_big": "❌ 文件超过 {max} MB 上限，无法发送。",
        "banned": "🚫 你的账号已被封禁。",
        "btn_help": "帮助", "btn_lang": "语言", "btn_premium": "⭐ 高级版", "btn_contact": "📩 联系管理员",
        "limit_reached": "🚫 今日免费下载次数（{n} 次）已用完。\n开通高级版可无限下载 ⭐",
        "prem_active": "⭐ 你是高级会员（至 {date}）— 无限下载!",
        "prem_free": "免费账号：每天 {n} 次下载，单个文件不超过 {mb} MB。\n⭐ 高级版 = 无限下载 + 文件最大 {maxmb} MB",
        "prem_offer": "💎 {days} 天高级版：\n⭐ {stars} 个 Telegram 星星\n💎 {ton} TON\n💳 {toman}（银行卡转账）",
        "pay_stars": "⭐ 用 Telegram 星星支付", "pay_crypto": "💎 用加密货币 (TON) 支付", "pay_card": "💳 银行卡转账",
        "sales_soon": "⭐ 高级版即将开售。先享受免费下载吧!",
        "invoice_title": "{days} 天高级版订阅",
        "invoice_desc": "无限下载 + 文件最大 {maxmb} MB",
        "crypto_inst": "💎 请将 {amount} TON 发送到此地址：\n`{wallet}`\n\n付款后点击下方按钮，将收据/交易哈希发送到这里，等待管理员确认。",
        "card_inst": "💳 请将 {amount} 转账到此卡：\n`{card}`\n\n付款后点击下方按钮，将收据照片发送到这里，等待管理员确认。",
        "card_not_set": "💳 尚未设置卡号，敬请期待。",
        "paid_btn": "✅ 我已付款",
        "send_receipt": "📎 请发送付款收据（照片或文字版交易哈希）。",
        "receipt_sent": "✅ 收据已发送给管理员，确认后将开通高级版。",
        "pay_approved": "🎉 付款已确认!高级版有效期至 {date}，无限下载!",
        "pay_rejected": "❌ 收据未通过确认。如有误，请联系客服。",
        "stars_success": "🎉 星星支付成功!高级版有效期至 {date}，无限下载!",
        "contact_prompt": "📩 在这里写下你的消息（文字、照片或视频）。\n将直接发送给管理员，回复也会发到这里。发送 /start 取消。",
        "contact_sent": "✅ 消息已发送给管理员，回复会发到这里。",
        "admin_reply": "📩 管理员回复：",
        "lang_changed": "✅ 语言已更改。",
    },
    "ar": {
        "welcome": "مرحباً! 👋\nأرسل رابطاً وسأرسل لك الملف 📥\nأحمّل من إنستغرام وتيك توك ويوتيوب وإكس (تويتر) وبينترست وساوند كلاود وسبوتيفاي ومئات المواقع والشبكات الاجتماعية الأخرى.\nفقط أرسل الرابط 👇",
        "help": "كيف أحمّل؟ 🤔\nأرسل الرابط؛ واحصل على الملف. 😎\nالمدعوم: إنستغرام، يوتيوب، تويتر، تيك توك، بينترست، ساوند كلاود وسبوتيفاي.",
        "join": "🔒 اشتراك إلزامي\nلاستلام الملفات، اشترك في القناة (القنوات) أدناه. ثم اضغط «اشتركت، تحقق».",
        "check_btn": "☑️ اشتركت، تحقق",
        "joined_ok": "✅ تم تأكيد اشتراكك.",
        "not_member": "لم تشترك بعد! اشترك أولاً ثم تحقق 🙏",
        "need_link": "أرسل رابطاً فقط 📥",
        "downloading": "⏬ جارٍ التحميل...",
        "sending": "📤 جارٍ الإرسال...",
        "failed": "❌ فشل التحميل. تحقق من الرابط وحاول مجدداً.",
        "too_big": "❌ الملف أكبر من حد {max} ميغابايت ولا أستطيع إرساله.",
        "banned": "🚫 حسابك محظور.",
        "btn_help": "مساعدة", "btn_lang": "اللغة", "btn_premium": "⭐ بريميوم", "btn_contact": "📩 مراسلة المدير",
        "limit_reached": "🚫 وصلت إلى حد التحميل المجاني اليوم ({n}).\nالبريميوم = تحميل بلا حدود ⭐",
        "prem_active": "⭐ أنت بريميوم (حتى {date}) — تحميل بلا حدود!",
        "prem_free": "حسابك مجاني: {n} تحميلات يومياً، حتى {mb} ميغابايت لكل ملف.\n⭐ بريميوم = تحميل بلا حدود + ملفات حتى {maxmb} ميغابايت",
        "prem_offer": "💎 اشتراك بريميوم {days} يوماً:\n⭐ {stars} نجمة تيليغرام\n💎 {ton} TON\n💳 {toman} (تحويل بالبطاقة)",
        "pay_stars": "⭐ الدفع بنجوم تيليغرام", "pay_crypto": "💎 الدفع بالعملات الرقمية (TON)", "pay_card": "💳 تحويل بالبطاقة",
        "sales_soon": "⭐ بيع البريميوم يفتح قريباً. استمتع بالتحميل المجاني الآن!",
        "invoice_title": "اشتراك بريميوم {days} يوماً",
        "invoice_desc": "تحميل بلا حدود + ملفات حتى {maxmb} ميغابايت",
        "crypto_inst": "💎 أرسل {amount} TON إلى هذا العنوان:\n`{wallet}`\n\nبعد الدفع، اضغط الزر أدناه وأرسل الإيصال / هاش المعاملة هنا ليوافق عليه المدير.",
        "card_inst": "💳 حوّل {amount} إلى هذه البطاقة:\n`{card}`\n\nبعد الدفع، اضغط الزر أدناه وأرسل صورة الإيصال هنا ليوافق عليه المدير.",
        "card_not_set": "💳 لم يُضف رقم البطاقة بعد؛ قريباً.",
        "paid_btn": "✅ دفعت",
        "send_receipt": "📎 أرسل إيصال الدفع (صورة أو هاش المعاملة نصاً).",
        "receipt_sent": "✅ أُرسل إيصالك إلى المدير؛ يُفعّل البريميوم بعد الموافقة.",
        "pay_approved": "🎉 تمت الموافقة على دفعتك! البريميوم فعّال حتى {date}. تحميل بلا حدود!",
        "pay_rejected": "❌ لم تتم الموافقة على إيصالك. إذا حدث خطأ، تواصل مع الدعم.",
        "stars_success": "🎉 نجح الدفع بالنجوم! البريميوم فعّال حتى {date}. تحميل بلا حدود!",
        "contact_prompt": "📩 اكتب رسالتك هنا (نص أو صورة أو فيديو).\nتصل مباشرة إلى المدير ويأتي الرد هنا. للإلغاء أرسل /start.",
        "contact_sent": "✅ أُرسلت رسالتك إلى المدير. سيصل الرد هنا.",
        "admin_reply": "📩 رد المدير:",
        "lang_changed": "✅ تم تغيير اللغة.",
    },
})
LANG_NAMES = {"fa": "🇮🇷 فارسی", "en": "🇬🇧 English", "ru": "🇷🇺 Русский",
              "zh": "🇨🇳 中文", "ar": "🇸🇦 العربية"}
_lang_cache: dict[int, str] = {}


def lang_of(uid: int) -> str:
    if uid in _lang_cache:
        return _lang_cache[uid]
    r = db.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    code = r["lang"] if r and r["lang"] in TEXTS else "fa"
    _lang_cache[uid] = code
    return code


def set_lang_db(uid: int, code: str):
    _lang_cache[uid] = code
    db.execute("UPDATE users SET lang=? WHERE user_id=?", (code, uid))
    db.commit()


def t(uid: int, key: str, **fmt) -> str:
    s = TEXTS[lang_of(uid)][key]
    return s.format(**fmt) if fmt else s


def main_keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(t(uid, "btn_help")), KeyboardButton(t(uid, "btn_lang"))],
            [KeyboardButton(t(uid, "btn_premium")), KeyboardButton(t(uid, "btn_contact"))]]
    if uid in ADMIN_IDS:
        rows.append([KeyboardButton("🎛 پنل مدیریت")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# Texts of the main reply keyboard (all languages) — used to let those buttons
# pass through the post-draft router instead of being swallowed as post content.
MAIN_KB_TEXTS = {TEXTS[c][k] for c in TEXTS
                 for k in ("btn_help", "btn_lang", "btn_premium", "btn_contact")}
MAIN_KB_TEXTS.add("🎛 پنل مدیریت")


def join_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📢 {ch}", url=FORCE_JOIN_URLS.get(ch, f"https://t.me/{ch.lstrip('@')}"))]
            for ch in FORCE_CHANNELS]
    rows.append([InlineKeyboardButton(t(uid, "check_btn"), callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


# ---------------- membership gate ----------------
async def is_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user.id in ADMIN_IDS:
        return True
    for ch in FORCE_CHANNELS:
        try:
            m = await context.bot.get_chat_member(ch, user.id)
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
        except Exception as e:
            log.warning("get_chat_member %s failed: %s", ch, e)
            return False
    return True


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_member(update, context):
        return True
    uid = update.effective_user.id
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(t(uid, "join"), reply_markup=join_keyboard(uid))
    return False


# ---------------- basic handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    POST_DRAFTS.pop(uid, None)  # /start resets any active post draft
    if is_banned(uid):
        await update.message.reply_text(t(uid, "banned"))
        return
    await update.message.reply_text(t(uid, "welcome"), reply_markup=main_keyboard(uid))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(t(uid, "help"), reply_markup=main_keyboard(uid))


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 `{update.effective_user.id}`", parse_mode="Markdown")


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"lang:{code}")]
                                for code, name in LANG_NAMES.items()])
    await update.message.reply_text("🌐 Choose your language / زبان را انتخاب کنید", reply_markup=kb)


async def set_lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    code = q.data.split(":", 1)[1]
    register_user(q.from_user)
    set_lang_db(q.from_user.id, code)
    await q.answer(TEXTS[code]["lang_changed"])
    await q.message.reply_text(TEXTS[code]["welcome"], reply_markup=main_keyboard(q.from_user.id))


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if await is_member(update, context):
        await q.answer(t(uid, "joined_ok"))
        await q.message.reply_text(t(uid, "welcome"), reply_markup=main_keyboard(uid))
    else:
        await q.answer(t(uid, "not_member"), show_alert=True)


# ---------------- contact admin (support) ----------------
async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data["await_support"] = True
    await update.message.reply_text(t(uid, "contact_prompt"))


async def deliver_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    uname = f"@{u.username}" if u.username else "—"
    header = (f"📩 پیام جدید از کاربر\n👤 {u.first_name} ({uname})\n"
              f"🆔 `{u.id}`\nبرای پاسخ، روی پیام کاربر ریپلای کن.")
    for admin in ADMIN_IDS:
        try:
            h = await context.bot.send_message(admin, header, parse_mode="Markdown")
            c = await context.bot.copy_message(admin, update.effective_chat.id,
                                               update.message.message_id)
            for m in (h, c):
                db.execute("INSERT OR REPLACE INTO support_map(admin_id, msg_id, user_id) VALUES(?,?,?)",
                           (admin, m.message_id, u.id))
            db.commit()
        except Exception:
            log.exception("support deliver failed")
    await update.message.reply_text(t(u.id, "contact_sent"))


async def deliver_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if uid not in ADMIN_IDS or not update.message.reply_to_message:
        return False
    row = db.execute("SELECT user_id FROM support_map WHERE admin_id=? AND msg_id=?",
                     (uid, update.message.reply_to_message.message_id)).fetchone()
    if not row:
        return False
    target = row["user_id"]
    await context.bot.send_message(target, t(target, "admin_reply"))
    await context.bot.copy_message(target, update.effective_chat.id, update.message.message_id)
    await update.message.reply_text("✅ پاسخت برای کاربر ارسال شد.")
    return True


async def pre_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 router: admin support replies, admin panel input, user support messages."""
    msg = update.message
    if msg is None:
        return
    if await deliver_admin_reply(update, context):
        raise ApplicationHandlerStop
    if await post_draft_input(update, context):
        raise ApplicationHandlerStop
    uid = update.effective_user.id
    if uid in ADMIN_IDS and context.user_data.get("await") and msg.text:
        await admin_input(update, context)
        raise ApplicationHandlerStop
    if context.user_data.get("await_support"):
        if msg.text and msg.text.startswith("/"):
            context.user_data.pop("await_support", None)
            return
        context.user_data.pop("await_support", None)
        await deliver_support(update, context)
        raise ApplicationHandlerStop


# ---------------- premium (user side) ----------------
def premium_text(uid: int) -> str:
    days = setting("premium_days")
    if is_premium(uid):
        exp = datetime.fromtimestamp(premium_until(uid)).strftime("%Y-%m-%d")
        status = t(uid, "prem_active", date=exp)
    else:
        status = t(uid, "prem_free", n=setting("free_daily_limit"),
                   mb=setting("free_max_mb"), maxmb=API_MAX_MB)
    offer = t(uid, "prem_offer", days=days, stars=setting("price_stars"),
              ton=setting("price_ton"), toman=f"{int(setting('price_toman')):,}")
    return f"{status}\n\n{offer}"


def premium_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(uid, "pay_stars"), callback_data="buy:stars")],
        [InlineKeyboardButton(t(uid, "pay_crypto"), callback_data="buy:crypto")],
        [InlineKeyboardButton(t(uid, "pay_card"), callback_data="buy:card")],
    ])


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    target = update.callback_query.message if update.callback_query else update.message
    if setting("sales_enabled") != "1" and uid not in ADMIN_IDS:
        await target.reply_text(t(uid, "sales_soon"))
        return
    await target.reply_text(premium_text(uid), reply_markup=premium_keyboard(uid))


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    method = q.data.split(":", 1)[1]
    days = setting("premium_days")
    if method == "stars":
        await q.answer()
        await context.bot.send_invoice(
            chat_id=uid, title=t(uid, "invoice_title", days=days),
            description=t(uid, "invoice_desc", maxmb=API_MAX_MB),
            payload=f"premium:{uid}", provider_token="", currency="XTR",
            prices=[LabeledPrice(f"Premium {days}d", int(setting("price_stars")))])
    elif method == "crypto":
        await q.answer()
        context.user_data["pay_method"] = "crypto"
        await q.message.reply_text(
            t(uid, "crypto_inst", amount=setting("price_ton"), wallet=setting("ton_wallet")),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(t(uid, "paid_btn"), callback_data="paid:crypto")]]))
    elif method == "card":
        card = setting("card_number")
        await q.answer()
        if not card:
            await q.message.reply_text(t(uid, "card_not_set"))
            return
        context.user_data["pay_method"] = "card"
        await q.message.reply_text(
            t(uid, "card_inst", amount=f"{int(setting('price_toman')):,}", card=card),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(t(uid, "paid_btn"), callback_data="paid:card")]]))


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    context.user_data["await_receipt"] = q.data.split(":", 1)[1]
    await q.answer()
    await q.message.reply_text(t(uid, "send_receipt"))


async def receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    method = context.user_data.pop("await_receipt")
    cur = db.execute("INSERT INTO payments(user_id, method, created) VALUES(?,?,?)",
                     (uid, method, int(time.time())))
    db.commit()
    pid = cur.lastrowid
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید و فعال‌سازی", callback_data=f"pay:ok:{pid}"),
        InlineKeyboardButton("❌ رد", callback_data=f"pay:no:{pid}")]])
    note = (f"💰 رسید پرداخت جدید #{pid}\nکاربر: {uid} (@{update.effective_user.username or '-'})\n"
            f"روش: {'TON' if method == 'crypto' else 'کارت به کارت'}")
    for admin in ADMIN_IDS:
        try:
            if update.message.photo:
                await context.bot.send_photo(admin, update.message.photo[-1].file_id,
                                             caption=note, reply_markup=kb)
            else:
                await context.bot.send_message(admin, f"{note}\nمتن کاربر:\n{update.message.text or ''}",
                                               reply_markup=kb)
        except Exception:
            log.exception("notify admin failed")
    await update.message.reply_text(t(uid, "receipt_sent"))


async def pay_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("فقط مدیر", show_alert=True)
        return
    _, action, pid = q.data.split(":")
    row = db.execute("SELECT * FROM payments WHERE id=?", (int(pid),)).fetchone()
    if not row or row["status"] != "pending":
        await q.answer("این پرداخت قبلاً بررسی شده.")
        return
    target = row["user_id"]
    if action == "ok":
        until = grant_premium(target, int(setting("premium_days")))
        db.execute("UPDATE payments SET status='approved' WHERE id=?", (pid,))
        db.commit()
        exp = datetime.fromtimestamp(until).strftime("%Y-%m-%d")
        await context.bot.send_message(target, t(target, "pay_approved", date=exp))
        await q.answer("✅ پریمیوم فعال شد")
    else:
        db.execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        db.commit()
        await context.bot.send_message(target, t(target, "pay_rejected"))
        await q.answer("❌ رد شد")


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    until = grant_premium(uid, int(setting("premium_days")))
    exp = datetime.fromtimestamp(until).strftime("%Y-%m-%d")
    await update.message.reply_text(t(uid, "stars_success", date=exp))
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, f"⭐ فروش جدید با ستاره: کاربر {uid} — پریمیوم تا {exp}")
        except Exception:
            pass


# ---------------- admin panel (Persian) ----------------
def admin_keyboard() -> InlineKeyboardMarkup:
    sales_on = setting("sales_enabled") == "1"
    sales_btn = ("🟢 فروش پریمیوم: فعال — برای خاموش‌کردن بزن" if sales_on
                 else "🔴 فروش پریمیوم: غیرفعال — برای روشن‌کردن بزن")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار", callback_data="adm:stats"),
         InlineKeyboardButton("📣 پیام همگانی", callback_data="adm:broadcast")],
        [InlineKeyboardButton("📣 پست در کانال", callback_data="adm:post")],
        [InlineKeyboardButton("⭐ افزودن پریمیوم", callback_data="adm:addprem"),
         InlineKeyboardButton("📋 لیست پریمیوم", callback_data="adm:listprem")],
        [InlineKeyboardButton("⭐ حذف پریمیوم", callback_data="adm:delprem"),
         InlineKeyboardButton("💰 پرداخت‌های در انتظار", callback_data="adm:pending")],
        [InlineKeyboardButton("🚫 بن کاربر", callback_data="adm:ban"),
         InlineKeyboardButton("✅ رفع بن", callback_data="adm:unban")],
        [InlineKeyboardButton(sales_btn, callback_data="adm:sales")],
        [InlineKeyboardButton("⚙️ تنظیمات", callback_data="adm:settings")],
    ])


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 برگشت به منوی مدیریت", callback_data="adm:home")]])


SETTING_LABELS = {
    "free_daily_limit": "سقف دانلود روزانه رایگان", "free_max_mb": "سقف حجم رایگان (مگابایت)",
    "premium_days": "مدت اشتراک (روز)", "price_stars": "قیمت ستاره", "price_toman": "قیمت تومان",
    "price_ton": "قیمت TON", "card_number": "شماره کارت", "sales_enabled": "فروش فعال؟ (1/0)",
}


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("🎛 پنل مدیریت ربات:", reply_markup=admin_keyboard())


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("فقط مدیر", show_alert=True)
        return
    action = q.data.split(":", 1)[1]
    await q.answer()
    if action in ("broadcast", "addprem", "delprem", "ban", "unban"):
        POST_DRAFTS.pop(q.from_user.id, None)  # starting another admin input abandons a post draft
    if action == "home":
        context.user_data.pop("await", None)
        POST_DRAFTS.pop(q.from_user.id, None)
        try:
            await q.message.edit_text("🎛 پنل مدیریت ربات:", reply_markup=admin_keyboard())
        except Exception:
            await q.message.reply_text("🎛 پنل مدیریت ربات:", reply_markup=admin_keyboard())
        return
    if action == "post":
        await _post_begin(q.from_user.id, context, q.message.reply_text)
        return
    if action == "sales":
        new = "0" if setting("sales_enabled") == "1" else "1"
        set_setting("sales_enabled", new)
        try:
            await q.message.edit_text("🎛 پنل مدیریت ربات:", reply_markup=admin_keyboard())
        except Exception:
            pass
        await q.message.reply_text("🟢 فروش پریمیوم فعال شد." if new == "1"
                                   else "🔴 فروش پریمیوم غیرفعال شد.",
                                   reply_markup=back_keyboard())
        return
    if action == "stats":
        total = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        prem = db.execute("SELECT COUNT(*) c FROM premium WHERE until>?", (int(time.time()),)).fetchone()["c"]
        dl_today = db.execute("SELECT COALESCE(SUM(count),0) s FROM daily WHERE day=?",
                              (date.today().isoformat(),)).fetchone()["s"]
        dl_total = db.execute("SELECT COALESCE(SUM(downloads_total),0) s FROM users").fetchone()["s"]
        pend = db.execute("SELECT COUNT(*) c FROM payments WHERE status='pending'").fetchone()["c"]
        await q.message.reply_text(f"📊 آمار ربات:\n👥 کاربران: {total}\n⭐ پریمیوم فعال: {prem}\n"
                                   f"⬇️ دانلود امروز: {dl_today}\n⬇️ دانلود کل: {dl_total}\n💰 پرداخت در انتظار: {pend}",
                                   reply_markup=back_keyboard())
    elif action == "broadcast":
        context.user_data["await"] = "broadcast"
        await q.message.reply_text("📣 متن پیام همگانی را بفرستید (به همه‌ی کاربران ارسال می‌شود):",
                                   reply_markup=back_keyboard())
    elif action == "addprem":
        context.user_data["await"] = "addprem"
        await q.message.reply_text("⭐ آیدی عددی کاربر و تعداد روز را بفرستید، مثل:\n`123456789 30`",
                                   parse_mode="Markdown", reply_markup=back_keyboard())
    elif action == "delprem":
        context.user_data["await"] = "delprem"
        await q.message.reply_text("⭐ آیدی عددی کاربر را بفرستید تا پریمیومش حذف شود:",
                                   reply_markup=back_keyboard())
    elif action == "listprem":
        rows = db.execute("SELECT user_id, until FROM premium WHERE until>? ORDER BY until",
                          (int(time.time()),)).fetchall()
        if not rows:
            await q.message.reply_text("⭐ هیچ اشتراک فعالی نیست.", reply_markup=back_keyboard())
        else:
            txt = "\n".join(f"• `{r['user_id']}` تا {datetime.fromtimestamp(r['until']).strftime('%Y-%m-%d')}"
                            for r in rows)
            await q.message.reply_text(f"⭐ اشتراک‌های فعال:\n{txt}", parse_mode="Markdown",
                                       reply_markup=back_keyboard())
    elif action == "pending":
        rows = db.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id").fetchall()
        if not rows:
            await q.message.reply_text("💰 پرداختی در انتظار نیست.", reply_markup=back_keyboard())
        for r in rows:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأیید", callback_data=f"pay:ok:{r['id']}"),
                InlineKeyboardButton("❌ رد", callback_data=f"pay:no:{r['id']}")]])
            await q.message.reply_text(f"#{r['id']} — کاربر {r['user_id']} — روش: {r['method']}", reply_markup=kb)
    elif action == "ban":
        context.user_data["await"] = "ban"
        await q.message.reply_text("🚫 آیدی عددی کاربر را بفرستید:", reply_markup=back_keyboard())
    elif action == "unban":
        context.user_data["await"] = "unban"
        await q.message.reply_text("✅ آیدی عددی کاربر را بفرستید:", reply_markup=back_keyboard())
    elif action == "settings":
        lines = [f"• {label}: `{setting(k)}`" for k, label in SETTING_LABELS.items()]
        lines.append(f"• آدرس TON: `{setting('ton_wallet')}`")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"✏️ {label}", callback_data=f"set:{k}")]
                                    for k, label in SETTING_LABELS.items()]
                                   + [[InlineKeyboardButton("🔙 برگشت به منوی مدیریت", callback_data="adm:home")]])
        await q.message.reply_text("⚙️ تنظیمات فعلی:\n" + "\n".join(lines),
                                   parse_mode="Markdown", reply_markup=kb)


async def admin_set_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("فقط مدیر", show_alert=True)
        return
    POST_DRAFTS.pop(q.from_user.id, None)  # editing a setting abandons a post draft
    key = q.data.split(":", 1)[1]
    context.user_data["await"] = f"set:{key}"
    await q.answer()
    await q.message.reply_text(f"✏️ مقدار جدید برای «{SETTING_LABELS[key]}» را بفرستید (فعلی: {setting(key)}):",
                               reply_markup=back_keyboard())


async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return
    mode = context.user_data.get("await")
    if not mode:
        return
    text = (update.message.text or "").strip()
    if mode == "broadcast":
        context.user_data.pop("await")
        ids = [r["user_id"] for r in db.execute("SELECT user_id FROM users").fetchall()]
        ok = 0
        for target in ids:
            try:
                await context.bot.send_message(target, text)
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await update.message.reply_text(f"📣 پیام همگانی ارسال شد: {ok} از {len(ids)} کاربر")
    elif mode == "addprem":
        try:
            target, days = text.split()
            until = grant_premium(int(target), int(days))
            context.user_data.pop("await")
            exp = datetime.fromtimestamp(until).strftime("%Y-%m-%d")
            await update.message.reply_text(f"⭐ پریمیوم کاربر {target} تا {exp} فعال شد.")
            await context.bot.send_message(int(target), t(int(target), "prem_active", date=exp))
        except Exception:
            await update.message.reply_text("فرمت اشتباه است؛ مثل: `123456789 30`", parse_mode="Markdown")
    elif mode == "delprem":
        db.execute("DELETE FROM premium WHERE user_id=?", (int(text),))
        db.commit()
        context.user_data.pop("await")
        await update.message.reply_text("⭐ پریمیوم حذف شد.")
    elif mode == "ban":
        db.execute("UPDATE users SET banned=1 WHERE user_id=?", (int(text),))
        db.commit()
        context.user_data.pop("await")
        await update.message.reply_text("🚫 کاربر بن شد.")
    elif mode == "unban":
        db.execute("UPDATE users SET banned=0 WHERE user_id=?", (int(text),))
        db.commit()
        context.user_data.pop("await")
        await update.message.reply_text("✅ بن کاربر برداشته شد.")
    elif mode.startswith("set:"):
        key = mode.split(":", 1)[1]
        set_setting(key, text)
        context.user_data.pop("await")
        await update.message.reply_text(f"⚙️ «{SETTING_LABELS[key]}» شد: {text}")


# ---------------- admin channel posting (/post) ----------------
def post_keyboard(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=url)] for label, url in buttons])


def post_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ لغو", callback_data="post:cancel")]])


def parse_button_line(line: str):
    """Parse one 'متن دکمه | لینک' line -> (label, url), or None if invalid."""
    if "|" not in line:
        return None
    label, url = (p.strip() for p in line.split("|", 1))
    if not label or not url or len(label) > POST_BUTTON_LABEL_MAX:
        return None
    if url.lower().startswith("t.me/"):
        url = "https://" + url
    if " " in url or url in ("http://", "https://"):
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        return None
    return (label, url)


async def _post_begin(uid: int, context: ContextTypes.DEFAULT_TYPE, send):
    """Start (or restart) a post draft for the admin; `send` posts the content prompt."""
    POST_DRAFTS[uid] = {"stage": "content", "text": None, "photo_file_id": None, "buttons": []}
    context.user_data.pop("await", None)
    context.user_data.pop("await_support", None)
    await send("📣 ساخت پست کانال\n\nمتن پست را بفرستید، یا یک عکس بفرستید (کپشن عکس، متن پست می‌شود).",
               reply_markup=post_cancel_keyboard())


async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return  # unknown command for everyone else — no error leak
    await _post_begin(update.effective_user.id, context, update.message.reply_text)


async def post_content_input(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: dict):
    msg = update.message
    if msg.photo:
        caption = (msg.caption or "").strip()
        if len(caption) > POST_MAX_CAPTION:
            await msg.reply_text(f"❌ کپشن عکس خیلی طولانی است (حداکثر {POST_MAX_CAPTION} کاراکتر). کوتاه‌ترش کنید و دوباره بفرستید.",
                                 reply_markup=post_cancel_keyboard())
            return
        draft["photo_file_id"] = msg.photo[-1].file_id
        draft["text"] = caption
    elif msg.text and msg.text.strip():
        text = msg.text.strip()
        if len(text) > POST_MAX_TEXT:
            await msg.reply_text(f"❌ متن پست خیلی طولانی است (حداکثر {POST_MAX_TEXT} کاراکتر). کوتاه‌ترش کنید و دوباره بفرستید.",
                                 reply_markup=post_cancel_keyboard())
            return
        draft["text"] = text
        draft["photo_file_id"] = None
    else:
        await msg.reply_text("فقط متن یا عکس بفرستید.",
                             reply_markup=post_cancel_keyboard())
        return
    draft["stage"] = "buttons"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ بدون دکمه", callback_data="post:nobuttons")],
        [InlineKeyboardButton("❌ لغو", callback_data="post:cancel")],
    ])
    await msg.reply_text("🔘 حالا دکمه‌ها را بفرستید؛ هر دکمه در یک خط، به این شکل:\n"
                         "`متن دکمه | لینک`\n\nمثال:\n`خرید و دریافت فوری | https://t.me/angevpnir_bot`\n"
                         "لینک می‌تواند با http:// یا https:// شروع شود، یا به شکل t.me/xxx باشد.",
                         parse_mode="Markdown", reply_markup=kb)


async def post_buttons_input(update: Update, context: ContextTypes.DEFAULT_TYPE, draft: dict):
    msg = update.message
    text = (msg.text or "").strip()
    if text == "بدون دکمه":
        draft["buttons"] = []
        await post_preview(update.effective_chat.id, context, draft)
        return
    if not text:
        await msg.reply_text("دکمه‌ها را به شکل «متن دکمه | لینک» بفرستید، یا «بدون دکمه» را بزنید.",
                             reply_markup=post_cancel_keyboard())
        return
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    valid, invalid = [], []
    for i, line in enumerate(lines, 1):
        parsed = parse_button_line(line)
        if parsed:
            valid.append(parsed)
        else:
            invalid.append(i)
    if len(draft["buttons"]) + len(valid) > POST_MAX_BUTTONS:
        await msg.reply_text(f"❌ حداکثر {POST_MAX_BUTTONS} دکمه می‌شود گذاشت (الان {len(draft['buttons'])} دکمه ذخیره شده). خط‌های اضافی را کم کنید و دوباره بفرستید.",
                             reply_markup=post_cancel_keyboard())
        return
    if invalid:
        draft["buttons"].extend(valid)  # keep the valid ones
        nums = "، ".join(str(i) for i in invalid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ ادامه با همین دکمه‌ها", callback_data="post:keepbuttons")],
            [InlineKeyboardButton("❌ لغو", callback_data="post:cancel")],
        ])
        await msg.reply_text(f"⚠️ خط‌های {nums} نامعتبر است؛ هر خط باید «متن دکمه | لینک» باشد و لینک با http:// یا https:// شروع شود یا به شکل t.me/xxx باشد."
                             f"\nدکمه‌های معتبر ({len(valid)} تا) ذخیره شد؛ خط‌های درست را دوباره بفرستید یا با همین دکمه‌ها ادامه بدهید.",
                             reply_markup=kb)
        return
    draft["buttons"].extend(valid)
    await post_preview(update.effective_chat.id, context, draft)


async def post_preview(chat_id: int, context: ContextTypes.DEFAULT_TYPE, draft: dict):
    """Send the exact post (with its keyboard) + a confirm row; moves draft to preview."""
    kb = post_keyboard(draft["buttons"])
    if draft["photo_file_id"]:
        await context.bot.send_photo(chat_id, draft["photo_file_id"],
                                     caption=draft["text"] or None, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id, draft["text"], reply_markup=kb)
    await context.bot.send_message(
        chat_id, "👆 پیش‌نمایش پست — برای انتشار تأیید کنید:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ انتشار", callback_data="post:publish"),
            InlineKeyboardButton("❌ لغو", callback_data="post:cancel")]]))
    draft["stage"] = "preview"


async def post_publish(q, context: ContextTypes.DEFAULT_TYPE, draft: dict):
    uid = q.from_user.id
    if not FORCE_CHANNELS:
        await q.message.reply_text("❌ کانالی تنظیم نشده است (FORCE_CHANNELS).")
        return
    channel = FORCE_CHANNELS[0]
    kb = post_keyboard(draft["buttons"])
    try:
        if draft["photo_file_id"]:
            sent = await context.bot.send_photo(channel, draft["photo_file_id"],
                                                caption=draft["text"] or None, reply_markup=kb)
        else:
            sent = await context.bot.send_message(channel, draft["text"], reply_markup=kb)
    except Exception as e:
        log.exception("channel post publish failed")
        await q.message.reply_text(f"❌ انتشار ناموفق بود. مطمئن شوید ربات در {channel} ادمین است و اجازه‌ی پست دارد.\nخطا: {e}")
        return
    POST_DRAFTS.pop(uid, None)
    link = f"https://t.me/{channel.lstrip('@')}/{sent.message_id}"
    await q.message.reply_text(f"✅ پست در {channel} منتشر شد.\n🆔 شناسه پیام: {sent.message_id}\n🔗 {link}",
                               reply_markup=back_keyboard())


async def post_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid not in ADMIN_IDS:
        await q.answer("فقط مدیر", show_alert=True)
        return
    action = q.data.split(":", 1)[1]
    if action == "cancel":
        POST_DRAFTS.pop(uid, None)
        await q.answer("لغو شد")
        try:
            await q.message.edit_text("❌ ساخت پست لغو شد.")
        except Exception:
            await q.message.reply_text("❌ ساخت پست لغو شد.")
        return
    draft = POST_DRAFTS.get(uid)
    if draft is None:
        await q.answer("پیش‌نویسی فعال نیست؛ اول /post را بفرستید.", show_alert=True)
        return
    if action == "nobuttons":
        if draft["stage"] != "buttons":
            await q.answer("در این مرحله ممکن نیست.")
            return
        draft["buttons"] = []
        await q.answer()
        await post_preview(q.message.chat.id, context, draft)
    elif action == "keepbuttons":
        if draft["stage"] != "buttons":
            await q.answer("در این مرحله ممکن نیست.")
            return
        await q.answer()
        await post_preview(q.message.chat.id, context, draft)
    elif action == "publish":
        if draft["stage"] != "preview":
            await q.answer("اول پیش‌نمایش را ببینید.")
            return
        await q.answer()
        await post_publish(q, context, draft)


async def post_draft_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Group -1 router for the post flow. Returns True when the message was consumed.

    Runs after support replies (deliver_admin_reply) and before the admin-panel
    'await' input and the support inbox, so it never intercepts those flows.
    Yields to an awaited payment receipt and to main-keyboard buttons, and lets
    commands (e.g. /start, which resets the draft) fall through to group 0.
    """
    msg = update.message
    uid = update.effective_user.id
    if uid not in ADMIN_IDS or uid not in POST_DRAFTS:
        return False
    draft = POST_DRAFTS[uid]
    if msg.text and msg.text.startswith("/"):
        return False
    if msg.text and msg.text.strip() == "❌ لغو":
        POST_DRAFTS.pop(uid, None)
        await msg.reply_text("❌ ساخت پست لغو شد.")
        return True
    if msg.text and msg.text in MAIN_KB_TEXTS:
        return False
    if context.user_data.get("await_support") or context.user_data.get("await_receipt"):
        return False
    if draft["stage"] == "content":
        await post_content_input(update, context, draft)
    elif draft["stage"] == "buttons":
        if not msg.text:
            await msg.reply_text("دکمه‌ها را به صورت متن بفرستید.",
                                 reply_markup=post_cancel_keyboard())
        else:
            await post_buttons_input(update, context, draft)
    elif draft["stage"] == "preview":
        await msg.reply_text("👆 پیش‌نمایش آماده است؛ با دکمه‌های ✅ انتشار یا ❌ لغو ادامه بده.")
    return True


# ---------------- downloader ----------------
def download(url: str, outdir: str, max_mb: int) -> tuple[str, dict]:
    cap = max_mb * MB
    opts = {
        "outtmpl": os.path.join(outdir, "%(title).80s.%(ext)s"),
        **({"ffmpeg_location": FFMPEG_LOCATION} if FFMPEG_LOCATION else {}),
        "format": f"bv*[height<=720][filesize<{cap}]+ba/b[height<=720][filesize<{cap}]/b/best",
        "merge_output_format": "mp4",
        "max_filesize": cap,
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "socket_timeout": 30, "retries": 3,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            files = [os.path.join(outdir, f) for f in os.listdir(outdir)]
            path = max(files, key=os.path.getsize)
    return path, info


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(update.effective_user)
    if is_banned(uid):
        await update.message.reply_text(t(uid, "banned"))
        return
    if not await gate(update, context):
        return
    prem = is_premium(uid)
    if not prem and today_count(uid) >= int(setting("free_daily_limit")):
        await update.message.reply_text(t(uid, "limit_reached", n=setting("free_daily_limit")),
                                        reply_markup=main_keyboard(uid))
        await premium_cmd(update, context)
        return
    max_mb = API_MAX_MB if prem else int(setting("free_max_mb"))
    status = await update.message.reply_text(t(uid, "downloading"))
    try:
        with tempfile.TemporaryDirectory() as td:
            path, info = await asyncio.to_thread(download, update.message.text.strip(), td, max_mb)
            if os.path.getsize(path) > API_MAX:
                await status.edit_text(t(uid, "too_big", max=API_MAX_MB))
                return
            await status.edit_text(t(uid, "sending"))
            caption = (info.get("title") or "")[:1000]
            ext = os.path.splitext(path)[1].lower()
            with open(path, "rb") as f:
                if ext in (".mp4", ".mkv", ".webm", ".mov"):
                    await update.message.reply_video(f, caption=caption, supports_streaming=True)
                elif ext in (".mp3", ".m4a", ".ogg", ".opus"):
                    await update.message.reply_audio(f, caption=caption)
                else:
                    await update.message.reply_document(f, caption=caption)
            bump_daily(uid)
            await status.delete()
    except Exception:
        log.exception("download failed")
        await status.edit_text(t(uid, "failed"))


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.user_data.get("await_receipt"):
        await receipt(update, context)
        return
    register_user(update.effective_user)
    if is_banned(uid):
        await update.message.reply_text(t(uid, "banned"))
        return
    if not await gate(update, context):
        return
    await update.message.reply_text(t(uid, "need_link"))


async def photo_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("await_receipt"):
        await receipt(update, context)


def main():
    builder = Application.builder().token(BOT_TOKEN)
    if LOCAL_API:
        builder = builder.base_url(f"{LOCAL_API}/bot").base_file_url(f"{LOCAL_API}/file/bot")
    app = builder.build()
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.TEXT | filters.PHOTO | filters.VIDEO
                                    | filters.VOICE | filters.ANIMATION | filters.Document.ALL),
        pre_input), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("post", post_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(MessageHandler(filters.Text([TEXTS[c]["btn_help"] for c in TEXTS]), help_cmd))
    app.add_handler(MessageHandler(filters.Text([TEXTS[c]["btn_lang"] for c in TEXTS]), lang_cmd))
    app.add_handler(MessageHandler(filters.Text([TEXTS[c]["btn_premium"] for c in TEXTS]), premium_cmd))
    app.add_handler(MessageHandler(filters.Text([TEXTS[c]["btn_contact"] for c in TEXTS]), contact_cmd))
    app.add_handler(MessageHandler(filters.Text(["🎛 پنل مدیریت"]), admin_cmd))
    app.add_handler(CallbackQueryHandler(set_lang_cb, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(buy, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(paid, pattern=r"^paid:"))
    app.add_handler(CallbackQueryHandler(pay_decision, pattern=r"^pay:"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(admin_set_cb, pattern=r"^set:"))
    app.add_handler(CallbackQueryHandler(post_cb, pattern=r"^post:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_msg))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"https?://"), handle_link))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, fallback))
    log.info("bot v3 started; admins=%s channels=%s local_api=%s cap=%dMB",
             ADMIN_IDS, FORCE_CHANNELS, bool(LOCAL_API), API_MAX_MB)
    app.run_polling()


if __name__ == "__main__":
    main()
