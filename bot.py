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
            days = [DAY_MAP[d.strip()] for d in parts[3].split(",") if d.strip() in DAY_MAP]
            if days:
                next_d = current_date + timedelta(days=1)
                for _ in range(14):
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

def get_push_token():
    """Get push token from Supabase."""
    result = supabase_request("GET", "push_tokens", filters="limit=1")
    if result and len(result) > 0:
        return result[0].get("token")
    return None

def send_push_notification(title, body):
    """Send push notification via Expo Push API."""
    token = get_push_token()
    if not token:
        logger.info("No push token found, skipping push notification")
        return
    try:
        payload = json.dumps({
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "priority": "high",
            "channelId": "default"
        }).encode()
        req = urllib.request.Request(
            "https://exp.host/--/api/v2/push/send",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            logger.info(f"Push sent: {result}")
    except Exception as e:
        logger.error(f"Push notification error: {e}")

def load_tasks():
    result = supabase_request("GET", "tasks", filters="order=urgency.asc,created_at.asc")
    return result if result else []

def save_task(task):
    task["created_at"] = datetime.now().isoformat()
    return supabase_request("POST", "tasks", data=task)

def update_task(task_id, changes):
    return supabase_request("PATCH", "tasks", data=changes, filters=f"id=eq.{task_id}")

def delete_task(task_id):
    return supabase_request("DELETE", "tasks", filters=f"id=eq.{task_id}")

def complete_task(task_id, tasks):
    task = next((t for t in tasks if str(t.get("id")) == str(task_id)), None)
    if not task:
        return "deleted"
    rule = task.get("recurrence_rule")
    if rule:
        next_date = calculate_next_date(date.today(), rule)
        if next_date:
            update_task(task_id, {"scheduled_date": next_date.isoformat()})
            return f"recurring:{next_date.strftime('%d/%m/%Y')}"
    delete_task(task_id)
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

def get_agent_decision(user_message, tasks, conversation_history=None):
    """Core agent: Claude reasons about what to do and asks for approval."""
    today = date.today()
    day_name = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"][today.weekday()]
    
    system = f"""Sei un agente intelligente che gestisce la vita di una persona con ADHD. 
Oggi è {day_name} {today.strftime('%d/%m/%Y')}, ore {datetime.now().strftime('%H:%M')}.

Hai accesso completo al database. Puoi aggiungere, modificare, eliminare, riorganizzare qualsiasi cosa.

Database attuale:
{json.dumps(tasks, ensure_ascii=False, indent=2)}

COME FUNZIONI:
1. Leggi il messaggio dell'utente
2. Ragiona su cosa fare
3. Proponi UN'azione chiara e chiedi approvazione
4. Spiega brevemente perché

Rispondi SEMPRE con questo JSON (senza backtick):
{{
  "reasoning": "cosa hai capito e cosa vuoi fare",
  "action": "save|update|delete|complete|chat|multi",
  "operations": [
    {{
      "type": "save|update|delete|complete",
      "task_id": "id o null per save",
      "data": {{}},
      "description": "cosa fai in italiano"
    }}
  ],
  "approval_message": "messaggio all'utente che spiega cosa farai e chiede conferma. Max 3 righe. Se è solo chat non chiedere approvazione.",
  "needs_approval": true
}}

Se è una domanda o chat normale: needs_approval = false, action = "chat", operations = []
Se l'utente dice sì/no/confermo/annulla: esegui o annulla l'operazione pendente.

Sii intelligente — se l'utente dice "ho chiamato zio andrea" capisci che è il task "Chiamare Zio Andrea" e proponi di completarlo.
Se dice "aggiungi candidature martedì giovedì venerdì mattina alle 11" crea il task ricorrente corretto.
"""

    messages = conversation_history or []
    messages = messages + [{"role": "user", "content": user_message}]
    
    raw = call_claude(messages, system, max_tokens=1000)
    logger.info(f"Agent decision: {raw}")
    return raw

def execute_operations(operations, tasks):
    """Execute approved operations."""
    results = []
    for op in operations:
        op_type = op.get("type")
        task_id = op.get("task_id")
        data = op.get("data", {})
        
        if op_type == "save":
            data["created_at"] = datetime.now().isoformat()
            result = supabase_request("POST", "tasks", data=data)
            results.append("✅ " + op.get("description", "Aggiunto"))
            
        elif op_type == "update" and task_id:
            update_task(task_id, data)
            results.append("✏️ " + op.get("description", "Aggiornato"))
            
        elif op_type == "delete" and task_id:
            delete_task(task_id)
            results.append("🗑️ " + op.get("description", "Eliminato"))
            
        elif op_type == "complete" and task_id:
            result = complete_task(task_id, tasks)
            if result.startswith("recurring:"):
                next_date = result.split(":")[1]
                results.append(f"✅ {op.get('description', 'Completato')} — prossimo: {next_date}")
            else:
                results.append("✅ " + op.get("description", "Completato e rimosso"))
    
    return results

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
UNA SOLA cosa da fare ADESSO. Diretto, breve. In italiano. Max 3 righe.
Database: {json.dumps(tasks, ensure_ascii=False)}"""
        response = call_claude([{"role": "user", "content": "Cosa faccio adesso?"}], system)
        await query.message.reply_text(response + "\n\n/start per il menu.")

    elif query.data == "add":
        context.user_data["mode"] = "agent"
        context.user_data["history"] = []
        context.user_data["pending"] = None
        await query.message.reply_text("Dimmi tutto. Anche disordinato.")

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

    elif query.data.startswith("complete_"):
        task_id = query.data[len("complete_"):]
        tasks_snapshot = context.user_data.get("tasks_snapshot", tasks)
        result = complete_task(task_id, tasks_snapshot)
        if result.startswith("recurring:"):
            next_date = result.split(":")[1]
            await query.message.reply_text(f"✅ Fatto! Prossimo: {next_date}\n\n/start per il menu.")
        else:
            await query.message.reply_text("✅ Rimosso!\n\n/start per il menu.")

    elif query.data == "recalibrate":
        await query.message.reply_text("🔄 Sto analizzando...")
        today = date.today()
        system = f"""Ricalibra urgenze. Oggi è {today.strftime('%d/%m/%Y')}.
Criteri: scadenze vicine=alta, salute=alta, viaggi piacere=bassa, ricorrenti daily=media.
JSON senza backtick: {{"updates": [{{"id": "uuid", "urgency": "alta|media|bassa", "reason": "motivo"}}]}}
Solo task che cambiano."""
        raw = call_claude([{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}], system, max_tokens=2000)
        try:
            clean = raw.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            changed = []
            for u in parsed.get("updates", []):
                if u.get("id") and u.get("urgency"):
                    update_task(u["id"], {"urgency": u["urgency"]})
                    task = next((t for t in tasks if str(t.get("id")) == u["id"]), None)
                    if task:
                        changed.append(f"• {task.get('title','?')} → {u['urgency']} ({u.get('reason','')})")
            msg = "✅ " + ("\n".join(changed[:15]) if changed else "Tutto ok, nessun cambio.")
            await query.message.reply_text(msg + "\n\n/start per il menu.")
        except Exception as e:
            await query.message.reply_text("Errore ricalibrazione.\n\n/start per il menu.")

    elif query.data == "approve":
        pending = context.user_data.get("pending")
        if pending:
            tasks_snap = load_tasks()
            results = execute_operations(pending, tasks_snap)
            context.user_data["pending"] = None
            context.user_data["history"] = []
            msg = "\n".join(results) if results else "Fatto!"
            await query.message.reply_text(msg + "\n\n/start per il menu.")
        else:
            await query.message.reply_text("Niente da approvare.\n\n/start per il menu.")

    elif query.data == "reject":
        context.user_data["pending"] = None
        await query.message.reply_text("Ok, annullato. Dimmi cosa vuoi fare diversamente.")

    elif query.data == "cancel":
        context.user_data.clear()
        await query.message.reply_text("Ok.\n\n/start per il menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    tasks = load_tasks()

    # Handle yes/no for pending approval
    pending = context.user_data.get("pending")
    if pending:
        text_lower = text.lower().strip()
        if any(w in text_lower for w in ["sì", "si", "yes", "ok", "confermo", "vai", "esegui", "fatto", "certo"]):
            results = execute_operations(pending, tasks)
            context.user_data["pending"] = None
            context.user_data["history"] = []
            msg = "\n".join(results) if results else "Fatto!"
            await update.message.reply_text(msg + "\n\n/start per il menu.")
            return
        elif any(w in text_lower for w in ["no", "annulla", "stop", "cancella", "aspetta"]):
            context.user_data["pending"] = None
            await update.message.reply_text("Annullato. Dimmi cosa vuoi fare diversamente.")
            return

    # Agent mode
    history = context.user_data.get("history", [])
    raw = get_agent_decision(text, tasks, history)

    try:
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        idx = clean.find("{")
        if idx >= 0:
            clean = clean[idx:]
        parsed = json.loads(clean)

        approval_msg = parsed.get("approval_message", "")
        needs_approval = parsed.get("needs_approval", False)
        operations = parsed.get("operations", [])
        action = parsed.get("action", "chat")

        # Update conversation history
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": raw})
        context.user_data["history"] = history[-10:]  # keep last 10 messages

        if needs_approval and operations:
            context.user_data["pending"] = operations
            keyboard = [
                [InlineKeyboardButton("✅ Sì, fallo", callback_data="approve"),
                 InlineKeyboardButton("❌ No, annulla", callback_data="reject")]
            ]
            await update.message.reply_text(
                approval_msg,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Chat response or immediate action
            if action == "chat":
                await update.message.reply_text(approval_msg + "\n\n/start per il menu.")
            elif operations and not needs_approval:
                results = execute_operations(operations, tasks)
                context.user_data["history"] = []
                msg = approval_msg + "\n\n" + "\n".join(results) if results else approval_msg
                await update.message.reply_text(msg + "\n\n/start per il menu.")
            else:
                await update.message.reply_text(approval_msg + "\n\n/start per il menu.")

    except Exception as e:
        logger.error(f"Agent error: {e} | Raw: {raw}")
        await update.message.reply_text(raw + "\n\n/start per il menu.")

async def briefing_mattutino(context):
    tasks = load_tasks()
    if not tasks:
        return
    briefing = get_today_briefing(tasks)
    msg = f"☀️ Buongiorno!\n\n{briefing}\n\nRispondimi quando sei pronto."
    await context.bot.send_message(chat_id=CHAT_ID, text=msg)
    send_push_notification("☀️ Buongiorno!", briefing[:100] + "...")

async def reminder_pomeriggio(context):
    tasks = load_tasks()
    if not tasks:
        return
    today = date.today()
    day_name = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"][today.weekday()]
    system = f"""Cervello esterno ADHD. Sono le 15:00 di {day_name}.
Database: {json.dumps(tasks, ensure_ascii=False)}
Due cose max per il pomeriggio. In italiano."""
    response = call_claude([{"role": "user", "content": "Cosa faccio questo pomeriggio?"}], system, max_tokens=300)
    await context.bot.send_message(chat_id=CHAT_ID, text=f"☀️ Pomeriggio!\n\n{response}\n\n/start per il menu.")
    send_push_notification("☀️ Pomeriggio!", response[:100] + "...")

async def recap_serale(context):
    tasks = load_tasks()
    system = f"""Cervello esterno ADHD. Sono le 21:00.
Database: {json.dumps(tasks, ensure_ascii=False)}
Recap serale breve. In italiano. Tono tranquillo."""
    response = call_claude([{"role": "user", "content": "Recap serale."}], system, max_tokens=400)
    await context.bot.send_message(chat_id=CHAT_ID, text=f"🌙 Recap serale\n\n{response}\n\n/start per il menu.")
    send_push_notification("🌙 Recap serale", response[:100] + "...")

async def check_proattivo(context):
    tasks = load_tasks()
    if not tasks:
        return
    now = datetime.now()
    if now.hour < 9 or now.hour >= 22:
        return
    today = date.today()
    day_name = ["Lunedì","Martedì","Mercoledì","Giovedì","Venerdì","Sabato","Domenica"][today.weekday()]
    system = f"""Cervello esterno ADHD. Ora sono le {now.strftime('%H:%M')} di {day_name}.
Decidi autonomamente se mandare un messaggio proattivo.
Manda solo se c'è qualcosa di urgente o un momento chiave.
JSON senza backtick: {{"send": true, "message": "messaggio"}} oppure {{"send": false}}
Database: {json.dumps(tasks, ensure_ascii=False)}"""
    try:
        raw = call_claude([{"role": "user", "content": "Devo mandare un messaggio proattivo?"}], system, max_tokens=300)
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        if parsed.get("send") and parsed.get("message"):
            await context.bot.send_message(chat_id=CHAT_ID, text=f"🧠 {parsed['message']}\n\n/start per il menu.")
            send_push_notification("🧠 Papelone", parsed['message'])
    except Exception as e:
        logger.error(f"Proattivo error: {e}")

async def ricalibra_urgenze(context):
    tasks = load_tasks()
    if not tasks:
        return
    today = date.today()
    system = f"""Ricalibra urgenze. Oggi {today.strftime('%d/%m/%Y')}.
JSON: {{"updates": [{{"id": "uuid", "urgency": "alta|media|bassa", "reason": "motivo"}}]}}
Solo task che cambiano."""
    try:
        raw = call_claude([{"role": "user", "content": f"Database: {json.dumps(tasks, ensure_ascii=False)}"}], system, max_tokens=2000)
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        changed = []
        for u in parsed.get("updates", []):
            if u.get("id") and u.get("urgency"):
                update_task(u["id"], {"urgency": u["urgency"]})
                task = next((t for t in tasks if str(t.get("id")) == u["id"]), None)
                if task:
                    changed.append(f"• {task.get('title','?')} → {u['urgency']}")
        if changed:
            await context.bot.send_message(chat_id=CHAT_ID, text="🔄 Urgenze ricalibrate:\n\n" + "\n".join(changed[:10]))
    except Exception as e:
        logger.error(f"Ricalibrazione error: {e}")

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
        job_queue.run_daily(reminder_pomeriggio, time=dtime(15, 0, tzinfo=tz))
        job_queue.run_daily(recap_serale, time=dtime(21, 0, tzinfo=tz))
        job_queue.run_daily(ricalibra_urgenze, time=dtime(2, 0, tzinfo=tz))
        for hour in [9, 11, 13, 17, 19]:
            job_queue.run_daily(check_proattivo, time=dtime(hour, 0, tzinfo=tz))

    logger.info("Agente avviato!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
