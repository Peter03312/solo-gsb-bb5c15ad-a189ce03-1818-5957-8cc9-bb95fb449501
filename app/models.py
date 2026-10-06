"""ORM 模型。

业务实体：
- Batch（借展运输批次）
- Artifact（文物，含温湿度上下限与约定采样间隔）
- TransportNode（有序运输节点）
- EvaluationVersion（评估版本，读数补录/更正生成新版本，旧版不可变）
- Reading（带时间戳的读数，绑定文物与节点）
- RiskEvent（风险事件：同一文物同一指标连续越界读数跨节点合并；
  相邻读数间隔超过采样间隔则标出缺测区间并中断事件）
- MissingGap（缺测区间）
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, String, Text, TypeDecorator, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """跨数据库统一的时区时间类型：统一存为 UTC ISO 8601 字符串。

    SQLite 原生 DateTime 读回会丢失时区，导致 offset-naive 与 offset-aware
    无法比较；这里自行序列化/反序列化，保证取出的时间始终带 UTC 时区，
    且 ISO 字符串字典序与时间顺序一致，可直接排序。
    """

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    borrower: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Artifact.id")
    nodes: Mapped[list["TransportNode"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="TransportNode.seq")
    versions: Mapped[list["EvaluationVersion"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="EvaluationVersion.version_no")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # 温度（℃）
    temp_low: Mapped[float] = mapped_column(Float, nullable=False)
    temp_high: Mapped[float] = mapped_column(Float, nullable=False)
    # 相对湿度（%RH）
    humidity_low: Mapped[float] = mapped_column(Float, nullable=False)
    humidity_high: Mapped[float] = mapped_column(Float, nullable=False)
    # 约定采样间隔（秒）
    sampling_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    batch: Mapped[Batch] = relationship(back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("batch_id", "code", name="uq_artifact_batch_code"),
    )


class TransportNode(Base):
    __tablename__ = "transport_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    location: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    planned_arrival: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="nodes")

    __table_args__ = (
        UniqueConstraint("batch_id", "seq", name="uq_node_batch_seq"),
    )


class EvaluationVersion(Base):
    __tablename__ = "evaluation_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)  # initial/supplement/correction
    remark: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="工作人员")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    batch: Mapped[Batch] = relationship(back_populates="versions")
    readings: Mapped[list["Reading"]] = relationship(
        back_populates="version", cascade="all, delete-orphan")
    events: Mapped[list["RiskEvent"]] = relationship(
        back_populates="version", cascade="all, delete-orphan")
    gaps: Mapped[list["MissingGap"]] = relationship(
        back_populates="version", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("batch_id", "version_no", name="uq_version_batch_no"),
    )


class Reading(Base):
    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("evaluation_versions.id", ondelete="CASCADE"), nullable=False)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("transport_nodes.id", ondelete="RESTRICT"), nullable=False)
    ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    metric: Mapped[str] = mapped_column(String(8), nullable=False)  # temperature/humidity
    value: Mapped[float] = mapped_column(Float, nullable=False)
    # original=原始读数  supplement=补录  correction=更正
    source: Mapped[str] = mapped_column(String(12), nullable=False, default="original")
    # 更正/补录时引用上一版本中被替换或相邻的读数
    ref_reading_id: Mapped[int | None] = mapped_column(
        ForeignKey("readings.id", ondelete="SET NULL"), nullable=True)

    version: Mapped[EvaluationVersion] = relationship(back_populates="readings")
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("risk_events.id", ondelete="SET NULL"), nullable=True)
    event: Mapped["RiskEvent | None"] = relationship(back_populates="readings", foreign_keys=[event_id])

    __table_args__ = (
        UniqueConstraint("version_id", "artifact_id", "metric", "ts",
                         name="uq_reading_version_artifact_metric_ts"),
        Index("ix_reading_version", "version_id"),
    )


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("evaluation_versions.id", ondelete="CASCADE"), nullable=False)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False)
    metric: Mapped[str] = mapped_column(String(8), nullable=False)
    event_no: Mapped[int] = mapped_column(Integer, nullable=False)

    start_ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    end_ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    start_node_id: Mapped[int] = mapped_column(
        ForeignKey("transport_nodes.id", ondelete="RESTRICT"), nullable=False)
    end_node_id: Mapped[int] = mapped_column(
        ForeignKey("transport_nodes.id", ondelete="RESTRICT"), nullable=False)

    peak_value: Mapped[float] = mapped_column(Float, nullable=False)
    peak_direction: Mapped[str] = mapped_column(String(6), nullable=False)  # high/low
    peak_deviation: Mapped[float] = mapped_column(Float, nullable=False)
    reading_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cross_node: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # open=待处置  closed=已关闭
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open")
    disposition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_no: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    closed_by: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    version: Mapped[EvaluationVersion] = relationship(
        back_populates="events", foreign_keys=[version_id])
    readings: Mapped[list[Reading]] = relationship(
        back_populates="event", foreign_keys="Reading.event_id")
    start_node: Mapped[TransportNode] = relationship(foreign_keys=[start_node_id])
    end_node: Mapped[TransportNode] = relationship(foreign_keys=[end_node_id])

    __table_args__ = (
        UniqueConstraint("version_id", "artifact_id", "metric", "event_no",
                         name="uq_event_version_artifact_metric_no"),
    )


class MissingGap(Base):
    """缺测区间：同一文物同一指标相邻读数间隔超过约定采样间隔。"""
    __tablename__ = "missing_gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("evaluation_versions.id", ondelete="CASCADE"), nullable=False)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False)
    metric: Mapped[str] = mapped_column(String(8), nullable=False)
    gap_start_ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    gap_end_ts: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    gap_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    start_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("transport_nodes.id", ondelete="SET NULL"), nullable=True)
    end_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("transport_nodes.id", ondelete="SET NULL"), nullable=True)
    cross_node: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 该缺测是否中断了此前的连续越界风险事件
    interrupted_event: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    version: Mapped[EvaluationVersion] = relationship(back_populates="gaps")
    artifact: Mapped[Artifact] = relationship()
    start_node: Mapped[TransportNode | None] = relationship(foreign_keys=[start_node_id])
    end_node: Mapped[TransportNode | None] = relationship(foreign_keys=[end_node_id])
