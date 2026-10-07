#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区域分拣服务（B1）

监听 /opt/scanner/data/<日期>/ 下新拍的 jpg：
  * 卡片二维码内容匹配 enter:<区域名>（大小写不敏感、兼容全角冒号）
      -> 切换当前区域 + 该照片挪到 _切换记录/ + 日志 "区域已切换: <名字>"
         （灯桥接会据此播 5 秒红绿蓝确认效果）
  * enter: 后面的名字非法/为空 -> 不切换，照片挪到 _切换记录/，日志 "区域卡无效: ..."
  * 普通设备照片 -> 移到 <日期>/<当前区域>/；未选区域时进 <日期>/未分拣/

命令行：
  python3 sorter.py --show              查看当前区域 + 今天各区数量
  python3 sorter.py --set 江北区         手动切换区域（同扫卡）
  python3 sorter.py --move 未分拣 江北区   把未分拣整批挪到指定区域
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import unquote

DATA_DIR = "/opt/scanner/data"
STATE_FILE = "/opt/scanner/current_region.json"
STATS_FILE = "/opt/scanner/region_stats.json"
SWITCH_DIR = "_切换记录"
UNSORTED = "未分拣"

CARD_RE = re.compile(r"^\s*enter\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
LOOKS_LIKE_CARD = re.compile(r"^\s*enter", re.IGNORECASE)
ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
CANCEL_WORDS = {"cancel", "取消", "取消区域", "结束", "none"}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def today():
    return datetime.now().strftime("%Y%m%d")


# ---------------- 状态 ----------------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def current_region():
    return load_json(STATE_FILE, {}).get("region")


def set_region(name):
    save_json(STATE_FILE, {"region": name, "updated": datetime.now().isoformat(timespec="seconds")})


def clear_region():
    save_json(STATE_FILE, {"region": None, "updated": datetime.now().isoformat(timespec="seconds")})


def stats_add(region):
    stats = load_json(STATS_FILE, {})
    day = stats.setdefault(today(), {})
    day[region] = day.get(region, 0) + 1
    save_json(STATS_FILE, stats)


# ---------------- 二维码 ----------------
def decode_qr(path):
    """用 zbarimg 取二维码**原始字节**再自己解码.

    注意: 普通的 zbarimg 会把二维码内容按日文 Shift-JIS 解释, 中文会变乱码
    （实测 "江北区" -> "豎溷圏蛹ｺ"）; 加 -Sbinary 才是原样字节.
    """
    try:
        proc = subprocess.run(
            ["zbarimg", "-q", "--raw", "-Sbinary", "-Sdisable", "-Sqrcode.enable", path],
            capture_output=True, timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"zbarimg 调用失败: {exc}")
        return []
    texts = []
    for raw in (proc.stdout or b"").splitlines():
        if not raw.strip():
            continue
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                texts.append(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    return texts


def clean_name(raw):
    name = unquote(raw)          # 卡片里是百分号编码, 这里解回中文
    name = ILLEGAL_RE.sub("", name).strip().strip(".")
    name = name[:32].strip()
    if not name or name in (".", ".."):
        return None
    return name


# ---------------- 文件动作 ----------------
def unique_dest(folder, filename):
    os.makedirs(folder, exist_ok=True)
    dst = os.path.join(folder, filename)
    if not os.path.exists(dst):
        return dst
    stem, ext = os.path.splitext(filename)
    for i in range(1, 1000):
        cand = os.path.join(folder, f"{stem}_{i}{ext}")
        if not os.path.exists(cand):
            return cand
    return dst


def handle_photo(path, date_dir):
    texts = decode_qr(path)
    name = os.path.basename(path)

    card_raw = None
    for text in texts:
        m = CARD_RE.match(text)
        if m:
            card_raw = m.group(1)
            break

    if card_raw is not None:
        region = clean_name(card_raw)
        if region is None:
            dst = unique_dest(os.path.join(date_dir, SWITCH_DIR), f"无效_{name}")
            os.replace(path, dst)
            log(f"区域卡无效: 名字不合法或为空 -> {os.path.basename(dst)}")
            return
        if region.lower() in CANCEL_WORDS:
            clear_region()
            dst = unique_dest(os.path.join(date_dir, SWITCH_DIR), f"取消_{name}")
            os.replace(path, dst)
            log(f"区域已取消: 后续照片进 {UNSORTED}/  (卡片照片: {os.path.basename(dst)})")
            return
        set_region(region)
        os.makedirs(os.path.join(date_dir, region), exist_ok=True)  # 扫卡即建好区域文件夹
        dst = unique_dest(os.path.join(date_dir, SWITCH_DIR), name)
        os.replace(path, dst)
        log(f"区域已切换: {region}  (已建/复用文件夹 {region}/, 卡片照片: {os.path.basename(dst)})")
        return

    if any(LOOKS_LIKE_CARD.match(t) for t in texts):
        dst = unique_dest(os.path.join(date_dir, SWITCH_DIR), f"无效_{name}")
        os.replace(path, dst)
        log(f"区域卡无效: 无法解析内容 -> {os.path.basename(dst)}")
        return

    region = current_region() or UNSORTED
    dst = unique_dest(os.path.join(date_dir, region), name)
    os.replace(path, dst)
    stats_add(region)
    if region == UNSORTED:
        log(f"未选择区域, 已放入 {UNSORTED}/: {name}")
    else:
        log(f"已归档 -> {region}/: {name}")


def watch_loop():
    log("区域分拣服务启动（监听新照片）")
    seen = set()
    while True:
        for date_dir in sorted(glob.glob(os.path.join(DATA_DIR, "*/"))):
            for path in sorted(glob.glob(os.path.join(date_dir, "*.jpg"))):
                if path in seen:
                    continue
                try:
                    if time.time() - os.path.getmtime(path) < 0.8:
                        continue  # 刚写完的再等一轮, 保险
                except OSError:
                    continue
                seen.add(path)
                try:
                    handle_photo(path, date_dir)
                except Exception as exc:  # noqa: BLE001
                    log(f"处理失败 {os.path.basename(path)}: {exc}")
        if len(seen) > 5000:
            seen = set(sorted(seen)[-1000:])
        time.sleep(0.7)


# ---------------- 命令行 ----------------
def cmd_show():
    region = current_region() or "(未选择, 照片进 未分拣/)"
    print(f"当前区域: {region}")
    stats = load_json(STATS_FILE, {}).get(today(), {})
    if stats:
        print(f"今天（{today()}）各区域照片数:")
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
            print(f"  {k:12s} {v} 张")
    else:
        print("今天还没有归档照片")


def cmd_set(name):
    clean = clean_name(name)
    if clean is None:
        print("区域名不合法")
        return 1
    set_region(clean)
    os.makedirs(os.path.join(DATA_DIR, today(), clean), exist_ok=True)
    print(f"当前区域已设为: {clean}")
    log(f"区域已切换: {clean}")  # 触发灯效
    return 0


def cmd_move(src, dst):
    src_dir = os.path.join(DATA_DIR, today(), src)
    dst_dir = os.path.join(DATA_DIR, today(), dst)
    files = sorted(glob.glob(os.path.join(src_dir, "*.jpg")))
    if not files:
        print(f"{src_dir} 里没有照片")
        return 1
    os.makedirs(dst_dir, exist_ok=True)
    for path in files:
        os.replace(path, unique_dest(dst_dir, os.path.basename(path)))
    print(f"已把 {len(files)} 张从 {src}/ 移到 {dst}/")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--set", metavar="区域名")
    ap.add_argument("--clear", action="store_true", help="取消当前区域（同扫取消卡）")
    ap.add_argument("--move", nargs=2, metavar=("源", "目标"))
    args = ap.parse_args()
    if args.show:
        cmd_show()
        return 0
    if args.set:
        return cmd_set(args.set)
    if args.clear:
        clear_region()
        print("当前区域已取消，后续照片进 未分拣/")
        log("区域已取消: 后续照片进 未分拣/")
        return 0
    if args.move:
        return cmd_move(*args.move)
    watch_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
