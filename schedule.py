from __future__ import annotations

import argparse
import fcntl
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.paths import CONFIG_DIR, DATA_DIR, LOG_DIR, ROOT, ensure_directories

LABEL = "com.amazon.competitor-monitor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SCHEDULE_PATH = CONFIG_DIR / "schedule.json"
WAKE_LABEL = f"{LABEL}.wake"
WAKE_PLIST_PATH = Path("/Library/LaunchDaemons") / f"{WAKE_LABEL}.plist"
WAKE_BASE_DIR = Path("/Library/Application Support/AmazonCompetitorMonitor")
TEST_LABEL = f"{LABEL}.permission-test"
TEST_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{TEST_LABEL}.plist"


def load_schedule() -> dict:
    with SCHEDULE_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    times = config.get("times") or []
    if not times:
        raise ValueError("schedule.json 至少需要一个执行时间")
    parsed = []
    for value in dict.fromkeys(times):
        match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(value))
        if not match:
            raise ValueError(f"无效时间 {value}，必须使用 HH:MM，例如 08:30")
        parsed.append({"Hour": int(match.group(1)), "Minute": int(match.group(2))})
    config["calendar"] = parsed
    if config.get("task", "all") not in {"product", "search", "bestseller", "all"}:
        raise ValueError("schedule.json 的 task 无效")
    return config


def plist_data(config: dict) -> dict:
    python = ROOT / ".venv" / "bin" / "python"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python), str(ROOT / "run.py"), config.get("task", "all"),
            "--project", config.get("project", "all"), "--scheduled",
        ],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": config["calendar"],
        "RunAtLoad": False,
        "StandardOutPath": str(LOG_DIR / "scheduler.stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "scheduler.stderr.log"),
        "ProcessType": "Interactive",
    }


def install_agent(config: dict) -> None:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(plist_data(config), handle, sort_keys=False)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_PATH)], check=True)


def _administrator(command: list[str]) -> None:
    shell_command = shlex.join(command).replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["/usr/bin/osascript", "-e", f'do shell script "{shell_command}" with administrator privileges'], check=True)


def _wake_plist_data(python: str) -> dict:
    helper = WAKE_BASE_DIR / "wake_helper.py"
    log_dir = Path("/Library/Logs/AmazonCompetitorMonitor")
    return {
        "Label": WAKE_LABEL,
        "ProgramArguments": [python, str(helper), "daily"],
        "StartCalendarInterval": {"Hour": 0, "Minute": 10},
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / "wake.stdout.log"),
        "StandardErrorPath": str(log_dir / "wake.stderr.log"),
    }


def install_wake(config: dict) -> None:
    python = str((ROOT / ".venv" / "bin" / "python").resolve())
    with tempfile.TemporaryDirectory(prefix="amazon-wake-", dir="/tmp") as temp_dir:
        admin_path = Path(temp_dir) / "wake_admin.py"
        helper_path = Path(temp_dir) / "wake_helper.py"
        config_path = Path(temp_dir) / "wake.json"
        plist_path = Path(temp_dir) / "wake.plist"
        shutil.copy2(ROOT / "wake_admin.py", admin_path)
        shutil.copy2(ROOT / "wake_helper.py", helper_path)
        config_path.write_text(json.dumps({"times": config["times"], "lead_minutes": config.get("wake_lead_minutes", 5)}, ensure_ascii=False), encoding="utf-8")
        with plist_path.open("wb") as handle:
            plistlib.dump(_wake_plist_data(python), handle, sort_keys=False)
        _administrator([python, str(admin_path), "install", str(helper_path), str(config_path), str(plist_path), python])


def uninstall_wake() -> None:
    python = str((ROOT / ".venv" / "bin" / "python").resolve())
    with tempfile.TemporaryDirectory(prefix="amazon-wake-", dir="/tmp") as temp_dir:
        admin_path = Path(temp_dir) / "wake_admin.py"
        shutil.copy2(ROOT / "wake_admin.py", admin_path)
        _administrator([python, str(admin_path), "uninstall", python])


