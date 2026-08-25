# -*- coding: utf-8 -*-
"""
logo_lama_fill.py — 固定框 + LAMA 逐帧空间重绘

用途：去除“全程固定位置、每帧都存在”的静态 logo/水印。
VSR 自带的 lama/sttn-det 模式都受 OCR 字幕检测门控，识别不到花体 logo 就不生成 mask；
而 sttn-auto 虽支持固定框，但用时序重绘，logo 每帧都在同位置无干净参考帧，深色背景填不掉。
本脚本直接对固定框区域用 LAMA（单帧空间重绘）逐帧填补，不依赖 OCR、不依赖时序。

用法:
  python -m backend.logo_lama_fill -i in.mp4 -o out.mp4 -c ymin ymax xmin xmax [--pad N]
坐标顺序与 main.py 的 -c 一致: (ymin, ymax, xmin, xmax)
"""
import os
import sys
import time
import argparse
import cv2
import numpy as np
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.main import SubtitleRemover  # 复用其 IO / 音频合并


def build_box_mask(h, w, box, pad):
    ymin, ymax, xmin, xmax = box
    y1 = max(0, ymin - pad)
    y2 = min(h, ymax + pad)
    x1 = max(0, xmin - pad)
    x2 = min(w, xmax + pad)
    mask = np.zeros((h, w), dtype="uint8")
    cv2.rectangle(mask, (x1, y1), (x2, y2), 255, thickness=-1)
    return mask


def _normalise_boxes(coords):
    """Return a list of ``(ymin, ymax, xmin, xmax)`` integer boxes."""
    if coords is None:
        raise ValueError("at least one logo area is required")
    if len(coords) == 4 and all(isinstance(value, (int, np.integer)) for value in coords):
        coords = [coords]
    boxes = []
    for box in coords:
        if len(box) != 4:
            raise ValueError("logo areas must contain four values: ymin, ymax, xmin, xmax")
        boxes.append(tuple(int(value) for value in box))
    return boxes


def run_logo_lama_fill(
    input_path,
    output_path,
    coords,
    pad=6,
    progress_callback=None,
    log_callback=None,
):
    """Fill fixed logo areas in every video frame with LAMA.

    ``progress_callback`` receives ``(processed_frames, total_frames)`` and
    ``log_callback`` receives a string.  The callbacks are intentionally
    optional so this function remains usable from the original CLI.
    """
    input_path = os.path.abspath(os.fspath(input_path))
    output_path = os.path.abspath(os.fspath(output_path))
    if not os.path.isfile(input_path):
        raise FileNotFoundError(input_path)
    if pad < 0 or pad > 512:
        raise ValueError("pad must be between 0 and 512")

    def emit(message):
        if log_callback is not None:
            log_callback(str(message))
        else:
            print(message, flush=True)

    sr = SubtitleRemover(input_path)
    sr.video_out_path = output_path
    boxes = _normalise_boxes(coords)
    h, w = sr.frame_height, sr.frame_width
    if h <= 0 or w <= 0 or sr.frame_count <= 0:
        raise ValueError("unable to read video dimensions or frames")

    mask = np.zeros((h, w), dtype="uint8")
    for box in boxes:
        ymin, ymax, xmin, xmax = box
        if ymin >= ymax or xmin >= xmax:
            raise ValueError("logo area must satisfy ymin < ymax and xmin < xmax")
        mask = np.maximum(mask, build_box_mask(h, w, box, pad))

    emit(
        f"Logo LAMA: frame={w}x{h}, areas={boxes}, pad={pad}, "
        f"masked_px={int((mask > 0).sum())}"
    )
    lama = sr.lama_inpaint
    start = time.time()
    processed = 0
    total = int(sr.frame_count)
    try:
        while True:
            ret, frame = sr.video_cap.read()
            if not ret:
                break
            inpainted = np.asarray(lama.inpaint(frame, mask))
            if inpainted.shape[:2] != (h, w):
                inpainted = cv2.resize(inpainted, (w, h))
            if inpainted.dtype != np.uint8:
                inpainted = np.clip(inpainted, 0, 255).astype(np.uint8)
            sr.video_writer.write(inpainted)
            processed += 1
            if progress_callback is not None:
                progress_callback(processed, total)
        sr.video_cap.release()
        sr.video_writer.release()
        sr.merge_audio_to_video()
    finally:
        try:
            sr.video_cap.release()
        except Exception:
            pass
        try:
            sr.video_writer.release()
        except Exception:
            pass
        if os.path.exists(sr.video_temp_file.name):
            try:
                os.remove(sr.video_temp_file.name)
            except OSError:
                pass

    emit(f"Complete: frames={processed}, saved to {output_path}")
    emit(f"Processing time: {round(time.time() - start)} seconds")
    return output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-c", "--coords", nargs=4, type=int, action="append", required=True,
                        help="ymin ymax xmin xmax (与 main.py -c 相同)")
    parser.add_argument("--pad", type=int, default=6, help="框四周额外扩展像素")
    args = parser.parse_args()

    run_logo_lama_fill(args.input, args.output, args.coords, pad=args.pad)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.set_start_method("spawn")
    main()
