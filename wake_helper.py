from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path("/Library/Application Support/AmazonCompetitorMonitor")
CONFIG_PATH = BASE_DIR / "wake.json"
STATE_PATH = BASE_DIR / "wake-state.json"
OWNER = "com.amazon.competitor-monitor.wake"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _wake_datetimes(day: datetime) -> list[datetime]:
    config = _load(CONFIG_PATH, {})
    lead = int(config.get("lead_minutes", 5))
    result = []
    for value in config.get("times", []):
        hour, minute = map(int, value.split(":"))
        run_at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        result.append(run_at - timedelta(minutes=lead))
    return result


def _event_text(value: datetime) -> str:
    return value.strftime("%m/%d/%y %H:%M:%S")


def schedule_days(offsets: list[int]) -> None:
    now = datetime.now()
    state = _load(STATE_PATH, {"events": []})
    known = set(state.get("events", []))
    for offset in offsets:
        day = now + timedelta(days=offset)
        for wake_at in _wake_datetimes(day):
            if wake_at <= now or _event_text(wake_at) in known:
                continue
            event = _event_text(wake_at)
            subprocess.run(["/usr/bin/pmset", "schedule", "wakeorpoweron", event, OWNER], check=True)
            known.add(event)
    cutoff = now - timedelta(days=1)
    state["events"] = sorted(event for event in known if datetime.strptime(event, "%m/%d/%y %H:%M:%S") > cutoff)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cancel() -> None:
    state = _load(STATE_PATH, {"events": []})
    for event in state.get("events", []):
        subprocess.run(["/usr/bin/pmset", "schedule", "cancel", "wakeorpoweron", event, OWNER], capture_output=True)
    STATE_PATH.unlink(missing_ok=True)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if command == "seed":
        cancel()
        schedule_days([0, 1, 2])
    elif command == "daily":
        schedule_days([2])
    elif command == "cancel":
        cancel()
    else:
        raise SystemExit(f"未知命令: {command}")


if __name__ == "__main__":
    main()
