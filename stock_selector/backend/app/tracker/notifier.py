"""提醒与通知模块。

支持四种通道：
    * **控制台**：直接打印到标准输出（带颜色无关的纯文本格式）；
    * **CSV**：追加写入本地文件，便于 Excel 跟踪；
    * **Webhook**：钉钉 / 企业微信 / 飞书群机器人；
    * **邮件**：SMTP + SSL（可选）。

所有通道都是「尽力而为」：单个通道失败不会影响其它通道，也不会中断追踪流程。
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import smtplib
import time
import urllib.parse
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core.logging import get_logger

logger = get_logger("notifier")

# 提醒级别
LEVEL_INFO = "info"
LEVEL_WARNING = "warning"
LEVEL_DANGER = "danger"

LEVEL_ICON = {LEVEL_INFO: "[信息]", LEVEL_WARNING: "[警示]", LEVEL_DANGER: "[危险]"}

# CSV 表头
CSV_FIELDS = [
    "created_at", "level", "category", "code", "name",
    "title", "message", "trade_date", "trigger_value",
]


@dataclass
class Notification:
    """一条待发送的提醒。"""

    title: str
    message: str = ""
    level: str = LEVEL_INFO
    category: str = "state_change"
    code: str = ""
    name: str = ""
    trade_date: Optional[str] = None
    trigger_value: Optional[float] = None
    strategy: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def level_icon(self) -> str:
        """级别图标文本。"""
        return LEVEL_ICON.get(self.level, "[信息]")

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "title": self.title,
            "message": self.message,
            "level": self.level,
            "category": self.category,
            "code": self.code,
            "name": self.name,
            "trade_date": self.trade_date,
            "trigger_value": self.trigger_value,
            "strategy": self.strategy,
        }

    def to_line(self) -> str:
        """单行文本形式，便于邮件 / 控制台。"""
        parts = [self.level_icon]
        if self.code:
            parts.append(f"{self.code} {self.name}".strip())
        parts.append(self.title)
        if self.message:
            parts.append(f"- {self.message}")
        return " ".join(parts)


class Notifier:
    """多渠道提醒发送器。

    :param config: ``config.yaml`` 中 ``tracker.notify`` 段
    :param base_dir: 相对路径（csv_path）的解析基准目录
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, base_dir: Optional[Path] = None) -> None:
        cfg = dict(config or {})
        self.console: bool = bool(cfg.get("console", True))
        self.csv_enabled: bool = bool(cfg.get("csv", False))
        self.csv_path: Optional[Path] = None
        if cfg.get("csv_path"):
            p = Path(str(cfg["csv_path"]))
            if not p.is_absolute() and base_dir is not None:
                p = (Path(base_dir) / p).resolve()
            self.csv_path = p

        self.webhook: str = str(cfg.get("webhook", "") or "")
        self.webhook_type: str = str(cfg.get("webhook_type", "dingtalk") or "dingtalk").lower()
        self.webhook_secret: str = str(cfg.get("webhook_secret", "") or "")

        self.email: str = str(cfg.get("email", "") or "")
        self.smtp_host: str = str(cfg.get("smtp_host", "") or "")
        self.smtp_port: int = int(cfg.get("smtp_port", 465) or 465)
        self.smtp_user: str = str(cfg.get("smtp_user", "") or "")
        self.smtp_password: str = str(cfg.get("smtp_password", "") or "")
        self.smtp_ssl: bool = bool(cfg.get("smtp_ssl", True))

        self.history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    @property
    def channels(self) -> List[str]:
        """返回已启用的通道列表。"""
        active = []
        if self.console:
            active.append("console")
        if self.csv_enabled and self.csv_path:
            active.append("csv")
        if self.webhook:
            active.append("webhook")
        if self.email and self.smtp_host:
            active.append("email")
        return active

    # ------------------------------------------------------------------
    def send(self, notification: Notification) -> Dict[str, Any]:
        """发送单条提醒，返回各通道结果。"""
        results: Dict[str, Any] = {}
        record = notification.to_dict()
        record["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if self.console:
            try:
                print(notification.to_line())
                results["console"] = {"ok": True, "detail": ""}
            except Exception as exc:  # pragma: no cover
                results["console"] = {"ok": False, "detail": str(exc)}

        if self.csv_enabled and self.csv_path:
            results["csv"] = self._write_csv(record)

        if self.webhook:
            results["webhook"] = self._send_webhook(notification)

        if self.email and self.smtp_host:
            results["email"] = self._send_email(notification)

        record["delivered"] = results
        self.history.append(record)
        return results

    def send_many(self, notifications: Sequence[Notification]) -> List[Dict[str, Any]]:
        """批量发送。"""
        return [self.send(n) for n in notifications]

    # ------------------------------------------------------------------
    def _write_csv(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """追加写入 CSV。"""
        try:
            assert self.csv_path is not None
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            exists = self.csv_path.exists() and self.csv_path.stat().st_size > 0
            with open(self.csv_path, "a", encoding="utf-8-sig", newline="") as fp:
                writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if not exists:
                    writer.writeheader()
                writer.writerow({k: record.get(k, "") for k in CSV_FIELDS})
            return {"ok": True, "detail": str(self.csv_path)}
        except Exception as exc:
            logger.warning("提醒写入 CSV 失败：%s", exc)
            return {"ok": False, "detail": str(exc)}

    # ------------------------------------------------------------------
    def _build_webhook_request(self, n: Notification):
        """构造 Webhook 请求的 (url, payload)。"""
        text = n.to_line()
        markdown = f"### {n.level_icon} {n.title}\n\n{n.message}\n\n"
        if n.code:
            markdown += f"- 股票：**{n.code} {n.name}**\n"
        if n.trade_date:
            markdown += f"- 日期：{n.trade_date}\n"
        if n.strategy:
            markdown += f"- 策略：{n.strategy}\n"

        url = self.webhook
        if self.webhook_type == "dingtalk":
            if self.webhook_secret:
                timestamp = str(round(time.time() * 1000))
                to_sign = f"{timestamp}\n{self.webhook_secret}"
                digest = hmac.new(
                    self.webhook_secret.encode("utf-8"),
                    to_sign.encode("utf-8"),
                    digestmod=hashlib.sha256,
                ).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}timestamp={timestamp}&sign={sign}"
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": n.title, "text": markdown},
            }
        elif self.webhook_type == "wecom":
            payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
        elif self.webhook_type == "feishu":
            if self.webhook_secret:
                timestamp = str(int(time.time()))
                string_to_sign = f"{timestamp}\n{self.webhook_secret}"
                signature = base64.b64encode(
                    hmac.new(
                        string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256
                    ).digest()
                ).decode("utf-8")
                payload = {
                    "timestamp": timestamp,
                    "sign": signature,
                    "msg_type": "text",
                    "content": {"text": text},
                }
            else:
                payload = {"msg_type": "text", "content": {"text": text}}
        else:
            payload = {"title": n.title, "text": text, "level": n.level, "code": n.code}
        return url, payload

    def _send_webhook(self, n: Notification) -> Dict[str, Any]:
        """发送 Webhook。"""
        try:
            import httpx

            url, payload = self._build_webhook_request(n)
            resp = httpx.post(url, json=payload, timeout=8.0)
            ok = resp.status_code < 300
            detail = f"HTTP {resp.status_code}"
            if not ok:
                detail += f" {resp.text[:200]}"
            else:
                # 钉钉/企业微信业务错误码在 body 中
                try:
                    body = resp.json()
                    if isinstance(body, dict) and body.get("errcode") not in (None, 0):
                        ok = False
                        detail = f"errcode={body.get('errcode')} {body.get('errmsg', '')}"
                except Exception:
                    pass
            if not ok:
                logger.warning("Webhook 发送失败：%s", detail)
            return {"ok": ok, "detail": detail}
        except Exception as exc:
            logger.warning("Webhook 发送异常：%s", exc)
            return {"ok": False, "detail": str(exc)}

    # ------------------------------------------------------------------
    def _send_email(self, n: Notification) -> Dict[str, Any]:
        """发送邮件。"""
        try:
            recipients = [e.strip() for e in self.email.split(",") if e.strip()]
            if not recipients:
                return {"ok": False, "detail": "未配置收件人"}
            msg = MIMEText(n.message or n.title, "plain", "utf-8")
            msg["Subject"] = Header(f"{n.level_icon} {n.title}", "utf-8")
            msg["From"] = self.smtp_user or "stock_selector"
            msg["To"] = ", ".join(recipients)

            if self.smtp_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
                server.starttls()
            try:
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.smtp_user or "stock_selector", recipients, msg.as_string())
            finally:
                server.quit()
            return {"ok": True, "detail": ",".join(recipients)}
        except Exception as exc:
            logger.warning("邮件发送失败：%s", exc)
            return {"ok": False, "detail": str(exc)}

    # ------------------------------------------------------------------
    def test(self) -> Dict[str, Any]:
        """发送一条测试提醒，用于验证配置。"""
        n = Notification(
            title="stock_selector 通知测试",
            message=f"当前启用通道：{', '.join(self.channels) or '无'}",
            level=LEVEL_INFO,
            category="test",
            trade_date=time.strftime("%Y-%m-%d"),
        )
        return self.send(n)


