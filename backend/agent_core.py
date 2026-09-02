"""
Logica dell'agente nutrizionale, adattata per girare nello stesso processo
del backend FastAPI (deployment su Render). A differenza della versione
originale usata in locale (agent/core.py), qui i tool non fanno chiamate
HTTP al backend: chiamano direttamente le funzioni di crud.py, perché
backend e agente vivono nello stesso servizio.
"""

import json
import os
import time
from datetime import date, timedelta

import requests
from google import genai
from google.genai import types

import crud
import models
import schemas
from database import SessionLocal

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# Numero massimo di tentativi e attesa (in secondi, raddoppia ad ogni tentativo)
# quando Gemini risponde con un errore temporaneo (es. 503 "modello sovraccarico").
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_BASE_DELAY = 1

# Riserva automatica: se Gemini continua a fallire, si passa a Groq (gratuito,
# API compatibile OpenAI). Se GROQ_API_KEY non è impostata, la riserva è
# semplicemente disattivata e l'errore di Gemini viene mostrato come prima.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Chiave gratuita del database nutrizionale USDA FoodData Central.
# DEMO_KEY funziona subito ma con limiti molto bassi (30 richieste/ora);
# per uso reale registra una chiave gratuita su https://fdc.nal.usda.gov/api-key-signup.html
USDA_API_KEY = os.getenv("USDA_API_KEY", "DEMO_KEY")
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

client = genai.Client()  # legge GEMINI_API_KEY dall'ambiente


# ---------- Definizione dei tool esposti al modello ----------

GET_DAILY_BALANCE = {
    "name": "get_daily_balance",
    "description": (
        "Restituisce il bilancio calorico di un utente per una data specifica: "
        "obiettivo calorico, calorie bruciate con l'attività, calorie assunte dai "
        "pasti registrati e calorie residue. Usa questo tool ogni volta che l'utente "
        "chiede quante calorie gli restano, quante ne ha consumate o bruciate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "ID dell'utente"},
            "giorno": {
                "type": "string",
                "description": "Data in formato YYYY-MM-DD. Se omessa, si intende oggi.",
            },
        },
        "required": ["user_id"],
    },
}

SEARCH_FOOD_NUTRITION = {
    "name": "search_food_nutrition",
    "description": (
        "Cerca un alimento nel database nutrizionale USDA FoodData Central e "
        "restituisce i valori nutrizionali per 100 grammi (calorie, proteine, "
        "carboidrati, grassi). USA SEMPRE questo tool prima di registrare un "
        "pasto, per calcolare le calorie in modo preciso invece di stimarle a "
        "occhio. Il database è in inglese: se l'alimento è descritto in "
        "italiano, traducilo in inglese prima di cercarlo (es. 'petto di "
        "pollo' -> 'chicken breast', 'riso cotto' -> 'white rice cooked')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Nome dell'alimento in inglese, es. 'chicken breast raw'",
            },
        },
        "required": ["query"],
    },
}

LOG_MEAL = {
    "name": "log_meal",
    "description": (
        "Registra un pasto per l'utente con calorie e, quando disponibili, "
        "i macronutrienti. Usa questo tool quando l'utente descrive qualcosa "
        "che ha mangiato o bevuto, dopo aver calcolato le calorie con "
        "search_food_nutrition quando possibile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "nome_alimento": {
                "type": "string",
                "description": "Descrizione breve del pasto/alimento, es. 'pizza margherita'",
            },
            "calorie": {
                "type": "number",
                "description": "Calorie totali del pasto in kcal",
            },
            "proteine_g": {
                "type": "number",
                "description": "Grammi di proteine totali del pasto, se noti",
            },
            "carboidrati_g": {
                "type": "number",
                "description": "Grammi di carboidrati totali del pasto, se noti",
            },
            "grassi_g": {
                "type": "number",
                "description": "Grammi di grassi totali del pasto, se noti",
            },
        },
        "required": ["user_id", "nome_alimento", "calorie"],
    },
}

