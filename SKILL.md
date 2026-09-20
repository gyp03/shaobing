---
name: jg-anomaly-detection-algorithms
description: 用于项目时间序列指标异常检测、本地算法解释、JPG 趋势图生成，以及基于元数据的告警发布。
author: MR.GUO
allowed-tools: 
disable: false
---

# 算法异常检测

## 目标

使用项目内置脚本检测时间序列指标异常，并按生产告警口径收敛：优先输出少量、可解释、业务影响更大的 `高` / `中` 异常，默认降权低基数百分比噪声。

## 适用场景

- 判断昨日、最近一小时、最近一天、最近一周等最新指标是否异常。
- 使用本地算法、同环比、Z-Score、STL、Mann-Kendall、滑动窗口 T 检验、孤立森林检测异常。
- 解释 `confidence`、异常程度、异常原因、触发算法。
- 为异常指标生成本地 JPG 趋势图。
- 用户明确要求按生产或预生产流程发布告警。

## 固定算法池

只能使用以下内置算法：

1. `yoy`：同环比突变检测。
2. `robust_zscore`：稳健 Z-Score 检测。
3. `stl`：趋势/周期残差检测。
4. `mann_kendall`：Mann-Kendall 趋势检验。
5. `sliding_window_t`：滑动窗口 T 检验。
6. `isolation_forest`：孤立森林检测。

详细公式和默认参数见 `references/algorithm_rules.md`。

## 算法适配选择

- 未显式配置算法时，先基于序列长度、当前值偏离中位数、当前值偏离前窗/前段均值、周期参考点、趋势统计量等特征，为每个候选算法计算适配分。
- 达到适合阈值的算法全部运行；若没有任何算法达到阈值，运行适配分最高的一个算法，确保不会出现“一个算法都不跑”。
- 显式配置 `detection_algor`、`detectors` 或 `检测算法` 时，尊重配置，只在配置的算法集合内做适配选择。
- 每次检测输出 `[DETECTOR_SELECT]` 日志，标记各算法适配分；带 `*` 的算法为本次实际运行算法。

## 脚本职责

- `scripts/anomaly_detect.py`：本地异常检测、候选异常和生产口径收敛。
- `scripts/render_anomaly_chart.py`：本地 JPG 趋势图渲染。
- `scripts/odps_io.py`：ODPS / OSS 配置读取、查询、执行和心跳。
- `scripts/metadata_loader.py`：元数据读取、字段推断、`ex_prompt` 和 `detection_algor` 解析。
- `scripts/publish_anomaly_alerts.py`：发布编排，串联元数据取数、检测、图表、OSS 上传和 ODPS 写入。

## 本地检测流程

1. 识别时间列、维度列、指标列和时间粒度。
2. 按维度组合拆分时间序列；无维度时按全局序列检测。
3. 若元数据显式配置 `detection_algor` / `检测算法`，只运行配置的算法；未配置时先做数据画像，为固定算法池打适配分，再运行适合算法。
4. 算法选择必须至少选中一个算法：若所有算法均未达到适合阈值，选择适配分最高的算法，而不是固定兜底某个算法。
5. 每个算法输出 `is_anomaly`、`confidence`、`anomaly_type`、`anomaly_score`、`explanation`。
6. 统一标记脚本产出结果为 `[算法检测]`；`[AI检测]` 仅表示外部 AI / 大模型检测。
7. 对候选异常做生产口径收敛：低基数降权、高体量优先、持续趋势优先。
8. 默认只输出最终告警和关键观察项，避免机械罗列全部候选波动。

示例：

```bash
python scripts/anomaly_detect.py data.tsv --time-col 日期 --dimension-cols 城市,城市id --metric-cols 订单量,GMV --format markdown
```

常用参数：