# ----------------------------------------------------------------------
# 提醒规则
# ----------------------------------------------------------------------
def build_state_change_notification(
    code: str,
    name: str,
    old_state: str,
    new_state: str,
    reason: str,
    trade_date: Optional[str],
    price: Optional[float] = None,
    strategy: str = "",
) -> Notification:
    """构造「状态变更」提醒。"""
    danger_states = {"止损", "失效"}
    warning_states = {"减仓", "清仓", "止盈"}
    if new_state in danger_states:
        level = LEVEL_DANGER
    elif new_state in warning_states:
        level = LEVEL_WARNING
    else:
        level = LEVEL_INFO
    return Notification(
        title=f"状态变更：{old_state} → {new_state}",
        message=f"{reason}；最新价 {price:.2f}" if price else reason,
        level=level,
        category="state_change",
        code=code,
        name=name,
        trade_date=trade_date,
        trigger_value=price,
        strategy=strategy,
    )


def build_signal_notification(
    code: str,
    name: str,
    signal: int,
    reason: str,
    trade_date: Optional[str],
    price: Optional[float] = None,
    strategy: str = "",
) -> Notification:
    """构造「策略信号」提醒。"""
    if signal > 0:
        title = "买入信号"
        level = LEVEL_INFO
    else:
        title = "卖出信号"
        level = LEVEL_WARNING
    return Notification(
        title=title,
        message=f"{reason}；最新价 {price:.2f}" if price else reason,
        level=level,
        category="buy_signal" if signal > 0 else "sell_signal",
        code=code,
        name=name,
        trade_date=trade_date,
        trigger_value=price,
        strategy=strategy,
    )


def build_risk_notification(
    code: str,
    name: str,
    flags: Sequence[str],
    level: str,
    trade_date: Optional[str],
    price: Optional[float] = None,
    strategy: str = "",
) -> Notification:
    """构造「风险提醒」。"""
    title = "风险提醒"
    if any("止损" in f for f in flags):
        title = "止损触发"
    elif any("目标价" in f for f in flags):
        title = "止盈触发"
    elif any("放量下跌" in f for f in flags):
        title = "放量下跌"
    elif any("均线" in f for f in flags):
        title = "跌破关键均线"
    return Notification(
        title=title,
        message="；".join(flags),
        level=level,
        category="risk",
        code=code,
        name=name,
        trade_date=trade_date,
        trigger_value=price,
        strategy=strategy,
    )
