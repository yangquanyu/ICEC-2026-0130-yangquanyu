#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notice_watcher.py -- 校园通知哨兵 v1.3.0

ICEC-2026 第二阶段 项目四：解决校园真实痛点的轻量化实用程序。

痛点：学院/教务处通知分散在各个网页，靠人工刷新容易错过报名、考试与截止时间。
方案：每日定时抓取配置的校园通知页面，用启发式规则识别通知类链接，
      与本地状态比对后生成"新增通知日报"，并全程留下遥测运行日志。

特性：
  1. 仅依赖 Python 3.8+ 标准库，零第三方包；
  2. 自动处理 UTF-8 / GBK 系中文网页编码；
  3. 每次运行向 process_logs/ 追加一条 JSONL 遥测记录（时间戳、耗时、抓取数、报错）；
  4. 网络层错误指数退避重试（3 次尝试，2s/4s）；
  5. 对新增通知抓取详情页，提取截止时间/发布单位/面向对象/一句话摘要，
     按截止时间升序输出日报，3 天内截止高亮（v1.3.0，8 位用户反馈驱动）；
  6. --dry-run 模式只抓取解析，不更新状态、不写日报，用于调试。

用法：
  python notice_watcher.py              # 运行一次
  python notice_watcher.py --dry-run    # 试跑：不更新状态、不写日报
"""

import argparse
import hashlib
import html as html_lib
import json
import re
import sys
import time
import uuid
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

VERSION = "v1.3.0"

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

# 主题类别词表（与课题 10 实验同一套口径，按顺序匹配，先命中先归类）
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
# 标题中的日期特征（命中 -> 加分）
DATE_IN_TEXT = re.compile(
    r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})|(\d{1,2}月\d{1,2}日)|([\[\(]\d{1,2}-\d{1,2}[\]\)])"
)
DATE_ISO = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
# 列表条目尾部的标准日期（如 <span class="float_r">2026-11-16</span>）
DATE_IN_TAIL = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
STATE_MAX_ENTRIES = 500


class LinkParser(HTMLParser):
    """提取列表页 <li><a>标题</a> ...尾部文本...</li> 结构中的 (标题, href, 尾部文本)。

    通知列表页的日期通常不在 <a> 文本内，而放在同一 <li> 的后续元素里，
    因此需要捕获 </a> 之后、</li> 之前的文本作为日期来源。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self._href = None
        self._title_parts = []
        self._tail_parts = []
        self._title_done = False

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "li":
            # 新列表项开始：丢弃任何悬挂的链接状态。
            # 面包屑导航等处的 <a> 不在 <li> 内，若不在此重置，
            # 其状态会延续到下一个 </li>，把列表第一条通知整个吞掉。
            self._href = None
            self._title_parts = []
            self._tail_parts = []
            self._title_done = False
        elif t == "a" and self._href is None:
            self._href = dict(attrs).get("href", "")
            self._title_parts = []
            self._tail_parts = []
            self._title_done = False

    def handle_data(self, data):
        if self._href is None:
            return
        if self._title_done:
            self._tail_parts.append(data)
        else:
            self._title_parts.append(data)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "a" and self._href is not None:
            self._title_done = True
        elif t == "li" and self._href is not None:
            title = re.sub(r"\s+", " ", "".join(self._title_parts)).strip()
            tail = re.sub(r"\s+", " ", "".join(self._tail_parts)).strip()
            self.items.append((title, self._href, tail))
            self._href = None


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


