import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CHAT_ID = 1077588790

DB_FILE = "/home/claude/adhd-bot/database.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {
        "tasks": [],
        "context": {},
        "history": []
    }

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def get_claude_response(prompt, db):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    system = """Sei l'assistente personale di una persona con ADHD. 
Il tuo compito è gestire la sua vita: lavoro, famiglia, salute, casa, tutto.

REGOLE FONDAMENTALI:
1. Dai SEMPRE una sola cosa da fare adesso. Mai liste lunghe.
2. Sii diretto, breve, concreto. Niente filosofia.
3. Non giudicare mai. L'ADHD non è pigrizia.
4. Quando aggiungi un task, conferma con una frase sola.
5. Quando ti chiede cosa fare, dai LA COSA PIÙ URGENTE/IMPORTANTE in quel momento.
6. Rispondi sempre in italiano.
7. Usa emoji con parsimonia ma in modo utile.

Database attuale della sua vita:
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
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "👋 Ciao! Sono il tuo cervello esterno.\n\nCosa vuoi fare?",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    db = load_db()
    
    if query.data == "now":
        if not db["tasks"]:
            await query.message.reply_text("🎉 Non hai niente in lista. Goditi il momento!")
            return
        response = get_claude_response("Dimmi UNA SOLA cosa che devo fare adesso. La più urgente o importante. Solo quella.", db)
        await query.message.reply_text(response)
        
    elif query.data == "add":
        context.user_data["waiting_for"] = "add"
        await query.message.reply_text("Dimmi cosa devo ricordare. Scrivi tutto, anche disordinato:")
        
    elif query.data == "list":
        if not db["tasks"]:
            await query.message.reply_text("Lista vuota. Inizia ad aggiungere cose!")
            return
        response = get_claude_response("Mostrami tutto quello che ho in lista, organizzato per area (lavoro, famiglia, salute, casa). Breve.", db)
        await query.message.reply_text(response)
        
    elif query.data == "done":
        if not db["tasks"]:
            await query.message.reply_text("Non hai task in lista!")
            return
        context.user_data["waiting_for"] = "done"
        # Show tasks as buttons
        task_buttons = []
        for i, task in enumerate(db["tasks"][:8]):  # max 8
            label = task.get("title", task.get("description", "Task"))[:40]
            task_buttons.append([InlineKeyboardButton(f"✅ {label}", callback_data=f"complete_{i}")])
        task_buttons.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel")])
        await query.message.reply_text(
            "Quale hai fatto?",
            reply_markup=InlineKeyboardMarkup(task_buttons)
        )
    
    elif query.data.startswith("complete_"):
        idx = int(query.data.split("_")[1])
        if idx < len(db["tasks"]):
            task = db["tasks"].pop(idx)
            db["history"].append({
                "task": task,
                "completed_at": datetime.now().isoformat()
            })
            save_db(db)
            await query.message.reply_text(f"✅ Fatto! Bene.\n\nVuoi sapere cosa fare adesso? Scrivi /start")
    
    elif query.data == "cancel":
        await query.message.reply_text("Ok, nessun problema. /start per tornare al menu.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    db = load_db()
    
    waiting = context.user_data.get("waiting_for")
    
    if waiting == "add":
        # Use Claude to parse and structure the input
        parse_prompt = f"""L'utente ha detto: "{text}"

Estrai tutti i task/impegni/cose da ricordare e restituisci un JSON così:
{{
  "tasks": [
    {{"title": "titolo breve", "description": "dettaglio", "area": "lavoro|famiglia|salute|casa|altro", "urgency": "alta|media|bassa"}}
  ],
  "response": "conferma breve in italiano di cosa hai aggiunto"
}}

Rispondi SOLO con il JSON, niente altro."""
        
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": parse_prompt}]
        )
        
        try:
            raw = response.content[0].text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            
            for task in parsed["tasks"]:
                task["created_at"] = datetime.now().isoformat()
                db["tasks"].append(task)
            
            save_db(db)
            context.user_data["waiting_for"] = None
            await update.message.reply_text(parsed["response"] + "\n\n/start per tornare al menu.")
        except:
            await update.message.reply_text("Ho capito, ma ho avuto un problema tecnico. Riprova.")
    
    else:
        # Free conversation with Claude
        response = get_claude_response(text, db)
        await update.message.reply_text(response + "\n\n/start per il menu.")

async def remind(context: ContextTypes.DEFAULT_TYPE):
    """Send proactive reminder"""
    db = load_db()
    if not db["tasks"]:
        return
    
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Sei l'assistente di una persona con ADHD. Manda un promemoria breve e motivante su cosa fare adesso. Una cosa sola. In italiano.",
        messages=[{"role": "user", "content": f"Database: {json.dumps(db, ensure_ascii=False)}. Dimmi UNA cosa da fare adesso."}]
    )
    
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"⏰ Ehi!\n\n{response.content[0].text}\n\n/start per il menu"
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Proactive reminders every 3 hours
    job_queue = app.job_queue
    job_queue.run_repeating(remind, interval=10800, first=10800)
    
    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
