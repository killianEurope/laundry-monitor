"""Build README.md (usage report for the last 7 and 30 days) from data/*.csv."""
import csv
import glob
import os
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
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

# EUR incl. VAT. Washers and dryer come from the shop's price list; detergent and softener are guesses.
PRICES = {"洗衣机 8kg": 5.5, "洗衣机 18kg": 11.0, "洗衣液": 1.0, "柔顺剂": 0.5}
DRYER_EUR_PER_10MIN = 2.0
VAT = 0.20


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


def dryer_minutes_per_event(polls, states, types, events):
    """Average dryer minutes paid per payment.

    Over the last 7 days, busy dryer-minutes per minute is measured only where polls are at most
    10 min apart, then divided by payments per minute over the whole 7 days. Pairing busy time
    with the payments seen in the same short windows would undercount sessions cut off when a
    run ends; using older, sparser weeks would undercount payments.
    """
    dryers = [no for no, t in types.items() if group_of(t) == "烘干机"]
    start = polls[-1] - timedelta(days=7)
    busy = dense_min = 0.0
    for k in range(1, len(polls)):
        gap = (polls[k] - polls[k - 1]).total_seconds() / 60
        if polls[k - 1] > start and gap <= 10:
            dense_min += gap
            busy += gap * sum(states[no][k - 1] in BUSY for no in dryers)
    span = (polls[-1] - max(start, polls[0])).total_seconds() / 60
    n = sum(1 for no, ts in events if no in dryers and ts > start)
    return (busy / dense_min) / (n / span) if dense_min and n else 20.0


def revenue(counts, minutes_per_dryer_event):
    """counts: group -> number of uses. Returns (EUR incl. VAT, dryer minutes)."""
    dryer_min = counts.get("烘干机", 0) * minutes_per_dryer_event
    eur = sum(PRICES.get(g, 0) * n for g, n in counts.items()) + dryer_min / 10 * DRYER_EUR_PER_10MIN
    return eur, dryer_min


