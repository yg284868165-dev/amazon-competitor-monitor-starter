from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path("/Library/Application Support/AmazonCompetitorMonitor")
PLIST_PATH = Path("/Library/LaunchDaemons/com.amazon.competitor-monitor.wake.plist")
LABEL = "com.amazon.competitor-monitor.wake"


def install(helper_source: str, config_source: str, plist_source: str, python: str) -> None:
    old_helper = BASE_DIR / "wake_helper.py"
    if old_helper.exists():
        subprocess.run([python, str(old_helper), "cancel"], capture_output=True)
    subprocess.run(["/bin/launchctl", "bootout", "system", str(PLIST_PATH)], capture_output=True)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    Path("/Library/Logs/AmazonCompetitorMonitor").mkdir(parents=True, exist_ok=True)
    shutil.copy2(helper_source, old_helper)
    shutil.copy2(config_source, BASE_DIR / "wake.json")
    shutil.copy2(plist_source, PLIST_PATH)
    subprocess.run([python, str(old_helper), "seed"], check=True)
    subprocess.run(["/bin/launchctl", "bootstrap", "system", str(PLIST_PATH)], check=True)


def uninstall(python: str) -> None:
    helper = BASE_DIR / "wake_helper.py"
    if helper.exists():
        subprocess.run([python, str(helper), "cancel"], capture_output=True)
    subprocess.run(["/bin/launchctl", "bootout", "system", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.unlink(missing_ok=True)
    if BASE_DIR.exists():
        shutil.rmtree(BASE_DIR)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "install":
        install(*sys.argv[2:6])
    elif len(sys.argv) == 3 and sys.argv[1] == "uninstall":
        uninstall(sys.argv[2])
    else:
        raise SystemExit("wake_admin.py 参数无效")
