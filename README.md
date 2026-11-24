# 设备安全通告采集与适用批次处置

自动化集成商用于持续监测监管机构与设备厂安全通告、把通告适用范围匹配到
多工厂机器人/控制柜台账，并驱动「工程师决策 → 工厂通知 → 工作单 → 检修
回执 → 解除评审」处置闭环的 Python 项目。**仅依赖 Python 3.10+ 标准库。**

合成机器人公告、设备台账和维修工作样例位于 `samples/`。日期采用 ISO 格式，
序列号范围按**字符串闭区间**理解。CSV 为 UTF-8、首行列名、空单元格表示
来源未提供值。所有示例标识均为合成数据。

## 核心设计原则

1. **原始证据只追加、内容寻址**：每次抓取到的字节按 SHA-256 存入证据目录，
   通告每个版本、每份附件都引用不可变哈希；静默更正与附件替换保留新旧两份。
2. **网页命中不等于维修指令**：采集与匹配只产出带证据的适用结论；风险事项
   必须经安全工程师显式决策（停用/检查/升级/不动作）后才能生成工作单。
3. **范围修订全程可追溯**：评估、决策、通知、工单、检修、解除均为只追加
   历史；工单锚定创建时的通告版本，通告撤回/缩窄不回写已完成记录。
4. **不可信数据隔离**：外部页面、门户导出与附件是数据不是代码；解析器只做
   结构化提取，安装后可全程用 `python3 -I` 运行（忽略环境变量与当前目录）。

## 目录结构

```
robotnotice/
  versions.py    型号别名归一化、序列号字符串闭区间、固件段级通配匹配
  evidence.py    只追加的内容寻址证据库
  storage.py     SQLite 表结构、连接与审计事件
  sources.py     HTTP/文件抓取器（ETag 条件请求）与 CSV/JSON/HTML 解析器
  collector.py   轮询、差异识别（新增/更正/撤回/再生效/附件替换）
  assets.py      台账导入、型号别名与待核项（缺序列号/冲突/未知型号）
  matching.py    通告当前版本对全量设备的适用评估
  workflow.py    风险事项打开/重开/关闭、现场限制、工程师决策
  dispatch.py    工厂通知、工作单、检修回执、解除评审
  scheduler.py   固定间隔持续监测
  console.py     调度台查询（状态、适用理由、待办、历史、证据链）
  cli.py         命令行入口
tests/           unittest 测试（中文 docstring），含本地 HTTP 服务用例
samples/         合成输入样例
```

## 数据模型要点

- `notice_revisions.change_kind`：`new` / `new-revision` /
  `revision-corrected`（同版本号静默更正）/ `withdrawn` / `reactivated` /
  `unchanged`。
- `applicability_evals.result`：`applicable` / `not-applicable` /
  `unverifiable`（型号命中但缺序列号、无法核验区间，转人工待核）。
- `risk_cases`：每台设备 × 每条通告唯一一行，状态 `open` / `in-progress` /
  `closed`，带 `reopened_count` 与关闭原因。
- 通告撤回或范围缩窄：自动关闭不再适用的事项，取消尚未发送的通知与未完成
  工单；已完成检修与解除记录按原通告版本保留。修订后再次覆盖已关闭设备时，
  自动重开事项并指向新版本证据。
- `site_policies` 表达客户现场动作限制；越权动作默认拒绝，显式覆盖会在
  决策行与审计事件中留痕。

## 安装与测试

```bash
python3 -m pip install -e .          # 安装后可用 robotnotice 命令与 python3 -I
python3 -m unittest discover -s tests -v
python3 -I -m unittest discover -s tests   # 隔离模式运行（推荐处理外部数据时）
```

## 命令行工作流

全局参数 `--db`、`--evidence` 默认为 `data/robotnotice.db` 与 `data/evidence/`，
所有命令支持 `--json` 与 `--actor`。

```bash
# 1. 注册并轮询公告源（file/http × csv_feed/json_feed/html_page）
robotnotice source-add reg1 监管公告 --kind file \
  --url samples/notices.csv --parser csv_feed
robotnotice collect reg1

# 2. 维护型号别名、导入台账并处理待核项
robotnotice alias 机械臂二号 RB-2 --note 门户旧叫法
robotnotice ledger-import samples/assets.csv
robotnotice verification
robotnotice verification-resolve 1 "现场补录序列号 145"

# 3. 评估适用性并同步风险事项（采集后可重复执行）
robotnotice evaluate --notice N-7
robotnotice sync --notice N-7

# 4. 安全工程师结合客户现场限制决策
robotnotice policy F-11 --site West --allow inspect,upgrade
robotnotice decide RC-A2-N-7 --action upgrade --rationale "固件需升级"

# 5. 调度员通知工厂、生成并执行工作单
robotnotice notify RC-A2-N-7 --message "请安排升级"
robotnotice notify-send NT-0001
robotnotice work-order RC-A2-N-7 --notification-id NT-0001
robotnotice work-start WO-0001
robotnotice inspect-submit WO-0001 --observed-firmware 2.2 --findings "升级完成"

# 6. 安全工程师解除评审（--reject 表示不满足解除条件）
robotnotice release RC-A2-N-7 --rationale "升级证据满足解除条件"

# 7. 调度台查询
robotnotice status --customer F-11 --notice N-7
robotnotice pending --customer F-11
robotnotice history --asset A1 --notice N-7
robotnotice timeline RC-A2-N-7
robotnotice notices
robotnotice evidence --notice N-7
```

持续监测可在独立进程中使用 `robotnotice.scheduler.Scheduler`（默认只采集
不派单，保持采集与处置解耦）。

## 与样例对应的语义

`N-7` 有两个版本：rev1 范围 `RB-2 / 100–180 / 2.x`，rev2 更正为
`RB-2 / 120–160 / 2.1`。因此当前版本下 A1（序列号 115、固件 2.0）不再
适用，只保留历史评估；A2（145、2.1）适用并生成风险事项。若只按型号
匹配，会把 A1 误停用。`N-8` 为已撤回通告，不会为其打开新事项；其历史
检修记录仍按原版本保留。

## 安全约束

- HTTP 抓取有下载体积上限与超时，条件请求优先，失败记录入 `fetch_runs`
  而不是中断轮询。
- 证据目录与数据库文件不会被解析器执行；处理下载/解压来的外部文件时，
  把文件放在独立空目录、脚本路径作为参数传入，并用 `python3 -I` 运行。
- 所有状态变化写 `audit_events`（操作人、动作、实体、JSON 明细）。