LOG_ACTIVITY = {
    "name": "log_activity",
    "description": (
        "Registra o aggiorna manualmente l'attività fisica di oggi per l'utente "
        "(passi, calorie bruciate, minuti di allenamento). Da usare solo se l'utente "
        "fornisce questi dati a voce, perché normalmente arriveranno in automatico "
        "dallo smartwatch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "calorie_attive_bruciate": {"type": "number"},
            "passi": {"type": "integer"},
            "minuti_allenamento": {"type": "integer"},
        },
        "required": ["user_id"],
    },
}

LOG_WEIGHT = {
    "name": "log_weight",
    "description": (
        "Registra il peso corporeo dell'utente per una data (oggi se non "
        "specificata). Usa questo tool ogni volta che l'utente ti comunica "
        "il suo peso attuale."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "peso_kg": {"type": "number", "description": "Peso in kg"},
            "data": {
                "type": "string",
                "description": "Data in formato YYYY-MM-DD. Se omessa, si intende oggi.",
            },
        },
        "required": ["user_id", "peso_kg"],
    },
}

GET_WEIGHT_HISTORY = {
    "name": "get_weight_history",
    "description": (
        "Restituisce lo storico del peso dell'utente negli ultimi N giorni "
        "(default 90). USA SEMPRE questo tool prima di definire o correggere "
        "un piano alimentare, per valutare l'andamento reale del percorso."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "giorni": {
                "type": "integer",
                "description": "Quanti giorni indietro guardare (default 90)",
            },
        },
        "required": ["user_id"],
    },
}

GET_MEAL_PLAN = {
    "name": "get_meal_plan",
    "description": (
        "Restituisce il piano alimentare attivo dell'utente (calorie e "
        "macronutrienti target), se esiste. Usa questo tool per controllare "
        "se l'utente ha già un piano prima di proporne uno nuovo."
    ),
    "parameters": {
        "type": "object",
        "properties": {"user_id": {"type": "integer"}},
        "required": ["user_id"],
    },
}

SET_MEAL_PLAN = {
    "name": "set_meal_plan",
    "description": (
        "Crea o aggiorna il piano alimentare attivo dell'utente: obiettivo "
        "calorico giornaliero, target di macronutrienti e, opzionalmente, "
        "un piano pasti concreto (colazione/pranzo/cena/spuntini). Usa "
        "questo tool quando definisci un piano per la prima volta, quando "
        "lo correggi in base ai progressi, o quando l'utente chiede di "
        "rivedere/cambiare i pasti suggeriti. Includi sempre calorie_target "
        "anche se stai modificando solo i pasti suggeriti (rileggi il "
        "valore attuale con get_meal_plan se non lo stai cambiando). Non "
        "proporre MAI un target sotto le 1200 kcal/giorno, a meno che "
        "l'utente non abbia esplicitamente detto di essere seguito da un "
        "medico o nutrizionista per un piano più aggressivo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "calorie_target": {
                "type": "integer",
                "description": "Obiettivo calorico giornaliero in kcal",
            },
            "obiettivo": {
                "type": "string",
                "description": "Descrizione dell'obiettivo, es. 'perdita 0.5 kg/settimana'",
            },
            "proteine_target_g": {"type": "number"},
            "carboidrati_target_g": {"type": "number"},
            "grassi_target_g": {"type": "number"},
            "pasti_suggeriti": {
                "type": "string",
                "description": (
                    "Piano pasti concreto in testo strutturato, es. "
                    "'Colazione: 40g avena + 200ml latte + 1 banana (~350 kcal)\\n"
                    "Pranzo: 150g petto di pollo + 80g riso + verdure (~550 kcal)\\n"
                    "Cena: ...\\nSpuntini: ...'. Componi porzioni realistiche "
                    "che sommate si avvicinino a calorie_target."
                ),
            },
            "note": {
                "type": "string",
                "description": "Eventuali note sul piano o sul perché è stato corretto",
            },
        },
        "required": ["user_id", "calorie_target"],
    },
}

RAW_TOOL_DEFINITIONS = [
    GET_DAILY_BALANCE,
    SEARCH_FOOD_NUTRITION,
    LOG_MEAL,
    LOG_ACTIVITY,
    LOG_WEIGHT,
    GET_WEIGHT_HISTORY,
    GET_MEAL_PLAN,
    SET_MEAL_PLAN,
]

