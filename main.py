import os
import json
import sqlite3
import asyncio
import zipfile
import tempfile
import logging
from io import BytesIO
from aiohttp import web

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV VARIABLES
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
BACKUP_GROUP_ID = int(os.environ["BACKUP_GROUP_ID"])
PORT = int(os.environ.get("PORT", 8000))

DB_PATH = "bot.db"

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)

# =========================
# DB INIT
# =========================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS slogans(
            text TEXT PRIMARY KEY,
            score INTEGER
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS user_scores(
            user_id INTEGER,
            chat_id INTEGER,
            score INTEGER,
            PRIMARY KEY(user_id, chat_id)
        )
        """)
        conn.commit()

init_db()

# =========================
# UTILITIES
# =========================
PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
EN_DIGITS = "0123456789"
TRANS_TABLE = str.maketrans("".join(PERSIAN_DIGITS), "".join(EN_DIGITS))

def parse_int(text: str) -> int:
    text = text.translate(TRANS_TABLE)
    return int(text.strip())

def export_db_json():
    data = {"slogans": [], "user_scores": []}
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        for row in c.execute("SELECT text, score FROM slogans"):
            data["slogans"].append({"text": row[0], "score": row[1]})
        for row in c.execute("SELECT user_id, chat_id, score FROM user_scores"):
            data["user_scores"].append({
                "user_id": row[0],
                "chat_id": row[1],
                "score": row[2]
            })
    return json.dumps(data, ensure_ascii=False, indent=2).encode()

async def send_backup(context: ContextTypes.DEFAULT_TYPE):
    data = export_db_json()
    mem = BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("db.json", data)
    mem.seek(0)
    await context.bot.send_document(
        BACKUP_GROUP_ID,
        document=InputFile(mem, filename="db.zip")
    )

async def import_backup(file_bytes: bytes):
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, "db.zip")
        with open(zpath, "wb") as f:
            f.write(file_bytes)
        with zipfile.ZipFile(zpath) as zf:
            zf.extract("db.json", tmp)
        jpath = os.path.join(tmp, "db.json")
        with open(jpath, encoding="utf-8") as f:
            data = json.load(f)

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM slogans")
        c.execute("DELETE FROM user_scores")
        for s in data["slogans"]:
            c.execute("INSERT INTO slogans VALUES (?,?)",
                      (s["text"], s["score"]))
        for u in data["user_scores"]:
            c.execute("INSERT INTO user_scores VALUES (?,?,?)",
                      (u["user_id"], u["chat_id"], u["score"]))
        conn.commit()

# =========================
# KEYBOARD
# =========================
ADMIN_KB = ReplyKeyboardMarkup(
    [["افزودن شعار", "حذف شعار"],
     ["لیست شعار ها"]],
    resize_keyboard=True
)

# =========================
# STATES
# =========================
ADD_TEXT, ADD_SCORE, DEL_TEXT = range(3)

# =========================
# ADMIN HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("مدیریت:", reply_markup=ADMIN_KB)

async def add_start(update: Update, context):
    await update.message.reply_text("محتوای شعار را بفرست یا لغو",
                                    reply_markup=ReplyKeyboardMarkup([["لغو"]], resize_keyboard=True))
    return ADD_TEXT

async def add_text(update: Update, context):
    if update.message.text == "لغو":
        return await cancel(update, context)
    context.user_data["new_text"] = update.message.text
    await update.message.reply_text("امتیاز شعار را بفرست:")
    return ADD_SCORE

async def add_score(update: Update, context):
    if update.message.text == "لغو":
        return await cancel(update, context)
    try:
        score = parse_int(update.message.text)
    except:
        await update.message.reply_text("عدد معتبر نیست دوباره:")
        return ADD_SCORE

    text = context.user_data["new_text"]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO slogans VALUES (?,?)", (text, score))
        conn.commit()

    await send_backup(context)
    await update.message.reply_text("ثبت شد.", reply_markup=ADMIN_KB)
    return ConversationHandler.END

async def del_start(update: Update, context):
    await update.message.reply_text("متن شعار برای حذف:",
                                    reply_markup=ReplyKeyboardMarkup([["لغو"]], resize_keyboard=True))
    return DEL_TEXT

async def del_text(update: Update, context):
    if update.message.text == "لغو":
        return await cancel(update, context)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM slogans WHERE text=?",
                     (update.message.text,))
        conn.commit()
    await send_backup(context)
    await update.message.reply_text("حذف شد.", reply_markup=ADMIN_KB)
    return ConversationHandler.END

async def list_slogans(update: Update, context):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT text, score FROM slogans ORDER BY score DESC"
        ).fetchall()
    if not rows:
        await update.message.reply_text("خالیه")
        return
    msg = "\n".join([f"{t}  ({s})" for t, s in rows])
    await update.message.reply_text(msg)

async def cancel(update: Update, context):
    await update.message.reply_text("لغو شد", reply_markup=ADMIN_KB)
    return ConversationHandler.END

# =========================
# GROUP LOGIC
# =========================
async def slogan_listener(update: Update, context):
    text = update.message.text
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT score FROM slogans WHERE text=?",
                           (text,)).fetchone()
        if not row:
            return
        score = row[0]
        uid = update.effective_user.id
        cid = update.effective_chat.id
        cur = conn.execute(
            "SELECT score FROM user_scores WHERE user_id=? AND chat_id=?",
            (uid, cid)).fetchone()
        total = (cur[0] if cur else 0) + score
        conn.execute("INSERT OR REPLACE INTO user_scores VALUES (?,?,?)",
                     (uid, cid, total))
        conn.commit()

    await send_backup(context)

    if score >= 0:
        msg = f"تبریک {score}+ امتیاز گرفتی 🎉\nجمع کل: {total}"
    else:
        msg = f"شرم بر تو! {score} امتیاز از دست دادی!\nالان: {total}"
    await update.message.reply_text(msg,
                                    reply_to_message_id=update.message.message_id)

async def my_state(update: Update, context):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT score FROM user_scores WHERE user_id=? AND chat_id=?",
            (uid, cid)).fetchone()
    total = row[0] if row else 0
    await update.message.reply_text(f"امتیاز شما: {total}")

async def leaderboard(update: Update, context):
    cid = update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
        SELECT user_id, score
        FROM user_scores
        WHERE chat_id=?
        ORDER BY score DESC
        LIMIT 10
        """, (cid,)).fetchall()

    medals = ["🔥", "⭐", "💎"]
    lines = []
    for i, (uid, score) in enumerate(rows):
        emoji = medals[i] if i < len(medals) else "▫️"
        lines.append(f"{emoji} {i+1}- {uid} : {score}")
    await update.message.reply_text("\n".join(lines) or "خالی")

