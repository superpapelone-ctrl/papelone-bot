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

def complete_task(task_id, tasks):
    """Complete a task. If recurring, update next date. Otherwise delete."""
    task = next((t for t in tasks if str(t.get("id")) == str(task_id)), None)
    if not task:
        logger.error(f"Task {task_id} not found")
        return False

    recurring = task.get("recurring")
    
    if recurring == "monthly":
        # Calculate next month same day
        anchor = task.get("recurring_anchor", 3)
        today = date.today()
        if today.month == 12:
            next_date = date(today.year + 1, 1, anchor)
        else:
            next_date = date(today.year, today.month + 1, anchor)
        update = {"scheduled_date": next_date.isoformat()}
        supabase_request("PATCH", "tasks", data=update, filters=f"id=eq.{task_id}")
        return "recurring"
    
    elif recurring == "weekly":
        next_date = date.today() + timedelta(days=7)
        update = {"scheduled_date": next_date.isoformat()}
        supabase_request("PATCH", "tasks", data=update, filters=f"id=eq.{task_id}")
        return "recurring"

    elif recurring == "daily":
        next_date = date.today() + timedelta(days=1)
        update = {"scheduled_date": next_date.isoformat()}
        supabase_request("PATCH", "tasks", data=update, filters=f"id=eq.{task_id}")
        return "recurring"
    
    else:
        supabase_request("DELETE", "tasks", filters=f"id=eq.{task_id}")
        return "deleted"

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

La giornata è divisa in slot:
- 🌅 MATTINA (9:00-13:00): lavoro, gestione affitti/finanze
- 🏌️ CLUB (13:00-15:00): qualcosa di leggero
- ☀️ POMERIGGIO (15:00-19:00): clienti sudamericani, lavoro, gestione
- 🌙 SERA (19:00+): personale, famiglia

REGOLE:
- MAX 2-3 task per slot
- Priorità a urgenze e scadenze vicine
- Task con data oggi o passata vanno PRIMA
- Sii motivante ma diretto
- In italiano

Database: {json.dumps(tasks, ensure_ascii=False)}"""

    return call_claude(
        [{"role": "user", "content": f"Crea il briefing per oggi {day_name}. Distribuisci i task negli slot."}],
        system, max_tokens=800
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
        system = f"""Sei il cervello esterno di una persona con ADHD. Ora sono le {datetime.now().strftime('%H:%M')} di {day_name}.
Dimmi UNA SOLA cosa da fare ADESSO considerando l'ora e il giorno.
Diretto, breve, umano. In italiano. Max 3 righe.
Database: {json.dumps(tasks, ensure_ascii=False)}"""
        response = call_claude([{"role": "user", "content": "Cosa faccio adesso?"}], system)
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
Mostra tutto in lista organizzato per area. Indica urgenza e data se presente. In italiano."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}], system
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "week":
        if not tasks:
            await query.message.reply_text("Lista vuota!\n\n/start per il menu.")
            return
        today = date.today()
        week_days = [(today + timedelta(days=i)).strftime('%A %d/%m') for i in range(7)]
        system = f"""Sei il cervello esterno di una persona con ADHD.
Crea visione settimanale da oggi. Giorni: {', '.join(week_days)}.
Max 2-3 task per giorno. Considera urgenze e scadenze. In italiano."""
        response = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}],
            system, max_tokens=1000
        )
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "done":
        if not tasks:
            await query.message.reply_text("Non hai task in lista!\n\n/start per il menu.")
            return
        context.user_data["tasks_snapshot"] = tasks
        task_buttons = []
        for task in tasks[:8]:
            label = task.get("title", "Task")[:40]
            task_id = str(task.get("id", ""))
            task_buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"complete_{task_id}")])
        task_buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
        await query.message.reply_text("Quale hai fatto?", reply_markup=InlineKeyboardMarkup(task_buttons))

    elif query.data.startswith("complete_"):
        task_id = query.data[len("complete_"):]
        tasks_snapshot = context.user_data.get("tasks_snapshot", tasks)
        result = complete_task(task_id, tasks_snapshot)
        if result == "recurring":
            await query.message.reply_text("✅ Ottimo! Aggiornato al prossimo appuntamento.\n\n/start per il menu.")
        else:
            await query.message.reply_text("✅ Rimosso dalla lista!\n\n/start per il menu.")

    elif query.data == "cancel":
        await query.message.reply_text("Ok.\n\n/start per il menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    mode = context.user_data.get("mode")
    tasks = load_tasks()

    if mode == "conversation":
        history = context.user_data.get("history", [])
        history.append({"role": "user", "content": text})

        system = """Sei il cervello esterno di una persona con ADHD.

