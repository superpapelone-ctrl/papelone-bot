import os
import json
import logging
from datetime import datetime, date, timedelta
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

SLOT_MATTINA = "mattina"
SLOT_CLUB = "club"
SLOT_POMERIGGIO = "pomeriggio"
SLOT_SERA = "sera"

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
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        logger.error(f"Supabase {method} {table} HTTP {e.code}: {e.read().decode()}")
        return None
    except Exception as e:
        logger.error(f"Supabase error: {e}")
        return None

def load_tasks():
    result = supabase_request("GET", "tasks", filters="order=urgency.asc,created_at.asc")
    return result if result else []

def save_task(task):
    task["created_at"] = datetime.now().isoformat()
    return supabase_request("POST", "tasks", data=task)

def delete_task(task_id):
    logger.info(f"Deleting task: {task_id}")
    return supabase_request("DELETE", "tasks", filters=f"id=eq.{task_id}")

def call_claude(messages, system, max_tokens=1000):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=messages
    )
    return response.content[0].text

def get_today_briefing(tasks):
    today = date.today()
    day_name = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"][today.weekday()]
    
    system = f"""Sei il cervello esterno di una persona con ADHD. Oggi è {day_name} {today.strftime('%d/%m/%Y')}.

Devi creare il briefing della giornata. La giornata è divisa in slot:
- 🌅 MATTINA (9:00-13:00): lavoro da casa o dal bar, gestione affitti/finanze
- 🏌️ CLUB (13:00-15:00): anche qui qualcosa se possibile
- ☀️ POMERIGGIO (15:00-19:00): clienti sudamericani, lavoro, gestione
- 🌙 SERA (19:00+): cose personali, famiglia

REGOLE:
- Assegna MAX 2-3 task per slot
- Dai priorità alle urgenze e alle scadenze vicine
- Considera i task ricorrenti del giorno
- Sii motivante ma diretto
- Formato: emoji slot + nome slot + lista task breve
- In italiano, tono amichevole

Database completo:
{json.dumps(tasks, ensure_ascii=False)}"""

    return call_claude(
        [{"role": "user", "content": f"Crea il briefing per oggi {day_name}. Scegli i task più importanti e distribuiscili negli slot."}],
        system,
        max_tokens=800
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("🎯 Cosa faccio adesso?", callback_data="now")],
        [InlineKeyboardButton("➕ Aggiungi qualcosa", callback_data="add")],
        [InlineKeyboardButton("📋 Tutto quello che ho", callback_data="list")],
        [InlineKeyboardButton("✅ Ho fatto una cosa", callback_data="done")],
        [InlineKeyboardButton("📅 La mia settimana", callback_data="week")],
    ]
    await update.message.reply_text(
        "👋 Ciao! Sono il tuo cervello esterno.\n\nCosa vuoi fare?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tasks = load_tasks()

    if query.data == "now":
        if not tasks:
            await query.message.reply_text("🎉 Non hai niente in lista!")
            return
        today = date.today()
        day_name = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"][today.weekday()]
        system = f"""Sei il cervello esterno di una persona con ADHD. Oggi è {day_name}.
Guarda il database e dimmi UNA SOLA cosa da fare ADESSO — la più urgente considerando l'ora del giorno.
Sono le {datetime.now().strftime('%H:%M')}.
Sii diretto, breve, umano. In italiano. Max 3 righe."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}],
            system
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "add":
        context.user_data["mode"] = "conversation"
        context.user_data["history"] = []
        await query.message.reply_text("Dimmi. Anche disordinato, anche in dialetto, anche a metà frase.")

    elif query.data == "list":
        if not tasks:
            await query.message.reply_text("Lista vuota!\n\n/start per il menu.")
            return
        system = """Sei il cervello esterno di una persona con ADHD.
Mostra tutto in lista, organizzato per area. Indica urgenza. Breve e chiaro. In italiano."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}],
            system
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "week":
        if not tasks:
            await query.message.reply_text("Lista vuota!\n\n/start per il menu.")
            return
        today = date.today()
        week_days = []
        for i in range(7):
            d = today + timedelta(days=i)
            week_days.append(d.strftime('%A %d/%m'))
        system = f"""Sei il cervello esterno di una persona con ADHD.
Crea una visione della settimana da oggi. Giorni: {', '.join(week_days)}.
Distribuisci i task più importanti nei giorni in modo realistico.
Max 2-3 task per giorno. Considera urgenze e scadenze.
In italiano, formato giorno per giorno."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}],
            system,
            max_tokens=1000
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "done":
        if not tasks:
            await query.message.reply_text("Non hai task in lista!\n\n/start per il menu.")
            return
        task_buttons = []
        for task in tasks[:8]:
            label = task.get("title", "Task")[:40]
            task_id = str(task.get("id", ""))
            task_buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"complete_{task_id}")])
        task_buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
        await query.message.reply_text("Quale hai fatto?", reply_markup=InlineKeyboardMarkup(task_buttons))

    elif query.data.startswith("complete_"):
        task_id = query.data[len("complete_"):]
        delete_task(task_id)
        await query.message.reply_text("✅ Ottimo! Rimosso.\n\n/start per il menu.")

    elif query.data == "cancel":
        await query.message.reply_text("Ok.\n\n/start per il menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    mode = context.user_data.get("mode")
    tasks = load_tasks()

    if mode == "conversation":
        history = context.user_data.get("history", [])
        history.append({"role": "user", "content": text})

        system = """Sei il cervello esterno di una persona con ADHD. Capisci cosa vuole ricordare o fare.

COMPORTAMENTO:
- Se chiaro → salva con JSON
- Se vago → fai UNA domanda
- Sei amico, non formale
- In italiano

QUANDO HAI CAPITO → JSON (senza backtick):
{"action": "save", "tasks": [{"title": "breve", "description": "dettaglio", "area": "lavoro|famiglia|salute|casa|altro", "urgency": "alta|media|bassa", "scheduled_date": "YYYY-MM-DD o null", "scheduled_time": "HH:MM o null", "recurring": "daily|weekly|null", "recurring_days": "lun,mar,mer,gio,ven o null", "slot": "mattina|club|pomeriggio|sera|null"}], "response": "conferma breve"}

QUANDO NON CAPISCI → JSON:
{"action": "ask", "response": "domanda"}"""

        raw = call_claude(history, system)
        logger.info(f"Claude: {raw}")

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
                    if save_task(task):
                        saved += 1
                context.user_data["mode"] = None
                context.user_data["history"] = []
                if saved > 0:
                    await update.message.reply_text(parsed["response"] + "\n\n/start per il menu.")
                else:
                    await update.message.reply_text("Ho capito ma problema nel salvare. Riprova.\n\n/start per il menu.")

        except Exception as e:
            logger.error(f"Parse error: {e} | Raw: {raw}")
            history.append({"role": "assistant", "content": raw})
            context.user_data["history"] = history
            await update.message.reply_text(raw + "\n\n(/start per il menu)")

    else:
        # Chat libera - può anche rimuovere task
        system = f"""Sei il cervello esterno di una persona con ADHD.
Database attuale: {json.dumps(tasks, ensure_ascii=False)}

IMPORTANTE: Se l'utente dice che ha fatto qualcosa (es. "ho chiamato il dentista", "fatto la spesa", "ho pagato"), 
identifica il task corrispondente e rispondi con JSON:
{{"action": "complete", "task_id": "id del task", "response": "ottimo! rimosso."}}

Se invece è una domanda o conversazione normale, rispondi normalmente in italiano, max 3 righe.
Non usare JSON per le risposte normali."""

        raw = call_claude([{"role": "user", "content": text}], system)
        
        try:
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            if clean.startswith("{"):
                parsed = json.loads(clean)
                if parsed.get("action") == "complete":
                    task_id = parsed.get("task_id")
                    if task_id:
                        delete_task(str(task_id))
                    await update.message.reply_text(parsed.get("response", "✅ Fatto!") + "\n\n/start per il menu.")
                    return
        except:
            pass
        
        await update.message.reply_text(raw + "\n\n/start per il menu.")

async def briefing_mattutino(context):
    tasks = load_tasks()
    if not tasks:
        return
    briefing = get_today_briefing(tasks)
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"☀️ Buongiorno!\n\n{briefing}\n\nRispondimi quando sei pronto. /start per il menu."
    )

async def reminder_pomeriggio(context):
    tasks = load_tasks()
    if not tasks:
        return
    system = f"""Sei il cervello esterno di una persona con ADHD. Sono le 15:00.
Slot pomeriggio: clienti sudamericani, lavoro, gestione.
Database: {json.dumps(tasks, ensure_ascii=False)}
Manda un promemoria breve per il pomeriggio. UNA o DUE cose. In italiano."""
    response = call_claude(
        [{"role": "user", "content": "Cosa devo fare questo pomeriggio?"}],
        system, max_tokens=300
    )
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"☀️ Pomeriggio!\n\n{response}\n\n/start per il menu."
    )

async def recap_serale(context):
    tasks = load_tasks()
    system = f"""Sei il cervello esterno di una persona con ADHD. Sono le 21:00.
Fai un recap serale breve: cosa resta da fare, cosa è prioritario domani mattina.
Database: {json.dumps(tasks, ensure_ascii=False)}
Max 5 righe. In italiano. Tono tranquillo."""
    response = call_claude(
        [{"role": "user", "content": "Recap della giornata e cosa fare domani."}],
        system, max_tokens=400
    )
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"🌙 Recap serale\n\n{response}\n\n/start per il menu."
    )

async def escalation(context):
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text="👋 Ehi! Hai visto il briefing di stamattina?\n\nDimmi una cosa sola che hai fatto oggi. Anche piccola.\n\n/start per il menu."
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    job_queue = app.job_queue
    if job_queue:
        # Briefing alle 8:00
        job_queue.run_daily(briefing_mattutino, time=datetime.strptime("08:00", "%H:%M").time())
        # Reminder pomeriggio alle 15:00
        job_queue.run_daily(reminder_pomeriggio, time=datetime.strptime("15:00", "%H:%M").time())
        # Recap serale alle 21:00
        job_queue.run_daily(recap_serale, time=datetime.strptime("21:00", "%H:%M").time())
        # Escalation alle 10:00 se non ha risposto
        job_queue.run_daily(escalation, time=datetime.strptime("10:00", "%H:%M").time())
    
    logger.info("Bot avviato!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
