#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notice_watcher.py -- 校园通知哨兵 v1.0.0

ICEC-2026 第二阶段 项目四：解决校园真实痛点的轻量化实用程序。

痛点：学院/教务处通知分散在各个网页，靠人工刷新容易错过报名、考试与截止时间。
方案：每日定时抓取配置的校园通知页面，用启发式规则识别通知类链接，
      与本地状态比对后生成"新增通知日报"，并全程留下遥测运行日志。

特性：
  1. 仅依赖 Python 3.8+ 标准库，零第三方包；
  2. 自动处理 UTF-8 / GBK 系中文网页编码；
  3. 每次运行向 process_logs/ 追加一条 JSONL 遥测记录（时间戳、耗时、抓取数、报错）；
  4. --dry-run 模式只抓取解析，不更新状态、不写日报，用于调试。

用法：
  python notice_watcher.py              # 运行一次
  python notice_watcher.py --dry-run    # 试跑：不更新状态、不写日报
"""

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

VERSION = "v1.0.0"

BASE_DIR = Path(__file__).resolve().parent.parent            # 02-20Days-Challenge/
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
STATE_PATH = Path(__file__).resolve().parent / "state" / "seen.json"
LOGS_DIR = BASE_DIR / "process_logs"
OUTPUT_DIR = BASE_DIR / "output"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 通知链接的 URL 路径特征词（命中任一 -> 加分）
URL_HINTS = ("notice", "news", "info", "article", "xinxifabu", "tzgg", "detail", "/20")
# 标题中的日期特征（命中 -> 加分）
DATE_IN_TEXT = re.compile(
    r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})|(\d{1,2}月\d{1,2}日)|([\[\(]\d{1,2}-\d{1,2}[\]\)])"
)
DATE_ISO = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
STATE_MAX_ENTRIES = 500


class LinkParser(HTMLParser):
    """提取页面中全部 <a> 标签的 (文本, href)。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._href = None
        self._text_parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href", "")
            self._text_parts = []

    def handle_data(self, data):
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            title = re.sub(r"\s+", " ", "".join(self._text_parts)).strip()
            self.links.append((title, self._href))
            self._href = None
            self._text_parts = []


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("keywords", [])
    cfg.setdefault("min_score", 2)
    cfg.setdefault("max_items", 50)
    cfg.setdefault("timeout_sec", 15)
    if not cfg.get("urls"):
        raise ValueError("config.json 中 urls 不能为空")
    return cfg


def fetch(url, timeout_sec):
    """请求页面并返回文本；依次尝试响应声明的编码、UTF-8、GB18030。失败抛异常。"""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(req, timeout=timeout_sec) as resp:
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


def notice_score(title, href, keywords):
    """启发式打分：URL 路径特征 / 标题日期特征 / 标题长度 / 关键词，各 1 分。"""
    score = 0
    h = href.lower()
    if any(k in h for k in URL_HINTS):
        score += 1
    if DATE_IN_TEXT.search(title):
        score += 1
    if 8 <= len(title) <= 80:
        score += 1
    if any(k in title for k in keywords):
        score += 1
    return score


def parse_notices(html_text, base_url, keywords, min_score, max_items):
    parser = LinkParser()
    parser.feed(html_text)
    seen, items = set(), []
    for title, href in parser.links:
        if not title or not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        if notice_score(title, href, keywords) < min_score:
            continue
        full = urljoin(base_url, href)
        if full in seen:
            continue
        seen.add(full)
        m = DATE_ISO.search(title)
        items.append({
            "title": title,
            "url": full,
            "date": "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else "",
        })
    items.sort(key=lambda x: x["date"] or "0000-00-00", reverse=True)
    return items[:max_items]


def load_seen():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("seen", {})
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_seen(seen):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    items = sorted(seen.items(), key=lambda kv: kv[1].get("first_seen", ""))
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"seen": dict(items[-STATE_MAX_ENTRIES:])}, f, ensure_ascii=False, indent=2)