- `--time-col`：时间列。
- `--dimension-cols`：维度列，多个列用英文逗号分隔。
- `--metric-cols`：指标列，多个列用英文逗号分隔。
- `--granularity`：时间粒度，默认 `daily`。
- `--period`：周期长度，日粒度默认 `7`。
- `--low-base-downgrade`：默认 `on`；只有用户明确要求不降权时才设为 `off`。
- `--format`：`markdown` 或 `json`。

## 图表生成

使用 `scripts/render_anomaly_chart.py` 生成本地 JPG，不依赖外部服务。图表必须包含：

- X 轴时间、Y 轴指标值、指标折线和最新检测点。
- 维度信息、异常等级、主触发算法和简短原因。
- `threshold` 阈值告警图必须绘制该指标配置的全部上限和下限水平线，并标注原始阈值值。
- 图表和告警说明中的均值等普通数值最多保留两位小数；绝对值小于 `1` 时最多保留四位，避免冗长小数。
- 生产发布标题：`${业务名称}/${指标名称} 异常趋势图`。
- 发布文件名：`${yyyymmddhhmiss}-${16位哈希}.jpg`；时间取自 `run_id`，哈希后缀取自幂等键，确保同一运行跨重试路径稳定并降低并发覆盖风险。

示例：

```bash
python scripts/render_anomaly_chart.py data.tsv --time-col 日期 --metric-col 订单量 --detector yoy --severity 中 --dimensions "城市=武汉" --reason "同环比同时明显上涨" --period 7 --output anomaly.jpg
```

## 发布模式

默认只做本地检测和图表生成。只有用户明确要求“生产发布流程”“预生产发布流程”“上传 OSS 并写入 ODPS”“写入告警表”等，才使用发布脚本并加 `--execute`。

同一用户请求或同一 Agent 会话只能执行一次带 `--execute` 的完整发布命令。发布脚本已经包含元数据查询、业务取数、异常检测、幂等校验、图表上传和结果写入，禁止在发布前后再执行另一遍完整发布进行“验证”。需要复核时只允许只读查询结果表；看到 `[RUN_DONE]` 后立即汇报并结束。

### 发布命令

```bash
# 生产发布：标题不带“预发测试-”
python scripts/publish_anomaly_alerts.py --from-metadata --publish-mode production --execute

# 日调度预生产发布：标题统一添加“预发测试-”
python scripts/publish_anomaly_alerts.py --from-metadata --monitor-period 日调度 --publish-mode preproduction --execute

# 手动数据发布
python scripts/publish_anomaly_alerts.py data.tsv --time-col 日期 --dimension-cols 城市 --metric-cols 订单量,GMV --task-name 算法异常检测告警 --business-name 订单监控 --publish-mode production --execute
```

### 元数据流程

默认元数据表：`ygcx_dw_pro.ods_shaobing_busi_schema`，仅读取 `status='on'` 的配置。核心字段：

- `id` / `主键id`
- `business_name` / `业务名称`
- `metric_name` / `指标名称`
- `data_sql` / `提数sql`
- `detection_algor` / `检测算法`
- `ex_prompt` / `提示词`
- `owner` / `负责人`
- `monitor_period` / `监控周期：日/小时/分钟`

每条元数据执行 `data_sql` 后再检测；无告警也要写检测明细表 `ygcx_dw_pro.ads_shaobing_busi_detect_detail`。

### 列识别规则

1. 显式配置优先：`ex_prompt` / 参数中的 `time_col`、`metric_cols`、`dimension_cols` 优先。
2. 指标列识别必须排除显式维度列，避免空维度或数值维度被误当指标。
3. 无显式指标时，优先解析 `data_sql` 最外层 SELECT：
   - 聚合表达式别名，如 `sum(x) AS 完单量`、`count(*) AS 订单数`，识别为指标。
   - 无聚合表达式时，按“维度在前、指标在后”的 SQL 输出约定，识别末尾派生别名数值列为指标，如 `finish_order_num AS 每日完单量`。
