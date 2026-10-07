#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拍照守护进程 -> ESP32 状态灯 桥接服务（旁路，不改拍照主程序）

读 scanner.service 的日志，翻译成状态指令从串口发给 ESP32：
  检测到放入物品            -> BUSY   黄白交替
  抓拍完成 且 清晰度 >= 100 -> OK     绿灯闪（一直闪到设备被拿走）
  抓拍完成 但 清晰度偏低    -> WARN   紫灯闪
  物品拿走 / 空台复位       -> IDLE   红灯常亮
  相机掉线 / 写盘失败       -> ERROR  红白快闪
  启动标定                  -> BOOT   蓝呼吸
  启动时台面非空(等清空)    -> BUSY
串口设备可以晚插：没有 /dev/ttyUSB* 时每 3 秒重试。
"""
import glob
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

SERIAL_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")
KEEPALIVE_S = 10

RULES = [
    (re.compile(r"检测到放入物品"), "BUSY"),
    (re.compile(r"抓拍完成:.*清晰度\s+(\d+)"), "SHOT"),
    (re.compile(r"提醒: 清晰度"), "WARN"),
    (re.compile(r"物品已拿走|空台复位完成|识别到空台|空台已重新标定|自动重标定背景并复位"),
     "IDLE"),
    (re.compile(r"注意: 启动画面亮区占比"), "BUSY"),
    (re.compile(r"连续 \d+ 帧读取失败|未找到高拍仪设备节点|写盘异常|打开设备失败|读取失败"),
     "ERROR"),
    (re.compile(r"流水线监控就绪"), "IDLE"),
    # 区域分拣服务（B1）的事件
    (re.compile(r"区域已切换"), "REGION"),
    (re.compile(r"区域已取消"), "CANCEL"),
    (re.compile(r"区域卡无效"), "ERROR"),
]

current = None
last_sent = 0.0


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def find_serial():
    for pattern in SERIAL_GLOBS:
        for path in sorted(glob.glob(pattern)):
            return path
    return None


class Led:
    def __init__(self):
        self.dev = None
        self.fh = None

    def ensure(self):
        if self.fh is not None:
            return True
        dev = find_serial()
        if dev is None:
            return False
        try:
            subprocess.run(
                ["stty", "-F", dev, "115200", "cs8", "-cstopb", "-parenb", "-crtscts",
                 "raw", "-echo"],
                check=True, capture_output=True, timeout=5,
            )
            self.fh = open(dev, "wb", buffering=0)
            self.dev = dev
            log(f"串口已打开: {dev} @115200")
            global current
            current = None  # 重新连上后强制重发一次当前状态
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"串口打开失败({dev}): {exc}")
            self.fh = None
            return False

    def send(self, state, force=False):
        global current, last_sent
        if not self.ensure():
            return
        if not force and state == current:
            return
        try:
            self.fh.write((state + "\n").encode())
            current = state
            last_sent = time.time()
            log(f"-> {state}")
        except Exception as exc:  # noqa: BLE001
            log(f"串口写失败: {exc}, 断开重连")
            try:
                self.fh.close()
            except Exception:  # noqa: BLE001
                pass
            self.fh = None


def handle(line, led):
    for pattern, action in RULES:
        m = pattern.search(line)
        if not m:
            continue
        if action == "SHOT":
            sharp = int(m.group(1))
            led.send("OK" if sharp >= 100 else "WARN")
            log(f"（清晰度 {sharp}）")
        else:
            led.send(action)
        return


def journal_stream():
    return subprocess.Popen(
        # 同时跟拍照服务和分拣服务（区域卡事件在 sorter 里）
        ["journalctl", "-u", "scanner.service", "-u", "sorter.service",
         "-f", "-n", "0", "-o", "cat"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )


def journal_thread(led):
    """后台线程: 持续跟 journal, 解析到关键事件就发状态."""
    while True:
        proc = journal_stream()
        try:
            for line in proc.stdout:
                handle(line.strip(), led)
        except Exception as exc:  # noqa: BLE001
            log(f"journal 读取异常: {exc}")
        finally:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        log("journalctl 退出, 2 秒后重连")
        time.sleep(2)


def main():
    global current
    led = Led()
    log("状态灯桥接服务启动（等待拍照日志 / 串口设备）")
    threading.Thread(target=journal_thread, args=(led,), daemon=True).start()
    waiting_logged = False
    boot_at = 0.0
    try:
        while True:
            # 主动维护串口: 插上就连, 连上后先发一次当前状态, 再定期心跳
            if led.ensure():
                if current is None:
                    led.send("BOOT", force=True)  # 刚连上先蓝呼吸一下
                    boot_at = time.time()
                    waiting_logged = False
                elif boot_at and time.time() - boot_at > 3:
                    boot_at = 0.0
                    led.send("IDLE", force=True)
                elif time.time() - last_sent > KEEPALIVE_S:
                    led.send(current, force=True)  # 心跳，防止灯重启后丢状态
            elif not waiting_logged:
                log("暂未发现串口设备（/dev/ttyUSB* 或 /dev/ttyACM*），继续等待")
                waiting_logged = True
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        pass


if __name__ == "__main__":
    sys.exit(main())
