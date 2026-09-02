"""
Bot Telegram per il nutrition coach.

Usa la stessa logica dell'agente CLI (vedi ../agent/core.py): l'unica cosa che
cambia è il canale con cui arrivano/escono i messaggi. Ogni chat Telegram viene
associata a uno user_id del backend al primo /start.

Uso:
    export ANTHROPIC_API_KEY=sk-ant-...
    export TELEGRAM_BOT_TOKEN=123456:ABC-...
    python telegram_bot.py

Richiede il backend FastAPI avviato (vedi README.md nella root del progetto).
"""

import json
import os
import sys
from pathlib import Path

from google.genai import types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Riusa la logica dell'agente definita in agent/core.py
sys.path.append(str(Path(__file__).resolve().parent.parent / "agent"))
import core  # noqa: E402

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAPPING_FILE = Path(__file__).resolve().parent / "chat_user_mapping.json"

# Stato in memoria: cronologia messaggi per ogni chat Telegram.
# Si perde se il bot viene riavviato — va bene per un MVP, in produzione
# andrebbe salvata anch'essa nel database.
conversations: dict[int, list] = {}


def load_mapping() -> dict[str, int]:
    if MAPPING_FILE.exists():
        return json.loads(MAPPING_FILE.read_text())
    return {}


def save_mapping(mapping: dict[str, int]) -> None:
    MAPPING_FILE.write_text(json.dumps(mapping, indent=2))


def get_user_id_for_chat(chat_id: int) -> int | None:
    mapping = load_mapping()
    return mapping.get(str(chat_id))


TELEGRAM_MAX_MESSAGE_LENGTH = 4000  # margine di sicurezza sotto il limite reale (4096)


async def send_long_message(update: Update, text: str) -> None:
    """
    Telegram rifiuta messaggi oltre 4096 caratteri. Se la risposta dell'agente
    è più lunga, la spezza in più messaggi consecutivi invece di far fallire
    l'invio.
    """
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        await update.message.reply_text(text)
        return

    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LENGTH):
        pezzo = text[i : i + TELEGRAM_MAX_MESSAGE_LENGTH]
        await update.message.reply_text(pezzo)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Comando /start: se la chat non è ancora collegata a un utente del backend,
    ne crea uno nuovo usando il nome Telegram. Se è già collegata, lo ricorda.
    """
    chat_id = update.effective_chat.id
    mapping = load_mapping()

    if str(chat_id) in mapping:
        user_id = mapping[str(chat_id)]
        await update.message.reply_text(
            f"Bentornato! Sei già collegato all'utente #{user_id}. "
            "Chiedimi pure quante calorie ti restano oggi."
        )
        return

    nome = update.effective_user.first_name or "Utente"
    try:
        user = core.create_backend_user(nome)
    except Exception as e:
        await update.message.reply_text(
            f"Non riesco a contattare il backend ({e}). "
            "È avviato su http://127.0.0.1:8000?"
        )
        return

    mapping[str(chat_id)] = user["id"]
    save_mapping(mapping)

    await update.message.reply_text(
        f"Ciao {nome}! Ti ho registrato come utente #{user['id']} "
        f"con obiettivo di {user['obiettivo_calorico_giornaliero']} kcal/giorno.\n\n"
        "Da ora puoi chiedermi cose come 'quante calorie mi restano oggi?' "
        "oppure dirmi cosa hai mangiato."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = get_user_id_for_chat(chat_id)

    if user_id is None:
        await update.message.reply_text(
            "Prima di iniziare, scrivi /start per registrarti."
        )
        return

    messages = conversations.setdefault(chat_id, [])
    messages.append(
        types.Content(role="user", parts=[types.Part.from_text(text=update.message.text)])
    )

    system_prompt = core.build_system_prompt(user_id)

    # run_turn fa chiamate bloccanti (Anthropic API + requests al backend):
    # la spostiamo in un thread separato per non bloccare il loop async del bot.
    import asyncio

    try:
        updated_messages, reply = await asyncio.to_thread(
            core.run_turn, messages, system_prompt
        )
        conversations[chat_id] = updated_messages
    except Exception as e:
        reply = f"Si è verificato un errore parlando con il backend o con Claude: {e}"

    await send_long_message(update, reply)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Imposta la variabile d'ambiente TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot Telegram avviato. Premi Ctrl+C per fermarlo.")
    app.run_polling()


if __name__ == "__main__":
    main()
