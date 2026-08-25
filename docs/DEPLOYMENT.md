# Web/API 部署指南

推荐将服务部署在 Linux GPU 服务器上，并通过局域网或受控反向代理访问。Web 页面和 API 由同一个 FastAPI 进程提供。

## 运行要求

- Python 3.11 或 3.12
- FFmpeg；仓库包含 Linux x64 版本，也可通过 `VSR_FFMPEG_PATH` 指定系统版本
- 足够的磁盘空间用于原视频、输出视频和模型
- PyTorch；NVIDIA 服务器需安装与驱动/CUDA 兼容的构建
- PaddlePaddle；默认 CPU 版，OCR 需要 GPU 时改装对应的 `paddlepaddle-gpu`

Ubuntu/Debian 基础包示例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev ffmpeg
```

## 安装

```bash
git clone git@github.com:AOE-HQ/video-subtitle-remover-web.git
cd video-subtitle-remover-web

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

先安装 PyTorch。CPU/macOS 可以直接安装：

```bash
pip install torch torchvision
```

NVIDIA 服务器请使用 [PyTorch 官方选择器](https://pytorch.org/get-started/locally/) 安装匹配版本，再安装项目依赖：

```bash
pip install -r requirements-web.txt
```

项目依赖默认包含 `paddlepaddle==3.2.2`。如果 OCR 也需要 CUDA，请卸载 CPU 包，并按 [PaddlePaddle 官方安装说明](https://www.paddlepaddle.org.cn/install/quick) 安装匹配 CUDA 的 `paddlepaddle-gpu`。

## 配置

```bash
cp .env.example .env
```

生产环境至少确认以下值：

```dotenv
WEB_HOST=0.0.0.0
WEB_PORT=8000
WEB_API_KEY=replace-with-a-long-random-secret
VSR_PYTHON=/absolute/path/to/video-subtitle-remover-web/.venv/bin/python
VSR_DATA_DIR=/absolute/path/to/video-subtitle-remover-web/web_data
VSR_MAX_UPLOAD_BYTES=2147483648
VSR_QUEUE_SIZE=8
VSR_MAX_JOBS=100
```

注意：

- `.env` 已被 Git 忽略，不要提交密钥。
- 使用绝对路径可避免 systemd 工作目录变化带来的歧义。
- `WEB_CORS_ORIGINS` 只在前后端跨域部署时需要；同源 Web 页面无需配置。
- 环境变量在服务启动时读取，修改后需要重启。

## 直接启动

```bash
./run_web.sh
```

验证：

```bash
curl http://127.0.0.1:8000/api/health
```

必须保持 `--workers 1`。增加 Uvicorn worker 会产生多个独立内存队列，并可能让多个任务同时争抢一块 GPU。

## systemd 用户服务

仓库中的 `web.service.example` 假定项目位于 `%h/video-subtitle-remover-web`。路径不同时先修改以下三项：

- `WorkingDirectory`
- `EnvironmentFile`
- `ExecStart`

安装用户服务：

```bash
mkdir -p ~/.config/systemd/user
cp web.service.example ~/.config/systemd/user/video-subtitle-remover-web.service
systemctl --user daemon-reload
systemctl --user enable --now video-subtitle-remover-web.service
```

查看状态和日志：

```bash
systemctl --user status video-subtitle-remover-web.service
journalctl --user -u video-subtitle-remover-web.service -f
```

若服务需要在用户退出登录后继续运行，管理员需要为部署用户启用 systemd linger。

## Nginx 反向代理

下面是同机反向代理的基础配置。上传上限要与 `VSR_MAX_UPLOAD_BYTES` 协调：

```nginx
server {
    listen 80;
    server_name _;

    client_max_body_size 2g;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

公网部署还应配置 HTTPS、来源网络限制、速率限制和强 API Key。不要把未加保护的 `0.0.0.0:8000` 直接暴露到互联网。

## GPU 检查

先确认驱动可见：

```bash
nvidia-smi
```

检查 PyTorch：

```bash
python -c 'import torch; print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU")'
```

检查 PaddlePaddle：

```bash
python -c 'import paddle; print(paddle.device.is_compiled_with_cuda()); print(paddle.device.get_device())'
```

处理期间 GPU 使用率为 0 不一定是异常：

- 上传、解封装、编码和部分 OCR 阶段主要使用网络、磁盘或 CPU。
- 模型加载期间显存可能增长，但 GPU 利用率会短暂回落。
- OpenCV 模式主要使用 CPU。
- CPU 版 PaddlePaddle 会让 OCR 阶段使用 CPU，即使 PyTorch 修复模型使用 GPU。
- 暂停运行任务后计算停止，但模型显存不会释放。

应结合任务日志、进程列表、显存占用和一段时间内的 GPU 利用率判断，而不是只看某一秒的瞬时值。

## 数据目录与清理

`VSR_DATA_DIR` 下包含：

```text
uploads/    上传的原视频
outputs/    处理后的 MP4
```

当前版本没有自动清理策略。`VSR_MAX_JOBS` 只限制内存中的任务记录，不限制磁盘文件数量。生产环境应建立独立的保留周期，并确保清理程序跳过仍在 `queued`、`processing` 或 `paused` 状态的任务文件。

服务重启会清空内存任务列表，磁盘上的旧文件不会自动重新关联到 API。

## 更新与回滚

更新前先等待当前任务结束；直接重启会中断处理，且任务状态无法恢复。

```bash
git pull --ff-only
source .venv/bin/activate
pip install -r requirements-web.txt
systemctl --user restart video-subtitle-remover-web.service
```

更新后检查健康接口和 systemd 日志。若需要可靠回滚，部署时应固定 Git 提交，并将模型、Python 环境和 `.env` 一并纳入版本化发布流程。

## 当前架构边界

- 单机、单进程、单处理 worker
- 任务状态不持久化
- 运行中暂停不支持任务抢占或释放显存
- 没有自动重试、取消、删除和磁盘生命周期 API
- 没有多用户权限或配额隔离

需要多 GPU 或生产级任务系统时，建议将 API、持久化队列、GPU worker 和对象存储拆分部署。
