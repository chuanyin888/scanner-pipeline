"""验证"CANCEL（取消区域）"灯效：白闪 3 下后回红灯."""
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
wf.write(b"CANCEL\n")
time.sleep(4)
stop = True
time.sleep(0.4)
rf.close()
wf.close()
print("完成（应看到 白->灭 三组，然后红灯）")
