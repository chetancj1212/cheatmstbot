"""
CheatMST Telegram Bot
Generates free credentials after channel join + 2 referrals.
Credentials are stored in Firebase RTDB (same structure as admin dashboard).
"""

import os
import random
import string
import hashlib
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
FIREBASE_URL = os.getenv("FIREBASE_URL", "").rstrip("/")
DB_SECRET = os.getenv("DB_SECRET", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@medinetwork")
REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "2"))
DEFAULT_LIMIT = int(os.getenv("DEFAULT_LIMIT", "10"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── Firebase REST Helpers ───────────────────────────────────────────────────
def _auth_param():
    """Return auth query string if DB_SECRET is set."""
    return f"?auth={DB_SECRET}" if DB_SECRET else ""


async def fb_get(path: str):
    """GET data from Firebase RTDB."""
    async with httpx.AsyncClient() as client:
        url = f"{FIREBASE_URL}/{path}.json{_auth_param()}"
        r = await client.get(url, timeout=10)
        if r.status_code == 200:
            return r.json()
        logger.error("Firebase GET %s → %s", path, r.status_code)
        return None


async def fb_put(path: str, data):
    """PUT (set) data at a Firebase RTDB path."""
    async with httpx.AsyncClient() as client:
        url = f"{FIREBASE_URL}/{path}.json{_auth_param()}"
        r = await client.put(url, json=data, timeout=10)
        return r.status_code == 200


async def fb_patch(path: str, data):
    """PATCH (update) data at a Firebase RTDB path."""
    async with httpx.AsyncClient() as client:
        url = f"{FIREBASE_URL}/{path}.json{_auth_param()}"
        r = await client.patch(url, json=data, timeout=10)
        return r.status_code == 200


# ─── Utility Functions ───────────────────────────────────────────────────────
def generate_credential():
    """Generate a credential string: 4 random letters + 4 random digits."""
    letters = "".join(random.choices(string.ascii_lowercase, k=4))
    digits = "".join(random.choices(string.digits, k=4))
    return letters + digits


def sha256(text: str) -> str:
    """SHA-256 hash (matches admin dashboard hashing)."""
    return hashlib.sha256(text.encode()).hexdigest()


async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Check if a user has joined the required Telegram channel."""
    try:
        member = await context.bot.get_chat_member(
            chat_id=CHANNEL_USERNAME, user_id=user_id
        )
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning("Channel check failed for %s: %s", user_id, e)
        return False


# ─── Bot Handlers ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — register user, process referrals, show status."""
    user = update.effective_user
    tid = str(user.id)

    # ── Process deep-link referral ──
    referrer_code = context.args[0] if context.args else None

    # ── Get or create bot_user record ──
    bot_user = await fb_get(f"bot_users/{tid}")

    if bot_user is None:
        # First-time user
        bot_user = {
            "username": user.username or user.first_name,
            "referral_code": f"ref_{tid}",
            "referral_count": 0,
            "referrals": {},
            "credentials_generated": False,
            "joined_at": datetime.now().isoformat(),
        }
        await fb_put(f"bot_users/{tid}", bot_user)

        # Credit referrer if link was used
        if referrer_code and referrer_code.startswith("ref_"):
            referrer_id = referrer_code[4:]  # strip "ref_"
            if referrer_id != tid:
                referrer = await fb_get(f"bot_users/{referrer_id}")
                if referrer:
                    referrals = referrer.get("referrals") or {}
                    if tid not in referrals:
                        referrals[tid] = user.username or user.first_name
                        new_count = len(referrals)
                        await fb_patch(
                            f"bot_users/{referrer_id}",
                            {"referrals": referrals, "referral_count": new_count},
                        )
                        # Notify referrer
                        try:
                            await context.bot.send_message(
                                chat_id=int(referrer_id),
                                text=(
                                    f"🎉 *New referral!* {user.first_name} joined using your link.\n"
                                    f"📊 Referrals: {new_count}/{REQUIRED_REFERRALS}"
                                ),
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass

    # ── Already has credentials → just remind ──
    if bot_user.get("credentials_generated"):
        uid = bot_user.get("generated_user_id", "—")
        await update.message.reply_text(
            f"✅ You already have your credentials!\n\n"
            f"🆔 *User ID:* `{uid}`\n"
            f"🔑 *Password:* shown once at generation time\n\n"
            f"Use /mycreds to view your User ID and usage.",
            parse_mode="Markdown",
        )
        return

    # ── Show welcome + progress ──
    await _send_status_message(update.message, context, user, bot_user, tid)


async def _send_status_message(target, context, user, bot_user, tid, edit=False):
    """Build and send (or edit) the status / welcome message."""
    bot_me = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_me.username}?start=ref_{tid}"

    ref_count = bot_user.get("referral_count", 0)
    joined = await is_channel_member(context, int(tid))

    ch_icon = "✅" if joined else "❌"
    rf_icon = "✅" if ref_count >= REQUIRED_REFERRALS else "❌"

    keyboard = [
        [
            InlineKeyboardButton(
                "📢 Join Channel",
                url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}",
            )
        ],
        [InlineKeyboardButton("🔄 Check Status", callback_data="check_status")],
        [InlineKeyboardButton("🎁 Get My Credentials", callback_data="generate_creds")],
        [InlineKeyboardButton("📩 Contact Admin", url="https://t.me/Contira")],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"👋 Welcome *{user.first_name}*!\n\n"
        f"🎯 Complete these steps to get your *FREE* credentials:\n\n"
        f"{ch_icon}  Join {CHANNEL_USERNAME}\n"
        f"{rf_icon}  Refer {REQUIRED_REFERRALS} friends ({ref_count}/{REQUIRED_REFERRALS})\n\n"
        f"📎 *Your Referral Link:*\n`{ref_link}`\n\n"
        f"Share this link with friends. Once both tasks are done, "
        f"tap *Get My Credentials*!"
    )

    if edit:
        await target.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await target.reply_text(text, parse_mode="Markdown", reply_markup=markup)


# ── Callback: Check Status ──────────────────────────────────────────────────
async def cb_check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    tid = str(user.id)

    bot_user = await fb_get(f"bot_users/{tid}")
    if not bot_user:
        await query.edit_message_text("⚠️ Please use /start first.")
        return

    if bot_user.get("credentials_generated"):
        uid = bot_user.get("generated_user_id", "—")
        await query.edit_message_text(
            f"✅ Your credentials are already generated!\n\n"
            f"🆔 *User ID:* `{uid}`\n\nUse /mycreds for details.",
            parse_mode="Markdown",
        )
        return

    await _send_status_message(query.message, context, user, bot_user, tid, edit=True)


# ── Callback: Generate Credentials ──────────────────────────────────────────
async def cb_generate_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    tid = str(user.id)

    bot_user = await fb_get(f"bot_users/{tid}")
    if not bot_user:
        await query.edit_message_text("⚠️ Please use /start first.")
        return

    # Already generated
    if bot_user.get("credentials_generated"):
        uid = bot_user.get("generated_user_id", "—")
        await query.edit_message_text(
            f"✅ Already generated!\n\n🆔 *User ID:* `{uid}`",
            parse_mode="Markdown",
        )
        return

    # ── Verify channel membership ──
    joined = await is_channel_member(context, user.id)
    if not joined:
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}",
                )
            ],
            [InlineKeyboardButton("🔄 Check Again", callback_data="check_status")],
            [InlineKeyboardButton("📩 Contact Admin", url="https://t.me/Contira")],
        ]
        await query.edit_message_text(
            f"❌ You haven't joined *{CHANNEL_USERNAME}* yet!\n"
            f"Join the channel first, then tap Check Again.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # ── Verify referral count ──
    ref_count = bot_user.get("referral_count", 0)
    if ref_count < REQUIRED_REFERRALS:
        remaining = REQUIRED_REFERRALS - ref_count
        bot_me = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_me.username}?start=ref_{tid}"
        keyboard = [
            [InlineKeyboardButton("🔄 Check Again", callback_data="check_status")],
            [InlineKeyboardButton("📩 Contact Admin", url="https://t.me/Contira")],
        ]
        await query.edit_message_text(
            f"❌ You still need *{remaining}* more referral(s)!\n\n"
            f"📎 Share your link:\n`{ref_link}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # ── All conditions met → generate credentials ──
    # Ensure unique user ID
    new_user_id = None
    for _ in range(10):
        candidate = generate_credential()
        existing = await fb_get(f"users/{candidate}")
        if existing is None:
            new_user_id = candidate
            break

    if new_user_id is None:
        await query.edit_message_text(
            "❌ Could not generate a unique ID. Please try again later."
        )
        return

    new_password = generate_credential()
    hashed_pw = sha256(new_password)

    # Save to Firebase (same schema as admin dashboard)
    user_data = {
        "password": hashed_pw,
        "limit": DEFAULT_LIMIT,
        "usage": 0,
        "last_reset": datetime.now().strftime("%Y-%m-%d"),
    }

    ok = await fb_put(f"users/{new_user_id}", user_data)
    if not ok:
        await query.edit_message_text(
            "❌ Failed to save credentials. Please try again or contact admin."
        )
        return

    # Mark bot_user as generated
    await fb_patch(
        f"bot_users/{tid}",
        {"credentials_generated": True, "generated_user_id": new_user_id},
    )

    await query.edit_message_text(
        f"🎉 *Credentials Generated Successfully!*\n\n"
        f"🆔 *User ID:* `{new_user_id}`\n"
        f"🔑 *Password:* `{new_password}`\n\n"
        f"⚠️ *SAVE YOUR PASSWORD NOW!*\n"
        f"It is encrypted and *cannot be recovered*.\n\n"
        f"📊 Daily Limit: {DEFAULT_LIMIT}",
        parse_mode="Markdown",
    )


# ── Command: /mycreds ────────────────────────────────────────────────────────
async def mycreds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's generated credentials and current usage."""
    tid = str(update.effective_user.id)
    bot_user = await fb_get(f"bot_users/{tid}")

    if not bot_user or not bot_user.get("credentials_generated"):
        await update.message.reply_text(
            "❌ You don't have credentials yet.\nUse /start to begin."
        )
        return

    uid = bot_user.get("generated_user_id", "—")
    user_info = await fb_get(f"users/{uid}")

    if user_info:
        usage = user_info.get("usage", 0)
        limit = user_info.get("limit", DEFAULT_LIMIT)
        reset = user_info.get("last_reset", "N/A")
        await update.message.reply_text(
            f"📋 *Your Credentials*\n\n"
            f"🆔 *User ID:* `{uid}`\n"
            f"🔑 *Password:* Hidden (SHA-256 encrypted)\n\n"
            f"📊 *Usage:* {usage}/{limit}\n"
            f"📅 *Last Reset:* {reset}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"🆔 *User ID:* `{uid}`\n⚠️ Could not fetch usage info.",
            parse_mode="Markdown",
        )


# ── Command: /status ─────────────────────────────────────────────────────────
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral + channel progress."""
    user = update.effective_user
    tid = str(user.id)
    bot_user = await fb_get(f"bot_users/{tid}")

    if not bot_user:
        await update.message.reply_text("Use /start first!")
        return

    joined = await is_channel_member(context, user.id)
    ref_count = bot_user.get("referral_count", 0)
    creds = bot_user.get("credentials_generated", False)

    ch = "✅ Joined" if joined else "❌ Not Joined"
    rf = f"{'✅' if ref_count >= REQUIRED_REFERRALS else '❌'} {ref_count}/{REQUIRED_REFERRALS}"

    bot_me = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_me.username}?start=ref_{tid}"

    await update.message.reply_text(
        f"📊 *Your Status*\n\n"
        f"📢 Channel: {ch}\n"
        f"👥 Referrals: {rf}\n"
        f"🎫 Credentials: {'✅ Generated' if creds else '⏳ Pending'}\n\n"
        f"📎 *Referral Link:*\n`{ref_link}`",
        parse_mode="Markdown",
    )


# ── Command: /help ────────────────────────────────────────────────────────────
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📩 Contact Admin", url="https://t.me/Contira")]
    ]
    await update.message.reply_text(
        "🤖 *CheatMST Bot — Commands*\n\n"
        "/start  — Start & get your referral link\n"
        "/status — Check your progress\n"
        "/mycreds — View your credentials & usage\n"
        "/help   — Show this message\n\n"
        "*How it works:*\n"
        f"1️⃣ Join {CHANNEL_USERNAME}\n"
        f"2️⃣ Refer {REQUIRED_REFERRALS} friends using your unique link\n"
        "3️⃣ Tap *Get My Credentials* to receive your free ID & password 🎉",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Command: /buy ─────────────────────────────────────────────────────────────
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redirect user to admin for purchasing credits / full version."""
    keyboard = [
        [InlineKeyboardButton("💎 Contact Admin to Buy", url="https://t.me/Contira")]
    ]
    await update.message.reply_text(
        "💎 *Upgrade to Full Version*\n\n"
        "🔓 Unlimited daily usage\n"
        "⚡ Priority support\n"
        "🔑 Custom credentials\n\n"
        "Tap the button below to contact the admin and purchase credits!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ─── Health Check Server (for Render) ─────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress noisy logs


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("🌐 Health server on port %s", port)
    server.serve_forever()


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN is missing! Set it in .env")
        return

    # Start health check server in background (Render needs an open port)
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("mycreds", mycreds))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CallbackQueryHandler(cb_check_status, pattern="^check_status$"))
    app.add_handler(CallbackQueryHandler(cb_generate_creds, pattern="^generate_creds$"))

    logger.info("🤖 CheatMST Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
