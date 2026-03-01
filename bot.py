import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8279777160:AAElFTqwzh1m-8iJ1SX5M6ryRKFnvhx6p1Q")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://marko870.github.io/xo-battle/")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"🎮 *أهلاً {user.first_name}!*\n\n"
        "مرحباً بك في *XO Battle* ⚡\n\n"
        "اضغط الزر لفتح اللعبة:\n"
        "• العب محلياً مع شخص بجانبك\n"
        "• أو أنشئ غرفة وشارك الكود مع صديقك",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        "📖 *كيف تلعب:*\n\n"
        "1️⃣ اضغط 'افتح اللعبة'\n"
        "2️⃣ اختار:\n"
        "   • *لعب محلي* — لاعبين على نفس الجهاز\n"
        "   • *إنشاء غرفة* — شارك الكود مع صديقك\n"
        "   • *انضم لغرفة* — ادخل كود صديقك\n\n"
        "3️⃣ العب واربح! 🏆",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    logger.info("Bot running...")
    app.run_polling()
