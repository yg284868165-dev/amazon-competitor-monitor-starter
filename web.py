from __future__ import annotations

import os
import subprocess
import threading
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

from app.config import load_projects, load_settings
from app.candidates import (
    create_candidate_alert, load_rules, mark_expired_candidates,
    refresh_automatic_classifications, save_rules, set_candidate_date, set_manual_status,
)
from app.notifications.serverchan import (
    build_daily_summary, build_weekly_summary, has_sendkey, load_notification_config,
    save_notification_config, save_sendkey, send_daily, send_test, send_weekly,
)
from app.paths import LOG_DIR, REPORT_DIR, ROOT, ensure_directories, report_filename
from app.presentation import build_product_identities
from app.reports.daily_report import build_daily_excel_report
from app.reports.excel_report import generate_report
from app.reports.weekly_report import build_weekly_excel_report
from app.storage.database import Database
from app.web_config import CONFIG_SCHEMAS, bulk_add_competitors, bulk_add_keywords, read_schedule, read_table, write_schedule, write_table
from run import failed_targets

app = Flask(__name__)
RUN_COMMANDS = {"product", "search", "bestseller", "all"}


def response_error(exc: Exception, status: int = 400):
    return jsonify({"ok": False, "error": str(exc)}), status


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/config/<kind>")
def get_config(kind: str):
    if kind not in CONFIG_SCHEMAS:
        return response_error(ValueError("未知配置类型"), 404)
    return jsonify({"ok": True, "headers": CONFIG_SCHEMAS[kind], "rows": read_table(kind)})


@app.put("/api/config/<kind>")
def save_config(kind: str):
    if kind not in CONFIG_SCHEMAS:
        return response_error(ValueError("未知配置类型"), 404)
    try:
        write_table(kind, (request.get_json(silent=True) or {}).get("rows", []))
        return jsonify({"ok": True})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/competitors/bulk")
def bulk_competitors():
    payload = request.get_json(silent=True) or {}
    try:
        result = bulk_add_competitors(
            str(payload.get("project_id") or "").strip(),
            str(payload.get("asins") or ""),
            str(payload.get("remark") or ""),
        )
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/keywords/bulk")
def bulk_keywords():
    payload = request.get_json(silent=True) or {}
    try:
        result = bulk_add_keywords(
            str(payload.get("project_id") or "").strip(),
            str(payload.get("keywords") or ""),
            payload.get("pages", 3),
        )
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return response_error(exc)


@app.get("/api/schedule")
def get_schedule():
    return jsonify({"ok": True, "schedule": read_schedule()})


@app.put("/api/schedule")
def save_schedule():
    try:
        write_schedule((request.get_json(silent=True) or {}).get("schedule", {}))
        return jsonify({"ok": True})
    except Exception as exc:
        return response_error(exc)


def schedule_command(command: str):
    result = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "schedule.py"), command],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    return jsonify({"ok": result.returncode == 0, "output": output}), (200 if result.returncode == 0 else 400)


def notification_schedule_command(command: str):
    result = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "notify_schedule.py"), command],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    return jsonify({"ok": result.returncode == 0, "output": output}), (200 if result.returncode == 0 else 400)


@app.post("/api/schedule/install")
def install_schedule():
    return schedule_command("install")


@app.post("/api/schedule/uninstall")
def uninstall_schedule():
    return schedule_command("uninstall")


@app.get("/api/schedule/status")
def schedule_status():
    return schedule_command("status")


@app.get("/api/notification")
def notification_config():
    db = Database()
    logs = db.fetchall(
        """SELECT * FROM notification_logs
           WHERE datetime(sent_at)>=datetime('now','-30 days')
           ORDER BY sent_at DESC,id DESC"""
    )
    successful: set[tuple[str, str]] = set()
    for row in logs:
        row["has_body"] = bool(row.pop("body", None))
        report_type = _notification_report_type(row)
        key = (report_type, str(row["report_date"]))
        row["report_type"] = report_type
        row["retryable"] = bool(
            not row["success"] and report_type in {"daily", "weekly"} and key not in successful
        )
        row["download_url"] = (
            f"/reports/summary/{report_type}?date={row['report_date']}"
            if report_type in {"daily", "weekly"} else None
        )
        if row["success"]:
            successful.add(key)
    return jsonify({"ok": True, "config": load_notification_config(), "has_sendkey": has_sendkey(), "logs": logs})


