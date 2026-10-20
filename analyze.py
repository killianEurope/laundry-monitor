"""Build README.md (usage report for the last 7 and 30 days) from data/*.csv."""
import csv
import glob
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
TZ = ZoneInfo("Europe/Paris")
BUSY = {2, 4, 6, 11, 12, 13, 14}  # occupied / running / countdown / waiting (see status page JS)
DOWN = {0, 3, 5}                   # error / out of order / not available
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
HOURS = range(7, 23)
POLL_MINUTES = 5
MIN_SAMPLES = 3  # a cell needs this many polls to be recommended as a quiet slot


def group_of(type_name):
    t = type_name.lower()
    if "18" in t:
        return "洗衣机 18kg"
    if "8kg" in t:
        return "洗衣机 8kg"
    if "che" in t and "linge" in t:
        return "烘干机"
    if "lessive" in t:
        return "洗衣液"
    if "adoucissant" in t:
        return "柔顺剂"
    return type_name


MAIN_GROUPS = ["洗衣机 8kg", "洗衣机 18kg", "烘干机"]


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def read_rows(pattern):
    rows = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, pattern))):
        with open(path, encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    return rows


def load():
    polls = sorted(parse_ts(r["ts_utc"]) for r in read_rows("polls-*.csv"))
    changes = defaultdict(list)  # machine_no -> [(ts, status, virtu_off)]
    types = {}
    for r in read_rows("changes-*.csv"):
        changes[r["machine_no"]].append((parse_ts(r["ts_utc"]), int(r["status"]), r["virtu_off"]))
        types[r["machine_no"]] = r["type"]
    for v in changes.values():
        v.sort()
    return polls, changes, types


def states_at_polls(polls, changes):
    """machine_no -> list of status aligned with polls (None before first record)."""
    out = {}
    for no, ch in changes.items():
        seq, i, cur = [], 0, None
        for p in polls:
            while i < len(ch) and ch[i][0] <= p:
                cur = ch[i][1]
                i += 1
            seq.append(cur)
        out[no] = seq
    return out


def cycle_events(changes):
    """(machine_no, first_seen_ts) for every new virtu_off value after the first one seen."""
    ev = []
    for no, ch in changes.items():
        prev = None
        for ts, _, vo in ch:
            if prev is not None and vo and vo != prev:
                ev.append((no, ts))
            if vo:
                prev = vo
    return ev


def shade(p):
    if p is None:
        return "·"
    icon = "🟩" if p < 0.25 else "🟨" if p < 0.5 else "🟧" if p < 0.75 else "🟥"
    return f"{icon}{round(p * 100)}"


def section(days, now, polls, states, types, events, open_hours):
    start = now - timedelta(days=days)
    idx = [k for k, p in enumerate(polls) if p > start]
    lines = [f"## 最近 {days} 天", ""]
    if not idx:
        return lines + ["暂无数据。", ""]

    first = polls[idx[0]]
    expected = (now - max(start, first)).total_seconds() / 60 / POLL_MINUTES + 1
    lines.append(
        f"数据范围：{first.astimezone(TZ):%m-%d %H:%M} → {now.astimezone(TZ):%m-%d %H:%M}（巴黎时间），"
        f"采样 {len(idx)} 次（覆盖率约 {min(1, len(idx) / expected):.0%}）"
    )
    if first - start > timedelta(hours=12):
        lines.append(f"> ⚠️ 记录开始不足 {days} 天，以下只统计已有数据。")
    lines.append("")

    # cycles per machine and per group
    cyc = defaultdict(int)
    daily = defaultdict(lambda: defaultdict(int))
    for no, ts in events:
        if ts > start:
            cyc[no] += 1
            daily[ts.astimezone(TZ).date()][group_of(types[no])] += 1

    # busy share per machine
    busy_share = {}
    for no, seq in states.items():
        vals = [seq[k] for k in idx if seq[k] is not None and seq[k] not in DOWN]
        busy_share[no] = sum(v in BUSY for v in vals) / len(vals) if vals else None

    lines += ["### 每台机器", "", "| 编号 | 类型 | 使用次数 | 占用率 |", "|---:|---|---:|---:|"]
    for no in sorted(states, key=int):
        b = busy_share[no]
        lines.append(f"| {no} | {types[no]} | {cyc[no]} | {'-' if b is None else f'{b:.0%}'} |")
    lines.append("")

    groups = [g for g in MAIN_GROUPS if any(group_of(t) == g for t in types.values())]
    if daily:
        lines += ["### 每天使用次数", "", "| 日期 | " + " | ".join(groups) + " |", "|---|" + "---:|" * len(groups)]
        for d in sorted(daily):
            lines.append(f"| {d:%m-%d} {WEEKDAYS[d.weekday()]} | " + " | ".join(str(daily[d][g]) for g in groups) + " |")
        lines.append("")

    # weekday x hour heatmap of share of machines busy, per group
    for g in groups:
        members = [no for no, t in types.items() if group_of(t) == g]
        acc = defaultdict(lambda: [0.0, 0])
        for k in idx:
            vals = [states[no][k] for no in members if states[no][k] is not None and states[no][k] not in DOWN]
            if vals:
                local = polls[k].astimezone(TZ)
                cell = acc[(local.weekday(), local.hour)]
                cell[0] += sum(v in BUSY for v in vals) / len(vals)
                cell[1] += 1
        avg = {key: s / n for key, (s, n) in acc.items()}
        lines += [f"### {g}：各时段平均占用率（%）", "", "| 时 | " + " | ".join(WEEKDAYS) + " |", "|---|" + ":-:|" * 7]
        for h in HOURS:
            lines.append(f"| {h:02d} | " + " | ".join(shade(avg.get((d, h))) for d in range(7)) + " |")
        best = sorted(
            (s / n, -n, d, h) for (d, h), (s, n) in acc.items() if h in open_hours and n >= MIN_SAMPLES
        )[:5]
        if best:
            lines.append("")
            lines.append(
                f"最空的时段（有人用过的钟点里、样本 ≥{MIN_SAMPLES} 次）："
                + "，".join(f"{WEEKDAYS[d]} {h:02d}点（{v:.0%}，{-n}次）" for v, n, d, h in best)
            )
        lines.append("")
    return lines


def main():
    polls, changes, types = load()
    out = [
        "# Laverie Richter 机器使用统计",
        "",
        "数据由 GitHub Actions 每 5 分钟查询一次 status.wi-line.fr 的公开状态接口，统计自动更新。",
        "占用率 = 采样时处于使用中的比例；使用次数 = 接口里 virtu_off 时间戳出现新值的次数（每开始一轮更新一次）。",
        "🟩 <25% 🟨 25–50% 🟧 50–75% 🟥 ≥75%　· = 无数据",
        "",
    ]
    if not polls:
        out.append("还没有数据。")
    else:
        now = polls[-1]
        states = states_at_polls(polls, changes)
        events = cycle_events(changes)
        # hours (Paris) in which any machine was ever seen busy; the others are probably closing hours
        open_hours = {
            polls[k].astimezone(TZ).hour for k in range(len(polls)) if any(seq[k] in BUSY for seq in states.values())
        }
        closed = [h for h in HOURS if h not in open_hours]
        if closed:
            out.append(f"> 以下钟点从没观察到有人使用（可能已关门），不参与推荐：{'、'.join(f'{h:02d}点' for h in closed)}")
            out.append("")
        out.append(f"_更新于 {datetime.now(timezone.utc).astimezone(TZ):%Y-%m-%d %H:%M}（巴黎时间）_")
        out.append("")
        for days in (7, 30):
            out += section(days, now, polls, states, types, events, open_hours)
    with open(os.path.join(ROOT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("README.md written")


if __name__ == "__main__":
    main()
