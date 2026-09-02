"""
Logica condivisa dell'agente: definizione dei tool, esecuzione (chiamate reali
al backend) e ciclo agentico con Gemini. Sia agent.py (CLI) che il bot Telegram
importano da qui, così la "logica" dell'agente esiste in un solo posto e le due
interfacce sono solo un modo diverso di scambiare messaggi con l'utente.

Usa l'API gratuita di Google Gemini (tier gratuito con limiti giornalieri,
niente carta di credito richiesta). Chiave da https://aistudio.google.com
"""

import datetime
import os

import requests
from google import genai
from google.genai import types

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

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

TOOLS = [
    types.Tool(
        function_declarations=[
            GET_DAILY_BALANCE,
            SEARCH_FOOD_NUTRITION,
            LOG_MEAL,
            LOG_ACTIVITY,
        ]
    )
]


def build_system_prompt(user_id: int) -> str:
    """
    Il system prompt include già lo user_id della conversazione, così l'utente
    non deve mai ripeterlo e il modello sa sempre per chi sta chiamando i tool.
    Fondamentale per il bot Telegram, dove ogni chat corrisponde a un utente fisso.
    """
    return (
        "Sei un coach nutrizionale amichevole e diretto. Rispondi sempre in italiano, "
        "in modo breve e concreto. Usa i tool a disposizione per leggere o aggiornare "
        "i dati reali dell'utente: non inventare mai numeri sulle calorie residue o "
        "bruciate, quelli devono sempre venire da get_daily_balance.\n\n"
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
        "calorie (es. 'una porzione di circa 150g di pollo, dal database USDA') "
        "così l'utente capisce se è un dato preciso o una stima.\n\n"
        f"L'utente con cui stai parlando ha user_id={user_id}: usalo sempre nei tool, "
        "anche se l'utente non lo specifica."
    )


def search_food_nutrition(query: str) -> list[dict]:
    """
    Cerca un alimento su USDA FoodData Central e restituisce i valori
    nutrizionali per 100g dei primi risultati più rilevanti. Usa i dataset
    "Foundation" e "SR Legacy": alimenti generici (non prodotti confezionati
    di marca), più adatti a calcolare pasti descritti in linguaggio naturale.
    """
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
    """Esegue una chiamata reale al backend FastAPI o al database USDA."""
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
        params = {}
        if tool_input.get("giorno"):
            params["giorno"] = tool_input["giorno"]
        resp = requests.get(
            f"{BACKEND_URL}/users/{tool_input['user_id']}/balance", params=params
        )
        resp.raise_for_status()
        return resp.json()

    if name == "log_meal":
        payload = {
            "nome_alimento": tool_input["nome_alimento"],
            "calorie": tool_input["calorie"],
            "fonte": "agente",
        }
        for campo in ("proteine_g", "carboidrati_g", "grassi_g"):
            if tool_input.get(campo) is not None:
                payload[campo] = tool_input[campo]
        resp = requests.post(
            f"{BACKEND_URL}/users/{tool_input['user_id']}/meals", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    if name == "log_activity":
        payload = {
            "data": datetime.date.today().isoformat(),
            "calorie_attive_bruciate": tool_input.get("calorie_attive_bruciate", 0),
            "passi": tool_input.get("passi", 0),
            "minuti_allenamento": tool_input.get("minuti_allenamento", 0),
            "fonte": "manuale",
        }
        resp = requests.put(
            f"{BACKEND_URL}/users/{tool_input['user_id']}/activity", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    raise ValueError(f"Tool sconosciuto: {name}")


def run_turn(contents: list, system_prompt: str) -> tuple[list, str]:
    """
    Gestisce un turno di conversazione, incluso l'eventuale ciclo di function
    calling. `contents` è la cronologia nel formato del SDK google-genai
    (lista di types.Content, con role "user" o "model"). Ritorna
    (contents_aggiornati, testo_risposta_finale).
    """
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=TOOLS,
    )

    while True:
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=config,
        )

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

        # Il modello vuole chiamare uno o più tool: li eseguiamo e rimandiamo
        # i risultati come "function_response" nello stesso turno.
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
        # torna in cima al while: il modello vede i risultati e continua


def create_backend_user(nome: str, obiettivo_calorico_giornaliero: int = 2000) -> dict:
    """Crea un nuovo utente nel backend. Usato dal comando /start del bot Telegram."""
    resp = requests.post(
        f"{BACKEND_URL}/users",
        json={"nome": nome, "obiettivo_calorico_giornaliero": obiettivo_calorico_giornaliero},
    )
    resp.raise_for_status()
    return resp.json()
