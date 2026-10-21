from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# Three tables live outside the ORM models and are written with raw SQL:
# the 24h-window tracker, the template-tap context, and the per-referral
# conversation thread. On the live box they were created by hand. A fresh
# clone (or a fresh test database) has none of them, and every relay then
# dies on "no such table". IF NOT EXISTS keeps the live schema untouched.
RAW_TABLES = [
    "CREATE TABLE IF NOT EXISTS wa_windows ("
    " wa_number TEXT PRIMARY KEY,"
    " last_inbound_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS template_context ("
    " wa_number TEXT PRIMARY KEY,"
    " referral_id INTEGER,"
    " sent_at TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS referral_messages ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " referral_id INTEGER NOT NULL,"
    " direction TEXT NOT NULL,"
    " from_number TEXT,"
    " body TEXT,"
    " created_at TIMESTAMP,"
    " delivered INTEGER NOT NULL DEFAULT 1)",
]


async def ensure_raw_tables() -> None:
    async with engine.begin() as conn:
        for ddl in RAW_TABLES:
            await conn.execute(text(ddl))


async def get_session():
    async with SessionLocal() as session:
        yield session