4. 若 SQL 表达式无法识别，回退为结果中除时间列和显式维度列外的所有有效数值列。
5. 全量为空的维度列不参与分组和图表展示。
6. 结果表 `metric_name` 必须使用提数 SQL 结果中的指标列名；元数据 `metric_name` 仅用于辅助识别。

### `ex_prompt` 配置

`ex_prompt` 可是自由文本，也可使用 JSON 或 `key=value`。支持键：

- `time_col` / `时间列`
- `metric_cols` / `指标列`
- `dimension_cols` / `维度列`
- `severity_levels` / `发布等级`
- `period` / `周期`
- `low_base_downgrade` / `低基数降权`
- `detectors` / `检测算法` / `使用算法` / `detection_algor`

自然语言阈值，如 `指标值超过3000就进行告警`，会作为该指标的发布门禁：当前值未命中阈值时，即使其他算法检测为异常也不发布；命中阈值时可发布自定义阈值告警，或放行已命中的算法告警。阈值告警原因必须同时展示当前值、上限/下限及具体差值，例如 `当前值0.105低于下限0.1086，比下限低0.0036`。

仅阈值监控：统一使用 `threshold` 作为配置和结果中的算法名。当元数据 `detection_algor` 或 `ex_prompt` 中的 `使用算法` / `检测算法` 配置为 `threshold` 或 `阈值` 时，不运行算法池，只根据阈值规则生成告警；该模式必须配置至少一条阈值规则，否则任务失败并提示补充规则。

配置示例：

```text
detection_algor = threshold
ex_prompt = 指标值超过3000就进行告警;发布等级=高
```

或：

```text
ex_prompt = 使用算法=threshold;指标值低于100就进行告警;发布等级=中
```

### 结果表规则

结果表：`ygcx_dw_pro.ads_shaobing_busi_detect_result`。

- `title`：生产为 `${基础标题}`；预生产为 `预发测试-${基础标题}`。生成前必须去掉已有 `预发测试-`，确保最多出现一次。
- `result`：写当前异常指标值 `current_value`。
- `metric_name`：写实际异常指标列名。
- `alarm_detector`：本 Skill 写 `1`；只有外部 AI 智能体检测才写 `2`。
- `create_user`：使用元数据 `owner`。
- 默认只发布 `中,高`；如 `ex_prompt` 配置 `severity_levels`，按配置发布。
- `content` 必须写入 `analysis`、`source_time`、`run_id`、`idempotency_key`、`ex_prompt`、`publish_mode`、`pipeline_version`、`code_fingerprint` 等关键上下文，确保生产和预生产可校验是否同一代码版本。
- `analysis` 智能分析必须在开头明确展示监测数据时间，例如 `监测时间：2026-09-16 08:00`，不得只显示告警生成时间。

### 幂等、断点和并行

- 每次新的用户发布请求必须使用新 `run_id`，或省略让脚本自动生成。
- 只有同一次任务因超时、断开或临时失败恢复时，才复用首次日志里的同一个 `run_id`。
- 告警幂等键是跨运行稳定的业务身份键：`title + metric + dimensions + source_time + publish_mode` 的 SHA-256；`run_id` 和可变化的元数据主键只用于执行追踪，不得参与业务防重。
- 加载最近告警时同时兼容旧版包含 `run_id` 的幂等记录，并从 `detail/source_time/publish_mode` 重建业务身份，避免升级后重复发布历史监测数据。
- 每个业务发布前只允许批量查询最近 30 天已有幂等状态；禁止逐告警查询 ODPS。
- INSERT 必须带 `NOT EXISTS` 作为第二层防重。
- 检查点默认在 `logs/checkpoints/`，仅 `--execute --from-metadata` 启用。
- 检查点必须记录 `pipeline_version` 和 `code_fingerprint`；当前脚本指纹不一致时不得复用旧检测缓存，必须重新检测。
- 检测完成后立即缓存检测结果；告警和明细全部成功后才标记元数据完成。
- 元数据任务默认并行，单任务失败使用相同 `run_id` 自动重试，不得影响其他任务。
- 正常调度、超时恢复和人工重跑禁止使用 `--force-republish`；`--no-resume` 仅用于明确要求重新检测。

