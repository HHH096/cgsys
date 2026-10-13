#!/bin/bash
# 看一眼临时隧道的网址。
# 临时隧道在服务器重启后网址会变，用这个重新查一次。

if grep -q $'\r' "$0" 2>/dev/null; then
    sed -i 's/\r$//' "$0"
    exec bash "$0" "$@"
fi

URL=$(journalctl -u cgsys-tunnel -n 300 --no-pager 2>/dev/null \
      | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)

if [ -n "$URL" ]; then
    echo "============================================================"
    echo "  组员用这个网址：$URL"
    echo "============================================================"
else
    echo "没找到网址。可能隧道没在跑，检查一下："
    echo "  systemctl status cgsys-tunnel"
    echo "  journalctl -u cgsys-tunnel -n 50 --no-pager"
    echo
    echo "（如果你装的是固定隧道，服务名是 cloudflared，网址就是你配的那个域名）"
fi
