import os
import json
import logging
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
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

DAY_MAP = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}

def calculate_next_date(current_date, recurrence_rule):
    """
    Parse and calculate next date from recurrence_rule.
    Format: every:N:unit[:modifier:value]
    Examples:
      every:1:day
      every:2:day  (giorni alterni)
      every:14:day (ogni 14 giorni)
      every:1:week
      every:1:week:mon,wed,fri  (solo certi giorni)
      every:1:month
      every:1:month:day:3  (ogni mese il giorno 3)
      every:3:month
      every:1:year
    """
    if not recurrence_rule:
        return None

    parts = recurrence_rule.split(":")
    if len(parts) < 3:
        return None

    n = int(parts[1])
    unit = parts[2]

    if unit == "day":
        return current_date + timedelta(days=n)

    elif unit == "week":
        if len(parts) > 3:
            # specific days of week
            days = [DAY_MAP[d.strip()] for d in parts[3].split(",") if d.strip() in DAY_MAP]
            if days:
                next_d = current_date + timedelta(days=1)
                for _ in range(14):  # look ahead 2 weeks max
                    if next_d.weekday() in days:
                        return next_d
                    next_d += timedelta(days=1)
        return current_date + timedelta(weeks=n)

    elif unit == "month":
        if len(parts) > 4 and parts[3] == "day":
            day = int(parts[4])
            next_month = current_date + relativedelta(months=n)
            try:
                return next_month.replace(day=day)
            except:
                return next_month
        return current_date + relativedelta(months=n)

    elif unit == "year":
        return current_date + relativedelta(years=n)

    return None

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
    task = next((t for t in tasks if str(t.get("id")) == str(task_id)), None)
    if not task:
        return "deleted"

    rule = task.get("recurrence_rule")
    if rule:
        next_date = calculate_next_date(date.today(), rule)
        if next_date:
            supabase_request("PATCH", "tasks", data={"scheduled_date": next_date.isoformat()}, filters=f"id=eq.{task_id}")
            return f"recurring:{next_date.strftime('%d/%m/%Y')}"

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
- Priorità a urgenze e scadenze vicine o scadute
- Task ricorrenti di oggi vanno inclusi
- Sii motivante ma diretto
- In italiano

