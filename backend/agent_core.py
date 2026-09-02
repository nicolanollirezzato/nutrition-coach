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

import anthropic
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

# Prima riserva: Groq (gratuito, API compatibile OpenAI). Se GROQ_API_KEY non
# è impostata, questa riserva è semplicemente disattivata.
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Seconda riserva (a pagamento, ma molto economica): Claude Haiku, usata solo
# se anche Groq fallisce. Se ANTHROPIC_API_KEY non è impostata, questa
# riserva è semplicemente disattivata e il comportamento resta invariato.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MAX_RETRIES = 3
CLAUDE_RETRY_BASE_DELAY = 1
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# Chiave gratuita del database nutrizionale USDA FoodData Central.
# DEMO_KEY funziona subito ma con limiti molto bassi (30 richieste/ora);
# per uso reale registra una chiave gratuita su https://fdc.nal.usda.gov/api-key-signup.html
USDA_API_KEY = os.getenv("USDA_API_KEY") or "DEMO_KEY"
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

client = genai.Client()  # legge GEMINI_API_KEY dall'ambiente


# ---------- Definizione dei tool esposti al modello ----------

GET_DAILY_BALANCE = {
    "name": "get_daily_balance",
    "description": (
        "Restituisce il bilancio calorico E dei macronutrienti (proteine, "
        "carboidrati, grassi) di un utente per una data specifica: obiettivo, "
        "quanto consumato finora, quanto resta. I campi macro sono presenti "
        "solo se esiste un piano alimentare attivo con target impostati. Usa "
        "questo tool ogni volta che l'utente chiede quante calorie/proteine/"
        "carboidrati/grassi gli restano, quante ne ha consumate o bruciate, "
        "o quando devi valutare come riadattare i pasti rimanenti della "
        "giornata in base a cosa ha già mangiato."
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

LIST_MEALS = {
    "name": "list_meals",
    "description": (
        "Restituisce l'elenco dei pasti registrati per l'utente in una data "
        "(oggi se omessa), con id, nome, calorie, macronutrienti e orario di "
        "ciascuno. USA SEMPRE questo tool — invece di chiedere all'utente "
        "quale fosse l'ultimo pasto — quando l'utente chiede di ricalcolare, "
        "correggere o modificare un pasto già registrato. Il pasto più "
        "recente è quello con l'orario più tardo nell'elenco."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "giorno": {
                "type": "string",
                "description": "Data in formato YYYY-MM-DD. Se omessa, si intende oggi.",
            },
        },
        "required": ["user_id"],
    },
}

UPDATE_MEAL = {
    "name": "update_meal",
    "description": (
        "Aggiorna un pasto GIÀ registrato (nome, calorie e/o macronutrienti), "
        "usando il suo id ottenuto da list_meals. Usa questo tool — e non "
        "log_meal — quando devi correggere/ricalcolare un pasto esistente: "
        "creare un nuovo pasto con log_meal invece di aggiornare quello "
        "esistente lo conteggerebbe due volte nel bilancio calorico "
        "giornaliero. Passa solo i campi che vuoi effettivamente cambiare."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "meal_id": {
                "type": "integer",
                "description": "Id del pasto da correggere, ottenuto da list_meals",
            },
            "nome_alimento": {"type": "string"},
            "calorie": {"type": "number"},
            "proteine_g": {"type": "number"},
            "carboidrati_g": {"type": "number"},
            "grassi_g": {"type": "number"},
        },
        "required": ["meal_id"],
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
                    "Piano pasti SETTIMANALE completo, organizzato per giorno "
                    "della settimana con pasti diversi ogni giorno (non "
                    "ripetuti). Struttura attesa: una sezione per ciascun "
                    "giorno (Lunedì, Martedì, ... Domenica), e dentro ogni "
                    "giorno le sezioni Colazione/Spuntino/Pranzo/Spuntino/"
                    "Cena con alimenti e quantità precise. Ogni giorno deve "
                    "sommare a circa calorie_target. Esempio struttura:\\n"
                    "'LUNEDÌ\\nColazione: ... (~xxx kcal)\\nPranzo: ...\\n"
                    "Cena: ...\\n\\nMARTEDÌ\\nColazione: ...\\n...'"
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

SEARCH_RECIPES = {
    "name": "search_recipes",
    "description": (
        "Cerca ricette nella libreria salvata, filtrando per categoria "
        "(colazione/pranzo/cena/spuntino). Restituisce nome, id e valori "
        "nutrizionali per la porzione BASE di ciascuna. USA QUESTO quando "
        "componi o rivedi un piano pasti: preferisci prendere spunto da "
        "ricette reali della libreria invece di improvvisare sempre da "
        "zero, quando ce n'è una adatta per quella fascia/categoria."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "categoria": {
                "type": "string",
                "description": "Una tra: colazione, pranzo, cena, spuntino. Se omessa, restituisce tutte.",
            },
        },
    },
}

GET_RECIPE = {
    "name": "get_recipe",
    "description": (
        "Restituisce il dettaglio completo di una ricetta: ingredienti con "
        "quantità BASE (per una porzione) e valori nutrizionali base. Usa "
        "questo per scalare le quantità in proporzione al target calorico "
        "del pasto che stai componendo: es. se la ricetta base è 500 kcal "
        "e il pasto target è 650 kcal, moltiplica ogni ingrediente e i "
        "macro per 650/500 = 1.3."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recipe_id": {"type": "integer"},
        },
        "required": ["recipe_id"],
    },
}