QUANDO HAI CAPITO → JSON senza backtick:
{"action": "save", "tasks": [{"title": "breve", "description": "dettaglio", "area": "lavoro|famiglia|salute|casa|altro", "urgency": "alta|media|bassa", "scheduled_date": "YYYY-MM-DD o null", "scheduled_time": "HH:MM o null", "recurring": "daily|weekly|monthly|null", "recurring_anchor": numero_giorno_o_null, "recurring_days": "lun,mar,mer,gio,ven o null", "slot": "mattina|club|pomeriggio|sera|null"}], "response": "conferma breve"}

QUANDO NON CAPISCI → JSON:
{"action": "ask", "response": "domanda"}

Sei amico, non formale. In italiano."""

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
                    await update.message.reply_text("Problema nel salvare. Riprova.\n\n/start per il menu.")

        except Exception as e:
            logger.error(f"Parse error: {e} | Raw: {raw}")
            history.append({"role": "assistant", "content": raw})
            context.user_data["history"] = history
            await update.message.reply_text(raw + "\n\n(/start per il menu)")

    else:
        # Chat libera - gestisce anche completamento via testo
        system = f"""Sei il cervello esterno di una persona con ADHD.
Ora sono le {datetime.now().strftime('%H:%M')}.
Database: {json.dumps(tasks, ensure_ascii=False)}

Se l'utente dice che ha fatto qualcosa → JSON senza backtick:
{{"action": "complete", "task_id": "id esatto dal database", "response": "messaggio breve"}}

Se è una domanda o conversazione → rispondi normalmente in italiano, max 3 righe."""

        raw = call_claude([{"role": "user", "content": text}], system)

        try:
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            if clean.startswith("{"):
                parsed = json.loads(clean)
                if parsed.get("action") == "complete":
                    task_id = str(parsed.get("task_id", ""))
                    if task_id:
                        result = complete_task(task_id, tasks)
                        if result == "recurring":
                            msg = "✅ " + parsed.get("response", "Fatto!") + " (aggiornato al prossimo appuntamento)"
                        else:
                            msg = "✅ " + parsed.get("response", "Rimosso!")
                        await update.message.reply_text(msg + "\n\n/start per il menu.")
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
    today = date.today()
    day_name = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"][today.weekday()]
    system = f"""Sei il cervello esterno di una persona con ADHD. Sono le 15:00 di {day_name}.
Slot pomeriggio: clienti sudamericani, lavoro, gestione.
Database: {json.dumps(tasks, ensure_ascii=False)}
Due cose max per il pomeriggio. In italiano."""
    response = call_claude([{"role": "user", "content": "Cosa faccio questo pomeriggio?"}], system, max_tokens=300)
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"☀️ Pomeriggio!\n\n{response}\n\n/start per il menu."
    )

async def recap_serale(context):
    tasks = load_tasks()
    system = f"""Sei il cervello esterno di una persona con ADHD. Sono le 21:00.
Recap serale: cosa resta, cosa è prioritario domani.
Database: {json.dumps(tasks, ensure_ascii=False)}
Max 5 righe. In italiano. Tono tranquillo."""
    response = call_claude([{"role": "user", "content": "Recap serale."}], system, max_tokens=400)
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"🌙 Recap serale\n\n{response}\n\n/start per il menu."
    )

async def escalation(context):
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text="👋 Ehi! Hai visto il briefing di stamattina?\n\nDimmi UNA cosa che hai fatto oggi. Anche piccola.\n\n/start per il menu."
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    job_queue = app.job_queue
    if job_queue:
        from datetime import time as dtime
        import pytz
        tz = pytz.timezone("Atlantic/Canary")
        job_queue.run_daily(briefing_mattutino, time=dtime(8, 0, tzinfo=tz))
        job_queue.run_daily(escalation, time=dtime(10, 0, tzinfo=tz))
        job_queue.run_daily(reminder_pomeriggio, time=dtime(15, 0, tzinfo=tz))
        job_queue.run_daily(recap_serale, time=dtime(21, 0, tzinfo=tz))

    logger.info("Bot avviato!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
