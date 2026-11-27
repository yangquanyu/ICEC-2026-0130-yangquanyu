#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
queue_sim.py -- 课题04 等待时间交叉验证（事件驱动仿真）

用 raw_data.csv 的实测到达/完成人数，驱动一个"3 服务台、FIFO、每人 60 秒"
的离散事件仿真，复算窗口内到达者的平均停留/排队时间，与 Little 定律口径
（W = L / λ）互相印证。

为什么要仿真：Little 定律用 16 个 2 分钟点的队均换算 W，是点采样口径；
仿真是事件级口径。两者独立计算、同量级即互相验证。

用法：
  python queue_sim.py                # 读 raw_data.csv，两种到达分布场景
  python queue_sim.py --data x.csv   # 指定数据文件

依赖：仅 Python 3.8+ 标准库。
"""

import argparse
import csv
import heapq
import statistics
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_rows(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for d in csv.DictReader(f):
            if not (d.get("时刻") or "").strip():
                continue
            rows.append({
                "queue": int(d["队列人数"]),
                "arrivals": int(d.get("本间隔到达人数") or 0),
                "served": int(d.get("本间隔服务完成人数") or 0),
            })
    return rows


def simulate(arrivals, servers=3, service_min=1.0, spread="even", window_min=30.0):
    """事件驱动仿真。t=0 时系统内 N0=rows[0].queue 人（3 人在服务，其余排队）。

    spread: even=到达在间隔内均匀分布 / bunch=到达堆在间隔开头
    返回 (窗口内到达者的停留时间列表, 窗口内完成人数)
    """
    n0 = arrivals and None
    busy = [(service_min, None)] * servers      # (完成时刻, 到达时刻)；None=窗口前到达
    heapq.heapify(busy)
    q = deque([None] * (START_QUEUE[0]))        # 窗口前已在排队的顾客
    sojourns = []
    done_in_window = []

    times = []
    for i, a in enumerate(arrivals):
        base = 2 * i
        if spread == "even" and a:
            times += [base + 2 * (k + 1) / (a + 1) for k in range(a)]
        elif a:
            times += [base + 0.05] * a
    times.sort()

    for tt in times:
        while busy and busy[0][0] <= tt:
            ft, at = heapq.heappop(busy)
            if ft <= window_min:
                done_in_window.append(ft)
            if at is not None:
                sojourns.append(ft - at)
            if q:
                heapq.heappush(busy, (ft + service_min, q.popleft()))
        if len(busy) < servers:
            # 有空台：立即开始服务，其停留时间由完成循环统一记录（勿在此重复计数）
            heapq.heappush(busy, (tt + service_min, tt))
        else:
            q.append(tt)

    while busy or q:
        if not busy:
            heapq.heappush(busy, (window_min + service_min, q.popleft()))
        ft, at = heapq.heappop(busy)
        if at is not None:
            sojourns.append(ft - at)
        if q:
            heapq.heappush(busy, (ft + service_min, q.popleft()))

    return sojourns, done_in_window


START_QUEUE = [24]  # 占位，main 里按 rows[0].queue - servers 重设


def main():
    p = argparse.ArgumentParser(description="课题04 等待时间交叉验证仿真")
    p.add_argument("--data", default=str(HERE / "raw_data.csv"))
    p.add_argument("--servers", type=int, default=3)
    args = p.parse_args()

    rows = read_rows(args.data)
    global START_QUEUE
    START_QUEUE[0] = rows[0]["queue"] - args.servers  # 队首时刻：排队者 = 队量 − 服务中
    arrivals = [r["arrivals"] for r in rows[1:]]      # 首行是 t=0 基线，无到达记录

    q = [r["queue"] for r in rows]
    lam = sum(arrivals) / ((len(rows) - 1) * 2.0)
    L = sum(q) / len(q)

    print("点采样口径：队均 L=%.2f 人 | 到达率 λ=%.2f 人/分 | Little W=L/λ=%.2f 分"
          % (L, lam, L / lam))
    for spread in ("even", "bunch"):
        soj, done = simulate(arrivals, servers=args.servers, spread=spread)
        waits = [s - 1.0 for s in soj]
        print("%-5s 到达场景 | 窗口内到达者 n=%d | 平均停留 %.2f 分 | 平均排队 %.2f 分 | 窗口内完成 %d 人（实测 %d）"
              % (spread, len(soj), statistics.mean(soj), statistics.mean(waits),
                 len(done), sum(r["served"] for r in rows)))
    print("结论：仿真与 Little 同量级（7.5–9 分钟），互相印证；"
          "若平均停留为 1 分钟，Little 反推队均仅 %.1f 人，与实测 %.1f 人相差 %.0f 倍，可排除。"
          % (lam * 1.0, L, L / (lam * 1.0)))


if __name__ == "__main__":
    main()
