"""
Modelli dello schema dati, coerenti con il documento di architettura:

- users: utenti dell'app, con obiettivo calorico giornaliero
- daily_activity: dati di attività per giorno (da Zepp via Terra, o manuali)
- meals: pasti registrati (manuali o via agente)
"""

from datetime import date, datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Text,
    JSON,
)
from sqlalchemy.orm import relationship

from database import Base


class Household(Base):
    """
    Nucleo familiare: collega più utenti per condividere la lista della
    spesa e coordinare i pasti (es. la stessa cena per tutta la famiglia,
    con quantità scalate individualmente). Non fonde nessun dato personale:
    ogni utente mantiene il proprio peso, obiettivo e piano.
    """

    __tablename__ = "households"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=True)
    creato_at = Column(DateTime, default=datetime.utcnow)

    membri = relationship("User", back_populates="household")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    obiettivo_calorico_giornaliero = Column(Integer, nullable=False, default=2000)

    # riferimento all'account collegato via Terra API (popolato in seguito,
    # quando integreremo davvero Zepp). Per ora resta NULL.
    zepp_account_id = Column(String, nullable=True, unique=True)

    # id della chat Telegram collegata a questo utente (impostato al primo /start)
    telegram_chat_id = Column(String, nullable=True, unique=True, index=True)

    # Profilo anagrafico, usato per calcolare il piano alimentare senza
    # doverlo richiedere ogni volta che la memoria della conversazione si
    # azzera (riavvio del servizio, passaggio a un provider AI diverso, ecc.)
    altezza_cm = Column(Float, nullable=True)
    eta = Column(Integer, nullable=True)
    sesso = Column(String, nullable=True)  # "M" | "F"
    livello_attivita = Column(String, nullable=True)  # es. "moderatamente attivo"

    # Nucleo familiare collegato, se presente (NULL = utente indipendente)
    household_id = Column(Integer, ForeignKey("households.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    household = relationship("Household", back_populates="membri")
    daily_activities = relationship("DailyActivity", back_populates="user")
    meals = relationship("Meal", back_populates="user")
    weight_entries = relationship("WeightEntry", back_populates="user")
    meal_plan = relationship("MealPlan", uselist=False, back_populates="user")


class DailyActivity(Base):
    __tablename__ = "daily_activity"
    __table_args__ = (
        # un solo record di attività per utente per giorno
        UniqueConstraint("user_id", "data", name="uq_user_data"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    data = Column(Date, nullable=False, default=date.today)

    calorie_attive_bruciate = Column(Float, nullable=False, default=0)
    passi = Column(Integer, nullable=False, default=0)
    minuti_allenamento = Column(Integer, nullable=False, default=0)

    # "zepp" quando arriverà da Terra API, "manuale" se inserito a mano
    fonte = Column(String, nullable=False, default="manuale")
    aggiornato_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="daily_activities")


class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    data = Column(Date, nullable=False, default=date.today)

    nome_alimento = Column(String, nullable=False)
    calorie = Column(Float, nullable=False)
    proteine_g = Column(Float, nullable=True)
    carboidrati_g = Column(Float, nullable=True)
    grassi_g = Column(Float, nullable=True)

    orario = Column(DateTime, default=datetime.utcnow)
    aggiornato_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    fonte = Column(String, nullable=False, default="manuale")  # "manuale" | "agente"

    user = relationship("User", back_populates="meals")


class WeightEntry(Base):
    """Peso corporeo registrato nel tempo, per valutare l'andamento del percorso."""

    __tablename__ = "weight_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "data", name="uq_user_weight_data"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    data = Column(Date, nullable=False, default=date.today)
    peso_kg = Column(Float, nullable=False)
    note = Column(String, nullable=True)

    user = relationship("User", back_populates="weight_entries")


class MealPlan(Base):
    """
    Piano alimentare attivo dell'utente: un solo piano per utente (viene
    aggiornato/corretto nel tempo dall'agente, non se ne accumulano tanti).
    """

    __tablename__ = "meal_plans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)

    obiettivo = Column(String, nullable=True)  # es. "perdita 0.5 kg/settimana"
    calorie_target = Column(Integer, nullable=False)
    proteine_target_g = Column(Float, nullable=True)
    carboidrati_target_g = Column(Float, nullable=True)
    grassi_target_g = Column(Float, nullable=True)

    # piano pasti vero e proprio (colazione/pranzo/cena/spuntini), testo
    # libero composto dall'agente — non solo target numerici astratti
    pasti_suggeriti = Column(Text, nullable=True)

    note = Column(String, nullable=True)

    creato_at = Column(DateTime, default=datetime.utcnow)
    aggiornato_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="meal_plan")


class Recipe(Base):
    """
    Libreria di ricette condivisa (non legata a un singolo utente): l'agente
    la usa come spunto quando compone o rivede un piano pasti, scalando le
    quantità in proporzione al target calorico del pasto in questione.
    """

    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    categoria = Column(String, nullable=False)  # "colazione" | "pranzo" | "cena" | "spuntino"

    # Lista di ingredienti per la porzione BASE, es.
    # [{"alimento": "petto di pollo", "quantita_g": 150}, {...}]
    ingredienti = Column(JSON, nullable=False)

    calorie_base = Column(Float, nullable=False)
    proteine_base_g = Column(Float, nullable=True)
    carboidrati_base_g = Column(Float, nullable=True)
    grassi_base_g = Column(Float, nullable=True)

    note = Column(String, nullable=True)  # es. brevi istruzioni di preparazione
