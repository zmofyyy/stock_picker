"""结束占用指定端口的进程（本机 netstat 输出不稳定，统一用 Python 处理）。"""

import subprocess
import sys

port = sys.argv[1]
out = subprocess.run(
    ["netstat", "-ano"], capture_output=True, text=True, errors="replace"
).stdout

pids = set()
for line in out.splitlines():
    s = line.strip()
    if f":{port}" not in s:
        continue
    if "LISTENING" not in s.upper():
        continue
    if not s.upper().startswith("TCP"):
        continue
    pid = s.split()[-1]
    if pid.isdigit() and pid != "0":
        pids.add(pid)

for pid in sorted(pids):
    r = subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True,
                       errors="replace")
    print(f"kill {pid}: rc={r.returncode} {r.stdout.strip() or r.stderr.strip()}")

print("killed:", sorted(pids) or "（无占用）")
