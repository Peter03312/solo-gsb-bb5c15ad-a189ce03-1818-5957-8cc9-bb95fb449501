"""中文 Web 页面（服务端渲染）。

页面：
- GET  /                      首页：全部批次与风险摘要
- GET  /batches/{id}          批次详情：摘要、文物、节点、版本、当前事件、读数录入
- POST /batches               新建批次
- POST /batches/{id}/artifacts 登记文物
- POST /batches/{id}/nodes     新增节点
- POST /batches/{id}/readings  提交读数（textarea 中的 JSON）
- GET  /versions/{id}         版本详情（读数/事件/缺测，旧版只读）
- GET  /batches/{id}/diff     版本差异
- POST /events/{id}/close     关闭事件（处置说明 + 证据编号）
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import serializers as sz
from . import services
from .database import get_db
from .errors import DomainError, NotFoundError
from .models import Artifact, Batch, RiskEvent

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["Web"])


def _ctx(request: Request, **kwargs: Any) -> dict[str, Any]:
    ctx = {
        "request": request,
        "METRIC_LABELS": services.METRIC_LABELS,
        "fmt_ts": services.fmt_ts,
        "interval_human": services.interval_human,
        "artifact_map": {},
    }
    ctx.update(kwargs)
    return ctx


def _artifact_map(db: Session, batch_id: int) -> dict[int, Any]:
    return {a.id: a for a in db.scalars(
        select(Artifact).where(Artifact.batch_id == batch_id)).all()}


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(Batch).order_by(Batch.id)).all()
    rows = [(b, services.batch_summary(db, b)) for b in batches]
    return templates.TemplateResponse("index.html", _ctx(request, rows=rows))


@router.post("/batches")
def web_create_batch(
        request: Request,
        batch_no: str = Form(...), name: str = Form(...),
        borrower: str = Form(""), description: str = Form(""),
        db: Session = Depends(get_db)):
    try:
        batch = services.create_batch(db, {
            "batch_no": batch_no, "name": name,
            "borrower": borrower, "description": description})
        db.commit()
    except DomainError as exc:
        batches = db.scalars(select(Batch).order_by(Batch.id)).all()
        rows = [(b, services.batch_summary(db, b)) for b in batches]
        return templates.TemplateResponse(
            "index.html", _ctx(request, rows=rows, error=str(exc)), status_code=400)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int, request: Request,
                 error: str | None = None, ok: str | None = None,
                 db: Session = Depends(get_db)):
    batch = services.get_batch(db, batch_id)
    summary = services.batch_summary(db, batch)
    return templates.TemplateResponse(
        "batch.html",
        _ctx(request, batch=batch, summary=summary, error=error, ok=ok,
             artifact_map=_artifact_map(db, batch_id)))


@router.post("/batches/{batch_id}/artifacts")
def web_create_artifact(
        batch_id: int, request: Request,
        code: str = Form(...), name: str = Form(...),
        temp_low: str = Form(...), temp_high: str = Form(...),
        humidity_low: str = Form(...), humidity_high: str = Form(...),
        sampling_interval_seconds: str = Form(...),
        db: Session = Depends(get_db)):
    try:
        services.create_artifact(db, batch_id, {
            "code": code, "name": name,
            "temp_low": _to_number(temp_low, "温度下限"),
            "temp_high": _to_number(temp_high, "温度上限"),
            "humidity_low": _to_number(humidity_low, "湿度下限"),
            "humidity_high": _to_number(humidity_high, "湿度上限"),
            "sampling_interval_seconds": _to_int(sampling_interval_seconds, "约定采样间隔"),
        })
        db.commit()
    except DomainError as exc:
        return _batch_error(request, db, batch_id, str(exc))
    return RedirectResponse(f"/batches/{batch_id}?ok=文物已登记", status_code=303)


@router.post("/batches/{batch_id}/nodes")
def web_create_node(
        batch_id: int, request: Request,
        seq: str = Form(...), name: str = Form(...), location: str = Form(""),
        planned_arrival: str = Form(""), db: Session = Depends(get_db)):
    try:
        payload: dict[str, Any] = {
            "seq": _to_int(seq, "节点顺序"), "name": name, "location": location}
        if planned_arrival.strip():
            payload["planned_arrival"] = planned_arrival.strip()
        services.create_node(db, batch_id, payload)
        db.commit()
    except DomainError as exc:
        return _batch_error(request, db, batch_id, str(exc))
    return RedirectResponse(f"/batches/{batch_id}?ok=节点已新增", status_code=303)


@router.post("/batches/{batch_id}/readings")
async def web_ingest(batch_id: int, request: Request, readings_json: str = Form(...),
                     created_by: str = Form("工作人员"), remark: str = Form(""),
                     db: Session = Depends(get_db)):
    try:
        try:
            data = json.loads(readings_json)
        except json.JSONDecodeError as exc:
            raise DomainError(f"读数 JSON 解析失败：{exc.msg}（第 {exc.lineno} 行附近）")
        if not isinstance(data, list):
            raise DomainError("读数必须是 JSON 数组，例如 [{\"artifact_id\":1,...}]")
        version = services.ingest_readings(db, batch_id, {
            "readings": data, "created_by": created_by, "remark": remark})
        db.commit()
        label = {"initial": "初始录入", "supplement": "补录", "correction": "更正"}[version.change_type]
        ok = f"已生成评估版本 V{version.version_no}（{label}），并完成按时间核验"
    except DomainError as exc:
        return _batch_error(request, db, batch_id, str(exc))
    return RedirectResponse(
        f"/batches/{batch_id}?ok={ok}", status_code=303)


@router.get("/versions/{version_id}", response_class=HTMLResponse)
def version_detail(version_id: int, request: Request, db: Session = Depends(get_db)):
    version = services.get_version(db, version_id)
    batch = services.get_batch(db, version.batch_id)
    readings = sorted(version.readings, key=lambda r: (r.artifact_id, r.metric, r.ts))
    events = sorted(version.events, key=lambda e: (e.artifact_id, e.metric, e.event_no))
    gaps = sorted(version.gaps, key=lambda g: (g.artifact_id, g.metric, g.gap_start_ts))
    return templates.TemplateResponse(
        "version.html",
        _ctx(request, batch=batch, version=version, readings=readings,
             events=events, gaps=gaps,
             artifact_map=_artifact_map(db, batch.id),
             node_map={n.id: n for n in batch.nodes}))


@router.get("/batches/{batch_id}/diff", response_class=HTMLResponse)
def batch_diff(batch_id: int, request: Request, base_no: str = "0",
               target_no: str = "0", db: Session = Depends(get_db)):
    batch = services.get_batch(db, batch_id)
    versions = list(batch.versions)
    error = None
    diff = None
    if len(versions) >= 2:
        try:
            base_n = int(base_no) if int(base_no) > 0 else versions[-2].version_no
            target_n = int(target_no) if int(target_no) > 0 else versions[-1].version_no
            diff = services.version_diff(db, batch_id, target_n, base_n)
        except (DomainError, NotFoundError, ValueError) as exc:
            error = str(exc)
    return templates.TemplateResponse(
        "diff.html", _ctx(request, batch=batch, versions=versions, diff=diff, error=error,
                          artifact_map=_artifact_map(db, batch_id),
                          node_map={n.id: n for n in batch.nodes}))


@router.post("/events/{event_id}/close")
def web_close_event(event_id: int, request: Request,
                    disposition: str = Form(...), evidence_no: str = Form(...),
                    closed_by: str = Form("工作人员"), db: Session = Depends(get_db)):
    event = db.get(RiskEvent, event_id)
    if event is None:
        raise NotFoundError(f"风险事件 {event_id} 不存在")
    try:
        services.close_event(db, event_id, {
            "disposition": disposition, "evidence_no": evidence_no, "closed_by": closed_by})
        db.commit()
    except DomainError as exc:
        version = services.get_version(db, event.version_id)
        batch = services.get_batch(db, version.batch_id)
        readings = sorted(version.readings, key=lambda r: (r.artifact_id, r.metric, r.ts))
        events = sorted(version.events, key=lambda e: (e.artifact_id, e.metric, e.event_no))
        gaps = sorted(version.gaps, key=lambda g: (g.artifact_id, g.metric, g.gap_start_ts))
        return templates.TemplateResponse(
            "version.html",
            _ctx(request, batch=batch, version=version, readings=readings, events=events,
                 gaps=gaps, close_error=(str(exc), event_id),
                 artifact_map=_artifact_map(db, batch.id),
                 node_map={n.id: n for n in batch.nodes}),
            status_code=400)
    return RedirectResponse(f"/versions/{event.version_id}?ok=事件已关闭", status_code=303)


# ---------- 辅助 ----------

def _to_number(raw: str, field: str) -> float:
    raw = (raw or "").strip()
    try:
        return float(raw)
    except ValueError:
        raise DomainError(f"{field}必须是数值，收到：{raw!r}")


def _to_int(raw: str, field: str) -> int:
    raw = (raw or "").strip()
    try:
        value = int(raw)
    except ValueError:
        raise DomainError(f"{field}必须是整数，收到：{raw!r}")
    return value


def _batch_error(request: Request, db: Session, batch_id: int, message: str):
    batch = services.get_batch(db, batch_id)
    summary = services.batch_summary(db, batch)
    return templates.TemplateResponse(
        "batch.html",
        _ctx(request, batch=batch, summary=summary, error=message,
             artifact_map=_artifact_map(db, batch_id)), status_code=400)
