# backend/app/core/database.py
import logging
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

logger = logging.getLogger(__name__)

# Create the Async Engine using the DATABASE_URL_ASYNC property
engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    future=True,
    pool_pre_ping=True,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy 2.0 models."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, Any]:
    """Dependency for providing async database sessions in FastAPI routes."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Healthcheck helper to verify database connectivity for health endpoint."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return False