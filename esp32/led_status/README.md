# ESP32 状态灯固件

串口（115200）收单行指令，驱动板载 WS2812（GPIO48）+ IO2 单色灯（GPIO2）。

| 指令 | 灯效 |
| --- | --- |
| `IDLE` | 红灯常亮（待命） |
| `BUSY` | 黄白交替（检测到设备） |
| `OK` | 绿灯闪（拍好了，直到设备被拿走） |
| `WARN` | 紫灯闪（可能失焦） |
| `ERROR` | 红白快闪（异常） |
| `BOOT` | 蓝灯呼吸（启动/标定） |
| `REGION` | 红→绿→蓝 5 秒 + IO2 常亮（区域卡切换成功） |
| `CANCEL` | 白灯闪 3 下（取消区域卡） |

编译烧录（PlatformIO）：

```bash
pio run                       # 编译
pio run -t upload --upload-port /dev/ttyACM0
# 或在 PC 上: pio run -t upload --upload-port COM6
```

注意：WS2812 不能高频刷新，本固件只在颜色变化时写一次；改动画速度时请保持低刷新率。
