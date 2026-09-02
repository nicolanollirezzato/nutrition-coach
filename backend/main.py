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

import os
from datetime import date

import requests
from fastapi import BackgroundTasks, FastAPI, Depends, HTTPException, Request
from google.genai import types
from sqlalchemy.orm import Session

import models
import schemas
import crud
import agent_core
from database import engine, get_db, SessionLocal

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

# Numero massimo di elementi (messaggi + chiamate/risultati tool) tenuti in
# memoria per ogni chat. Senza un limite, una conversazione lunga rimanderebbe
# per intero l'intera storia ad ogni singolo messaggio successivo, con un
# costo in token che cresce senza controllo. Tagliare i più vecchi non tocca
# in alcun modo i dati salvati nel database (pasti, peso, piano) — solo il
# "filo del discorso" più lontano nel tempo.
MAX_HISTORY_ITEMS = 10


def _tronca_cronologia(history: list) -> list:
    if len(history) > MAX_HISTORY_ITEMS:
        return history[-MAX_HISTORY_ITEMS:]
    return history


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
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Riceve gli aggiornamenti che Telegram invia dopo aver configurato il
    webhook (vedi istruzioni nel docstring del modulo). Risponde SUBITO a
    Telegram (entro pochi millisecondi) e processa il messaggio vero e
    proprio in un task in background: comporre un piano settimanale può
    richiedere decine di secondi (molte chiamate a ricette/USDA), e se
    Telegram non riceve una risposta abbastanza in fretta RITENTA l'invio
    dello stesso messaggio, causando elaborazioni doppie e risposte
    duplicate. Rispondere subito elimina questo problema alla radice.
    """
    update = await request.json()
    message = update.get("message")

    if not message or "text" not in message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message["text"].strip()
    from_info = message.get("from", {})

    background_tasks.add_task(_processa_messaggio_telegram, chat_id, text, from_info)
    return {"ok": True}


def _processa_messaggio_telegram(chat_id: int, text: str, from_info: dict) -> None:
    """
    Logica vera del webhook, eseguita in background (fuori dal ciclo di
    richiesta/risposta HTTP con Telegram). Apre una propria sessione DB
    perché quella iniettata da FastAPI nella richiesta originale non è
    più valida una volta che la risposta HTTP è già stata inviata.
    """
    db = SessionLocal()
    try:
        user = crud.get_user_by_telegram_chat_id(db, str(chat_id))

        if text == "/start":
            if user is not None:
                send_telegram_message(
                    chat_id, f"Bentornato! Sei già collegato all'utente #{user.id}."
                )
            else:
                nome = from_info.get("first_name", "Utente")
                nuovo_utente = crud.create_user_with_telegram(db, str(chat_id), nome)
                send_telegram_message(
                    chat_id,
                    f"Ciao {nome}! Ti ho registrato come utente #{nuovo_utente.id} "
                    f"con obiettivo di {nuovo_utente.obiettivo_calorico_giornaliero} kcal/giorno.\n\n"
                    "Da ora puoi chiedermi cose come 'quante calorie mi restano oggi?' "
                    "oppure dirmi cosa hai mangiato.",
                )
            return

        if user is None:
            send_telegram_message(chat_id, "Prima di iniziare, scrivi /start per registrarti.")
            return

        system_prompt = agent_core.build_system_prompt(user.id)
        history = _tronca_cronologia(conversations.setdefault(chat_id, []))
        history.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))

        try:
            updated_history, reply = agent_core.run_turn(history, system_prompt, text)
            conversations[chat_id] = _tronca_cronologia(updated_history)
        except Exception as e:
            reply = f"Si è verificato un errore parlando con l'agente: {e}"

        send_telegram_message(chat_id, reply)

    except Exception as e:
        # Rete di sicurezza: qualsiasi errore imprevisto (es. connessione al
        # database) non deve far sparire il messaggio nel nulla — l'utente
        # riceve comunque un avviso invece di un silenzio senza spiegazioni.
        try:
            send_telegram_message(
                chat_id, f"Si è verificato un errore imprevisto: {e}. Riprova tra poco."
            )
        except Exception:
            pass
    finally:
        db.close()
