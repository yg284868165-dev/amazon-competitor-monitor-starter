from __future__ import annotations

import json
import logging
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.ad_patterns import build_weekly_ad_insights
from app.config import load_projects
from app.paths import CONFIG_DIR
from app.presentation import build_product_identities, format_change_summary
from app.storage.database import Database
from app.trends import analyze_all_weekly_trends

KEYCHAIN_SERVICE = "AmazonCompetitorMonitor.ServerChan"
KEYCHAIN_ACCOUNT = os.environ.get("USER", "amazon-monitor")
CONFIG_PATH = CONFIG_DIR / "notification.json"
SERVERCHAN_RETRY_DELAYS_SECONDS = (30, 120, 300)
LOGGER = logging.getLogger("monitor")
INTRADAY_EVENT_FIELDS = {
    "current_price", "coupon_text", "deal_text", "business_price_text", "availability",
    "featured_seller", "offer_count", "ships_from",
    "high_return_rate", "listing_status", "main_category_name", "subcategory_names",
}


class ServerChanTransportError(RuntimeError):
    """A transient network/TLS/response error that is safe to retry."""


class ServerChanAPIError(RuntimeError):
    """A definitive ServerChan response that should not be retried automatically."""


def default_config() -> dict[str, Any]:
    return {"enabled": False, "time": "09:00", "send_when_no_changes": True, "max_items": 15}


def load_notification_config() -> dict[str, Any]:
    config = default_config()
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config.update(json.load(handle))
    return config


def validate_notification_config(value: dict[str, Any]) -> dict[str, Any]:
    send_time = str(value.get("time") or "09:00").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("微信发送时间必须使用 HH:MM，例如 09:00")
    max_items = int(value.get("max_items") or 15)
    if not 1 <= max_items <= 50:
        raise ValueError("摘要最多展示数量必须在 1–50 之间")
    return {
        "enabled": bool(value.get("enabled")), "time": send_time,
        "send_when_no_changes": bool(value.get("send_when_no_changes", True)),
        "max_items": max_items,
    }


def save_notification_config(value: dict[str, Any]) -> dict[str, Any]:
    config = validate_notification_config(value)
    temp = CONFIG_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, CONFIG_PATH)
    return config


def save_sendkey(sendkey: str) -> None:
    value = sendkey.strip()
    if not re.fullmatch(r"SCT[a-zA-Z0-9_-]+", value):
        raise ValueError("SendKey 格式无效，应当以 SCT 开头")
    subprocess.run([
        "/usr/bin/security", "add-generic-password", "-U", "-a", KEYCHAIN_ACCOUNT,
        "-s", KEYCHAIN_SERVICE, "-w", value,
    ], check=True, capture_output=True, text=True)


def get_sendkey() -> str:
    result = subprocess.run([
        "/usr/bin/security", "find-generic-password", "-w", "-a", KEYCHAIN_ACCOUNT,
        "-s", KEYCHAIN_SERVICE,
    ], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("尚未保存 Server酱 SendKey")
    return result.stdout.strip()


def has_sendkey() -> bool:
    try:
        return bool(get_sendkey())
    except RuntimeError:
        return False


def _values_equal(old_value: Any, new_value: Any) -> bool:
    """Treat equivalent numeric strings as equal when collapsing intraday events."""
    if old_value is None or new_value is None:
        return old_value is new_value
    old_text, new_text = str(old_value).strip(), str(new_value).strip()
    try:
        return Decimal(old_text) == Decimal(new_text)
    except InvalidOperation:
        return old_text == new_text


def _weekly_values_equal(field_name: str | None, old_value: Any, new_value: Any) -> bool:
    if field_name == "offer_count":
        empty = {None, "", 0, "0", "0.0"}
        if old_value in empty and new_value in empty:
            return True
    if field_name in {
        "coupon_text", "deal_text", "business_price_text", "featured_seller", "ships_from",
    } and not str(old_value or "").strip() and not str(new_value or "").strip():
        return True
    return _values_equal(old_value, new_value)


def _collapse_daily_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple, dict[str, Any]] = {}
    for row in events:
        key = (
            row["project_id"], row["source_type"], row.get("asin"),
            row.get("keyword"), row.get("category_name"), row.get("field_name"),
            row.get("event_type"),
        )
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["_transitions"] = [{
                "old_value": row.get("old_value"), "new_value": row.get("new_value"),
                "event_time": row.get("event_time"),
            }]
        else:
            grouped[key]["_transitions"].append({
                "old_value": row.get("old_value"), "new_value": row.get("new_value"),
                "event_time": row.get("event_time"),
            })
            grouped[key]["new_value"] = row.get("new_value")
            grouped[key]["message"] = row.get("message")
            grouped[key]["event_time"] = row.get("event_time")
            grouped[key]["details_json"] = row.get("details_json")
    # A field can fluctuate during the day and return to its starting value. Such a
    # sequence is useful in history, but it is not a net change worth notifying.
    net_changes = [
        row for row in grouped.values()
        if row.get("field_name") in INTRADAY_EVENT_FIELDS
        or not _values_equal(row.get("old_value"), row.get("new_value"))
    ]
    main_image_changes = {
        (row["project_id"], row.get("asin"))
        for row in net_changes if row.get("field_name") == "image_hash"
    }
    return [
        row for row in net_changes
        if not (
            row.get("field_name") == "product_images_hash"
            and (row["project_id"], row.get("asin")) in main_image_changes
        )
    ]


