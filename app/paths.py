from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
REPORT_DIR = OUTPUT_DIR / "reports"
SCREENSHOT_DIR = OUTPUT_DIR / "screenshots"
HTML_DIR = OUTPUT_DIR / "html"
LOG_DIR = OUTPUT_DIR / "logs"
DB_PATH = DATA_DIR / "monitor.db"


def _safe_project_name(project_name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", str(project_name)).strip().strip(".") or "未命名项目"


def report_filename(project_name: str) -> str:
    return f"{_safe_project_name(project_name)}_Amazon竞品监控.xlsx"


def legacy_change_report_filename(project_name: str) -> str:
    return f"{_safe_project_name(project_name)}_竞品变化提醒.xlsx"


def ensure_directories() -> None:
    for directory in (
        CONFIG_DIR,
        DATA_DIR,
        REPORT_DIR,
        SCREENSHOT_DIR,
        HTML_DIR,
        LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
