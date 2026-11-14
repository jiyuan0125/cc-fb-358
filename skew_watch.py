#!/usr/bin/env python3
"""skew_watch.py — 设备条数倾斜监控。

输入：
  counts/<YYYY-MM-DD>.json   每行 {"dev","count","state"}，state ∈ ACTIVE/SILENT/BURST/RETURN
  watch_conf.json            {"share_threshold_pct": N, "ratio_threshold": M}

输出：
  watch.csv                  日行 dev,date,share,ratio,alarm,withdrawn
                             其后附周计数行 dev,周窗标号,周合计条数
  stdout                     五项报告：检测窗覆盖 / 设备总数 / 告警数 / 撤销数 / 周计数

判定口径：
  * share = 设备当日条数 / 当日全部设备条数之和 × 100
  * ratio = 当日条数 / 前一日条数
  * 双阈值同时成立（含等于）才算倾斜，比较用精确有理数，避免 10.0/3.0 这类边界被浮点误差判掉
  * 检测窗首日无前日，不参与判定，ratio 留空
  * BURST 标记有效期：自 BURST 当日起，RETURN 到达即终止（RETURN 当日只判撤销，不判告警）
  * 有效期内双阈值同时成立记 alarm=1
  * RETURN 到达当日撤销仍成立的告警，withdrawn=1；撤销后历史行保留，后续新 BURST 重新起算
  * 前一日条数为 0 时环比无定义，ratio 留空且不参与判定
  * 周窗以周日收尾（ISO 周，周一起），标号取收尾周日所属年份与自然周号，如 2026-W43
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

STATES = {"ACTIVE", "SILENT", "BURST", "RETURN"}
HEADER = ["dev", "date", "share", "ratio", "alarm", "withdrawn"]


def die(msg: str) -> "NoReturn":  # noqa: F821
    sys.stderr.write(f"skew_watch: {msg}\n")
    raise SystemExit(2)


def load_counts(counts_dir: Path) -> dict[date, dict[str, dict]]:
    """读取 counts 目录，返回 {日期: {dev: {"count": int, "state": str}}}。"""
    days: dict[date, dict[str, dict]] = {}
    for path in sorted(counts_dir.glob("*.json")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            sys.stderr.write(f"skew_watch: 跳过非日期文件 {path.name}\n")
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            die(f"无法读取 {path.name}: {exc}")
        if not isinstance(rows, list):
            die(f"{path.name} 顶层应为数组，实际为 {type(rows).__name__}")

        per_dev: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict) or not {"dev", "count", "state"} <= row.keys():
                die(f"{path.name} 存在字段不全的行: {row!r}")
            dev, count, state = row["dev"], row["count"], row["state"]
            if state not in STATES:
                die(f"{path.name} {dev} 状态非法: {state!r}")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                die(f"{path.name} {dev} 条数非法: {count!r}")
            if dev in per_dev:
                die(f"{path.name} 设备重复登记: {dev}")
            per_dev[dev] = {"count": count, "state": state}
        if not per_dev:
            continue
        days[day] = per_dev
    if not days:
        die(f"{counts_dir} 下没有可用的日文件")
    return days


def load_conf(conf_path: Path) -> tuple[Fraction, Fraction]:
    try:
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"无法读取配置 {conf_path}: {exc}")
    missing = {"share_threshold_pct", "ratio_threshold"} - set(conf)
    if missing:
        die(f"配置缺少字段: {', '.join(sorted(missing))}")
    try:
        return (
            Fraction(str(conf["share_threshold_pct"])),
            Fraction(str(conf["ratio_threshold"])),
        )
    except (ValueError, ZeroDivisionError) as exc:
        die(f"配置阈值非法: {exc}")


def check_gaps(days: list[date]) -> None:
    """检测窗应连续，缺口会破坏环比与周窗，直接报错。"""
    for prev, cur in zip(days, days[1:]):
        if (cur - prev).days != 1:
            die(f"检测窗存在缺口: {prev} 之后是 {cur}")


def fmt(value: Fraction) -> str:
    return f"{float(value):.2f}"


def build(days, data_by_day, share_thr: Fraction, ratio_thr: Fraction):
    """返回 (日行, 周行, 统计)。"""
    daily: list[list] = []
    episodes: dict[str, dict[str, bool]] = defaultdict(lambda: {"open": False, "standing": False})
    alarm_rows = 0
    withdrawn_rows = 0

    week_totals: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for idx, day in enumerate(days):
        per_dev = data_by_day[day]
        total = sum(r["count"] for r in per_dev.values())
        prev_day = days[idx - 1] if idx > 0 else None

        for dev in sorted(per_dev):
            rec = per_dev[dev]
            count, state = rec["count"], rec["state"]
            ep = episodes[dev]

            share = Fraction(count * 100, total) if total else Fraction(0)
            ratio = None
            prev_rec = data_by_day[prev_day].get(dev) if prev_day else None
            if prev_rec is not None and prev_rec["count"] > 0:
                ratio = Fraction(count, prev_rec["count"])

            alarm = 0
            withdrawn = 0

            if state == "RETURN":
                # RETURN 到达：撤销仍成立的告警并结束本期有效期
                if ep["standing"]:
                    withdrawn = 1
                ep["open"] = False
                ep["standing"] = False
            else:
                if state == "BURST":
                    ep["open"] = True
                if ep["open"] and ratio is not None:
                    if share >= share_thr and ratio >= ratio_thr:
                        alarm = 1
                        ep["standing"] = True

            alarm_rows += alarm
            withdrawn_rows += withdrawn
            daily.append([
                dev, day.isoformat(), fmt(share),
                "" if ratio is None else fmt(ratio),
                alarm, withdrawn,
            ])

            iso = day.isocalendar()
            week_totals[(iso[0], iso[1])][dev] += count

    weekly: list[list] = []
    for (iso_year, iso_week) in sorted(week_totals):
        label = f"{iso_year}-W{iso_week:02d}"
        for dev in sorted(week_totals[(iso_year, iso_week)]):
            weekly.append([dev, label, week_totals[(iso_year, iso_week)][dev]])

    stats = {
        "alarm_rows": alarm_rows,
        "withdrawn_rows": withdrawn_rows,
        "week_rows": len(weekly),
        "week_labels": sorted({f"{y}-W{w:02d}" for y, w in week_totals}),
    }
    return daily, weekly, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="设备条数倾斜监控")
    ap.add_argument("--counts", default="counts", help="日计数目录（默认 counts）")
    ap.add_argument("--conf", default="watch_conf.json", help="阈值配置（默认 watch_conf.json）")
    ap.add_argument("--out", default="watch.csv", help="输出 CSV（默认 watch.csv）")
    args = ap.parse_args()

    data_by_day = load_counts(Path(args.counts))
    share_thr, ratio_thr = load_conf(Path(args.conf))
    days = sorted(data_by_day)
    check_gaps(days)

    daily, weekly, stats = build(days, data_by_day, share_thr, ratio_thr)

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(daily)
        writer.writerows(weekly)

    devices = {dev for day in days for dev in data_by_day[day]}
    print(f"检测窗覆盖: {days[0].isoformat()} ~ {days[-1].isoformat()} ({len(days)} 天)")
    print(f"设备总数: {len(devices)}")
    print(f"告警数: {stats['alarm_rows']}")
    print(f"撤销数: {stats['withdrawn_rows']}")
    print(f"周计数: {stats['week_rows']} 行 ({len(stats['week_labels'])} 个周窗)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
