"""核心业务服务。

包含：
1. 批次 / 文物 / 节点维护与严格入参校验（重复时间戳、无序上下限、非数值读数均拒绝并说明原因）
2. 读数录入：首次为初始版本，补录/更正一律生成新的不可变评估版本
3. 按时间核验：同一文物同一指标的连续越限读数合并为风险事件（跨节点自动合并）；
   相邻读数间隔 > 约定采样间隔时标出缺测区间并中断当前事件
4. 事件关闭：必须填写处置说明与证据编号；新版事件不继承旧版关闭状态
5. 版本间差异：新增/移除/更正读数与事件对比
6. 批次风险摘要
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError, NotFoundError
from .models import (
    Artifact, Batch, EvaluationVersion, MissingGap, Reading, RiskEvent, TransportNode,
)

METRIC_TEMPERATURE = "temperature"
METRIC_HUMIDITY = "humidity"
METRICS = (METRIC_TEMPERATURE, METRIC_HUMIDITY)
METRIC_LABELS = {METRIC_TEMPERATURE: "温度(℃)", METRIC_HUMIDITY: "湿度(%RH)"}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_ts(value: Any, field: str = "时间戳") -> datetime:
    """解析 ISO 8601 时间戳；无时区按 UTC 处理；拒绝非法输入。"""
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field}格式无效：必须是 ISO 8601 字符串，例如 2026-10-01T08:00:00Z")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError(
            f"{field}格式无效：{value!r} 不是合法的 ISO 8601 时间，"
            "例如 2026-10-01T08:00:00Z 或 2026-10-01T16:00:00+08:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def require_text(value: Any, field: str, max_len: int = 200) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field}不能为空，且必须是字符串")
    text = value.strip()
    if len(text) > max_len:
        raise DomainError(f"{field}长度不能超过 {max_len} 个字符")
    return text


def parse_number(value: Any, field: str) -> float:
    """严格数值校验：布尔、NaN、Inf、数字字符串等一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(f"{field}必须是 JSON 数值（数字），收到的是 {type(value).__name__}：{value!r}")
    f = float(value)
    if f != f or f in (float("inf"), float("-inf")):
        raise DomainError(f"{field}不能为 NaN 或无穷大")
    return f


def parse_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        # 允许 10.0 这种整值浮点
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise DomainError(f"{field}必须是正整数（秒），收到：{value!r}")
    if value <= 0:
        raise DomainError(f"{field}必须大于 0，收到：{value}")
    return value


def get_batch(db: Session, batch_id: int) -> Batch:
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise NotFoundError(f"批次 {batch_id} 不存在")
    return batch


def get_artifact(db: Session, artifact_id: int, batch_id: int | None = None) -> Artifact:
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or (batch_id is not None and artifact.batch_id != batch_id):
        raise NotFoundError(f"文物 {artifact_id} 不存在" +
                            (f"或不属于批次 {batch_id}" if batch_id else ""))
    return artifact


def get_node(db: Session, node_id: int, batch_id: int | None = None) -> TransportNode:
    node = db.get(TransportNode, node_id)
    if node is None or (batch_id is not None and node.batch_id != batch_id):
        raise NotFoundError(f"运输节点 {node_id} 不存在" +
                            (f"或不属于批次 {batch_id}" if batch_id else ""))
    return node


def get_version(db: Session, version_id: int, batch_id: int | None = None) -> EvaluationVersion:
    version = db.get(EvaluationVersion, version_id)
    if version is None or (batch_id is not None and version.batch_id != batch_id):
        raise NotFoundError(f"评估版本 {version_id} 不存在" +
                            (f"或不属于批次 {batch_id}" if batch_id else ""))
    return version


def current_version(db: Session, batch_id: int) -> EvaluationVersion | None:
    return db.scalar(
        select(EvaluationVersion)
        .where(EvaluationVersion.batch_id == batch_id, EvaluationVersion.is_current.is_(True))
        .order_by(EvaluationVersion.version_no.desc()))


def interval_human(seconds: int) -> str:
    """把秒数渲染成易读中文。"""
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m} 分" + (f"{s} 秒" if s else "")
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h} 小时" + (f"{m} 分" if m else "")


# ---------------------------------------------------------------------------
# 批次 / 文物 / 节点
# ---------------------------------------------------------------------------