GET_USER_PROFILE = {
    "name": "get_user_profile",
    "description": (
        "Restituisce i dati anagrafici salvati dell'utente (altezza, età, "
        "sesso, livello di attività), se presenti. USA SEMPRE questo tool "
        "prima di chiedere questi dati per costruire un piano alimentare — "
        "potrebbero essere già stati salvati in una conversazione "
        "precedente, anche se non li vedi nella cronologia attuale."
    ),
    "parameters": {
        "type": "object",
        "properties": {"user_id": {"type": "integer"}},
        "required": ["user_id"],
    },
}

SET_USER_PROFILE = {
    "name": "set_user_profile",
    "description": (
        "Salva o aggiorna i dati anagrafici dell'utente (altezza, età, "
        "sesso, livello di attività). Chiamalo ogni volta che l'utente "
        "fornisce questi dati, così non dovrai richiederli di nuovo in "
        "futuro. Passa solo i campi che l'utente ha effettivamente fornito."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "altezza_cm": {"type": "number"},
            "eta": {"type": "integer"},
            "sesso": {"type": "string", "description": "M o F"},
            "livello_attivita": {
                "type": "string",
                "description": "es. sedentario, leggermente attivo, moderatamente attivo, molto attivo",
            },
        },
        "required": ["user_id"],
    },
}

JOIN_HOUSEHOLD = {
    "name": "join_household",
    "description": (
        "Collega l'utente corrente allo stesso nucleo familiare di un "
        "altro utente esistente, identificato per nome (es. 'sono il "
        "marito di Nicola, collegami'). Da questo momento condivideranno "
        "la lista della spesa e potranno avere la stessa cena coordinata. "
        "Non fonde nessun dato personale: ognuno mantiene il proprio "
        "peso, obiettivo e piano."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "nome_altro_membro": {
                "type": "string",
                "description": "Nome della persona a cui collegarsi, come indicato dall'utente",
            },
        },
        "required": ["user_id", "nome_altro_membro"],
    },
}

GET_HOUSEHOLD_MEMBERS = {
    "name": "get_household_members",
    "description": (
        "Restituisce gli altri membri del nucleo familiare dell'utente "
        "(id e nome) e se la famiglia ha attivato la sincronizzazione di "
        "TUTTI i pasti principali (sincronizza_tutti_pasti: se False, "
        "coordina solo la cena; se True, coordina anche colazione e "
        "pranzo). Lista vuota se l'utente non è collegato a nessuna "
        "famiglia. USA SEMPRE questo prima di generare la lista della "
        "spesa o comporre un piano pasti, per sapere se e quanto "
        "coordinare con altri membri."
    ),
    "parameters": {
        "type": "object",
        "properties": {"user_id": {"type": "integer"}},
        "required": ["user_id"],
    },
}

SET_HOUSEHOLD_MEAL_SYNC = {
    "name": "set_household_meal_sync",
    "description": (
        "Attiva o disattiva, per TUTTA la famiglia dell'utente, la "
        "sincronizzazione di tutti e tre i pasti principali (colazione, "
        "pranzo, cena) — invece della sola cena, che è il comportamento "
        "di default. Usa questo quando l'utente chiede esplicitamente di "
        "sincronizzare/condividere anche colazione e pranzo con la "
        "famiglia (non solo la cena)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "user_id": {"type": "integer"},
            "attiva": {
                "type": "boolean",
                "description": "true per sincronizzare tutti i pasti, false per tornare alla sola cena",
            },
        },
        "required": ["user_id", "attiva"],
    },
}

RAW_TOOL_DEFINITIONS = [
    GET_DAILY_BALANCE,
    SEARCH_FOOD_NUTRITION,
    LOG_MEAL,
    LOG_ACTIVITY,
    LIST_MEALS,
    UPDATE_MEAL,
    LOG_WEIGHT,
    GET_WEIGHT_HISTORY,
    GET_MEAL_PLAN,
    SET_MEAL_PLAN,
    SEARCH_RECIPES,
    GET_RECIPE,
    GET_USER_PROFILE,
    SET_USER_PROFILE,
    JOIN_HOUSEHOLD,
    GET_HOUSEHOLD_MEMBERS,
    SET_HOUSEHOLD_MEAL_SYNC,
]

# Formato per Gemini (google-genai)
TOOLS = [types.Tool(function_declarations=RAW_TOOL_DEFINITIONS)]

# Formato per Groq (compatibile OpenAI): stessa descrizione, wrapper diverso
GROQ_TOOLS = [
    {"type": "function", "function": tool_def} for tool_def in RAW_TOOL_DEFINITIONS
]

# Formato per Claude (Anthropic): stessa descrizione, chiave "input_schema"
# invece di "parameters" — unica differenza rispetto agli altri due formati.
ANTHROPIC_TOOLS = [
    {
        "name": tool_def["name"],
        "description": tool_def["description"],
        "input_schema": tool_def["parameters"],
    }
    for tool_def in RAW_TOOL_DEFINITIONS
]


