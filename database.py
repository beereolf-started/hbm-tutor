# ╔══════════════════════════════════════════════════════════════╗
# ║  HBM РЕПЕТИТОР — database.py                                ║
# ║  Подключение к PostgreSQL через SQLAlchemy                   ║
# ╚══════════════════════════════════════════════════════════════╝

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Строка подключения: postgres://user:password@host:port/dbname
# Пароль подставь свой (тот, что вводишь в psql)
DATABASE_URL = "postgresql://postgres:hbm2024@127.0.0.1:5432/hbm"

engine = create_engine(DATABASE_URL, echo=False)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()


def get_db():
    """Генератор сессии для FastAPI Depends."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
