"""提醒与通知测试。

覆盖：控制台 / CSV / Webhook 三种通道，以及各类提醒构造规则。
Webhook 使用本地 HTTP 服务模拟，不访问外部网络。
"""

from __future__ import annotations

import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List

import pytest

from backend.app.tracker.notifier import (
    LEVEL_DANGER,
    LEVEL_INFO,
    LEVEL_WARNING,
    Notification,
    Notifier,
    build_risk_notification,
    build_signal_notification,
    build_state_change_notification,
)


# ----------------------------------------------------------------------
# 模拟 Webhook 服务
# ----------------------------------------------------------------------
class _Recorder(BaseHTTPRequestHandler):
    """记录收到的 Webhook 请求。"""

    received: List[Dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}
        _Recorder.received.append({"path": self.path, "payload": payload})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"errcode":0,"errmsg":"ok"}')

    def log_message(self, *args):  # noqa: D102
        pass


@pytest.fixture()
def webhook_server():
    """启动本地 Webhook 模拟服务。"""
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/robot/send"
    server.shutdown()
    server.server_close()


# ----------------------------------------------------------------------
# 基本功能
# ----------------------------------------------------------------------
def test_notification_fields() -> None:
    """提醒对象字段与文本形式。"""
    n = Notification(
        title="买入信号", message="金叉", level=LEVEL_INFO,
        code="600000.SH", name="浦发银行", trade_date="2023-03-01",
    )
    assert n.level_icon == "[信息]"
    line = n.to_line()
    assert "600000.SH" in line
    assert "买入信号" in line
    assert n.to_dict()["code"] == "600000.SH"


def test_notifier_channels() -> None:
    """通道启用判断。"""
    assert Notifier({"console": True}).channels == ["console"]
    assert Notifier({"console": False}).channels == []
    n = Notifier({"console": False, "csv": True, "csv_path": "/tmp/x.csv"})
    assert "csv" in n.channels
    n2 = Notifier({"console": False, "webhook": "http://x"})
    assert "webhook" in n2.channels


def test_console_channel(capsys) -> None:
    """控制台输出。"""
    notifier = Notifier({"console": True, "csv": False})
    result = notifier.send(Notification(title="测试提醒", message="内容"))
    assert result["console"]["ok"] is True
    captured = capsys.readouterr()
    assert "测试提醒" in captured.out


def test_csv_channel(tmp_path: Path) -> None:
    """CSV 追加写入（含表头）。"""
    path = tmp_path / "alerts" / "alerts.csv"
    notifier = Notifier({"console": False, "csv": True, "csv_path": str(path)})

    notifier.send(Notification(title="信号一", message="a", code="600000.SH"))
    notifier.send(Notification(title="信号二", message="b", code="000001.SZ"))

    assert path.exists()
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.DictReader(fp))
    assert len(rows) == 2
    assert rows[0]["title"] == "信号一"
    assert rows[1]["code"] == "000001.SZ"


def test_csv_relative_path_resolution(tmp_path: Path) -> None:
    """相对路径以 base_dir 为基准。"""
    notifier = Notifier(
        {"console": False, "csv": True, "csv_path": "./reports/a.csv"},
        base_dir=tmp_path,
    )
    notifier.send(Notification(title="x"))
    assert (tmp_path / "reports" / "a.csv").exists()


# ----------------------------------------------------------------------
# Webhook
# ----------------------------------------------------------------------
def test_webhook_dingtalk(webhook_server: str) -> None:
    """钉钉 Webhook 格式。"""
    notifier = Notifier(
        {"console": False, "webhook": webhook_server, "webhook_type": "dingtalk"}
    )
    result = notifier.send(
        Notification(title="止损提醒", message="跌破止损价", level=LEVEL_DANGER,
                     code="600000.SH", name="浦发银行")
    )
    assert result["webhook"]["ok"] is True
    assert len(_Recorder.received) == 1
    payload = _Recorder.received[0]["payload"]
    assert payload["msgtype"] == "markdown"
    assert "止损提醒" in payload["markdown"]["title"]


def test_webhook_wecom(webhook_server: str) -> None:
    """企业微信 Webhook 格式。"""
    _Recorder.received = []
    notifier = Notifier(
        {"console": False, "webhook": webhook_server, "webhook_type": "wecom"}
    )
    notifier.send(Notification(title="买入信号", message="金叉"))
    payload = _Recorder.received[0]["payload"]
    assert payload["msgtype"] == "markdown"
    assert "买入信号" in payload["markdown"]["content"]


