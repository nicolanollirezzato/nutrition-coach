"""
Funzioni di accesso ai dati. Tenerle separate dagli endpoint FastAPI
rende più facile testarle e riutilizzarle (es. da uno script di seed
o dal futuro handler dei webhook Terra).
"""

from datetime import date as date_type

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import schemas


# ---------- Users ----------

def create_user(db: Session, user: schemas.UserCreate) -> models.User:
    db_user = models.User(
        nome=user.nome,
        obiettivo_calorico_giornaliero=user.obiettivo_calorico_giornaliero,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def get_user(db: Session, user_id: int) -> models.User | None:
    return db.query(models.User).filter(models.User.id == user_id).first()


def get_user_by_telegram_chat_id(db: Session, chat_id: str) -> models.User | None:
    return (
        db.query(models.User)
        .filter(models.User.telegram_chat_id == chat_id)
        .first()
    )


def create_user_with_telegram(
    db: Session, chat_id: str, nome: str, obiettivo_calorico_giornaliero: int = 2000
) -> models.User:
    """Crea un utente già collegato a una chat Telegram (usato dal comando /start)."""
    db_user = models.User(
        nome=nome,
        obiettivo_calorico_giornaliero=obiettivo_calorico_giornaliero,
        telegram_chat_id=chat_id,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


# ---------- Activity ----------

def upsert_daily_activity(
    db: Session, user_id: int, activity: schemas.ActivityUpsert
) -> models.DailyActivity:
    """
    Crea o aggiorna il record di attività per un utente in una data.
    Questa è la stessa funzione che userà in futuro il webhook di Terra
    per scrivere i dati sincronizzati dallo smartwatch Zepp.
    """
    existing = (
        db.query(models.DailyActivity)
        .filter(
            models.DailyActivity.user_id == user_id,
            models.DailyActivity.data == activity.data,
        )
        .first()
    )

    if existing:
        existing.calorie_attive_bruciate = activity.calorie_attive_bruciate
        existing.passi = activity.passi
        existing.minuti_allenamento = activity.minuti_allenamento
        existing.fonte = activity.fonte
        db.commit()
        db.refresh(existing)
        return existing

    db_activity = models.DailyActivity(
        user_id=user_id,
        data=activity.data,
        calorie_attive_bruciate=activity.calorie_attive_bruciate,
        passi=activity.passi,
        minuti_allenamento=activity.minuti_allenamento,
        fonte=activity.fonte,
    )
    db.add(db_activity)
    db.commit()
    db.refresh(db_activity)
    return db_activity


# ---------- Meals ----------

def create_meal(db: Session, user_id: int, meal: schemas.MealCreate) -> models.Meal:
    db_meal = models.Meal(
        user_id=user_id,
        data=meal.data or date_type.today(),
        nome_alimento=meal.nome_alimento,
        calorie=meal.calorie,
        proteine_g=meal.proteine_g,
        carboidrati_g=meal.carboidrati_g,
        grassi_g=meal.grassi_g,
        fonte=meal.fonte,
    )
    db.add(db_meal)
    db.commit()
    db.refresh(db_meal)
    return db_meal


def list_meals_for_day(db: Session, user_id: int, day: date_type) -> list[models.Meal]:
    return (
        db.query(models.Meal)
        .filter(models.Meal.user_id == user_id, models.Meal.data == day)
        .order_by(models.Meal.orario)
        .all()
    )


# ---------- Bilancio calorico ----------

def get_daily_balance(db: Session, user_id: int, day: date_type) -> schemas.DailyBalance:
    user = get_user(db, user_id)
    if user is None:
        raise ValueError(f"Utente {user_id} non trovato")

    activity = (
        db.query(models.DailyActivity)
        .filter(models.DailyActivity.user_id == user_id, models.DailyActivity.data == day)
        .first()
    )
    calorie_bruciate = activity.calorie_attive_bruciate if activity else 0.0

    calorie_assunte = (
        db.query(func.coalesce(func.sum(models.Meal.calorie), 0.0))
        .filter(models.Meal.user_id == user_id, models.Meal.data == day)
        .scalar()
    )

    numero_pasti = (
        db.query(func.count(models.Meal.id))
        .filter(models.Meal.user_id == user_id, models.Meal.data == day)
        .scalar()
    )

    calorie_residue = (
        user.obiettivo_calorico_giornaliero + calorie_bruciate - calorie_assunte
    )

    return schemas.DailyBalance(
        user_id=user_id,
        data=day,
        obiettivo_calorico_giornaliero=user.obiettivo_calorico_giornaliero,
        calorie_attive_bruciate=calorie_bruciate,
        calorie_assunte=calorie_assunte,
        calorie_residue=calorie_residue,
        numero_pasti_registrati=numero_pasti,
    )
