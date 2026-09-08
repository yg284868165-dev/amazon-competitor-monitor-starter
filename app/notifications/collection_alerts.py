from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from app.config import load_projects
from app.notifications.serverchan import load_notification_config, send_serverchan
from app.storage.database import Database


LOGGER = logging.getLogger("monitor")
TASK_LABELS = {"product": "商品", "search": "关键词", "bestseller": "BSR榜单"}
RUN_STATUS_LABELS = {
    "success": "成功", "partial": "部分完成", "failed": "失败", "running": "运行中",
}


def _retry_chain(db: Database, source_run_id: str) -> list[dict[str, Any]]:
    return db.fetchall(
        """WITH RECURSIVE chain AS (
               SELECT * FROM collection_runs WHERE run_id=?
               UNION ALL
               SELECT child.* FROM collection_runs child
               JOIN chain parent ON child.source_run_id=parent.run_id
           ) SELECT * FROM chain ORDER BY started_at""",
        (source_run_id,),
    )


def _failed_target_summary(db: Database, run_id: str) -> tuple[int, str]:
    rows = db.fetchall(
        """SELECT task_type,COUNT(*) count FROM collection_task_outcomes
           WHERE run_id=? AND success=0 GROUP BY task_type ORDER BY task_type""",
        (run_id,),
    )
    counts = Counter({row["task_type"]: int(row["count"]) for row in rows})
    total = sum(counts.values())
    detail = "、".join(
        f"{TASK_LABELS.get(task, task)}{counts[task]}项"
        for task in ("product", "search", "bestseller") if counts[task]
    )
    return total, detail or "失败目标待确认"


def queue_scheduled_collection_alerts(db: Database, source_run_ids: list[str]) -> int:
    """Queue one final alert per scheduled root run after its retry window."""
    if not source_run_ids or not load_notification_config().get("enabled"):
        return 0
    projects = {item.project_id: item.project_name for item in load_projects()}
    queued = 0
    for source_run_id in source_run_ids:
        chain = _retry_chain(db, source_run_id)
        if not chain:
            continue
        original = chain[0]
        descendants = chain[1:]
        if any(row["status"] == "running" for row in chain):
            continue
        recovered_runs = [row for row in descendants if row["status"] == "success"]
        recovered = bool(recovered_runs)
        final = recovered_runs[-1] if recovered else chain[-1]
        project_name = projects.get(original["project_id"], original["project_id"])
        initial_failed = int(original.get("failed_count") or 0)
        started_at = str(original["started_at"]).replace("T", " ")
        initial_status = RUN_STATUS_LABELS.get(original["status"], original["status"])
        if recovered:
            title = f"Amazon采集异常已恢复｜{project_name}"
            body = "\n".join([
                f"## {title}", "",
                f"项目：{project_name}",
                f"原定采集时间：{started_at}",
                f"首次结果：{initial_status}，失败{initial_failed}项",
                f"重试结果：成功{int(final.get('success_count') or 0)}项，失败0项",
                "处理结果：失败目标已经通过重试恢复，无需人工操作。",
            ])
            final_status = "recovered"
        else:
            remaining, detail = _failed_target_summary(db, final["run_id"])
            remaining = remaining or int(final.get("failed_count") or 0)
            title = f"Amazon采集失败｜{project_name}"
            body = "\n".join([
                f"## {title}", "",
                f"项目：{project_name}",
                f"原定采集时间：{started_at}",
                f"首次结果：{initial_status}，失败{initial_failed}项",
                f"最终结果：仍有{remaining}项失败（{detail}）",
                "处理建议：网络恢复后在管理页面“最近运行”中点击“重试失败项”。",
            ])
            final_status = "failed"
        db.queue_collection_alert(
            source_run_id, original["project_id"], final_status, title, body,
        )
        queued += 1
    return queued


def flush_pending_collection_alerts(db: Database) -> dict[str, int]:
    """Send queued alerts; leave them pending if ServerChan is unreachable."""
    if not load_notification_config().get("enabled"):
        return {"sent": 0, "pending": len(db.pending_collection_alerts())}
    sent = 0
    for row in db.pending_collection_alerts():
        try:
            message = send_serverchan(row["title"], row["body"])
            db.mark_collection_alert_sent(int(row["id"]))
            db.record_notification(
                str(row["created_at"])[:10], True, row["title"], 1, message,
                channel="serverchan_collection_alert", body=row["body"],
            )
            sent += 1
        except Exception as exc:
            error = str(exc)
            db.mark_collection_alert_failed(int(row["id"]), error)
            db.record_notification(
                str(row["created_at"])[:10], False, row["title"], 1, error,
                channel="serverchan_collection_alert", body=row["body"],
            )
            LOGGER.warning("采集异常通知发送失败，已保留等待下次补发: %s", exc)
            break
    return {"sent": sent, "pending": len(db.pending_collection_alerts())}
