#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流水线高拍仪自动抓拍守护进程.

工作流程: 放入设备 -> 画面静止 -> 抓拍高清图 -> 拿走设备 -> 空台复位 -> 下一台.

- 启动时做一次自动对焦收敛并锁定 (focus_automatic_continuous=0 + focus_absolute),
  之后不再反复对焦, 避免数秒延迟.
- 主循环只做低分辨率灰度差分 (毫秒级), 高清写盘交给后台写盘线程.
- 带设备节点自检 / 断流自动重连 / 状态文件, 供 systemd 守护.

用法:
  python3 scanner_daemon.py                 # 常驻运行
  python3 scanner_daemon.py --check         # 自检: 设备节点 + 控制项 + 状态文件
  python3 scanner_daemon.py --focus-test    # 对焦校准测试并抓一张样张
  python3 scanner_daemon.py --snapshot out.jpg   # 抓一张样张(不启动状态机)
"""

import argparse
import glob
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np

# ==================== 可调参数 ====================
CFG = {
    # 设备: 优先用 by-id 稳定路径, 避免 /dev/video0(其实是机顶盒硬件解码器)
    "by_id_glob": "/dev/v4l/by-id/*USB_2.0_Camera*video-index0",
    "name_match": "USB 2.0 Camera",
    "name_exclude": ("meson", "decoder"),
    # 采集
    "width": 2592,
    "height": 1944,
    "fourcc": "MJPG",
    # 输出
    "save_dir": "/opt/scanner/data",
    "jpeg_quality": 95,
    "date_subdir": True,
    "min_free_mb": 500,
    # 检测 ROI (相对整幅的比例: y1, x1, y2, x2)
    "roi": (0.12, 0.12, 0.88, 0.88),
    "detect_width": 300,      # 差分用的低分辨率宽度
    "diff_threshold": 30,     # 与空台背景比较的像素差门限
    "item_ratio": 0.005,      # 判定"台上有物品"的最小变化面积占比
    "motion_threshold": 15,   # 帧间差分门限
    "motion_ratio": 0.001,    # 判定"仍在动"的变化面积占比
    "stable_frames": 3,       # 连续静止帧数 -> 抓拍
    "empty_frames": 4,        # 连续空台帧数 -> 复位等待下一台
    "rearm_motion_ratio": 0.02,  # 空台后仍有余动时的额外等待门限
    "min_capture_gap": 0.5,   # 两次抓拍最小间隔(秒)
    # 对焦
    "af_warmup_s": 3.0,       # 启动时自动对焦收敛时间
    "focus_min": 0,
    "focus_max": 1023,
    "focus_store": "/opt/scanner/focus.json",  # 锁定后的对焦值存档, 重启直接复用
    "recalibrate_flag": "/opt/scanner/recalibrate.flag",  # 出现该文件即重新标定空台
    "sharpness_warn": 100,    # 抓拍清晰度低于此值就提醒重标定
    "plate_bright_limit": 5.0,  # 中心区亮像素(>150)占比低于此值 且 清晰度低 = 空台
    "plate_sharp_limit": 300,   # 实测: 空底板清晰度≈124, 深色电路板≈667, 白色光猫≈1400+
    "empty_ncc": 0.90,          # 与空台的结构相关度高于此值 且 平均差很小 => 就是空台（实测 空台0.975 / 光猫0.632 / 手机0.246）
    "empty_mean_abs": 9.0,
    # 自适应与自愈
    "bg_adapt_rate": 0.02,    # 空台时背景缓慢适应(抗灯光/落灰漂移)
    "read_fail_limit": 40,    # 连续读帧失败 -> 重连设备
    "reopen_delay_s": 3.0,
    "status_path": "/opt/scanner/status.json",
    "status_interval_s": 5.0,
    "writer_threads": 2,
    "queue_size": 4,
    "log_fps_interval_s": 60.0,
}
# ==================================================

RUNNING = True
LOCKED_FOCUS = None
STATS_LOCK = threading.Lock()
STATS = {
    "captures": 0,
    "drops": 0,
    "errors": 0,
    "last_capture": None,
    "last_capture_ms": None,
    "last_sharpness": None,
    "state": "BOOT",
    "fps": 0.0,
    "loop_ms": 0.0,
    "device": None,
    "focus": None,
    "started": time.time(),
}


def log(msg):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{stamp}] {msg}", flush=True)


def handle_signal(signum, _frame):
    global RUNNING
    RUNNING = False
    log(f"收到信号 {signum}, 准备退出")


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# -------------------- 设备节点 --------------------
def find_device(cfg=CFG, verbose=True):
    """返回高拍仪采集节点路径; 找不到返回 None."""
    for path in sorted(glob.glob(cfg["by_id_glob"])):
        if os.path.exists(path):
            return path
    # 退化: 按 /sys/class/video4linux 名称匹配
    for namefile in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            name = open(namefile, encoding="utf-8", errors="replace").read().strip()
        except OSError:
            continue
        low = name.lower()
        if any(bad in low for bad in cfg["name_exclude"]):
            continue
        if cfg["name_match"].lower() in low:
            return "/dev/" + os.path.basename(os.path.dirname(namefile))
    if verbose:
        log("未找到高拍仪设备节点 (检查 USB 连接 / lsusb)")
    return None


def v4l2(args, timeout=10):
    try:
        proc = subprocess.run(
            ["v4l2-ctl"] + args, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, "v4l2-ctl not found"
    except subprocess.TimeoutExpired:
        return 124, "v4l2-ctl timeout"


def get_ctrl(dev, name):
    rc, out = v4l2(["-d", dev, "--get-ctrl", name])
    if rc != 0:
        return None
    out = out.strip()
    if ":" in out:
        out = out.split(":", 1)[1].strip()
    try:
        return int(out)
    except ValueError:
        return None


def set_ctrl(dev, name, value):
    rc, out = v4l2(["-d", dev, "--set-ctrl", f"{name}={value}"])
    if rc != 0:
        log(f"设置 {name}={value} 失败: {out.strip()}")
        return False
    return True


# -------------------- 相机 --------------------
def open_capture(dev, cfg=CFG):
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    fourcc = cfg["fourcc"]
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None
    h, w = frame.shape[:2]
    if (w, h) != (cfg["width"], cfg["height"]):
        log(f"注意: 实际分辨率 {w}x{h} != 请求 {cfg['width']}x{cfg['height']}")
    return cap


def init_focus(dev, cfg=CFG, force_calibrate=False):
    """启动时自动对焦收敛并锁定; 重连时复用已锁定值."""
    global LOCKED_FOCUS
    if not force_calibrate:
        value = LOCKED_FOCUS if LOCKED_FOCUS is not None else load_stored_focus(cfg)
        if value is not None:
            set_ctrl(dev, "focus_automatic_continuous", 0)
            set_ctrl(dev, "focus_absolute", value)
            LOCKED_FOCUS = value
            log(f"复用已锁定对焦值: {value} (跳过自动对焦, 零等待)")
            return value

    log("对焦校准: 打开连续自动对焦, 等待收敛 ...")
    if not set_ctrl(dev, "focus_automatic_continuous", 1):
        log("该设备不支持 focus_automatic_continuous, 跳过对焦锁定")
        return None
    time.sleep(cfg["af_warmup_s"])
    value = get_ctrl(dev, "focus_absolute")
    if value is None:
        log("读取 focus_absolute 失败, 跳过对焦锁定")
        return None
    value = max(cfg["focus_min"], min(cfg["focus_max"], value))
    set_ctrl(dev, "focus_automatic_continuous", 0)
    set_ctrl(dev, "focus_absolute", value)
    time.sleep(0.3)
    verify = get_ctrl(dev, "focus_absolute")
    auto = get_ctrl(dev, "focus_automatic_continuous")
    log(f"对焦锁定完成: focus_absolute={verify} (auto={auto})")
    if verify is not None:
        LOCKED_FOCUS = verify
        save_stored_focus(verify, cfg)
        return verify
    return None


def load_stored_focus(cfg=CFG):
    try:
        with open(cfg["focus_store"], encoding="utf-8") as fh:
            return int(json.load(fh)["focus_absolute"])
    except Exception:  # noqa: BLE001
        return None


def save_stored_focus(value, cfg=CFG):
    try:
        os.makedirs(os.path.dirname(cfg["focus_store"]), exist_ok=True)
        prev = load_stored_focus(cfg)
        with open(cfg["focus_store"], "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "focus_absolute": value,
                    "prev": prev,
                    "ts": datetime.now().isoformat(),
                },
                fh,
            )
        log(f"对焦值已存档: {cfg['focus_store']} = {value}")
    except OSError as exc:
        log(f"对焦值存档失败: {exc}")


def calibrate_background(cap, cfg=CFG, frames=15):
    """采集空台背景; 若期间画面变化明显则提示."""
    samples = []
    prev = None
    motion_seen = 0
    for _ in range(frames):
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        gray = detect_view(frame, cfg)
        samples.append(gray)
        if prev is not None:
            if count_diff(prev, gray, cfg["motion_threshold"]) > int(
                gray.size * cfg["motion_ratio"] * 4
            ):
                motion_seen += 1
        prev = gray
        time.sleep(0.05)
    if not samples:
        raise RuntimeError("空台标定失败: 无法读取画面")
    bg = np.median(np.stack(samples), axis=0).astype(np.uint8)
    if motion_seen > frames // 4:
        log("警告: 标定期间画面明显变化, 请确认高拍仪下方为干净空台")
    log(f"空台背景标定完成 (采样 {len(samples)} 帧)")
    return bg


def detect_view(frame, cfg=CFG):
    """ROI + 抽点降采样 + 灰度 + 模糊, 得到低分辨率差分视图.

    直接对 ROI 做步进切片(不整幅 cvtColor), 8MP 帧上约 5ms, 保证主循环毫秒级.
    """
    h, w = frame.shape[:2]
    y1, x1, y2, x2 = cfg["roi"]
    roi = frame[int(h * y1) : int(h * y2), int(w * x1) : int(w * x2)]
    step = max(1, round(min(roi.shape[1], roi.shape[0]) / cfg["detect_width"]))
    small = roi[::step, ::step]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (11, 11), 0)


def count_diff(a, b, threshold):
    return cv2.countNonZero(cv2.threshold(cv2.absdiff(a, b), threshold, 255, cv2.THRESH_BINARY)[1])


def frame_sharpness(frame):
    """全分辨率中心区清晰度 (拉普拉斯方差): 实拍标签约 1000+, 空台只有个位数."""
    h, w = frame.shape[:2]
    roi = frame[int(h * 0.15) : int(h * 0.85), int(w * 0.15) : int(w * 0.85)]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def frame_bright_pct(frame):
    """中心区亮像素占比(%): 空台是深色底板(≈0.1%), 有白色标签的实物 15-25%."""
    h, w = frame.shape[:2]
    roi = frame[int(h * 0.15) : int(h * 0.85), int(w * 0.15) : int(w * 0.85)]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float((gray > 150).mean()) * 100


def frame_is_plate(frame, cfg=CFG):
    """判断当前画面是不是空台.

    两个条件同时满足才算: 亮区占比低(没有白标签/白壳) 且 清晰度低(没有电路板/丝印等细节).
    只用亮区占比会把深色电路板误判成空台(2026-10-07 实测踩过).
    """
    if frame_bright_pct(frame) >= cfg["plate_bright_limit"]:
        return False
    return frame_sharpness(frame) < cfg["plate_sharp_limit"]


def is_empty_like(frame, bg, cfg=CFG):
    """判断画面是不是"就是空台"（即使自动曝光漂移导致整体亮度不同）.

    做法：两边各自减去平均亮度后算归一化互相关（NCC）。
    纯曝光/光线变化只会让亮度整体平移，NCC 依旧很高；
    真放了东西结构就变了，NCC 会明显下降。
    """
    g = detect_view(frame, cfg)
    if g.shape != bg.shape:
        return False
    a = g.astype("float32")
    b = bg.astype("float32")
    a -= float(a.mean())
    b -= float(b.mean())
    denom = float(np.sqrt((a * a).sum()) * np.sqrt((b * b).sum())) + 1e-6
    ncc = float((a * b).sum()) / denom
    mean_abs = float(np.abs(a - b).mean())
    return ncc > cfg["empty_ncc"] and mean_abs < cfg["empty_mean_abs"]


# -------------------- 写盘线程 --------------------
def writer_worker(cfg=CFG):
    while True:
        task = save_queue.get()
        if task is None:
            save_queue.task_done()
            return
        frame, path = task
        t0 = time.time()
        try:
            if free_mb(path) < cfg["min_free_mb"]:
                raise RuntimeError("剩余空间不足, 跳过写盘")
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, cfg["jpeg_quality"]])
            if not ok:
                raise RuntimeError("JPEG 编码失败")
            with open(path + ".tmp", "wb") as fh:
                fh.write(buf.tobytes())
            os.replace(path + ".tmp", path)
            sharp = frame_sharpness(frame)
            cost = (time.time() - t0) * 1000
            with STATS_LOCK:
                STATS["captures"] += 1
                STATS["last_capture"] = os.path.basename(path)
                STATS["last_capture_ms"] = round(cost, 1)
                STATS["last_sharpness"] = round(sharp, 1)
            log(
                f"抓拍完成: {path} ({os.path.getsize(path) // 1024}KB, "
                f"写盘 {cost:.0f}ms, 清晰度 {sharp:.0f})"
            )
            if sharp < cfg["sharpness_warn"]:
                log(
                    f"提醒: 清晰度 {sharp:.0f} 偏低(疑似失焦), "
                    "把典型设备放底板上执行 scannerctl focus 重新标定"
                )
        except Exception as exc:  # noqa: BLE001
            with STATS_LOCK:
                STATS["errors"] += 1
            log(f"写盘异常: {exc}")
        finally:
            save_queue.task_done()


def free_mb(path):
    try:
        st = os.statvfs(os.path.dirname(path) or "/")
        return st.f_bavail * st.f_frsize / (1024 * 1024)
    except OSError:
        return 999999.0


def build_path(cfg=CFG):
    now = datetime.now()
    base = cfg["save_dir"]
    if cfg["date_subdir"]:
        base = os.path.join(base, now.strftime("%Y%m%d"))
    os.makedirs(base, exist_ok=True)
    name = f"SN_{now.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    return os.path.join(base, name)


def enqueue_capture(frame, cfg=CFG):
    path = build_path(cfg)
    try:
        save_queue.put_nowait((frame, path))
    except queue.Full:
        with STATS_LOCK:
            STATS["drops"] += 1
        log("写盘队列已满, 丢弃本次抓拍 (磁盘/编码跟不上)")
        return None
    return path


save_queue = queue.Queue(maxsize=CFG["queue_size"])


# -------------------- 状态文件 --------------------
def status_writer():
    path = CFG["status_path"]
    while RUNNING:
        try:
            with STATS_LOCK:
                data = dict(STATS)
            data["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data["uptime_s"] = round(time.time() - STATS["started"], 1)
            data["queue"] = save_queue.qsize()
            tmp = path + ".tmp"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            log(f"状态文件写入异常: {exc}")
        for _ in range(int(CFG["status_interval_s"] * 10)):
            if not RUNNING:
                return
            time.sleep(0.1)


# -------------------- 主循环 --------------------
def run_loop(cfg=CFG):
    global LOCKED_FOCUS
    cv2.setNumThreads(2)
    for _ in range(cfg["writer_threads"]):
        threading.Thread(target=writer_worker, daemon=True).start()
    threading.Thread(target=status_writer, daemon=True).start()

    state = "EMPTY"
    stable = 0
    empty_run = 0
    failed = 0
    last_capture = 0.0
    frames = 0
    fps_t0 = time.time()
    cap = None
    dev = None
    mode = "RUN"
    hint_at = 0.0
    last_sharp_check = 0.0

    while RUNNING:
        if cap is None:
            dev = find_device(cfg)
            if dev is None:
                with STATS_LOCK:
                    STATS["state"] = "NO_DEVICE"
                time.sleep(cfg["reopen_delay_s"])
                continue
            cap = open_capture(dev, cfg)
            if cap is None:
                log(f"打开设备失败: {dev}, {cfg['reopen_delay_s']}s 后重试")
                with STATS_LOCK:
                    STATS["state"] = "OPEN_FAILED"
                time.sleep(cfg["reopen_delay_s"])
                continue
            with STATS_LOCK:
                STATS["device"] = dev
            log(f"已打开设备: {dev}")
            focus = init_focus(dev, cfg)
            with STATS_LOCK:
                STATS["focus"] = focus
            try:
                bg = calibrate_background(cap, cfg)
            except Exception as exc:  # noqa: BLE001
                log(f"{exc}")
                cap.release()
                cap = None
                time.sleep(cfg["reopen_delay_s"])
                continue
            prev = None
            state, stable, empty_run, failed = "EMPTY", 0, 0, 0
            ok, probe = cap.read()
            probe_bright = frame_bright_pct(probe) if ok else None
            if probe_bright is not None and probe_bright < cfg["plate_bright_limit"]:
                mode = "RUN"
                log("流水线监控就绪: 空台=等待, 放入物品静止即抓拍")
            else:
                mode = "WAIT_PLATE"
                log(
                    f"注意: 启动画面亮区占比 {probe_bright:.1f}%, 台面上有东西; "
                    "请清空台面, 清空后会自动完成空台标定并开始工作"
                )

        t0 = time.time()
        ok, frame = cap.read()
        if not ok or frame is None:
            failed += 1
            if failed >= cfg["read_fail_limit"]:
                log(f"连续 {failed} 帧读取失败, 重连设备")
                with STATS_LOCK:
                    STATS["errors"] += 1
                cap.release()
                cap = None
            time.sleep(0.05)
            continue
        failed = 0

        if os.path.exists(cfg["recalibrate_flag"]):
            log("收到重新标定空台请求, 重新采集背景 ...")
            try:
                bg = calibrate_background(cap, cfg)
                os.remove(cfg["recalibrate_flag"])
                state, stable, empty_run = "EMPTY", 0, 0
                prev = None
                log("空台已重新标定, 状态复位")
            except Exception as exc:  # noqa: BLE001
                log(f"重新标定失败: {exc}")
            continue

        gray = detect_view(frame, cfg)
        if prev is None:
            prev = gray
            continue
        motion = count_diff(prev, gray, cfg["motion_threshold"])
        prev = gray

        diff_px = count_diff(bg, gray, cfg["diff_threshold"])
        has_item = diff_px > int(gray.size * cfg["item_ratio"])
        moving = motion > int(gray.size * cfg["motion_ratio"])

        now = time.time()
        if mode == "WAIT_PLATE":
            # 启动时台面非空: 等出现"低纹理 + 静止"的画面, 即认为台面已清空
            if not moving and has_item:
                bright = frame_bright_pct(frame)
                if bright < cfg["plate_bright_limit"]:
                    log(f"识别到空台(亮区占比 {bright:.1f}%), 采用为背景, 开始工作")
                    bg = calibrate_background(cap, cfg)
                    prev = None
                    state, stable, empty_run = "EMPTY", 0, 0
                    mode = "RUN"
                    continue
                if now - hint_at > 30:
                    hint_at = now
                    log(f"台面上仍有物品(亮区占比 {bright:.1f}%), 请清空台面完成空台标定")
            with STATS_LOCK:
                STATS["state"] = "WAIT_EMPTY"
            continue

        if state == "EMPTY":
            if has_item:
                state = "SETTLING"
                stable = 0
                log(f"检测到放入物品 (变化像素 {diff_px}), 等待静止 ...")
            elif not moving:
                # 空台缓慢适应环境光/落灰
                cv2.addWeighted(bg, 1 - cfg["bg_adapt_rate"], gray, cfg["bg_adapt_rate"], 0, dst=bg)
        elif state == "SETTLING":
            if not has_item:
                empty_run += 1
                if empty_run >= cfg["empty_frames"]:
                    state = "EMPTY"
                    empty_run = 0
                    stable = 0
                    log("物品已拿走, 复位等待下一台")
            else:
                empty_run = 0
                if not moving:
                    stable += 1
                    if stable >= cfg["stable_frames"] and now - last_capture >= cfg["min_capture_gap"]:
                        if is_empty_like(frame, bg, cfg):
                            # 空台被自动曝光漂移"伪装"成物品时不再拍下来（用户要求：空台不必留图）
                            log("画面与空台一致（曝光漂移误触发），跳过保存")
                        else:
                            path = enqueue_capture(frame, cfg)
                            if path:
                                last_capture = now
                                log(f"画面静止 {stable} 帧, 已投递抓拍: {os.path.basename(path)}")
                        state = "CAPTURED"
                        stable = 0
                else:
                    stable = 0
        elif state == "CAPTURED":
            if not has_item:
                empty_run += 1
                if empty_run >= cfg["empty_frames"]:
                    state = "EMPTY"
                    empty_run = 0
                    log("空台复位完成, 可以放入下一台")
            else:
                empty_run = 0
                if not moving and now - last_sharp_check > 2.0:
                    last_sharp_check = now
                    bright = frame_bright_pct(frame)
                    if bright < cfg["plate_bright_limit"]:
                        log(
                            f"当前是空台画面(亮区占比 {bright:.1f}%)但背景记录不符, "
                            "自动重标定背景并复位"
                        )
                        bg = calibrate_background(cap, cfg)
                        prev = None
                        state, stable, empty_run = "EMPTY", 0, 0
                        continue

        with STATS_LOCK:
            STATS["state"] = state
        frames += 1
        dt = time.time() - t0
        with STATS_LOCK:
            STATS["loop_ms"] = round(dt * 1000, 1)
        if time.time() - fps_t0 >= cfg["log_fps_interval_s"]:
            fps = frames / (time.time() - fps_t0)
            with STATS_LOCK:
                STATS["fps"] = round(fps, 2)
            log(f"运行中: {fps:.1f} fps, 状态 {state}, 已抓拍 {STATS['captures']} 张")
            frames = 0
            fps_t0 = time.time()

    if cap is not None:
        cap.release()
    save_queue.put(None)
    log("主循环已退出")


# -------------------- 自检 / 测试模式 --------------------
def do_check():
    dev = find_device(CFG)
    print(f"设备节点: {dev or '未找到'}")
    if not dev:
        return 1
    rc, out = v4l2(["-d", dev, "--list-ctrls"])
    keep = [ln for ln in out.splitlines() if any(k in ln for k in ("focus", "exposure", "white_balance"))]
    print("关键控制项:")
    print("\n".join(keep) if keep else "  (无)")
    rc, out = v4l2(["-d", dev, "--get-fmt-video"])
    for ln in out.splitlines():
        if "Width/Height" in ln or "Pixel Format" in ln:
            print("当前格式:", ln.strip())
    if os.path.exists(CFG["status_path"]):
        with open(CFG["status_path"], encoding="utf-8") as fh:
            print("状态文件:", fh.read())
    else:
        print("状态文件: 不存在 (服务未运行过)")
    return 0


def do_snapshot(path):
    dev = find_device(CFG)
    if not dev:
        return 1
    cap = open_capture(dev, CFG)
    if cap is None:
        print("打开设备失败")
        return 1
    try:
        # 新开的视频流会让镜头回到默认位, 必须重新套用锁定对焦值
        init_focus(dev, CFG)
        frame = None
        time.sleep(0.6)  # 让自动曝光/增益先收敛, 否则前几帧偏黑偏亮
        for _ in range(8):
            ok, frame = cap.read()
        if frame is None:
            print("读帧失败")
            return 1
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, CFG["jpeg_quality"]])
        sharp = frame_sharpness(frame)
        print(f"已保存 {path} {frame.shape[1]}x{frame.shape[0]} 清晰度={sharp:.1f}")
    finally:
        cap.release()
    return 0


def do_focus_test():
    dev = find_device(CFG)
    if not dev:
        return 1
    cap = open_capture(dev, CFG)
    if cap is None:
        print("打开设备失败")
        return 1
    try:
        init_focus(dev, CFG, force_calibrate=True)
        time.sleep(0.5)
        frame = None
        for _ in range(3):
            ok, frame = cap.read()
        if frame is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            print(f"对焦测试清晰度={cv2.Laplacian(gray, cv2.CV_64F).var():.1f}")
        rc, out = v4l2(["-d", dev, "--list-ctrls"])
        keep = [ln for ln in out.splitlines() if "focus" in ln]
        print("\n".join(keep))
    finally:
        cap.release()
    return 0


def do_self_test(cfg=CFG):
    """不依赖物理动作的自检: 生成两张测试图, 走完整异步写盘链路并校验结果."""
    global RUNNING
    import threading as _threading

    threads = [
        _threading.Thread(target=writer_worker, daemon=True) for _ in range(cfg["writer_threads"])
    ]
    for t in threads:
        t.start()
    paths = []
    for i in range(2):
        frame = np.full((cfg["height"], cfg["width"], 3), 40, dtype=np.uint8)
        cv2.rectangle(
            frame,
            (cfg["width"] // 4, cfg["height"] // 4),
            (cfg["width"] * 3 // 4, cfg["height"] * 3 // 4),
            (235, 235, 235),
            -1,
        )
        cv2.putText(
            frame,
            f"SELFTEST-{i+1}",
            (cfg["width"] // 3, cfg["height"] // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            4,
            (10, 10, 10),
            8,
        )
        path = build_path(cfg)
        save_queue.put((frame, path))
        paths.append(path)
    save_queue.join()
    ok = True
    for path in paths:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        size_kb = os.path.getsize(path) // 1024 if os.path.exists(path) else 0
        good = img is not None and img.shape[1] == cfg["width"]
        print(f"自检写盘 {'OK ' if good else 'FAIL'} {path} {size_kb}KB")
        ok = ok and good
        if good:
            os.remove(path)
    RUNNING = False
    print("自检结果:", "通过" if ok else "失败")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="流水线高拍仪自动抓拍守护进程")
    parser.add_argument("--check", action="store_true", help="自检设备节点与控制项")
    parser.add_argument("--snapshot", metavar="PATH", help="抓一张样张后退出")
    parser.add_argument("--focus-test", action="store_true", help="对焦校准测试")
    parser.add_argument("--self-test", action="store_true", help="异步写盘链路自检(不动物品)")
    args = parser.parse_args()
    os.makedirs(CFG["save_dir"], exist_ok=True)
    if args.check:
        return do_check()
    if args.snapshot:
        return do_snapshot(args.snapshot)
    if args.focus_test:
        return do_focus_test()
    if args.self_test:
        return do_self_test(CFG)
    run_loop(CFG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
