import os
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import anthropic
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
CHAT_ID = 1077588790

logger.info(f"SUPABASE_URL: {SUPABASE_URL[:30] if SUPABASE_URL else 'EMPTY'}")
logger.info(f"SUPABASE_KEY: {SUPABASE_KEY[:20] if SUPABASE_KEY else 'EMPTY'}")

def supabase_request(method, table, data=None, filters=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if filters:
        url += f"?{filters}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            logger.info(f"Supabase {method} {table}: OK")
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        logger.error(f"Supabase {method} {table} HTTP {e.code}: {error_body}")
        return None
    except Exception as e:
        logger.error(f"Supabase {method} {table} error: {e}", exc_info=True)
        return None

def load_db():
    result = supabase_request("GET", "tasks", filters="order=created_at.asc")
    return {"tasks": result if result else []}

def save_task(task):
    task["created_at"] = datetime.now().isoformat()
    return supabase_request("POST", "tasks", data=task)

def complete_task(task_id):
    return supabase_request("DELETE", "tasks", filters=f"id=eq.{task_id}")

def get_claude_response(prompt, db):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = """Sei l'assistente personale di una persona con ADHD. 
Gestisci la sua vita: lavoro, famiglia, salute, casa, tutto.

REGOLE:
1. Dai SEMPRE una sola cosa da fare adesso. Mai liste lunghe.
2. Sii diretto, breve, concreto.
3. Non giudicare mai.
4. Rispondi sempre in italiano.

Database attuale:
""" + json.dumps(db, ensure_ascii=False, indent=2)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎯 Cosa faccio adesso?", callback_data="now")],
        [InlineKeyboardButton("➕ Aggiungi qualcosa", callback_data="add")],
        [InlineKeyboardButton("📋 Tutto quello che ho", callback_data="list")],
        [InlineKeyboardButton("✅ Ho fatto una cosa", callback_data="done")],
    ]
    await update.message.reply_text(
        "👋 Ciao! Sono il tuo cervello esterno.\n\nCosa vuoi fare?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = load_db()

    if query.data == "now":
        if not db["tasks"]:
            await query.message.reply_text("🎉 Non hai niente in lista. Goditi il momento!")
            return
        response = get_claude_response("Dimmi UNA SOLA cosa che devo fare adesso. La più urgente. Solo quella.", db)
        await query.message.reply_text(response)

    elif query.data == "add":
        context.user_data["waiting_for"] = "add"
        await query.message.reply_text("Dimmi cosa devo ricordare. Scrivi tutto, anche disordinato:")

    elif query.data == "list":
        if not db["tasks"]:
            await query.message.reply_text("Lista vuota. Inizia ad aggiungere cose!")
            return
        response = get_claude_response("Mostrami tutto in lista, organizzato per area. Breve.", db)
        await query.message.reply_text(response)

    elif query.data == "done":
        if not db["tasks"]:
            await query.message.reply_text("Non hai task in lista!")
            return
        task_buttons = []
        for i, task in enumerate(db["tasks"][:8]):
            label = task.get("title", "Task")[:40]
            task_id = task.get("id", i)
            task_buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"complete_{task_id}")])
        task_buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
        await query.message.reply_text("Quale hai fatto?", reply_markup=InlineKeyboardMarkup(task_buttons))

    elif query.data.startswith("complete_"):
        task_id = query.data.split("_")[1]
        complete_task(task_id)
        await query.message.reply_text("✅ Fatto! Bene.\n\n/start per tornare al menu.")

    elif query.data == "cancel":
        await query.message.reply_text("Ok. /start per tornare al menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    db = load_db()
    waiting = context.user_data.get("waiting_for")

    if waiting == "add":
        parse_prompt = f"""L'utente ha detto: "{text}"

Estrai tutti i task e restituisci SOLO questo JSON, nient'altro, senza backtick:
{{"tasks": [{{"title": "titolo breve", "description": "dettaglio", "area": "lavoro|famiglia|salute|casa|altro", "urgency": "alta|media|bassa"}}], "response": "conferma breve in italiano"}}"""

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": parse_prompt}]
        )

        try:
            raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
            logger.info(f"Claude raw response: {raw}")
            parsed = json.loads(raw)
            saved = 0
            for task in parsed["tasks"]:
                result = save_task(task)
                if result:
                    saved += 1
                    logger.info(f"Task saved: {task['title']}")
                else:
                    logger.error(f"Task NOT saved: {task['title']}")
            context.user_data["waiting_for"] = None
            if saved > 0:
                await update.message.reply_text(parsed["response"] + "\n\n/start per il menu.")
            else:
                await update.message.reply_text("Ho capito ma non riesco a salvare. Problema con il database.\n\n/start per il menu.")
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            await update.message.reply_text("Ho avuto un problema tecnico. Riprova.\n\n/start per il menu.")
    else:
        response = get_claude_response(text, db)
        await update.message.reply_text(response + "\n\n/start per il menu.")

async def remind(context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db["tasks"]:
        return
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Sei l'assistente di una persona con ADHD. Manda un promemoria breve su cosa fare adesso. Una cosa sola. In italiano.",
        messages=[{"role": "user", "content": f"Database: {json.dumps(db, ensure_ascii=False)}. Dimmi UNA cosa da fare adesso."}]
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=f"⏰ Ehi!\n\n{response.content[0].text}\n\n/start per il menu")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(remind, interval=10800, first=10800)
    logger.info("Bot avviato!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
