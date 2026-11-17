#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_task.py -- 课题10「AI 方法」：批量提取通知信息

任务：从 10 个通知链接中提取 标题 / 发布日期 / 来源 / 类别，汇总为 CSV。
与人工逐条打开网页复制粘贴做耗时与准确率对比。

用法：
  python extract_task.py                  # 处理 samples.txt 全部链接，输出 results_ai.csv
  python extract_task.py --out 输出.csv   # 自定义输出路径

依赖：仅 Python 3.8+ 标准库。
"""

import argparse
import csv
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 类别口径：主题类别（固定词表，与人工填表一致；按顺序匹配，先命中先归类）
CATEGORY_RULES = [
    (("补助", "认定", "奖学金", "奖助"), "奖助公示"),
    (("招聘", "拟聘", "博士后", "资格复审", "考核成绩"), "人事招聘"),
    (("补考", "统考"), "考试与补考"),
    (("选课", "课程", "重修", "补修", "教材"), "选课与课程"),
    (("学分", "申报"), "学分与申报"),
    (("大赛", "竞赛", "选拔赛"), "赛事通知"),
    (("入学", "须知", "新生"), "入学教育"),
]


def classify(title):
    """按固定词表把通知标题归入主题类别；词表未命中归"其他"。"""
    for kws, cat in CATEGORY_RULES:
        if any(k in title for k in kws):
            return cat
    return "其他"


def fetch(url, timeout=15):
    """请求页面并返回文本；依次尝试响应声明的编码、UTF-8、GB18030。"""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        declared = resp.headers.get_content_charset()
    for enc in (declared, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def extract(html_text, url):
    """从通知详情页提取 (标题, 发布日期, 来源, 类别)。"""
    m = re.search(r"<title>(.*?)</title>", html_text, re.S)
    title = ""
    if m:
        # <title>标题_通知公告_深技师-主站</title> -> 取第一段
        title = re.sub(r"\s+", " ", m.group(1)).split("_")[0].strip()
    dm = re.search(r"发布时间[：:]\s*(\d{4}-\d{2}-\d{2})", html_text)
    date = dm.group(1) if dm else ""
    sm = re.search(r"来源[：:]\s*([^\s<　]{2,30})", html_text)
    source = sm.group(1).strip() if sm else ""
    return title, date, source, classify(title)


def main():
    parser = argparse.ArgumentParser(description="批量提取通知信息（课题10 AI 方法）")
    parser.add_argument("--samples", default=str(HERE / "samples.txt"))
    parser.add_argument("--out", default=str(HERE / "results_ai.csv"))
    args = parser.parse_args()

    with open(args.samples, encoding="utf-8") as f:
        urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    rows, t0 = [], time.time()
    for i, url in enumerate(urls, 1):
        s = time.time()
        try:
            title, date, source, cat = extract(fetch(url), url)
            status = "OK"
        except (URLError, HTTPError, OSError) as exc:
            title = date = source = cat = ""
            status = "FAIL:%s" % type(exc).__name__
        rows.append([i, title, date, source, cat, url, status, round(time.time() - s, 2)])
        print("[%2d/10] %5.1fs  %-8s %s" % (i, time.time() - s, status, title[:32]))

    total = time.time() - t0
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "标题", "发布日期", "来源", "类别", "链接", "状态", "单件耗时(秒)"])
        w.writerows(rows)
        w.writerow(["合计", "", "", "", "", "", "", round(total, 2)])
    print("\n总耗时 %.1f 秒 | 单件平均 %.1f 秒 | 结果已写入 %s" % (total, total / max(len(urls), 1), args.out))


if __name__ == "__main__":
    main()
