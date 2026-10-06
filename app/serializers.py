"""ORM 对象 -> API JSON 字典。"""
from __future__ import annotations

from typing import Any

from . import services
from .models import (
    Artifact, Batch, EvaluationVersion, MissingGap, Reading, RiskEvent, TransportNode,
)


def serialize_batch(b: Batch, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    data = {
        "id": b.id,
        "batch_no": b.batch_no,
        "name": b.name,
        "borrower": b.borrower,
        "description": b.description,
        "created_at": services.fmt_ts(b.created_at),
        "artifact_count": len(b.artifacts),
        "node_count": len(b.nodes),
        "version_count": len(b.versions),
    }
    if summary is not None:
        data["risk_summary"] = serialize_summary(summary)
    return data


def serialize_artifact(a: Artifact) -> dict[str, Any]:
    return {
        "id": a.id,
        "batch_id": a.batch_id,
        "code": a.code,
        "name": a.name,
        "temp_low": a.temp_low,
        "temp_high": a.temp_high,
        "humidity_low": a.humidity_low,
        "humidity_high": a.humidity_high,
        "sampling_interval_seconds": a.sampling_interval_seconds,
        "sampling_interval_human": services.interval_human(a.sampling_interval_seconds),
    }


def serialize_node(n: TransportNode) -> dict[str, Any]:
    return {
        "id": n.id,
        "batch_id": n.batch_id,
        "seq": n.seq,
        "name": n.name,
        "location": n.location,
        "planned_arrival": services.fmt_ts(n.planned_arrival),
    }


def serialize_reading(r: Reading) -> dict[str, Any]:
    return {
        "id": r.id,
        "version_id": r.version_id,
        "artifact_id": r.artifact_id,
        "node_id": r.node_id,
        "ts": services.fmt_ts(r.ts),
        "metric": r.metric,
        "metric_label": services.METRIC_LABELS[r.metric],
        "value": r.value,
        "source": r.source,
        "source_label": {"original": "原始", "supplement": "补录", "correction": "更正"}[r.source],
        "ref_reading_id": r.ref_reading_id,
        "event_id": r.event_id,
    }


def serialize_gap(g: MissingGap) -> dict[str, Any]:
    return {
        "id": g.id,
        "version_id": g.version_id,
        "artifact_id": g.artifact_id,
        "metric": g.metric,
        "metric_label": services.METRIC_LABELS[g.metric],
        "gap_start_ts": services.fmt_ts(g.gap_start_ts),
        "gap_end_ts": services.fmt_ts(g.gap_end_ts),
        "gap_seconds": g.gap_seconds,
        "gap_human": services.interval_human(g.gap_seconds),
        "interval_seconds": g.interval_seconds,
        "start_node_id": g.start_node_id,
        "end_node_id": g.end_node_id,
        "cross_node": g.cross_node,
        "interrupted_event": g.interrupted_event,
    }


def serialize_event(e: RiskEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "version_id": e.version_id,
        "artifact_id": e.artifact_id,
        "metric": e.metric,
        "metric_label": services.METRIC_LABELS[e.metric],
        "event_no": e.event_no,
        "start_ts": services.fmt_ts(e.start_ts),
        "end_ts": services.fmt_ts(e.end_ts),
        "start_node_id": e.start_node_id,
        "end_node_id": e.end_node_id,
        "start_node_name": e.start_node.name,
        "end_node_name": e.end_node.name,
        "peak_value": e.peak_value,
        "peak_direction": e.peak_direction,
        "peak_direction_label": "超上限" if e.peak_direction == "high" else "低于下限",
        "peak_deviation": round(e.peak_deviation, 4),
        "reading_count": e.reading_count,
        "cross_node": e.cross_node,
        "interrupted": e.interrupted,
        "status": e.status,
        "status_label": "已关闭" if e.status == "closed" else "待处置",
        "disposition": e.disposition,
        "evidence_no": e.evidence_no,
        "closed_by": e.closed_by,
        "closed_at": services.fmt_ts(e.closed_at),
    }


def serialize_version(v: EvaluationVersion, with_lists: bool = False) -> dict[str, Any]:
    data = {
        "id": v.id,
        "batch_id": v.batch_id,
        "version_no": v.version_no,
        "change_type": v.change_type,
        "change_type_label": {"initial": "初始录入", "supplement": "补录",
                              "correction": "更正"}[v.change_type],
        "remark": v.remark,
        "created_by": v.created_by,
        "created_at": services.fmt_ts(v.created_at),
        "is_current": v.is_current,
        "reading_count": len(v.readings),
        "event_open": sum(1 for e in v.events if e.status == "open"),
        "event_closed": sum(1 for e in v.events if e.status == "closed"),
        "gap_count": len(v.gaps),
    }
    if with_lists:
        data["readings"] = [serialize_reading(r) for r in
                            sorted(v.readings, key=lambda r: (r.artifact_id, r.metric, r.ts))]
        data["events"] = [serialize_event(e) for e in
                          sorted(v.events, key=lambda e: (e.artifact_id, e.metric, e.event_no))]
        data["gaps"] = [serialize_gap(g) for g in
                        sorted(v.gaps, key=lambda g: (g.artifact_id, g.metric, g.gap_start_ts))]
    return data


def serialize_summary(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "version_no": s["version_no"],
        "version_id": s["version_id"],
        "reading_total": s["reading_total"],
        "event_open": s["event_open"],
        "event_closed": s["event_closed"],
        "event_cross_node": s["event_cross_node"],
        "event_interrupted": s["event_interrupted"],
        "gap_total": s["gap_total"],
        "artifact_count": s["artifact_count"],
        "node_count": s["node_count"],
        "version_count": s["version_count"],
        "by_artifact": [
            {
                "artifact_id": row["artifact"].id,
                "artifact_code": row["artifact"].code,
                "artifact_name": row["artifact"].name,
                "reading_count": row["reading_count"],
                "event_open": row["event_open"],
                "event_closed": row["event_closed"],
                "gap_count": row["gap_count"],
            } for row in s["by_artifact"]
        ],
        "open_events": [serialize_event(e) for e in s["open_events"]],
    }
