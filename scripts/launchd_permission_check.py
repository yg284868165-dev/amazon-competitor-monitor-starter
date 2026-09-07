import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.notifications.serverchan import get_sendkey
from app.paths import CONFIG_DIR, DB_PATH, ROOT


def main() -> None:
    (ROOT / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    (CONFIG_DIR / "settings.json").read_text(encoding="utf-8")
    if not DB_PATH.is_file():
        raise RuntimeError("监控数据库不存在")
    if not get_sendkey():
        raise RuntimeError("无法从 macOS 钥匙串读取 SendKey")
    print("launchd 桌面权限和钥匙串读取测试通过")


if __name__ == "__main__":
    main()
