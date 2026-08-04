#!/usr/bin/env bash
# 安装 Aura Lily 的 mDNS 广播 launchd 服务（macOS 专用）。
# 作用：广播 _aura-lily._tcp:8787，让 ESP32 固件在任意局域网自动发现网关。
# 用法：bash tools/macos/install_mdns_service.sh [--uninstall]
set -euo pipefail

LABEL="com.aura-lily.mdns"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="${SCRIPT_DIR}/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
    rm -f "${PLIST_DST}"
    echo "已卸载 ${LABEL}"
    exit 0
fi

mkdir -p "${HOME}/Library/LaunchAgents"
cp "${PLIST_SRC}" "${PLIST_DST}"
# 重复安装时先卸载旧实例，避免 bootstrap 冲突。
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
launchctl kickstart -k "${DOMAIN}/${LABEL}"
echo "已安装并启动 ${LABEL}（日志：/tmp/aura-lily-mdns.log）"
echo "验证：dns-sd -B _aura-lily._tcp local."