## 参数速查

- `--from-metadata`：启用元数据驱动流程。
- `--metadata-sql`：覆盖默认元数据 SQL。
- `--monitor-period`：只运行指定周期；`日调度` 会过滤 `monitor_period='日'`。
- `--publish-mode`：`production` 或 `preproduction`。
- `--metadata-workers`：元数据并行度，默认 `4`。
- `--metadata-retries`：单元数据失败重试次数，默认 `2`。
- `--checkpoint-dir`：检查点目录，默认 `logs/checkpoints/`。
- `--run-id`：逻辑运行 ID；新发布用新 ID，恢复才复用旧 ID。
- `--idempotency-lookback-days`：幂等查询范围，默认 `30`。
- `--severity-levels`：发布等级，默认 `中,高`。
- `--output-dir`：本地 JPG 输出根目录；未传时自动使用工作区根目录（优先 `QODER_WORKSPACE` / `/data/workspace`），不得依赖当前 `cwd`。
- `--log-dir` / `--log-file`：完整运行日志位置。
- `--config`：可选旧 JSON 配置文件；环境变量优先。
- `--alert-table` / `--detail-table`：覆盖结果表或明细表。
- `--execute`：真实上传 OSS 并写入 ODPS；不加则 dry-run。

## 连接配置

默认从环境变量读取，环境变量优先于 JSON 配置。不要在 Skill、日志、命令示例或代码中写真实密钥。

必填：

- `ODPS_ACCESS_ID`
- `ODPS_ACCESS_KEY`
- `ODPS_PROJECT`
- `ODPS_ENDPOINT`
- `ALIYUN_ACCESS_KEY_ID`
- `ALIYUN_ACCESS_KEY_SECRET`
- `OSS_BUCKET_NAME`
- `OSS_REGION`

可选：`OSS_ENDPOINT`、`OSS_DIR`、`OSS_PUBLIC_HOST`、`OSS_URL_EXPIRE_SECONDS`。

依赖：`matplotlib`、`oss2`、`pyodps`。

## 生产告警收敛

- 高业务影响优先：历史均值、近期均值、绝对量大的维度优先。
- 持续趋势优先：近 7 日均值、窗口均值位移或趋势检验明显时，优先于单日百分比突变。
- Mann-Kendall 趋势检验默认同时要求：最新值相对前段均值偏离不低于 `10%`、相对近段均值偏离不低于 `10%`；不要求最新值方向与整体趋势一致。
- 滑动窗口 T 检验默认同时要求：最新值相对前窗均值偏离不低于 `10%`、相对近期窗口均值偏离不低于 `10%`；任一门槛不满足时，即使 T 检验显著也不发布告警。
- 低基数降权：默认启用；`1 -> 2`、`2 -> 6`、`1 元 -> 6 元` 等默认降为观察项。
- 比例类指标谨慎：分母或单量很小时，比例变化只作为辅助证据。

分级口径：

| 异常程度 | 判断条件 | 处置建议 |
|---|---|---|
| 高 | `confidence >= 0.90` 且业务影响足够大 | 直接告警 |
| 中 | `0.70 <= confidence < 0.90`，或趋势明显 | 告警或复核 |
| 低 | `0.50 <= confidence < 0.70`，或低基数高百分比波动 | 观察 |
| 正常 | `confidence < 0.50`、无算法触发或已降噪 | 不告警 |

`confidence` 是规则分数，不是概率；多算法触发时取最高触发算法的 `confidence`。

## 输出格式

默认输出生产口径摘要：

