from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from app.notifications.serverchan import has_sendkey, load_notification_config
from app.paths import LOG_DIR, ROOT, ensure_directories

DAILY_LABEL = "com.amazon.competitor-monitor.notification"
WEEKLY_LABEL = "com.amazon.competitor-monitor.weekly-notification"
DAILY_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{DAILY_LABEL}.plist"
WEEKLY_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{WEEKLY_LABEL}.plist"


def plist_data(config: dict, report_type: str = "daily") -> dict:
    if report_type not in {"daily", "weekly"}:
        raise ValueError("未知简报类型")
    if report_type == "daily":
        label = DAILY_LABEL
        hour, minute = (int(value) for value in config["time"].split(":"))
        calendar = {"Hour": hour, "Minute": minute}
        stdout_name, stderr_name = "notification.stdout.log", "notification.stderr.log"
    else:
        label = WEEKLY_LABEL
        calendar = {"Weekday": 1, "Hour": 10, "Minute": 0}
        stdout_name, stderr_name = "weekly-notification.stdout.log", "weekly-notification.stderr.log"
    return {
        "Label": label,
        "ProgramArguments": [
            str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "notify.py"), report_type,
        ],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": calendar,
        "RunAtLoad": False,
        "StandardOutPath": str(LOG_DIR / stdout_name),
        "StandardErrorPath": str(LOG_DIR / stderr_name),
        "ProcessType": "Interactive",
    }


def _install_agent(domain: str, path: Path, value: dict) -> None:
    with path.open("wb") as handle:
        plistlib.dump(value, handle, sort_keys=False)
    subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)


def install() -> None:
    ensure_directories()
    config = load_notification_config()
    if not config["enabled"]:
        raise RuntimeError("请先启用微信通知配置")
    if not has_sendkey():
        raise RuntimeError("请先保存 Server酱 SendKey")
    DAILY_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _install_agent(domain, DAILY_PLIST_PATH, plist_data(config, "daily"))
    _install_agent(domain, WEEKLY_PLIST_PATH, plist_data(config, "weekly"))
    print(f"微信日报定时任务已安装：每天 {config['time']}")
    print("微信周报定时任务已安装：每周一 10:00")


def uninstall() -> None:
    domain = f"gui/{os.getuid()}"
    for path in (DAILY_PLIST_PATH, WEEKLY_PLIST_PATH):
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    print("微信日报和周报定时任务已卸载；SendKey和配置未删除")


def status() -> None:
    config = load_notification_config()
    daily = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{DAILY_LABEL}"], capture_output=True, text=True)
    weekly = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{WEEKLY_LABEL}"], capture_output=True, text=True)
    print("配置启用:", bool(config["enabled"]))
    print("日报时间:", f"每天 {config['time']}")
    print("周报时间: 每周一 10:00")
    print("SendKey:", "已安全保存" if has_sendkey() else "未保存")
    print("日报状态:", "已安装" if daily.returncode == 0 else "未安装")
    print("周报状态:", "已安装" if weekly.returncode == 0 else "未安装")
    print("系统状态:", "已安装" if daily.returncode == 0 and weekly.returncode == 0 else "未完整安装")


def main() -> int:
    command = argparse.ArgumentParser(description="Server酱微信日报与周报定时任务")
    command.add_argument("command", choices=["install", "uninstall", "status"])
    value = command.parse_args().command
    if value == "install":
        install()
    elif value == "uninstall":
        uninstall()
    else:
        status()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"微信通知定时任务操作失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
