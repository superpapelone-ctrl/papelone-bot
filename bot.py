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
            logger.info(f"Supabase {method} {table}: OK - {result}")
            return result
    except urllib.error.HTTPError as e:
        logger.error(f"Supabase {method} {table} HTTP {e.code}: {e.read().decode()}")
        return None
    except Exception as e:
        logger.error(f"Supabase error: {e}")
        return None

def load_db():
    result = supabase_request("GET", "tasks", filters="order=created_at.asc")
    return {"tasks": result if result else []}

def save_task(task):
    task["created_at"] = datetime.now().isoformat()
    return supabase_request("POST", "tasks", data=task)

def complete_task(task_id):
    logger.info(f"Completing task with id: {task_id}")
    result = supabase_request("DELETE", "tasks", filters=f"id=eq.{task_id}")
    logger.info(f"Delete result: {result}")
    return result

def call_claude(messages, system):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        system=system,
        messages=messages
    )
    return response.content[0].text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
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
            await query.message.reply_text("🎉 Non hai niente in lista. Aggiungila qualcosa prima!")
            return
        system = """Sei il cervello esterno di una persona con ADHD. 
Guarda il database e dimmi UNA SOLA cosa da fare adesso — la più urgente o importante.
Sii diretto, breve, umano. In italiano. Max 3 righe."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(db, ensure_ascii=False)}"}],
            system
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "add":
        context.user_data["mode"] = "conversation"
        context.user_data["history"] = []
        await query.message.reply_text(
            "Dimmi. Anche disordinato, anche in dialetto, anche a metà frase — ci penso io."
        )

    elif query.data == "list":
        if not db["tasks"]:
            await query.message.reply_text("Lista vuota!\n\n/start per il menu.")
            return
        system = """Sei il cervello esterno di una persona con ADHD.
Mostra tutto quello che ha in lista, organizzato per area (lavoro, famiglia, salute, casa, altro).
Breve e chiaro. In italiano."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(db, ensure_ascii=False)}"}],
            system
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "done":
        db = load_db()
        if not db["tasks"]:
            await query.message.reply_text("Non hai task in lista!\n\n/start per il menu.")
            return
        task_buttons = []
        for task in db["tasks"][:8]:
            label = task.get("title", "Task")[:40]
            task_id = str(task.get("id", ""))
            logger.info(f"Creating button for task: {task_id} - {label}")
            task_buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"complete_{task_id}")])
        task_buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
        await query.message.reply_text("Quale hai fatto?", reply_markup=InlineKeyboardMarkup(task_buttons))

    elif query.data.startswith("complete_"):
        task_id = query.data[len("complete_"):]
        logger.info(f"Received complete request for task_id: {task_id}")
        result = complete_task(task_id)
        if result is not None:
            await query.message.reply_text("✅ Ottimo! Rimosso dalla lista.\n\n/start per il menu.")
        else:
            await query.message.reply_text("✅ Fatto! (Potrebbe esserci stato un problema con la rimozione)\n\n/start per il menu.")

    elif query.data == "cancel":
        await query.message.reply_text("Ok.\n\n/start per il menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    mode = context.user_data.get("mode")

    if mode == "conversation":
        history = context.user_data.get("history", [])
        history.append({"role": "user", "content": text})

        system = """Sei il cervello esterno di una persona con ADHD. Il tuo compito è capire cosa vuole ricordare o fare.

COMPORTAMENTO:
- Se il messaggio è chiaro (anche breve, anche volgare, anche in dialetto) → estrai i task e rispondi con JSON
- Se è vago o incompleto → fai UNA domanda sola per capire meglio, in modo naturale e amichevole
- Non essere formale, sei un amico che aiuta
- Rispondi SEMPRE in italiano

QUANDO HAI CAPITO, rispondi SOLO con questo JSON (niente altro, niente backtick):
{"action": "save", "tasks": [{"title": "titolo breve", "description": "dettaglio", "area": "lavoro|famiglia|salute|casa|altro", "urgency": "alta|media|bassa"}], "response": "conferma breve e umana"}

QUANDO HAI BISOGNO DI CHIARIMENTO, rispondi SOLO con questo JSON:
{"action": "ask", "response": "la tua domanda"}"""

        raw = call_claude(history, system)
        logger.info(f"Claude response: {raw}")

        try:
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)

            if parsed["action"] == "ask":
                history.append({"role": "assistant", "content": raw})
                context.user_data["history"] = history
                await update.message.reply_text(parsed["response"])

            elif parsed["action"] == "save":
                saved = 0
                for task in parsed.get("tasks", []):
                    result = save_task(task)
                    if result:
                        saved += 1
                context.user_data["mode"] = None
                context.user_data["history"] = []
                if saved > 0:
                    await update.message.reply_text(parsed["response"] + "\n\n/start per il menu.")
                else:
                    await update.message.reply_text("Ho capito ma ho avuto un problema a salvare. Riprova.\n\n/start per il menu.")

        except Exception as e:
            logger.error(f"Parse error: {e} | Raw: {raw}")
            history.append({"role": "assistant", "content": raw})
            context.user_data["history"] = history
            await update.message.reply_text(raw + "\n\n(Scrivi /start se vuoi tornare al menu)")

    else:
        db = load_db()
        system = f"""Sei il cervello esterno di una persona con ADHD.
Conosci la sua vita dal database: {json.dumps(db, ensure_ascii=False)}
Rispondi in modo utile e diretto. In italiano. Max 3 righe."""
        response = call_claude([{"role": "user", "content": text}], system)
        await update.message.reply_text(response + "\n\n/start per il menu.")

async def remind(context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db["tasks"]:
        return
    system = """Sei il cervello esterno di una persona con ADHD. 
Manda un promemoria breve e motivante. UNA cosa sola. In italiano."""
    response = call_claude(
        [{"role": "user", "content": f"Database: {json.dumps(db, ensure_ascii=False)}. Cosa dovrei fare adesso?"}],
        system
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=f"⏰ Ehi!\n\n{response}\n\n/start per il menu")

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
