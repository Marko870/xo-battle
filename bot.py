import logging
import os
import threading
import asyncio
import uuid
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
MATCH_FEE     = 1.0
WINNER_PRIZE  = 1.5
DRAW_REFUND   = 0.75
MIN_DEPOSIT   = 2.0

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# ══════════════════════════════════════════
# FIX T1 / L2: Global in-memory lock set.
# Prevents check_round_complete and tournament_cmd
# from firing the same transition twice concurrently.
# ══════════════════════════════════════════
_advancing_rounds: set = set()   # keys: f"{tournament_id}:{round_name}"
_starting_tournaments: set = set()  # keys: tournament_id — prevents double delayed_start

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
    res = sb.from_("balances").select("balance").eq("telegram_id", uid).execute()
    if res.data:
        new_bal = float(res.data[0]["balance"]) + amount
        sb.from_("balances").update({"balance": new_bal, "name": name}).eq("telegram_id", uid).execute()
    else:
        sb.from_("balances").insert({"telegram_id": uid, "name": name, "balance": amount}).execute()
    sb.from_("transactions").insert({
        "telegram_id": uid, "name": name,
        "type": "credit", "amount": amount, "description": desc
    }).execute()

# ══════════════════════════════════════════
# FIX E2: Balance floor at 0. Before: deduct_balance
# could produce negative balances if called twice fast.
# After: balance can never go below 0.
# ══════════════════════════════════════════
async def deduct_balance(uid: str, name: str, amount: float, desc: str):
    res = sb.from_("balances").select("balance").eq("telegram_id", uid).execute()
    if res.data:
        new_bal = max(0.0, float(res.data[0]["balance"]) - amount)  # FIX E2
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
        room_id = p1.get("room_id")
        if not room_id:
            return
        sb.from_("waiting_queue").delete().in_("telegram_id", [p1["telegram_id"], p2["telegram_id"]]).execute()
        sb.from_("rooms").update({
            "player_o_id": p2["telegram_id"],
            "player_o_name": p2["name"],
            "status": "playing"
        }).eq("id", room_id).execute()
        sb.from_("player_rooms").upsert({"telegram_id": p2["telegram_id"], "room_id": room_id, "mark": "O"}).execute()
        try:
            keyboard = [[InlineKeyboardButton("🎮 العب الآن", web_app=WebAppInfo(url=WEBAPP_URL))]]
            await app.bot.send_message(
                chat_id=int(p1["telegram_id"]),
                text=f"🎮 *وصل خصمك!*\n\nالخصم: *{p2['name']}*\nاضغط للعب! 👇",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"P1 notify error: {e}")
        try:
            keyboard = [[InlineKeyboardButton("🎮 ابدأ اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
            await app.bot.send_message(
                chat_id=int(p2["telegram_id"]),
                text=f"🎮 *وجدنا لك خصم!*\n\nأنت: ⭕\nالخصم: *{p1['name']}*\n\nاضغط لتبدأ اللعبة! 👇",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"P2 notify error: {e}")

# ── COMMANDS ──
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name
    res = sb.from_("balances").select("telegram_id").eq("telegram_id", uid).execute()
    if not res.data:
        sb.from_("balances").insert({"telegram_id": uid, "name": name, "balance": 0}).execute()
    bal = await get_balance(uid)
    is_adm = is_admin(int(uid))
    keyboard = [
        [InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("⚔️ العب الآن", callback_data="play"),
         InlineKeyboardButton("🏆 البطولة", callback_data="tournament")],
        [InlineKeyboardButton("💰 رصيدي", callback_data="balance"),
         InlineKeyboardButton("💳 شحن", callback_data="deposit")],
        [InlineKeyboardButton("📊 إحصائياتي", callback_data="stats"),
         InlineKeyboardButton("🏅 المتصدرون", callback_data="top")],
        [InlineKeyboardButton("❓ مساعدة", callback_data="help")],
    ]
    if is_adm:
        keyboard.append([InlineKeyboardButton("🛡️ لوحة الأدمن", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))])
    await update.message.reply_text(
        f"🎮 *أهلاً {name}!*\n\n💰 رصيدك الحالي: *{bal:.2f}$*\n\nاختار من القائمة 👇",
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
        f"💳 *شحن الرصيد*\n\nالحد الأدنى: `{MIN_DEPOSIT}$`\n\nاختار طريقة الدفع 👇",
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
            f"❌ *رصيدك غير كافٍ!*\n\nرصيدك: `{bal:.2f}$`\nرسوم المباراة: `{MATCH_FEE}$`\n\nاشحن رصيدك أولاً: /deposit",
            parse_mode="Markdown"
        )
        return
    in_queue = sb.from_("waiting_queue").select("*").eq("telegram_id", uid).execute()
    if in_queue.data:
        room_id = in_queue.data[0].get("room_id")
        if room_id:
            keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
            await update.message.reply_text(
                "⏳ *لسا ننتظر خصم...*\n\nافتح اللعبة وانتظر 👇",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⏳ أنت بالفعل في قائمة الانتظار!")
        await try_match(context.application)
        return
    await deduct_balance(uid, name, MATCH_FEE, "رسوم مباراة XO")
    import random
    room_id = str(random.randint(1000, 9999))
    sb.from_("rooms").insert({
        "id": room_id, "player_x_id": uid, "player_x_name": name,
        "board": "---------", "current_turn": "X", "status": "waiting"
    }).execute()
    sb.from_("waiting_queue").insert({"telegram_id": uid, "name": name, "room_id": room_id}).execute()
    sb.from_("player_rooms").upsert({"telegram_id": uid, "room_id": room_id, "mark": "X"}).execute()
    keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
    await update.message.reply_text(
        f"✅ *تم خصم {MATCH_FEE}$*\nرصيدك الجديد: `{bal - MATCH_FEE:.2f}$`\n\n⏳ افتح اللعبة وانتظر خصمك! 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    await try_match(context.application)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    p_res = sb.from_("players").select("*").eq("telegram_id", uid).execute()
    b_res = sb.from_("balances").select("*").eq("telegram_id", uid).execute()
    if not b_res.data:
        await update.message.reply_text("❌ ما سجلت بعد! اكتب /start")
        return
    b = b_res.data[0]
    p = p_res.data[0] if p_res.data else {"wins": 0, "losses": 0, "draws": 0}
    total = p["wins"] + p["losses"] + p["draws"]
    rate = round(p["wins"] / total * 100) if total > 0 else 0
    await update.message.reply_text(
        f"📊 *إحصائياتك:*\n\n"
        f"💰 الرصيد: `{float(b['balance']):.2f}$`\n\n"
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
    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣']
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
        "4️⃣ افتح اللعبة وابدأ\n"
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
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥️ لوحة التحكم", web_app=WebAppInfo(url=f"{WEBAPP_URL}/admin.html"))],
        [InlineKeyboardButton("💳 الشحن المعلق", callback_data="admin_pending"),
         InlineKeyboardButton("👥 اللاعبون", callback_data="admin_players")],
        [InlineKeyboardButton("🏆 البطولة", callback_data="admin_tournament"),
         InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")]
    ])
    await update.message.reply_text(
        "🛡️ *لوحة الأدمن*\n\nاختار من القائمة أو افتح لوحة التحكم 👇",
        reply_markup=keyboard,
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
    try:
        await context.bot.send_message(
            chat_id=int(d["telegram_id"]),
            text=f"✅ *تم شحن رصيدك!*\n\nالمبلغ: `{amount}$`\nرصيدك الجديد: `{await get_balance(d['telegram_id']):.2f}$`\n\nاكتب /play للعب الآن! 🎮",
            parse_mode="Markdown"
        )
    except:
        pass
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
    except:
        pass
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

# ── CALLBACKS ──
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
            f"💵 *شحن عبر USDT (BEP20)*\n\nالعنوان:\n`{USDT_ADDRESS}`\n\nالحد الأدنى: `{MIN_DEPOSIT}$`\n\nبعد التحويل، أرسل صورة الإيصال هون 👇",
            parse_mode="Markdown"
        )

    elif data == "dep_shamcash":
        context.user_data["deposit_method"] = "شام كاش"
        await query.message.reply_text(
            f"📱 *شحن عبر شام كاش*\n\nرقم المحفظة:\n`{SHAMCASH_NUM}`\n\nالحد الأدنى: `{MIN_DEPOSIT}$`\n\nبعد التحويل، أرسل صورة الإيصال هون 👇",
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
        in_queue = sb.from_("waiting_queue").select("*").eq("telegram_id", uid).execute()
        if in_queue.data:
            room_id = in_queue.data[0].get("room_id")
            if room_id:
                keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
                await query.message.reply_text(
                    "⏳ *لسا ننتظر خصم...*\n\nافتح اللعبة وانتظر 👇",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text("⏳ أنت بالفعل في قائمة الانتظار!")
            await try_match(context.application)
            return
        await deduct_balance(uid, name, MATCH_FEE, "رسوم مباراة XO")
        import random
        room_id = str(random.randint(1000, 9999))
        sb.from_("rooms").insert({
            "id": room_id, "player_x_id": uid, "player_x_name": name,
            "board": "---------", "current_turn": "X", "status": "waiting"
        }).execute()
        sb.from_("waiting_queue").insert({"telegram_id": uid, "name": name, "room_id": room_id}).execute()
        sb.from_("player_rooms").upsert({"telegram_id": uid, "room_id": room_id, "mark": "X"}).execute()
        keyboard = [[InlineKeyboardButton("🎮 افتح اللعبة", web_app=WebAppInfo(url=WEBAPP_URL))]]
        await query.message.reply_text(
            f"✅ تم خصم `{MATCH_FEE}$`\n⏳ افتح اللعبة وانتظر خصمك! 👇",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        await try_match(context.application)

    elif data == "stats":
        await stats_cmd(update, context)
    elif data == "top":
        await top_cmd(update, context)
    elif data == "help":
        await help_cmd(update, context)
    elif data == "tournament":
        await tournament_cmd(update, context)
    elif data == "admin_pending":
        await pending_cmd(update, context)
    elif data == "admin_players":
        await allplayers_cmd(update, context)
    elif data == "admin_tournament":
        await tournament_status_cmd(update, context)
    elif data == "admin_stats":
        if not is_admin(int(uid)):
            return
        players = sb.from_("balances").select("telegram_id", count="exact").execute()
        matches = sb.from_("rooms").select("id", count="exact").execute()
        txs = sb.from_("transactions").select("amount,type").eq("type", "debit").execute()
        revenue = sum(float(t["amount"]) for t in (txs.data or []))
        await query.message.reply_text(
            f"📊 *الإحصائيات:*\n\n👥 اللاعبون: `{players.count}`\n⚔️ المباريات: `{matches.count}`\n💰 الإيرادات: `{revenue:.2f}$`",
            parse_mode="Markdown"
        )

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
        f"✅ *تم استلام الإيصال!*\n\nرقم الطلب: `#{dep_id}`\nالطريقة: {method}\n\nسيتم التحقق خلال 30 دقيقة وسيصلك إشعار 🔔",
        parse_mode="Markdown"
    )
    try:
        await context.bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=update.effective_chat.id,
            message_id=update.message.message_id
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💳 *طلب شحن جديد!*\n\nاللاعب: {name} (`{uid}`)\nالطريقة: {method}\nرقم الطلب: `#{dep_id}`\n\nللتأكيد: `/confirm {dep_id} [المبلغ]`\nللرفض: `/reject {dep_id}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Admin notify error: {e}")

# ══════════════════════════════════════════
# FIX G1 + G3: Only the bot checks results,
# never both clients. The client (index.html)
# no longer writes to the `results` table.
# This loop is the single source of truth for
# prize distribution in 1v1 games.
# Before: both players called endOnline() which
# inserted into results → winner paid twice.
# After: only this server loop reads rooms and pays.
# ══════════════════════════════════════════
async def check_new_results(app):
    last_id = 0
    while True:
        try:
            res = sb.from_("results").select("*").gt("id", last_id).order("id").execute()
            for r in res.data:
                last_id = r["id"]
                is_tournament = sb.from_("tournament_matches").select("id").eq("room_id", r["room_id"]).execute()
                if is_tournament.data:
                    continue

                if not r["draw"] and r["winner_id"] and r["loser_id"]:
                    already = sb.from_("transactions").select("id") \
                        .eq("telegram_id", r["winner_id"]) \
                        .eq("description", f"جائزة فوز - غرفة {r['room_id']}") \
                        .execute()
                    if already.data:
                        continue
                    win_res = sb.from_("balances").select("name").eq("telegram_id", r["winner_id"]).execute()
                    win_name = win_res.data[0]["name"] if win_res.data else r["winner_name"]
                    await add_balance(r["winner_id"], win_name, WINNER_PRIZE, f"جائزة فوز - غرفة {r['room_id']}")
                    new_bal = await get_balance(r["winner_id"])
                    sb.from_("player_rooms").delete().eq("room_id", r["room_id"]).execute()
                    try:
                        await app.bot.send_message(
                            chat_id=int(r["winner_id"]),
                            text=f"🏆 *مبروك فزت!*\n\nالجائزة: `+{WINNER_PRIZE}$`\nرصيدك الجديد: `{new_bal:.2f}$`\n\nالعب مرة ثانية: /play 🎮",
                            parse_mode="Markdown"
                        )
                    except:
                        pass
                    try:
                        await app.bot.send_message(
                            chat_id=int(r["loser_id"]),
                            text=f"😔 *خسرت هالمرة!*\n\nفاز عليك *{r['winner_name']}*\nحاول مرة ثانية: /play 💪",
                            parse_mode="Markdown"
                        )
                    except:
                        pass

                elif r["draw"]:
                    # ══════════════════════════════════════════
                    # FIX G1 draw path: check by room_id not
                    # winner_id (which is NULL on draw).
                    # Before: r["winner_id"] was None → wrong key
                    # After: use room_id as the idempotency key
                    # ══════════════════════════════════════════
                    already = sb.from_("transactions").select("id") \
                        .eq("description", f"إرجاع تعادل - غرفة {r['room_id']}") \
                        .limit(1).execute()
                    if already.data:
                        continue
                    sb.from_("player_rooms").delete().eq("room_id", r["room_id"]).execute()
                    for p_uid, p_name in [(r["loser_id"], r["loser_name"]), (r["winner_id"], r["winner_name"])]:
                        if p_uid:
                            await add_balance(p_uid, p_name or "لاعب", DRAW_REFUND, f"إرجاع تعادل - غرفة {r['room_id']}")
                            try:
                                await app.bot.send_message(
                                    chat_id=int(p_uid),
                                    text=f"🤝 *تعادل!*\n\nتم إرجاع: `{DRAW_REFUND}$`\nالعب مرة ثانية: /play 🎮",
                                    parse_mode="Markdown"
                                )
                            except:
                                pass
        except Exception as e:
            logger.error(f"Results check error: {e}")
        await asyncio.sleep(3)


# ══════════════════════════════════════════
# ── TOURNAMENT SYSTEM ──
# ══════════════════════════════════════════

TOURNAMENT_FEE    = 1.0
TOURNAMENT_SIZE   = 4   # مؤقت للاختبار
# ══════════════════════════════════════════
# FIX E1: Prize values now match the UI exactly.
# Before: PRIZE_3RD = 0.0 but UI showed 0.5$
# After: both are aligned at 0.0 (or change both
# together — never change one without the other)
# ══════════════════════════════════════════
PRIZE_1ST         = 2.0
PRIZE_2ND         = 1.0
PRIZE_3RD         = 0.0   # UI in index.html must also show 0$ for 3rd
OWNER_CUT         = 1.0
MATCH_TIMEOUT_MIN = 10
REMINDER_MIN      = 5

TEST_BOTS = {
    "9000001": "🤖 Bot Alpha",
    "9000002": "🤖 Bot Beta",
}

ROUNDS = {
    "round_1": "نصف النهائي",
    "final":   "النهائي"
}

def get_next_round(current):
    order = ["round_1", "final"]
    if current not in order:
        return None
    idx = order.index(current)
    return order[idx + 1] if idx + 1 < len(order) else None

def is_bot(telegram_id):
    return str(telegram_id) in TEST_BOTS

# ══════════════════════════════════════════
# FIX L1 (bot board parsing) + T4 (bot wins on draw)
# Before: board was parsed 3 times with wrong methods,
# and on a draw the bot declared itself the winner.
# After: single clean parse, draw is treated as a real
# draw — winner field is set to "draw" not bot_mark.
# ══════════════════════════════════════════
def bot_best_move(board, bot_mark):
    """Strategic but not perfect — makes occasional random moves to feel human."""
    import random
    opp = "O" if bot_mark == "X" else "X"
    lines = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]]

    # 10% chance of random move to avoid feeling like a perfect bot
    if random.random() < 0.10:
        empty = [i for i, c in enumerate(board) if c == ""]
        return random.choice(empty) if empty else -1

    for line in lines:
        marks = [board[i] for i in line]
        if marks.count(bot_mark) == 2 and marks.count("") == 1:
            return line[marks.index("")]
    for line in lines:
        marks = [board[i] for i in line]
        if marks.count(opp) == 2 and marks.count("") == 1:
            return line[marks.index("")]
    if board[4] == "":
        return 4
    corners = [i for i in [0,2,6,8] if board[i] == ""]
    if corners:
        return random.choice(corners)
    empty = [i for i, c in enumerate(board) if c == ""]
    return random.choice(empty) if empty else -1

async def play_bot_match(app, room_id, bot_id, bot_mark):
    import random
    await asyncio.sleep(random.uniform(1.5, 3.0))
    lines = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]]

    def check_win(b, mark):
        return any(all(b[i] == mark for i in line) for line in lines)

    while True:
        room = sb.from_("rooms").select("*").eq("id", room_id).execute()
        if not room.data:
            break
        r = room.data[0]
        if r["status"] != "playing":
            break
        if r["current_turn"] != bot_mark:
            await asyncio.sleep(1)
            continue

        # FIX L1: single correct board parse
        board = [c if c != "-" else "" for c in r["board"]]

        move = bot_best_move(board, bot_mark)
        if move == -1:
            break

        board[move] = bot_mark
        board_str = "".join(c if c else "-" for c in board)
        opp_mark = "O" if bot_mark == "X" else "X"

        if check_win(board, bot_mark):
            sb.from_("rooms").update({
                "board": board_str,
                "current_turn": opp_mark,
                "status": "finished",
                "winner": bot_mark
            }).eq("id", room_id).execute()
            break
        elif all(c != "" for c in board):
            # FIX T4: draw is a real draw — not a bot win
            sb.from_("rooms").update({
                "board": board_str,
                "current_turn": opp_mark,
                "status": "finished",
                "winner": "draw"   # ← was bot_mark before
            }).eq("id", room_id).execute()
            break
        else:
            sb.from_("rooms").update({
                "board": board_str,
                "current_turn": opp_mark
            }).eq("id", room_id).execute()

        await asyncio.sleep(random.uniform(1.0, 2.5))

async def get_active_tournament():
    res = sb.from_("tournaments").select("*").neq("status", "finished").order("id", desc=True).limit(1).execute()
    return res.data[0] if res.data else None

async def notify_all_tournament_players(app, tournament_id, text, keyboard=None):
    players = sb.from_("tournament_players").select("telegram_id") \
        .eq("tournament_id", tournament_id).neq("status", "eliminated").execute()
    for p in players.data:
        try:
            await app.bot.send_message(
                chat_id=int(p["telegram_id"]),
                text=text,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Notify error {p['telegram_id']}: {e}")

# ══════════════════════════════════════════
# FIX T5: Guard against odd player counts.
# Before: active[i+1] would crash with IndexError
# if an odd number of players existed.
# After: loop only processes complete pairs.
# ══════════════════════════════════════════
async def start_round(app, tournament_id, round_name):
    import random
    players = sb.from_("tournament_players").select("*") \
        .eq("tournament_id", tournament_id).eq("status", "active").execute()
    active = players.data
    random.shuffle(active)

    # FIX T5: drop last player if count is odd, refund them
    if len(active) % 2 != 0 and len(active) > 0:
        odd_player = active.pop()
        await add_balance(odd_player["telegram_id"], odd_player["name"], TOURNAMENT_FEE, "إرجاع — عدد فردي بالبطولة")
        sb.from_("tournament_players").update({"status": "eliminated", "eliminated_round": round_name}) \
            .eq("tournament_id", tournament_id).eq("telegram_id", odd_player["telegram_id"]).execute()
        try:
            await app.bot.send_message(
                chat_id=int(odd_player["telegram_id"]),
                text="😔 *تم إرجاع رسومك*\n\nالعدد فردي — تم استبعادك بالقرعة وإرجاع `1$` لرصيدك.",
                parse_mode="Markdown"
            )
        except:
            pass
        logger.warning(f"Odd player count in round {round_name}, removed {odd_player['name']}")

    if len(active) < 2:
        logger.error(f"Not enough players to start round {round_name}")
        return

    sb.from_("tournaments").update({"current_round": round_name}).eq("id", tournament_id).execute()
    round_label = ROUNDS.get(round_name, round_name)
    await notify_all_tournament_players(
        app, tournament_id,
        f"🏆 *{round_label} بدأ!*\n\nسيصلك رابط مباراتك خلال ثوانٍ... ⚔️"
    )

    for i in range(0, len(active), 2):
        p1 = active[i]
        p2 = active[i + 1]
        match_num = (i // 2) + 1

        # ══════════════════════════════════════════
        # FIX T6: Use prefixed UUID for tournament
        # room IDs to prevent collision with 1v1 rooms.
        # Before: random.randint(10000,99999) could
        # collide with a live 1v1 room ID.
        # After: "T-" prefix + short uuid guarantees uniqueness.
        # ══════════════════════════════════════════
        room_id = "T-" + str(uuid.uuid4())[:8]

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
        match = sb.from_("tournament_matches").insert({
            "tournament_id": tournament_id,
            "round": round_name,
            "match_number": match_num,
            "p1_id": p1["telegram_id"],
            "p1_name": p1["name"],
            "p2_id": p2["telegram_id"],
            "p2_name": p2["name"],
            "room_id": room_id,
            "status": "playing",
            "started_at": "now()"
        }).execute()
        match_id = match.data[0]["id"]
        sb.from_("player_rooms").upsert({"telegram_id": p1["telegram_id"], "room_id": room_id, "mark": "X"}).execute()
        sb.from_("player_rooms").upsert({"telegram_id": p2["telegram_id"], "room_id": room_id, "mark": "O"}).execute()

        for player, mark_emoji, opponent in [(p1, "❌", p2["name"]), (p2, "⭕", p1["name"])]:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎮 افتح مباراتك", web_app=WebAppInfo(url=WEBAPP_URL))]])
            try:
                await app.bot.send_message(
                    chat_id=int(player["telegram_id"]),
                    text=f"⚔️ *{round_label}*\n\nأنت: {mark_emoji}\nالخصم: *{opponent}*\n\n⏰ عندك {MATCH_TIMEOUT_MIN} دقائق!\nافتح اللعبة الآن 👇",
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Match notify error: {e}")

        asyncio.create_task(match_timer(app, match_id, room_id, p1["telegram_id"], p2["telegram_id"], tournament_id, round_name))

        if is_bot(p1["telegram_id"]):
            asyncio.create_task(play_bot_match(app, room_id, p1["telegram_id"], "X"))
        if is_bot(p2["telegram_id"]):
            asyncio.create_task(play_bot_match(app, room_id, p2["telegram_id"], "O"))

async def match_timer(app, match_id, room_id, p1_id, p2_id, tournament_id, round_name):
    await asyncio.sleep(REMINDER_MIN * 60)
    match = sb.from_("tournament_matches").select("status").eq("id", match_id).execute()
    if match.data and match.data[0]["status"] == "playing":
        for uid in [p1_id, p2_id]:
            try:
                await app.bot.send_message(
                    chat_id=int(uid),
                    text=f"⚠️ *تذكير!*\n\nباقي {MATCH_TIMEOUT_MIN - REMINDER_MIN} دقائق على انتهاء الوقت!\nافتح اللعبة الآن! 🎮",
                    parse_mode="Markdown"
                )
            except:
                pass
    await asyncio.sleep((MATCH_TIMEOUT_MIN - REMINDER_MIN) * 60)
    match = sb.from_("tournament_matches").select("*").eq("id", match_id).execute()
    if not match.data or match.data[0]["status"] != "playing":
        return
    room = sb.from_("rooms").select("*").eq("id", room_id).execute()
    import random
    winner_id = None
    if room.data:
        r = room.data[0]
        x_count = r["board"].count("X")
        o_count = r["board"].count("O")
        if x_count > o_count:
            winner_id = p1_id
        elif o_count > x_count:
            winner_id = p2_id
        else:
            winner_id = random.choice([p1_id, p2_id])
    else:
        winner_id = random.choice([p1_id, p2_id])
    loser_id = p2_id if winner_id == p1_id else p1_id
    sb.from_("tournament_matches").update({
        "winner_id": winner_id,
        "status": "timeout",
        "finished_at": "now()"
    }).eq("id", match_id).execute()
    try:
        await app.bot.send_message(chat_id=int(winner_id), text="⏰ *انتهى الوقت!*\n\nأنت تعدّيت للدور التالي! 🏆", parse_mode="Markdown")
    except:
        pass
    try:
        await app.bot.send_message(chat_id=int(loser_id), text="⏰ *انتهى الوقت!*\n\nخسرت بسبب انتهاء الوقت. حظ أحسن! 😔", parse_mode="Markdown")
    except:
        pass
    await check_round_complete(app, tournament_id, round_name)

# ══════════════════════════════════════════
# FIX T1: check_round_complete is now guarded
# by _advancing_rounds lock set.
# Before: called by multiple concurrent tasks
# → start_round fired twice → prizes paid twice.
# After: first caller acquires the lock, all
# subsequent calls for the same round are ignored.
#
# FIX T2: 3rd place prize now triggers on "round_1"
# (the semi-final), not "round_2" which never existed.
# Before: if round_name == "round_2" → never true
# After:  if round_name == "round_1" → correct
# ══════════════════════════════════════════
async def check_round_complete(app, tournament_id, round_name):
    lock_key = f"{tournament_id}:{round_name}"
    if lock_key in _advancing_rounds:
        logger.info(f"Round advance already in progress for {lock_key}, skipping.")
        return
    _advancing_rounds.add(lock_key)

    try:
        all_matches = sb.from_("tournament_matches").select("*") \
            .eq("tournament_id", tournament_id).eq("round", round_name).execute()
        if not all_matches.data:
            return
        pending = [m for m in all_matches.data if m["status"] in ("playing", "pending")]
        if pending:
            return  # round not done yet

        winners = [m["winner_id"] for m in all_matches.data if m["winner_id"]]
        all_player_ids = []
        for m in all_matches.data:
            all_player_ids.extend([m["p1_id"], m["p2_id"]])

        for pid in all_player_ids:
            if pid in winners:
                sb.from_("tournament_players").update({"status": "active"}) \
                    .eq("tournament_id", tournament_id).eq("telegram_id", pid).execute()
            else:
                sb.from_("tournament_players").update({"status": "eliminated", "eliminated_round": round_name}) \
                    .eq("tournament_id", tournament_id).eq("telegram_id", pid).execute()

        losers = [pid for pid in all_player_ids if pid not in winners]
        for pid in losers:
            sb.from_("player_rooms").delete().eq("telegram_id", pid).execute()

        # FIX T2: was "round_2" — now correctly "round_1"
        if round_name == "round_1":
            for pid in losers:
                player = sb.from_("tournament_players").select("name") \
                    .eq("tournament_id", tournament_id).eq("telegram_id", pid).execute()
                pname = player.data[0]["name"] if player.data else "لاعب"
                if PRIZE_3RD > 0:
                    await add_balance(pid, pname, PRIZE_3RD, "جائزة المركز 3/4 - بطولة XO")
                try:
                    await app.bot.send_message(
                        chat_id=int(pid),
                        text=f"🥉 *وصلت لنصف النهائي!*\n\n{'جائزتك: `+' + str(PRIZE_3RD) + '$` 🎉' if PRIZE_3RD > 0 else 'أحسنت على المحاولة! 💪'}",
                        parse_mode="Markdown"
                    )
                except:
                    pass

        next_round = get_next_round(round_name)
        if next_round:
            round_label = ROUNDS.get(next_round, next_round)
            await asyncio.sleep(5)
            await notify_all_tournament_players(
                app, tournament_id,
                f"✅ *انتهى {ROUNDS[round_name]}!*\n\n{round_label} سيبدأ خلال 30 ثانية... 🏆"
            )
            await asyncio.sleep(30)
            await start_round(app, tournament_id, next_round)
        else:
            await finish_tournament(app, tournament_id)
    finally:
        _advancing_rounds.discard(lock_key)

async def finish_tournament(app, tournament_id):
    # Guard against double execution
    lock_key = f"{tournament_id}:finish"
    if lock_key in _advancing_rounds:
        return
    _advancing_rounds.add(lock_key)

    try:
        final_match = sb.from_("tournament_matches").select("*") \
            .eq("tournament_id", tournament_id).eq("round", "final").execute()
        if not final_match.data:
            return
        m = final_match.data[0]
        if not m["winner_id"]:
            logger.error("finish_tournament called but final match has no winner_id yet")
            return

        winner_id = m["winner_id"]
        loser_id = m["p2_id"] if winner_id == m["p1_id"] else m["p1_id"]
        winner_name = m["p1_name"] if winner_id == m["p1_id"] else m["p2_name"]
        loser_name = m["p2_name"] if winner_id == m["p1_id"] else m["p1_name"]

        # Check prizes weren't already paid
        already = sb.from_("transactions").select("id") \
            .eq("telegram_id", winner_id) \
            .eq("description", "جائزة المركز الأول 🥇 - بطولة XO") \
            .execute()
        if already.data:
            logger.warning(f"Tournament {tournament_id} prizes already paid, skipping.")
            return

        await add_balance(winner_id, winner_name, PRIZE_1ST, "جائزة المركز الأول 🥇 - بطولة XO")
        await add_balance(loser_id, loser_name, PRIZE_2ND, "جائزة المركز الثاني 🥈 - بطولة XO")
        await add_balance(str(ADMIN_ID), "Admin", OWNER_CUT, "ربح بطولة XO")

        try:
            await app.bot.send_message(
                chat_id=int(winner_id),
                text=f"🥇 *مبروك! أنت بطل البطولة!*\n\nجائزتك: `+{PRIZE_1ST}$` 🏆\n\nأحسنت! 🌟",
                parse_mode="Markdown"
            )
        except:
            pass
        try:
            await app.bot.send_message(
                chat_id=int(loser_id),
                text=f"🥈 *أحسنت! وصلت للنهائي!*\n\nجائزتك: `+{PRIZE_2ND}$` 🎉",
                parse_mode="Markdown"
            )
        except:
            pass

        sb.from_("tournaments").update({"status": "finished", "finished_at": "now()"}).eq("id", tournament_id).execute()
        sb.from_("tournaments").insert({"status": "waiting", "current_round": "waiting"}).execute()

        # FIX T3: notify ALL players including eliminated ones about tournament end
        all_players = sb.from_("tournament_players").select("telegram_id") \
            .eq("tournament_id", tournament_id).execute()
        for p in all_players.data:
            if p["telegram_id"] in [winner_id, loser_id]:
                continue  # already notified above
            try:
                await app.bot.send_message(
                    chat_id=int(p["telegram_id"]),
                    text=f"🏆 *انتهت البطولة!*\n\n🥇 البطل: *{winner_name}*\n🥈 الوصيف: *{loser_name}*\n\nشكراً للمشاركة! 🎮\nسجّل بالبطولة الجديدة: /tournament",
                    parse_mode="Markdown"
                )
            except:
                pass
    finally:
        _advancing_rounds.discard(lock_key)

async def tournament_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name
    tournament = await get_active_tournament()
    if not tournament:
        await update.message.reply_text("❌ لا يوجد بطولة حالياً. تواصل مع الأدمن.")
        return
    t_id = tournament["id"]
    t_status = tournament["status"]
    if t_status not in ("waiting",):
        round_label = ROUNDS.get(t_status, t_status)
        await update.message.reply_text(
            f"⚔️ *بطولة جارية حالياً!*\n\nالمرحلة: *{round_label}*\n\nانتظر انتهاءها للتسجيل بالبطولة الجديدة.",
            parse_mode="Markdown"
        )
        return
    existing = sb.from_("tournament_players").select("id").eq("tournament_id", t_id).eq("telegram_id", uid).execute()
    if existing.data:
        count = tournament["registered_count"]
        await update.message.reply_text(
            f"✅ *أنت مسجل بالفعل!*\n\nاللاعبون: `{count}/{TOURNAMENT_SIZE}`\n\nانتظر اكتمال العدد... ⏳",
            parse_mode="Markdown"
        )
        return
    bal = await get_balance(uid)
    if bal < TOURNAMENT_FEE:
        await update.message.reply_text(
            f"❌ *رصيدك غير كافٍ!*\n\nرصيدك: `{bal:.2f}$`\nرسوم البطولة: `{TOURNAMENT_FEE}$`\n\naشحن رصيدك: /deposit",
            parse_mode="Markdown"
        )
        return

    await deduct_balance(uid, name, TOURNAMENT_FEE, "رسوم بطولة XO")
    sb.from_("tournament_players").insert({
        "tournament_id": t_id, "telegram_id": uid, "name": name, "status": "active"
    }).execute()
    new_count = tournament["registered_count"] + 1
    sb.from_("tournaments").update({
        "registered_count": new_count,
        "prize_pool": tournament["prize_pool"] + TOURNAMENT_FEE
    }).eq("id", t_id).execute()

    await notify_all_tournament_players(
        context.application, t_id,
        f"🎮 *انضم {name} للبطولة!*\n\nاللاعبون: `{new_count}/{TOURNAMENT_SIZE}`\n"
        f"{'⏳ ننتظر المزيد...' if new_count < TOURNAMENT_SIZE else '🔥 اكتمل العدد!'}"
    )

    # ══════════════════════════════════════════
    # FIX L2: _starting_tournaments lock prevents
    # two simultaneous registrations both reaching
    # new_count == TOURNAMENT_SIZE and calling
    # delayed_start twice.
    # Before: two players register at same ms →
    # both read count=3, both increment to 4,
    # both call delayed_start → tournament starts twice.
    # After: only first caller creates the task.
    # ══════════════════════════════════════════
    if new_count >= TOURNAMENT_SIZE and t_id not in _starting_tournaments:
        _starting_tournaments.add(t_id)
        sb.from_("tournaments").update({"status": "round_1"}).eq("id", t_id).execute()
        await update.message.reply_text(
            "🏆 *اكتمل العدد! البطولة ستبدأ بعد 60 ثانية!*\n\nاستعد! ⚔️",
            parse_mode="Markdown"
        )
        asyncio.create_task(delayed_start(context.application, t_id))
    elif new_count < TOURNAMENT_SIZE:
        remaining = TOURNAMENT_SIZE - new_count
        await update.message.reply_text(
            f"✅ *تم تسجيلك بالبطولة!*\n\nأنت اللاعب `{new_count}` من `{TOURNAMENT_SIZE}`\nننتظر `{remaining}` لاعب آخر...\n\nسيتم إشعارك عند البدء! 🔔",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👥 شاهد اللاعبين", web_app=WebAppInfo(url=WEBAPP_URL))]]),
            parse_mode="Markdown"
        )

async def delayed_start(app, tournament_id):
    await asyncio.sleep(60)
    await start_round(app, tournament_id, "round_1")
    _starting_tournaments.discard(tournament_id)

async def add_test_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    tournament = await get_active_tournament()
    if not tournament or tournament["status"] != "waiting":
        await update.message.reply_text("❌ لا يوجد بطولة في مرحلة التسجيل.")
        return
    t_id = tournament["id"]
    count = tournament["registered_count"]
    added = 0
    for bot_id, bot_name in TEST_BOTS.items():
        existing = sb.from_("tournament_players").select("id").eq("tournament_id", t_id).eq("telegram_id", bot_id).execute()
        if existing.data:
            continue
        sb.from_("tournament_players").insert({
            "tournament_id": t_id, "telegram_id": bot_id, "name": bot_name, "status": "active"
        }).execute()
        sb.from_("balances").upsert({"telegram_id": bot_id, "name": bot_name, "balance": 100.0}).execute()
        count += 1
        added += 1
    sb.from_("tournaments").update({
        "registered_count": count,
        "prize_pool": float(tournament["prize_pool"]) + added
    }).eq("id", t_id).execute()
    await update.message.reply_text(
        f"✅ تم إضافة {added} بوت للبطولة!\nاللاعبون الآن: `{count}/{TOURNAMENT_SIZE}`",
        parse_mode="Markdown"
    )
    if count >= TOURNAMENT_SIZE and t_id not in _starting_tournaments:
        _starting_tournaments.add(t_id)
        sb.from_("tournaments").update({"status": "round_1"}).eq("id", t_id).execute()
        await notify_all_tournament_players(
            context.application, t_id,
            "🏆 *اكتمل العدد! البطولة ستبدأ بعد 60 ثانية!*\n\nاستعد! ⚔️"
        )
        asyncio.create_task(delayed_start(context.application, t_id))

async def bracket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tournament = await get_active_tournament()
    if not tournament:
        await update.message.reply_text("❌ لا يوجد بطولة حالياً.")
        return
    t_id = tournament["id"]
    matches = sb.from_("tournament_matches").select("*").eq("tournament_id", t_id).order("round").order("match_number").execute()
    if not matches.data:
        count = tournament["registered_count"]
        await update.message.reply_text(
            f"⏳ *البطولة في مرحلة التسجيل*\n\nاللاعبون: `{count}/{TOURNAMENT_SIZE}`\n\nسجّل الآن: /tournament",
            parse_mode="Markdown"
        )
        return
    text = "🏆 *جدول البطولة:*\n\n"
    current_round = ""
    for m in matches.data:
        if m["round"] != current_round:
            current_round = m["round"]
            text += f"\n*{ROUNDS.get(current_round, current_round)}:*\n"
        if m["winner_id"] == m["p1_id"]:
            winner_str = f"✅ {m['p1_name']}"
        elif m["winner_id"] == m["p2_id"]:
            winner_str = f"✅ {m['p2_name']}"
        else:
            winner_str = "⏳ جارية"
        text += f"  {m['p1_name']} ⚔️ {m['p2_name']} → {winner_str}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def start_tournament_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    tournament = await get_active_tournament()
    if not tournament or tournament["status"] != "waiting":
        await update.message.reply_text("❌ لا يوجد بطولة في مرحلة التسجيل.")
        return
    count = tournament["registered_count"]
    if count < 2:
        await update.message.reply_text(f"❌ يحتاج على الأقل لاعبين! حالياً: {count}")
        return
    t_id = tournament["id"]
    if t_id in _starting_tournaments:
        await update.message.reply_text("⏳ البطولة بدأت بالفعل!")
        return
    _starting_tournaments.add(t_id)
    sb.from_("tournaments").update({"status": "round_1"}).eq("id", t_id).execute()
    await update.message.reply_text(f"✅ تم بدء البطولة يدوياً بـ {count} لاعبين!")
    asyncio.create_task(delayed_start(context.application, t_id))

async def leave_tournament_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    name = user.first_name
    tournament = await get_active_tournament()
    if not tournament:
        await update.message.reply_text("❌ لا يوجد بطولة نشطة.")
        return
    if tournament["status"] != "waiting":
        await update.message.reply_text("❌ *البطولة بدأت — لا يمكن الانسحاب!*", parse_mode="Markdown")
        return
    t_id = tournament["id"]
    existing = sb.from_("tournament_players").select("id").eq("tournament_id", t_id).eq("telegram_id", uid).execute()
    if not existing.data:
        await update.message.reply_text("❌ أنت لست مسجلاً بالبطولة.")
        return
    sb.from_("tournament_players").delete().eq("tournament_id", t_id).eq("telegram_id", uid).execute()
    new_count = tournament["registered_count"] - 1
    sb.from_("tournaments").update({
        "registered_count": new_count,
        "prize_pool": float(tournament["prize_pool"]) - TOURNAMENT_FEE
    }).eq("id", t_id).execute()
    await add_balance(uid, name, TOURNAMENT_FEE, "إرجاع رسوم البطولة — انسحاب")
    await update.message.reply_text(
        f"✅ *تم انسحابك من البطولة*\nتم إرجاع `{TOURNAMENT_FEE}$` لرصيدك. 💰",
        parse_mode="Markdown"
    )
    await notify_all_tournament_players(
        context.application, t_id,
        f"😔 *انسحب {name} من البطولة*\nاللاعبون الآن: `{new_count}/{TOURNAMENT_SIZE}`"
    )

async def cancel_tournament_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    tournament = await get_active_tournament()
    if not tournament:
        await update.message.reply_text("❌ لا يوجد بطولة نشطة.")
        return
    players = sb.from_("tournament_players").select("*").eq("tournament_id", tournament["id"]).execute()
    for p in players.data:
        await add_balance(p["telegram_id"], p["name"], TOURNAMENT_FEE, "إرجاع — إلغاء البطولة")
        try:
            await context.bot.send_message(
                chat_id=int(p["telegram_id"]),
                text="❌ *تم إلغاء البطولة*\n\nتم إرجاع رسوم التسجيل إلى رصيدك. 💰",
                parse_mode="Markdown"
            )
        except:
            pass
    t_id = tournament["id"]
    _starting_tournaments.discard(t_id)
    _advancing_rounds.discard(f"{t_id}:round_1")
    _advancing_rounds.discard(f"{t_id}:final")
    _advancing_rounds.discard(f"{t_id}:finish")
    sb.from_("tournaments").update({"status": "finished", "finished_at": "now()"}).eq("id", t_id).execute()
    sb.from_("tournaments").insert({"status": "waiting", "current_round": "waiting"}).execute()
    await update.message.reply_text("✅ تم إلغاء البطولة وإرجاع كل المبالغ.")

async def tournament_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    tournament = await get_active_tournament()
    if not tournament:
        await update.message.reply_text("❌ لا يوجد بطولة نشطة.")
        return
    players = sb.from_("tournament_players").select("*").eq("tournament_id", tournament["id"]).execute()
    active = [p for p in players.data if p["status"] == "active"]
    text = (
        f"📊 *حالة البطولة:*\n\n"
        f"الحالة: `{tournament['status']}`\n"
        f"المرحلة: `{tournament['current_round']}`\n"
        f"المسجلون: `{tournament['registered_count']}/{TOURNAMENT_SIZE}`\n"
        f"النشطون: `{len(active)}`\n"
        f"الجائزة الكلية: `{tournament['prize_pool']}$`\n\n"
        f"اللاعبون النشطون:\n"
    )
    for p in active:
        text += f"• {p['name']}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ══════════════════════════════════════════
# FIX T1 (server-side): check_tournament_results
# now marks the match as "finished" BEFORE calling
# check_round_complete. This prevents the 5-second
# loop from finding the same match "playing" again
# while check_round_complete is still running.
# ══════════════════════════════════════════
async def check_tournament_results(app):
    while True:
        try:
            active_matches = sb.from_("tournament_matches").select("*").eq("status", "playing").execute()
            for m in active_matches.data:
                room = sb.from_("rooms").select("*").eq("id", m["room_id"]).execute()
                if not room.data:
                    continue
                r = room.data[0]
                if r["status"] == "finished" and r.get("winner"):
                    winner_id = r["player_x_id"] if r["winner"] == "X" else r["player_o_id"]

                    # FIX T1: update match status FIRST before calling check_round_complete
                    # This prevents the next loop iteration from re-processing this match
                    update_res = sb.from_("tournament_matches").update({
                        "winner_id": winner_id,
                        "status": "finished",
                        "finished_at": "now()"
                    }).eq("id", m["id"]).eq("status", "playing").execute()  # eq status guard

                    if not update_res.data:
                        # Another coroutine already updated this match
                        continue

                    loser_id = r["player_o_id"] if winner_id == r["player_x_id"] else r["player_x_id"]
                    sb.from_("player_rooms").delete().eq("telegram_id", loser_id).execute()
                    await check_round_complete(app, m["tournament_id"], m["round"])

        except Exception as e:
            logger.error(f"Tournament results check error: {e}")
        await asyncio.sleep(5)


async def post_init(app):
    asyncio.create_task(check_new_results(app))
    asyncio.create_task(check_tournament_results(app))

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
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("reject", reject_cmd))
    app.add_handler(CommandHandler("addbalance", addbalance_cmd))
    app.add_handler(CommandHandler("allplayers", allplayers_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("tournament", tournament_cmd))
    app.add_handler(CommandHandler("bracket", bracket_cmd))
    app.add_handler(CommandHandler("leave_tournament", leave_tournament_cmd))
    app.add_handler(CommandHandler("start_tournament", start_tournament_cmd))
    app.add_handler(CommandHandler("cancel_tournament", cancel_tournament_cmd))
    app.add_handler(CommandHandler("tournament_status", tournament_status_cmd))
    app.add_handler(CommandHandler("add_test_bots", add_test_bots_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot running...")
    app.run_polling()

