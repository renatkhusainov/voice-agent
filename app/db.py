# app/db.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

# hide_parameters: a failed INSERT otherwise raises an error that embeds the bound
# values ("[parameters: {'text': <what the caller said>}]"), which lands in logs.
engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