def _daily_change_items(db: Database, report_date: date) -> list[dict[str, Any]]:
    events = db.fetchall(
        """SELECT * FROM change_events WHERE substr(event_time,1,10)=?
           AND (source_type<>'bestseller' OR event_type IN ('entered_bestseller','new_competitor_found'))
           AND source_type<>'search'
           AND COALESCE(field_name,'') NOT IN ('main_bsr','rating_count')
           ORDER BY event_time,id""", (report_date.isoformat(),),
    )
    return _collapse_daily_events(events)


def _weekly_recap_items(db: Database, week_end: date) -> list[dict[str, Any]]:
    """Return the exact daily-report events for a complete Monday-Sunday week."""
    if week_end.weekday() != 6:
        raise ValueError("周报统计截止日期必须是周日")
    week_start = week_end - timedelta(days=6)
    items: list[dict[str, Any]] = []
    for offset in range(7):
        report_date = week_start + timedelta(days=offset)
        for row in _daily_change_items(db, report_date):
            items.append({**row, "report_date": report_date.isoformat()})

    grouped: dict[tuple, list[dict[str, Any]]] = {}
    for row in items:
        key = (
            row["project_id"], row["source_type"], row.get("asin"),
            row.get("keyword"), row.get("category_name"), row.get("field_name"),
        )
        grouped.setdefault(key, []).append(row)
    for rows in grouped.values():
        weekly_state = (
            "周内已恢复原状态" if _weekly_values_equal(
                rows[0].get("field_name"), rows[0].get("old_value"), rows[-1].get("new_value"),
            )
            else "截至周末仍保持变化"
        )
        for row in rows:
            row["weekly_state"] = weekly_state
    return items


def _weekly_date_label(value: Any) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return str(value or "")
    return f"{parsed.month}月{parsed.day}日"


def _rating_number(value: Any) -> int | None:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return int(number) if number == number.to_integral_value() else None


def _weekly_rating_changes(db: Database, report_date: date) -> list[dict[str, Any]]:
    """Return Monday-Sunday review-count deltas only for a Sunday report."""
    if report_date.weekday() != 6:
        return []
    week_start = report_date - timedelta(days=6)
    week_end = report_date + timedelta(days=1)
    rows = db.fetchall(
        """SELECT * FROM change_events
           WHERE field_name='rating_count' AND event_time>=? AND event_time<?
           ORDER BY event_time,id""",
        (week_start.isoformat(), week_end.isoformat()),
    )
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        asin = str(row.get("asin") or "")
        if not asin:
            continue
        key = (row["project_id"], asin)
        old_count = _rating_number(row.get("old_value"))
        new_count = _rating_number(row.get("new_value"))
        if key not in grouped:
            grouped[key] = {
                "project_id": row["project_id"], "asin": asin,
                "old_count": old_count if old_count is not None else new_count,
                "new_count": new_count,
            }
        elif new_count is not None:
            grouped[key]["new_count"] = new_count
    result = []
    for row in grouped.values():
        old_count, new_count = row["old_count"], row["new_count"]
        if old_count is None or new_count is None or old_count == new_count:
            continue
        result.append({**row, "change_count": new_count - old_count})
    return result


