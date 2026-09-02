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
)
from sqlalchemy.orm import relationship

from database import Base


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

    created_at = Column(DateTime, default=datetime.utcnow)

    daily_activities = relationship("DailyActivity", back_populates="user")
    meals = relationship("Meal", back_populates="user")


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
    fonte = Column(String, nullable=False, default="manuale")  # "manuale" | "agente"

    user = relationship("User", back_populates="meals")
