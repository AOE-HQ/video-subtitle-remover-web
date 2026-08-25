# HTTP API 参考

服务默认地址为 `http://127.0.0.1:8000`，交互式文档位于 `/docs`，OpenAPI Schema 位于 `/openapi.json`。

## 鉴权

设置 `WEB_API_KEY` 后，除健康检查和文档页面外的 API 都需要鉴权。支持两种请求头：

```http
X-API-Key: your-api-key
```

或：

```http
Authorization: Bearer your-api-key
```

API Key 不支持 URL 查询参数。修改 `WEB_API_KEY` 后需要重启服务。

## 接口列表

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/health` | 否 | 服务、队列和 FFmpeg 状态 |
| `GET` | `/api/config` | 是 | 模式、坐标顺序和服务限制 |
| `GET` | `/api/jobs` | 是 | 最近任务列表 |
| `POST` | `/api/jobs` | 是 | 上传视频并创建异步任务 |
| `GET` | `/api/jobs/{job_id}` | 是 | 查询单个任务 |
| `POST` | `/api/jobs/{job_id}/pause` | 是 | 暂停排队或运行任务 |
| `POST` | `/api/jobs/{job_id}/resume` | 是 | 恢复暂停任务 |
| `GET` | `/api/jobs/{job_id}/download` | 是 | 下载成功任务的 MP4 结果 |

当前版本没有取消或删除任务的接口。

## 健康检查

```bash
curl http://127.0.0.1:8000/api/health
```

示例响应：

```json
{
  "status": "ok",
  "service": "video-subtitle-remover",
  "worker": true,
  "queue_size": 0,
  "ffmpeg_available": true
}
```

`status=ok` 表示 HTTP 服务和队列线程可用，不代表所有模型都已成功加载。模型在任务子进程中按需加载。

## 创建任务

`POST /api/jobs` 使用 `multipart/form-data`，成功返回 `202 Accepted`。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `file` | 文件 | 必填 | 常见视频格式，默认上限 2 GiB |
| `mode` | 字符串 | `sttn-auto` | 处理模式 |
| `subtitle_area_coords` | JSON 字符串 | 空 | 一个区域或多个区域 |
| `pad` | 整数 | `6` | 固定水印区域向外扩展的像素数，范围 `0..512` |
| `detect_mode` | 字符串 | `PP_OCRv5_SERVER` | `PP_OCRv5_SERVER` 或 `PP_OCRv5_MOBILE` |

支持的 `mode`：

- `sttn-auto`
- `sttn-det`
- `lama`
- `propainter`
- `opencv`
- `logo-lama`

区域顺序为 `[ymin, ymax, xmin, xmax]`。可以传单个区域：

```text
[30, 120, 40, 260]
```

也可以传多个区域：

```text
[[30, 120, 40, 260], [620, 700, 80, 1160]]
```

`logo-lama` 至少需要一个区域；其他模式可以留空。

### 固定水印示例

```bash
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H 'X-API-Key: your-api-key' \
  -F 'file=@input.mp4' \
  -F 'mode=logo-lama' \
  -F 'subtitle_area_coords=[[30,120,40,260]]' \
  -F 'pad=6'
```

### OCR 检测示例

```bash
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H 'X-API-Key: your-api-key' \
  -F 'file=@input.mp4' \
  -F 'mode=sttn-det' \
  -F 'detect_mode=PP_OCRv5_SERVER'
```

## 任务对象

创建、查询、暂停和恢复接口均返回任务对象：

```json
{
  "id": "f3e81cc9a54b4eeeb7516dff7a45f87c",
  "status": "queued",
  "mode": "sttn-det",
  "mode_label": "STTN Detect",
  "filename": "input.mp4",
  "created_at": "2026-08-25T10:00:00+00:00",
  "updated_at": "2026-08-25T10:00:00+00:00",
  "progress": 0,
  "message": "已进入处理队列",
  "logs": ["文件上传完成，等待处理。"],
  "can_pause": true,
  "can_resume": false
}
```

成功后会增加：

```json
{
  "download_url": "/api/jobs/f3e81cc9a54b4eeeb7516dff7a45f87c/download"
}
```

失败时会增加 `error` 字段。`logs` 最多返回最近 240 行。

## 状态流转

```text
queued -> processing -> succeeded
                    \-> failed

queued ------> paused -> queued
processing --> paused -> processing
```

| 状态 | 含义 |
| --- | --- |
| `queued` | 文件已上传，等待单 worker 处理 |
| `processing` | 子进程正在运行 |
| `paused` | 排队任务或子进程已暂停 |
| `succeeded` | 输出文件已生成，可下载 |
| `failed` | 处理失败，查看 `error` 和 `logs` |

暂停运行任务使用 POSIX 的 `SIGSTOP`/`SIGCONT`，Windows 上不支持运行中暂停。暂停不会释放模型显存，也不会让单 worker 自动执行下一个任务。

## 查询任务

查询单个任务：

```bash
curl -H 'X-API-Key: your-api-key' \
  http://127.0.0.1:8000/api/jobs/<job_id>
```

查询最近任务：

```bash
curl -H 'X-API-Key: your-api-key' \
  'http://127.0.0.1:8000/api/jobs?limit=20'
```

`limit` 范围为 `1..100`。列表按创建顺序倒序返回。

推荐客户端在 `queued`、`processing` 或 `paused` 状态下每 1 到 3 秒轮询一次，避免无意义的高频请求。

## 暂停与恢复

```bash
curl -X POST -H 'X-API-Key: your-api-key' \
  http://127.0.0.1:8000/api/jobs/<job_id>/pause

curl -X POST -H 'X-API-Key: your-api-key' \
  http://127.0.0.1:8000/api/jobs/<job_id>/resume
```

对已结束任务调用暂停，或对非暂停任务调用恢复，会返回 `409 Conflict`。

## 下载结果

```bash
curl -L -H 'X-API-Key: your-api-key' \
  -o output.mp4 \
  http://127.0.0.1:8000/api/jobs/<job_id>/download
```

任务未成功时返回 `409`；记录存在但结果文件已被外部清理时返回 `410 Gone`。输出统一以 `video/mp4` 返回。

## 常见错误

| HTTP 状态 | 常见原因 |
| --- | --- |
| `401` | API Key 缺失或错误 |
| `404` | 任务 ID 不存在 |
| `409` | 当前状态不允许暂停、恢复或下载 |
| `413` | 文件超过 `VSR_MAX_UPLOAD_BYTES` |
| `415` | 不支持的文件扩展名 |
| `422` | 模式、坐标、`pad` 或 OCR 模式无效 |
| `429` | 等待队列或任务记录已满 |
| `501` | 当前操作系统不支持暂停运行进程 |

## 持久化与并发限制

- 任务元数据只在当前 Web 进程内存中保存。
- 服务重启后，旧的上传和结果文件仍在磁盘，但无法再通过任务 API 查询。
- `VSR_MAX_JOBS` 淘汰任务记录时不会删除对应磁盘文件。
- 不要增加 Uvicorn worker 数量。多个 Web worker 会创建互不共享的队列，并可能同时使用同一块 GPU。
- 当前队列适合单机单 GPU。分布式或多 GPU 场景需要外部队列和独立 worker。