def create_batch(db: Session, data: dict[str, Any]) -> Batch:
    batch_no = require_text(data.get("batch_no"), "批次编号", 64)
    name = require_text(data.get("name"), "批次名称", 200)
    if db.scalar(select(Batch).where(Batch.batch_no == batch_no)):
        raise DomainError(f"批次编号 {batch_no!r} 已存在")
    batch = Batch(
        batch_no=batch_no, name=name,
        borrower=require_text(data.get("borrower") or "未填写借展方", "借展方", 200),
        description=(data.get("description") or "").strip()[:2000],
    )
    db.add(batch)
    db.flush()
    return batch


def create_artifact(db: Session, batch_id: int, data: dict[str, Any]) -> Artifact:
    get_batch(db, batch_id)
    code = require_text(data.get("code"), "文物编号", 64)
    name = require_text(data.get("name"), "文物名称", 200)
    temp_low = parse_number(data.get("temp_low"), "温度下限")
    temp_high = parse_number(data.get("temp_high"), "温度上限")
    humidity_low = parse_number(data.get("humidity_low"), "湿度下限")
    humidity_high = parse_number(data.get("humidity_high"), "湿度上限")
    sampling = parse_positive_int(data.get("sampling_interval_seconds"), "约定采样间隔")

    # 无序上下限拒绝保存并说明原因
    if temp_low >= temp_high:
        raise DomainError(f"温度上下限非法：下限 {temp_low} 必须严格小于上限 {temp_high}")
    if humidity_low >= humidity_high:
        raise DomainError(f"湿度上下限非法：下限 {humidity_low} 必须严格小于上限 {humidity_high}")
    if not -100 <= humidity_low <= humidity_high <= 100:
        raise DomainError("湿度上下限应落在 -100~100(%RH) 的合理范围内")
    if db.scalar(select(Artifact).where(Artifact.batch_id == batch_id, Artifact.code == code)):
        raise DomainError(f"文物编号 {code!r} 在该批次中已存在")

    artifact = Artifact(
        batch_id=batch_id, code=code, name=name,
        temp_low=temp_low, temp_high=temp_high,
        humidity_low=humidity_low, humidity_high=humidity_high,
        sampling_interval_seconds=sampling,
    )
    db.add(artifact)
    db.flush()
    return artifact


def create_node(db: Session, batch_id: int, data: dict[str, Any]) -> TransportNode:
    get_batch(db, batch_id)
    name = require_text(data.get("name"), "节点名称", 200)
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        raise DomainError("节点顺序 seq 必须是正整数")
    if db.scalar(select(TransportNode).where(
            TransportNode.batch_id == batch_id, TransportNode.seq == seq)):
        raise DomainError(f"节点顺序 {seq} 在该批次中已被占用")
    node = TransportNode(
        batch_id=batch_id, seq=seq, name=name,
        location=(data.get("location") or "").strip()[:200],
        planned_arrival=parse_ts(data["planned_arrival"], "计划到达时间")
        if data.get("planned_arrival") else None,
    )
    db.add(node)
    db.flush()
    return node


# ---------------------------------------------------------------------------
# 读数录入（生成新版本）
# ---------------------------------------------------------------------------

@dataclass
class ReadingItem:
    artifact_id: int
    node_id: int
    ts: datetime
    metric: str
    value: float
    source: str
    ref_reading_id: int | None = None


def _resolve_reading_items(
        db: Session, batch: Batch, raw_items: list[Any]) -> list[ReadingItem]:
    """解析并校验本次提交的读数（不做重复校验，重复在外层统一处理）。"""
    artifacts = {a.id: a for a in db.scalars(
        select(Artifact).where(Artifact.batch_id == batch.id)).all()}
    nodes = {n.id: n for n in db.scalars(
        select(TransportNode).where(TransportNode.batch_id == batch.id)).all()}

    items: list[ReadingItem] = []
    for idx, raw in enumerate(raw_items, start=1):
        where = f"第 {idx} 条读数"
        if not isinstance(raw, dict):
            raise DomainError(f"{where}：必须是 JSON 对象，包含 artifact_id/node_id/ts/metric/value")

        artifact_id = raw.get("artifact_id")
        node_id = raw.get("node_id")
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int):
            raise DomainError(f"{where}：artifact_id 必须是文物整数 ID")
        if isinstance(node_id, bool) or not isinstance(node_id, int):
            raise DomainError(f"{where}：node_id 必须是节点整数 ID")
        if artifact_id not in artifacts:
            raise DomainError(f"{where}：文物 {artifact_id} 不存在或不属于批次 {batch.batch_no}")
        if node_id not in nodes:
            raise DomainError(f"{where}：运输节点 {node_id} 不存在或不属于批次 {batch.batch_no}")

        ts = parse_ts(raw.get("ts"), f"{where} 的时间戳")
        metric = raw.get("metric")
        if metric not in METRICS:
            raise DomainError(f"{where}：指标 metric 只能是 {METRIC_TEMPERATURE!r}（温度）"
                              f"或 {METRIC_HUMIDITY!r}（湿度），收到 {metric!r}")
        value = parse_number(raw.get("value"), f"{where} 的读数 value")

        source = (raw.get("source") or "original")
        if source not in ("original", "supplement", "correction"):
            raise DomainError(f"{where}：source 只能是 original/supplement/correction")

        items.append(ReadingItem(
            artifact_id=artifact_id, node_id=node_id, ts=ts,
            metric=metric, value=value, source=source))
    return items


