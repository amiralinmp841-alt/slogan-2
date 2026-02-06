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
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ================= ENV =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
BACKUP_GROUP_ID = int(os.environ["BACKUP_GROUP_ID"])
PORT = int(os.environ.get("PORT", 8000))

DB_PATH = "bot.db"
logging.basicConfig(level=logging.INFO)

# ================= DB =================
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

# ================= HELPERS =================
def is_admin(update: Update):
    return update.effective_user.id == ADMIN_ID

PERSIAN_DIGITS="۰۱۲۳۴۵۶۷۸۹"
TRANS=str.maketrans(PERSIAN_DIGITS,"0123456789")

def parse_int(x:str):
    return int(x.translate(TRANS).strip())

def mention_html(uid,name):
    safe=name.replace("<","").replace(">","")
    return f'<a href="tg://user?id={uid}">{safe}</a>'

# ================= BACKUP =================
def export_db():
    data={"slogans":[],"user_scores":[]}
    with sqlite3.connect(DB_PATH) as conn:
        for r in conn.execute("SELECT text,score FROM slogans"):
            data["slogans"].append({"text":r[0],"score":r[1]})
        for r in conn.execute("SELECT user_id,chat_id,score FROM user_scores"):
            data["user_scores"].append({
                "user_id":r[0],
                "chat_id":r[1],
                "score":r[2]
            })
    return json.dumps(data,ensure_ascii=False).encode()

async def send_backup(context):
    mem=BytesIO()
    with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("db.json",export_db())
    mem.seek(0)
    await context.bot.send_document(
        BACKUP_GROUP_ID,
        document=InputFile(mem,"db.zip")
    )

async def import_backup(data):
    with tempfile.TemporaryDirectory() as t:
        zp=os.path.join(t,"db.zip")
        open(zp,"wb").write(data)
        with zipfile.ZipFile(zp) as z:
            z.extract("db.json",t)
        js=json.load(open(os.path.join(t,"db.json"),encoding="utf-8"))
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM slogans")
        conn.execute("DELETE FROM user_scores")
        for s in js["slogans"]:
            conn.execute("INSERT INTO slogans VALUES (?,?)",(s["text"],s["score"]))
        for u in js["user_scores"]:
            conn.execute("INSERT INTO user_scores VALUES (?,?,?)",
                         (u["user_id"],u["chat_id"],u["score"]))
        conn.commit()

# ================= KEYBOARD =================
ADMIN_KB=ReplyKeyboardMarkup(
    [["افزودن شعار","حذف شعار"],
     ["لیست شعار ها"]],
    resize_keyboard=True
)

ADD_TEXT,ADD_SCORE,DEL_TEXT=range(3)

# ================= ADMIN =================
async def start(update:Update,context):
    if not is_admin(update):
        return
    await update.message.reply_text("پنل مدیریت:",reply_markup=ADMIN_KB)

async def add_start(update,context):
    if not is_admin(update): return
    await update.message.reply_text("متن شعار؟ یا لغو")
    return ADD_TEXT

async def add_text(update,context):
    if update.message.text=="لغو": return ConversationHandler.END
    context.user_data["txt"]=update.message.text
    await update.message.reply_text("امتیاز؟")
    return ADD_SCORE

async def add_score(update,context):
    try: score=parse_int(update.message.text)
    except:
        await update.message.reply_text("عدد نامعتبر")
        return ADD_SCORE
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO slogans VALUES (?,?)",
                     (context.user_data["txt"],score))
        conn.commit()
    await send_backup(context)
    await update.message.reply_text("ثبت شد",reply_markup=ADMIN_KB)
    return ConversationHandler.END

async def del_start(update,context):
    if not is_admin(update): return
    await update.message.reply_text("متن شعار جهت حذف؟")
    return DEL_TEXT

async def del_text(update,context):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM slogans WHERE text=?",
                     (update.message.text,))
        conn.commit()
    await send_backup(context)
    await update.message.reply_text("حذف شد",reply_markup=ADMIN_KB)
    return ConversationHandler.END

async def list_slogans(update,context):
    if not is_admin(update): return
    with sqlite3.connect(DB_PATH) as conn:
        rows=conn.execute(
            "SELECT text,score FROM slogans ORDER BY score DESC"
        ).fetchall()
    if not rows:
        await update.message.reply_text("خالی")
        return
    txt="\n".join([f"`{t}`  ({s})" for t,s in rows])
    await update.message.reply_text(txt,parse_mode=ParseMode.MARKDOWN)

