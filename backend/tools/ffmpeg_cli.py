import os
import stat
import shutil

import platform
from .common_tools import merge_big_file_if_not_exists
from backend.config import BASE_DIR

class FFmpegCLI:
    
    """
    进程管理器类，用于管理子进程的生命周期
    使用弱引用避免内存泄漏
    """
    _instance = None
    
    @classmethod
    def instance(cls):
        """单例模式获取实例"""
        if cls._instance is None:
            cls._instance = FFmpegCLI()
        return cls._instance
    
    def __init__(self):
        # System ffmpeg is commonly owned by root; the web service must be
        # able to use it without trying to mutate its mode bits.
        try:
            os.chmod(self.ffmpeg_path, stat.S_IRWXU + stat.S_IRWXG + stat.S_IRWXO)
        except OSError:
            pass
        
    @property
    def ffmpeg_path(self):
        configured_path = os.environ.get("VSR_FFMPEG_PATH")
        if configured_path:
            if not os.path.isfile(configured_path):
                raise FileNotFoundError(f"VSR_FFMPEG_PATH does not exist: {configured_path}")
            return configured_path
        system = platform.system()
        if system == "Windows":
            ffmpeg_dir = os.path.join(BASE_DIR, 'ffmpeg', 'win_x64')
            merge_big_file_if_not_exists(ffmpeg_dir, 'ffmpeg.exe')
            return os.path.join(ffmpeg_dir, 'ffmpeg.exe')
        elif system == "Linux":
            # The bundled binary is x86_64.  Prefer a system ARM64 binary on
            # aarch64 hosts (for example the deployment target).
            if platform.machine().lower() in {"aarch64", "arm64"}:
                system_ffmpeg = shutil.which("ffmpeg")
                if system_ffmpeg:
                    return system_ffmpeg
            return os.path.join(BASE_DIR, 'ffmpeg',  'linux_x64', 'ffmpeg')
        else:
            system_ffmpeg = shutil.which("ffmpeg")
            return system_ffmpeg or os.path.join(BASE_DIR, 'ffmpeg', 'macos', 'ffmpeg')
