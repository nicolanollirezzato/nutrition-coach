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


def update_meal(db: Session, meal_id: int, update: schemas.MealUpdate) -> models.Meal | None:
    """
    Aggiorna un pasto già registrato (es. dopo aver ricalcolato le calorie
    con dati più precisi), invece di crearne uno nuovo che verrebbe
    conteggiato due volte nel bilancio giornaliero.
    """
    meal = db.query(models.Meal).filter(models.Meal.id == meal_id).first()
    if meal is None:
        return None

    if update.nome_alimento is not None:
        meal.nome_alimento = update.nome_alimento
    if update.calorie is not None:
        meal.calorie = update.calorie
    if update.proteine_g is not None:
        meal.proteine_g = update.proteine_g
    if update.carboidrati_g is not None:
        meal.carboidrati_g = update.carboidrati_g
    if update.grassi_g is not None:
        meal.grassi_g = update.grassi_g

    db.commit()
    db.refresh(meal)
    return meal


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

    proteine_assunte, carboidrati_assunti, grassi_assunti = (
        db.query(
            func.coalesce(func.sum(models.Meal.proteine_g), 0.0),
            func.coalesce(func.sum(models.Meal.carboidrati_g), 0.0),
            func.coalesce(func.sum(models.Meal.grassi_g), 0.0),
        )
        .filter(models.Meal.user_id == user_id, models.Meal.data == day)
        .first()
    )

    numero_pasti = (
        db.query(func.count(models.Meal.id))
        .filter(models.Meal.user_id == user_id, models.Meal.data == day)
        .scalar()
    )

    calorie_residue = (
        user.obiettivo_calorico_giornaliero + calorie_bruciate - calorie_assunte
    )

    # I target di macronutrienti vivono nel piano alimentare, non nell'utente:
    # se non esiste un piano, i campi restano None (nessun target da rispettare).
    piano = get_meal_plan(db, user_id)

    proteine_target = piano.proteine_target_g if piano else None
    carboidrati_target = piano.carboidrati_target_g if piano else None
    grassi_target = piano.grassi_target_g if piano else None

    return schemas.DailyBalance(
        user_id=user_id,
        data=day,
        obiettivo_calorico_giornaliero=user.obiettivo_calorico_giornaliero,
        calorie_attive_bruciate=calorie_bruciate,
        calorie_assunte=calorie_assunte,
        calorie_residue=calorie_residue,
        numero_pasti_registrati=numero_pasti,
        proteine_target_g=proteine_target,
        proteine_assunte_g=proteine_assunte,
        proteine_residue_g=(proteine_target - proteine_assunte) if piano else None,
        carboidrati_target_g=carboidrati_target,
        carboidrati_assunti_g=carboidrati_assunti,
        carboidrati_residui_g=(carboidrati_target - carboidrati_assunti) if piano else None,
        grassi_target_g=grassi_target,
        grassi_assunti_g=grassi_assunti,
        grassi_residui_g=(grassi_target - grassi_assunti) if piano else None,
    )



# ---------- Peso ----------

def upsert_weight_entry(
    db: Session, user_id: int, entry: schemas.WeightUpsert
) -> models.WeightEntry:
    """Crea o aggiorna la pesata di un utente in una data (un valore per giorno)."""
    giorno = entry.data or date_type.today()
    existing = (
        db.query(models.WeightEntry)
        .filter(models.WeightEntry.user_id == user_id, models.WeightEntry.data == giorno)
        .first()
    )

    if existing:
        existing.peso_kg = entry.peso_kg
        existing.note = entry.note
        db.commit()
        db.refresh(existing)
        return existing

    db_entry = models.WeightEntry(
        user_id=user_id, data=giorno, peso_kg=entry.peso_kg, note=entry.note
    )
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return db_entry


def list_weight_entries(
    db: Session, user_id: int, since: date_type
) -> list[models.WeightEntry]:
    return (
        db.query(models.WeightEntry)
        .filter(models.WeightEntry.user_id == user_id, models.WeightEntry.data >= since)
        .order_by(models.WeightEntry.data)
        .all()
    )


# ---------- Piano alimentare ----------

def get_meal_plan(db: Session, user_id: int) -> models.MealPlan | None:
    return db.query(models.MealPlan).filter(models.MealPlan.user_id == user_id).first()


def upsert_meal_plan(
    db: Session, user_id: int, plan: schemas.MealPlanUpsert
) -> models.MealPlan:
    """
    Crea o aggiorna il piano alimentare attivo. Sincronizza anche
    l'obiettivo calorico giornaliero dell'utente, così get_daily_balance
    riflette subito il nuovo piano senza bisogno di altre modifiche.
    """
    existing = get_meal_plan(db, user_id)

    if existing:
        existing.obiettivo = plan.obiettivo
        existing.calorie_target = plan.calorie_target
        existing.proteine_target_g = plan.proteine_target_g
        existing.carboidrati_target_g = plan.carboidrati_target_g
        existing.grassi_target_g = plan.grassi_target_g
        existing.pasti_suggeriti = plan.pasti_suggeriti
        existing.note = plan.note
        db_plan = existing
    else:
        db_plan = models.MealPlan(
            user_id=user_id,
            obiettivo=plan.obiettivo,
            calorie_target=plan.calorie_target,
            proteine_target_g=plan.proteine_target_g,
            carboidrati_target_g=plan.carboidrati_target_g,
            grassi_target_g=plan.grassi_target_g,
            pasti_suggeriti=plan.pasti_suggeriti,
            note=plan.note,
        )
        db.add(db_plan)

    user = get_user(db, user_id)
    if user is not None:
        user.obiettivo_calorico_giornaliero = plan.calorie_target

    db.commit()
    db.refresh(db_plan)
    return db_plan
