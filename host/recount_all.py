#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重算所有照片文件夹的「数量标记」文件（<N>.txt）。

用法: python3 /opt/scanner/recount_all.py
（平时不用跑：分拣服务/手机补录服务会在每张照片落盘后自动更新；
  只有"手动删过照片"或"手动拷过照片"之后，需要跑一次来重算。）
"""
import glob
import os

from photo_count import update_count_marker

DATA_DIR = "/opt/scanner/data"


def main():
    total = 0
    for date_dir in sorted(glob.glob(os.path.join(DATA_DIR, "*/"))):
        leftover = len(glob.glob(os.path.join(date_dir, "*.jpg")))
        if leftover:
            print(f"注意: {date_dir} 根目录还有 {leftover} 张照片没分拣（分拣服务可能没在跑）")
        for folder in sorted(glob.glob(os.path.join(date_dir, "*/"))):
            n = update_count_marker(folder)
            total += 1
            print(f"{n:5d}  {folder}")
    print(f"共 {total} 个文件夹已重算")


if __name__ == "__main__":
    main()