def install() -> None:
    ensure_directories()
    config = load_schedule()
    if not config.get("enabled"):
        raise RuntimeError("schedule.json 中 enabled=false；确认时间后改为 true 再安装")
    lock_path = DATA_DIR / "monitor.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("监控任务正在运行，请等待本次采集结束后再安装或更新定时任务") from None
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                pass
    install_agent(config)
    if config.get("wake_enabled"):
        install_wake(config)
    print(f"已安装: {PLIST_PATH}")
    print("执行时间:", ", ".join(config["times"]))
    print("自动唤醒:", f"已启用（提前 {config.get('wake_lead_minutes', 5)} 分钟）" if config.get("wake_enabled") else "未启用")
    if config.get("wake_enabled"):
        print("可靠运行提醒: 保持用户登录，请使用睡眠而非关机或退出登录；MacBook 建议连接电源并保持开盖。")
        print("下一步: 运行 .venv/bin/python schedule.py permission-test")
        print("然后运行: .venv/bin/python schedule.py status，确认系统状态和自动唤醒均为已安装。")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    if WAKE_PLIST_PATH.exists():
        uninstall_wake()
    print("定时任务已卸载；配置和历史数据未删除")


def status() -> None:
    config = load_schedule()
    print("配置启用:", bool(config.get("enabled")))
    print("执行时间:", ", ".join(config["times"]))
    print("任务:", config.get("task", "all"), "项目:", config.get("project", "all"))
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    print("系统状态:", "已安装" if result.returncode == 0 else "未安装")
    wake_result = subprocess.run(["launchctl", "print", f"system/{WAKE_LABEL}"], capture_output=True, text=True)
    print("自动唤醒:", "已安装" if wake_result.returncode == 0 else "未安装", f"（提前 {config.get('wake_lead_minutes', 5)} 分钟）" if config.get("wake_enabled") else "")


def run_now() -> int:
    config = load_schedule()
    command = [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "run.py"), config.get("task", "all"), "--project", config.get("project", "all")]
    return subprocess.run(command, cwd=ROOT).returncode


def reload_agent() -> None:
    config = load_schedule()
    if not config.get("enabled"):
        raise RuntimeError("定时采集配置尚未启用")
    install_agent(config)
    print("采集 LaunchAgent 已重新加载（自动唤醒配置未改动）")


def permission_test() -> None:
    ensure_directories()
    python = ROOT / ".venv" / "bin" / "python"
    test_stdout = LOG_DIR / "launchd-permission-test.stdout.log"
    test_stderr = LOG_DIR / "launchd-permission-test.stderr.log"
    test_stdout.unlink(missing_ok=True)
    test_stderr.unlink(missing_ok=True)
    value = {
        "Label": TEST_LABEL,
        "ProgramArguments": [str(python), str(ROOT / "scripts" / "launchd_permission_check.py")],
        "WorkingDirectory": str(ROOT), "RunAtLoad": False,
        "StandardOutPath": str(test_stdout),
        "StandardErrorPath": str(test_stderr),
        "ProcessType": "Interactive",
    }
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{TEST_LABEL}"
    TEST_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TEST_PLIST_PATH.open("wb") as handle:
        plistlib.dump(value, handle, sort_keys=False)
    try:
        subprocess.run(["launchctl", "bootout", domain, str(TEST_PLIST_PATH)], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(TEST_PLIST_PATH)], check=True)
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True)
        result = None
        for _ in range(20):
            time.sleep(0.5)
            result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
            if "active count = 0" in result.stdout and "runs = 1" in result.stdout:
                break
        stdout = test_stdout.read_text(encoding="utf-8") if test_stdout.exists() else ""
        stderr = test_stderr.read_text(encoding="utf-8") if test_stderr.exists() else ""
        if not result or "last exit code = 0" not in result.stdout:
            raise RuntimeError((stderr or result.stdout if result else "测试任务无状态").strip())
        print(stdout.strip() or "launchd 权限测试通过")
    finally:
        subprocess.run(["launchctl", "bootout", domain, str(TEST_PLIST_PATH)], capture_output=True)
        TEST_PLIST_PATH.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon 竞品监控 macOS 定时任务管理")
    parser.add_argument("command", choices=["install", "uninstall", "status", "run-now", "reload-agent", "permission-test"])
    command = parser.parse_args().command
    if command == "install":
        install()
    elif command == "uninstall":
        uninstall()
    elif command == "status":
        status()
    elif command == "run-now":
        return run_now()
    elif command == "reload-agent":
        reload_agent()
    else:
        permission_test()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"定时任务操作失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