def _daily_run_statistics(db: Database, report_date: date) -> dict[str, dict[str, int]]:
    """Summarize root batches by their final retry-chain result."""
    roots = db.fetchall(
        """SELECT * FROM collection_runs WHERE substr(started_at,1,10)=?
           AND source_run_id IS NULL ORDER BY started_at""",
        (report_date.isoformat(),),
    )
    result: dict[str, dict[str, int]] = {}
    for root in roots:
        values = result.setdefault(root["project_id"], {
            "batches": 0, "completed": 0, "recovered": 0,
            "unresolved": 0, "running": 0, "failed_items": 0,
        })
        values["batches"] += 1
        if root["status"] == "success":
            values["completed"] += 1
            continue
        later_full_success = any(
            candidate["project_id"] == root["project_id"]
            and candidate["task_type"] == root["task_type"]
            and candidate["started_at"] > root["started_at"]
            and candidate["status"] == "success"
            for candidate in roots
        )
        descendants = db.fetchall(
            """WITH RECURSIVE chain(run_id,status,started_at) AS (
                   SELECT run_id,status,started_at FROM collection_runs WHERE source_run_id=?
                   UNION ALL
                   SELECT child.run_id,child.status,child.started_at
                   FROM collection_runs child JOIN chain parent ON child.source_run_id=parent.run_id
               ) SELECT status FROM chain ORDER BY started_at DESC""",
            (root["run_id"],),
        )
        if later_full_success or any(row["status"] == "success" for row in descendants):
            values["recovered"] += 1
        elif any(row["status"] == "running" for row in descendants) or root["status"] == "running":
            values["running"] += 1
        else:
            values["unresolved"] += 1
            values["failed_items"] += int(root.get("failed_count") or 0)
    return result


