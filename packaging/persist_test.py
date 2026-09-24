"""验证打包后计划/追踪真的不丢：写入 → 强杀 → 重启 → 复核。

强杀（taskkill /F，不触发优雅关闭）模拟用户直接关窗口 / 断电，
这正是 onefile 打包最容易出事的地方（数据若落在解包目录就会被一起删掉）。

用法：python packaging/persist_test.py <exe路径> [port]
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

EXE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
    Path(__file__).resolve().parent.parent / "dist" / "stock_picker.exe"
PORT = sys.argv[2] if len(sys.argv) > 2 else "8779"
BASE = f"http://127.0.0.1:{PORT}"
# 关键：用**独立的 HOME**，绝不动 dist/data 里用户真实的计划 / 追踪。
# 以前这里指向 EXE.parent，而第 5 步会「遍历接口返回的全部条目逐个删除」——
# 那会把用户自己建的记录一起删掉（只是当年跑的时候库里还没数据，才没出事）。
HOME = EXE.parent / "_persist_test_home"
DB = HOME / "data" / "stock_picker.db"
HOME.mkdir(parents=True, exist_ok=True)
ENV = {**os.environ, "STOCK_PICKER_HOME": str(HOME)}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def api(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def wait_up(secs=90):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        try:
            d = api("GET", "/api/status", timeout=5)
            if d.get("ready"):
                return time.perf_counter() - t0
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"服务 {secs}s 未就绪")


def wait_preload(secs=240):
    """等「启动即预加载」跑完 —— 冷启动（独立 HOME，无缓存）会重建一次缓存。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < secs:
        try:
            d = api("GET", "/api/bootstrap", timeout=10)
            if d.get("ready"):
                return time.perf_counter() - t0
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"预加载 {secs}s 未就绪")


def kill(port):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         errors="replace").stdout
    pids = set()
    for line in out.splitlines():
        s = line.strip()
        if f":{port}" in s and "LISTENING" in s.upper() and s.upper().startswith("TCP"):
            pid = s.split()[-1]
            if pid.isdigit() and pid != "0":
                pids.add(pid)
    for pid in sorted(pids):
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    return sorted(pids)


print(f"exe  : {EXE}")
print(f"数据 : {HOME}\\data  （独立 HOME，不碰 dist/data 里的真实数据）")
print(f"端口 : {PORT}\n")

