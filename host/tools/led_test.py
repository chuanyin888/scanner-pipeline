"""在机顶盒上直接驱动 ESP32 灯做回归测试：老状态 + 新 REGION 效果."""
import subprocess
import threading
import time

DEV = "/dev/ttyACM0"
SEQ = [("IDLE", 2), ("BUSY", 2), ("OK", 3), ("WARN", 2), ("ERROR", 2), ("REGION", 7), ("IDLE", 2)]

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
            text = line.decode("utf-8", "replace").strip()
            print(f"[{time.time() - t0:5.2f}s] {text}", flush=True)


threading.Thread(target=reader, daemon=True).start()
for cmd, wait in SEQ:
    print(f"--- {cmd} ---", flush=True)
    wf.write((cmd + "\n").encode())
    time.sleep(wait)

stop = True
time.sleep(0.5)
rf.close()
wf.close()
print("测试结束")
