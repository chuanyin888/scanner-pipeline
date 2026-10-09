# 流水线高拍仪自动抓拍系统

放到高拍仪下的设备，**静止即自动抓拍高清图**，拍完用灯光告诉操作员"可以拿走了"；
配合**区域二维码卡**，照片自动归档到「日期 / 区域」子文件夹。

工作流程：`放入设备 → 画面静止 → 自动抓拍 → 绿闪提示 → 拿走设备 → 空台复位 → 下一台`

## 组成

| 模块 | 位置 | 作用 |
| --- | --- | --- |
| 采集守护 | `host/scanner_daemon.py` | 对焦锁定、空台检测、状态机、异步写盘、断线自愈 |
| 扫焦标定 | `host/focus_sweep.py` | 逐档扫描焦点，用实测清晰度选最锐利值并存档 |
| 区域分拣 | `host/sorter.py` | 识别 `enter:<区域名>` 二维码卡，照片按区域归档 |
| 状态灯桥接 | `host/led_bridge.py` | 把上面两个服务的日志事件翻译成串口指令发给 ESP32 |
| NAS 同步 | `host/nas_sync.py` | 每 5 分钟把抓拍数据推到 NAS；历史日期只写一次并冻结 |
| 状态灯固件 | `esp32/led_status/` | ESP32-S3 + 板载 WS2812，按状态变色 |
| 区域卡工具 | `cards/make_card.py` | 输入区域名 → 生成可打印二维码卡（含"取消区域"卡） |

## 硬件

- 主机：任意 arm64 Debian/Linux 机顶盒（本项目在 Amlogic S905L3A + Armbian 上验证）
- 相机：USB UVC 高拍仪，支持 MJPG（本项目用 2592×1944@10fps）
- 指示灯：ESP32-S3 开发板（板载 WS2812，数据脚 GPIO48；另有 IO2 单色灯）
- 相机与指示灯都插在机顶盒 USB 口上，指示灯同时从 USB 取电

## 部署

### 1. 依赖

```bash
sudo apt-get update
sudo apt-get install -y python3-opencv python3-numpy v4l-utils libv4l-dev zbar-tools
```

> 二维码解码用 `zbarimg`（Debian 的 OpenCV 未编入 QUIRC，`cv2.QRCodeDetector` 解不了码）。

### 2. 安装服务

```bash
sudo mkdir -p /opt/scanner/data /opt/scanner/fw
sudo cp host/scanner_daemon.py host/focus_sweep.py host/sorter.py host/led_bridge.py /opt/scanner/
sudo cp host/bin/scannerctl host/bin/sorterctl host/bin/ledcmd /usr/local/bin/
sudo chmod +x /usr/local/bin/scannerctl /usr/local/bin/sorterctl /usr/local/bin/ledcmd
sudo cp host/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scanner.service sorter.service led-bridge.service
```

启动前请确认**台面是空的**（空台背景要干净），日志里会提示。

### 3. 对焦标定（重要）

相机自带的自动对焦在**无纹理的深色台面**上会收敛到错误值（实测空台 480 / 实物需要 552，
清晰度差几百倍）。所以要把一台典型设备放在镜头下，执行：

```bash
scannerctl focus     # 逐档扫焦，约 30 秒，结果存档 /opt/scanner/focus.json
```

之后重启、重插都直接复用该值，零等待。

### 4. ESP32 状态灯

```bash
cd esp32/led_status
pio run -t upload --upload-port /dev/ttyACM0     # 板子插在机顶盒上即可
```

串口设备名可能是 `/dev/ttyACM0`（CDC-ACM）或 `/dev/ttyUSB0`（ch341 驱动），
`led_bridge.py` 两种都会自动找。

### 5. 区域卡

```bash
py cards/make_card.py 江北区 江宁区 栖霞区     # 生成 PNG + 合并 PDF
py cards/make_card.py --cancel                 # 生成"取消区域"卡
```

Windows 上可直接双击 `cards/做区域卡.bat`，按提示输入区域名。

## 命令速查

| 命令 | 作用 |
| --- | --- |
| `scannerctl status` | 设备节点 + 相机控制项 + 实时状态 |
| `scannerctl logs` | 实时抓拍日志 |
| `scannerctl snapshot` | 立刻抓一张样张（含清晰度评分） |
| `scannerctl focus` | 扫焦标定（把典型设备放台面上执行） |
| `scannerctl recalibrate` | 重新标定空台背景（先清空台面） |
| `scannerctl self-test` | 异步写盘链路自检 |
| `sorterctl --show` | 当前区域 + 今日各区照片数 |
| `sorterctl --set 江北区` | 手动切换区域（等同扫卡） |
| `sorterctl --clear` | 取消当前区域 |
| `sorterctl --move 未分拣 江北区` | 把未分拣整批挪到指定区域 |
| `ledcmd REGION` | 手动触发灯效（调试用） |

## 状态灯约定

