#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐档扫描 focus_absolute, 用清晰度评分找最焦点, 锁定并存档.

比相机自带连续对焦可靠: 拿"实测画面最锐利"当唯一标准.

用法:
  python3 focus_sweep.py                 # 粗扫+细扫, 自动锁定最优值
  python3 focus_sweep.py --coarse-only   # 只粗扫
  python3 focus_sweep.py --keep          # 扫描但不写入 focus.json
"""

import argparse
import subprocess
import sys
import time

import cv2

sys.path.insert(0, "/opt/scanner")
from scanner_daemon import CFG, detect_view, find_device, save_stored_focus, set_ctrl  # noqa: E402


def sharpness(frame):
    """中心区域清晰度 (拉普拉斯方差), 越大越锐利."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi = gray[int(h * 0.15) : int(h * 0.85), int(w * 0.15) : int(w * 0.85)]
    return float(cv2.Laplacian(roi, cv2.CV_64F).var())


def open_cap(dev):
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CFG["fourcc"]))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CFG["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CFG["height"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
    ok, frame = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("无法读取画面, 请检查相机")
    return cap


def set_focus(dev, value):
    rc = subprocess.run(
        ["v4l2-ctl", "-d", dev, "--set-ctrl", f"focus_absolute={value}"],
        capture_output=True,
        text=True,
    )
    return rc.returncode == 0


def measure(cap, dev, value, settle=0.18, samples=2):
    set_focus(dev, value)
    time.sleep(settle)
    best = 0.0
    for i in range(samples):
        ok, frame = cap.read()
        if not ok:
            continue
        if i == samples - 1:
            best = sharpness(frame)
    return best


def sweep(cap, dev, start, stop, step, label):
    results = []
    for value in range(start, stop + 1, step):
        score = measure(cap, dev, value)
        results.append((value, score))
        bar = "#" * max(0, min(40, int(score / 20)))
        print(f"  {label} {value:4d} 清晰度 {score:8.1f} {bar}", flush=True)
    results.sort(key=lambda r: r[1], reverse=True)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coarse-step", type=int, default=32)
    parser.add_argument("--fine-step", type=int, default=8)
    parser.add_argument("--coarse-only", action="store_true")
    parser.add_argument("--keep", action="store_true", help="只扫描, 不写入 focus.json")
    args = parser.parse_args()

    dev = find_device()
    if not dev:
        print("未找到高拍仪")
        return 1
    print(f"设备: {dev}")

    set_ctrl(dev, "focus_automatic_continuous", 0)
    cap = open_cap(dev)
    try:
        ok, frame = cap.read()
        current = None
        rc = subprocess.run(
            ["v4l2-ctl", "-d", dev, "--get-ctrl", "focus_absolute"],
            capture_output=True,
            text=True,
        )
        if rc.returncode == 0 and ":" in rc.stdout:
            current = int(rc.stdout.split(":")[1].strip())
        print(f"当前对焦值 {current}, 基线清晰度 {sharpness(frame):.1f}")

        print("粗扫 (0-1023) ...")
        coarse = sweep(cap, dev, 0, 1023, args.coarse_step, "coarse")
        best_coarse = coarse[0]
        print(f"粗扫最优: focus={best_coarse[0]} 清晰度={best_coarse[1]:.1f}")
        if args.coarse_only:
            best = best_coarse
        else:
            lo = max(0, best_coarse[0] - args.coarse_step)
            hi = min(1023, best_coarse[0] + args.coarse_step)
            print(f"细扫 {lo}-{hi} ...")
            fine = sweep(cap, dev, lo, hi, args.fine_step, "fine")
            best = fine[0]
            print(f"细扫最优: focus={best[0]} 清晰度={best[1]:.1f}")

        set_focus(dev, best[0])
        time.sleep(0.3)
        ok, frame = cap.read()
        verify = sharpness(frame)
        set_ctrl(dev, "focus_automatic_continuous", 0)
        set_focus(dev, best[0])
        print(f"最终: focus_absolute={best[0]} 复测清晰度={verify:.1f}")
        if not args.keep:
            save_stored_focus(best[0], CFG)
        cv2.imwrite("/tmp/focus_best.jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        print("样张: /tmp/focus_best.jpg")
    finally:
        cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