# Formato per Gemini (google-genai)
TOOLS = [types.Tool(function_declarations=RAW_TOOL_DEFINITIONS)]

# Formato per Groq (compatibile OpenAI): stessa descrizione, wrapper diverso
GROQ_TOOLS = [
    {"type": "function", "function": tool_def} for tool_def in RAW_TOOL_DEFINITIONS
]


def build_system_prompt(user_id: int) -> str:
    """
    Il system prompt include già lo user_id della conversazione, così l'utente
    non deve mai ripeterlo e il modello sa sempre per chi sta chiamando i tool.
    """
    return (
        "Sei un coach nutrizionale amichevole e diretto. Rispondi sempre in italiano, "
        "in modo breve e concreto. Usa i tool a disposizione per leggere o aggiornare "
        "i dati reali dell'utente: non inventare mai numeri sulle calorie residue o "
        "bruciate, quelli devono sempre venire da get_daily_balance.\n\n"
        "IMPORTANTE: se il risultato di un tool contiene una chiave 'errore', NON "
        "interpretarlo come 'nessun dato trovato' — riporta all'utente il messaggio "
        "di errore esatto che hai ricevuto, così può essere risolto. Un errore "
        "tecnico e 'non esiste ancora nulla' sono due cose diverse.\n\n"
        "DIAGNOSTICA TEMPORANEA: se get_meal_plan restituisce piano=None, "
        "includi SEMPRE nella tua risposta, testualmente, i valori delle chiavi "
        "debug_user_id_interrogato e debug_totale_piani_nel_db così come li hai "
        "ricevuti dal tool — è temporaneo per risolvere un problema tecnico, "
        "l'utente lo sa e li sta aspettando.\n\n"
        "Quando l'utente descrive un pasto:\n"
        "1. Identifica gli alimenti principali e le quantità (se l'utente non dà "
        "le quantità, assumi porzioni standard ragionevoli e dillo chiaramente).\n"
        "2. Per ogni alimento, chiama search_food_nutrition (in inglese) per "
        "ottenere i valori nutrizionali precisi per 100g.\n"
        "3. Calcola le calorie e i macronutrienti totali in base alla quantità "
        "reale (es. 150g di petto di pollo = 1.5 volte il valore per 100g).\n"
        "4. Se un alimento non si trova nel database USDA, fai una stima "
        "approssimativa ragionevole e dillo esplicitamente all'utente, "
        "specificando che non è un dato preciso da database.\n"
        "5. Registra il pasto con log_meal, includendo calorie e macronutrienti "
        "quando li hai calcolati.\n"
        "6. Nella risposta finale, spiega brevemente come hai calcolato le "
        "calorie così l'utente capisce se è un dato preciso o una stima.\n\n"
        "PIANO ALIMENTARE — definizione e correzione:\n"
        "Quando l'utente chiede un piano alimentare per la prima volta, prima "
        "di proporlo raccogli (con una o due domande, non un interrogatorio): "
        "peso attuale, altezza, età, sesso, livello di attività fisica "
        "abituale, e l'obiettivo (quanto vuole perdere/guadagnare e in quanto "
        "tempo, se lo sa). Poi:\n"
        "1. Calcola un obiettivo calorico giornaliero ragionevole (per la "
        "perdita di peso, un deficit moderato è più sostenibile di uno "
        "aggressivo: orientativamente 300-500 kcal/giorno sotto il "
        "mantenimento, mai sotto le 1200 kcal/giorno).\n"
        "2. Suggerisci target di macronutrienti equilibrati (proteine "
        "adeguate a preservare la massa magra, orientativamente 1.6-2g per "
        "kg di peso corporeo, il resto diviso tra carboidrati e grassi).\n"
        "3. Salva il piano con set_meal_plan.\n"
        "4. Se l'utente chiede anche pasti concreti (colazione/pranzo/cena/"
        "spuntini) e non solo i numeri target, componi un piano pasti "
        "realistico che sommato si avvicini a calorie_target: usa "
        "search_food_nutrition per alimenti reali con quantità precise, poi "
        "salvalo nel campo pasti_suggeriti di set_meal_plan (testo "
        "strutturato per pasto, con calorie indicative per ciascuno).\n"
        "5. Ricorda sempre che non sei un medico o un nutrizionista: per "
        "condizioni mediche, gravidanza, disturbi alimentari o esigenze "
        "particolari, l'utente deve rivolgersi a un professionista — dillo "
        "esplicitamente quando proponi un piano per la prima volta.\n\n"
        "Quando l'utente chiede di vedere il piano pasti attuale, chiama "
        "get_meal_plan e presenta pasti_suggeriti in modo leggibile (se "
        "presente); se non esiste ancora un piano pasti concreto, offriti "
        "di crearne uno.\n\n"
        "CONSULTAZIONE RAPIDA (es. 'cosa mangiamo stasera?', 'cosa c'è a "
        "pranzo oggi?'):\n"
        "Chiama get_meal_plan e rispondi SOLO con la parte pertinente di "
        "pasti_suggeriti (es. solo la cena se chiede 'stasera', solo il "
        "pranzo se chiede 'a pranzo'), senza rigenerare o riscrivere tutto "
        "il piano. 'Stasera'/'a cena' = cena, 'oggi a pranzo'/'a mezzogiorno' "
        "= pranzo, 'colazione'/'al mattino' = colazione. Se non esiste "
        "ancora un piano pasti concreto, dillo e offriti di crearne uno.\n\n"
        "LISTA DELLA SPESA:\n"
        "Quando l'utente chiede la lista della spesa (es. 'fammi la lista "
        "della spesa', 'cosa devo comprare per la settimana'):\n"
        "1. Chiama get_meal_plan per leggere pasti_suggeriti.\n"
        "2. Se non esiste un piano pasti concreto, dillo e offriti prima di "
        "crearne uno (senza quello non puoi generare una lista sensata).\n"
        "3. Assumi che il piano si ripeta per tutti i giorni della "
        "settimana (7 giorni), a meno che l'utente non specifichi "
        "diversamente (es. 'solo per 3 giorni').\n"
        "4. Moltiplica le quantità di ogni ingrediente per il numero di "
        "giorni, poi consolida gli ingredienti uguali o molto simili "
        "sommando le quantità (es. se compare pollo sia a pranzo che a "
        "cena, sommali in una sola voce).\n"
        "5. Presenta la lista organizzata per categoria (es. Proteine, "
        "Carboidrati/cereali, Frutta e verdura, Latticini, Dispensa/altro), "
        "con quantità totali arrotondate in modo pratico per la spesa (es. "
        "'circa 1.2 kg' invece di '1173g').\n"
        "6. Ricorda che è una stima basata sul piano: l'utente può avere "
        "già alcuni ingredienti in casa, quindi la lista va adattata.\n\n"
        "Quando l'utente chiede di modificare/variare i pasti suggeriti "
        "(es. 'non mi piace il pesce', 'cambia la cena'), chiama prima "
        "get_meal_plan per vedere il piano attuale, poi chiama set_meal_plan "
        "passando lo stesso calorie_target (a meno che non ci sia un motivo "
        "per cambiarlo) e il nuovo pasti_suggeriti aggiornato — non "
        "riscrivere tutto da zero se basta modificare un pasto.\n\n"
        "Quando l'utente chiede di correggere/rivedere il piano, o quando ti "
        "sembra naturale farlo dopo un aggiornamento di peso:\n"
        "1. Chiama get_weight_history e get_meal_plan per vedere il piano "
        "attuale e l'andamento reale del peso nel tempo.\n"
        "2. Se il peso non si muove nella direzione dell'obiettivo dopo "
        "alcune settimane di dati, proponi una correzione moderata (es. "
        "-100/-150 kcal), spiegando il motivo. Se il peso scende più "
        "velocemente del previsto o l'utente riporta fame eccessiva/stanchezza, "
        "proponi di alzare leggermente le calorie invece di scendere ancora.\n"
        "3. Aggiorna il piano con set_meal_plan solo dopo aver spiegato la "
        "modifica e il perché, non silenziosamente.\n"
        "4. Non correggere il piano sulla base di un singolo giorno: servono "
        "più giorni/settimane di dati per un trend affidabile.\n\n"
        f"L'utente con cui stai parlando ha user_id={user_id}: usalo sempre nei tool, "
        "anche se l'utente non lo specifica."
    )


