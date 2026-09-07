from __future__ import annotations

import argparse
import asyncio
import copy
import fcntl
import os
import subprocess
from contextlib import contextmanager
import logging

from app.browser.manager import BrowserManager
from app.candidates import candidate_backlog
from app.collectors.bestseller_collector import collect_bestsellers
from app.collectors.candidate_collector import collect_candidate_details
from app.collectors.product_collector import collect_products
from app.collectors.search_collector import collect_searches
from app.comparison.engine import compare_bestsellers, compare_products, compare_searches
from app.config import Project, load_categories, load_competitors, load_keywords, load_projects, load_settings
from app.logging_setup import configure_logging
from app.paths import DATA_DIR, ensure_directories
from app.reports.excel_report import generate_report
from app.retention import apply_retention
from app.storage.database import Database

LOGGER = logging.getLogger("monitor")


def configured_targets(
    task: str, competitors: list, keywords: list, categories: list,
) -> dict[str, set[str]]:
    """Build the exact target manifest for one configured collection batch."""
    return {
        "product": {item.asin for item in competitors} if task in {"product", "all"} else set(),
        "search": {item.keyword for item in keywords} if task in {"search", "all"} else set(),
        "bestseller": {item.category_name for item in categories} if task in {"bestseller", "all"} else set(),
    }


def current_config_targets(project_id: str, task: str) -> dict[str, set[str]]:
    """Rebuild targets for legacy runs created before manifests were persisted."""
    return configured_targets(
        task,
        load_competitors(project_id) if task in {"product", "all"} else [],
        load_keywords(3, project_id) if task in {"search", "all"} else [],
        load_categories(project_id) if task in {"bestseller", "all"} else [],
    )


