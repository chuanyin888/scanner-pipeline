# Photo import app - Android build script (no Gradle)
# Usage: powershell -ExecutionPolicy Bypass -File .\build.ps1
#
# 依赖通过环境变量（或同名参数）传入，避免把本机路径/口令写进仓库：
#   PHOTOAPP_JDK       JDK 目录（含 bin\java.exe）
#   PHOTOAPP_SDK       Android SDK 目录（需含 build-tools\34.0.0 与 platforms\android-34）
#   PHOTOAPP_KEYSTORE  签名 keystore 路径
#   PHOTOAPP_KS_PASS   keystore 口令
#   PHOTOAPP_KS_ALIAS  keystore 别名（默认 shipout）

param(
    [string]$OutDir = "",
    [string]$Jdk = $env:PHOTOAPP_JDK,
    [string]$Sdk = $env:PHOTOAPP_SDK,
    [string]$Keystore = $env:PHOTOAPP_KEYSTORE,
    [string]$KsPass = $env:PHOTOAPP_KS_PASS,
    [string]$KsAlias = $(if ($env:PHOTOAPP_KS_ALIAS) { $env:PHOTOAPP_KS_ALIAS } else { "shipout" })
)

$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot
$App       = Join-Path $Root 'app'
$Build     = Join-Path $Root 'build'

if (-not $Jdk -or -not (Test-Path "$Jdk\bin\java.exe")) {
    throw "缺少 JDK：请设置环境变量 PHOTOAPP_JDK 或用 -Jdk 指定（当前值: '$Jdk'）"
}
if (-not $Sdk -or -not (Test-Path $Sdk)) {
    throw "缺少 Android SDK：请设置环境变量 PHOTOAPP_SDK 或用 -Sdk 指定（当前值: '$Sdk'）"
}
$BuildTool = Join-Path $Sdk 'build-tools\34.0.0'
$Platform  = Join-Path $Sdk 'platforms\android-34\android.jar'

if (-not $OutDir) { $OutDir = Join-Path $Root 'outputs' }

if (-not (Test-Path $Platform)) { throw "missing android.jar: $Platform" }
$env:JAVA_HOME = $Jdk
$env:PATH = "$Jdk\bin;$env:PATH"

Write-Host "== 1/7 prepare" -ForegroundColor Cyan
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force -Path $Build, "$Build\gen", "$Build\classes", "$Build\dex", "$Build\assets" | Out-Null
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$sources = Get-ChildItem -Path "$App\java" -Recurse -Filter *.java | ForEach-Object { $_.FullName }

robocopy (Join-Path $App 'res') (Join-Path $Build 'res') /E /NFL /NDL /NJH /NJS /NP | Out-Null
if (Test-Path (Join-Path $App 'assets')) {
    robocopy (Join-Path $App 'assets') "$Build\assets" /E /NFL /NDL /NJH /NJS /NP | Out-Null
}

Write-Host "== 2/7 compile resources (aapt2)" -ForegroundColor Cyan
& "$BuildTool\aapt2.exe" compile --dir "$Build\res" -o "$Build\res.zip"
if ($LASTEXITCODE -ne 0) { throw 'aapt2 compile failed' }

$linkArgs = @(
    'link', '-o', "$Build\base.apk",
    '-I', $Platform,
    '--manifest', "$App\AndroidManifest.xml",
    '-R', "$Build\res.zip",
    '--java', "$Build\gen",
    '--min-sdk-version', '24',
    '--target-sdk-version', '34',
    '--version-code', '12',
    '--version-name', '2.1',
    '-A', "$Build\assets"
)
& "$BuildTool\aapt2.exe" @linkArgs
if ($LASTEXITCODE -ne 0) { throw 'aapt2 link failed' }

Write-Host "== 3/7 compile java" -ForegroundColor Cyan
& "$Jdk\bin\javac.exe" -encoding UTF-8 -nowarn -cp $Platform -d "$Build\classes" $sources
if ($LASTEXITCODE -ne 0) { throw 'javac failed' }

Write-Host "== 4/7 dex (d8)" -ForegroundColor Cyan
& "$Jdk\bin\jar.exe" cf "$Build\classes.jar" -C "$Build\classes" .
if ($LASTEXITCODE -ne 0) { throw 'jar failed' }
& "$BuildTool\d8.bat" --release --min-api 24 --lib $Platform --output "$Build\dex" "$Build\classes.jar"
if ($LASTEXITCODE -ne 0) { throw 'd8 failed' }

Write-Host "== 5/7 package apk" -ForegroundColor Cyan
Copy-Item "$Build\base.apk" "$Build\unsigned.apk" -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open("$Build\unsigned.apk", 'Update')
try {
    Get-ChildItem "$Build\dex\*.dex" | ForEach-Object {
        $entry = $zip.CreateEntry($_.Name, [System.IO.Compression.CompressionLevel]::NoCompression)
        $out = $entry.Open()
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
        $out.Write($bytes, 0, $bytes.Length)
        $out.Close()
    }
} finally {
    $zip.Dispose()
}

Write-Host "== 6/7 sign" -ForegroundColor Cyan
if (-not $Keystore -or -not (Test-Path $Keystore)) {
    throw "缺少 keystore：请设置环境变量 PHOTOAPP_KEYSTORE 或用 -Keystore 指定（当前值: '$Keystore'）"
}
if (-not $KsPass) { throw "缺少签名口令：请设置环境变量 PHOTOAPP_KS_PASS 或用 -KsPass 指定" }
& "$BuildTool\zipalign.exe" -f -p 4 "$Build\unsigned.apk" "$Build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'zipalign failed' }

$apkName = [string]([char]0x5149) + [char]0x732B + [char]0x8865 + [char]0x5F55 + '_v2.1.apk'
$apkPath = Join-Path $OutDir $apkName
& "$BuildTool\apksigner.bat" sign --ks $Keystore --ks-key-alias $KsAlias --ks-pass "pass:$KsPass" --key-pass "pass:$KsPass" --out $apkPath "$Build\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw 'apksigner failed' }

Write-Host "== 7/7 verify" -ForegroundColor Cyan
& "$BuildTool\apksigner.bat" verify --print-certs $apkPath | Select-Object -First 2
$size = [math]::Round((Get-Item $apkPath).Length / 1MB, 2)
$sha = (Get-FileHash $apkPath -Algorithm SHA256).Hash
Write-Host ""
Write-Host "APK: $apkPath ($size MB)" -ForegroundColor Green
Write-Host "SHA256: $sha" -ForegroundColor Green
