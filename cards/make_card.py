#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区域卡生成器：输入区域名 -> 输出可打印的 A5 横版卡片（含大二维码 + 大字区域名）

卡上的二维码内容固定为  enter:<区域名>
用法:
  py make_card.py 江北区 江宁区 栖霞区           # 生成多张卡片 + 一个合并 PDF
  py make_card.py --out D:\\cards 六合区
"""
import argparse
import os
import sys
from urllib.parse import quote

import qrcode
from PIL import Image, ImageDraw, ImageFont
from PIL import JpegImagePlugin  # noqa: F401  注册 JPEG 编解码器(PDF 需要)

W, H = 2480, 1748          # A5 横版 @300dpi
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 粗体
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_card(region, out_png):
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    # 外框
    d.rectangle([8, 8, W - 9, H - 9], outline="black", width=8)

    # 二维码
    payload = "enter:" + quote(region, safe="")   # 百分号编码: 纯 ASCII, 任何扫码器都不会乱码
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 980
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    img.paste(qr_img, ((W - qr_size) // 2, 110))

    # 区域名（大字）
    font_big = load_font(230)
    text = region
    box = d.textbbox((0, 0), text, font=font_big)
    tw = box[2] - box[0]
    while tw > W - 200 and font_big.size > 60:
        font_big = load_font(font_big.size - 12)
        box = d.textbbox((0, 0), text, font=font_big)
        tw = box[2] - box[0]
    d.text(((W - tw) // 2 - box[0], 1180), text, font=font_big, fill="black")

    # 说明
    font_small = load_font(52)
    tip = "把这张卡放到高拍仪下 = 切换到该区域（后续照片自动归到此文件夹）"
    box = d.textbbox((0, 0), tip, font=font_small)
    d.text(((W - (box[2] - box[0])) // 2 - box[0], 1500), tip, font=font_small, fill="#555555")
    d.text((60, 60), payload, font=load_font(38), fill="#999999")

    img.save(out_png, dpi=(300, 300))
    return img


def make_cancel_card(out_png):
    """取消区域卡：内容 enter:cancel，样式与区域卡明显不同（淡黄底 + 大红叉）."""
    img = Image.new("RGB", (W, H), "#FFF6E0")
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, W - 9, H - 9], outline="black", width=14)
    d.rectangle([34, 34, W - 35, H - 35], outline="#C0392B", width=6)

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data("enter:cancel")
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((820, 820), Image.NEAREST)
    img.paste(qr_img, (140, 200))

    d.line([(1120, 300), (1360, 540)], fill="#C0392B", width=38)
    d.line([(1360, 300), (1120, 540)], fill="#C0392B", width=38)

    font_big = load_font(200)
    d.text((1420, 320), "取消", font=font_big, fill="#C0392B")
    d.text((1420, 520), "区域", font=font_big, fill="#C0392B")

    font_mid = load_font(62)
    d.text((140, 1180), "放一下 = 结束当前区域", font=font_mid, fill="black")
    d.text((140, 1280), "之后的照片进  未分拣/  文件夹", font=font_mid, fill="black")
    d.text((140, 1400), "（不会删除任何已有照片和文件夹）", font=load_font(48), fill="#666666")
    d.text((60, 70), "enter:cancel", font=load_font(40), fill="#999999")

    img.save(out_png, dpi=(300, 300))
    return img


def make_big_card(label, out_png, payload):
    """大码版：二维码几乎占满整张（正方形），适合手机显示或放大打印.

    小码+偏位+虚焦时扫码器解不出来（实测过），大码能大幅提高成功率。
    """
    side = 2000
    img = Image.new("RGB", (side, side + 260), "white")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((side - 40, side - 40), Image.NEAREST)
    img.paste(qr_img, (20, 20))
    f = load_font(150)
    d = ImageDraw.Draw(img)
    box = d.textbbox((0, 0), label, font=f)
    d.text(((side - (box[2] - box[0])) // 2 - box[0], side + 40), label, font=f, fill="black")
    img.save(out_png, dpi=(300, 300))
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("regions", nargs="*", help="区域名，例如 江北区 江宁区")
    ap.add_argument("--cancel", action="store_true", help="生成一张『取消区域』卡")
    ap.add_argument("--big", action="store_true", help="生成大码版（二维码占满整页，适合手机显示）")
    ap.add_argument("--interactive", action="store_true", help="交互式输入区域名")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if args.cancel:
        if args.big:
            png = os.path.join(args.out, "取消区域卡_大码.png")
            make_big_card("取消区域", png, "enter:cancel")
        else:
            png = os.path.join(args.out, "取消区域卡.png")
            make_cancel_card(png)
        print(f"已生成: {png}")
        return 0

    if args.interactive or not args.regions:
        print("=" * 46)
        print(" 区域卡生成器 —— 卡上二维码内容 = enter:<区域名>")
        print("=" * 46)
        line = input("请输入区域名（多个用空格分隔，直接回车退出）: ").strip()
        if not line:
            print("已取消")
            return 1
        args.regions = line.split()

    pages = []
    if args.big:
        for name in args.regions:
            safe = name.strip().replace("/", "_").replace("\\", "_")
            png = os.path.join(args.out, f"区域卡_{safe}_大码.png")
            make_big_card(safe, png, "enter:" + quote(safe, safe=""))
            print(f"已生成: {png}")
        return 0
    for name in args.regions:
        safe = name.strip().replace("/", "_").replace("\\", "_")
        png = os.path.join(args.out, f"区域卡_{safe}.png")
        pages.append(make_card(safe, png))
        print(f"已生成: {png}")
    pdf = os.path.join(args.out, "区域卡合集.pdf")
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=300)
    print(f"已生成合并 PDF（可直接打印，每页一张卡）: {pdf}")


if __name__ == "__main__":
    sys.exit(main())