def search_food_nutrition(query: str) -> list[dict]:
    """Cerca un alimento su USDA FoodData Central, valori nutrizionali per 100g."""
    params = {
        "api_key": USDA_API_KEY,
        "query": query,
        "pageSize": 5,
        "dataType": "Foundation,SR Legacy",
    }
    resp = requests.get(USDA_SEARCH_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    risultati = []
    for food in data.get("foods", [])[:3]:
        nutrienti = {
            n.get("nutrientName"): n.get("value") for n in food.get("foodNutrients", [])
        }
        risultati.append(
            {
                "descrizione": food.get("description"),
                "calorie_per_100g": nutrienti.get("Energy"),
                "proteine_per_100g": nutrienti.get("Protein"),
                "carboidrati_per_100g": nutrienti.get("Carbohydrate, by difference"),
                "grassi_per_100g": nutrienti.get("Total lipid (fat)"),
            }
        )
    return risultati


def execute_tool(name: str, tool_input: dict) -> dict:
    """
    Esegue un tool. get_daily_balance / log_meal / log_activity aprono una
    sessione DB dedicata e la chiudono subito dopo, per restare semplici e
    sicuri anche con più richieste concorrenti (Render può servire più utenti).
    """
    # Difesa contro provider diversi (Gemini vs Groq) che a volte restituiscono
    # user_id come stringa o float invece che intero: forziamo sempre il tipo
    # corretto qui, in un solo posto, invece che in ogni singolo ramo sotto.
    if "user_id" in tool_input:
        try:
            tool_input["user_id"] = int(tool_input["user_id"])
        except (TypeError, ValueError):
            return {"errore": f"user_id non valido: {tool_input.get('user_id')!r}"}

    if name == "search_food_nutrition":
        try:
            risultati = search_food_nutrition(tool_input["query"])
        except requests.HTTPError as e:
            return {
                "errore": f"USDA API non raggiungibile o limite richieste superato: {e}"
            }
        if not risultati:
            return {"risultati": [], "nota": "Nessun alimento trovato nel database USDA"}
        return {"risultati": risultati}

    if name == "get_daily_balance":
        db = SessionLocal()
        try:
            giorno = (
                date.fromisoformat(tool_input["giorno"])
                if tool_input.get("giorno")
                else date.today()
            )
            balance = crud.get_daily_balance(db, tool_input["user_id"], giorno)
            return {
                "user_id": balance.user_id,
                "data": balance.data.isoformat(),
                "obiettivo_calorico_giornaliero": balance.obiettivo_calorico_giornaliero,
                "calorie_attive_bruciate": balance.calorie_attive_bruciate,
                "calorie_assunte": balance.calorie_assunte,
                "calorie_residue": balance.calorie_residue,
                "numero_pasti_registrati": balance.numero_pasti_registrati,
            }
        except ValueError as e:
            return {"errore": str(e)}
        finally:
            db.close()

    if name == "log_meal":
        db = SessionLocal()
        try:
            meal_in = schemas.MealCreate(
                nome_alimento=tool_input["nome_alimento"],
                calorie=tool_input["calorie"],
                proteine_g=tool_input.get("proteine_g"),
                carboidrati_g=tool_input.get("carboidrati_g"),
                grassi_g=tool_input.get("grassi_g"),
                fonte="agente",
            )
            meal = crud.create_meal(db, tool_input["user_id"], meal_in)
            return {
                "id": meal.id,
                "nome_alimento": meal.nome_alimento,
                "calorie": meal.calorie,
                "data": meal.data.isoformat(),
            }
        finally:
            db.close()

    if name == "log_activity":
        db = SessionLocal()
        try:
            activity_in = schemas.ActivityUpsert(
                data=date.today(),
                calorie_attive_bruciate=tool_input.get("calorie_attive_bruciate", 0),
                passi=tool_input.get("passi", 0),
                minuti_allenamento=tool_input.get("minuti_allenamento", 0),
                fonte="manuale",
            )
            activity = crud.upsert_daily_activity(db, tool_input["user_id"], activity_in)
            return {
                "data": activity.data.isoformat(),
                "calorie_attive_bruciate": activity.calorie_attive_bruciate,
                "passi": activity.passi,
                "minuti_allenamento": activity.minuti_allenamento,
            }
        finally:
            db.close()

    if name == "log_weight":
        db = SessionLocal()
        try:
            weight_in = schemas.WeightUpsert(
                peso_kg=tool_input["peso_kg"],
                data=date.fromisoformat(tool_input["data"]) if tool_input.get("data") else None,
            )
            entry = crud.upsert_weight_entry(db, tool_input["user_id"], weight_in)
            return {"data": entry.data.isoformat(), "peso_kg": entry.peso_kg}
        finally:
            db.close()

    if name == "get_weight_history":
        db = SessionLocal()
        try:
            giorni = tool_input.get("giorni", 90)
            since = date.today() - timedelta(days=giorni)
            entries = crud.list_weight_entries(db, tool_input["user_id"], since)
            if not entries:
                return {"storico": [], "nota": "Nessuna pesata registrata in questo periodo"}
            return {
                "storico": [
                    {"data": e.data.isoformat(), "peso_kg": e.peso_kg} for e in entries
                ]
            }
        finally:
            db.close()

    if name == "get_meal_plan":
        db = SessionLocal()
        try:
            plan = crud.get_meal_plan(db, tool_input["user_id"])
            if plan is None:
                # Info diagnostica temporanea: fa capire se il problema è il
                # valore di user_id usato nella query o l'assenza reale del dato.
                totale_piani = db.query(models.MealPlan).count()
                return {
                    "piano": None,
                    "debug_user_id_interrogato": tool_input["user_id"],
                    "debug_totale_piani_nel_db": totale_piani,
                }
            return {
                "piano": {
                    "obiettivo": plan.obiettivo,
                    "calorie_target": plan.calorie_target,
                    "proteine_target_g": plan.proteine_target_g,
                    "carboidrati_target_g": plan.carboidrati_target_g,
                    "grassi_target_g": plan.grassi_target_g,
                    "pasti_suggeriti": plan.pasti_suggeriti,
                    "note": plan.note,
                    "aggiornato_at": plan.aggiornato_at.isoformat(),
                }
            }
        finally:
            db.close()

    if name == "set_meal_plan":
        db = SessionLocal()
        try:
            plan_in = schemas.MealPlanUpsert(
                calorie_target=tool_input["calorie_target"],
                obiettivo=tool_input.get("obiettivo"),
                proteine_target_g=tool_input.get("proteine_target_g"),
                carboidrati_target_g=tool_input.get("carboidrati_target_g"),
                grassi_target_g=tool_input.get("grassi_target_g"),
                pasti_suggeriti=tool_input.get("pasti_suggeriti"),
                note=tool_input.get("note"),
            )
            plan = crud.upsert_meal_plan(db, tool_input["user_id"], plan_in)
            return {
                "obiettivo": plan.obiettivo,
                "calorie_target": plan.calorie_target,
                "proteine_target_g": plan.proteine_target_g,
                "carboidrati_target_g": plan.carboidrati_target_g,
                "grassi_target_g": plan.grassi_target_g,
                "pasti_suggeriti": plan.pasti_suggeriti,
            }
        finally:
            db.close()

    raise ValueError(f"Tool sconosciuto: {name}")


def _is_transient_error(exc: Exception) -> bool:
    """
    Riconosce errori temporanei di Gemini (es. 503 "modello sovraccarico",
    429 "troppe richieste") per cui ha senso riprovare automaticamente,
    invece di errori permanenti (es. chiave API sbagliata, richiesta malformata).
    """
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if status_code in (503, 429):
        return True
    testo = str(exc).upper()
    return "UNAVAILABLE" in testo or "OVERLOADED" in testo or "RESOURCE_EXHAUSTED" in testo


def _generate_with_retry(contents: list, config: types.GenerateContentConfig):
    """
    Chiama Gemini riprovando automaticamente sugli errori temporanei, con
    attesa crescente (1s, 2s, 4s) prima di ogni nuovo tentativo. Se dopo
    tutti i tentativi l'errore persiste, lo rilancia così l'utente riceve
    comunque un messaggio chiaro invece di un blocco silenzioso.
    """
    ultimo_errore = None
    for tentativo in range(GEMINI_MAX_RETRIES):
        try:
            return client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )
        except Exception as e:
            ultimo_errore = e
            if not _is_transient_error(e) or tentativo == GEMINI_MAX_RETRIES - 1:
                raise
            time.sleep(GEMINI_RETRY_BASE_DELAY * (2 ** tentativo))
    raise ultimo_errore


