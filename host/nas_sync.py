#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把抓拍数据同步到 NAS 的「入库数据」共享.

规则（按用户要求）：
  * 每 5 分钟跑一次（由 nas-sync.timer 触发）
  * **当天**的日期目录：每轮增量同步，只补新文件，不覆盖已存在的
  * **历史**日期目录：只做一次"最终同步"，成功后在状态文件里标记 final，
    老文件以后**永远不再重写**；冻结之后新增的文件（手机补录等）会补传一次
  * NAS 没挂载 / 同步失败：本轮跳过，不标记 final，下一轮自动重试
  * 只增不删：绝不在 NAS 上删除文件

结构：/opt/scanner/data/<日期>/<区域>/*.jpg  ->  <NAS>/入库数据/<日期>/<区域>/*.jpg
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

SRC = "/opt/scanner/data"
DST = "/mnt/nas_ruku"
STATE_FILE = "/opt/scanner/nas_sync_state.json"
LOCK_FILE = "/run/nas_sync.lock"
DATE_RE = re.compile(r"^\d{8}$")


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def rsync(src_dir, dst_dir, dry=False):
    """同步到 NAS.

    注意: 必须用 --inplace。普通 rsync 会在目标目录建 "._名字.XXXX" 形式的临时文件，
    飞牛 NAS 的 Samba 不认这种名字（mkstemp 报 ENOENT），实测只有 --inplace 能写入。
    另外不加 --ignore-existing：万一某次断电留下半个文件，下一轮能自动补全；
    历史目录"只写一次"是靠状态文件冻结实现的，不是靠跳过已存在文件。
    """
    cmd = [
        "rsync", "-rt", "--inplace", "--no-perms", "--no-owner", "--no-group",
        "--exclude=[0-9]*.txt",
        "--exclude=*.tmp",
        "--exclude=/SN_*.jpg",
        "--stats", "--human-readable", src_dir + "/", dst_dir + "/",
    ]
    if dry:
        cmd.insert(1, "-n")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return proc.returncode, proc.stdout + proc.stderr


def parse_ts(s):
    """把状态文件里的 'YYYY-mm-dd HH:MM:SS' 转成时间戳；无法解析时返回 None."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None


def rsync_extra(src_dir, dst_dir, rel_paths):
    """只补传 rel_paths 列出的文件（冻结目录的补录用）.

    用 --files-from 精确点传：列表之外的文件一律不碰，
    所以 NAS 上被手动删掉的老文件不会被"复活"。
    """
    cmd = [
        "rsync", "-rt", "--inplace", "--no-perms", "--no-owner", "--no-group",
        "--exclude=[0-9]*.txt",
        "--exclude=*.tmp",
        "--files-from=-", "--stats", "--human-readable",
        src_dir + "/", dst_dir + "/",
    ]
    proc = subprocess.run(cmd, input="\n".join(rel_paths) + "\n",
                          capture_output=True, text=True, timeout=1800)
    return proc.returncode, proc.stdout + proc.stderr


def sync_new_after_freeze(day, info, src, dst):
    """历史目录冻结后新增的文件（手机补录等）：增量补传一次，返回补传个数.

    判据：文件 mtime 晚于冻结时间（info["at"]）。
      * 冻结后新增 -> 补传；
      * 冻结前的老文件（哪怕 NAS 上被删了）-> 一律不碰。
    已补传过的记在 info["extra"]（相对路径 -> mtime），不会重复传。
    """
    freeze_ts = parse_ts(info.get("at", ""))
    if freeze_ts is None or not os.path.isdir(src) or not os.path.isdir(dst):
        return 0
    extra = info.get("extra", {})
    todo = {}
    for dirpath, _dirs, files in os.walk(src):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                mt = round(os.path.getmtime(path), 3)
            except OSError:
                continue
            if mt <= freeze_ts + 1:
                continue  # 冻结前的老文件，永不重写
            if re.match(r"^\d+\.txt$", name):
                continue  # 数量标记文件（名字=图片数）不同步到 NAS
            if name.endswith(".tmp"):
                continue  # 写盘中的临时文件
            rel = os.path.relpath(path, src)
            if "/" not in rel:
                continue  # 根目录里的都是"待分拣"路过文件，分拣进区域文件夹后再同步
            if abs(extra.get(rel, -1) - mt) < 0.001:
                continue  # 这个文件之前已经补传过
            todo[rel] = mt
    if not todo:
        return 0
    rels = sorted(todo)
    rc, out = rsync_extra(src, dst, rels)
    if rc != 0:
        log(f"历史 {day} 补录补传失败（rsync exit={rc}）:\n{out[-800:]}")
        return 0
    pushed = 0
    added_bytes = 0
    for rel in rels:
        sp = os.path.join(src, rel)
        dp = os.path.join(dst, rel)
        try:
            if os.path.exists(dp) and os.path.getsize(dp) == os.path.getsize(sp):
                extra[rel] = todo[rel]
                pushed += 1
                added_bytes += os.path.getsize(sp)
            else:
                log(f"历史 {day} 补传校验未通过: {rel}")
        except OSError:
            pass
    if pushed:
        info["extra"] = extra
        info["files"] = info.get("files", 0) + pushed
        info["bytes"] = info.get("bytes", 0) + added_bytes
        log(f"历史 {day}: 冻结后新增 {pushed} 个补录文件已补传（老文件未动）")
    return pushed


def count_files(root):
    n = 0
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                n += 1
            except OSError:
                pass
    return n, total


def main():
    # 防重叠
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log("上一次同步还在跑，跳过本轮")
        return 0
    os.write(lock_fd, str(os.getpid()).encode())

    today = datetime.now().strftime("%Y%m%d")
    try:
        if not os.path.ismount(DST):
            log(f"NAS 未挂载（{DST}），本轮跳过，等挂载后自动重试")
            return 1

        state = load_state()
        dates = sorted(
            d for d in os.listdir(SRC)
            if DATE_RE.match(d) and os.path.isdir(os.path.join(SRC, d))
        )
        if not dates:
            log("没有日期目录，无需同步")
            return 0

        changed = False
        for day in dates:
            info = state.get(day, {})
            src = os.path.join(SRC, day)
            dst = os.path.join(DST, day)
            if info.get("status") == "final":
                # 历史目录已冻结：老文件永不重写，但冻结后新增的（补录）要补传
                if sync_new_after_freeze(day, info, src, dst):
                    changed = True
                continue

            n_local, bytes_local = count_files(src)
            kind = "当天" if day == today else ("历史" if day < today else "未来")

            if kind == "未来":
                log(f"{day} 是未来日期（请检查系统时间），跳过")
                continue

            t0 = time.time()
            rc, out = rsync(src, dst)
            cost = time.time() - t0
            if rc != 0:
                log(f"同步失败 {day}（rsync exit={rc}，耗时 {cost:.1f}s）:\n{out[-800:]}")
                continue

            n_remote, bytes_remote = count_files(dst)
            if kind == "当天":
                log(f"当天 {day}: 本地 {n_local} 个文件 / NAS {n_remote} 个，耗时 {cost:.1f}s")
            else:
                if n_remote < n_local or bytes_remote < bytes_local:
                    log(f"历史 {day} 校验未通过：本地 {n_local} 个({bytes_local}B) "
                        f"vs NAS {n_remote} 个({bytes_remote}B)，不冻结，下轮重试")
                    continue
                state[day] = {
                    "status": "final",
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "files": n_remote,
                    "bytes": bytes_remote,
                }
                changed = True
                log(f"历史 {day} 已完成最终同步并冻结（{n_remote} 个文件，{bytes_remote} 字节），"
                    "以后不会再被重写")

        if changed:
            save_state(state)
        return 0
    finally:
        os.close(lock_fd)
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