def _notification_report_type(row: dict) -> str:
    if "collection_alert" in str(row.get("channel") or ""):
        return "collection_alert"
    return "weekly" if "周报" in str(row.get("title") or "") else "daily"


@app.get("/api/notification/logs/<int:log_id>")
def notification_log_detail(log_id: int):
    db = Database()
    rows = db.fetchall("SELECT * FROM notification_logs WHERE id=?", (log_id,))
    if not rows:
        return response_error(ValueError("发送记录不存在"), 404)
    row = rows[0]
    body = str(row.get("body") or "")
    body_source = "stored" if body else "unavailable"
    report_type = _notification_report_type(row)
    if not body and report_type in {"daily", "weekly"}:
        try:
            target = date.fromisoformat(str(row["report_date"]))
            if report_type == "weekly":
                _, body, _ = build_weekly_summary(target)
            else:
                _, body, _ = build_daily_summary(target, load_notification_config()["max_items"])
            body_source = "rebuilt"
        except (TypeError, ValueError):
            body = "历史发送记录没有保存简报正文，且当前数据已无法重新生成。"
    elif not body and report_type == "collection_alert":
        alerts = db.fetchall(
            """SELECT body FROM collection_alerts
               WHERE title=? AND substr(created_at,1,10)=? ORDER BY id DESC LIMIT 1""",
            (row["title"], row["report_date"]),
        )
        body = str(alerts[0]["body"]) if alerts else "历史采集异常记录没有保存通知正文。"
        body_source = "alert_queue" if alerts else "unavailable"
    return jsonify({"ok": True, "row": {
        **row, "report_type": report_type, "body": body, "body_source": body_source,
    }})


@app.post("/api/notification/logs/<int:log_id>/retry")
def retry_notification(log_id: int):
    db = Database()
    rows = db.fetchall("SELECT * FROM notification_logs WHERE id=?", (log_id,))
    if not rows:
        return response_error(ValueError("发送记录不存在"), 404)
    row = rows[0]
    if row["success"]:
        return response_error(ValueError("该简报已经发送成功，无需重发"))
    report_type = _notification_report_type(row)
    if report_type not in {"daily", "weekly"}:
        return response_error(ValueError("采集异常通知会保留在待发队列，并在下次定时采集时自动补发"))
    later_logs = db.fetchall(
        "SELECT * FROM notification_logs WHERE id>? AND report_date=? AND success=1 ORDER BY id",
        (log_id, row["report_date"]),
    )
    if any(_notification_report_type(item) == report_type for item in later_logs):
        return response_error(ValueError("该周期简报后来已经补发成功"))
    try:
        target = date.fromisoformat(str(row["report_date"]))
        result = send_weekly(target, force=True) if report_type == "weekly" else send_daily(target, force=True)
        return jsonify({"ok": True, "report_type": report_type, **result})
    except Exception as exc:
        return response_error(exc)


@app.put("/api/notification")
def save_notification():
    payload = request.get_json(silent=True) or {}
    try:
        config = save_notification_config(payload.get("config") or {})
        sendkey = str(payload.get("sendkey") or "").strip()
        if sendkey:
            save_sendkey(sendkey)
        return jsonify({"ok": True, "config": config, "has_sendkey": has_sendkey()})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/notification/test")
def test_notification():
    try:
        return jsonify({"ok": True, **send_test()})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/notification/send-yesterday")
def send_yesterday_notification():
    try:
        return jsonify({"ok": True, **send_daily(force=True)})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/notification/send-weekly")
def send_weekly_notification():
    try:
        return jsonify({"ok": True, **send_weekly(force=True)})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/notification/schedule/<command>")
def notification_schedule_action(command: str):
    if command not in {"install", "uninstall"}:
        return response_error(ValueError("无效操作"))
    return notification_schedule_command(command)


