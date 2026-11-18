# 案例 03：Excel 保存的 CSV 读取乱码（GBK）

## 问题现象

课题 10 人工结果表 `results_manual_template.csv` 由本人在 Excel 中填写保存后，
程序用 `encoding="utf-8"` 读取，中文全部变成乱码，csv 解析虽不报错但内容不可用。

## 提问方式

向 AI 描述：文件在 Excel 里显示正常、程序读取乱码；已知 Excel 在中文 Windows 上默认用 GBK 存 CSV。
问：如何不丢内容地转换，以及后续怎么写才能同时兼容 Excel 和 git。

## AI 诊断路径

1. 确认根因：Excel 另存 CSV 默认编码是 ANSI（中文 Windows 即 GBK），文件本身没坏，
   是读取端编码假设错误；
2. 给出方案：读取端用 `encoding="gbk"` 尝试，写端用 `utf-8-sig`（带 BOM）——
   BOM 让 Excel 双击打开 UTF-8 文件时仍然正确识别中文，git 里也能正常 diff；
3. 提醒：不要"修"用户已填的内容，只做编码转换，值必须逐字不变。

## 根因

工具链编码假设不匹配：Excel（GBK 世界）↔ Python 脚本（UTF-8 世界）之间缺一层显式转换。

## 修复

在 `compare_results.py` 中增加 `read_manual()`：用 GBK 读入原档 → 原样写出
`results_manual.csv`（UTF-8 BOM）→ 后续评分只用 UTF-8 版。原 GBK 文件保留不动，
作为"人工原始产出"的证据。

## 验证

转换前后用 `csv.reader` 逐行打印比对：10 行数据、7 个字段全部一致；
UTF-8 版用 Excel 双击打开中文正常。评分脚本随后正确识别出 2 处类别分歧——
若编码没修对，这些分歧根本读不出来。

## 复盘

- 数据交接处的编码问题是人机协作的高发坑：人用 Excel、程序用 UTF-8，中间必须显式声明；
- "带 BOM 的 UTF-8"是同时满足 Excel 用户和 git 的最低成本共识；
- 先验证"内容无损"再做后续处理，顺序不能反。
