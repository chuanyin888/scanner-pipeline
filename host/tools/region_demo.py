"""演示"区域卡切换成功"灯效：红->绿->蓝 5 秒 + IO2 常亮，演两遍."""
import subprocess
import threading
import time

DEV = "/dev/ttyACM0"
subprocess.run(
    ["stty", "-F", DEV, "115200", "cs8", "-cstopb", "-parenb", "-crtscts", "raw", "-echo"],
    check=True,
)
rf = open(DEV, "rb", buffering=0)
wf = open(DEV, "wb", buffering=0)
t0 = time.time()
stop = False


def reader():
    while not stop:
        line = rf.readline()
        if line:
            print(f"[{time.time() - t0:5.2f}s] {line.decode('utf-8', 'replace').strip()}", flush=True)


threading.Thread(target=reader, daemon=True).start()
for i in (1, 2):
    print(f"===== 第 {i} 遍：区域卡切换成功 =====", flush=True)
    wf.write(b"REGION\n")
    time.sleep(6.5)

stop = True
time.sleep(0.4)
rf.close()
wf.close()
print("演示结束（灯应停在红色待命）")
