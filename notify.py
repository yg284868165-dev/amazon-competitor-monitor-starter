from __future__ import annotations

import argparse
from datetime import date

from app.notifications.serverchan import send_daily, send_test, send_weekly


def main() -> int:
    parser = argparse.ArgumentParser(description="Amazon竞品监控微信摘要")
    parser.add_argument("command", choices=["daily", "weekly", "test"])
    parser.add_argument("--date", help="日报汇总日期或周报截止周日 YYYY-MM-DD")
    args = parser.parse_args()
    if args.command == "test":
        result = send_test()
    elif args.command == "weekly":
        result = send_weekly(date.fromisoformat(args.date) if args.date else None)
    else:
        result = send_daily(date.fromisoformat(args.date) if args.date else None)
    print(result["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