@app.get("/api/notification/schedule/status")
def notification_schedule_status():
    return notification_schedule_command("status")


@app.get("/api/new-products")
def new_products():
    db = Database()
    mark_expired_candidates(db)
    changed = refresh_automatic_classifications(db)
    latest_runs: dict[str, str | None] = {}
    for row in changed:
        if row["relevance_status"] != "same":
            continue
        project_id = str(row["project_id"])
        if project_id not in latest_runs:
            latest = db.fetchall(
                "SELECT run_id FROM collection_runs WHERE project_id=? ORDER BY started_at DESC LIMIT 1",
                (project_id,),
            )
            latest_runs[project_id] = latest[0]["run_id"] if latest else None
        if latest_runs[project_id]:
            create_candidate_alert(db, latest_runs[project_id], project_id, row["asin"])
    projects = {item.project_id: item for item in load_projects()}
    identities = {project_id: build_product_identities(db, project_id) for project_id in projects}
    rows = db.fetchall("SELECT * FROM bsr_new_candidates ORDER BY first_seen_at DESC")
    for row in rows:
        row["project_name"] = projects[row["project_id"]].project_name if row["project_id"] in projects else row["project_id"]
        row["product"] = identities.get(row["project_id"], {}).get(row["asin"], row["asin"])
    return jsonify({"ok": True, "rules": load_rules(), "rows": rows})


@app.put("/api/new-products/rules")
def update_new_product_rules():
    payload = request.get_json(silent=True) or {}
    try:
        rules = payload.get("rules") or {}
        valid_projects = {item.project_id for item in load_projects()}
        if set(rules) - valid_projects:
            raise ValueError("新品规则中包含不存在的产品项目")
        cleaned = save_rules(rules)
        db = Database()
        refresh_automatic_classifications(db)
        for row in db.fetchall(
            "SELECT * FROM bsr_new_candidates WHERE classification_source='auto' AND relevance_status='same'"
        ):
            latest = db.fetchall("SELECT run_id FROM collection_runs WHERE project_id=? ORDER BY started_at DESC LIMIT 1", (row["project_id"],))
            if latest:
                create_candidate_alert(db, latest[0]["run_id"], row["project_id"], row["asin"])
        return jsonify({"ok": True, "rules": cleaned})
    except Exception as exc:
        return response_error(exc)


@app.put("/api/new-products/candidate")
def update_new_product_candidate():
    payload = request.get_json(silent=True) or {}
    try:
        project_id, asin = str(payload.get("project_id") or ""), str(payload.get("asin") or "").upper()
        row = set_manual_status(Database(), project_id, asin, str(payload.get("status") or ""))
        projects = {item.project_id: item for item in load_projects()}
        if project_id in projects:
            latest = Database().fetchall("SELECT run_id FROM collection_runs WHERE project_id=? ORDER BY started_at DESC LIMIT 1", (project_id,))
            if latest:
                generate_report(Database(), latest[0]["run_id"], projects[project_id])
        return jsonify({"ok": True, "row": row})
    except Exception as exc:
        return response_error(exc)


@app.put("/api/new-products/date")
def update_new_product_date():
    payload = request.get_json(silent=True) or {}
    try:
        project_id = str(payload.get("project_id") or "").strip()
        asin = str(payload.get("asin") or "").strip().upper()
        db = Database()
        row = set_candidate_date(
            db, project_id, asin, str(payload.get("date_first_available") or ""),
        )
        projects = {item.project_id: item for item in load_projects()}
        if project_id in projects:
            latest = db.fetchall(
                "SELECT run_id FROM collection_runs WHERE project_id=? ORDER BY started_at DESC LIMIT 1",
                (project_id,),
            )
            if latest:
                create_candidate_alert(db, latest[0]["run_id"], project_id, asin)
                generate_report(db, latest[0]["run_id"], projects[project_id])
        return jsonify({"ok": True, "row": row})
    except Exception as exc:
        return response_error(exc)


