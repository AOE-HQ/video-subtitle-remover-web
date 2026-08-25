"""Small HTTP service for the video subtitle/watermark remover.

The web process owns only the queue and API state.  Each actual removal runs
in a child Python process so a failed model load cannot take down the API and
GPU-backed models are never used concurrently.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles


ROOT_DIR = Path(__file__).resolve().parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = Path(os.getenv("VSR_DATA_DIR", str(ROOT_DIR / "web_data"))).expanduser().resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
MAX_UPLOAD_BYTES = int(os.getenv("VSR_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024)))
MAX_LOG_LINES = 240
MAX_JOBS = int(os.getenv("VSR_MAX_JOBS", "100"))
QUEUE_SIZE = int(os.getenv("VSR_QUEUE_SIZE", "8"))
API_KEY = os.getenv("WEB_API_KEY", "").strip()
PYTHON_EXECUTABLE = os.getenv("VSR_PYTHON", sys.executable)

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".m2v", ".ts", ".mxf", ".3gp", ".ogv", ".rmvb",
}

MODE_INFO = [
    {
        "id": "sttn-auto",
        "label": "STTN Auto",
        "description": "固定区域时序修复，适合连续字幕和动态背景。",
        "requires_area": False,
    },
    {
        "id": "sttn-det",
        "label": "STTN Detect",
        "description": "结合 OCR 检测字幕帧，适合字幕间歇出现的视频。",
        "requires_area": False,
    },
    {
        "id": "lama",
        "label": "LAMA",
        "description": "逐帧空间修复，动画和静态背景通常更自然。",
        "requires_area": False,
    },
    {
        "id": "propainter",
        "label": "ProPainter",
        "description": "高质量时序修复，显存占用较高。",
        "requires_area": False,
    },
    {
        "id": "opencv",
        "label": "OpenCV",
        "description": "快速传统算法，适合简单、规则的字幕区域。",
        "requires_area": False,
    },
    {
        "id": "logo-lama",
        "label": "固定水印 LAMA",
        "description": "对全程固定位置的 Logo/水印逐帧填补。",
        "requires_area": True,
    },
]
MODE_IDS = {item["id"] for item in MODE_INFO}

_jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_jobs_lock = threading.RLock()
_job_queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_SIZE)
_worker_started = False
_worker_lock = threading.Lock()
_processes: dict[str, subprocess.Popen[str]] = {}
_processes_lock = threading.RLock()

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": job["id"],
        "status": job["status"],
        "mode": job["mode"],
        "mode_label": job["mode_label"],
        "filename": job["filename"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "progress": job["progress"],
        "message": job["message"],
        "logs": list(job["logs"]),
    }
    if job["status"] == "succeeded":
        result["download_url"] = f"/api/jobs/{job['id']}/download"
    if job.get("error"):
        result["error"] = job["error"]
    result["can_pause"] = job["status"] in {"queued", "processing"}
    result["can_resume"] = job["status"] == "paused"
    return result


def _update_job(job_id: str, **updates: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.update(updates)
        job["updated_at"] = _now()


def _append_log(job_id: str, message: str) -> None:
    clean = str(message).replace("\x1b[2K", "").strip()
    if not clean:
        return
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["logs"].append(clean)
        if len(job["logs"]) > MAX_LOG_LINES:
            del job["logs"][:-MAX_LOG_LINES]
        job["message"] = clean
        job["updated_at"] = _now()


def _parse_coords(raw: str | None) -> list[list[int]]:
    if raw is None or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="subtitle_area_coords 必须是 JSON 数组") from exc
    if isinstance(value, list) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        value = [value]
    if not isinstance(value, list) or not value:
        raise HTTPException(status_code=422, detail="subtitle_area_coords 至少需要一个区域")
    coords: list[list[int]] = []
    for area in value:
        if not isinstance(area, list) or len(area) != 4:
            raise HTTPException(status_code=422, detail="每个区域必须是 [ymin, ymax, xmin, xmax]")
        if not all(isinstance(item, (int, float)) and float(item).is_integer() for item in area):
            raise HTTPException(status_code=422, detail="区域坐标必须是整数")
        ymin, ymax, xmin, xmax = [int(item) for item in area]
        if min(ymin, ymax, xmin, xmax) < 0 or ymin >= ymax or xmin >= xmax:
            raise HTTPException(status_code=422, detail="区域必须满足 0 <= ymin < ymax、0 <= xmin < xmax")
        if max(ymin, ymax, xmin, xmax) > 10000:
            raise HTTPException(status_code=422, detail="区域坐标不能超过 10000")
        coords.append([ymin, ymax, xmin, xmax])
    return coords


def _check_api_key(value: str | None) -> None:
    if API_KEY and (not value or not hmac.compare_digest(value, API_KEY)):
        raise HTTPException(status_code=401, detail="缺少或无效的 API key")


async def require_api_key(
    request: Request,
    header_key: str | None = Depends(api_key_header),
) -> None:
    supplied = header_key
    if not supplied:
        authorization = request.headers.get("Authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    _check_api_key(supplied)


def _ffmpeg_available() -> bool:
    configured = os.getenv("VSR_FFMPEG_PATH")
    if configured:
        return Path(configured).is_file()
    return bool(shutil.which("ffmpeg") or (ROOT_DIR / "backend/ffmpeg/linux_x64/ffmpeg").is_file())


def _signal_running_job(job_id: str, process_signal: int) -> bool:
    if os.name != "posix":
        raise HTTPException(status_code=501, detail="当前系统不支持暂停运行中的处理进程")
    with _processes_lock:
        process = _processes.get(job_id)
        if process is None or process.poll() is not None:
            return False
        try:
            os.killpg(process.pid, process_signal)
        except ProcessLookupError:
            return False
    return True


def _run_job(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None or job["status"] != "queued":
            return
        job["status"] = "processing"
        job["paused_from"] = None
        job["updated_at"] = _now()
        input_path = job["input_path"]
        output_path = job["output_path"]
        mode = job["mode"]
        coords = job["coords"]
        pad = job["pad"]
        detect_mode = job["detect_mode"]

    command = [PYTHON_EXECUTABLE]
    if mode == "logo-lama":
        command += ["-m", "backend.logo_lama_fill", "-i", input_path, "-o", output_path, "--pad", str(pad)]
        for area in coords:
            command += ["-c", *[str(value) for value in area]]
    else:
        command += [
            "-m", "backend.main", "--input", input_path, "--output", output_path,
            "--inpaint-mode", mode, "--subtitle-detect-mode", detect_mode,
        ]
        for area in coords:
            command += ["--subtitle-area-coords", *[str(value) for value in area]]

    _append_log(job_id, f"启动处理：{' '.join(command[:5])} …")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("VSR_WEB_JOB_ID", job_id)
    percent_pattern = re.compile(r"(?<!\d)(\d{1,3})%")
    frame_pattern = re.compile(r"frames?\s*[=:]\s*(\d+)", re.IGNORECASE)
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=os.name == "posix",
        )
        with _processes_lock:
            _processes[job_id] = process
        assert process.stdout is not None
        for line in process.stdout:
            _append_log(job_id, line)
            match = percent_pattern.search(line)
            if match:
                _update_job(job_id, progress=min(99, max(0, int(match.group(1)))))
            frame_match = frame_pattern.search(line)
            if frame_match:
                _update_job(job_id, processed_frames=int(frame_match.group(1)))
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"处理进程退出码 {return_code}")
        if not Path(output_path).is_file() or Path(output_path).stat().st_size == 0:
            raise RuntimeError("处理结束但没有生成有效输出文件")
        _update_job(job_id, status="succeeded", progress=100, message="处理完成", paused_from=None)
        _append_log(job_id, "处理完成，可以下载结果。")
    except Exception as exc:
        if process is not None and process.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
        _update_job(job_id, status="failed", progress=0, error=str(exc), message="处理失败", paused_from=None)
        _append_log(job_id, f"错误：{exc}")
    finally:
        if process is not None:
            with _processes_lock:
                if _processes.get(job_id) is process:
                    _processes.pop(job_id, None)


def _worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        try:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is not None:
                    job["enqueued"] = False
                status = job.get("status") if job is not None else None
            if status != "queued":
                continue
            _run_job(job_id)
        finally:
            _job_queue.task_done()


def _start_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        thread = threading.Thread(target=_worker_loop, name="vsr-web-worker", daemon=True)
        thread.start()
        _worker_started = True


app = FastAPI(
    title="Video Subtitle Remover API",
    version="1.0.0",
    description="通过现有视频去字幕/固定水印算法创建异步处理任务。",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

origins = [origin.strip() for origin in os.getenv("WEB_CORS_ORIGINS", "").split(",") if origin.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


@app.on_event("startup")
def startup_event() -> None:
    _start_worker()


@app.get("/api/health", tags=["system"])
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "video-subtitle-remover",
        "worker": _worker_started,
        "queue_size": _job_queue.qsize(),
        "ffmpeg_available": _ffmpeg_available(),
    }


@app.get("/api/config", dependencies=[Depends(require_api_key)], tags=["system"])
def config_info() -> dict[str, Any]:
    return {
        "modes": MODE_INFO,
        "coordinate_order": "[ymin, ymax, xmin, xmax]",
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "queue_size": QUEUE_SIZE,
        "api_key_required": bool(API_KEY),
        "ffmpeg_available": _ffmpeg_available(),
    }


@app.get("/api/jobs", dependencies=[Depends(require_api_key)], tags=["jobs"])
def list_jobs(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    with _jobs_lock:
        items = list(_jobs.values())[-limit:]
        items.reverse()
        return {"items": [_public_job(item) for item in items], "total": len(_jobs)}


@app.post("/api/jobs", status_code=202, dependencies=[Depends(require_api_key)], tags=["jobs"])
async def create_job(
    file: UploadFile = File(...),
    mode: str = Form("sttn-auto"),
    subtitle_area_coords: str | None = Form(None),
    pad: int = Form(6),
    detect_mode: str = Form("PP_OCRv5_SERVER"),
) -> dict[str, Any]:
    if mode not in MODE_IDS:
        raise HTTPException(status_code=422, detail=f"不支持的处理模式：{mode}")
    if pad < 0 or pad > 512:
        raise HTTPException(status_code=422, detail="pad 必须在 0 到 512 之间")
    if detect_mode not in {"PP_OCRv5_SERVER", "PP_OCRv5_MOBILE"}:
        raise HTTPException(status_code=422, detail="不支持的字幕检测模式")
    coords = _parse_coords(subtitle_area_coords)
    if mode == "logo-lama" and not coords:
        raise HTTPException(status_code=422, detail="固定水印模式必须填写至少一个区域")

    filename = Path(file.filename or "input.mp4").name
    extension = Path(filename).suffix.lower()
    if extension not in VIDEO_EXTENSIONS:
        raise HTTPException(status_code=415, detail="仅支持常见视频格式（mp4、mov、mkv、webm 等）")
    with _jobs_lock:
        active_count = sum(item["status"] in {"queued", "processing", "paused"} for item in _jobs.values())
        if active_count >= QUEUE_SIZE + 1:
            raise HTTPException(status_code=429, detail="处理队列已满，请稍后重试")
        if len(_jobs) >= MAX_JOBS:
            removable_ids = [
                old_id for old_id, old_job in _jobs.items()
                if old_job["status"] not in {"queued", "processing", "paused"}
            ]
            while len(_jobs) >= MAX_JOBS and removable_ids:
                _jobs.pop(removable_ids.pop(0), None)
            if len(_jobs) >= MAX_JOBS:
                raise HTTPException(status_code=429, detail="任务记录已满，请稍后重试")

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}{extension}"
    output_path = OUTPUT_DIR / f"{job_id}_no_watermark.mp4"
    received = 0
    try:
        with input_path.open("wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="上传文件超过大小限制")
                destination.write(chunk)
    except HTTPException:
        input_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        input_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"无法保存上传文件：{exc}") from exc
    finally:
        await file.close()

    mode_label = next(item["label"] for item in MODE_INFO if item["id"] == mode)
    job = {
        "id": job_id,
        "status": "queued",
        "mode": mode,
        "mode_label": mode_label,
        "filename": filename,
        "created_at": _now(),
        "updated_at": _now(),
        "progress": 0,
        "message": "已进入处理队列",
        "logs": ["文件上传完成，等待处理。"],
        "error": None,
        "input_path": str(input_path),
        "output_path": str(output_path),
        "coords": coords,
        "pad": pad,
        "detect_mode": detect_mode,
        "paused_from": None,
        "enqueued": True,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    try:
        _job_queue.put_nowait(job_id)
    except queue.Full:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        input_path.unlink(missing_ok=True)
        raise HTTPException(status_code=429, detail="处理队列已满，请稍后重试")
    return _public_job(job)


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_api_key)], tags=["jobs"])
def get_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _public_job(job)


@app.post("/api/jobs/{job_id}/pause", dependencies=[Depends(require_api_key)], tags=["jobs"])
def pause_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        status = job["status"]
        if status == "paused":
            return _public_job(job)
        if status not in {"queued", "processing"}:
            raise HTTPException(status_code=409, detail="当前任务状态不能暂停")
        if status == "processing" and not _signal_running_job(job_id, signal.SIGSTOP):
            raise HTTPException(status_code=409, detail="处理进程已经结束，请刷新任务状态")
        job["status"] = "paused"
        job["paused_from"] = status
        job["updated_at"] = _now()

    message = "处理进程已暂停，GPU 显存会保持占用。" if status == "processing" else "排队任务已暂停。"
    _append_log(job_id, message)
    _update_job(job_id, message=message)
    with _jobs_lock:
        return _public_job(_jobs[job_id])


@app.post("/api/jobs/{job_id}/resume", dependencies=[Depends(require_api_key)], tags=["jobs"])
def resume_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job["status"] != "paused":
            raise HTTPException(status_code=409, detail="任务没有处于暂停状态")
        paused_from = job.get("paused_from")
        if paused_from == "processing":
            if not _signal_running_job(job_id, signal.SIGCONT):
                raise HTTPException(status_code=409, detail="处理进程已经结束，请刷新任务状态")
            next_status = "processing"
        elif paused_from == "queued":
            next_status = "queued"
            if not job.get("enqueued", False):
                try:
                    _job_queue.put_nowait(job_id)
                except queue.Full as exc:
                    raise HTTPException(status_code=429, detail="处理队列已满，请稍后重试") from exc
                job["enqueued"] = True
        else:
            raise HTTPException(status_code=409, detail="任务缺少可恢复的暂停状态")
        job["status"] = next_status
        job["paused_from"] = None
        job["updated_at"] = _now()

    message = "任务已继续处理。"
    _append_log(job_id, message)
    _update_job(job_id, message=message)
    with _jobs_lock:
        return _public_job(_jobs[job_id])


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_api_key)], tags=["jobs"])
def download_job(job_id: str) -> FileResponse:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="任务尚未成功完成")
        output_path = Path(job["output_path"])
        filename = f"{Path(job['filename']).stem}_no_watermark.mp4"
    if not output_path.is_file():
        raise HTTPException(status_code=410, detail="结果文件已被清理")
    return FileResponse(output_path, media_type="video/mp4", filename=filename)


if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_app:app",
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8000")),
        reload=False,
    )