GROQ_MAX_RETRIES = 3
GROQ_RETRY_BASE_DELAY = 1


def _groq_post_with_retry(payload: dict) -> dict:
    """
    Chiama l'API di Groq riprovando automaticamente su errori temporanei
    (503, 429 "troppe richieste"), con la stessa logica di attesa crescente
    usata per Gemini.
    """
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    ultimo_errore = None
    for tentativo in range(GROQ_MAX_RETRIES):
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
        if resp.status_code not in (429, 503):
            resp.raise_for_status()
            return resp.json()
        ultimo_errore = requests.HTTPError(
            f"{resp.status_code} Client Error: {resp.reason} for url: {resp.url}", response=resp
        )
        if tentativo == GROQ_MAX_RETRIES - 1:
            raise ultimo_errore
        time.sleep(GROQ_RETRY_BASE_DELAY * (2 ** tentativo))
    raise ultimo_errore


def run_turn_groq(user_text: str, system_prompt: str) -> str:
    """
    Riserva usata quando Gemini continua a fallire dopo i tentativi. Gestisce
    un turno "singolo" (senza la cronologia completa di Gemini, per restare
    semplice): il modello vede comunque tutti i dati reali dell'utente
    tramite gli stessi tool, quindi la risposta resta accurata anche se la
    conversazione "sente" un piccolo salto di contesto in quel turno.
    """
    if not GROQ_API_KEY:
        raise RuntimeError("Nessuna riserva disponibile: GROQ_API_KEY non impostata")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    for _ in range(6):  # limite di sicurezza sui giri di tool use
        data = _groq_post_with_retry(
            {
                "model": GROQ_MODEL,
                "messages": messages,
                "tools": GROQ_TOOLS,
                "tool_choice": "auto",
            }
        )
        message = data["choices"][0]["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return message.get("content") or ""

        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"] or "{}")
            try:
                result = execute_tool(name, args)
            except Exception as e:
                result = {"errore": str(e)}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    return "Non sono riuscito a completare la richiesta, riprova tra poco."


def run_turn(contents: list, system_prompt: str, user_text: str | None = None) -> tuple[list, str]:
    """
    Gestisce un turno di conversazione, incluso l'eventuale ciclo di function
    calling. `contents` è la cronologia nel formato del SDK google-genai.
    Ritorna (contents_aggiornati, testo_risposta_finale).

    Se Gemini continua a fallire dopo i tentativi automatici e `user_text` è
    disponibile, passa alla riserva Groq per quel turno.
    """
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=TOOLS,
    )

    while True:
        try:
            response = _generate_with_retry(contents, config)
        except Exception as errore_gemini:
            if user_text is not None and GROQ_API_KEY:
                try:
                    reply = run_turn_groq(user_text, system_prompt)
                except Exception as errore_groq:
                    raise RuntimeError(
                        "i sistemi AI (Gemini e la riserva Groq) sono entrambi "
                        "momentaneamente sovraccarichi. Riprova tra qualche minuto."
                    ) from errore_groq
                contents.append(
                    types.Content(role="model", parts=[types.Part.from_text(text=reply)])
                )
                return contents, reply
            raise

        candidate = response.candidates[0]
        contents.append(candidate.content)

        function_calls = [
            part.function_call
            for part in candidate.content.parts
            if part.function_call is not None
        ]

        if not function_calls:
            final_text = "".join(
                part.text for part in candidate.content.parts if part.text
            )
            return contents, final_text

        response_parts = []
        for call in function_calls:
            try:
                result = execute_tool(call.name, dict(call.args))
            except Exception as e:
                result = {"errore": str(e)}

            response_parts.append(
                types.Part.from_function_response(name=call.name, response=result)
            )

        contents.append(types.Content(role="user", parts=response_parts))