def ingest_readings(
        db: Session, batch_id: int, payload: dict[str, Any]) -> EvaluationVersion:
    """创建一个新评估版本并重新核验。

    - 首批读数 -> 初始版本（change_type=initial）
    - 之后每次提交都生成新版本；按与上一版的差异自动判定为「补录 supplement」
      或「更正 correction」（同时存在时记为 correction）
    - 严格拒绝：重复时间戳（同版本内同文物同指标）、非数值、非法时间、越界引用
    """
    batch = get_batch(db, batch_id)
    raw_items = payload.get("readings")
    if not isinstance(raw_items, list) or not raw_items:
        raise DomainError("readings 必须是非空数组，每条含 artifact_id/node_id/ts/metric/value")

    items = _resolve_reading_items(db, batch, raw_items)

    # 本次提交内部重复时间戳检查（同文物 + 同指标 + 同 ts；节点不同也不允许）
    seen: set[tuple[int, str, datetime]] = set()
    for item in items:
        key = (item.artifact_id, item.metric, item.ts)
        if key in seen:
            raise DomainError(
                f"重复时间戳：文物 {item.artifact_id} 的{METRIC_LABELS[item.metric]}"
                f"在 {fmt_ts(item.ts)} 出现了多条读数；同一文物同一指标同一时间点只允许一条")
        seen.add(key)

    prev = current_version(db, batch_id)

    # 复制上一版读数作为新版本基底（旧版本保持不变，可查）
    base_by_key: dict[tuple[int, str, datetime], Reading] = {}
    merged: dict[tuple[int, str, datetime], ReadingItem] = {}
    if prev is not None:
        for r in prev.readings:
            key = (r.artifact_id, r.metric, r.ts)
            base_by_key[key] = r
            merged[key] = ReadingItem(
                artifact_id=r.artifact_id, node_id=r.node_id, ts=r.ts,
                metric=r.metric, value=r.value, source=r.source)

    change_type = "initial" if prev is None else "supplement"
    has_correction = False
    for item in items:
        key = (item.artifact_id, item.metric, item.ts)
        old = base_by_key.get(key)
        if old is not None:
            if old.node_id == item.node_id and old.value == item.value \
                    and old.source == item.source:
                raise DomainError(
                    f"重复读数：文物 {item.artifact_id} 的{METRIC_LABELS[item.metric]}"
                    f"在 {fmt_ts(item.ts)} 的读数（节点 {item.node_id}，值 {item.value}）"
                    "与上一版本完全相同，无需重复提交")
            # 同时间戳再次出现 = 更正
            has_correction = True
            item.source = "correction"
            item.ref_reading_id = old.id
        else:
            if prev is not None and item.source == "original":
                item.source = "supplement"
        merged[key] = item

    if prev is not None:
        change_type = "correction" if has_correction else "supplement"
        prev.is_current = False

    version_no = (prev.version_no + 1) if prev else 1
    version = EvaluationVersion(
        batch_id=batch_id, version_no=version_no, change_type=change_type,
        remark=(payload.get("remark") or "").strip()[:1000],
        created_by=require_text(payload.get("created_by") or "工作人员", "提交人", 100),
        is_current=True,
    )
    db.add(version)
    db.flush()

    created: list[Reading] = []
    for item in sorted(merged.values(), key=lambda x: (x.artifact_id, x.metric, x.ts)):
        reading = Reading(
            version_id=version.id, artifact_id=item.artifact_id, node_id=item.node_id,
            ts=item.ts, metric=item.metric, value=item.value, source=item.source,
            ref_reading_id=item.ref_reading_id,
        )
        db.add(reading)
        created.append(reading)
    db.flush()

    evaluate(db, version, created, artifacts_map=None)
    return version


# ---------------------------------------------------------------------------
# 按时间核验：缺测区间 + 风险事件（跨节点合并、缺测中断）
# ---------------------------------------------------------------------------

