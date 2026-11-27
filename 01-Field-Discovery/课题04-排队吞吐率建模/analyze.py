#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py -- 课题04 食堂/电梯高峰排队吞吐率分析

读取 raw_data.csv（15 组现场采样），计算到达率、服务率、平均队列、
Little 定律等待时间、队列增长趋势，判定流程瓶颈，并生成：
  - analysis_report.md   分析报告（含改进方案待填节）
  - queue_chart.svg      队列人数趋势图（含到达/服务对比柱状图）

用法：
  python analyze.py                    # 分析 raw_data.csv
  python analyze.py --data 数据.csv    # 指定数据文件
  python analyze.py --servers 2        # 服务台数量（默认 1，如电梯可设为轿厢数）
  python analyze.py --demo             # 用内置模拟数据演示（验证脚本用）

依赖：仅 Python 3.8+ 标准库。
"""

import argparse
import csv
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SVG_FONT = "Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif"


def num(s, cast=float):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return cast(s)
    except ValueError:
        return None


def read_rows(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for d in csv.DictReader(f):
            if not (d.get("时刻") or "").strip():
                continue
            rows.append({
                "time": d["时刻"].strip(),
                "queue": num(d.get("队列人数"), int),
                "arrivals": num(d.get("本间隔到达人数"), int) or 0,
                "served": num(d.get("本间隔服务完成人数"), int) or 0,
                "service_sec": num(d.get("单次服务耗时秒")),
                "note": (d.get("备注") or "").strip(),
            })
    return rows


def demo_rows():
    """15 组合成模拟数据：队列由 3 人爬升到 11 人后趋缓。"""
    queue = [3, 4, 5, 6, 6, 7, 8, 9, 9, 10, 10, 11, 11, 11, 11]
    arrivals = [4, 5, 5, 4, 5, 5, 4, 5, 4, 5, 4, 5, 4, 4, 4]
    served = [3, 4, 4, 3, 4, 4, 3, 4, 3, 4, 3, 4, 3, 3, 4]
    sec = [42, 45, 47, 44, 46, 48, 43, 47, 45, 46, 44, 48, 46, 45, 47]
    times = ["11:%02d" % (45 + 2 * i) if 45 + 2 * i < 60 else "12:%02d" % (45 + 2 * i - 60) for i in range(15)]
    return [{"time": t, "queue": q, "arrivals": a, "served": s, "service_sec": x, "note": ""}
            for t, q, a, s, x in zip(times, queue, arrivals, served, sec)]


def slope(y):
    """最小二乘直线斜率（单位：每记录点）。"""
    n = len(y)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(y) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (v - my) for x, v in zip(xs, y)) / den if den else 0.0


def analyze(rows, interval_min, servers):
    n = len(rows)
    duration = (n - 1) * interval_min  # 15 个记录点覆盖 14 个间隔
    q = [r["queue"] for r in rows]
    A = sum(r["arrivals"] for r in rows)
    C = sum(r["served"] for r in rows)
    lam = A / duration                      # 到达率 人/分
    thr = C / duration                      # 系统实际吞吐 人/分
    L = sum(q) / n                          # 平均队列人数（含被服务者）
    secs = [r["service_sec"] for r in rows if r["service_sec"]]
    avg_sec = sum(secs) / len(secs) if secs else None
    mu = 60.0 / avg_sec if avg_sec else None          # 单服务台服务率 人/分
    rho = lam / (servers * mu) if mu else None        # 利用率
    W = L / lam if lam else None                      # Little 定律：平均停留时间（分）
    Wq = (W - avg_sec / 60.0) if (W is not None and avg_sec) else None  # 纯排队等待 = 停留 − 服务耗时
    sl = slope(q) / interval_min                     # 队列增长斜率（人/分钟）
    backlog = [(rows[i]["time"], rows[i]["arrivals"], rows[i]["served"])
               for i in range(n) if rows[i]["arrivals"] > rows[i]["served"]]
    return {
        "n": n, "duration": duration, "A": A, "C": C, "lam": lam, "thr": thr,
        "L": L, "avg_sec": avg_sec, "mu": mu, "rho": rho, "W": W, "Wq": Wq, "slope": sl,
        "backlog": backlog, "qmax": max(q) if q else 0,
        "q_first": q[0] if q else None, "q_last": q[-1] if q else None,
    }


def verdict(m):
    out = []
    if m["rho"] is not None and m["rho"] > 1.0:
        out.append("**服务能力不足（超负荷）**：理论需求达到服务能力的 %.0f%%，到达率已超过系统最大吞吐，队列必然持续增长，必须增加服务台或压缩单次服务耗时。" % (m["rho"] * 100))
    elif m["rho"] is not None and m["rho"] >= 0.85:
        out.append("**服务能力瓶颈**：服务台利用率 %.0f%%，已接近饱和，到达的微小波动就会转化为长队。" % (m["rho"] * 100))
    if m["slope"] >= 0.2:
        out.append("**系统不稳定**：%d 分钟内队列以约 %.1f 人/分钟的速度持续增长（峰值 %d 人），到达已超过服务能力，队尾会不断后移。" % (m["duration"], m["slope"], m["qmax"]))
    if len(m["backlog"]) >= m["n"] * 0.4:
        out.append("**积压频发**：%d/%d 个采样间隔内到达大于服务完成，队列只增不减的时段占四成以上。" % (len(m["backlog"]), m["n"]))
    if m["q_first"] is not None and m["q_last"] is not None and m["q_first"] - m["q_last"] >= 5:
        out.append("**观测窗口处于高峰后的衰减段**：队列由 %d 人降至 %d 人（净减 %d 人），到达率持续低于系统吞吐——说明采样开始前高峰已把队列堆到 %d 人，真正的瓶颈发生在观测窗口之前，需结合供餐节奏判断高峰起点。" % (m["q_first"], m["q_last"], m["q_first"] - m["q_last"], m["q_first"]))
    if not out:
        out.append("**系统整体稳定**：未出现持续积压，排队主要来自到达时刻集中造成的短时拥堵。")
    return out


def suggestions(m):
    s = []
    if m["rho"] is not None and m["rho"] >= 0.85:
        s.append("高峰期增设 1 个备用服务台/窗口（兼职学生即可，零硬件成本），或把一项服务拆成两步并行；")
    if m["avg_sec"] is not None and m["avg_sec"] >= 40:
        s.append("压缩单次服务耗时（当前均值 %.0f 秒）：预置常用选项、扫码预点餐/预约，把点单与取餐分离；" % m["avg_sec"])
    if m["slope"] >= 0.2:
        s.append("到达端限流分流：错峰公告、分时段下课/就餐引导，把到达曲线拉平；")
    s.append("排队区管理：明确队列标识与预计等待时间提示，降低插队与弃队（弃队等于白排队，进一步浪费服务能力）。")
    return s


def chart_svg(rows):
    W, H = 680, 320
    ml, mr, mt, mb = 46, 14, 34, 46
    pw, ph = W - ml - mr, H - mt - mb
    n = len(rows)
    q = [r["queue"] for r in rows]
    a = [r["arrivals"] for r in rows]
    s = [r["served"] for r in rows]
    ymax = max(max(q), max(a), max(s)) + 2
    bw = pw / n * 0.32

    def x(i):
        return ml + pw * (i + 0.5) / n

    def y(v):
        return mt + ph * (1 - v / ymax)

    parts = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" font-family="%s">' % (W, H, SVG_FONT)]
    parts.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    parts.append('<text x="%d" y="20" font-size="13" fill="#1f2937" text-anchor="middle" font-weight="bold">高峰排队指标趋势（队列人数 / 到达 / 服务完成）</text>' % (W // 2))
    # 网格与 Y 轴刻度
    for gv in range(0, int(ymax) + 1, max(1, int(ymax) // 5)):
        gy = y(gv)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e5e7eb" stroke-width="1"/>' % (ml, gy, ml + pw, gy))
        parts.append('<text x="%d" y="%.1f" font-size="10" fill="#6b7280" text-anchor="end">%d</text>' % (ml - 5, gy + 3, gv))
    # 柱状：到达 / 服务完成
    for i in range(n):
        ha = ph * a[i] / ymax
        hs = ph * s[i] / ymax
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#93c5fd"/>' % (x(i) - bw - 1, mt + ph - ha, bw, ha))
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#86efac"/>' % (x(i) + 1, mt + ph - hs, bw, hs))
    # 折线：队列人数
    pts = " ".join("%.1f,%.1f" % (x(i), y(q[i])) for i in range(n))
    parts.append('<polyline points="%s" fill="none" stroke="#dc2626" stroke-width="2.2"/>' % pts)
    for i in range(n):
        parts.append('<circle cx="%.1f" cy="%.1f" r="3" fill="#dc2626"/>' % (x(i), y(q[i])))
    # X 轴标签（每 3 个显示一次）
    for i in range(n):
        if i % 3 == 0 or i == n - 1:
            parts.append('<text x="%.1f" y="%d" font-size="10" fill="#6b7280" text-anchor="middle">%s</text>' % (x(i), H - mb + 16, rows[i]["time"]))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#9ca3af" stroke-width="1"/>' % (ml, mt + ph, ml + pw, mt + ph))
    # 图例
    lx, ly = ml + 8, mt + 14
    for color, label in (("#dc2626", "队列人数"), ("#93c5fd", "到达"), ("#86efac", "服务完成")):
        parts.append('<rect x="%d" y="%d" width="10" height="10" fill="%s"/>' % (lx, ly - 9, color))
        parts.append('<text x="%d" y="%d" font-size="10" fill="#374151">%s</text>' % (lx + 14, ly, label))
        lx += 78
    parts.append("</svg>")
    return "\n".join(parts)


def write_report(m, rows, servers, interval_min, path):
    L = []
    L.append("# 课题04 排队吞吐率分析报告")
    L.append("")
    L.append("- 采样窗口：%s — %s，每 %d 分钟记录一次，共 %d 组" % (rows[0]["time"], rows[-1]["time"], interval_min, m["n"]))
    L.append("- 有效观测时长：%d 分钟；服务台数量：%d" % (m["duration"], servers))
    L.append("")
    L.append("## 核心指标")
    L.append("")
    L.append("| 指标 | 数值 | 含义 |")
    L.append("|---|---|---|")
    L.append("| 总到达人数 | %d 人 | 窗口内到达总量 |" % m["A"])
    L.append("| 总服务完成 | %d 人 | 窗口内完成服务离开总量 |" % m["C"])
    L.append("| 到达率 λ | %.2f 人/分 | 平均每分钟新到顾客数 |" % m["lam"])
    L.append("| 系统吞吐 | %.2f 人/分 | 平均每分钟能服务完的人数 |" % m["thr"])
    L.append("| 平均队列长度 L | %.1f 人 | 系统内平均人数（含正被服务者）|" % m["L"])
    if m["avg_sec"]:
        L.append("| 平均单次服务耗时 | %.0f 秒 | 抽样的服务台忙时均值 |" % m["avg_sec"])
    if m["mu"]:
        L.append("| 单服务台服务率 μ | %.2f 人/分 | 60 / 平均单次服务耗时 |" % m["mu"])
    if m["rho"] is not None:
        L.append("| 服务台利用率 ρ | %.0f%% | λ / (服务台数 × μ)，>85%% 视为饱和 |" % (m["rho"] * 100))
    if m["W"] is not None:
        L.append("| 平均停留时间 W | %.1f 分钟 | Little 定律：L = λ × W；系统内停留，含约 %.0f 分钟服务 |" % (m["W"], (m["avg_sec"] or 0) / 60.0))
    if m["Wq"] is not None:
        L.append("| 平均排队等待 Wq | %.1f 分钟 | W − 平均单次服务耗时，即纯排队时间 |" % m["Wq"])
    L.append("| 队列增长斜率 | %.2f 人/分钟 | >0.2 视为持续增长（不稳定）|" % m["slope"])
    L.append("| 队列峰值 | %d 人 | 观测到的最大队长 |" % m["qmax"])
    L.append("")
    L.append("## 积压时段（到达 > 服务完成）")
    L.append("")
    if m["backlog"]:
        for t, ar, sv in m["backlog"]:
            L.append("- %s：到达 %d 人，完成 %d 人，净积压 %+d 人" % (t, ar, sv, ar - sv))
    else:
        L.append("无。所有间隔内服务完成均不小于到达。")
    L.append("")
    L.append("## 瓶颈判定")
    L.append("")
    for v in verdict(m):
        L.append("- " + v)
    L.append("")
    L.append("## 低成本改进方案（百元预算内）")
    L.append("")
    for i, s in enumerate(suggestions(m), 1):
        L.append("%d. %s" % (i, s))
    L.append("")
    L.append("（请结合现场观察补充 1–2 条针对本点位的具体措施，并估算实施成本。）")
    L.append("")
    L.append("## 队列趋势图")
    L.append("")
    L.append("![queue_chart](queue_chart.svg)")
    L.append("")
    path.write_text("\n".join(L), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description="课题04 排队吞吐率分析")
    p.add_argument("--data", default=str(HERE / "raw_data.csv"))
    p.add_argument("--servers", type=int, default=1)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--demo", action="store_true")
    args = p.parse_args()

    if args.demo:
        rows = demo_rows()
    else:
        rows = read_rows(args.data)
        if len(rows) < 2:
            raise SystemExit("数据不足：%s 中有效记录少于 2 行" % args.data)

    m = analyze(rows, args.interval, args.servers)
    write_report(m, rows, args.servers, args.interval, HERE / "analysis_report.md")
    (HERE / "queue_chart.svg").write_text(chart_svg(rows), encoding="utf-8")

    print("λ=%.2f 人/分 | 吞吐=%.2f 人/分 | 平均队列=%.1f 人 | 利用率=%s | W=%.1f 分 | 斜率=%.2f"
          % (m["lam"], m["thr"], m["L"],
             ("%.0f%%" % (m["rho"] * 100)) if m["rho"] is not None else "N/A",
             m["W"] or 0, m["slope"]))
    for v in verdict(m):
        print("- " + v)
    print("报告: analysis_report.md | 图: queue_chart.svg")


if __name__ == "__main__":
    main()
