"""
Schemi Pydantic: definiscono la forma dei dati che entrano/escono dalle API.
Tenerli separati dai modelli SQLAlchemy evita di esporre per sbaglio colonne
interne e rende più facile validare gli input dell'agente.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ---------- User ----------

class UserCreate(BaseModel):
    nome: str
    obiettivo_calorico_giornaliero: int = 2000


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nome: str
    obiettivo_calorico_giornaliero: int
    zepp_account_id: Optional[str] = None
    created_at: datetime


# ---------- DailyActivity ----------

class ActivityUpsert(BaseModel):
    """Usato sia per l'inserimento manuale sia (in futuro) dai webhook Terra."""

    data: date
    calorie_attive_bruciate: float = 0
    passi: int = 0
    minuti_allenamento: int = 0
    fonte: str = "manuale"


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    data: date
    calorie_attive_bruciate: float
    passi: int
    minuti_allenamento: int
    fonte: str


# ---------- Meal ----------

class MealCreate(BaseModel):
    nome_alimento: str
    calorie: float
    proteine_g: Optional[float] = None
    carboidrati_g: Optional[float] = None
    grassi_g: Optional[float] = None
    data: Optional[date] = None  # se omessa, oggi
    fonte: str = "manuale"


class MealOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    data: date
    nome_alimento: str
    calorie: float
    proteine_g: Optional[float]
    carboidrati_g: Optional[float]
    grassi_g: Optional[float]
    fonte: str


# ---------- Bilancio calorico ----------

class DailyBalance(BaseModel):
    user_id: int
    data: date
    obiettivo_calorico_giornaliero: int
    calorie_attive_bruciate: float
    calorie_assunte: float
    calorie_residue: float
    numero_pasti_registrati: int