def _violation(value: float, low: float, high: float) -> tuple[bool, str, float]:
    """返回 (是否越限, 越限方向, 超出量)。"""
    if value > high:
        return True, "high", value - high
    if value < low:
        return True, "low", low - value
    return False, "", 0.0


def evaluate(db: Session, version: EvaluationVersion,
             readings: Iterable[Reading], artifacts_map=None) -> None:
    """对整个版本按 (文物, 指标) 分组、按时间排序后核验。

    规则（严格按需求）：
    - 连续越限读数合并为同一个风险事件，即使相邻读数位于不同运输节点（跨节点合并）；
    - 任一相邻读数间隔 > 该文物约定采样间隔，立即记录一条缺测区间；
      若此前存在进行中的越限事件，事件在缺口前最后一条读数处中断（标记 interrupted），
      缺口之后再次越限则另起新事件；
    - 回到正常区间同样结束当前事件（自然结束，不算中断）。
    """
    readings = list(readings)
    artifact_ids = {r.artifact_id for r in readings}
    artifacts = artifacts_map or {a.id: a for a in db.scalars(
        select(Artifact).where(Artifact.id.in_(artifact_ids))).all()}

    groups: dict[tuple[int, str], list[Reading]] = {}
    for r in readings:
        groups.setdefault((r.artifact_id, r.metric), []).append(r)

    for (artifact_id, metric), group in sorted(groups.items()):
        artifact = artifacts[artifact_id]
        if metric == METRIC_TEMPERATURE:
            low, high = artifact.temp_low, artifact.temp_high
        else:
            low, high = artifact.humidity_low, artifact.humidity_high
        interval = timedelta(seconds=artifact.sampling_interval_seconds)

        group.sort(key=lambda r: r.ts)
        event_no = 0
        current: RiskEvent | None = None

        def flush_event() -> None:
            nonlocal current
            if current is not None:
                db.flush()
                current = None

        prev_r: Reading | None = None
        for r in group:
            if prev_r is not None:
                delta = r.ts - prev_r.ts
                if delta > interval:
                    # 缺测区间：(上一条读数时间, 本条读数时间)
                    cross = prev_r.node_id != r.node_id
                    gap = MissingGap(
                        version_id=version.id, artifact_id=artifact_id, metric=metric,
                        gap_start_ts=prev_r.ts, gap_end_ts=r.ts,
                        gap_seconds=int(delta.total_seconds()),
                        interval_seconds=artifact.sampling_interval_seconds,
                        start_node_id=prev_r.node_id, end_node_id=r.node_id,
                        cross_node=cross,
                        interrupted_event=current is not None,
                    )
                    db.add(gap)
                    if current is not None:
                        # 缺测中断当前越限事件
                        current.interrupted = True
                        current.end_ts = prev_r.ts
                        current.end_node_id = prev_r.node_id
                        current.cross_node = current.cross_node or cross
                        flush_event()

            bad, direction, deviation = _violation(r.value, low, high)
            if bad:
                if current is None:
                    event_no += 1
                    current = RiskEvent(
                        version_id=version.id, artifact_id=artifact_id, metric=metric,
                        event_no=event_no,
                        start_ts=r.ts, end_ts=r.ts,
                        start_node_id=r.node_id, end_node_id=r.node_id,
                        peak_value=r.value, peak_direction=direction,
                        peak_deviation=deviation, reading_count=1,
                        cross_node=False, interrupted=False, status="open")
                    db.add(current)
                    db.flush()
                else:
                    current.reading_count += 1
                    current.end_ts = r.ts
                    current.end_node_id = r.node_id
                    if r.node_id != current.start_node_id:
                        current.cross_node = True
                    if deviation > current.peak_deviation:
                        current.peak_value = r.value
                        current.peak_direction = direction
                        current.peak_deviation = deviation
                r.event_id = current.id
            else:
                # 读数回到允许范围：事件自然结束
                flush_event()
            prev_r = r

        flush_event()

    db.flush()


# ---------------------------------------------------------------------------
# 事件关闭
# ---------------------------------------------------------------------------

def close_event(db: Session, event_id: int, data: dict[str, Any]) -> RiskEvent:
    event = db.get(RiskEvent, event_id)
    if event is None:
        raise NotFoundError(f"风险事件 {event_id} 不存在")
    if event.status == "closed":
        raise DomainError("该事件已关闭，关闭结论保持可查，不可重复关闭；"
                          "如读数已更正，请基于新版本事件重新处置")
    disposition = require_text(data.get("disposition"), "处置说明", 2000)
    evidence_no = require_text(data.get("evidence_no"), "证据编号", 200)
    closed_by = require_text(data.get("closed_by") or "工作人员", "处置人", 100)

    event.status = "closed"
    event.disposition = disposition
    event.evidence_no = evidence_no
    event.closed_by = closed_by
    event.closed_at = datetime.now(timezone.utc)
    db.flush()
    return event