@contextmanager
def execution_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = DATA_DIR / "monitor.lock"
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有监控任务正在运行，本次任务已跳过") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def prevent_idle_sleep():
    """Keep the Mac awake after Python has started, without wrapping Python in caffeinate."""
    process = subprocess.Popen(
        ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        if process.poll() is None:
            process.terminate()


async def execute_project(task: str, project: Project, base_settings: dict) -> tuple[int, str]:
    settings = copy.deepcopy(base_settings)
    settings["amazon"]["postal_code"] = project.postal_code
    competitors = load_competitors(project.project_id)
    keywords = load_keywords(int(settings["search"].get("default_pages", 3)), project.project_id)
    categories = load_categories(project.project_id)
    task_counts = {"product": len(competitors), "search": len(keywords), "bestseller": len(categories)}
    total = sum(task_counts.values()) if task == "all" else task_counts[task]
    db = Database()
    run_id = db.start_run(project.project_id, task, total)
    success = failed = captcha = 0
    report_path = None
    try:
        db.record_targets(
            run_id, project.project_id,
            configured_targets(task, competitors, keywords, categories),
        )
        async with BrowserManager(settings) as browser:
            if task in {"product", "all"}:
                result = await collect_products(browser, db, run_id, project.project_id, competitors, settings)
                success += result[0]; failed += result[1]; captcha += result[2]
            if task in {"search", "all"}:
                result = await collect_searches(browser, db, run_id, project.project_id, keywords, settings)
                success += result[0]; failed += result[1]; captcha += result[2]
            if task in {"bestseller", "all"}:
                result = await collect_bestsellers(browser, db, run_id, project.project_id, categories, settings)
                success += result[0]; failed += result[1]; captcha += result[2]
                candidates = candidate_backlog(db, project.project_id, compare_bestsellers(db, run_id, project.project_id))
                await collect_candidate_details(browser, db, run_id, project.project_id, candidates, settings)
        compare_products(db, run_id, project.project_id, settings)
        compare_searches(db, run_id, project.project_id, settings)
        db.finish_run(run_id, success, failed, captcha)
        report_path = generate_report(db, run_id, project)
        db.finish_run(run_id, success, failed, captcha, report_path)
        LOGGER.info("项目 %s 运行结束: 成功=%d 失败=%d 报告=%s", project.project_name, success, failed, report_path)
        return (0 if failed == 0 else 2), run_id
    except Exception:
        LOGGER.exception("运行批次失败: %s", run_id)
        db.finish_run(run_id, success, max(failed, total - success), captcha, report_path)
        return 1, run_id


def failed_targets(
    db: Database, source_run_id: str, retries: int, seen: set[str] | None = None,
) -> dict[str, set[str]]:
    result = {"product": set(), "search": set(), "bestseller": set()}
    seen = set(seen or ())
    if source_run_id in seen:
        return result
    seen.add(source_run_id)
    runs = db.fetchall(
        "SELECT task_type,source_run_id,project_id,status,total_tasks FROM collection_runs WHERE run_id=?",
        (source_run_id,),
    )
    if not runs:
        return result
    outcomes = db.fetchall("SELECT task_type,target,success FROM collection_task_outcomes WHERE run_id=?", (source_run_id,))
    parent_run_id = runs[0].get("source_run_id")
    if runs[0]["task_type"] in {"retry", "auto_retry"} and parent_run_id:
        # 重试批次可能在浏览器初始化阶段整体失败，此时还没有逐项结果。
        # 先继承上一个批次的失败目标，再用本批次已经完成的结果逐项扣除。
        result = failed_targets(db, parent_run_id, retries, seen)
        for row in outcomes:
            kind, target = row["task_type"], row["target"]
            if kind not in result:
                continue
            if row["success"]:
                result[kind].discard(target)
            else:
                result[kind].add(target)
        return result
    if outcomes:
        for row in outcomes:
            if not row["success"] and row["task_type"] in result:
                result[row["task_type"]].add(row["target"])
        if len(outcomes) >= int(runs[0].get("total_tasks") or 0):
            return result
    # 断网等异常可能发生在首个页面创建/邮编初始化阶段。旧批次尚未保存目标清单时，
    # 运行汇总只有失败数量；从当前仍启用的项目配置重建，再扣除已成功目标。
    if runs[0]["status"] in {"partial", "failed"}:
        configured = current_config_targets(runs[0]["project_id"], runs[0]["task_type"])
        for kind, targets in configured.items():
            result[kind].update(targets)
        for row in outcomes:
            if row["success"] and row["task_type"] in result:
                result[row["task_type"]].discard(row["target"])
        if any(result.values()):
            return result
    # 兼容尚未记录逐项结果的历史批次。
    errors = db.fetchall(
        "SELECT task_type,target,MAX(retry_count) AS last_retry FROM collection_errors WHERE run_id=? GROUP BY task_type,target",
        (source_run_id,),
    )
    for row in errors:
        kind, target = row["task_type"], row["target"]
        if kind not in result or int(row["last_retry"] or 0) < retries:
            continue
        if kind == "product":
            succeeded = db.fetchall("SELECT 1 FROM product_snapshots WHERE run_id=? AND asin=? AND success=1 LIMIT 1", (source_run_id, target))
        elif kind == "bestseller":
            succeeded = db.fetchall("SELECT 1 FROM bestseller_snapshots WHERE run_id=? AND category_name=? LIMIT 1", (source_run_id, target))
        else:
            succeeded = []  # 搜索词只要有一页最终失败，整项就需要重试。
        if not succeeded:
            result[kind].add(target)
    return result


async def retry_run(source_run_id: str, task_type: str = "retry") -> int:
    ensure_directories()
    configure_logging()
    settings = load_settings()
    db = Database()
    apply_retention(db, settings)
    rows = db.fetchall("SELECT * FROM collection_runs WHERE run_id=?", (source_run_id,))
    if not rows:
        raise ValueError("找不到要重试的采集批次")
    source = rows[0]
    project = {item.project_id: item for item in load_projects()}.get(source["project_id"])
    if not project:
        raise ValueError("原批次所属项目不存在或未启用")
    targets = failed_targets(db, source_run_id, int(settings["amazon"].get("retries", 2)))
    competitors = [x for x in load_competitors(project.project_id) if x.asin in targets["product"]]
    keywords = [x for x in load_keywords(int(settings["search"].get("default_pages", 3)), project.project_id) if x.keyword in targets["search"]]
    categories = [x for x in load_categories(project.project_id) if x.category_name in targets["bestseller"]]
    total = len(competitors) + len(keywords) + len(categories)
    if total == 0:
        raise ValueError("该批次没有可重试的失败项；相关配置可能已被删除或停用")
    retry_id = db.start_run(project.project_id, task_type, total, source_run_id=source_run_id)
    success = failed = captcha = 0
    report_path = None
    retry_settings = copy.deepcopy(settings)
    retry_settings["amazon"]["postal_code"] = project.postal_code
    try:
        db.record_targets(retry_id, project.project_id, {
            "product": {item.asin for item in competitors},
            "search": {item.keyword for item in keywords},
            "bestseller": {item.category_name for item in categories},
        })
        async with BrowserManager(retry_settings) as browser:
            for collector, items in ((collect_products, competitors), (collect_searches, keywords), (collect_bestsellers, categories)):
                if items:
                    ok, bad, blocked = await collector(browser, db, retry_id, project.project_id, items, retry_settings)
                    success += ok; failed += bad; captcha += blocked
            if categories:
                candidates = candidate_backlog(db, project.project_id, compare_bestsellers(db, retry_id, project.project_id))
                await collect_candidate_details(browser, db, retry_id, project.project_id, candidates, retry_settings)
        compare_products(db, retry_id, project.project_id, retry_settings)
        compare_searches(db, retry_id, project.project_id, retry_settings)
        db.finish_run(retry_id, success, failed, captcha)
        report_path = generate_report(db, retry_id, project)
        db.finish_run(retry_id, success, failed, captcha, report_path)
        LOGGER.info("失败项重试结束: 原批次=%s 新批次=%s 成功=%d 失败=%d", source_run_id, retry_id, success, failed)
        return 0 if failed == 0 else 2
    except Exception:
        LOGGER.exception("失败项重试批次异常: %s", retry_id)
        db.finish_run(retry_id, success, max(failed, total - success), captcha, report_path)
        return 1


def unresolved_retry_source(db: Database, source_run_id: str) -> str | None:
    """Follow failed retry descendants, or stop if a sibling already resolved the source."""
    current = source_run_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        children = db.fetchall(
            "SELECT run_id,status FROM collection_runs WHERE source_run_id=? ORDER BY started_at DESC",
            (current,),
        )
        if not children:
            return current
        if any(row["status"] in {"running", "success"} for row in children):
            return None
        failed_children = [row for row in children if row["status"] in {"partial", "failed"}]
        if not failed_children:
            return current
        current = failed_children[0]["run_id"]
    return None


async def auto_retry_failed_runs(source_run_ids: list[str], settings: dict) -> list[int] | None:
    config = settings.get("auto_retry", {})
    if not source_run_ids or not bool(config.get("enabled", True)):
        return None
    delay_seconds = max(0, int(config.get("delay_seconds", 300)))
    LOGGER.warning(
        "本轮有 %d 个未完整批次，将在 %d 秒后自动精准重试一次",
        len(source_run_ids), delay_seconds,
    )
    await asyncio.sleep(delay_seconds)
    db = Database()
    retries = int(settings["amazon"].get("retries", 2))
    results: list[int] = []
    for original_run_id in source_run_ids:
        source_run_id = unresolved_retry_source(db, original_run_id)
        if source_run_id is None:
            LOGGER.info("批次 %s 已由其他重试处理，跳过自动重试", original_run_id)
            results.append(0)
            continue
        targets = failed_targets(db, source_run_id, retries)
        if not any(targets.values()):
            LOGGER.info("批次 %s 已无失败目标，跳过自动重试", source_run_id)
            results.append(0)
            continue
        try:
            LOGGER.info("开始自动精准重试批次 %s", source_run_id)
            results.append(await retry_run(source_run_id, task_type="auto_retry"))
        except Exception:
            LOGGER.exception("自动精准重试启动失败: %s", source_run_id)
            results.append(1)
    return results


async def execute(task: str, project_selector: str, scheduled: bool = False) -> int:
    ensure_directories()
    configure_logging()
    settings = load_settings()
    db = Database()
    apply_retention(db, settings)
    if scheduled:
        from app.notifications.collection_alerts import flush_pending_collection_alerts
        flush_pending_collection_alerts(db)
    projects = load_projects()
    selected = projects if project_selector == "all" else [item for item in projects if item.project_id == project_selector]
    if not selected:
        raise ValueError(f"未找到已启用的监控项目: {project_selector}")
    exit_codes: list[int] = []
    failed_run_ids: list[str] = []
    for project in selected:
        LOGGER.info("开始监控项目: %s (%s)", project.project_name, project.project_id)
        exit_code, run_id = await execute_project(task, project, settings)
        exit_codes.append(exit_code)
        if exit_code:
            failed_run_ids.append(run_id)
    auto_retry_results = await auto_retry_failed_runs(failed_run_ids, settings)
    if scheduled and failed_run_ids:
        from app.notifications.collection_alerts import (
            flush_pending_collection_alerts, queue_scheduled_collection_alerts,
        )
        queue_scheduled_collection_alerts(db, failed_run_ids)
        flush_pending_collection_alerts(db)
    if auto_retry_results is not None:
        return max(auto_retry_results, default=0)
    return max(exit_codes, default=0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon 美国站竞品监控器（前台页面采集）")
    parser.add_argument("task", choices=["product", "search", "bestseller", "all", "retry"], help="要执行的采集任务")
    parser.add_argument("--project", default="all", help="project_id，默认运行全部启用项目")
    parser.add_argument("--run-id", help="retry 时指定原采集批次 ID")
    parser.add_argument("--scheduled", action="store_true", help="标记为系统定时采集，用于失败通知")
    args = parser.parse_args()
    with execution_lock(), prevent_idle_sleep():
        if args.task == "retry":
            if not args.run_id:
                parser.error("retry 必须提供 --run-id")
            return asyncio.run(retry_run(args.run_id))
        return asyncio.run(execute(args.task, args.project, scheduled=args.scheduled))


if __name__ == "__main__":
    raise SystemExit(main())