```text
结论：[算法检测] 正常 / 异常
异常程度：高 / 中 / 低 / 无
主异常对象：维度 + 指标
最终置信度：0.xx
主触发逻辑：算法名 + 收敛规则
核心原因：业务可读解释
建议动作：排查 / 观察 / 不处理
候选观察项：如有，单独列出
```

表格分析优先使用：

| 指标/维度 | 当前值 | 触发算法 | 触发逻辑 | confidence | 异常程度 | 是否最终告警 | 判断原因 |
|---|---:|---|---|---:|---|---|---|

## 长任务执行要求

- 必须保持实时输出，避免 60 秒无输出超时。
- 禁止将发布命令直接接到 `tail -300`、`tail -n 300` 等等待 EOF 的管道。
- 禁止使用 `sleep 60`、`sleep 90` 等静默等待；必须等待时每 10 秒输出心跳。
- 等待后台进程时，禁止只执行 `kill -0 <pid>; sleep` 的静默轮询；必须在每轮输出 `WAIT pid=<pid> elapsed=<秒数>`，并可追加 `tail -n 5 /tmp/publish_run.log` 保持实时输出。
- 等待逻辑必须优先识别日志完成标记：成功时以 `[RUN_DONE]` 为最终完成标志；出现 `[TASK_FAILED]` / `Traceback` 时按失败结束。读取最终日志后立即汇报，禁止再次执行带 `--execute` 的发布命令。
- 使用 `grep` 做日志匹配时，若只是统计/观察，必须追加 `|| true`，避免“无匹配”导致命令以 exit code 1 被误判为任务失败。
- Linux 示例：`PYTHONUNBUFFERED=1 python3 -u ... 2>&1 | tee /tmp/publish_run.log`。
- Windows PowerShell 示例：`$env:PYTHONUNBUFFERED='1'; python -u ...`。
- 脚本内部对单次 ODPS 长操作每 20 秒输出心跳。
- 禁止为等待数据或分区落地而创建轮询脚本（如 `/tmp/poll_part.py`），也禁止循环重复查询 ODPS。单次查询成功但返回 0 行时，立即输出 `[NO_DATA]`，将该元数据任务按无数据正常结束，不重试、不等待。
- 只有网络、ODPS 执行异常等真实错误才允许按重试配置恢复；“查询无数据”不属于错误，不得触发重试。
- Qoder Cloud Agent 是干净环境，写本地图表或中间文件前必须先创建父目录；尤其是 `output_dir/shaobing-uploaded-images/YYYYMMDD/*.jpg`，保存前必须 `mkdir -p` 对应日期目录。
- 技能脚本统一使用 LF 行尾，避免云端编辑/patch 因 Windows CRLF 行尾失败；如发现 CRLF，先整体规范化行尾再修改。

## 异常处理原则

- 可恢复问题必须主动修复或使用同一 `run_id` 恢复，不得直接甩给用户。
- 缺依赖先安装；目录问题先创建或切换可写目录；网络、ODPS、OSS 临时错误先确认远端状态再续跑。
- SQL、字段或参数错误先定位并修正；不能吞异常、伪造成功、跳过失败元数据或换新 `run_id` 规避问题。
- 重试耗尽仍失败时，说明根因、已采取动作、远端任务状态、检查点路径和同 `run_id` 恢复命令。

## 代码质量约束

- 优先配置化，避免为每个业务场景复制逻辑。
- 保持单一职责，新增能力先复用已有 helper、数据结构和渲染函数。
- 单脚本接近 `800` 行优先拆分；超过 `1000` 行前必须重构，不继续追加大段逻辑。
- 核心检测逻辑必须本地可测，不依赖真实外部服务。
- 新增参数、阈值、检测器、收敛规则或发布语义时同步更新文档。
- 修改后至少执行语法检查；检测结果变化需用小样例验证。
- 禁止硬编码 token、API Key、webhook 或云服务密钥。
- 文档只保留规则和命令，长解释、公式和历史过程放到 `references/`，避免堆叠重复信息。
