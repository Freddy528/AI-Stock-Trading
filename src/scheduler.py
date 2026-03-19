"""定时交易信号调度器 - 基于 APScheduler"""
import sys
import os
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# 确保能 import 同目录模块
sys.path.insert(0, os.path.dirname(__file__))

from config import SIGNAL_SCHEDULE
from signal_generator import generate_signal
from summary_generator import generate_daily_summary


def _is_trading_day():
    """简单判断是否为交易日（周一到周五）"""
    return datetime.now().weekday() < 5


def run_signal_job(signal_type, label):
    """执行一次信号生成任务"""
    if not _is_trading_day():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 非交易日，跳过 {label}")
        return

    try:
        if signal_type == "daily_summary":
            filepath, content = generate_daily_summary()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {label}生成完毕: {filepath}")
        else:
            filepath, content = generate_signal(signal_type, label)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {label}信号生成完毕: {filepath}")
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {label}任务失败: {e}")


def start_scheduler():
    """启动定时调度器"""
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    for item in SIGNAL_SCHEDULE:
        hour = item["hour"]
        minute = item["minute"]
        signal_type = item["type"]
        label = item["label"]

        trigger = CronTrigger(
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
            timezone="Asia/Shanghai",
        )

        job_id = f"signal_{hour:02d}{minute:02d}_{signal_type}"
        scheduler.add_job(
            run_signal_job,
            trigger=trigger,
            args=[signal_type, label],
            id=job_id,
            name=f"{label}信号 {hour:02d}:{minute:02d}",
        )
        print(f"  已注册任务: {label} @ {hour:02d}:{minute:02d} ({signal_type})")

    print(f"\n{'='*60}")
    print(f"交易信号调度器已启动")
    print(f"共 {len(SIGNAL_SCHEDULE)} 个定时任务")
    print(f"信号输出目录: signals/YYYY-MM-DD/")
    print(f"按 Ctrl+C 停止")
    print(f"{'='*60}\n")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n调度器已停止")


def run_once(signal_type=None, label=None):
    """手动执行一次（用于测试）"""
    if signal_type == "daily_summary":
        return generate_daily_summary()
    if signal_type and label:
        return generate_signal(signal_type, label)

    # 根据当前时间自动判断类型
    now = datetime.now()
    hour, minute = now.hour, now.minute
    current_minutes = hour * 60 + minute

    if current_minutes < 9 * 60 + 30:
        return generate_signal("pre_market", "盘前")
    elif current_minutes >= 14 * 60 + 45:
        return generate_signal("pre_close", "盘尾")
    else:
        return generate_signal("intraday", "盘中")


def list_jobs():
    """列出所有定时任务"""
    print("\n定时任务列表：")
    print(f"  {'时间':>6}  {'类型':<12}  {'标签'}")
    print("  " + "-" * 36)
    for item in SIGNAL_SCHEDULE:
        print(f"  {item['hour']:02d}:{item['minute']:02d}  {item['type']:<12}  {item['label']}")
    print(f"\n  共 {len(SIGNAL_SCHEDULE)} 个定时任务\n")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="交易信号定时调度器")
    sub = parser.add_subparsers(dest="command")

    # python scheduler.py daemon — 启动常驻调度
    sub.add_parser("daemon", help="启动定时调度（常驻运行）")

    # python scheduler.py now [--type TYPE]  — 立即生成一次
    now_parser = sub.add_parser("now", help="立即生成一次信号")
    now_parser.add_argument("--type", choices=["pre_market", "intraday", "pre_close", "daily_summary"],
                            help="指定信号类型，不指定则根据当前时间自动判断")
    now_parser.add_argument("--label", help="信号标签")

    # python scheduler.py list — 列出任务
    sub.add_parser("list", help="列出所有定时任务")

    args = parser.parse_args()

    if args.command == "daemon":
        start_scheduler()
    elif args.command == "now":
        run_once(args.type, args.label)
    elif args.command == "list":
        list_jobs()
    else:
        parser.print_help()
        print("\n示例:")
        print("  python scheduler.py daemon      # 启动定时调度")
        print("  python scheduler.py now          # 立即生成一次信号")
        print("  python scheduler.py now --type pre_market  # 生成盘前信号")
        print("  python scheduler.py list         # 列出所有任务")