| 状态 | 灯效 | 含义 |
| --- | --- | --- |
| 待命 | 红灯常亮 | 空台，可以放设备 |
| 检测到设备 | 黄白交替 | 等画面稳定 / 对焦中 |
| 拍好了 | 绿灯不停闪 | 已落盘，闪到把设备拿走为止 |
| 可能失焦 | 紫灯闪 | 拍了但清晰度偏低，建议重拍 |
| 异常 | 红白快闪 | 相机掉线 / 写盘失败 |
| 启动/标定 | 蓝灯呼吸 | 开机、标定背景中 |
| 区域切换 | 红→绿→蓝 5 秒 + IO2 常亮 | 区域卡生效 |
| 取消区域 | 白灯闪 3 下 | 取消卡生效 |

## 区域卡规则

- 二维码内容：`enter:<区域名>`，例如 `enter:江北区`
  - 生成时区域名会被**百分号编码**（纯 ASCII），避免不同扫码器把中文解释成乱码
  - 大小写不敏感，兼容全角冒号
- `enter:cancel`（或 `enter:取消`）= **取消区域卡**：清空当前区域，之后照片进 `未分拣/`
- 扫描区域卡会**立刻建立** `data/<日期>/<区域>/` 目录；重复扫同一张卡 = 继续用同一个目录
- 同一张卡在不同日期使用时会自动落到当天日期目录下
- **区域卡本身的照片不保留**（识别成功后直接删除）；切换留痕写在 `/opt/scanner/region_history.log` 一行文字
- 其它二维码（例如设备标签上的）一律忽略

## 数据目录

```

## NAS 同步（入库数据）

每 5 分钟由 `nas-sync.timer` 触发 `nas_sync.py`，把 `data/<日期>/<区域>/*.jpg` 推送到 NAS：

```ini
# /etc/fstab（示例）
//<NAS地址>/入库数据 /mnt/nas_ruku cifs credentials=/etc/nas-creds.cred,vers=3.0,iocharset=utf8,nofail,x-systemd.automount,_netdev 0 0
```

| 场景 | 行为 |
| --- | --- |
| **当天**目录 | 每轮增量同步，只补新文件 |
| **历史**日期目录 | 只做一次最终同步 → 校验文件数+字节数 → 写 `/opt/scanner/nas_sync_state.json` 标记 `final` → 以后**永久跳过，不再重写** |
| NAS 未挂载 / 同步失败 | 本轮跳过、不冻结，下一轮自动重试 |
| 删除 | **只增不删**，绝不动 NAS 上的文件 |

注意事项：

1. 同步必须用 `rsync --inplace`：普通 rsync 会在目标目录建 `._名字.XXXX` 临时文件，
   部分 NAS（如飞牛）的 Samba 不认这种名字，报 `mkstemp ... ENOENT` 写不进去。
2. "冻结"只是同步脚本自己的状态标记，**不会锁定 NAS 目录**：其它程序照常读写删。
   注意当天目录若在 NAS 上被删除，下一轮会从本机补回来（源文件还在）；要删除当天的图，
   先把它当历史（第二天）处理，或手动在状态文件里标记 `final`。
/opt/scanner/data/
└── 20261007/
    ├── 江北区/
    │   ├── SN_20261007_200148_519171.jpg
    │   └── ...
    ├── 未分拣/                 # 还没扫区域卡时拍的照片
    └── _切换记录/              # 区域卡本身的照片
```

## 设计要点与坑（踩过的）

1. **相机节点不能用索引 0**：机顶盒的 `/dev/video0` 往往是 SoC 硬件解码器，
   高拍仪实际在 `/dev/video1`。程序用 `/dev/v4l/by-id/...` 稳定路径，并按名称匹配。
2. **对焦**：UVC 相机的自动对焦在无纹理平面上不可信 → 用扫焦法选最锐利档并锁定，
   值存 `focus.json`，重启/重连零等待复用。新开视频流要重新套用一次锁定值。
3. **主循环要轻**：ROI 抽点降采样后做灰度差分（约 4ms），5MP 下约 6fps；
   高清 JPEG 由后台写盘线程处理（约 130ms/张），不阻塞采集。
4. **空台判定**：亮区占比 + 清晰度两个条件同时满足才算空台
   （只用亮区占比会把深色电路板误判成空台；实测空台≈124、深色板≈667、白壳设备≈1400）。
5. **WS2812 不能高频刷新**：连续高速写帧会导致灯"卡在第一种颜色"，
   所以只在颜色变化时写一次，各效果都用离散色阶。
6. **zbar 的字符集坑**：`zbarimg` 默认把二维码内容按日文 Shift-JIS 解释，中文会变乱码；
   必须加 `-Sbinary` 取原始字节再按 UTF-8 解码。
7. **状态机**：`EMPTY → SETTLING → CAPTURED → 空台复位`，拍完不复位不会重复触发；
   启动时若台面非空会进入等待模式，等清空后自动重标定。
8. **空台不落盘**：自动曝光漂移会把空台"伪装"成新物品（实测一天误拍 8 张）。
   落盘前用"减均值后的归一化互相关（NCC）"判断是否就是空台：空台漂移照片 0.975、
   带标签光猫 0.632、手机屏幕 0.246 —— 阈值取 0.90，空台只记日志不存图。
9. **区域卡不留图**：卡片识别成功后直接删除照片，避免入库数据里混入卡片照片。

## 运行环境

本项目为内部使用，仓库保持**私有**。
