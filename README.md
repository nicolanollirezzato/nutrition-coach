# Nutrition Coach — deploy gratuito 24/7 (Render + Neon)

Questa versione unisce backend e bot Telegram in un **unico servizio web**
(dentro la cartella `backend/`), pensato per girare gratis e senza bisogno
di tenere il tuo computer acceso:

- **Render** ospita il servizio (Python/FastAPI) gratuitamente. Il piano
  gratuito "addormenta" il servizio dopo ~15 minuti di inattività: la prima
  richiesta dopo una pausa impiega 30-60 secondi in più per svegliarsi, poi
  torna veloce.
- **Neon** ospita il database Postgres gratuitamente, per sempre, senza
  scadenze — necessario perché lo spazio disco di Render si azzera ad ogni
  riavvio del servizio, quindi non possiamo più usare un file SQLite locale.
- Il bot Telegram non "interroga" più Telegram in continuazione (polling):
  è Telegram stesso a inviarci un messaggio via internet ogni volta che
  qualcuno scrive (webhook) — questo è ciò che permette al bot di girare
  come normale servizio web, compatibile col piano gratuito.

Le cartelle `agent/` e `bot/` (usate per testare tutto in locale) restano
nel progetto come riferimento, ma **non servono più**: da qui in poi tutto
vive dentro `backend/`.

---

## Cosa ti serve prima di iniziare

1. Un account **GitHub** (gratuito) — serve a Render per leggere il codice.
2. Un account **Neon** (gratuito) — per il database.
3. Un account **Render** (gratuito) — per far girare il servizio.
4. Le chiavi che hai già: token Telegram, chiave Gemini, chiave USDA (o `DEMO_KEY`).

---

## 1. Crea il database su Neon

1. Vai su [neon.tech](https://neon.tech) e registrati (puoi usare l'account Google).
2. Crea un nuovo progetto (basta un nome, es. "nutrition-coach").
3. Nella pagina del progetto, cerca la **connection string** — un testo tipo:
   ```
   postgresql://utente:password@ep-xxxxx.eu-central-1.aws.neon.tech/neondb?sslmode=require
   ```
4. Copiala e tienila da parte: sarà la tua `DATABASE_URL`.

---

## 2. Metti il codice su GitHub

Render legge il codice da un repository GitHub, quindi va caricato lì prima del deploy.

1. Vai su [github.com](https://github.com) e crea un account (se non l'hai già).
2. Crea un nuovo repository (pulsante verde "New"), dagli un nome es.
   `nutrition-coach`, tienilo **pubblico o privato** (indifferente), non
   aggiungere nessun file di esempio.
3. Il modo più semplice per caricare i file senza usare comandi da terminale
   è **GitHub Desktop** ([desktop.github.com](https://desktop.github.com)):
   installalo, accedi con lo stesso account, scegli "Add local repository",
   seleziona la cartella `nutrition-coach` sul tuo computer, poi clicca
   "Publish repository".

Alla fine, il codice deve essere visibile online sulla pagina del tuo
repository GitHub.

---

## 3. Crea il servizio su Render

1. Vai su [render.com](https://render.com) e registrati (puoi collegare
   direttamente l'account GitHub, così Render vede subito i tuoi repository).
2. Clicca "New" → "Web Service".
3. Seleziona il repository `nutrition-coach` appena creato.
4. Configura questi campi:
   - **Root Directory**: `backend`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
5. Nella sezione **Environment Variables**, aggiungi (vedi anche `backend/.env.example`):
   - `DATABASE_URL` → la connection string di Neon copiata al passo 1
   - `GEMINI_API_KEY` → la tua chiave Gemini
   - `USDA_API_KEY` → la tua chiave USDA (o lasciala vuota per usare `DEMO_KEY`)
   - `TELEGRAM_BOT_TOKEN` → il token del tuo bot da BotFather
6. Clicca "Create Web Service". Il primo deploy richiede qualche minuto:
   segui i log finché non vedi qualcosa tipo `Application startup complete`.
7. Una volta pronto, Render ti assegna un indirizzo pubblico tipo:
   ```
   https://nutrition-coach-xxxx.onrender.com
   ```
   Copialo: ti serve per l'ultimo passo.

---

## 4. Collega Telegram al servizio (un comando, una volta sola)

Apri questo indirizzo nel browser (sostituendo token e URL con i tuoi):

```
https://api.telegram.org/bot<IL_TUO_TOKEN>/setWebhook?url=https://nutrition-coach-xxxx.onrender.com/telegram/webhook
```

Se vedi una risposta con `"ok":true`, il collegamento è andato a buon fine.
Da questo momento, ogni messaggio inviato al bot arriva direttamente al tuo
servizio su Render — non serve più nessun terminale aperto sul tuo computer.

---

## 5. Testa il bot

1. Apri la chat col tuo bot su Telegram.
2. Scrivi `/start`.
3. Prova con qualcosa come *"quante calorie mi restano oggi?"* o *"ho mangiato 150g di pollo"*.

La prima richiesta dopo un periodo di inattività può metterci qualche secondo
in più a rispondere (il servizio si sta "svegliando") — è normale sul piano gratuito.

---

## Note e limiti

- **Cronologia della conversazione**: vive in memoria nel processo. Se
  Render riavvia il servizio (dopo lo sleep, o per un nuovo deploy), la
  cronologia si azzera — il bilancio calorico no, quello è nel database.
- **Aggiornare il codice in futuro**: basta modificare i file e ripubblicarli
  su GitHub (con GitHub Desktop: "Commit" poi "Push") — Render fa il
  redeploy automaticamente ad ogni push.
- **Limiti di Neon**: 0,5 GB di spazio, che per pasti/attività testuali dura
  moltissimo tempo prima di essere un problema reale.
- **Limiti di USDA con `DEMO_KEY`**: circa 30 richieste/ora — se usi il bot
  spesso, registra una chiave personale gratuita (è immediato, via email).

---

## Sviluppo locale (facoltativo)

Se in futuro vuoi testare modifiche prima di pubblicarle, puoi ancora far
girare tutto sul tuo computer con SQLite, esattamente come all'inizio:

```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # su Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Senza impostare `DATABASE_URL`, il backend userà automaticamente un file
SQLite locale (`nutrition_coach.db`) invece di Neon — comodo per provare
senza toccare il database di produzione. In locale, però, il webhook
Telegram non riceverà nulla (Telegram deve poter raggiungere un indirizzo
pubblico): per test locali con Telegram, usa ancora la versione con bot in
polling nelle cartelle `agent/` e `bot/`.