# ---------------------------------------------------------------------------
# 版本差异
# ---------------------------------------------------------------------------

def _reading_key(r: Reading) -> tuple[int, str, str]:
    return r.artifact_id, r.metric, fmt_ts(r.ts)


def version_diff(db: Session, batch_id: int,
                 version_no: int, base_no: int | None = None) -> dict[str, Any]:
    batch = get_batch(db, batch_id)
    versions = {v.version_no: v for v in batch.versions}
    if version_no not in versions:
        raise NotFoundError(f"批次 {batch.batch_no} 不存在版本 V{version_no}")
    new = versions[version_no]
    if base_no is None:
        base_no = version_no - 1
    if base_no not in versions:
        raise NotFoundError(f"基准版本 V{base_no} 不存在，可在页面上选择其它版本对比")
    base = versions[base_no]
    if base.id == new.id:
        raise DomainError("请选择两个不同的版本进行对比")

    old_map = {_reading_key(r): r for r in base.readings}
    new_map = {_reading_key(r): r for r in new.readings}

    added, removed, changed = [], [], []
    for key, r in sorted(new_map.items(), key=lambda kv: kv[0]):
        old = old_map.get(key)
        if old is None:
            added.append(r)
        elif old.value != r.value or old.node_id != r.node_id:
            changed.append((old, r))
    for key, r in sorted(old_map.items(), key=lambda kv: kv[0]):
        if key not in new_map:
            removed.append(r)

    def event_key(e: RiskEvent) -> tuple[int, str, str, str]:
        return e.artifact_id, e.metric, fmt_ts(e.start_ts), fmt_ts(e.end_ts)

    old_events = {event_key(e): e for e in base.events}
    new_events = {event_key(e): e for e in new.events}
    events_added = [e for k, e in sorted(new_events.items(), key=lambda kv: kv[0])
                    if k not in old_events]
    events_removed = [e for k, e in sorted(old_events.items(), key=lambda kv: kv[0])
                      if k not in new_events]
    events_common = [
        (old_events[k], e) for k, e in sorted(new_events.items(), key=lambda kv: kv[0])
        if k in old_events]

    return {
        "batch": batch,
        "base": base,
        "new": new,
        "readings_added": added,
        "readings_removed": removed,
        "readings_changed": changed,
        "events_added": events_added,
        "events_removed": events_removed,
        "events_common": events_common,
    }


# ---------------------------------------------------------------------------
# 批次风险摘要
# ---------------------------------------------------------------------------

def batch_summary(db: Session, batch: Batch) -> dict[str, Any]:
    version = current_version(db, batch.id)
    stats = {
        "version_no": None,
        "version_id": None,
        "reading_total": 0,
        "event_open": 0,
        "event_closed": 0,
        "event_cross_node": 0,
        "event_interrupted": 0,
        "gap_total": 0,
        "artifact_count": len(batch.artifacts),
        "node_count": len(batch.nodes),
        "version_count": len(batch.versions),
        "by_artifact": [],
        "open_events": [],
    }
    if version is None:
        return stats

    stats["version_no"] = version.version_no
    stats["version_id"] = version.id
    stats["reading_total"] = len(version.readings)
    stats["gap_total"] = len(version.gaps)
    open_events = [e for e in version.events if e.status == "open"]
    stats["event_open"] = len(open_events)
    stats["event_closed"] = len(version.events) - len(open_events)
    stats["event_cross_node"] = sum(1 for e in version.events if e.cross_node)
    stats["event_interrupted"] = sum(1 for e in version.events if e.interrupted)
    stats["open_events"] = sorted(
        version.events, key=lambda e: (e.artifact_id, e.metric, e.event_no))

    for a in batch.artifacts:
        evs = [e for e in version.events if e.artifact_id == a.id]
        gaps = [g for g in version.gaps if g.artifact_id == a.id]
        stats["by_artifact"].append({
            "artifact": a,
            "reading_count": sum(1 for r in version.readings if r.artifact_id == a.id),
            "event_open": sum(1 for e in evs if e.status == "open"),
            "event_closed": sum(1 for e in evs if e.status == "closed"),
            "gap_count": len(gaps),
        })
    return stats