def fetch(url, timeout_sec, retries=2):
    """请求页面并返回 (文本, 实际尝试次数)。

    编码依次尝试响应声明 -> UTF-8 -> GB18030。
    网络层错误（SSL 重置、连接中断、超时）按指数退避重试：
    第 1 次失败等 2 秒、第 2 次失败等 4 秒后再试，仍失败则抛出最后一次异常。
    """
    last = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
            with urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read()
                declared = resp.headers.get_content_charset()
            for enc in (declared, "utf-8", "gb18030"):
                if not enc:
                    continue
                try:
                    return raw.decode(enc), attempt + 1
                except (UnicodeDecodeError, LookupError):
                    continue
            return raw.decode("utf-8", errors="replace"), attempt + 1
        except (URLError, HTTPError, OSError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(2.0 * (2 ** attempt))
    raise last


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
    base_netloc = urlparse(base_url).netloc
    base_norm = base_url.rstrip("/")
    seen, items = set(), []
    for title, href, tail in parser.items:
        if not title or not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        full = urljoin(base_url, href)
        # 过滤外站链接（侧边栏办事大厅、学籍查询等）与指向列表页自身的导航链接
        if urlparse(full).netloc != base_netloc:
            continue
        if full.rstrip("/") == base_norm:
            continue
        if notice_score(title, href, keywords) < min_score:
            continue
        if full in seen:
            continue
        seen.add(full)
        m = DATE_IN_TAIL.search(tail) or DATE_ISO.search(title)
        items.append({
            "title": title,
            "url": full,
            "date": "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3))) if m else "",
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


def extract_deadline(text, today=None):
    """从正文提取截止时间，返回 YYYY-MM-DD 或空串。

    只认两类表述：①"X月X日…前/止/截止"；②"截止/请于…X月X日"。
    事件日期（"定于X月X日进行"）不含截止语义，不误判。
    缺年份时按当前年推断；若推断结果已过去 30 天以上，则归为明年。
    """
    today = today or date.today()
    for pat, has_year in (
        (r"(\d{4})年(\d{1,2})月(\d{1,2})日[^。；\s]{0,12}(?:前|以前|之前|止|截止)", True),
        (r"(\d{1,2})月(\d{1,2})日[^。；\s]{0,12}(?:前|以前|之前|止|截止)", False),
        (r"(?:截止|请于)[：:\s]*(\d{4})?年?(\d{1,2})月(\d{1,2})日", None),
    ):
        m = re.search(pat, text)
        if not m:
            continue
        g = m.groups()
        try:
            if has_year:
                dl = date(int(g[0]), int(g[1]), int(g[2]))
            elif has_year is False:
                dl = date(today.year, int(g[0]), int(g[1]))
                if (today - dl).days > 30:
                    dl = date(today.year + 1, int(g[0]), int(g[1]))
            else:
                dl = date(int(g[0] or today.year), int(g[1]), int(g[2]))
        except ValueError:
            continue
        return dl.isoformat()
    return ""


AUDIENCE_TOKENS = ("全体新生", "全体学生", "全体师生", "毕业班学生", "相关教师", "各班级", "教学单位")


def extract_detail(raw_html):
    """从通知详情页提取 (来源, 截止时间, 面向对象, 摘要)；提不到的字段返回空串，不编造。"""
    source = ""
    m = re.search(r"来源[：:]\s*([^\s<　]{2,30})", raw_html)
    if m:
        source = m.group(1).strip()
    # 先剥离注释/脚本/样式，避免把 HTML 注释与 JS 残渣当正文
    clean = re.sub(r"<!--.*?-->", " ", raw_html, flags=re.S)
    clean = re.sub(r"<script.*?</script>", " ", clean, flags=re.S)
    clean = re.sub(r"<style.*?</style>", " ", clean, flags=re.S)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", clean))
    text = re.sub(r"\s+", " ", text).strip()
    deadline = extract_deadline(text)
    audience = ""
    for tok in AUDIENCE_TOKENS:
        if tok in text:
            audience = tok
            break
    if not audience:
        am = re.search(r"(20\d{2}级[^。；，\s]{0,8}学生|\d{4}届[^。；，\s]{0,8}学生)", text)
        if am:
            audience = am.group(1)[:20]
    summary = ""
    pm = re.search(r"发布时间[：:][0-9\-: ]+", text)
    body = text[pm.end():].strip(" 　") if pm else text
    body = re.sub(r"^[\s\S]{0,30}?-->", "", body).strip()  # 兜底去残留注释尾
    sm = re.search(r"^[^。；！]{%d,%d}[。；！]" % (10, 60), body)
    if sm:
        summary = sm.group(0).strip()
    elif len(body) >= 10:
        summary = body[:50]
    return source, deadline, audience, summary


def write_digest(new_items, run_ts, stats):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / ("digest-%s.md" % run_ts[:10])
    L = [
        "# 校园通知日报 %s" % run_ts[:10],
        "",
        "- 抓取日期：%s（与条目内的通知发布日期不同，抓取日期是本次运行日期）" % run_ts[:10],
        "- 生成时间：%s" % run_ts,
        "- 程序版本：%s" % VERSION,
        "- 扫描页面：%d 个（成功 %d / 失败 %d）" % (stats["urls_total"], stats["urls_ok"], stats["urls_failed"]),
        "- 新增通知：%d 条" % len(new_items),
        "",
    ]
    if not new_items:
        L += ["## 今日无新增通知", "", "官网通知公告栏本次运行没有新增条目。", ""]
    else:
        today = date.today()
        rows = []
        for it in new_items:
            dl = it.get("deadline", "")
            mark = ""
            if dl:
                delta = (date.fromisoformat(dl) - today).days
                if 0 <= delta <= 3:
                    mark = " **⏰%d天后截止**" % delta
                elif delta < 0:
                    mark = " （已截止）"
            rows.append((dl or "9999-99-99", it.get("date") or "0000-00-00", it, mark))
        rows.sort(key=lambda r: (r[0], r[1]))
        L += [
            "## 新增通知（按截止时间升序，无截止时间的排在后面）",
            "",
            "| 标题 | 分类 | 发布日期 | 截止时间 | 发布单位 | 面向对象 | 链接 |",
            "|---|---|---|---|---|---|---|",
        ]
        for _, _, it, mark in rows:
            L.append("| %s%s | %s | %s | %s | %s | %s | [打开](%s) |" % (
                it["title"], mark, it.get("category", ""), it.get("date", ""),
                it.get("deadline", "") or "未注明", it.get("source", ""),
                it.get("audience", "") or "未注明", it["url"]))
        L.append("")
        summaries = [it for it in new_items if it.get("summary")]
        if summaries:
            L += ["## 一句话摘要", ""]
            for it in summaries[:10]:
                L.append("- **%s**：%s" % (it["title"][:24], it["summary"]))
            L.append("")
    L += ["---", "", "本日报由 notice_watcher %s 自动生成；截止时间与面向对象为正则提取，提不到时标注'未注明'，不做猜测。" % VERSION, ""]
    path.write_text("\n".join(L), encoding="utf-8")
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
    errors, all_items, urls_ok, fetch_attempts = [], [], 0, 0
    for url in cfg["urls"]:
        try:
            html_text, attempts = fetch(url, cfg["timeout_sec"])
            fetch_attempts += attempts
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

    # 主题标签对所有新增通知免费可打；详情页字段（截止时间/来源/面向对象/摘要）
    # 只对前 detail_max 条抓取，控制单次运行的请求量
    for it in new_items:
        it["category"] = classify(it["title"])
    detail_max = cfg.get("detail_max", 20)
    detail_fetched, deadline_found = 0, 0
    for it in new_items[:detail_max]:
        try:
            raw, _ = fetch(it["url"], cfg["timeout_sec"])
            source, deadline, audience, summary = extract_detail(raw)
            it["source"], it["deadline"] = source, deadline
            it["audience"], it["summary"] = audience, summary
            detail_fetched += 1
            if deadline:
                deadline_found += 1
        except (URLError, HTTPError, OSError, ValueError) as exc:
            errors.append({"stage": "detail", "url": it["url"], "msg": "%s: %s" % (type(exc).__name__, exc)})

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
        "detail_fetched": detail_fetched,
        "deadline_found": deadline_found,
        "fetch_attempts": fetch_attempts,
        "dry_run": dry_run,
        "errors": errors,
    }
    log_path = log_telemetry(record)

    print("[%s] %s | 页面 %d/%d 成功 | 识别 %d 条 | 新增 %d 条 | 详情抓取 %d（含截止 %d）| 耗时 %dms"
          % (VERSION, status.upper(), urls_ok, len(cfg["urls"]),
             len(all_items), len(new_items), detail_fetched, deadline_found, record["duration_ms"]))
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