# =========================
# RECEIVE BACKUP
# =========================
async def recv_doc(update: Update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    doc = update.message.document
    if doc.file_name != "db.zip":
        return
    f = await doc.get_file()
    data = await f.download_as_bytearray()
    await import_backup(data)
    await update.message.reply_text("DB جایگزین شد")

# =========================
# WEB SERVER / HEALTH
# =========================
async def health(request):
    return web.Response(text="OK")

async def telegram_webhook(request):
    app = request.app["bot_app"]
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return web.Response()

# =========================
# MAIN
# =========================
def build_app():
    application = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^افزودن شعار$"), add_start)],
        states={
            ADD_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_text)],
            ADD_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_score)],
        },
        fallbacks=[MessageHandler(filters.Regex("^لغو$"), cancel)],
    )

    del_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^حذف شعار$"), del_start)],
        states={
            DEL_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, del_text)],
        },
        fallbacks=[MessageHandler(filters.Regex("^لغو$"), cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(add_conv)
    application.add_handler(del_conv)
    application.add_handler(MessageHandler(filters.Regex("^لیست شعار ها$"), list_slogans))
    application.add_handler(CommandHandler("my_state", my_state))
    application.add_handler(CommandHandler("leader_board", leaderboard))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, slogan_listener))
    application.add_handler(MessageHandler(filters.Document.ALL, recv_doc))

    return application

async def main():
    tg_app = build_app()
    await tg_app.initialize()
    await tg_app.bot.set_webhook(f"{WEBHOOK_URL}/telegram")

    web_app = web.Application()
    web_app["bot_app"] = tg_app
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    web_app.router.add_post("/telegram", telegram_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await tg_app.start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
