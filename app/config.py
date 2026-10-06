"""应用配置。

所有配置均可通过环境变量覆盖，便于在 Docker / Compose 中调整。
"""
from __future__ import annotations

import os
from pathlib import Path

# SQLite 数据库文件路径（生产环境可通过 DATABASE_URL 切换其它数据库）
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'museum_transport.db'}")

# 是否在首次启动且数据库为空时写入演示批次（青铜鼎运输 B-2026-001）
SEED_DEMO = os.getenv("SEED_DEMO", "true").lower() in ("1", "true", "yes", "on")

# 应用监听配置（供 uvicorn / README 参考）
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
