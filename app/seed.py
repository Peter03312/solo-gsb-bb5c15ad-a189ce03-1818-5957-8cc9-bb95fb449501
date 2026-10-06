"""演示数据：首次启动且数据库为空时创建「青铜鼎借展运输」批次。

用例覆盖：跨节点连续越限合并、缺测中断事件、旧版关闭结论保留、
补录/更生产生新版本且事件不继承关闭状态。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .database import session_scope
from .models import Artifact, Batch, TransportNode
from . import services

CST = timezone(timedelta(hours=8))
INTERVAL = 3600  # 约定采样间隔：1 小时


def seed_demo_data() -> None:
    with session_scope() as db:
        if db.scalar(select(Batch).where(Batch.batch_no == "B-2026-001")) is not None:
            return

        batch = services.create_batch(db, {
            "batch_no": "B-2026-001",
            "name": "青铜鼎赴伦敦借展运输",
            "borrower": "伦敦东方艺术博物馆",
            "description": "首都博物馆藏品借展，含公路转运、空运与展厅交接三段。",
        })

        a1 = services.create_artifact(db, batch.id, {
            "code": "QT-001", "name": "西周青铜鼎",
            "temp_low": 18.0, "temp_high": 22.0,
            "humidity_low": 45.0, "humidity_high": 55.0,
            "sampling_interval_seconds": INTERVAL,
        })
        a2 = services.create_artifact(db, batch.id, {
            "code": "YH-014", "name": "元代青花梅瓶",
            "temp_low": 19.0, "temp_high": 21.0,
            "humidity_low": 50.0, "humidity_high": 55.0,
            "sampling_interval_seconds": INTERVAL,
        })

        base_day = datetime(2026, 10, 1, 8, 0, tzinfo=CST)
        node_specs = [
            ("N1", 1, "出库装车（首都博物馆）", "北京"),
            ("N2", 2, "空运抵伦敦机场", "伦敦"),
            ("N3", 3, "展厅交接", "伦敦东方艺术博物馆"),
        ]
        for _, seq, name, location in node_specs:
            services.create_node(db, batch.id, {
                "seq": seq, "name": name, "location": location,
                "planned_arrival": (base_day + timedelta(hours=6 * (seq - 1))).isoformat(),
            })

        nodes = list(batch.nodes)
        n1, n2, n3 = nodes[0].id, nodes[1].id, nodes[2].id

        def ts(hour: int) -> str:
            return (base_day + timedelta(hours=hour)).isoformat()

        # ---- V1 初始读数：青铜鼎温度连续越上限，跨 N1->N2 合并为一个事件 ----
        v1 = [
            # 青铜鼎温度（限值 18~22）：10 时起连续超限，跨节点合并
            {"artifact_id": a1.id, "node_id": n1, "ts": ts(0), "metric": "temperature", "value": 20.5},
            {"artifact_id": a1.id, "node_id": n1, "ts": ts(1), "metric": "temperature", "value": 22.6},
            {"artifact_id": a1.id, "node_id": n1, "ts": ts(2), "metric": "temperature", "value": 23.4},
            {"artifact_id": a1.id, "node_id": n2, "ts": ts(3), "metric": "temperature", "value": 22.8},
            {"artifact_id": a1.id, "node_id": n2, "ts": ts(4), "metric": "temperature", "value": 20.9},
            # 青铜鼎湿度：13 时与 11 时间隔 2 小时 > 1 小时 -> 缺测区间，中断前面的湿度越限事件
            {"artifact_id": a1.id, "node_id": n2, "ts": ts(5), "metric": "humidity", "value": 56.2},
            {"artifact_id": a1.id, "node_id": n2, "ts": ts(6), "metric": "humidity", "value": 57.0},
            {"artifact_id": a1.id, "node_id": n3, "ts": ts(8), "metric": "humidity", "value": 56.5},
            {"artifact_id": a1.id, "node_id": n3, "ts": ts(9), "metric": "humidity", "value": 54.0},
            # 青花梅瓶温度：全部正常
            {"artifact_id": a2.id, "node_id": n1, "ts": ts(0), "metric": "temperature", "value": 20.0},
            {"artifact_id": a2.id, "node_id": n2, "ts": ts(3), "metric": "temperature", "value": 20.2},
            {"artifact_id": a2.id, "node_id": n3, "ts": ts(9), "metric": "temperature", "value": 19.8},
        ]
        services.ingest_readings(db, batch.id, {
            "readings": v1, "created_by": "押运员王磊", "remark": "初始运输读数",
        })

        # 在 V1 关闭青铜鼎温度跨节点事件
        from .models import EvaluationVersion, RiskEvent
        version1 = db.scalar(select(EvaluationVersion).where(
            EvaluationVersion.batch_id == batch.id, EvaluationVersion.version_no == 1))
        temp_event = db.scalar(select(RiskEvent).where(
            RiskEvent.version_id == version1.id,
            RiskEvent.artifact_id == a1.id,
            RiskEvent.metric == "temperature"))
        services.close_event(db, temp_event.id, {
            "disposition": "途中恒温箱空调短时故障，已现场重启并加盖隔热层，温度恢复至限值内，文物目检无损。",
            "evidence_no": "EV-20261001-07",
            "closed_by": "文保员李静",
        })

        # ---- V2 补录：展厅交接后 18:00 湿度 55.8（仍越上限）产生一个新的待处置事件；
        #      旧版 V1 的温度关闭结论不受影响，新事件均不继承关闭状态 ----
        v2 = [
            {"artifact_id": a1.id, "node_id": n3, "ts": ts(10), "metric": "humidity",
             "value": 55.8, "source": "supplement"},
        ]
        services.ingest_readings(db, batch.id, {
            "readings": v2, "created_by": "文保员李静",
            "remark": "补录展厅交接时湿度读数",
        })

        # ---- V3 更正：2 时温度应为 21.9（原 23.4 系校准错误）----
        v3 = [
            {"artifact_id": a1.id, "node_id": n1, "ts": ts(2), "metric": "temperature",
             "value": 21.9, "source": "correction"},
        ]
        services.ingest_readings(db, batch.id, {
            "readings": v3, "created_by": "文保员李静",
            "remark": "依据校准证书更正当日 10:00 温度读数",
        })
