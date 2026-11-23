"""跨服务共享的数据库小工具，避免 service 模块之间循环导入。"""
from fastapi import HTTPException
from sqlalchemy import select

from src.models.tables import audit
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action,
                                       reference=str(reference), created_at=utc_time()))


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)