def weekly_revenue(polls, types, events, mpe):
    first, last = polls[0].astimezone(TZ), polls[-1].astimezone(TZ)
    weeks = defaultdict(lambda: defaultdict(int))
    for no, ts in events:
        d = ts.astimezone(TZ).date()
        weeks[d - timedelta(days=d.weekday())][group_of(types[no])] += 1
    lines = [
        "## 每周收入估算",
        "",
        f"价目表：8kg 洗衣 {PRICES['洗衣机 8kg']} €、18kg 洗衣 {PRICES['洗衣机 18kg']} €、烘干 {DRYER_EUR_PER_10MIN} €/10 分钟；"
        f"洗衣液 {PRICES['洗衣液']} €、柔顺剂 {PRICES['柔顺剂']} € 是假设价。"
        f"烘干时长按实测每次付款平均 {mpe:.0f} 分钟估算。价格均含 20% 增值税。",
        "",
        "| 周（周一–周日） | 8kg | 18kg | 烘干（分钟） | 洗衣液/柔顺剂 | 收入（含税） | 不含税 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for wk in sorted(weeks):
        c = weeks[wk]
        eur, dryer_min = revenue(c, mpe)
        wk_start = datetime.combine(wk, time.min, TZ)
        partial = first > wk_start + timedelta(hours=7) or last < wk_start + timedelta(days=7)
        label = f"{wk:%m-%d}–{wk + timedelta(days=6):%m-%d}" + ("（不完整）" if partial else "")
        lines.append(
            f"| {label} | {c['洗衣机 8kg']} | {c['洗衣机 18kg']} | {dryer_min:.0f} | {c['洗衣液']}/{c['柔顺剂']} "
            f"| {eur:,.0f} € | {eur / (1 + VAT):,.0f} € |"
        )
    return lines + [""]


def section(days, now, polls, states, types, events, open_hours, mpe):
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
    per_group = defaultdict(int)
    daily = defaultdict(lambda: defaultdict(int))
    for no, ts in events:
        if ts > start:
            cyc[no] += 1
            per_group[group_of(types[no])] += 1
            daily[ts.astimezone(TZ).date()][group_of(types[no])] += 1
    eur, _ = revenue(per_group, mpe)
    lines += [f"**收入估算：约 {eur:,.0f} €（含税），不含税约 {eur / (1 + VAT):,.0f} €**", ""]

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


FACTORY_KIND = {"mal": "洗衣", "sec": "烘干", "ddc": "洗衣液"}


def parse_start(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def factory_cycles():
    """Laverie Factory: one cycle per new started_at value seen after the first poll.

    Returns (polls, machines, cycles): machines maps machine_no to its latest row, cycles is a
    list of (machine_no, start, duration_min, price_eur).
    """
    polls = sorted(parse_ts(r["ts_utc"]) for r in read_rows("factory/polls-*.csv"))
    by = defaultdict(list)
    for r in read_rows("factory/changes-*.csv"):
        by[r["machine_no"]].append(r)
    machines, cycles = {}, []
    for no, rows in by.items():
        rows.sort(key=lambda r: r["ts_utc"])
        machines[no] = rows[-1]
        prev = None
        for r in rows:
            if prev is not None and r["started_at"] and r["started_at"] != prev:
                dur = float(r["duration_min"]) if r["duration_min"] else 0.0
                cycles.append((no, parse_start(r["started_at"]), dur, float(r["price_eur"])))
            if r["started_at"]:
                prev = r["started_at"]
    return polls, machines, cycles


def factory_section(richter_7d):
    polls, machines, cycles = factory_cycles()
    lines = [
        "## 竞争对手：Laverie Factory Port Marianne（约 420 m）",
        "",
        "数据来自该店 Touch'n Pay 公开机器页面；每次启动按该机器标价（含税）计入收入。"
        "“规格”是接口里的 special_val，可能是容量（kg）。",
        "",
    ]
    if not polls:
        return lines + ["还没有数据。", ""]
    first, now = polls[0], polls[-1]
    lines += [f"数据从 {first.astimezone(TZ):%Y-%m-%d %H:%M}（巴黎时间）开始。", ""]
    kind = {no: FACTORY_KIND.get(r["kind"], r["kind"]) for no, r in machines.items()}

    # weekly revenue
    weeks = defaultdict(lambda: defaultdict(float))
    for no, start, _, price in cycles:
        d = start.astimezone(TZ).date()
        w = weeks[d - timedelta(days=d.weekday())]
        w[kind[no]] += 1
        w["eur"] += price
    lines += [
        "### 每周收入估算",
        "",
        "| 周（周一–周日） | 洗衣（次） | 烘干（次） | 洗衣液（次） | 收入（含税） | 不含税 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for wk in sorted(weeks):
        c = weeks[wk]
        wk_start = datetime.combine(wk, time.min, TZ)
        partial = first > wk_start + timedelta(hours=7) or now < wk_start + timedelta(days=7)
        label = f"{wk:%m-%d}–{wk + timedelta(days=6):%m-%d}" + ("（不完整）" if partial else "")
        lines.append(
            f"| {label} | {c['洗衣']:.0f} | {c['烘干']:.0f} | {c['洗衣液']:.0f} "
            f"| {c['eur']:,.0f} € | {c['eur'] / (1 + VAT):,.0f} € |"
        )
    lines.append("")

    # per machine, last 7 days
    start7 = now - timedelta(days=7)
    window_min = (now - max(start7, first)).total_seconds() / 60
    uses, eur, busy_min = defaultdict(int), defaultdict(float), defaultdict(float)
    for no, start, dur, price in cycles:
        if start > start7:
            uses[no] += 1
            eur[no] += price
            busy_min[no] += dur
    lines += [
        "### 最近 7 天每台机器",
        "",
        "| 编号 | 名称 | 规格 | 标价 | 使用次数 | 收入 | 占用率 | 备注 |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for no in sorted(machines, key=int):
        r = machines[no]
        note = []
        if r["in_service"] == "1":
            note.append("维护中")
        if kind[no] != "洗衣液" and r["started_at"] and now - parse_start(r["started_at"]) > timedelta(days=3):
            note.append(f"最近一次启动 {parse_start(r['started_at']).astimezone(TZ):%m-%d}，可能停用")
        occ = f"{busy_min[no] / window_min:.0%}" if kind[no] != "洗衣液" and window_min > 0 else "-"
        lines.append(
            f"| {no} | {r['name']} | {r['spec']} | {float(r['price_eur']):.2f} € | {uses[no]} | {eur[no]:,.0f} € "
            f"| {occ} | {'；'.join(note)} |"
        )
    lines.append("")

    # side by side with Laverie Richter
    f_counts = defaultdict(int)
    for no, start, _, _ in cycles:
        if start > start7:
            f_counts[kind[no]] += 1
    f_eur = sum(eur.values())
    r_counts, r_eur, r_washers = richter_7d
    lines += [
        "### 和 Laverie Richter 对比（最近 7 天）",
        "",
        "| | Laverie Richter | Laverie Factory |",
        "|---|---:|---:|",
        f"| 洗衣机台数 | {r_washers} | {sum(1 for k in kind.values() if k == '洗衣')} |",
        f"| 洗衣次数 | {r_counts.get('洗衣机 8kg', 0) + r_counts.get('洗衣机 18kg', 0)} | {f_counts['洗衣']} |",
        f"| 烘干次数 | {r_counts.get('烘干机', 0)} | {f_counts['烘干']} |",
        f"| 收入（含税） | {r_eur:,.0f} € | {f_eur:,.0f} € |",
    ]
    if first > start7:
        lines.append("")
        lines.append("> ⚠️ 竞争对手记录不足 7 天，对比只供参考。")
    return lines + [""]


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
        mpe = dryer_minutes_per_event(polls, states, types, events)
        out += weekly_revenue(polls, types, events, mpe)
        for days in (7, 30):
            out += section(days, now, polls, states, types, events, open_hours, mpe)
        counts7 = defaultdict(int)
        for no, ts in events:
            if ts > now - timedelta(days=7):
                counts7[group_of(types[no])] += 1
        washers = sum(1 for t in types.values() if group_of(t) in ("洗衣机 8kg", "洗衣机 18kg"))
        out += factory_section((counts7, revenue(counts7, mpe)[0], washers))
    with open(os.path.join(ROOT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("README.md written")


if __name__ == "__main__":
    main()
