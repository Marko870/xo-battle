import logging
import os
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIG ──
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
WEBAPP_URL   = os.environ.get("WEBAPP_URL", "https://Marko870.github.io/xo-battle")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://tigiprpkkchufrzjhtsb.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_KEY")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "565781136"))

USDT_ADDRESS  = "0xd29c0f1d945a650b0c8158396682c56f586af13e"
SHAMCASH_NUM  = "4d20723d3c4ffb59473370ab4e3fedd4"
MATCH_FEE     = 1.0   # رسوم المباراة
WINNER_PRIZE  = 1.5   # جائزة الفائز
DRAW_REFUND   = 0.75  # إرجاع عند التعادل
MIN_DEPOSIT   = 2.0   # حد أدنى للشحن

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Server وهمي لـ Render ──
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
    def log_message(self, *args): pass

threading.Thread(target=lambda: HTTPServer(('0.0.0.0', 10000), Handler).serve_forever(), daemon=True).start()

# ── HELPERS ──
def is_admin(uid): return uid == ADMIN_ID

async def get_balance(uid: str) -> float:
    res = sb.from_("balances").select("balance").eq("telegram_id", uid).execute()
    return float(res.data[0]["balance"]) if res.data else 0.0

async def add_balance(uid: str, name: str, amount: float, desc: str):
    # upsert balance
    res = sb.from_("balances").select("balance").eq("telegram_id", uid).execute()
    if res.data:
        new_bal = float(res.data[0]["balance"]) + amount
        sb.from_("balances").update({"balance": new_bal, "name": name}).eq("telegram_id", uid).execute()
    else:
        sb.from_("balances").insert({"telegram_id": uid, "name": name, "balance": amount}).execute()
    # log transaction
    sb.from_("transactions").insert({
        "telegram_id": uid, "name": name,
        "type": "credit", "amount": amount, "description": desc
    }).execute()

async def deduct_balance(uid: str, name: str, amount: float, desc: str):
    res = sb.from_("balances").select("balance").eq("telegram_id", uid).execute()
    if res.data:
        new_bal = float(res.data[0]["balance"]) - amount
        sb.from_("balances").update({"balance": new_bal}).eq("telegram_id", uid).execute()
    sb.from_("transactions").insert({
        "telegram_id": uid, "name": name,
        "type": "debit", "amount": amount, "description": desc
    }).execute()

