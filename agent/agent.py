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
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

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

LOG_MEAL = {
    "name": "log_meal",
    "description": (
        "Registra un pasto per l'utente con una stima delle calorie. "
        "Usa questo tool quando l'utente descrive qualcosa che ha mangiato o bevuto."
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
                "description": "Stima delle calorie del pasto in kcal",
            },
        },
        "required": ["user_id", "nome_alimento", "calorie"],
    },
}