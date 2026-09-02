"""
Backend del nutrition coach + webhook Telegram, pensato per girare come
UNICO servizio (comodo per il deploy gratuito su Render: un solo processo
che riceve richieste HTTP, sia dall'utente/documentazione sia da Telegram).

Avvio locale:
    uvicorn main:app --reload

Documentazione interattiva generata automaticamente su:
    http://127.0.0.1:8000/docs

Per collegare Telegram, dopo il deploy imposta il webhook una volta sola:
    https://api.telegram.org/bot<IL_TUO_TOKEN>/setWebhook?url=https://<il-tuo-servizio>.onrender.com/telegram/webhook
"""

import asyncio
import os
from datetime import date

import requests
from fastapi import FastAPI, Depends, HTTPException, Request
from google.genai import types
from sqlalchemy.orm import Session

import models
import schemas
import crud
import agent_core
from database import engine, get_db

# Crea le tabelle se non esistono già (per un progetto più maturo si
# userebbe Alembic per le migrazioni, ma per l'MVP va benissimo così)
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Nutrition Coach API",
    description="Backend per il coach nutrizionale AI, con dati di attività da Zepp/Terra e pasti registrati.",
    version="0.1.0",
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_MAX_MESSAGE_LENGTH = 4000  # margine di sicurezza sotto il limite reale (4096)

# Cronologia delle conversazioni in memoria: chat_id -> lista di types.Content.
# Si perde se il servizio si riavvia (es. dopo lo sleep di Render) — il
# bilancio calorico no, quello resta nel database.
conversations: dict[int, list] = {}


# ---------- Users (API dirette, utili per test/debug da /docs) ----------

@app.post("/users", response_model=schemas.UserOut)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    return crud.create_user(db, user)


@app.get("/users/{user_id}", response_model=schemas.UserOut)
def read_user(user_id: int, db: Session = Depends(get_db)):
    db_user = crud.get_user(db, user_id)
    if db_user is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return db_user


# ---------- Activity ----------

@app.put("/users/{user_id}/activity", response_model=schemas.ActivityOut)
def upsert_activity(
    user_id: int, activity: schemas.ActivityUpsert, db: Session = Depends(get_db)
):
    """
    Crea o aggiorna l'attività di un giorno. Per ora questo endpoint viene
    chiamato manualmente o dall'agente; in futuro sarà anche il target dei
    webhook inviati da Terra API quando arrivano nuovi dati dallo smartwatch.
    """
    if crud.get_user(db, user_id) is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return crud.upsert_daily_activity(db, user_id, activity)


# ---------- Meals ----------

@app.post("/users/{user_id}/meals", response_model=schemas.MealOut)
def add_meal(user_id: int, meal: schemas.MealCreate, db: Session = Depends(get_db)):
    if crud.get_user(db, user_id) is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return crud.create_meal(db, user_id, meal)


@app.get("/users/{user_id}/meals", response_model=list[schemas.MealOut])
def list_meals(user_id: int, giorno: date | None = None, db: Session = Depends(get_db)):
    giorno = giorno or date.today()
    return crud.list_meals_for_day(db, user_id, giorno)


# ---------- Bilancio calorico ----------

@app.get("/users/{user_id}/balance", response_model=schemas.DailyBalance)
def daily_balance(user_id: int, giorno: date | None = None, db: Session = Depends(get_db)):
    """
    Endpoint di supporto/debug: restituisce il bilancio calorico del giorno.
    L'agente, quando gira nello stesso processo, usa direttamente crud.py
    invece di chiamare questo endpoint via HTTP (vedi agent_core.py).
    """
    giorno = giorno or date.today()
    try:
        return crud.get_daily_balance(db, user_id, giorno)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------- Webhook Telegram ----------

def send_telegram_message(chat_id: int, text: str) -> None:
    """
    Invia un messaggio Telegram via chiamata HTTP diretta all'API Bot
    (niente libreria python-telegram-bot: qui non serve, ci basta un POST).
    Se il testo supera il limite di Telegram, lo spezza in più messaggi.
    """
    for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_LENGTH):
        pezzo = text[i : i + TELEGRAM_MAX_MESSAGE_LENGTH]
        requests.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": pezzo},
            timeout=15,
        )


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Riceve gli aggiornamenti che Telegram invia dopo aver configurato il
    webhook (vedi istruzioni nel docstring del modulo). Sostituisce il bot
    "in polling" usato in locale: qui è Telegram a contattarci via HTTP,
    invece che il contrario, il che permette di girare come normale
    servizio web (compatibile col piano gratuito di Render).
    """
    update = await request.json()
    message = update.get("message")

    if not message or "text" not in message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message["text"].strip()

    user = crud.get_user_by_telegram_chat_id(db, str(chat_id))

    if text == "/start":
        if user is not None:
            send_telegram_message(
                chat_id, f"Bentornato! Sei già collegato all'utente #{user.id}."
            )
        else:
            nome = message.get("from", {}).get("first_name", "Utente")
            nuovo_utente = crud.create_user_with_telegram(db, str(chat_id), nome)
            send_telegram_message(
                chat_id,
                f"Ciao {nome}! Ti ho registrato come utente #{nuovo_utente.id} "
                f"con obiettivo di {nuovo_utente.obiettivo_calorico_giornaliero} kcal/giorno.\n\n"
                "Da ora puoi chiedermi cose come 'quante calorie mi restano oggi?' "
                "oppure dirmi cosa hai mangiato.",
            )
        return {"ok": True}

    if user is None:
        send_telegram_message(chat_id, "Prima di iniziare, scrivi /start per registrarti.")
        return {"ok": True}

    system_prompt = agent_core.build_system_prompt(user.id)
    history = conversations.setdefault(chat_id, [])
    history.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))

    try:
        updated_history, reply = await asyncio.to_thread(
            agent_core.run_turn, history, system_prompt
        )
        conversations[chat_id] = updated_history
    except Exception as e:
        reply = f"Si è verificato un errore parlando con l'agente: {e}"

    send_telegram_message(chat_id, reply)
    return {"ok": True}