def write_digest(new_items, run_ts, stats):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / ("digest-%s.md" % run_ts[:10])
    lines = [
        "# 校园通知日报 %s" % run_ts[:10],
        "",
        "- 生成时间：%s" % run_ts,
        "- 程序版本：%s" % VERSION,
        "- 扫描页面：%d 个（成功 %d / 失败 %d）" % (stats["urls_total"], stats["urls_ok"], stats["urls_failed"]),
        "- 识别通知：%d 条，其中新增：%d 条" % (stats["items_total"], len(new_items)),
        "",
        "## 新增通知",
        "",
    ]
    if new_items:
        for i, it in enumerate(new_items, 1):
            lines.append("%d. [%s](%s) （%s）" % (i, it["title"], it["url"], it["date"] or "日期未标注"))
    else:
        lines.append("本次运行没有新增通知。")
    lines += [
        "",
        "## 说明",
        "",
        "本日报由 notice_watcher %s 自动生成，数据来源于 config.json 配置的校园通知页面。" % VERSION,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def log_telemetry(record):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / ("telemetry-%s.jsonl" % record["ts"][:10])
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def run_once(cfg, dry_run=False):
    started = time.time()
    run_ts = datetime.now().astimezone().isoformat(timespec="seconds")
    errors, all_items, urls_ok = [], [], 0
    for url in cfg["urls"]:
        try:
            html_text = fetch(url, cfg["timeout_sec"])
            all_items.extend(parse_notices(html_text, url, cfg["keywords"], cfg["min_score"], cfg["max_items"]))
            urls_ok += 1
        except (URLError, HTTPError, OSError, ValueError) as exc:
            errors.append({"stage": "fetch/parse", "url": url, "msg": "%s: %s" % (type(exc).__name__, exc)})

    seen = load_seen()
    new_items = []
    for it in all_items:
        key = hashlib.sha1(it["url"].encode("utf-8")).hexdigest()
        if key not in seen:
            new_items.append(it)
            if not dry_run:
                seen[key] = {"title": it["title"], "first_seen": run_ts}

    stats = {
        "urls_total": len(cfg["urls"]),
        "urls_ok": urls_ok,
        "urls_failed": len(cfg["urls"]) - urls_ok,
        "items_total": len(all_items),
    }
    status = "error" if urls_ok == 0 else ("partial" if errors else "ok")
    digest_path = None
    if not dry_run:
        save_seen(seen)
        digest_path = write_digest(new_items, run_ts, stats)

    record = {
        "ts": run_ts,
        "run_id": uuid.uuid4().hex[:8],
        "version": VERSION,
        "duration_ms": int((time.time() - started) * 1000),
        "status": status,
        "urls_total": stats["urls_total"],
        "urls_ok": stats["urls_ok"],
        "urls_failed": stats["urls_failed"],
        "items_total": stats["items_total"],
        "items_new": len(new_items),
        "new_titles": [it["title"] for it in new_items[:10]],
        "dry_run": dry_run,
        "errors": errors,
    }
    log_path = log_telemetry(record)

    print("[%s] %s | 页面 %d/%d 成功 | 识别 %d 条 | 新增 %d 条 | 耗时 %dms"
          % (VERSION, status.upper(), urls_ok, len(cfg["urls"]),
             len(all_items), len(new_items), record["duration_ms"]))
    for it in new_items:
        print("  + [%s] %s" % (it["date"] or "----", it["title"]))
    print("遥测日志：%s" % log_path)
    if digest_path:
        print("通知日报：%s" % digest_path)
    for e in errors:
        print("错误：%s -> %s" % (e["url"], e["msg"]), file=sys.stderr)
    return 0 if status != "error" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="校园通知哨兵：定时抓取校园通知并生成新增日报")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只抓取解析，不更新状态与日报")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("配置加载失败：%s" % exc, file=sys.stderr)
        return 2
    try:
        return run_once(cfg, dry_run=args.dry_run)
    except Exception as exc:  # 兜底：任何未预期错误也必须落一条遥测
        log_telemetry({
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": uuid.uuid4().hex[:8],
            "version": VERSION,
            "status": "error",
            "errors": [{"stage": "runtime", "msg": "%s: %s" % (type(exc).__name__, exc)}],
        })
        print("运行失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
