"""数据库引擎、会话与建表入口。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from . import config

# 确保 SQLite 文件所在目录存在
if config.DATABASE_URL.startswith("sqlite:///"):
    db_path = config.DATABASE_URL.replace("sqlite:///", "", 1)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    config.DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {},
)

# SQLite 开启外键约束
if config.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True,
                            expire_on_commit=False)


def init_db() -> None:
    """创建全部表。"""
    from . import models  # noqa: F401  保证模型已注册

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务型会话上下文。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
