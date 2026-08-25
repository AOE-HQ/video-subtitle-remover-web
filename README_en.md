[简体中文](README.md) | English

<div align="center">
  <img src="design/icon_1024.PNG" alt="VSR Logo" width="128" height="128">
  <h1>Video Subtitle Remover Web & API</h1>
  <p>A Web workspace and asynchronous HTTP API for removing hardcoded subtitles and watermarks from video.</p>
</div>

<div align="center">

![License](https://img.shields.io/badge/License-Apache%202.0-red.svg)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688.svg)
![Platforms](https://img.shields.io/badge/OS-Windows%20%7C%20macOS%20%7C%20Linux-green.svg)

</div>

## About This Fork

This is the Web/API edition of [YaoFANGUK/video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover). It exposes the existing STTN, LAMA, ProPainter, OpenCV, and OCR pipeline through a browser workspace and an asynchronous job API, making it suitable for deployment on a shared LAN GPU server.

The primary entry point is `web_app.py`. The upstream GUI and CLI remain available for compatibility, but they are not the main interface of this repository.

Only process video that you own or are authorized to modify, and follow applicable copyright and platform rules.

## Features

- Browser upload, upload progress, mode selection, processing logs, recent jobs, and result download
- Asynchronous REST API with job IDs and polling
- A serialized single-worker queue that avoids concurrent GPU/model contention
- Pause and resume for queued and running jobs
- Optional API key authentication, CORS, upload limits, and queue limits
- One child process per processing job, isolating model failures from the Web service
- Fixed-region watermark removal and OCR-assisted intermittent subtitle/dynamic text removal
- Generated OpenAPI schema, Swagger UI, and ReDoc

```text
Web browser / API client
           |
           v
  FastAPI + in-memory queue
           |
           v
 Single processing subprocess
           |
           v
STTN / LAMA / ProPainter / OpenCV
```

## Quick Start

Python 3.11 or 3.12 is recommended. Create and activate a virtual environment first:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

PyTorch is intentionally not pinned in `requirements-web.txt`; install a build appropriate for the machine:

```bash
# CPU or macOS
pip install torch torchvision

# For NVIDIA, install a build matching the driver/CUDA runtime:
# https://pytorch.org/get-started/locally/
```

Install the Web and core runtime dependencies:

```bash
pip install -r requirements-web.txt
```

`requirements-web.txt` installs CPU PaddlePaddle by default. To run Paddle OCR on an NVIDIA GPU, replace `paddlepaddle` with the official `paddlepaddle-gpu` package that matches the server CUDA version. Inpainting acceleration is primarily determined by the installed PyTorch build and runtime hardware detection.

Start the service:

```bash
cp .env.example .env
./run_web.sh
```

Default endpoints:

- Web workspace: `http://127.0.0.1:8000/`
- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`
- Health check: `http://127.0.0.1:8000/api/health`

With `WEB_HOST=0.0.0.0`, other LAN devices can use `http://<server-ip>:8000/`. Configure an API key, reverse proxy, and network access controls before exposing the service publicly.

## Choosing a Mode

| Mode | Best for | Region | Notes |
| --- | --- | --- | --- |
| `sttn-auto` | Continuous subtitles and moving backgrounds | Optional | Default temporal inpainting mode |
| `sttn-det` | Intermittent subtitles or moving text watermarks | Optional | Uses OCR to select frames for processing |
| `lama` | Animation, static backgrounds, frame-wise repair | Optional | Spatial inpainting with weaker temporal consistency |
| `propainter` | Motion-heavy footage where quality is preferred | Optional | Higher VRAM use and slower processing |
| `opencv` | Simple backgrounds and quick previews | Optional | Fast traditional algorithm with limited complex-scene quality |
| `logo-lama` | A logo/watermark fixed at the same position | Required | Repairs every frame in the supplied regions without OCR |

Region coordinates use `[ymin, ymax, xmin, xmax]` and are submitted as JSON:

```json
[[30, 120, 40, 260], [620, 700, 80, 1160]]
```

For a fixed watermark, use `logo-lama` with a tight region. For text that appears intermittently or moves, try `sttn-det` with no region for full-frame OCR, or provide a larger activity region to reduce false positives. This version does not include a general non-text logo tracker, so moving graphic watermarks may require an additional detector/tracker and per-frame masks.

## API Example

Create a job with `multipart/form-data`:

```bash
curl -X POST http://127.0.0.1:8000/api/jobs \
  -H 'X-API-Key: your-api-key' \
  -F 'file=@input.mp4' \
  -F 'mode=logo-lama' \
  -F 'subtitle_area_coords=[[30,120,40,260]]' \
  -F 'pad=6'
```

Poll, pause, resume, and download:

```bash
curl -H 'X-API-Key: your-api-key' \
  http://127.0.0.1:8000/api/jobs/<job_id>

curl -X POST -H 'X-API-Key: your-api-key' \
  http://127.0.0.1:8000/api/jobs/<job_id>/pause

curl -X POST -H 'X-API-Key: your-api-key' \
  http://127.0.0.1:8000/api/jobs/<job_id>/resume

curl -L -H 'X-API-Key: your-api-key' \
  -o output.mp4 \
  http://127.0.0.1:8000/api/jobs/<job_id>/download
```

Omit the authentication header when `WEB_API_KEY` is empty. See [the API reference](docs/API.md) for all endpoints, parameters, statuses, and response fields.

## Queue and Pause Semantics

- The service always runs one processing job at a time; other jobs remain queued.
- Pausing a queued job prevents it from starting.
- Pausing a running job freezes its subprocess but does not release loaded models or GPU memory.
- A paused running job does not allow the next queued job to start because the single worker is still waiting for that job to resume or finish.
- Job metadata is held in process memory and is not restored after a service restart.
- Uploaded and generated files are stored under `VSR_DATA_DIR`; automatic disk cleanup is not currently implemented.

Multi-GPU concurrency, preemptive switching, restart recovery, and retention policies require an external persistent queue, independent workers, and object storage.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEB_HOST` | `0.0.0.0` | Listen address |
| `WEB_PORT` | `8000` | Listen port |
| `WEB_API_KEY` | empty | Protect configuration and job endpoints when set |
| `WEB_CORS_ORIGINS` | empty | Comma-separated allowed origins |
| `VSR_PYTHON` | current service interpreter | Explicit Python executable for processing subprocesses |
| `VSR_DATA_DIR` | `./web_data` | Upload and result directory |
| `VSR_FFMPEG_PATH` | auto-detected | Explicit FFmpeg executable path |
| `VSR_MAX_UPLOAD_BYTES` | `2147483648` | Per-file upload limit, 2 GiB by default |
| `VSR_QUEUE_SIZE` | `8` | Waiting queue capacity |
| `VSR_MAX_JOBS` | `100` | Maximum in-memory job records |

See [the deployment guide](docs/DEPLOYMENT.md) for Linux installation, systemd, reverse proxy, GPU diagnostics, and data retention notes.

## Compatibility Entrypoints

The upstream interfaces remain available:

```bash
# CLI
python -m backend.main --input input.mp4 --output output.mp4 --inpaint-mode sttn-auto

# Desktop GUI; requires the Qt dependencies from requirements.txt
python gui.py
```

Refer to the [upstream documentation](https://github.com/YaoFANGUK/video-subtitle-remover) for desktop packages, detailed platform installation, and model training.

## Layout

```text
web_app.py                 FastAPI, job queue, and static Web entry point
web/                       Browser workspace
backend/                   OCR, inpainting algorithms, and models
run_web.sh                 Single-worker service launcher
requirements-web.txt       Web dependencies without Qt
.env.example               Configuration example
web.service.example        Example systemd user service
docs/API.md                API reference
docs/DEPLOYMENT.md         Deployment and operations guide
```

## Upstream and License

This repository is derived from [YaoFANGUK/video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover) and remains licensed under the [Apache License 2.0](LICENSE). Models and third-party components may carry additional licenses or usage restrictions; review them before deployment or redistribution.
