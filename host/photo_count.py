#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件夹图片数量标记：在文件夹里维护一个名字=图片数量的 txt 文件。

规则：
  * 每有新照片落进文件夹后调用 update_count_marker(文件夹)
  * 数一遍文件夹里的 *.jpg，把标记文件改名为 "<数量>.txt"（旧的标记删掉，只留一个）
  * 名字即数量：SSH 里 ls 一眼就能看到这个文件夹有多少张照片
  * 永远不抛异常，不影响拍照/上传主流程
"""
import glob
import os
import re

_COUNT_TXT_RE = re.compile(r"^\d+\.txt$")


def count_jpg(folder):
    try:
        return len(glob.glob(os.path.join(folder, "*.jpg")))
    except OSError:
        return 0


def update_count_marker(folder):
    """把 folder 里的数量标记文件改成当前图片数（<N>.txt），返回 N；出错返回 -1。"""
    try:
        n = count_jpg(folder)
        want = str(n) + ".txt"
        for path in glob.glob(os.path.join(folder, "*.txt")):
            base = os.path.basename(path)
            if _COUNT_TXT_RE.match(base) and base != want:
                try:
                    os.remove(path)
                except OSError:
                    pass
        target = os.path.join(folder, want)
        if not os.path.exists(target):
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("本文件的名字就是本文件夹的图片数量（自动维护：每加一张照片自动改名）\n")
        return n
    except Exception:  # noqa: BLE001
        return -1