# ================= GROUP =================
async def slogan_listener(update,context):
    text=update.message.text
    with sqlite3.connect(DB_PATH) as conn:
        r=conn.execute("SELECT score FROM slogans WHERE text=?",(text,)).fetchone()
        if not r: return
        score=r[0]
        uid=update.effective_user.id
        cid=update.effective_chat.id
        cur=conn.execute(
            "SELECT score FROM user_scores WHERE user_id=? AND chat_id=?",
            (uid,cid)).fetchone()
        total=(cur[0] if cur else 0)+score
        conn.execute("INSERT OR REPLACE INTO user_scores VALUES (?,?,?)",
                     (uid,cid,total))
        conn.commit()
    await send_backup(context)

    msg=("تبریک" if score>=0 else "شرم بر تو!") \
        + f"\n{score:+} امتیاز\nجمع کل: {total}"
    await update.message.reply_text(msg,reply_to_message_id=update.message.id)

async def my_state(update,context):
    uid=update.effective_user.id
    cid=update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        r=conn.execute(
            "SELECT score FROM user_scores WHERE user_id=? AND chat_id=?",
            (uid,cid)).fetchone()
    await update.message.reply_text(f"امتیاز شما: {r[0] if r else 0}")

async def leaderboard(update,context):
    cid=update.effective_chat.id
    with sqlite3.connect(DB_PATH) as conn:
        rows=conn.execute("""
        SELECT user_id,score
        FROM user_scores
        WHERE chat_id=?
        ORDER BY score DESC
        LIMIT 20
        """,(cid,)).fetchall()

    medals=["🥇","🥈","🥉"]
    titles=["اول","دوم","سوم"]
    lines=[]
    for i,(uid,score) in enumerate(rows):
        user=await context.bot.get_chat(uid)
        name=user.first_name
        mention=mention_html(uid,name)
        medal=medals[i] if i<3 else "▫️"
        title=titles[i] if i<3 else str(i+1)
        lines.append(
f"• نفر {title}{medal} :\n( {score} امتیاز | {mention} )"
        )
    await update.message.reply_text(
        "\n".join(lines) or "خالی",
        parse_mode=ParseMode.HTML
    )

# ================= BACKUP RECEIVE =================
async def recv(update,context):
    if not is_admin(update): return
    doc=update.message.document
    if not doc or doc.file_name!="db.zip": return
    f=await doc.get_file()
    data=await f.download_as_bytearray()
    await import_backup(data)
    await update.message.reply_text("جایگزین شد")

# ================= WEB =================
async def health(req): return web.Response(text="OK")

async def hook(req):
    app=req.app["tg"]
    data=await req.json()
    upd=Update.de_json(data,app.bot)
    await app.process_update(upd)
    return web.Response()

# ================= MAIN =================
def build():
    app=Application.builder().token(BOT_TOKEN).build()

    add=ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^افزودن شعار$"),add_start)],
        states={
            ADD_TEXT:[MessageHandler(filters.TEXT,add_text)],
            ADD_SCORE:[MessageHandler(filters.TEXT,add_score)],
        },
        fallbacks=[]
    )
    dele=ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^حذف شعار$"),del_start)],
        states={DEL_TEXT:[MessageHandler(filters.TEXT,del_text)]},
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start",start))
    app.add_handler(add)
    app.add_handler(dele)
    app.add_handler(MessageHandler(filters.Regex("^لیست شعار ها$"),list_slogans))
    app.add_handler(CommandHandler("my_state",my_state))
    app.add_handler(CommandHandler("leader_board",leaderboard))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS,slogan_listener))
    app.add_handler(MessageHandler(filters.Document.ALL,recv))
    return app

async def main():
    tg=build()
    await tg.initialize()
    await tg.bot.set_webhook(f"{WEBHOOK_URL}/telegram")

    webapp=web.Application()
    webapp["tg"]=tg
    webapp.router.add_get("/",health)
    webapp.router.add_get("/health",health)
    webapp.router.add_post("/telegram",hook)

    runner=web.AppRunner(webapp)
    await runner.setup()
    await web.TCPSite(runner,"0.0.0.0",PORT).start()

    await tg.start()
    await asyncio.Event().wait()

if __name__=="__main__":
    asyncio.run(main())