# ---------- 启动 ----------
print("1) 启动 exe（--no-browser）")
kill(PORT)
proc = subprocess.Popen([str(EXE), "--port", PORT, "--no-browser"],
                        cwd=str(HOME), env=ENV,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
up = wait_up()
check("exe 启动并就绪", True, f"{up:.1f}s")
pl = wait_preload()
check("启动即预加载完成", True, f"{pl:.1f}s（冷启动，含重建缓存）")
check("数据目录落在 STOCK_PICKER_HOME", DB.exists(), str(DB))

# ---------- 写入 ----------
# 先记基线：库可能已有数据（比如冒烟测试留下的），断言用增量而不是绝对值
base = api("GET", "/api/stats")
b_plans, b_watch = base.get("plans", 0), base.get("watch", 0)
print(f"\n2) 写入计划 3 条 + 追踪 2 条（基线：plans={b_plans} watch={b_watch}）")
codes = [("600519.SH", "贵州茅台", 1500.0), ("000001.SZ", "平安银行", 11.5), ("601398.SH", "工商银行", 6.2)]
for i, (code, name, px) in enumerate(codes):
    api("POST", "/api/plans", {"code": code, "name": name, "signal_date": "2026-09-21",
                               "base_close": px, "volume": 100000, "ma_volume": 50000,
                               "vol_ratio": 2.0 + i * 0.1, "buy_price": px, "sell_price": px * 1.045,
                               "quantity": 100 * (i + 1), "status": "待买入", "note": f"persist-test-{i}"})
for code, name, px in codes[:2]:
    api("POST", "/api/watchlist", {"code": code, "name": name, "signal_date": "2026-09-21",
                                   "base_close": px, "buy_price": px, "sell_price": px * 1.045,
                                   "status": "关注中"})
st = api("GET", "/api/stats")
check("写入后统计（增量 +3 / +2）",
      st.get("plans") == b_plans + 3 and st.get("watch") == b_watch + 2,
      f"plans={st.get('plans')} watch={st.get('watch')}")


# ---------- 强杀 ----------
print("\n3) 强杀进程（taskkill /F，不走优雅关闭）")
pids = kill(PORT)
check("进程已强杀", bool(pids), f"killed pid={pids}")
try:
    api("GET", "/api/status", timeout=3)
    check("强杀后端口不再响应", False, "仍在响应")
except Exception:
    check("强杀后端口不再响应", True)
# WAL 未被 checkpoint 时，-wal 文件里应当还留着数据
wal = DB.with_name(DB.name + "-wal")
check("WAL 文件存在（未 checkpoint，数据在此）", wal.exists(),
      f"{wal.stat().st_size} bytes" if wal.exists() else "无")

# ---------- 重启复核 ----------
print("\n4) 重启后复核（WAL 恢复）")
proc2 = subprocess.Popen([str(EXE), "--port", PORT, "--no-browser"],
                         cwd=str(HOME), env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
wait_up()
wait_preload()
d = api("GET", "/api/plans")
plans = d.get("items") or []
d2 = api("GET", "/api/watchlist")
watch = d2.get("items") or []
check(f"计划条数保持 {b_plans + 3}", len(plans) == b_plans + 3, f"{len(plans)} 条")
check(f"追踪条数保持 {b_watch + 2}", len(watch) == b_watch + 2, f"{len(watch)} 条")
got = {p["code"]: p for p in plans}
check("计划代码/名称/价格逐条一致",
      all(got.get(c) and got[c]["name"] == n and abs(float(got[c]["buy_price"]) - px) < 1e-6
          for c, n, px in codes),
      str({c: (got.get(c) or {}).get("name") for c, _, _ in codes}))
check("备注/数量等字段未丢",
      all(got.get(c) and got[c].get("note") == f"persist-test-{i}"
          for i, (c, _, _) in enumerate(codes)),
      str([got.get(c, {}).get("note") for c, _, _ in codes]))

con = sqlite3.connect(DB)
ic = con.execute("PRAGMA integrity_check").fetchone()[0]
jm = con.execute("PRAGMA journal_mode").fetchone()[0]
n = con.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
con.close()
check("重启后 integrity_check=ok", ic.lower() == "ok", ic)
check("重启后 journal_mode=wal", jm.lower() == "wal", jm)
check(f"DB 文件内计划数 = {b_plans + 3}", n == b_plans + 3, str(n))

st = api("GET", "/api/storage")
check("启动时自动做了备份", (st.get("count") or 0) >= 1, f"{st.get('count')} 份快照")

# ---------- 清理 ----------
print("\n5) 清理测试数据")
for p in plans:
    api("DELETE", f"/api/plans/{p['id']}")
for w in watch:
    api("DELETE", f"/api/watchlist/{w['id']}")
st = api("GET", "/api/stats")
check("测试数据已清理（回到基线）",
      st.get("plans") == b_plans and st.get("watch") == b_watch,
      f"plans={st.get('plans')} watch={st.get('watch')}")

print(f"\n{'='*56}\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
# 收尾：停掉测试实例并删掉独立 HOME（省下 ~40MB 的缓存）
kill(PORT)
try:
    shutil.rmtree(HOME, ignore_errors=True)
    print(f"全部通过 ✓  （已停测试实例并清理 {HOME.name}/）")
except Exception as exc:
    print(f"全部通过 ✓  （测试实例已停；{HOME.name}/ 清理失败：{exc}）")
