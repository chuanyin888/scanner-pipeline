# 光猫补录（安卓 App）

高拍仪拍不到的高设备（太高/太大），用手机拍照补录，直接进机顶盒的
`data/<日期>/<区域>/`，5 分钟内自动同步到 NAS。

## 功能

- 选择目标文件夹（机顶盒上最近 14 天的「日期/区域」白名单）
- 连拍：拍完立即可以拍下一张，自动后台上传（流式上传，失败自动重试、可一键重传）
- 台数统计：本批拍了几张 / 已上传几张 / 该目录共几张
- 拍照尺寸可选：**4:3 与 16:9**，预览画面宽高比会跟随所选档位，**看到什么拍什么**（不会被裁切）
- 相机自愈：个别机型"同一台相机第二张必卡"，App 检测到 3 秒无响应会自动重启相机并重拍
- 运行日志：本地留存 + 自动上报机顶盒（`POST /applog`），排查问题时用

## 用法

1. 在 App 顶部把「服务器地址」改成自己的机顶盒地址（形如 `http://<机顶盒IP>:8090`，改一次会记住）
2. 点「刷新目录」→ 选目标文件夹
3. 对着设备按「拍照」；拍完直接换下一台

## 打包（无 Gradle）

依赖：JDK 17 + Android SDK（build-tools 34.0.0 / platform android-34）+ 一个签名 keystore。

```powershell
$env:PHOTOAPP_JDK      = "D:\Android\jdk-17"
$env:PHOTOAPP_SDK      = "D:\Android\sdk"
$env:PHOTOAPP_KEYSTORE = "D:\keys\my.keystore"
$env:PHOTOAPP_KS_PASS  = "你的keystore口令"
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

产物在 `outputs/`。把 APK 放到机顶盒 `/opt/scanner/app/` 下，手机访问
`http://<机顶盒IP>:8090/app.apk` 即可下载安装。

> 仓库里的源码不包含任何口令/密钥/真实内网地址，全部通过环境变量或 App 界面配置。
