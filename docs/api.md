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

**只读标定试算**：在一份已完成清单的短时间窗上，沿现有块读取（`group_chunks_by_rate`）与标定公式（`y=gain*x+offset`、频域 `phase_shift_rad`）计算各通道 RMS、基波相位，以及候选系数与基线版本的差值。**不创建分析任务、报告或标定版本，不写库**，可随时取消。

```json
{
  "start_seconds": 0.0,
  "duration_seconds": 0.12,
  "candidate_coefficients": {
    "Va": {"gain": 2.0, "offset": 0.0, "phase_shift_rad": 0.0}
  },
  "baseline_calibration_version_id": "…（可选，省略时与 gain=1/offset=0/phase=0 对比）",
  "params": {}
}
```

`duration_seconds` 与 `end_seconds` 二选一。返回 `channels[]`（每通道 baseline/candidate/delta，含 `rms_ratio` 与包裹到 (-π,π] 的相位差）、实际命中的采样率段/块序号/样本数，以及 `diagnostics`：

- `missing_chunk`（error）：窗口内声明块未上传，不推算样本、不出指标；窗口外缺块只给 `missing_chunk_outside_window` warning；
- `sample_rate_cross_segment`（error）：窗口跨采样率段边界，不重采样、不拼接，须改选单段窗口；单段内试算时仅给 `sample_rate_changed` warning；
- `non_integer_cycle`（warning）：窗长不等于 `round(cycles*fs/f0)`，指标仅供诊断；
- `fundamental_unresolvable`（error）：窗太短以致基波不占任何 DFT bin，此时仍给真实 RMS 差值，基波相位为 `null`。

`status` 为 `ok/warning/error`，`persisted` 恒为 `false`。窗口越界、同时给时长与终点、候选系数不覆盖完整通道集合等返回 422。

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
