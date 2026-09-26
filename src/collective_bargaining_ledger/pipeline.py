"""可恢复的审议准备流水线。

用途：园区工会在每轮协商开始前，固定要走一串准备动作（任命人员、开启回合、
任命双方代表……）。流水线把每个动作当作一个检查点步骤：

- 步骤动作在服务层完成，服务层所有写操作都带业务号，天然幂等；
- 每个步骤开始登记一次尝试、完成后写入检查点，检查点独立事务落盘并 fsync；
- 进程被 ``SIGKILL``、断电或人工终止后，用同一数据库重新启动，
  已完成的步骤直接跳过，正在执行未完成的步骤用原业务号重试，沿用原结果。

“从原检查进度继续”由此可被测试直接验证：第二次运行绝不会产生第二个回合、
第二任代表或重复任命。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable

from .canonical import canonical_json
from .service import BargainingService
from .storage import Store


@dataclass(frozen=True)
class Step:
    name: str
    request_id: str
    action: Callable[[BargainingService], dict]


class PipelineRunner:
    def __init__(self, store: Store):
        self.store = store

    def run(
        self,
        pipeline_id: str,
        round_id_hint: str,
        steps: list[Step],
        *,
        crash_after: str | None = None,
    ) -> dict:
        """执行（或恢复）流水线。

        ``crash_after`` 仅用于可测试性：命名步骤完成后立即以 ``os._exit``
        模拟硬终止（不执行任何 Python 清理），证明恢复依赖的是落盘检查点
        而不是内存状态。
        """

        done = self._completed_steps(pipeline_id)
        executed: list[str] = []
        skipped: list[str] = []
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO pipeline_state(pipeline_id,round_id,status,current_step,"
                "updated_at) VALUES(?,?, 'running',0,?) "
                "ON CONFLICT(pipeline_id) DO UPDATE SET status='running',updated_at=excluded.updated_at",
                (pipeline_id, round_id_hint, _now()),
            )
        for index, step in enumerate(steps):
            if step.name in done:
                skipped.append(step.name)
                continue
            service = BargainingService(self.store)
            # 登记尝试；这一步与检查点写入分别提交，崩溃窗口落在两者之间。
            self._record_attempt(pipeline_id, index, step)
            result = step.action(service)
            self._checkpoint(pipeline_id, index, step, result)
            executed.append(step.name)
            if crash_after == step.name:
                # 硬终止：模拟进程被 kill -9，不缓冲、不清理。
                sys.stdout.flush()
                os._exit(86)
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE pipeline_state SET status='completed',current_step=?,updated_at=? "
                "WHERE pipeline_id=?",
                (len(steps), _now(), pipeline_id),
            )
        state = self.state(pipeline_id)
        return {"pipeline_id": pipeline_id, "executed": executed,
                "resumed_skipped": skipped, "state": state}

    def state(self, pipeline_id: str) -> dict:
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_state WHERE pipeline_id=?", (pipeline_id,)
            ).fetchone()
            if row is None:
                return {}
            checkpoints = [
                {"step_index": r["step_index"], "step_name": r["step_name"],
                 "attempts": r["attempts"], "completed_at": r["completed_at"],
                 "detail": r["detail"]}
                for r in conn.execute(
                    "SELECT * FROM checkpoints WHERE pipeline_id=? ORDER BY step_index",
                    (pipeline_id,),
                )
            ]
            return {"status": row["status"], "current_step": row["current_step"],
                    "checkpoints": checkpoints}

    def _completed_steps(self, pipeline_id: str) -> set[str]:
        # 只把检查点已落盘（completed_at 非空）的步骤视为完成；
        # 崩溃在“动作已提交、检查点未写”窗口的步骤会重新执行，
        # 由服务层业务号幂等保证沿用原结果。
        return {
            r["step_name"]
            for r in self.store.query_all(
                "SELECT step_name FROM checkpoints WHERE pipeline_id=? "
                "AND completed_at != ''",
                (pipeline_id,),
            )
        }

    def _record_attempt(self, pipeline_id: str, index: int, step: Step) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO checkpoints(pipeline_id,step_index,step_name,attempts,"
                "detail,completed_at) VALUES(?,?,?,1,'started','') "
                "ON CONFLICT(pipeline_id,step_index) DO UPDATE SET attempts=attempts+1",
                (pipeline_id, index, step.name),
            )
            conn.execute(
                "UPDATE pipeline_state SET current_step=?,updated_at=? WHERE pipeline_id=?",
                (index, _now(), pipeline_id),
            )

    def _checkpoint(self, pipeline_id: str, index: int, step: Step, result: dict) -> None:
        with self.store.tx() as conn:
            conn.execute(
                "UPDATE checkpoints SET detail=?,completed_at=? "
                "WHERE pipeline_id=? AND step_index=?",
                (canonical_json(result), _now(), pipeline_id, index),
            )


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bootstrap_steps(
    facilitator_token: str,
    title: str,
    union_name: str = "职工方首席代表",
    enterprise_name: str = "企业方首席代表",
    *,
    key_prefix: str = "bootstrap-2026-round",
) -> tuple[str, list[Step]]:
    """构造“新回合开启”标准流水线步骤。

    回合号与双方代表令牌都由 ``key_prefix`` 确定性派生，业务号也固定。
    这样进程在任意窗口被终止后重试，使用的都是同一编号、同一令牌、
    同一业务号，服务层幂等保证沿用原结果，绝不产生第二个回合或第二任代表。
    流水线完成后可用 :func:`bootstrap_artifacts` 从检查点取回令牌。
    """

    from .canonical import content_hash

    round_id = "R-" + content_hash({"bootstrap": key_prefix, "kind": "round"})[:12]
    union_token = "rep-u-" + content_hash({"bootstrap": key_prefix, "side": "union"})[:20]
    enterprise_token = "rep-e-" + content_hash(
        {"bootstrap": key_prefix, "side": "enterprise"}
    )[:20]

    def open_round(service: BargainingService) -> dict:
        return service.open_round(
            facilitator_token, title, f"{key_prefix}:open_round", round_id=round_id
        )

    def appoint_union(service: BargainingService) -> dict:
        return service.appoint_representative(
            facilitator_token, round_id, "union", union_token,
            union_name, f"{key_prefix}:appoint_union",
        )

    def appoint_enterprise(service: BargainingService) -> dict:
        return service.appoint_representative(
            facilitator_token, round_id, "enterprise", enterprise_token,
            enterprise_name, f"{key_prefix}:appoint_enterprise",
        )

    steps = [
        Step("open_round", f"{key_prefix}:open_round", open_round),
        Step("appoint_union", f"{key_prefix}:appoint_union", appoint_union),
        Step("appoint_enterprise", f"{key_prefix}:appoint_enterprise", appoint_enterprise),
    ]
    return round_id, steps


def bootstrap_artifacts(key_prefix: str) -> dict[str, str]:
    """不依赖运行内存，按相同派生规则取回回合号与代表令牌。"""

    from .canonical import content_hash

    return {
        "round_id": "R-" + content_hash({"bootstrap": key_prefix, "kind": "round"})[:12],
        "union_token": "rep-u-"
        + content_hash({"bootstrap": key_prefix, "side": "union"})[:20],
        "enterprise_token": "rep-e-"
        + content_hash({"bootstrap": key_prefix, "side": "enterprise"})[:20],
    }