# ── MATCHMAKING ──
async def try_match(app):
    queue = sb.from_("waiting_queue").select("*").order("joined_at").execute()
    if len(queue.data) >= 2:
        p1 = queue.data[0]
        p2 = queue.data[1]
        # احذف من القائمة
        sb.from_("waiting_queue").delete().in_("telegram_id", [p1["telegram_id"], p2["telegram_id"]]).execute()
        # أنشئ غرفة في Supabase
        import random
        room_id = str(random.randint(1000, 9999))
        sb.from_("rooms").insert({
            "id": room_id,
            "player_x_id": p1["telegram_id"],
            "player_x_name": p1["name"],
            "player_o_id": p2["telegram_id"],
            "player_o_name": p2["name"],
            "board": "---------",
            "current_turn": "X",
            "status": "playing"
        }).execute()
        # أرسل للاعبين
        webapp_url = f"{WEBAPP_URL}?room={room_id}"
        for player, mark in [(p1, "❌"), (p2, "⭕")]:
            keyboard = [[InlineKeyboardButton("🎮 ابدأ اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
            try:
                await app.bot.send_message(
                    chat_id=int(player["telegram_id"]),
                    text=f"🎮 *وجدنا لك خصم!*\n\n"
                         f"أنت: {mark}\n"
                         f"كود الغرفة: `{room_id}`\n\n"
                         f"افتح اللعبة وادخل الكود 👇",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Match notify error: {e}")

# ── COMMANDS ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name

    # تسجيل اللاعب إذا جديد
    res = sb.from_("balances").select("telegram_id").eq("telegram_id", uid).execute()
    if not res.data:
        sb.from_("balances").insert({"telegram_id": uid, "name": name, "balance": 0}).execute()

    bal = await get_balance(uid)
    keyboard = [
        [InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("💰 رصيدي", callback_data="balance"),
         InlineKeyboardButton("💳 شحن", callback_data="deposit")],
        [InlineKeyboardButton("⚔️ العب الآن", callback_data="play"),
         InlineKeyboardButton("📊 إحصائياتي", callback_data="stats")],
        [InlineKeyboardButton("🏆 المتصدرون", callback_data="top")]
    ]
    await update.message.reply_text(
        f"🎮 *أهلاً {name}!*\n\n"
        f"💰 رصيدك الحالي: *{bal:.2f}$*\n\n"
        "اختار من القائمة 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    bal = await get_balance(uid)
    await update.message.reply_text(
        f"💰 *رصيدك الحالي:* `{bal:.2f}$`\n\n"
        f"رسوم المباراة: `{MATCH_FEE}$`\n"
        f"جائزة الفوز: `{WINNER_PRIZE}$`\n\n"
        "لشحن الرصيد: /deposit",
        parse_mode="Markdown"
    )

async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💵 USDT (BEP20)", callback_data="dep_usdt")],
        [InlineKeyboardButton("📱 شام كاش", callback_data="dep_shamcash")]
    ]
    await update.message.reply_text(
        f"💳 *شحن الرصيد*\n\n"
        f"الحد الأدنى: `{MIN_DEPOSIT}$`\n\n"
        "اختار طريقة الدفع 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def play_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name
    bal = await get_balance(uid)

    if bal < MATCH_FEE:
        await update.message.reply_text(
            f"❌ *رصيدك غير كافٍ!*\n\n"
            f"رصيدك: `{bal:.2f}$`\n"
            f"رسوم المباراة: `{MATCH_FEE}$`\n\n"
            "اشحن رصيدك أولاً: /deposit",
            parse_mode="Markdown"
        )
        return

    # تحقق إذا بالانتظار
    in_queue = sb.from_("waiting_queue").select("telegram_id").eq("telegram_id", uid).execute()
    if in_queue.data:
        await update.message.reply_text("⏳ أنت بالفعل في قائمة الانتظار!")
        return

    # خصم الرسوم وأضف للانتظار
    await deduct_balance(uid, name, MATCH_FEE, "رسوم مباراة XO")
    sb.from_("waiting_queue").insert({"telegram_id": uid, "name": name}).execute()

    await update.message.reply_text(
        f"✅ *تم خصم {MATCH_FEE}$*\n"
        f"رصيدك الجديد: `{bal - MATCH_FEE:.2f}$`\n\n"
        "⏳ ننتظر خصم لك... سيتم إشعارك فوراً! 🎮",
        parse_mode="Markdown"
    )

    # جرب المطابقة
    await try_match(context.application)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    # إحصائيات
    p_res = sb.from_("players").select("*").eq("telegram_id", uid).execute()
    b_res = sb.from_("balances").select("*").eq("telegram_id", uid).execute()

    if not b_res.data:
        await update.message.reply_text("❌ ما سجلت بعد! اكتب /start")
        return

    b = b_res.data[0]
    p = p_res.data[0] if p_res.data else {"wins":0,"losses":0,"draws":0}
    total = p["wins"] + p["losses"] + p["draws"]
    rate = round(p["wins"]/total*100) if total > 0 else 0

    await update.message.reply_text(
        f"📊 *إحصائياتك:*\n\n"
        f"💰 الرصيد: `{float(b['balance']):.2f}$`\n"
        f"📥 مجموع الشحن: `{float(b.get('total_deposited',0)):.2f}$`\n\n"
        f"🏆 انتصارات: `{p['wins']}`\n"
        f"😔 خسارات: `{p['losses']}`\n"
        f"🤝 تعادلات: `{p['draws']}`\n"
        f"🎮 مجموع: `{total}`\n"
        f"📈 نسبة الفوز: `{rate}%`",
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
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *طريقة اللعب:*\n\n"
        "1️⃣ اشحن رصيدك: /deposit\n"
        "2️⃣ اضغط /play للدخول للانتظار\n"
        "3️⃣ لما يتجمع خصم، بتوصلك رسالة\n"
        "4️⃣ افتح اللعبة وادخل الكود\n"
        "5️⃣ العب واربح! 🏆\n\n"
        f"💰 رسوم المباراة: `{MATCH_FEE}$`\n"
        f"🏆 جائزة الفوز: `{WINNER_PRIZE}$`\n"
        f"🤝 إرجاع عند التعادل: `{DRAW_REFUND}$`\n\n"
        "📋 *الأوامر:*\n"
        "/start — القائمة الرئيسية\n"
        "/balance — رصيدك\n"
        "/deposit — شحن الرصيد\n"
        "/play — العب الآن\n"
        "/stats — إحصائياتك\n"
        "/top — المتصدرون",
        parse_mode="Markdown"
    )

# ── ADMIN COMMANDS ──
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ ليس لديك صلاحية!")
        return
    await update.message.reply_text(
        "🛡️ *لوحة الأدمن:*\n\n"
        "/confirm [ID] [مبلغ] — تأكيد شحن\n"
        "/reject [ID] — رفض شحن\n"
        "/addbalance [user_id] [مبلغ] — إضافة رصيد\n"
        "/allplayers — كل اللاعبين\n"
        "/pending — طلبات الشحن المعلقة\n"
        "/queue — قائمة الانتظار",
        parse_mode="Markdown"
    )

async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("الاستخدام: /confirm [ID] [مبلغ]")
        return
    dep_id, amount = args[0], float(args[1])
    dep = sb.from_("deposits").select("*").eq("id", dep_id).execute()
    if not dep.data:
        await update.message.reply_text("❌ طلب غير موجود!")
        return
    d = dep.data[0]
    sb.from_("deposits").update({"status": "confirmed", "amount": amount}).eq("id", dep_id).execute()
    await add_balance(d["telegram_id"], d["name"], amount, f"شحن مؤكد #{dep_id}")
    # إشعار اللاعب
    try:
        await context.bot.send_message(
            chat_id=int(d["telegram_id"]),
            text=f"✅ *تم شحن رصيدك!*\n\n"
                 f"المبلغ: `{amount}$`\n"
                 f"رصيدك الجديد: `{await get_balance(d['telegram_id']):.2f}$`\n\n"
                 "اكتب /play للعب الآن! 🎮",
            parse_mode="Markdown"
        )
    except: pass
    await update.message.reply_text(f"✅ تم تأكيد الشحن #{dep_id} بمبلغ {amount}$")

async def reject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args:
        await update.message.reply_text("الاستخدام: /reject [ID]")
        return
    dep_id = args[0]
    dep = sb.from_("deposits").select("*").eq("id", dep_id).execute()
    if not dep.data:
        await update.message.reply_text("❌ طلب غير موجود!")
        return
    d = dep.data[0]
    sb.from_("deposits").update({"status": "rejected"}).eq("id", dep_id).execute()
    try:
        await context.bot.send_message(
            chat_id=int(d["telegram_id"]),
            text="❌ *تم رفض طلب الشحن*\n\nتواصل مع الدعم إذا كان هناك خطأ.",
            parse_mode="Markdown"
        )
    except: pass
    await update.message.reply_text(f"✅ تم رفض الطلب #{dep_id}")

async def addbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("الاستخدام: /addbalance [user_id] [مبلغ]")
        return
    uid, amount = args[0], float(args[1])
    res = sb.from_("balances").select("name").eq("telegram_id", uid).execute()
    name = res.data[0]["name"] if res.data else "مجهول"
    await add_balance(uid, name, amount, "إضافة يدوية من الأدمن")
    await update.message.reply_text(f"✅ تم إضافة {amount}$ للاعب {uid}")

async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    res = sb.from_("deposits").select("*").eq("status", "pending").order("created_at").execute()
    if not res.data:
        await update.message.reply_text("✅ لا يوجد طلبات معلقة")
        return
    text = "📋 *طلبات الشحن المعلقة:*\n\n"
    for d in res.data:
        text += f"🔹 ID: `{d['id']}` | {d['name']} | {d['method']}\n"
        text += f"   للتأكيد: `/confirm {d['id']} [مبلغ]`\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def allplayers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    res = sb.from_("balances").select("*").order("balance", desc=True).execute()
    if not res.data:
        await update.message.reply_text("لا يوجد لاعبون")
        return
    text = "👥 *كل اللاعبين:*\n\n"
    for p in res.data[:15]:
        text += f"• {p['name']} — `{float(p['balance']):.2f}$`\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    res = sb.from_("waiting_queue").select("*").order("joined_at").execute()
    if not res.data:
        await update.message.reply_text("✅ قائمة الانتظار فارغة")
        return
    text = "⏳ *قائمة الانتظار:*\n\n"
    for p in res.data:
        text += f"• {p['name']} (`{p['telegram_id']}`)\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ── CALLBACKS (الأزرار) ──
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    uid = str(user.id)
    name = user.first_name

    if data == "balance":
        bal = await get_balance(uid)
        await query.message.reply_text(f"💰 *رصيدك:* `{bal:.2f}$`", parse_mode="Markdown")

    elif data == "deposit":
        keyboard = [
            [InlineKeyboardButton("💵 USDT (BEP20)", callback_data="dep_usdt")],
            [InlineKeyboardButton("📱 شام كاش", callback_data="dep_shamcash")]
        ]
        await query.message.reply_text(
            f"💳 *شحن الرصيد*\n\nالحد الأدنى: `{MIN_DEPOSIT}$`\n\nاختار طريقة الدفع:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data == "dep_usdt":
        context.user_data["deposit_method"] = "USDT"
        await query.message.reply_text(
            f"💵 *شحن عبر USDT (BEP20)*\n\n"
            f"العنوان:\n`{USDT_ADDRESS}`\n\n"
            f"الحد الأدنى: `{MIN_DEPOSIT}$`\n\n"
            "بعد التحويل، أرسل صورة الإيصال هون 👇",
            parse_mode="Markdown"
        )

    elif data == "dep_shamcash":
        context.user_data["deposit_method"] = "شام كاش"
        await query.message.reply_text(
            f"📱 *شحن عبر شام كاش*\n\n"
            f"رقم المحفظة:\n`{SHAMCASH_NUM}`\n\n"
            f"الحد الأدنى: `{MIN_DEPOSIT}$`\n\n"
            "بعد التحويل، أرسل صورة الإيصال هون 👇",
            parse_mode="Markdown"
        )

    elif data == "play":
        bal = await get_balance(uid)
        if bal < MATCH_FEE:
            await query.message.reply_text(
                f"❌ رصيدك غير كافٍ!\nرصيدك: `{bal:.2f}$`\nاشحن: /deposit",
                parse_mode="Markdown"
            )
            return
        in_queue = sb.from_("waiting_queue").select("telegram_id").eq("telegram_id", uid).execute()
        if in_queue.data:
            await query.message.reply_text("⏳ أنت بالفعل في قائمة الانتظار!")
            return
        await deduct_balance(uid, name, MATCH_FEE, "رسوم مباراة XO")
        sb.from_("waiting_queue").insert({"telegram_id": uid, "name": name}).execute()
        await query.message.reply_text(
            f"✅ تم خصم `{MATCH_FEE}$`\n⏳ ننتظر خصم... سيتم إشعارك فوراً! 🎮",
            parse_mode="Markdown"
        )
        await try_match(context.application)

    elif data == "stats":
        await stats_cmd(update, context)

    elif data == "top":
        await top_cmd(update, context)

# ── استقبال صورة الإيصال ──
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name
    method = context.user_data.get("deposit_method", "غير محدد")

    photo_id = update.message.photo[-1].file_id
    res = sb.from_("deposits").insert({
        "telegram_id": uid, "name": name,
        "method": method, "receipt_file_id": photo_id,
        "status": "pending", "amount": 0
    }).execute()

    dep_id = res.data[0]["id"]
    context.user_data.pop("deposit_method", None)

    await update.message.reply_text(
        f"✅ *تم استلام الإيصال!*\n\n"
        f"رقم الطلب: `#{dep_id}`\n"
        f"الطريقة: {method}\n\n"
        "سيتم التحقق خلال 30 دقيقة وسيصلك إشعار 🔔",
        parse_mode="Markdown"
    )

    # إشعار الأدمن
    try:
        await context.bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💳 *طلب شحن جديد!*\n\n"
                 f"اللاعب: {name} (`{uid}`)\n"
                 f"الطريقة: {method}\n"
                 f"رقم الطلب: `#{dep_id}`\n\n"
                 f"للتأكيد: `/confirm {dep_id} [المبلغ]`\n"
                 f"للرفض: `/reject {dep_id}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")

# ── نظام إشعار الفوز ──
async def check_new_results(app):
    last_id = 0
    while True:
        try:
            res = sb.from_("results").select("*").gt("id", last_id).order("id").execute()
            for r in res.data:
                last_id = r["id"]
                if not r["draw"] and r["winner_id"] and r["loser_id"]:
                    # إضافة الجائزة للفائز
                    win_res = sb.from_("balances").select("name").eq("telegram_id", r["winner_id"]).execute()
                    win_name = win_res.data[0]["name"] if win_res.data else r["winner_name"]
                    await add_balance(r["winner_id"], win_name, WINNER_PRIZE, f"جائزة فوز - غرفة {r['room_id']}")
                    # إشعار الفائز
                    new_bal = await get_balance(r["winner_id"])
                    try:
                        await app.bot.send_message(
                            chat_id=int(r["winner_id"]),
                            text=f"🏆 *مبروك فزت!*\n\n"
                                 f"الجائزة: `+{WINNER_PRIZE}$`\n"
                                 f"رصيدك الجديد: `{new_bal:.2f}$`\n\n"
                                 "العب مرة ثانية: /play 🎮",
                            parse_mode="Markdown"
                        )
                    except: pass
                    # إشعار الخاسر
                    try:
                        await app.bot.send_message(
                            chat_id=int(r["loser_id"]),
                            text=f"😔 *خسرت هالمرة!*\n\n"
                                 f"فاز عليك *{r['winner_name']}*\n"
                                 "حاول مرة ثانية: /play 💪",
                            parse_mode="Markdown"
                        )
                    except: pass
                elif r["draw"]:
                    # إرجاع عند التعادل
                    for uid, uname in [(r["winner_id"], r["winner_name"]), (r["loser_id"], r["loser_name"])]:
                        if uid:
                            await add_balance(uid, uname, DRAW_REFUND, f"إرجاع تعادل - غرفة {r['room_id']}")
                            try:
                                await app.bot.send_message(
                                    chat_id=int(uid),
                                    text=f"🤝 *تعادل!*\n\n"
                                         f"تم إرجاع: `{DRAW_REFUND}$`\n\n"
                                         "العب مرة ثانية: /play 🎮",
                                    parse_mode="Markdown"
                                )
                            except: pass
        except Exception as e:
            logger.error(f"Results check error: {e}")
        await asyncio.sleep(3)

async def post_init(app):
    asyncio.create_task(check_new_results(app))

# ── MAIN ──
if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("play", play_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # أدمن
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance_cmd))
    app.add_handler(CommandHandler("allplayers", allplayers_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    # callbacks وصور
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot running...")
    app.run_polling()