Database: {json.dumps(tasks, ensure_ascii=False)}"""
    return call_claude(
        [{"role": "user", "content": f"Briefing per oggi {day_name}."}],
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
        [InlineKeyboardButton("🔄 Ricalibra urgenze ora", callback_data="recalibrate")],
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
UNA SOLA cosa da fare ADESSO considerando l'ora. Diretto, breve. In italiano. Max 3 righe.
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
Mostra tutto in lista organizzato per area. Indica urgenza, data e ricorrenza. In italiano."""
        response = call_claude([{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}], system)
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "week":
        if not tasks:
            await query.message.reply_text("Lista vuota!\n\n/start per il menu.")
            return
        today = date.today()
        week_days = [(today + timedelta(days=i)).strftime('%A %d/%m') for i in range(7)]
        system = f"""Sei il cervello esterno di una persona con ADHD.
Visione settimanale da oggi. Giorni: {', '.join(week_days)}.
Max 3 task per giorno. Considera urgenze, scadenze e ricorrenze. In italiano."""
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
            is_recurring = bool(task.get("recurrence_rule"))
            prefix = "🔄 " if is_recurring else "✅ "
            task_id = str(task.get("id", ""))
            task_buttons.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"complete_{task_id}")])
        task_buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
        await query.message.reply_text("Quale hai fatto?", reply_markup=InlineKeyboardMarkup(task_buttons))

    elif query.data == "cancel":
        await query.message.reply_text("Ok.\n\n/start per il menu.")
        task_id = query.data[len("complete_"):]
        tasks_snapshot = context.user_data.get("tasks_snapshot", tasks)
        result = complete_task(task_id, tasks_snapshot)
        if result.startswith("recurring:"):
            next_date = result.split(":")[1]
            await query.message.reply_text(f"✅ Ottimo! Prossimo appuntamento: {next_date}\n\n/start per il menu.")
        else:
            await query.message.reply_text("✅ Rimosso dalla lista!\n\n/start per il menu.")

    elif query.data == "recalibrate":
        await query.message.reply_text("🔄 Sto analizzando tutti i task... un momento.")
        tasks = load_tasks()
        today = date.today()
        system = f"""Sei un sistema di gestione della vita di una persona con ADHD. Oggi è {today.strftime('%d/%m/%Y')}.

Ricalibra l'urgenza di ogni task in base a:
1. Scadenze vicine (entro 3 giorni = alta, entro 7 giorni = media)
2. Scadenze passate = sempre alta
3. Salute (propria o dei figli) = sempre alta
4. Lavoro con scadenza imminente = alta
5. Task creati da più di 30 giorni = aumenta urgenza
6. Viaggi/piacere senza data = bassa
7. Ricorrenti giornalieri = media

Rispondi SOLO con JSON senza backtick:
{{"updates": [{{"id": "uuid", "urgency": "alta|media|bassa", "reason": "motivo breve"}}]}}

Solo i task che cambiano urgenza."""

        raw = call_claude(
            [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}],
            system, max_tokens=2000
        )

        try:
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            updates = parsed.get("updates", [])
            changed = []
            for update in updates:
                task_id = update.get("id")
                new_urgency = update.get("urgency")
                reason = update.get("reason", "")
                if task_id and new_urgency:
                    supabase_request("PATCH", "tasks", data={"urgency": new_urgency}, filters=f"id=eq.{task_id}")
                    task = next((t for t in tasks if str(t.get("id")) == str(task_id)), None)
                    if task:
                        changed.append(f"• {task.get('title', '?')} → {new_urgency} ({reason})")
            if changed:
                msg = "✅ Ricalibrazione completata:\n\n" + "\n".join(changed[:15])
            else:
                msg = "✅ Tutto ok — nessuna urgenza da cambiare."
            await query.message.reply_text(msg + "\n\n/start per il menu.")
        except Exception as e:
            logger.error(f"Recalibrate error: {e}")
            await query.message.reply_text("Problema nella ricalibrazione. Riprova.\n\n/start per il menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    mode = context.user_data.get("mode")
    tasks = load_tasks()

    if mode == "conversation":
        history = context.user_data.get("history", [])
        history.append({"role": "user", "content": text})

        system = """Sei il cervello esterno di una persona con ADHD.

Formato ricorrenza (recurrence_rule):
- every:1:day → ogni giorno
- every:2:day → giorni alterni
- every:14:day → ogni 14 giorni
- every:1:week → ogni settimana
- every:1:week:mon,tue,wed,thu,fri → lun-ven
- every:1:week:mon,wed,fri → lun, mer, ven
- every:1:month → ogni mese
- every:1:month:day:3 → ogni mese il giorno 3
- every:3:month → ogni 3 mesi
- every:6:month → ogni 6 mesi
- every:1:year → ogni anno

QUANDO HAI CAPITO → JSON senza backtick:
{"action": "save", "tasks": [{"title": "breve", "description": "dettaglio", "area": "lavoro|famiglia|salute|casa|altro", "urgency": "alta|media|bassa", "scheduled_date": "YYYY-MM-DD o null", "scheduled_time": "HH:MM o null", "recurrence_rule": "regola o null", "slot": "mattina|club|pomeriggio|sera|null"}], "response": "conferma breve"}

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
        system = f"""Sei il cervello esterno di una persona con ADHD.
Ora sono le {datetime.now().strftime('%H:%M')}.
Database: {json.dumps(tasks, ensure_ascii=False)}

Se l'utente dice che ha FATTO qualcosa → JSON senza backtick:
{{"action": "complete", "task_id": "id esatto", "response": "messaggio"}}

Se l'utente vuole ELIMINARE definitivamente un task → JSON senza backtick:
{{"action": "delete", "task_id": "id esatto", "response": "messaggio"}}

Altrimenti rispondi normalmente in italiano, max 3 righe."""

        raw = call_claude([{"role": "user", "content": text}], system)

        try:
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            if clean.startswith("{"):
                parsed = json.loads(clean)
                action = parsed.get("action")
                task_id = str(parsed.get("task_id", ""))

                if action == "complete" and task_id:
                    result = complete_task(task_id, tasks)
                    if result.startswith("recurring:"):
                        next_date = result.split(":")[1]
                        msg = f"✅ {parsed.get('response', 'Fatto!')} — prossimo: {next_date}"
                    else:
                        msg = "✅ " + parsed.get("response", "Rimosso!")
                    await update.message.reply_text(msg + "\n\n/start per il menu.")
                    return

                elif action == "delete" and task_id:
                    supabase_request("DELETE", "tasks", filters=f"id=eq.{task_id}")
                    msg = "🗑️ " + parsed.get("response", "Task eliminato definitivamente.")
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

async def ricalibra_urgenze(context):
    """Nightly job: Claude reviews all tasks and recalibrates urgency."""
    tasks = load_tasks()
    if not tasks:
        return

    today = date.today()
    system = f"""Sei un sistema di gestione della vita di una persona con ADHD. Oggi è {today.strftime('%d/%m/%Y')}.

Il tuo compito è ricalibrate l'urgenza di ogni task in base a:
1. Scadenze vicine (entro 3 giorni = alta, entro 7 giorni = media)
2. Scadenze passate = sempre alta
3. Salute (propria o dei figli) = sempre alta
4. Lavoro con scadenza imminente = alta
5. Task creati da più di 30 giorni senza essere completati = aumenta urgenza
6. Viaggi/piacere senza data = bassa
7. Ricorrenti giornalieri = media (non sempre alta)

Rispondi SOLO con JSON senza backtick, lista di aggiornamenti:
{{"updates": [{{"id": "uuid", "urgency": "alta|media|bassa", "reason": "motivo breve"}}]}}

Solo i task che cambiano urgenza. Se va bene com'è, non includerlo."""

    raw = call_claude(
        [{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}],
        system, max_tokens=2000
    )

    try:
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        updates = parsed.get("updates", [])
        
        changed = []
        for update in updates:
            task_id = update.get("id")
            new_urgency = update.get("urgency")
            reason = update.get("reason", "")
            if task_id and new_urgency:
                supabase_request("PATCH", "tasks", data={"urgency": new_urgency}, filters=f"id=eq.{task_id}")
                task = next((t for t in tasks if str(t.get("id")) == str(task_id)), None)
                if task:
                    changed.append(f"• {task.get('title', '?')} → {new_urgency} ({reason})")
        
        if changed:
            msg = "🔄 *Urgenze ricalibrate stanotte:*\n\n" + "\n".join(changed[:10])
            await context.bot.send_message(chat_id=CHAT_ID, text=msg)
            logger.info(f"Ricalibrazione: {len(changed)} task aggiornati")
        else:
            logger.info("Ricalibrazione: nessun cambio necessario")

    except Exception as e:
        logger.error(f"Ricalibrazione error: {e} | Raw: {raw}")

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
        job_queue.run_daily(ricalibra_urgenze, time=dtime(2, 0, tzinfo=tz))

    logger.info("Bot avviato!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
