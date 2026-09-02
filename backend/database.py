"""
Configurazione del database.

Di default usa SQLite (file locale, zero setup) tramite la variabile
DATABASE_URL. Per passare a Postgres in produzione basta impostare:

    DATABASE_URL=postgresql://user:password@host:5432/nutrition_coach

Lo schema (models.py) resta identico: SQLAlchemy astrae il dialetto.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./nutrition_coach.db")

# Alcuni provider (es. Neon, Heroku) forniscono la stringa di connessione con
# il prefisso "postgres://", non più accettato da SQLAlchemy 2.x, che richiede
# "postgresql://". Lo normalizziamo qui per evitare di doverlo ricordare ogni
# volta quando si imposta la variabile d'ambiente.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# connect_args serve solo per SQLite (permette l'uso multi-thread di FastAPI)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency FastAPI: fornisce una sessione DB per ogni richiesta."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
