# 项目四：校园通知哨兵（notice_watcher）

## 痛点

学院/教务处/实验室的通知分散在多个网页，靠人工刷新容易错过报名、考试与截止时间。

## 方案

每日定时抓取校园通知页面 → 启发式规则识别通知类链接 → 与本地状态比对 → 生成"新增通知日报"，全程留下遥测运行日志。

- 技术栈：Python 3.8+ 标准库，**零第三方依赖**
- 当前版本：v1.3.0（2026-11-23）

## 目录

```
02-20Days-Challenge/
├── README.md                 # 本文件
├── src/
│   ├── notice_watcher.py     # 主程序
│   ├── config.json           # 目标页面与关键词配置
│   └── state/seen.json       # 已见通知状态（运行时生成）
├── process_logs/             # 遥测日志 telemetry-YYYY-MM-DD.jsonl（每日同步）
├── iterations/               # 版本迭代记录
└── output/                   # 每日通知日报 digest-YYYY-MM-DD.md（运行时生成）
```

## 使用方法

手动运行一次（在仓库根目录或任意目录均可）：

```bash
python "D:/第三年上学期/考试/ICEC-2026-0130-yangquanyu/02-20Days-Challenge/src/notice_watcher.py"
```

试跑（不更新状态、不写日报，用于改配置后验证）：

```bash
python .../src/notice_watcher.py --dry-run
```

## 每日自动运行（Windows 任务计划）

打开"任务计划程序"→ 创建基本任务：

1. 触发器：每天 08:00
2. 操作：启动程序
   - 程序/脚本：`python`
   - 添加参数：`D:\第三年上学期\考试\ICEC-2026-0130-yangquanyu\02-20Days-Challenge\src\notice_watcher.py`
3. 完成。每天运行后执行一次 `git add . && git commit -m "logs" && git push` 同步遥测日志。

## 遥测日志字段

每条 JSONL 记录包含：时间戳 `ts`、运行 ID `run_id`、版本 `version`、耗时 `duration_ms`、
状态 `status`（ok/partial/error）、页面成功/失败数、识别通知数、新增数 `items_new`、报错明细 `errors`。

## 迭代规范

每次改动在 `iterations/` 下新建 `vX.Y.Z.md`，记录：改了什么、为什么改（对应哪条日志）、改动前后效果。
