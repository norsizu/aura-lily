"""可选的 mDNS 服务广播（_aura-lily._tcp），供 ESP32 固件在局域网自动发现网关。

默认关闭。Docker Desktop for Mac 的容器发不出局域网组播，应改用宿主机
`tools/macos/install_mdns_service.sh`（dns-sd + launchd）代播；Linux 原生部署
可设置 AURA_MDNS_ADVERTISE_ENABLED=1，由本模块通过 python-zeroconf 广播。
zeroconf 是可选依赖：缺失或广播失败时静默降级，不影响网关主流程。
"""
from __future__ import annotations

import os
import socket
import sys
from typing import Any

MDNS_SERVICE_TYPE = "_aura-lily._tcp.local."
DEFAULT_INSTANCE_NAME = "aura-lily"


def _log(message: str) -> None:
    print(f"aura-lily-gateway mdns: {message}", file=sys.stderr, flush=True)


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name, "") or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _pick_lan_ip() -> str:
    """探测本机对外局域网 IP（UDP connect 不会真的发包）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


class MdnsAdvertiser:
    """持有 zeroconf 注册句柄，close() 时注销广播。"""

    def __init__(self, zeroconf_instance: Any, service_info: Any) -> None:
        self._zeroconf = zeroconf_instance
        self._service_info = service_info

    def close(self) -> None:
        try:
            self._zeroconf.unregister_service(self._service_info)
        except Exception:  # pragma: no cover - 注销失败无需影响退出
            pass
        try:
            self._zeroconf.close()
        except Exception:  # pragma: no cover
            pass


def maybe_start_mdns_advertise(port: int) -> MdnsAdvertiser | None:
    """按 AURA_MDNS_ADVERTISE_ENABLED 决定是否广播；任何失败都静默跳过。"""
    if not _env_bool("AURA_MDNS_ADVERTISE_ENABLED", False):
        return None
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        _log("skipped: python-zeroconf not installed (pip install zeroconf)")
        return None

    lan_ip = _pick_lan_ip()
    if not lan_ip or lan_ip.startswith("127."):
        _log(f"skipped: no usable LAN address (got {lan_ip or 'none'})")
        return None

    instance = str(os.environ.get("AURA_MDNS_INSTANCE_NAME", "") or "").strip() or DEFAULT_INSTANCE_NAME
    hostname = socket.gethostname().split(".", 1)[0] or "aura-lily-host"
    try:
        info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            f"{instance}.{MDNS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(lan_ip)],
            port=int(port),
            properties={"path": "/ws"},
            server=f"{hostname}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
    except Exception as exc:
        _log(f"skipped: register failed ({exc.__class__.__name__}: {exc})")
        return None
    _log(f"advertising {instance} at {lan_ip}:{port} ({MDNS_SERVICE_TYPE})")
    return MdnsAdvertiser(zc, info)
