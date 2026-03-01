# 🎮 XO Battle — Telegram WebApp

لعبة XO تنافسية داخل تيليغرام كـ WebApp احترافية.

---

## 🗂️ هيكل المشروع

```
xo-battle/
├── index.html       ← الـ WebApp (HTML/CSS/JS)
├── bot.py           ← بوت تيليغرام
├── requirements.txt
└── README.md
```

---

## ⚡ الخطوات الكاملة

### الخطوة 1 — رفع الـ WebApp على GitHub Pages

```bash
# 1. إنشاء repo جديد على github.com باسم: xo-battle

# 2. رفع الملفات
git init
git add .
git commit -m "XO Battle WebApp"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/xo-battle.git
git push -u origin main

# 3. تفعيل GitHub Pages:
# Settings → Pages → Source: main → / (root) → Save
# رابط موقعك: https://YOUR_USERNAME.github.io/xo-battle
```

### الخطوة 2 — إنشاء البوت

1. روح على `@BotFather` في تيليغرام
2. `/newbot` ← اختار اسم ← احصل على **TOKEN**
3. `/setmenubutton` ← اختار البوت ← حط رابط الـ WebApp

### الخطوة 3 — ربط الـ WebApp بالبوت

افتح `bot.py` وبدّل:
```python
BOT_TOKEN = "TOKEN_FROM_BOTFATHER"
WEBAPP_URL = "https://YOUR_USERNAME.github.io/xo-battle"
```

### الخطوة 4 — تشغيل البوت

```bash
pip install -r requirements.txt
python bot.py
```

أو على Render.com (مجاني):
- Environment Variables:
  - `BOT_TOKEN` = توكن البوت
  - `WEBAPP_URL` = رابط GitHub Pages

---

## 🎯 مزايا اللعبة

| الميزة | التفاصيل |
|--------|---------|
| 🎮 لعب محلي | لاعبين على نفس الجهاز |
| 🏠 إنشاء غرفة | كود 4 أرقام للمشاركة |
| 🔗 انضم لغرفة | ادخل الكود والعب |
| 📊 إحصائيات | تتبع الانتصارات والتعادلات |
| 🌙 ثيم داكن | تصميم احترافي |
| 📱 متجاوب | يعمل على كل الأجهزة |

---

## 🔧 للتطوير المستقبلي

لجعل الأونلاين حقيقياً (بدل localStorage):
- أضف **Supabase** أو **Firebase** مجاني
- أو بني API بسيط بـ FastAPI
- استبدل functions `saveOnlineState()` و `getRoom()` في `index.html`
