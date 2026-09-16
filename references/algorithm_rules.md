# 算法异常检测规则参考

## 内置脚本

检测脚本位于 `scripts/anomaly_detect.py`，为完整独立实现。

脚本输出统一带 `[算法检测]` 标识。该标识表示结果来自 Skill 内置算法；如果用户另行提供 `[AI检测]` 结果，只把它当作外部来源，不与本脚本结果混淆。

推荐调用：

```bash
python scripts/anomaly_detect.py data.tsv --time-col 日期 --dimension-cols 城市,城市id --metric-cols 指标1,指标2 --format markdown
```

## 默认参数

| 算法 | min_data_points | threshold | confidence_base | extra |
|---|---:|---:|---:|---|
| `yoy` | 5 | 0.5 | 0.7 | `{}` |
| `robust_zscore` | 4 | 3.0 | 0.7 | `{}` |
| `stl` | 9 | 4.0 | 0.7 | `{period: 7}` |
| `mann_kendall` | 5 | 1.96 | 0.7 | `{min_change_pct: 0.10, min_recent_deviation_pct: 0.10}` |
| `sliding_window_t` | 8 | 2.0 | 0.7 | `{min_change_pct: 0.10, min_recent_deviation_pct: 0.10}` |
| `isolation_forest` | 4 | - | 0.7 | `{window_ratio: 0.25, score_threshold: 0.65}` |

## 统一异常程度

异常程度不要只看百分比或 `confidence`，必须同时考虑绝对量、持续性和业务影响。

| 等级 | 置信度/业务条件 |
|---|---|
| 高 | `confidence >= 0.90` 且绝对量/业务影响足够大；或高体量维度出现多指标共振 |
| 中 | `0.70 <= confidence < 0.90`；或持续趋势明显但未达到强突变 |
| 低 | `0.50 <= confidence < 0.70`；或低基数高百分比波动 |
| 正常 | `confidence < 0.50`、无算法触发，或低基数波动无持续性 |

## 聚合规则

1. 每个算法独立判断是否异常。
2. 只保留 `is_anomaly=True` 的算法结果作为候选触发证据。
3. 如果没有算法触发，整体结论为正常。
4. 如果一个或多个算法触发，选择 `confidence` 最大的触发结果作为算法主结果。
5. 再执行生产告警收敛：低基数降权、高体量优先、趋势持续优先、多指标共振优先。
6. 最终异常程度按“算法主结果 + 业务收敛”共同判断。
7. 辅助触发算法必须在说明中列出，但候选异常不要直接等同最终告警。

## 1. `yoy`：同环比突变检测

### 适用

适合日粒度、小时粒度、周粒度等有明确前一参考点和周期参考点的数据。

### 参考点

日粒度默认：

- 前一参考点：昨日，`previous_lag = 1`。
- 周期参考点：上周同期，`seasonal_lag = 7`。

### 计算

```text
previous_change_rate = (current - previous_value) / previous_value
seasonal_change_rate = (current - seasonal_value) / seasonal_value
```

### 异常条件

```text
previous_change_rate 与 seasonal_change_rate 方向一致
且 abs(previous_change_rate) > threshold
且 abs(seasonal_change_rate) > threshold
```

默认：`threshold = 0.5`。

### confidence

```text
effective_change = min(abs(previous_change_rate), abs(seasonal_change_rate))
confidence = min(1.0, (effective_change - threshold) / threshold * 0.5 + confidence_base)
```

## 2. `robust_zscore`：稳健 Z-Score

### 适用

适合判断最新点相对历史中位水平是否出现尖峰或断崖下跌。

### 计算

```text
median = median(values)
mad = median(abs(values - median))
robust_z = 0.6745 * (current - median) / mad
abs_z = abs(robust_z)
```

### 异常条件

```text
abs_z > threshold
```

默认：`threshold = 3.0`。

### confidence

```text
confidence = min(1.0, (abs_z - threshold) / threshold * 0.3 + confidence_base)
```

## 3. `stl`：趋势/周期残差检测

### 适用

适合有稳定周期的数据。数据量过短或周期不稳定时只能作为辅助证据。

### 计算

```text
trend = moving_average(values, period)
seasonal = seasonal_average(values, period)
residual = values - trend - seasonal
z_score = abs(current_residual) / std(residual)
```

### 异常条件

```text
z_score > threshold
```

默认：`threshold = 4.0`，`period = 7`。

### confidence

```text
confidence = min(1.0, (z_score - threshold) / threshold * 0.3 + confidence_base)
```

