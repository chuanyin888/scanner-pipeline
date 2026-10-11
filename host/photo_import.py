#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补录上传服务: 手机把"高拍仪拍不到"的照片传上来, 按标准命名存到指定文件夹.

接口:
  GET  /                      手机浏览器备用上传页
  GET  /folders               可选文件夹列表(最近 14 天的 日期/区域)
  GET  /app.apk               下载安卓 App
  POST /upload?folder=&ts=    上传 JPEG(body 为原始字节), 自动命名为 SN_日期_时分秒_微秒.jpg

存到 /opt/scanner/data/<日期>/<区域>/ 后由 nas_sync 每 5 分钟同步到 NAS.
"""
import glob
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import photo_count

DATA_DIR = "/opt/scanner/data"
HTTP_PORT = 8090
MAX_BYTES = 40 * 1024 * 1024
APK_DIR = "/opt/scanner/app"
LOG_FILE = "/opt/scanner/import_log.txt"
APP_LOG_DIR = "/opt/scanner/logs"


def log(msg):
    line = "[" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "] " + msg
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def list_folders(days=14):
    """返回可选的 日期/区域 列表(按日期倒序, 跳过 _ 开头的内部目录)."""
    out = []
    today = datetime.now().date()
    for i in range(days):
        day = (today - timedelta(days=i)).strftime("%Y%m%d")
        root = os.path.join(DATA_DIR, day)
        if not os.path.isdir(root):
            continue
        subs = sorted(
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and not d.startswith("_")
        )
        for s in subs:
            out.append(day + "/" + s)
    return out


def save_photo(folder, ts_ms, body):
    if folder not in list_folders():
        return None, "目录不在允许列表里"
    if not body or body[:2] != b"\xff\xd8":
        return None, "不是有效的 JPEG(缺少 FFD8 头)"
    try:
        ts = float(ts_ms) / 1000.0 if ts_ms else datetime.now().timestamp()
        dt = datetime.fromtimestamp(ts)
    except (TypeError, ValueError, OSError):
        dt = datetime.now()
    name = "SN_" + dt.strftime("%Y%m%d_%H%M%S") + "_" + format(dt.microsecond, "06d") + ".jpg"
    dst_dir = os.path.join(DATA_DIR, folder)
    os.makedirs(dst_dir, exist_ok=True)
    path = os.path.join(dst_dir, name)
    if os.path.exists(path):
        base, ext = os.path.splitext(name)
        name = base + "_" + str(os.getpid()) + ext
        path = os.path.join(dst_dir, name)
    with open(path, "wb") as fh:
        fh.write(body)
    photo_count.update_count_marker(dst_dir)
    total = len(glob.glob(os.path.join(dst_dir, "*.jpg")))
    return {"name": name, "folder": folder, "bytes": len(body), "count": total}, None


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>光猫补录</title>
<style>body{font-family:system-ui,"Microsoft YaHei";margin:16px;max-width:640px}
select,input,button{font-size:16px;padding:8px;width:100%;box-sizing:border-box;margin:6px 0}
button{background:#1f8a4c;color:#fff;border:0;border-radius:8px;padding:12px}
pre{background:#f4f4f4;padding:10px;border-radius:8px;white-space:pre-wrap}</style></head><body>
<h3>光猫补录(上传到机顶盒)</h3>
<label>补录到哪个文件夹</label><select id="folder"></select>
<label>照片</label><input id="file" type="file" accept="image/*" capture="environment">
<button onclick="up()">上传</button><pre id="out">选择文件夹和照片后点上传</pre>
<script>
fetch('/folders').then(r=>r.json()).then(a=>{const s=document.getElementById('folder');
 a.forEach(x=>{const o=document.createElement('option');o.textContent=x;s.appendChild(o)})});
async function up(){const f=document.getElementById('file').files[0];
 const folder=document.getElementById('folder').value;const out=document.getElementById('out');
 if(!f){out.textContent='先选一张照片';return}
 out.textContent='上传中 '+Math.round(f.size/1024)+'KB ...';
 const r=await fetch('/upload?folder='+encodeURIComponent(folder)+'&ts='+Date.now(),
   {method:'POST',headers:{'Content-Type':'image/jpeg'},body:f});
 out.textContent=await r.text();}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif url.path == "/folders":
            self._send(200, json.dumps(list_folders(), ensure_ascii=False))
        elif url.path == "/health":
            self._send(200, json.dumps({"ok": True, "service": "photo-import"}))
        elif url.path == "/count":
            q = urllib.parse.parse_qs(url.query)
            folder = q.get("folder", [""])[0]
            if folder not in list_folders():
                self._send(400, json.dumps({"ok": False, "msg": "目录不在允许列表里"},
                                           ensure_ascii=False))
                return
            n = len(glob.glob(os.path.join(DATA_DIR, folder, "*.jpg")))
            self._send(200, json.dumps({"ok": True, "folder": folder, "count": n},
                                       ensure_ascii=False))
        elif url.path == "/app.apk":
            apks = sorted(glob.glob(os.path.join(APK_DIR, "*.apk")))
            if not apks:
                self._send(404, json.dumps({"error": "服务器上还没有 APK"}))
                return
            with open(apks[-1], "rb") as fh:
                data = fh.read()
            fname = os.path.basename(apks[-1])
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(len(data)))
            # 头里只能放 ASCII；中文名按 RFC 5987 放在 filename* 里
            self.send_header(
                "Content-Disposition",
                'attachment; filename="guangmao-bulu_v1.0.apk"; filename*=UTF-8\'\''
                + urllib.parse.quote(fname),
            )
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_HEAD(self):
        """个别浏览器/下载器会先发 HEAD 探测，回一个空的 200 即可."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/applog":
            q = urllib.parse.parse_qs(url.query)
            device = re.sub(r"[^A-Za-z0-9_.-]", "", q.get("device", ["unknown"])[0])[:40] or "unknown"
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 1024 * 1024:
                self._send(400, json.dumps({"ok": False, "msg": "内容长度不合法"},
                                           ensure_ascii=False))
                return
            body = self.rfile.read(length)
            os.makedirs(APP_LOG_DIR, exist_ok=True)
            path = os.path.join(APP_LOG_DIR, "app_" + device + ".txt")
            with open(path, "wb") as fh:
                fh.write(("===== 收到于 " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                          + " (" + str(length) + "B) =====\n").encode("utf-8"))
                fh.write(body)
            log("收到App运行日志: " + device + " (" + str(length) + "B)")
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if url.path != "/upload":
            self._send(404, json.dumps({"error": "not found"}))
            return
        q = urllib.parse.parse_qs(url.query)
        folder = q.get("folder", [""])[0]
        ts_ms = q.get("ts", [""])[0]
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BYTES:
            self._send(400, json.dumps({"ok": False, "msg": "内容长度不合法"},
                                       ensure_ascii=False))
            return
        body = self.rfile.read(length)
        info, err = save_photo(folder, ts_ms, body)
        if err:
            log("上传被拒绝: " + err + " (folder=" + folder + ", " + str(length) + "B)")
            self._send(400, json.dumps({"ok": False, "msg": err}, ensure_ascii=False))
            return
        log("补录成功: " + info["folder"] + "/" + info["name"]
            + " (" + str(info["bytes"] // 1024) + "KB)")
        self._send(200, json.dumps({"ok": True, **info}, ensure_ascii=False))


if __name__ == "__main__":
    os.makedirs(APK_DIR, exist_ok=True)
    log("补录上传服务启动, 端口 " + str(HTTP_PORT)
        + ", 可选目录 " + str(len(list_folders())) + " 个")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
