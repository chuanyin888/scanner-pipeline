# 流水线高拍仪自动抓拍系统 (/opt/scanner)

放入设备 → 画面静止 → 自动抓拍高清图 → 拿走设备 → 空台复位 → 下一台。
开机自启，USB 掉线自动重连，无需人工干预。

## 组成

| 文件 | 作用 |
| --- | --- |
| `/opt/scanner/scanner_daemon.py` | 采集守护进程（对焦锁定 + 状态机 + 异步写盘 + 自愈） |
| `/opt/scanner/focus_sweep.py` | 逐档扫焦标定：实测画面最锐利处才是对焦值（现场调焦用） |
| `/opt/scanner/photo_import.py` | 手机补录上传服务（端口 8090）：高拍仪拍不到的设备用手机拍照/相册上传 |
| `/etc/systemd/system/scanner.service` | 开机自启与崩溃自拉起 |
| `/opt/scanner/focus.json` | 锁定的对焦值存档（重启直接复用，零等待） |
| `/opt/scanner/status.json` | 实时状态（每 5 秒刷新：状态/帧率/已抓拍数/清晰度） |
| `/opt/scanner/data/YYYYMMDD/` | 抓拍图片落盘目录，文件名 `SN_年月日_时分秒_微秒.jpg` |

## 常用命令

```bash
scannerctl status      # 设备节点 + 对焦/曝光控制项 + 实时状态
scannerctl logs        # 实时日志（Ctrl+C 退出）
scannerctl snapshot    # 立刻抓一张样张（含清晰度评分）
scannerctl focus       # 扫焦标定：先把典型设备放底板上，全程别动（约 30 秒）
scannerctl recalibrate # 重新标定空台背景：先清空台面再执行
scannerctl self-test   # 异步写盘链路自检
scannerctl files       # 最近 20 张抓拍
systemctl status scanner.service
```

## 手机补录（高拍仪拍不到的高设备）

机顶盒上跑着一个上传服务（`photo-import.service`，端口 **8090**）：

| 入口 | 说明 |
| --- | --- |
| `http://<机顶盒IP>:8090/app.apk` | 下载安卓 App「光猫补录」 |
| `http://<机顶盒IP>:8090/` | 备用：手机浏览器直接拍照/选相册上传（不用装 App） |
| `POST /applog?device=` | App 运行日志上报（存到 `/opt/scanner/logs/app_<型号>.txt`，出问题分析用） |

App 用法：刷新目录 → 选「日期/区域」→ 拍照或从相册选 → 上传。照片会被自动命名成
`SN_日期_时分秒_微秒.jpg` 存进对应文件夹，5 分钟内自动同步到 NAS。
上传只允许写到「最近 14 天的日期/区域」白名单目录，且必须是真的 JPEG。

## 图片数量标记（SSH 里一眼看数量）

每个照片文件夹自动维护一个 **名字就是图片数量** 的 txt 文件（如 `165.txt`）：
高拍仪分拣 / 手机补录每落一张照片，就自动改名一次（只统计 `.jpg`）。
该文件不会同步到 NAS（`nas_sync.py` 已排除 `<数字>.txt`）。
手动删过照片要重算：`python3 /opt/scanner/recount_all.py`

## 关键设计

- **设备节点**：用 `/dev/v4l/by-id/usb-Sonix_..._video-index0`。
  注意 `/dev/video0` 是机顶盒自带的硬件解码器，不是高拍仪（高拍仪在 video1）。
- **对焦锁定**：首次启动自动对焦收敛 → 写入 `focus_absolute` → 关闭连续自动对焦；
  值存入 `focus.json`，之后每次重启/重连都是零等待复用，不会反复对焦。
  **注意**：相机自带对焦在"空台"上容易收敛到错值（实测空台 480 vs 实物最佳 552，
  清晰度差 600 倍），所以正式使用前请在底板上放一台典型设备执行 `scannerctl focus`
  做一次扫焦标定，之后就一直复用这个值。
- **空台背景**：启动时请在**空台**状态下启动；如果启动时台上有东西，拿走后台面会被
  当成"新物品"。补救方式：清空台面后执行 `scannerctl recalibrate`。
- **启动时台面非空**：日志会提示"台面上有东西"，此时处于 WAIT_EMPTY 状态、不会抓拍；
  把台面清空约 5 秒，它会自动识别空台（亮区占比 < 5%）→ 重标定 → 开始工作，
  之后按正常流程放设备即可。
- **空台判定**：用中心区"亮像素占比"判断（深色底板 ≈0.1%，有白色标签的实物 15~25%），
  比清晰度阈值稳（底板在不同曝光下噪声差异大）。
- **主循环轻量**：只在 ROI 上抽点降采样做灰度差分（约 4ms），
  5MP(2592x1944) 下约 6-7 fps，一帧 145ms 左右。
- **异步写盘**：主循环只把帧丢进队列，2 个写盘线程负责 JPEG 编码落盘（约 130ms/张）。
- **状态机**：EMPTY → SETTLING(检测到物品) → CAPTURED(已拍) → 空台复位 → EMPTY，
  拍完不复位就不会重复触发。
- **自愈**：连续读帧失败 40 次 → 重连设备；节点消失时每 3 秒重试，插回后自动恢复。

## 灵敏度调节（文件顶部 CFG）

| 现象 | 调整 |
| --- | --- |
| 手还没拿开就提前拍 | `stable_frames` 3 → 4 或 5 |
| 放上设备不触发 | `item_ratio` 0.005 → 0.003 |
| 空台偶尔误触发 | `item_ratio` 0.005 → 0.008 |
| 想更快的响应 | `width/height` 改 1920x1080（约 8fps，照片变小） |
| 想更高画质 | `width/height` 改 3264x2448（约 4.4fps，一帧约 225ms） |

改完执行 `systemctl restart scanner.service`。

## 日志与排障

```bash
journalctl -u scanner.service -n 100 -o cat     # 最近日志
journalctl -u scanner.service -f -o cat         # 实时日志
v4l2-ctl -d /dev/video1 --list-ctrls            # 相机控制项
```

抓拍日志会带清晰度评分（拉普拉斯方差）：正常实拍应该在 1000 上下，
低于 100 会自动提醒，说明失焦（放一台设备执行 `scannerctl focus`）或台面确实空的。