## 4. `mann_kendall`：趋势检验

### 适用

适合判断持续上升、持续下降等趋势异常，不适合作为单点突变的唯一依据。

### 计算

```text
s = sum(sign(values[j] - values[i])) for i < j
var_s = Mann-Kendall variance with tie correction
z = s / sqrt(var_s)
tau = s / (n * (n - 1) / 2)
front_change_pct = abs(current - front_avg) / abs(front_avg)
recent_change_pct = abs(current - recent_avg) / abs(recent_avg)
```

### 异常条件

```text
abs(z) > threshold
且 front_change_pct >= min_change_pct
且 recent_change_pct >= min_recent_deviation_pct
```

不要求当前值方向与整体趋势一致。

默认：`threshold = 1.96`，`min_change_pct = 0.10`，`min_recent_deviation_pct = 0.10`。

### confidence

```text
confidence = min(1.0, (abs(z) - threshold) / threshold * 0.3 + confidence_base)
```

## 5. `sliding_window_t`：滑动窗口 T 检验

### 适用

适合判断最近一段数据是否相对上一段发生均值位移或台阶式变化。

### 计算

```text
window_size = max(2, min(len(values) // 4, 7))
first_window = values[-2 * window_size : -window_size]
second_window = values[-window_size :]
mean1 = mean(first_window)
mean2 = mean(second_window)
se = sqrt(var(first_window) / n + var(second_window) / n)
t_stat = (mean2 - mean1) / se
front_change_pct = abs(current - mean1) / abs(mean1)
recent_change_pct = abs(current - mean2) / abs(mean2)
```

### 异常条件

```text
abs(t_stat) > threshold
且 front_change_pct >= min_change_pct
且 recent_change_pct >= min_recent_deviation_pct
```

默认：`threshold = 2.0`，`min_change_pct = 0.10`，`min_recent_deviation_pct = 0.10`。

### confidence

```text
confidence = min(1.0, (abs(t_stat) - threshold) / threshold * 0.3 + confidence_base)
```

## 6. `isolation_forest`：孤立森林

### 适用

适合发现形态上孤立的异常点。历史数据越多越可靠，短序列只作为辅助证据。

### 计算

```text
scores = isolation_score(values)
recent_score = scores[-1]
```

### 异常条件

```text
recent_score > score_threshold
```

默认：`score_threshold = 0.65`。

### confidence

```text
range_above = 1.0 - score_threshold
confidence = confidence_base + (recent_score - score_threshold) / range_above * (1.0 - confidence_base)
confidence = min(1.0, confidence)
```

## 生产告警收敛规则

### 低基数降权

以下情况默认不要直接标为高异常：

```text
单量：1 -> 2、1 -> 3、2 -> 4
金额：1 元 -> 6 元、0.9 元 -> 4 元
补偿率：0 -> 0.01、0.01 -> 0.02 且单量很小
```

处理方式：

1. 若只有百分比大、绝对量很小，降为 `低` 或观察项。
2. 若低基数波动同时伴随连续多日趋势、多指标共振，可升为 `中`。
3. 若维度是高体量城市/核心渠道，且绝对影响明显，可保留为正式告警。

### 趋势优先

趋势类异常优先关注以下模式：

```text
近 7 日均值相对前窗明显下降/上升
近 3 日均值相对前窗明显下降/上升
最近多个点全部低于/高于前窗均值
```

这种模式可由 `mann_kendall`、`sliding_window_t` 或窗口均值对比辅助解释；输出时归因到具体算法或明确写为“窗口均值收敛规则”。

### Top 异常输出

同一指标维度很多时：

1. 默认只输出最重要的 Top 异常。
2. 同一 `ExceptionMeasure` 最多保留 3 个最严重维度。
3. 其余低影响候选写为“观察项”，不要混入最终告警列表。
4. 当已有算法运行结果时，以实际运行结果为最终告警，Skill 额外发现的仅作为复核建议。

## 推荐算法组合

| 任务 | 优先算法 | 辅助算法 |
|---|---|---|
| 生产口径判断昨天是否异常 | `mann_kendall`, `sliding_window_t` | `yoy`, `robust_zscore` |
| 明确要求周同比+日环比 | `yoy` | `robust_zscore` |
| 判断单点突增/突降 | `yoy`, `robust_zscore` | `isolation_forest` |
| 判断趋势异常 | `mann_kendall` | `sliding_window_t` |
| 判断周期残差异常 | `stl` | `robust_zscore` |
| 判断台阶式变化 | `sliding_window_t` | `mann_kendall` |
