# 主要 HTTP 接口

所有时间使用 ISO 8601。原始块编码为 `float32le-interleaved`。

## 清单与上传

### `POST /manifests`

创建不可变分块清单，请求示例：

```json
{
  "name": "line-20261001",
  "nominal_sample_rate": 6000,
  "expected_chunks": [
    {
      "sequence": 0,
      "sha256": "...64 hex...",
      "byte_offset": 0,
      "byte_length": 3600,
      "sample_count": 300,
      "sample_rate": 6000,
      "channels": ["Va", "Vb", "Vc"],
      "start_time": "2026-10-01T00:00:00",
      "end_time": "2026-10-01T00:00:00.049833333",
      "encoding": "float32le-interleaved"
    }
  ]
}
```

创建失败返回 422，`detail.issues` 中列出重叠、空洞、序号、时间或通道集合错误。

### `PUT /manifests/{manifest_id}/chunks/{sequence}/raw`

`multipart/form-data` 字段名：`chunk_file`。可乱序、重传。重复摘要返回已有块；摘要/长度错误返回 422。

### `POST /manifests/{manifest_id}/finalize`

执行最终核对。成功：

```json
{"completed": true, "already_completed": false, "warnings": []}
```

失败返回 422，issues 不做静默合并。并发时只有一个请求能完成首次转换。

### `GET /manifests/{id}/preview?calibration_version_id=...`

返回分段、采样率、降采样波形和缺口/质量 issue，供 Vue/ECharts 展示。后端使用磁盘 memmap 和降采样，避免把数 GB 原始波形全部放入响应内存。

### `POST /manifests/{id}/calibration-preview`

标定员发布新版本前，在一份**已完成**清单的短时间窗上试算候选系数。只读，不创建分析任务、报告或标定版本，也不写数据库。

请求体（二选一提供候选）：

```json
{
  "start_seconds": 0.0,
  "sample_count": 720,
  "fundamental_hz": 50.0,
  "cycles_per_window": 6,
  "candidate_coefficients": {
    "Va": {"gain": 2.0, "offset": 0.0, "phase_shift_rad": 0.0}
  }
}
```

也可用 `"candidate_version_id": "<calibration id>"` 试算一个已保存版本，并可
用 `"baseline_version_id"` 指定对比基线（默认当前 active 版本）。候选与基线
必须恰好提供一种，且系数需覆盖清单全部通道。

计算沿现有块读取路径（`group_chunks_by_rate`，仅拉取窗口涉及的块）和同一
标定公式（`y = gain*x + offset`，频域常数相位）输出每个通道的：

- 基线 / 候选 RMS 与基波相位（DFT 单频-bin，度与弧度）；
- 差值 `delta.rms` 与包裹到 `(-π, π]` 的 `delta.fundamental_phase_*`。

例如 gain 翻倍且 offset=0 时候选 RMS 恰为基线 2 倍；纯 `phase_shift_rad` 只
改变基波相位、不改变 RMS。

明确诊断（HTTP 仍为 200，体现在 `status`/`diagnostics`）：

- `preview_missing_chunks`（error）：窗口涉及的块未上传或字节缺失，绝不补样本；
- `preview_crosses_rate_boundary`（error）：窗口跨越采样率变化段，不做插值拼接；
- `preview_window_beyond_recording` / `preview_window_out_of_range`（error）：窗口越界，不补零；
- `non_integer_cycle`（warning）：样本数不等于 `round(cycles_per_window*fs/f0)`，相位/bin 指标仅供诊断；
- `preview_start_off_sample`（warning）：起点未落在采样点，已取最近样本。

取消预览只是在前端丢弃结果；服务器没有任何状态需要回滚。正式任务仍只有在
`POST /analysis-tasks` 显式选择并冻结标定版本后才会使用新系数。

## 标定

### `POST /calibrations`

```json
{
  "channel_set_hash": "manifest.channel_set_hash",
  "change_note": "CT 二次接线复核后修订",
  "coefficients": {
    "Va": {"gain": 1, "offset": 0, "phase_shift_rad": 0, "saturation_low": -450, "saturation_high": 450}
  }
}
```

新 active 版本会让使用旧 active 版本发布的报告进入 `needs_review`。

## 任务与报告

### `POST /analysis-tasks`

```json
{
  "manifest_id": "...",
  "calibration_version_id": "...",
  "params": {"fundamental_hz": 50, "cycles_per_window": 6, "max_harmonic": 15},
  "idempotency_key": "lab-job-123"
}
```

创建时冻结清单、标定和参数。若使用 Celery，提交后自动发送 `run_analysis`；本地测试可调用 `/run`。

### 执行、重试、取消

- `POST /analysis-tasks/{id}/run`
- `POST /analysis-tasks/{id}/retry`
- `POST /analysis-tasks/{id}/cancel`
- `POST /maintenance/recover-stale-tasks`

### `GET /reports/{id}`

报告状态：

- `published`：正常发布；
- `needs_review`：标定后来更新，需要复核；
- `diagnostic_failed`：分析有 error 阶段，保留诊断但不是完成报告。
