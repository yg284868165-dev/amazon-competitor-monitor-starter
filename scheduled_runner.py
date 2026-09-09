from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

from app.notifications.collection_alerts import flush_pending_collection_alerts
from app.paths import CONFIG_DIR, ROOT, ensure_directories
from app.scheduling import (
    CATCH_UP_MINUTES, claim_slot, due_slots, finish_slot,
    queue_delayed_start_alert, record_failed_schedule_projects, record_missed_slot,
)
from app.storage.database import Database


def load_config() -> dict:
    with (CONFIG_DIR / "schedule.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(now: datetime | None = None) -> int:
    ensure_directories()
    current = (now or datetime.now()).replace(microsecond=0)
    config = load_config()
    if not config.get("enabled"):
        return 0
    db = Database()
    slots = due_slots(db, config, current)
    execution_rows = db.fetchall(
        "SELECT slot_time,status,claimed_at FROM scheduled_executions WHERE slot_time<=?",
        (current.isoformat(timespec="seconds"),),
    )
    executions = {row["slot_time"]: row for row in execution_rows}
    pending = [
        slot for slot in slots
        if slot.isoformat(timespec="seconds") not in executions
        or executions[slot.isoformat(timespec="seconds")]["status"] == "running"
    ]
    if not pending:
        flush_pending_collection_alerts(db)
        return 0

    catch_up_cutoff = current - timedelta(minutes=CATCH_UP_MINUTES)
    runnable = pending[-1] if pending[-1] >= catch_up_cutoff else None
    for slot in pending:
        if slot != runnable:
            record_missed_slot(db, slot, config, current)

    if runnable is None or not claim_slot(db, runnable, config, current):
        return 0
    delay = max(0, int((current - runnable).total_seconds()))
    queue_delayed_start_alert(db, runnable, delay)
    flush_pending_collection_alerts(db)
    command = [
        str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "run.py"),
        config.get("task", "all"), "--project", config.get("project", "all"), "--scheduled",
    ]
    # 详细采集日志由 run.py 按日期写入项目 output/logs；避免再把同一批大量日志
    # 追加到 launchd 的固定诊断文件中。
    result = subprocess.run(
        command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    finished = datetime.now().replace(microsecond=0)
    finish_slot(db, runnable, result.returncode, finished)
    if result.returncode:
        created = db.fetchall(
            """SELECT run_id FROM collection_runs
               WHERE source_run_id IS NULL AND datetime(started_at)>=datetime(?)""",
            (current.isoformat(timespec="seconds"),),
        )
        if not created:
            record_failed_schedule_projects(
                db, runnable, config, finished, "schedule_start_failed",
                "定时入口已触发，但采集程序未能创建任务；可能存在运行锁或启动环境异常",
            )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
