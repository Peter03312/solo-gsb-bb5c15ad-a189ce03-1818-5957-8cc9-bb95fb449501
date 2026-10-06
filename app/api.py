"""HTTP API（/api 前缀，JSON）。

约定：
- 成功：200/201 + JSON
- 业务校验失败：400 {"error": "中文原因", "details": ...}
- 资源不存在：404
- 交互文档：/api/docs（Swagger UI），/api/redoc
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import serializers as sz
from .database import get_db
from .errors import DomainError, NotFoundError
from .models import Artifact, Batch, EvaluationVersion, TransportNode
from . import services

router = APIRouter(prefix="/api", tags=["API"])


async def json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise DomainError("请求体必须是合法的 JSON（Content-Type: application/json）")
    if not isinstance(payload, dict):
        raise DomainError("请求体必须是 JSON 对象")
    return payload


# ---------- 批次 ----------

@router.get("/batches", summary="批次列表（含当前版本风险摘要）")
def list_batches(db: Session = Depends(get_db)):
    result = []
    for b in db.scalars(select(Batch).order_by(Batch.id)).all():
        result.append(sz.serialize_batch(b, services.batch_summary(db, b)))
    return result


@router.post("/batches", status_code=201, summary="创建批次")
async def create_batch(request: Request, db: Session = Depends(get_db)):
    payload = await json_body(request)
    batch = services.create_batch(db, payload)
    db.commit()
    db.refresh(batch)
    return sz.serialize_batch(batch)


@router.get("/batches/{batch_id}", summary="批次详情（含风险摘要）")
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = services.get_batch(db, batch_id)
    data = sz.serialize_batch(batch, services.batch_summary(db, batch))
    data["artifacts"] = [sz.serialize_artifact(a) for a in batch.artifacts]
    data["nodes"] = [sz.serialize_node(n) for n in batch.nodes]
    data["versions"] = [sz.serialize_version(v) for v in batch.versions]
    return data


# ---------- 文物 / 节点 ----------

@router.post("/batches/{batch_id}/artifacts", status_code=201, summary="登记文物（温湿度上下限、采样间隔）")
async def create_artifact(batch_id: int, request: Request, db: Session = Depends(get_db)):
    payload = await json_body(request)
    artifact = services.create_artifact(db, batch_id, payload)
    db.commit()
    return sz.serialize_artifact(artifact)


@router.get("/batches/{batch_id}/artifacts", summary="批次文物清单")
def list_artifacts(batch_id: int, db: Session = Depends(get_db)):
    batch = services.get_batch(db, batch_id)
    return [sz.serialize_artifact(a) for a in
            db.scalars(select(Artifact).where(Artifact.batch_id == batch.id)
                       .order_by(Artifact.id)).all()]


@router.post("/batches/{batch_id}/nodes", status_code=201, summary="新增有序运输节点")
async def create_node(batch_id: int, request: Request, db: Session = Depends(get_db)):
    payload = await json_body(request)
    node = services.create_node(db, batch_id, payload)
    db.commit()
    return sz.serialize_node(node)


@router.get("/batches/{batch_id}/nodes", summary="运输节点清单（按顺序）")
def list_nodes(batch_id: int, db: Session = Depends(get_db)):
    batch = services.get_batch(db, batch_id)
    return [sz.serialize_node(n) for n in batch.nodes]


# ---------- 版本与读数 ----------

@router.post("/batches/{batch_id}/readings", status_code=201,
             summary="提交读数：首次为初始版本，补录/更正生成新评估版本并重新核验")
async def ingest_readings(batch_id: int, request: Request, db: Session = Depends(get_db)):
    payload = await json_body(request)
    version = services.ingest_readings(db, batch_id, payload)
    db.commit()
    version = services.get_version(db, version.id)
    return {
        "version": sz.serialize_version(version),
        "events": [sz.serialize_event(e) for e in
                   sorted(version.events, key=lambda e: (e.artifact_id, e.metric, e.event_no))],
        "gaps": [sz.serialize_gap(g) for g in
                 sorted(version.gaps, key=lambda g: (g.artifact_id, g.metric, g.gap_start_ts))],
        "message": f"已生成评估版本 V{version.version_no}"
                   + ("（更正）" if version.change_type == "correction"
                      else "（补录）" if version.change_type == "supplement" else "（初始录入）"),
    }


@router.get("/batches/{batch_id}/versions", summary="批次的全部评估版本")
def list_versions(batch_id: int, db: Session = Depends(get_db)):
    batch = services.get_batch(db, batch_id)
    return [sz.serialize_version(v) for v in reversed(batch.versions)]


@router.get("/versions/{version_id}", summary="版本完整数据（读数/事件/缺测）")
def get_version(version_id: int, db: Session = Depends(get_db)):
    version = services.get_version(db, version_id)
    return sz.serialize_version(version, with_lists=True)


@router.get("/versions/{version_id}/readings", summary="版本读数明细")
def version_readings(version_id: int, db: Session = Depends(get_db)):
    version = services.get_version(db, version_id)
    return [sz.serialize_reading(r) for r in
            sorted(version.readings, key=lambda r: (r.artifact_id, r.metric, r.ts))]


@router.get("/batches/{batch_id}/versions/{version_no}/diff",
            summary="版本间差异（读数增/删/改，事件对比；默认与上一版对比）")
def version_diff(batch_id: int, version_no: int, base_no: int | None = None,
                 db: Session = Depends(get_db)):
    diff = services.version_diff(db, batch_id, version_no, base_no)
    return {
        "base_version": sz.serialize_version(diff["base"]),
        "target_version": sz.serialize_version(diff["new"]),
        "readings_added": [sz.serialize_reading(r) for r in diff["readings_added"]],
        "readings_removed": [sz.serialize_reading(r) for r in diff["readings_removed"]],
        "readings_changed": [
            {"base": sz.serialize_reading(old), "target": sz.serialize_reading(new)}
            for old, new in diff["readings_changed"]
        ],
        "events_added": [sz.serialize_event(e) for e in diff["events_added"]],
        "events_removed": [sz.serialize_event(e) for e in diff["events_removed"]],
        "events_common": [
            {"base": sz.serialize_event(o), "target": sz.serialize_event(n)}
            for o, n in diff["events_common"]
        ],
        "note": "新版本事件一律重新计算且不继承旧版关闭状态；关闭结论仅保存在其所属版本内。",
    }


# ---------- 风险事件 ----------

@router.get("/events/{event_id}", summary="风险事件详情")
def get_event(event_id: int, db: Session = Depends(get_db)):
    from .models import RiskEvent
    event = db.get(RiskEvent, event_id)
    if event is None:
        raise NotFoundError(f"风险事件 {event_id} 不存在")
    data = sz.serialize_event(event)
    data["readings"] = [sz.serialize_reading(r) for r in
                        sorted(event.readings, key=lambda r: r.ts)]
    return data


@router.post("/events/{event_id}/close", summary="关闭事件（必须提供处置说明 + 证据编号）")
async def close_event(event_id: int, request: Request, db: Session = Depends(get_db)):
    payload = await json_body(request)
    event = services.close_event(db, event_id, payload)
    db.commit()
    return {"message": "事件已关闭，关闭结论在该版本中永久保留可查",
            "event": sz.serialize_event(event)}