def test_webhook_feishu(webhook_server: str) -> None:
    """飞书 Webhook 格式。"""
    _Recorder.received = []
    notifier = Notifier(
        {"console": False, "webhook": webhook_server, "webhook_type": "feishu"}
    )
    notifier.send(Notification(title="风险提醒", message="放量下跌"))
    payload = _Recorder.received[0]["payload"]
    assert payload["msg_type"] == "text"
    assert "风险提醒" in payload["content"]["text"]


def test_webhook_dingtalk_sign(webhook_server: str) -> None:
    """配置 secret 时 URL 应带签名参数。"""
    _Recorder.received = []
    notifier = Notifier(
        {
            "console": False,
            "webhook": webhook_server,
            "webhook_type": "dingtalk",
            "webhook_secret": "SECabcdef123456",
        }
    )
    notifier.send(Notification(title="签名测试"))
    assert "timestamp=" in _Recorder.received[0]["path"]
    assert "sign=" in _Recorder.received[0]["path"]


def test_webhook_failure_is_isolated() -> None:
    """Webhook 失败不应抛异常。"""
    notifier = Notifier(
        {"console": False, "webhook": "http://127.0.0.1:1/not-exist", "webhook_type": "wecom"}
    )
    result = notifier.send(Notification(title="失败测试"))
    assert result["webhook"]["ok"] is False


def test_send_without_channels_is_safe() -> None:
    """未启用任何通道时不应报错。"""
    notifier = Notifier({"console": False, "csv": False})
    assert notifier.channels == []
    assert notifier.send(Notification(title="无通道")) == {}


def test_test_method() -> None:
    """测试通知接口。"""
    notifier = Notifier({"console": False})
    notifier.test()
    assert len(notifier.history) == 1
    assert notifier.history[0]["category"] == "test"


# ----------------------------------------------------------------------
# 提醒构造规则
# ----------------------------------------------------------------------
def test_state_change_levels() -> None:
    """状态变更提醒的级别规则。"""
    danger = build_state_change_notification(
        "600000.SH", "浦发银行", "持仓", "止损", "跌破止损价", "2023-03-01", 8.9, "ma_cross"
    )
    assert danger.level == LEVEL_DANGER
    assert "止损" in danger.title

    warning = build_state_change_notification(
        "600000.SH", "浦发银行", "持仓", "清仓", "卖出信号", "2023-03-01"
    )
    assert warning.level == LEVEL_WARNING

    info = build_state_change_notification(
        "600000.SH", "浦发银行", "观察", "买入", "金叉", "2023-03-01"
    )
    assert info.level == LEVEL_INFO


def test_signal_notification() -> None:
    """买入 / 卖出信号提醒。"""
    buy = build_signal_notification("600000.SH", "浦发银行", 1, "金叉", "2023-03-01", 10.2)
    assert buy.title == "买入信号"
    assert buy.category == "buy_signal"

    sell = build_signal_notification("600000.SH", "浦发银行", -1, "死叉", "2023-03-01")
    assert sell.title == "卖出信号"
    assert sell.level == LEVEL_WARNING


def test_risk_notification_titles() -> None:
    """风险提醒标题按类型区分。"""
    stop = build_risk_notification("600000.SH", "A", ["跌破止损价（9.00 ≤ 9.00）"], LEVEL_DANGER, "2023-03-01")
    assert stop.title == "止损触发"

    tp = build_risk_notification("600000.SH", "A", ["达到目标价（15.00 ≥ 15.00）"], LEVEL_INFO, "2023-03-01")
    assert tp.title == "止盈触发"

    vol = build_risk_notification("600000.SH", "A", ["放量下跌（量比 2.00）"], LEVEL_DANGER, "2023-03-01")
    assert vol.title == "放量下跌"

    ma = build_risk_notification("600000.SH", "A", ["跌破关键均线（10.00）"], LEVEL_WARNING, "2023-03-01")
    assert ma.title == "跌破关键均线"

    other = build_risk_notification("600000.SH", "A", ["浮亏较大"], LEVEL_WARNING, "2023-03-01")
    assert other.title == "风险提醒"