def build_system_prompt(user_id: int) -> str:
    """
    Il system prompt include già lo user_id della conversazione, così l'utente
    non deve mai ripeterlo e il modello sa sempre per chi sta chiamando i tool.
    Include anche la data odierna: necessaria per capire richieste come
    "cosa mangiamo stasera?" rispetto a un piano pasti settimanale, ed è
    indispensabile soprattutto quando risponde Groq (che, a differenza di
    Gemini, non ha alcuna nozione della data corrente).
    """
    giorni_settimana = [
        "lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica",
    ]
    oggi = date.today()
    giorno_corrente = giorni_settimana[oggi.weekday()]

    return (
        f"Oggi è {giorno_corrente} {oggi.strftime('%d/%m/%Y')}.\n\n"
        "Sei un coach nutrizionale amichevole e diretto. Rispondi sempre in italiano, "
        "in modo breve e concreto. Usa i tool a disposizione per leggere o aggiornare "
        "i dati reali dell'utente: non inventare mai numeri sulle calorie residue o "
        "bruciate, quelli devono sempre venire da get_daily_balance.\n\n"
        "LUNGHEZZA DELLE RISPOSTE: le tue risposte visibili all'utente non "
        "devono MAI superare i 1000 caratteri (circa 150-180 parole). Se il "
        "contenuto naturale sarebbe più lungo (es. un piano pasti "
        "completo), NON scriverlo tutto: dai un riassunto breve e invita "
        "l'utente a chiedere il dettaglio specifico che gli serve (es. "
        "'chiedimi un giorno specifico se vuoi i dettagli'). I dati "
        "restano comunque salvati per intero nel database anche se la "
        "tua risposta è breve — non è necessario ripetere tutto in chat "
        "per \"non perderlo\".\n\n"
        "REGOLA FONDAMENTALE — MAI CHIEDERE CIÒ CHE PUOI GIÀ SAPERE: prima di "
        "chiedere all'utente cosa ha mangiato in un pasto di OGGI (colazione, "
        "pranzo, cena, spuntini), chiama SEMPRE PRIMA list_meals per la data "
        "odierna. Se un pasto per quella fascia risulta già registrato "
        "(anche se non lo ricordi dalla conversazione attuale — potrebbe "
        "essere stato registrato in un turno precedente, in una sessione "
        "precedente, o da un provider AI diverso che non condivide la "
        "cronologia), USA QUEI DATI direttamente, senza richiederli di "
        "nuovo. Non fidarti solo della cronologia della conversazione: "
        "verifica sempre col database, che è la fonte di verità. Chiedi "
        "solo se list_meals conferma che per quel pasto/oggi non c'è "
        "davvero nulla di registrato. Questo vale anche quando l'utente "
        "chiede di 'ricalcolare il piano in base al pranzo di oggi' o "
        "simili: controlla prima se il pranzo è già lì, non ripartire da "
        "zero chiedendolo.\n\n"
        "IMPORTANTE: se il risultato di un tool contiene una chiave 'errore', NON "
        "interpretarlo come 'nessun dato trovato' — riporta all'utente il messaggio "
        "di errore esatto che hai ricevuto, così può essere risolto. Un errore "
        "tecnico e 'non esiste ancora nulla' sono due cose diverse.\n\n"
        "Quando l'utente descrive un pasto:\n"
        "0. PRIMA di registrare qualsiasi cosa, chiama list_meals per oggi. "
        "Se esiste già un pasto registrato con orario vicino (pochi minuti "
        "o poche decine di minuti prima) i cui alimenti si sovrappongono "
        "in modo significativo con quello che l'utente sta descrivendo ora, "
        "è quasi certamente una CORREZIONE o un DETTAGLIO AGGIUNTIVO dello "
        "stesso pasto, non un pasto nuovo — in questo caso NON chiamare "
        "log_meal (creerebbe un duplicato che gonfia il totale calorico "
        "del giorno): aggiorna quella stessa entry con update_meal usando "
        "il suo id. Chiama log_meal solo se è chiaramente un pasto diverso "
        "(fascia oraria diversa — es. pranzo vs spuntino — o alimenti "
        "completamente diversi).\n"
        "1. Identifica gli alimenti principali e le quantità (se l'utente non dà "
        "le quantità, assumi porzioni standard ragionevoli e dillo chiaramente).\n"
        "2. Per ogni alimento, chiama search_food_nutrition (in inglese) per "
        "ottenere i valori nutrizionali precisi per 100g.\n"
        "3. Calcola le calorie e i macronutrienti totali in base alla quantità "
        "reale (es. 150g di petto di pollo = 1.5 volte il valore per 100g).\n"
        "4. Se un alimento non si trova nel database USDA, fai una stima "
        "approssimativa ragionevole e dillo esplicitamente all'utente, "
        "specificando che non è un dato preciso da database.\n"
        "5. Registra (log_meal) o aggiorna (update_meal, vedi punto 0) il "
        "pasto, includendo calorie e macronutrienti quando li hai calcolati.\n"
        "6. Nella risposta finale, spiega brevemente come hai calcolato le "
        "calorie così l'utente capisce se è un dato preciso o una stima.\n\n"
        "PIANO ALIMENTARE — definizione e correzione:\n"
        "PRIMA di chiedere qualsiasi dato personale (peso, altezza, età...), "
        "applica la REGOLA FONDAMENTALE vista sopra: chiama SEMPRE PRIMA "
        "get_meal_plan E get_user_profile (in questo ordine, prima di "
        "scrivere qualsiasi domanda all'utente). Se get_meal_plan mostra "
        "già un piano con calorie_target e macro impostati, USA QUEI "
        "VALORI — non richiedere di nuovo i dati personali. Questo vale "
        "anche quando l'utente chiede di 'rifare', 'rinnovare' o 'variare' "
        "il piano pasti (es. 'rifammi un piano fit'): di solito intende "
        "ricomporre i pasti (magari con ricette diverse) MANTENENDO gli "
        "stessi obiettivi calorici/macro già salvati, non ripartire da "
        "zero chiedendo peso/altezza/età. Se invece serve davvero definire "
        "un piano ex novo (nessun piano precedente) e get_user_profile "
        "mostra già altezza/età/sesso/attività salvati da una conversazione "
        "precedente, usa quelli e chiedi SOLO i campi mancanti (es. se "
        "manca solo il peso attuale, chiedi solo quello). Chiedi tutti i "
        "dati da zero solo se sia get_meal_plan sia get_user_profile "
        "risultano completamente vuoti.\n\n"
        "Ogni volta che l'utente fornisce peso/altezza/età/sesso/attività "
        "(anche solo parzialmente), salvali SUBITO: il peso con log_weight, "
        "gli altri con set_user_profile — così non dovrai richiederli mai "
        "più, anche se la conversazione dovesse ripartire da zero in "
        "futuro (riavvio del servizio, cambio di provider AI, ecc.).\n\n"
        "Quando invece serve davvero definire un piano da zero (nessun "
        "piano precedente, o l'utente vuole ricominciare), prima di "
        "proporlo raccogli (con una o due domande, non un interrogatorio, "
        "e solo i dati che get_user_profile non ha già): peso attuale, "
        "altezza, età, sesso, livello di attività fisica abituale, e "
        "l'obiettivo (quanto vuole perdere/guadagnare e in quanto tempo, "
        "se lo sa). Poi:\n"
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
        "SETTIMANALE (7 giorni diversi, non lo stesso ripetuto) che ogni "
        "giorno sommi circa a calorie_target. REGOLA DI PRIORITÀ per ogni "
        "pasto: chiama SEMPRE PRIMA search_recipes per quella categoria. "
        "La libreria contiene centinaia di ricette (comprese molte 'fit', "
        "più proteiche): usa quelle come prima scelta, non come eccezione. "
        "Se trovi almeno una ricetta adatta, chiama get_recipe e SCALA le "
        "quantità in proporzione al target di quel pasto (es. ricetta base "
        "500 kcal, pasto target 650 kcal -> moltiplica ogni ingrediente e "
        "i macro per 650/500). Componi un pasto da zero con "
        "search_food_nutrition SOLO se search_recipes non restituisce "
        "nulla di adatto per quella categoria/esigenza (es. l'utente ha "
        "chiesto qualcosa che nessuna ricetta salvata copre). Se l'utente "
        "chiede esplicitamente pasti 'fit'/ad alta proteina, filtra e "
        "preferisci le ricette con 'fit'/'proteic' nel nome o con un "
        "rapporto proteine/calorie alto. Varia le ricette tra un giorno e "
        "l'altro per rendere il piano sostenibile.\n"
        "4ter. SALVA SUBITO, POI RIASSUMI BREVEMENTE — regola critica "
        "anti-spreco: componi il testo COMPLETO del piano (tutti i 7 "
        "giorni) e chiama set_meal_plan con pasti_suggeriti popolato PRIMA "
        "di scrivere la tua risposta visibile all'utente. Se ti accorgi "
        "di aver già generato molto contenuto senza aver ancora chiamato "
        "set_meal_plan, chiamalo SUBITO con quello che hai composto finora "
        "(anche solo alcuni giorni), così il lavoro non va perso se lo "
        "spazio di risposta dovesse esaurirsi. Nella risposta finale "
        "all'utente NON ripetere l'intero piano giorno per giorno: sarebbe "
        "uno spreco enorme di token, dato che il piano resta comunque "
        "consultabile in ogni momento (es. 'cosa mangiamo lunedì?', "
        "'fammi vedere il piano'). Dai invece una conferma BREVE (3-5 "
        "frasi): che il piano settimanale è stato salvato, il target "
        "medio giornaliero (calorie e macro), un solo esempio (es. il "
        "menu di oggi) a titolo di assaggio, e invita l'utente a chiedere "
        "un giorno specifico se vuole vedere il resto.\n"
        "4bis. COORDINAMENTO PASTI IN FAMIGLIA: prima di comporre il piano "
        "di ogni giorno, chiama get_household_members. Se l'utente ha "
        "familiari collegati, guarda il campo sincronizza_tutti_pasti "
        "restituito:\n"
        "   - Se è False (default): applica il coordinamento SOLO alla "
        "cena.\n"
        "   - Se è True: applica lo stesso coordinamento a TUTTI e tre i "
        "pasti principali (colazione, pranzo, cena) — non agli spuntini.\n"
        "Per ciascun pasto da coordinare: chiama get_meal_plan per ogni "
        "familiare e guarda se ha già quel pasto pianificato per quel "
        "giorno con il nome di una ricetta della libreria. Se sì, usa la "
        "STESSA ricetta anche per questo utente (comodo per cucinare un "
        "solo piatto in famiglia), scalando le quantità al SUO target "
        "individuale (calorico e di macro, diverso da quello degli altri "
        "membri). Se nessun familiare ha ancora quel pasto pianificato per "
        "quel giorno, scegli tu una ricetta normalmente e menzionalo nella "
        "nota, così gli altri membri potranno riprenderla quando "
        "comporranno il loro piano.\n"
        "Se l'utente chiede esplicitamente di sincronizzare/condividere "
        "anche colazione e pranzo con la famiglia (non solo la cena), "
        "chiama set_household_meal_sync con attiva=true. Se chiede di "
        "tornare a sincronizzare solo la cena, chiamalo con attiva=false.\n"
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
        "Sai già che giorno è oggi (vedi inizio di queste istruzioni). "
        "Chiama get_meal_plan e rispondi SOLO con la parte pertinente di "
        "pasti_suggeriti per IL GIORNO DELLA SETTIMANA CORRENTE (es. se "
        "oggi è martedì e chiede 'stasera', cerca la sezione di martedì e "
        "dai solo la cena di quel giorno), senza rigenerare o riscrivere "
        "tutto il piano. 'Stasera'/'a cena' = cena, 'oggi a pranzo'/'a "
        "mezzogiorno' = pranzo, 'colazione'/'al mattino' = colazione. Se "
        "l'utente chiede di un altro giorno (es. 'cosa mangio venerdì?'), "
        "usa quel giorno invece di oggi. Se non esiste ancora un piano "
        "pasti concreto, dillo e offriti di crearne uno.\n\n"
        "NUCLEO FAMILIARE — collegare più utenti:\n"
        "Quando l'utente dice di voler collegarsi a un familiare già "
        "registrato (es. 'sono il marito di Nicola, collegami', 'unisciti "
        "alla famiglia di Anna'), chiama join_household con il nome "
        "indicato. Se il tool restituisce un errore per nome ambiguo o "
        "non trovato, chiedi di specificare meglio. Dopo il collegamento, "
        "spiega che da ora condividerete la lista della spesa e la cena "
        "sarà coordinata (stessa ricetta, porzioni individuali) — e che "
        "possono chiedere di estendere il coordinamento anche a colazione "
        "e pranzo se preferiscono. Peso/obiettivo/piano restano comunque "
        "individuali. All'inizio di una richiesta di lista della spesa o "
        "di composizione del piano, chiama sempre get_household_members "
        "per sapere se l'utente ha familiari collegati e se ha attivato "
        "la sincronizzazione di tutti i pasti.\n\n"
        "LISTA DELLA SPESA:\n"
        "Quando l'utente chiede la lista della spesa (es. 'fammi la lista "
        "della spesa', 'cosa devo comprare per la settimana'):\n"
        "1. Chiama get_household_members. Se ci sono membri collegati, "
        "chiama get_meal_plan anche per ciascuno di loro (oltre al "
        "richiedente) e costruisci UNA lista unica per tutta la famiglia, "
        "sommando gli ingredienti di tutti i piani insieme — non liste "
        "separate. Se non ci sono membri, procedi solo col proprio piano.\n"
        "2. Chiama get_meal_plan per leggere pasti_suggeriti (del "
        "richiedente e di eventuali membri).\n"
        "3. Se non esiste un piano pasti concreto (né per il richiedente "
        "né per i membri), dillo e offriti prima di crearne uno.\n"
        "4. Il piano copre già 7 giorni diversi: somma gli ingredienti di "
        "tutti i giorni presenti nel piano (non moltiplicare per 7, il "
        "piano NON si ripete uguale ogni giorno).\n"
        "5. Consolida gli ingredienti uguali o molto simili sommando le "
        "quantità (es. se il pollo compare in più giorni o per più "
        "persone della famiglia, sommalo in una sola voce).\n"
        "6. Presenta la lista organizzata per categoria (es. Proteine, "
        "Carboidrati/cereali, Frutta e verdura, Latticini, Dispensa/altro), "
        "con quantità totali arrotondate in modo pratico per la spesa (es. "
        "'circa 1.2 kg' invece di '1173g').\n"
        "7. Ricorda che è una stima basata sul piano: l'utente può avere "
        "già alcuni ingredienti in casa, quindi la lista va adattata.\n"
        "8. Dato che il piano è composto PRIORITARIAMENTE da ricette della "
        "libreria (vedi regola di priorità sopra), per ogni pasto del "
        "piano che corrisponde a una ricetta chiama get_recipe per "
        "ottenere gli ingredienti esatti con le quantità — questa è la "
        "fonte principale e più precisa per la lista della spesa, da "
        "preferire sempre rispetto a stimare dal testo libero del piano. "
        "Stima dal testo solo per gli eventuali pasti composti da zero "
        "(quando search_recipes non aveva nulla di adatto).\n\n"
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
        "RICALCOLO/CORREZIONE DI UN PASTO GIÀ REGISTRATO — quando l'utente "
        "chiede di ricalcolare, correggere o aggiornare le calorie di un "
        "pasto (es. 'ricalcola l'ultimo pasto', 'in realtà la porzione era "
        "più grande', 'correggi quello di prima'):\n"
        "1. NON chiedere all'utente quale fosse il pasto o i suoi dettagli "
        "se ha già registrato qualcosa oggi — chiama SEMPRE prima list_meals "
        "per la data di oggi (o quella indicata) per vederlo da solo.\n"
        "2. Se list_meals restituisce un solo pasto recente pertinente, è "
        "quello a cui l'utente si riferisce (di solito l'ultimo per "
        "orario). Se ce ne sono più di uno plausibili e non è ovvio a "
        "quale si riferisca, allora sì chiedi chiarimento.\n"
        "3. Ricalcola i valori corretti (con search_food_nutrition se serve "
        "maggiore precisione, tenendo conto della correzione indicata "
        "dall'utente, es. porzione più grande).\n"
        "4. Aggiorna quella stessa entry con update_meal passando il suo "
        "id — NON usare log_meal per la correzione, altrimenti il pasto "
        "verrebbe contato due volte nel bilancio del giorno.\n\n"
        "RIADATTAMENTO IN TEMPO REALE (giornaliero) — quando l'utente ti dice "
        "cosa ha mangiato (specialmente se fuori piano, una porzione più "
        "grande, un pasto saltato), o ti chiede di ricalcolare/adattare i "
        "pasti restanti in base a un pasto già fatto oggi:\n"
        "0. Se il pasto in questione non è nel messaggio attuale (es. "
        "'ricalcola in base al pranzo di oggi'), applica la REGOLA "
        "FONDAMENTALE vista sopra: controlla con list_meals prima di "
        "chiedere, invece di richiedere all'utente di ridescriverlo.\n"
        "1. Se invece il pasto viene descritto ora, registralo con log_meal, poi "
        "chiama get_daily_balance per il giorno corrente: ora include anche i "
        "residui di proteine/carboidrati/grassi, non solo le calorie.\n"
        "2. Chiama get_meal_plan per vedere i pasti ancora da fare oggi "
        "(usa la sezione del giorno della settimana corrente).\n"
        "3. Se i residui (calorie e/o macro) sono già molto bassi o "
        "negativi rispetto ai pasti ancora previsti, suggerisci come "
        "alleggerire i prossimi pasti (es. porzioni più piccole, meno "
        "carboidrati o grassi a cena) mantenendo comunque un pasto "
        "completo — non suggerire mai di saltare un pasto interamente o "
        "scendere sotto le 1200 kcal totali giornaliere.\n"
        "4. Se invece è avanzato margine (es. ha mangiato meno del previsto), "
        "puoi suggerire di aumentare leggermente il pasto successivo.\n"
        "5. Questi aggiustamenti sono per default solo CONSIGLI nella "
        "conversazione (non serve salvare nulla con set_meal_plan): "
        "modifica il piano salvato solo se l'utente esplicitamente chiede "
        "di aggiornarlo in modo permanente per quel giorno.\n\n"
        "RIADATTAMENTO SETTIMANALE — se l'utente segnala un giorno "
        "significativamente sopra o sotto il target (es. un pasto fuori "
        "molto abbondante, un evento) e chiede di 'recuperare' nei giorni "
        "successivi:\n"
        "1. Chiama get_meal_plan per vedere i giorni rimanenti della "
        "settimana.\n"
        "2. Distribuisci un piccolo aggiustamento (es. -100/-150 kcal per "
        "2-3 giorni, non un taglio drastico in un solo giorno) sui pasti "
        "dei giorni successivi, spiegando la logica.\n"
        "3. Anche qui, aggiorna pasti_suggeriti con set_meal_plan solo se "
        "l'utente conferma di volerlo salvare così; altrimenti è un "
        "consiglio verbale per quella settimana.\n\n"
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
                "proteine_target_g": balance.proteine_target_g,
                "proteine_assunte_g": balance.proteine_assunte_g,
                "proteine_residue_g": balance.proteine_residue_g,
                "carboidrati_target_g": balance.carboidrati_target_g,
                "carboidrati_assunti_g": balance.carboidrati_assunti_g,
                "carboidrati_residui_g": balance.carboidrati_residui_g,
                "grassi_target_g": balance.grassi_target_g,
                "grassi_assunti_g": balance.grassi_assunti_g,
                "grassi_residui_g": balance.grassi_residui_g,
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

    if name == "list_meals":
        db = SessionLocal()
        try:
            giorno = (
                date.fromisoformat(tool_input["giorno"])
                if tool_input.get("giorno")
                else date.today()
            )
            meals = crud.list_meals_for_day(db, tool_input["user_id"], giorno)
            if not meals:
                return {"pasti": [], "nota": "Nessun pasto registrato in questa data"}
            return {
                "pasti": [
                    {
                        "id": m.id,
                        "nome_alimento": m.nome_alimento,
                        "calorie": m.calorie,
                        "proteine_g": m.proteine_g,
                        "carboidrati_g": m.carboidrati_g,
                        "grassi_g": m.grassi_g,
                        "orario": m.orario.isoformat(),
                        "aggiornato_at": m.aggiornato_at.isoformat() if m.aggiornato_at else None,
                        "fonte": m.fonte,
                    }
                    for m in meals
                ]
            }
        finally:
            db.close()

    if name == "update_meal":
        db = SessionLocal()
        try:
            update_in = schemas.MealUpdate(
                nome_alimento=tool_input.get("nome_alimento"),
                calorie=tool_input.get("calorie"),
                proteine_g=tool_input.get("proteine_g"),
                carboidrati_g=tool_input.get("carboidrati_g"),
                grassi_g=tool_input.get("grassi_g"),
            )
            meal = crud.update_meal(db, int(tool_input["meal_id"]), update_in)
            if meal is None:
                return {"errore": f"Nessun pasto trovato con id {tool_input['meal_id']}"}
            return {
                "id": meal.id,
                "nome_alimento": meal.nome_alimento,
                "calorie": meal.calorie,
                "proteine_g": meal.proteine_g,
                "carboidrati_g": meal.carboidrati_g,
                "grassi_g": meal.grassi_g,
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
                return {"piano": None}
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

    if name == "search_recipes":
        db = SessionLocal()
        try:
            ricette = crud.list_recipes(db, tool_input.get("categoria"))
            if not ricette:
                return {"ricette": [], "nota": "Nessuna ricetta trovata per questa categoria"}
            return {
                "ricette": [
                    {
                        "id": r.id,
                        "nome": r.nome,
                        "categoria": r.categoria,
                        "calorie_base": r.calorie_base,
                        "proteine_base_g": r.proteine_base_g,
                        "carboidrati_base_g": r.carboidrati_base_g,
                        "grassi_base_g": r.grassi_base_g,
                    }
                    for r in ricette
                ]
            }
        finally:
            db.close()

    if name == "get_recipe":
        db = SessionLocal()
        try:
            ricetta = crud.get_recipe(db, int(tool_input["recipe_id"]))
            if ricetta is None:
                return {"errore": f"Nessuna ricetta trovata con id {tool_input['recipe_id']}"}
            return {
                "id": ricetta.id,
                "nome": ricetta.nome,
                "categoria": ricetta.categoria,
                "ingredienti_base": ricetta.ingredienti,
                "calorie_base": ricetta.calorie_base,
                "proteine_base_g": ricetta.proteine_base_g,
                "carboidrati_base_g": ricetta.carboidrati_base_g,
                "grassi_base_g": ricetta.grassi_base_g,
                "note": ricetta.note,
            }
        finally:
            db.close()

    if name == "get_user_profile":
        db = SessionLocal()
        try:
            user = crud.get_user(db, tool_input["user_id"])
            if user is None:
                return {"errore": f"Utente {tool_input['user_id']} non trovato"}
            return {
                "altezza_cm": user.altezza_cm,
                "eta": user.eta,
                "sesso": user.sesso,
                "livello_attivita": user.livello_attivita,
            }
        finally:
            db.close()

    if name == "set_user_profile":
        db = SessionLocal()
        try:
            profile_in = schemas.UserProfileUpdate(
                altezza_cm=tool_input.get("altezza_cm"),
                eta=tool_input.get("eta"),
                sesso=tool_input.get("sesso"),
                livello_attivita=tool_input.get("livello_attivita"),
            )
            user = crud.update_user_profile(db, tool_input["user_id"], profile_in)
            if user is None:
                return {"errore": f"Utente {tool_input['user_id']} non trovato"}
            return {
                "altezza_cm": user.altezza_cm,
                "eta": user.eta,
                "sesso": user.sesso,
                "livello_attivita": user.livello_attivita,
            }
        finally:
            db.close()

    if name == "join_household":
        db = SessionLocal()
        try:
            user, errore = crud.join_household_by_name(
                db, tool_input["user_id"], tool_input["nome_altro_membro"]
            )
            if errore:
                return {"errore": errore}
            return {"household_id": user.household_id, "collegato_a": tool_input["nome_altro_membro"]}
        finally:
            db.close()

    if name == "get_household_members":
        db = SessionLocal()
        try:
            membri = crud.get_household_members(db, tool_input["user_id"])
            if not membri:
                return {
                    "membri": [],
                    "sincronizza_tutti_pasti": None,
                    "nota": "L'utente non fa parte di nessun nucleo familiare",
                }
            sync = crud.get_household_sync_setting(db, tool_input["user_id"])
            return {
                "membri": [{"id": m.id, "nome": m.nome} for m in membri],
                "sincronizza_tutti_pasti": sync,
            }
        finally:
            db.close()

    if name == "set_household_meal_sync":
        db = SessionLocal()
        try:
            risultato = crud.set_household_sync_setting(
                db, tool_input["user_id"], bool(tool_input["attiva"])
            )
            if risultato is None:
                return {"errore": "L'utente non fa parte di nessun nucleo familiare"}
            return {"sincronizza_tutti_pasti": risultato}
        finally:
            db.close()

    raise ValueError(f"Tool sconosciuto: {name}")


def _execute_tool_logged(name: str, tool_input: dict) -> dict:
    """
    Esegue un tool e stampa sempre nei log del server (visibili su Render,
    scheda "Logs") qualunque errore capiti — sia un'eccezione imprevista sia
    un errore già gestito e restituito come {"errore": ...}. Senza questo,
    un fallimento silenzioso di un tool è invisibile: l'agente lo trasforma
    in un messaggio all'utente, ma nessuna traccia tecnica arriva ai log.
    """
    try:
        result = execute_tool(name, tool_input)
    except Exception as e:
        print(f"[TOOL EXCEPTION] {name}({tool_input}) -> {e}", flush=True)
        return {"errore": str(e)}

    if isinstance(result, dict) and "errore" in result:
        print(f"[TOOL ERROR] {name}({tool_input}) -> {result['errore']}", flush=True)
    return result


MAX_RISPOSTA_CARATTERI = 1000


def _limita_lunghezza(testo: str, max_caratteri: int = MAX_RISPOSTA_CARATTERI) -> str:
    """
    Taglia la risposta finale a un massimo di caratteri, tagliando all'ultimo
    spazio utile per non spezzare una parola a metà. Applicata a tutti e tre
    i motori (Gemini, Groq, Claude) come rete di sicurezza indipendente
    dall'istruzione data nel system prompt, che da sola non è garantita al
    100% (i modelli non seguono sempre alla lettera i vincoli di lunghezza).
    """
    if len(testo) <= max_caratteri:
        return testo
    tagliato = testo[:max_caratteri]
    ultimo_spazio = tagliato.rfind(" ")
    if ultimo_spazio > max_caratteri * 0.8:
        tagliato = tagliato[:ultimo_spazio]
    return tagliato.rstrip() + "… (risposta accorciata per restare concisa — chiedi pure di continuare)"


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
                "max_tokens": 4096,
            }
        )
        message = data["choices"][0]["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return _limita_lunghezza(message.get("content") or "")

        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            args = json.loads(tool_call["function"]["arguments"] or "{}")
            result = _execute_tool_logged(name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

    return "Non sono riuscito a completare la richiesta, riprova tra poco."


def _is_transient_anthropic_error(exc: Exception) -> bool:
    """Come _is_transient_error, ma per le eccezioni del SDK Anthropic."""
    status_code = getattr(exc, "status_code", None)
    if status_code in (503, 429, 529):  # 529 = "overloaded_error" di Anthropic
        return True
    testo = str(exc).upper()
    return "OVERLOADED" in testo or "RATE_LIMIT" in testo


def _claude_create_with_retry(messages: list, system_blocks: list):
    """Chiama Claude riprovando sugli errori temporanei, stessa logica delle altre riserve."""
    ultimo_errore = None
    for tentativo in range(CLAUDE_MAX_RETRIES):
        try:
            return claude_client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                system=system_blocks,
                tools=ANTHROPIC_TOOLS,
                messages=messages,
            )
        except Exception as e:
            ultimo_errore = e
            if not _is_transient_anthropic_error(e) or tentativo == CLAUDE_MAX_RETRIES - 1:
                raise
            time.sleep(CLAUDE_RETRY_BASE_DELAY * (2 ** tentativo))
    raise ultimo_errore


def run_turn_claude(user_text: str, system_prompt: str) -> str:
    """
    Seconda riserva, usata solo se anche Groq fallisce. Il system prompt è
    marcato con cache_control: essendo lungo e identico ad ogni chiamata,
    dopo la prima volta le richieste successive pagano solo il 10% del
    prezzo normale su quella parte (prompt caching di Anthropic).
    """
    if claude_client is None:
        raise RuntimeError("Nessuna riserva disponibile: ANTHROPIC_API_KEY non impostata")

    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    messages = [{"role": "user", "content": user_text}]

    for _ in range(6):  # limite di sicurezza sui giri di tool use
        response = _claude_create_with_retry(messages, system_blocks)

        blocchi_assistente = [
            (
                {"type": "text", "text": blocco.text}
                if blocco.type == "text"
                else {
                    "type": "tool_use",
                    "id": blocco.id,
                    "name": blocco.name,
                    "input": blocco.input,
                }
            )
            for blocco in response.content
        ]
        messages.append({"role": "assistant", "content": blocchi_assistente})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            testo = "".join(b.text for b in response.content if b.type == "text")
            return _limita_lunghezza(testo)

        risultati_tool = []
        for blocco in tool_use_blocks:
            result = _execute_tool_logged(blocco.name, dict(blocco.input))
            risultati_tool.append(
                {
                    "type": "tool_result",
                    "tool_use_id": blocco.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )
        messages.append({"role": "user", "content": risultati_tool})

    return "Non sono riuscito a completare la richiesta, riprova tra poco."


def run_turn(contents: list, system_prompt: str, user_text: str | None = None) -> tuple[list, str]:
    """
    Gestisce un turno di conversazione, incluso l'eventuale ciclo di function
    calling. `contents` è la cronologia nel formato del SDK google-genai.
    Ritorna (contents_aggiornati, testo_risposta_finale).

    Se Gemini continua a fallire dopo i tentativi automatici e `user_text` è
    disponibile, prova prima la riserva Groq, poi Claude Haiku come ultima
    risorsa (nell'ordine: gratuito -> gratuito -> economico a pagamento).
    """
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=TOOLS,
    )

    while True:
        try:
            response = _generate_with_retry(contents, config)
        except Exception:
            reply = None
            errori_riserve = []

            if user_text is not None and GROQ_API_KEY:
                try:
                    reply = run_turn_groq(user_text, system_prompt)
                except Exception as errore_groq:
                    errori_riserve.append(f"Groq: {errore_groq}")

            if reply is None and user_text is not None and ANTHROPIC_API_KEY:
                try:
                    reply = run_turn_claude(user_text, system_prompt)
                except Exception as errore_claude:
                    errori_riserve.append(f"Claude: {errore_claude}")

            if reply is not None:
                contents.append(
                    types.Content(role="model", parts=[types.Part.from_text(text=reply)])
                )
                return contents, reply

            if errori_riserve:
                raise RuntimeError(
                    "tutti i sistemi AI disponibili (Gemini e le riserve configurate) "
                    "sono momentaneamente non disponibili. Riprova tra qualche minuto."
                )
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
            return contents, _limita_lunghezza(final_text)

        response_parts = []
        for call in function_calls:
            result = _execute_tool_logged(call.name, dict(call.args))

            response_parts.append(
                types.Part.from_function_response(name=call.name, response=result)
            )

        contents.append(types.Content(role="user", parts=response_parts))
