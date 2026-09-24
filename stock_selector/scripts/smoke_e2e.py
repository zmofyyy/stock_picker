"""端到端冒烟测试：真实启动后端服务，逐个请求前端页面与 API。

用法::

    python scripts/smoke_e2e.py [--config config.yaml]

脚本会：
1. 用随机空闲端口启动 ``cli.py serve``；
2. 请求首页、SPA 路由、静态资源与全部主要 API；
3. 打印 ``状态码 / 路径 / 摘要`` 表格与后端错误行；
4. 关闭服务并以退出码反映结果（0 = 全部通过）。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    """获取一个空闲端口。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LineDrain(threading.Thread):
    """持续读取子进程输出，避免管道写满导致子进程阻塞（死锁）。

    使用 ``subprocess.PIPE`` 时不消费输出，一旦缓冲区写满，
    子进程（uvicorn）会卡在写日志上，外部却表现为「请求无响应」。
    """

    def __init__(self, stream: Any) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self.lines: List[str] = []

    def run(self) -> None:  # pragma: no cover - 线程主体
        try:
            for line in self._stream:
                self.lines.append(line)
        except Exception:
            pass

    def stop(self) -> List[str]:
        """等待线程结束并返回已收集的输出行。"""
        self.join(timeout=5)
        return list(self.lines)


# 待验证的请求：(方法, 路径, 请求体, 期望内容类型)
CHECKS: List[Tuple[str, str, Optional[Dict[str, Any]], str]] = [
    ("GET", "/", None, "html"),
    ("GET", "/Tracker", None, "html"),  # SPA 路由回退
    ("GET", "/api/health", None, "json"),
    ("GET", "/api", None, "json"),
    ("GET", "/api/data/status", None, "json"),
    ("GET", "/api/data/strategies", None, "json"),
    ("GET", "/api/data/stocks?limit=5", None, "json"),
    ("GET", "/api/data/cache", None, "json"),
    ("GET", "/api/data/basics", None, "json"),
    ("POST", "/api/data/basics/template", None, "json"),
    ("GET", "/api/screen", None, "json"),
    ("POST", "/api/screen/run", {"strategy": "ma_cross", "date": "2024-07-15", "limit": 10}, "json"),
    ("POST", "/api/screen/run", {"strategy": "rsi", "date": "2024-07-15", "limit": 10}, "json"),
    ("POST", "/api/screen/run", {"strategy": "volume_breakout", "date": "2024-07-15", "limit": 10}, "json"),
    ("POST", "/api/screen/run", {"strategy": "volume_surge", "date": "2024-07-05", "limit_universe": 200}, "json"),
    ("GET", "/api/backtest/config", None, "json"),
    ("POST", "/api/backtest/run", {"strategy": "ma_cross", "start": "2023-06-01", "end": "2024-07-15", "initial_cash": 1000000}, "json"),
    ("POST", "/api/tracker/update", {"date": "2024-07-15"}, "json"),
    ("GET", "/api/tracker/watchlist", None, "json"),
    ("GET", "/api/tracker/dashboard", None, "json"),
    ("GET", "/api/tracker/states", None, "json"),
    ("GET", "/api/tracker/groups", None, "json"),
    ("GET", "/api/tracker/storage", None, "json"),
    ("GET", "/api/tracker/alerts", None, "json"),
    ("GET", "/api/tracker/signals", None, "json"),
    ("GET", "/api/tracker/report?kind=summary", None, "json"),
    ("GET", "/api/report/summary", None, "json"),
    ("GET", "/api/report/daily", None, "json"),
    ("GET", "/api/settings", None, "json"),
    ("GET", "/api/settings/scheduler", None, "json"),
    ("POST", "/api/tracker/replay", {"start": "2024-07-01", "end": "2024-07-15"}, "json"),
]


def main(argv: Optional[List[str]] = None) -> int:
    """执行端到端冒烟。"""
    parser = argparse.ArgumentParser(description="stock_selector 端到端冒烟测试")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--keep", action="store_true", help="测试完成后不关闭服务")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if args.config:
        env["STOCK_SELECTOR_CONFIG"] = str(Path(args.config).resolve())

    port = free_port()
    # 注意：--config 是全局参数，必须放在子命令（serve）之前
    cmd = [sys.executable, "-u", "cli.py"]
    if args.config:
        cmd += ["--config", str(Path(args.config).resolve())]
    cmd += ["serve", "--host", args.host, "--port", str(port)]

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    drain = _LineDrain(proc.stdout)
    drain.start()
    base = f"http://{args.host}:{port}"

    up = False
    for _ in range(120):
        if proc.poll() is not None:
            break
        try:
            httpx.get(base + "/api/health", timeout=1.5)
            up = True
            break
        except Exception:
            time.sleep(0.5)

    print("=" * 78)
    print(f"  stock_selector 端到端冒烟测试   {base}")
    print("=" * 78)
    if not up:
        print("[失败] 服务未能启动，输出如下：")
        proc.kill()
        for line in drain.stop()[-40:]:
            print("   ", line.rstrip())
        return 1

    failures: List[str] = []
    for method, path, body, kind in CHECKS:
        try:
            resp = httpx.request(method, base + path, json=body, timeout=300.0)
        except Exception as exc:  # pragma: no cover
            failures.append(f"{method} {path}")
            print(f"ERR --- {method:4} {path:<40} {type(exc).__name__}: {exc}")
            continue

        summary = ""
        if kind == "json":
            try:
                data = resp.json()
                if isinstance(data, dict):
                    summary = f"ok={data.get('ok')}"
                    inner = data.get("data")
                    if isinstance(inner, list):
                        summary += f"  items={len(inner)}"
                    elif isinstance(inner, dict):
                        summary += f"  keys={len(inner)}"
                else:
                    summary = f"items={len(data)}"
            except Exception:
                summary = resp.text[:60]
        else:
            summary = "SPA index.html" if "id=\"root\"" in resp.text or "<script" in resp.text else resp.text[:60]

        ok = resp.status_code < 400
        if not ok:
            failures.append(f"{method} {path}")
        print(f"{'OK ' if ok else 'BAD'} {resp.status_code:>3} {method:4} {path:<40} {summary}")
        if not ok:
            print(f"      -> {resp.text[:300]}")

    # 首页引用的静态资源
    try:
        html = httpx.get(base + "/", timeout=15).text
        for asset in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)[:5]:
            rr = httpx.get(base + asset, timeout=60)
            ok = rr.status_code == 200
            if not ok:
                failures.append(f"STATIC {asset}")
            print(f"{'OK ' if ok else 'BAD'} {rr.status_code:>3} GET  {asset:<40} {len(rr.content)} bytes")
    except Exception as exc:  # pragma: no cover
        print("静态资源检查失败：", exc)

    if not args.keep:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except Exception:  # pragma: no cover
        proc.kill()
    out_lines = drain.stop()

    errs = [
        l.rstrip()
        for l in out_lines
        if " ERROR " in l or "Traceback" in l or "Exception" in l
    ]
    print("-" * 78)
    print("后端错误日志：", "无" if not errs else "")
    for line in errs[-12:]:
        print("  ", line)

    print("=" * 78)
    if failures:
        print(f"结果：{len(failures)} 项失败 -> " + ", ".join(failures))
        return 1
    print(f"结果：{len(CHECKS)} 项 API + 静态资源全部通过")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
