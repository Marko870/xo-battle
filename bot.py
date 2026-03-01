import logging
import os
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
WEBAPP_URL  = os.environ.get("WEBAPP_URL", "https://Marko870.github.io/xo-battle")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://tigiprpkkchufrzjhtsb.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRpZ2lwcnBra2NodWZyempodHNiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzIzOTA2NTMsImV4cCI6MjA4Nzk2NjY1M30.wXJjrErqET_ZqP2WoJ0rQ2Oly38vdZJWH2fAK8drbRs")

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Server وهمي لـ Render ──
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *args): pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', 10000), Handler).serve_forever(), daemon=True).start()

# ── إشعار الفوز ──
async def notify_winner(app, winner_id: str, winner_name: str, loser_name: str, room_id: str):
    try:
        await app.bot.send_message(
            chat_id=int(winner_id),
            text=f"🏆 *مبروك {winner_name}!*\n\n"
                 f"فزت على *{loser_name}* في غرفة `{room_id}` 🎉\n\n"
                 f"تحقق من إحصائياتك بكتابة /stats",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Could not notify winner {winner_id}: {e}")

async def notify_loser(app, loser_id: str, loser_name: str, winner_name: str):
    try:
        await app.bot.send_message(
            chat_id=int(loser_id),
            text=f"😔 *{loser_name}* خسرت هالمرة\n\n"
                 f"فاز عليك *{winner_name}*\n"
                 f"حاول مرة ثانية! 💪",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Could not notify loser {loser_id}: {e}")

# ── مراقبة نتائج جديدة ──
async def check_new_results(app):
    last_id = 0
    while True:
        try:
            res = sb.from_("results").select("*").gt("id", last_id).order("id").execute()
            for r in res.data:
                last_id = r["id"]
                if not r["draw"] and r["winner_id"] and r["loser_id"]:
                    await notify_winner(app, r["winner_id"], r["winner_name"], r["loser_name"], r["room_id"])
                    await notify_loser(app, r["loser_id"], r["loser_name"], r["winner_name"])
        except Exception as e:
            logger.error(f"Check results error: {e}")
        await asyncio.sleep(3)

# ── Commands ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"🎮 *أهلاً {user.first_name}!*\n\n"
        "مرحباً بك في *XO Battle* ⚡\n\n"
        "• العب أونلاين ضد أصدقائك\n"
        "• انظر إحصائياتك بـ /stats\n"
        "• المتصدرون بـ /top",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    res = sb.from_("players").select("*").eq("telegram_id", uid).execute()
    if not res.data:
        await update.message.reply_text("❌ ما لعبت بعد! افتح اللعبة أولاً 🎮")
        return
    p = res.data[0]
    total = p['wins'] + p['losses'] + p['draws']
    rate = round(p['wins']/total*100) if total > 0 else 0
    await update.message.reply_text(
        f"📊 *إحصائياتك يا {p['name']}:*\n\n"
        f"🏆 انتصارات: *{p['wins']}*\n"
        f"😔 خسارات: *{p['losses']}*\n"
        f"🤝 تعادلات: *{p['draws']}*\n"
        f"🎮 مجموع: *{total}*\n"
        f"📈 نسبة الفوز: *{rate}%*",
        parse_mode="Markdown"
    )

async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = sb.from_("players").select("*").order("wins", desc=True).limit(5).execute()
    if not res.data:
        await update.message.reply_text("لا يوجد لاعبون بعد!")
        return
    medals = ['🥇','🥈','🥉','4️⃣','5️⃣']
    text = "🏆 *المتصدرون:*\n\n"
    for i, p in enumerate(res.data):
        text += f"{medals[i]} *{p['name']}* — {p['wins']} فوز\n"
    keyboard = [[InlineKeyboardButton("🎮 العب الآن", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def post_init(app):
    asyncio.create_task(check_new_results(app))

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    logger.info("Bot running...")
    app.run_polling()