def build_daily_summary(report_date: date, max_items: int = 15) -> tuple[str, str, int]:
    db = Database()
    projects = {item.project_id: item.project_name for item in load_projects()}
    items = _daily_change_items(db, report_date)
    title = f"Amazon竞品昨日监控｜{report_date.month}月{report_date.day}日"
    lines = [f"## {title}"]
    runs_by_project = _daily_run_statistics(db, report_date)
    identities_by_project = {project_id: build_product_identities(db, project_id) for project_id in projects}
    shown = 0
    project_ids = list(dict.fromkeys([
        *projects, *(row["project_id"] for row in items),
    ]))
    active_project_ids = [project_id for project_id in project_ids if any(row["project_id"] == project_id for row in items) or project_id in runs_by_project]
    for project_index, project_id in enumerate(active_project_ids):
        project_items = [row for row in items if row["project_id"] == project_id]
        run = runs_by_project.get(project_id)
        if not project_items and not run:
            continue
        lines.extend(["", f"### {projects.get(project_id, project_id)}"])
        if run:
            summary = (
                f"采集主批次 {run['batches']} 个：正常完成{run['completed']}，"
                f"重试恢复{run['recovered']}，仍失败{run['unresolved']}"
            )
            if run["running"]:
                summary += f"，仍在运行{run['running']}"
            if run["failed_items"]:
                summary += f"（未恢复失败项{run['failed_items']}个）"
            lines.append(summary)
        lines.append(f"重点变化 {len(project_items)} 项")
        sections = [
            ("商品内容与运营变化", [row for row in project_items if row["source_type"] == "product"]),
            ("新竞争对手", [row for row in project_items if row["source_type"] == "bestseller"]),
        ]
        remaining_projects = len(active_project_ids) - project_index
        project_limit = max(0, (max_items - shown) // remaining_projects)
        queues = [(section_name, list(section_items)) for section_name, section_items in sections]
        selected: dict[str, list[dict[str, Any]]] = {section_name: [] for section_name, _ in sections}
        selected_count = 0
        while selected_count < project_limit and any(queue for _, queue in queues):
            for section_name, queue in queues:
                if queue and selected_count < project_limit:
                    selected[section_name].append(queue.pop(0))
                    selected_count += 1
                    if selected_count >= project_limit:
                        break
        identities = identities_by_project.get(project_id, {})
        for section_name, section_items in sections:
            visible = selected[section_name]
            if not visible:
                continue
            lines.extend(["", f"**{section_name}**"])
            for row in visible:
                asin = str(row.get("asin") or "")
                target = identities.get(asin, asin or row.get("keyword") or row.get("category_name") or "—")
                lines.append(f"- {target}：{format_change_summary(row)}")
                shown += 1
    total_items = len(items)
    if not runs_by_project and not total_items:
        lines.extend(["", "昨日没有采集记录。"])
    elif not total_items:
        lines.extend(["", "昨日未发现竞品变化。"])
    if total_items > shown:
        lines.extend(["", f"另有 {total_items - shown} 项变化未展开，请查看 Excel 报告。"])
    return title, "\n".join(lines), total_items


def build_weekly_summary(week_end: date) -> tuple[str, str, int]:
    if week_end.weekday() != 6:
        raise ValueError("周报统计截止日期必须是周日")
    db = Database()
    week_start = week_end - timedelta(days=6)
    recap_items = _weekly_recap_items(db, week_end)
    rating_changes = _weekly_rating_changes(db, week_end)
    rank_trends = analyze_all_weekly_trends(db, week_end)
    ad_insights = build_weekly_ad_insights(db, week_end)
    projects = {item.project_id: item.project_name for item in load_projects()}
    title = (
        f"Amazon竞品周报｜{week_start.month}月{week_start.day}日"
        f"—{week_end.month}月{week_end.day}日"
    )
    lines = [
        f"## {title}", "",
        "统计口径：完整自然周（周一至周日）；排名同时观察近1周、2周和4周。",
        "广告观察使用美国西部时间；策略推测只代表重复采样线索，不等同于竞对实际竞价设置。",
    ]
    project_ids = list(dict.fromkeys([
        *projects, *(row["project_id"] for row in recap_items),
        *(row["project_id"] for row in rating_changes),
        *(row["project_id"] for row in rank_trends),
        *(row["project_id"] for row in ad_insights),
    ]))
    identities_by_project = {
        project_id: build_product_identities(db, project_id) for project_id in project_ids
    }
    active_project_ids = [
        project_id for project_id in project_ids
        if any(row["project_id"] == project_id for row in recap_items)
        or any(row["project_id"] == project_id for row in rating_changes)
        or any(row["project_id"] == project_id for row in rank_trends)
        or any(row["project_id"] == project_id for row in ad_insights)
    ]
    for project_id in active_project_ids:
        project_recap = [row for row in recap_items if row["project_id"] == project_id]
        project_ratings = [row for row in rating_changes if row["project_id"] == project_id]
        project_trends = [row for row in rank_trends if row["project_id"] == project_id]
        project_ads = [row for row in ad_insights if row["project_id"] == project_id]
        sections = [
            ("评价数量变化", project_ratings),
            ("大类目BSR趋势", [row for row in project_trends if row["metric"] == "main_bsr"]),
            ("小类目BSR趋势", [row for row in project_trends if row["metric"] == "category_bsr"]),
            ("关键词自然排名趋势", [row for row in project_trends if row["metric"] == "organic_rank"]),
        ]
        lines.extend([
            "", f"### {projects.get(project_id, project_id)}",
            f"本周重点变化 {len(project_recap)} 项，评价变化 {len(project_ratings)} 个商品，"
            f"明显排名趋势 {len(project_trends)} 项，广告投放观察 {len(project_ads)} 项",
        ])
        identities = identities_by_project.get(project_id, {})
        if project_recap:
            lines.extend(["", "**本周重点复盘**"])
            by_product: dict[str, list[dict[str, Any]]] = {}
            for row in project_recap:
                key = str(row.get("asin") or row.get("keyword") or row.get("category_name") or "其他变化")
                by_product.setdefault(key, []).append(row)
            for product_key, product_items in by_product.items():
                first = product_items[0]
                asin = str(first.get("asin") or "")
                target = identities.get(asin, asin or product_key)
                lines.append(f"- {target}")
                for row in product_items:
                    lines.append(
                        f"  - {_weekly_date_label(row.get('report_date'))}｜{row['weekly_state']}："
                        f"{format_change_summary(row)}"
                    )
        for section_name, values in sections:
            if not values:
                continue
            lines.extend(["", f"**{section_name}**"])
            for row in values:
                if "old_count" in row:
                    asin = str(row.get("asin") or "")
                    target = identities.get(asin, asin or "—")
                    direction = "增加" if row["change_count"] > 0 else "减少"
                    lines.append(
                        f"- {target}：评价数量 {row['old_count']} → {row['new_count']}"
                        f"（{direction}{abs(row['change_count'])}条）"
                    )
                else:
                    context = row.get("category_name") or f"“{row.get('keyword')}”"
                    summary = "；".join(row["summaries"])
                    lines.append(f"- {row['product']}｜{context}：{summary}")
        if project_ads:
            lines.extend(["", "**广告投放观察**"])
            for row in project_ads:
                lines.extend([
                    f"- {row['product']}｜“{row['keyword']}”",
                    f"  - 观察：{row['observation']}",
                    f"  - 推测：{row['inference']}（可信度：{row['confidence']}）",
                ])
    total_items = len(recap_items) + len(rating_changes) + len(rank_trends) + len(ad_insights)
    if not total_items:
        lines.extend(["", "上周未发现日报重点变化、评价数量净变化、明显排名趋势或可信广告投放规律。"])
    return title, "\n".join(lines), total_items


def send_serverchan(title: str, body: str) -> str:
    sendkey = get_sendkey()
    data = urllib.parse.urlencode({"title": title[:32], "desp": body}).encode("utf-8")
    request = urllib.request.Request(f"https://sctapi.ftqq.com/{sendkey}.send", data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.URLError, TimeoutError, ssl.SSLError,
        ConnectionError, json.JSONDecodeError,
    ) as exc:
        raise ServerChanTransportError(f"Server酱请求失败：{exc}") from exc
    if payload.get("code") != 0:
        raise ServerChanAPIError(
            f"Server酱发送失败：{payload.get('message') or payload.get('data') or payload}"
        )
    return str(payload.get("message") or "发送成功")


def _send_serverchan_with_retry(title: str, body: str) -> tuple[str, int]:
    total_attempts = len(SERVERCHAN_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return send_serverchan(title, body), attempt
        except ServerChanTransportError as exc:
            if attempt >= total_attempts:
                raise ServerChanTransportError(
                    f"Server酱网络请求连续{total_attempts}次失败（初次发送及3次自动重试）：{exc}"
                ) from exc
            delay = SERVERCHAN_RETRY_DELAYS_SECONDS[attempt - 1]
            LOGGER.warning(
                "Server酱第%d次请求失败，%d秒后进行第%d次尝试: %s",
                attempt, delay, attempt + 1, exc,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def _success_message(message: str, attempt: int) -> str:
    if attempt == 1:
        return message
    return f"{message}（自动重试第{attempt - 1}次后成功）"


def send_daily(report_date: date | None = None, force: bool = False) -> dict[str, Any]:
    config = load_notification_config()
    if not force and not config["enabled"]:
        return {"sent": False, "message": "微信通知未启用"}
    target_date = report_date or (date.today() - timedelta(days=1))
    title, body, count = build_daily_summary(target_date, config["max_items"])
    if count == 0 and not config["send_when_no_changes"] and "没有采集记录" not in body:
        return {"sent": False, "message": "昨日无变化，按设置不发送", "item_count": 0}
    db = Database()
    try:
        message, attempt = _send_serverchan_with_retry(title, body)
        message = _success_message(message, attempt)
        db.record_notification(target_date.isoformat(), True, title, count, message, body=body)
        return {"sent": True, "message": message, "item_count": count, "title": title}
    except Exception as exc:
        db.record_notification(target_date.isoformat(), False, title, count, str(exc), body=body)
        raise


def send_weekly(week_end: date | None = None, force: bool = False) -> dict[str, Any]:
    config = load_notification_config()
    if not force and not config["enabled"]:
        return {"sent": False, "message": "微信通知未启用"}
    today = date.today()
    target_date = week_end or (today - timedelta(days=today.weekday() + 1))
    title, body, count = build_weekly_summary(target_date)
    if count == 0 and not config["send_when_no_changes"]:
        return {"sent": False, "message": "上周无日报重点变化、评价数量净变化、明显排名趋势或可信广告投放规律，按设置不发送", "item_count": 0}
    db = Database()
    try:
        message, attempt = _send_serverchan_with_retry(title, body)
        message = _success_message(message, attempt)
        db.record_notification(target_date.isoformat(), True, title, count, message, body=body)
        return {"sent": True, "message": message, "item_count": count, "title": title}
    except Exception as exc:
        db.record_notification(target_date.isoformat(), False, title, count, str(exc), body=body)
        raise


def send_test() -> dict[str, Any]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = send_serverchan("Amazon竞品监控测试", f"Server酱连接成功。\n\n发送时间：{now}")
    return {"sent": True, "message": message}