@app.post("/api/run")
def start_run():
    payload = request.get_json(silent=True) or {}
    task = str(payload.get("task") or "all")
    project = str(payload.get("project") or "all")
    if task not in RUN_COMMANDS:
        return response_error(ValueError("无效任务类型"))
    valid_projects = {item.project_id for item in load_projects()}
    if project != "all" and project not in valid_projects:
        return response_error(ValueError("产品项目不存在"))
    ensure_directories()
    log_path = LOG_DIR / "web-manual-run.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "run.py"), task, "--project", project],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    return jsonify({"ok": True, "pid": process.pid, "message": "任务已在后台启动"})


@app.get("/api/runs")
def runs():
    db = Database()
    rows = db.fetchall("SELECT * FROM collection_runs ORDER BY started_at DESC LIMIT 30")
    return jsonify({"ok": True, "rows": rows})


@app.post("/api/runs/<run_id>/retry")
def retry_failed_run(run_id: str):
    db = Database()
    rows = db.fetchall("SELECT status FROM collection_runs WHERE run_id=?", (run_id,))
    if not rows:
        return response_error(ValueError("采集批次不存在"), 404)
    if rows[0]["status"] not in {"partial", "failed"}:
        return response_error(ValueError("只有部分成功或失败的批次可以重试"))
    retries = int(load_settings()["amazon"].get("retries", 2))
    targets = failed_targets(db, run_id, retries)
    if not any(targets.values()):
        return response_error(ValueError("该批次没有可重试的失败项；相关配置可能已被删除或停用"))
    ensure_directories()
    log_path = LOG_DIR / "web-manual-run.log"
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "run.py"), "retry", "--run-id", run_id],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    return jsonify({"ok": True, "pid": process.pid, "message": "失败项重试已在后台启动"})


@app.get("/api/reports")
def reports():
    result = []
    for project in load_projects():
        folder = REPORT_DIR / project.project_name
        main_filename = report_filename(project.project_name)
        result.append({
            "project_id": project.project_id,
            "project_name": project.project_name,
            "main_filename": main_filename,
            "main_exists": (folder / main_filename).exists(),
        })
    yesterday = date.today() - timedelta(days=1)
    week_end = date.today() - timedelta(days=date.today().weekday() + 1)
    return jsonify({
        "ok": True,
        "rows": result,
        "daily": {"date": yesterday.isoformat()},
        "weekly": {
            "start_date": (week_end - timedelta(days=6)).isoformat(),
            "end_date": week_end.isoformat(),
        },
    })


@app.get("/reports/summary/<report_type>")
def download_summary(report_type: str):
    if report_type == "daily":
        try:
            target = date.fromisoformat(request.args["date"]) if request.args.get("date") else date.today() - timedelta(days=1)
        except ValueError:
            return response_error(ValueError("日报日期格式无效"))
        payload = build_daily_excel_report(target)
        filename = f"Amazon竞品日报_{target.isoformat()}.xlsx"
        return send_file(
            payload,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    elif report_type == "weekly":
        try:
            target = date.fromisoformat(request.args["date"]) if request.args.get("date") else date.today() - timedelta(days=date.today().weekday() + 1)
        except ValueError:
            return response_error(ValueError("周报日期格式无效"))
        if target.weekday() != 6:
            return response_error(ValueError("周报截止日期必须是周日"))
        week_start = target - timedelta(days=6)
        payload = build_weekly_excel_report(target)
        filename = f"Amazon竞品周报_{week_start.isoformat()}至{target.isoformat()}.xlsx"
        return send_file(
            payload,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
    else:
        return response_error(ValueError("简报不存在"), 404)


@app.get("/reports/<project_id>/<report_type>")
def download_report(project_id: str, report_type: str):
    projects = {item.project_id: item for item in load_projects()}
    if project_id not in projects or report_type != "main":
        return response_error(ValueError("报告不存在"), 404)
    filename = report_filename(projects[project_id].project_name)
    return send_from_directory(REPORT_DIR / projects[project_id].project_name, filename, as_attachment=True)


def main() -> None:
    ensure_directories()
    print("本地管理页面: http://127.0.0.1:8765")
    if os.environ.get("AMAZON_MONITOR_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()
