#!/bin/bash
# 采购系统 · 配置 Cloudflare 隧道（在这台服务器上跑）
#
# 目的：让不在同一个网的组员也能访问。
# 隧道是**主动往外连**的，所以这台服务器**不需要公网 IP**。
#
# 两种用法：
#
#   ① 临时隧道（先试试能不能通）
#        bash 配置隧道.sh
#      会打印一个 https://xxx.trycloudflare.com 网址。
#      ⚠️ 服务器重启后这个网址会变，要重新看。
#
#   ② 固定隧道（长期用，网址不变）
#      先去 Cloudflare 控制台建隧道、拿到 token（步骤见《迁移到云服务器.md》），然后：
#        bash 配置隧道.sh <那串token>

# Windows 上传过来可能带 CRLF，先自愈
if grep -q $'\r' "$0" 2>/dev/null; then
    sed -i 's/\r$//' "$0"
    exec bash "$0" "$@"
fi

set -e

SERVICE=cgsys-tunnel
CF=/usr/local/bin/cloudflared
PORT=8000
TOKEN="$1"

say() { echo; echo "==> $*"; }

# ---------------------------------------------------------------- 0. 前置检查
if ! systemctl is-active --quiet cgsys; then
    echo "⚠️ 采购系统（cgsys 服务）好像没在跑。先跑 部署到服务器.sh，或者 systemctl start cgsys"
fi

# ---------------------------------------------------------------- 1. cloudflared
say "1/3 准备 cloudflared"
if [ -x "$CF" ]; then
    echo "已经有了：$($CF --version 2>/dev/null | head -1)"
else
    case "$(uname -m)" in
        x86_64)        CF_ARCH=amd64 ;;
        aarch64|arm64) CF_ARCH=arm64 ;;
        *) echo "不认识的 CPU 架构：$(uname -m)"; exit 1 ;;
    esac
    URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
    echo "下载 $URL"
    if ! curl -fL --retry 3 -o "$CF" "$URL"; then
        echo
        echo "下载失败。可以手动下这个文件："
        echo "  $URL"
        echo "放到 /usr/local/bin/cloudflared ，然后 chmod +x 它，再重跑本脚本。"
        exit 1
    fi
    chmod +x "$CF"
fi

# ---------------------------------------------------------------- 2. 装服务
if [ -n "$TOKEN" ]; then
    say "2/3 装【固定隧道】服务（网址不会变）"
    # cloudflared 自带这个子命令：直接把 token 装成 systemd 服务
    "$CF" service install "$TOKEN"
    systemctl enable --now cloudflared
    echo
    echo "✅ 固定隧道服务已装好（服务名 cloudflared）"
    echo "   网址就是你刚才在 Cloudflare 里配的那个域名。"
    echo "   看日志：journalctl -u cloudflared -f"
else
    say "2/3 装【临时隧道】服务（网址每次重启会变）"
    cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=采购系统 Cloudflare 临时隧道
After=network.target cgsys.service

[Service]
Type=simple
ExecStart=${CF} tunnel --url http://localhost:${PORT} --no-autoupdate
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now "$SERVICE"
fi

# ---------------------------------------------------------------- 3. 报网址
say "3/3 等它连上去（十几秒）"
sleep 15

if [ -n "$TOKEN" ]; then
    systemctl is-active --quiet cloudflared && echo "✅ cloudflared 在跑" \
        || { echo "❌ 没起来，看日志："; journalctl -u cloudflared -n 30 --no-pager; exit 1; }
    echo
    echo "组员用你在 Cloudflare 里配的那个域名访问。"
else
    URL=$(journalctl -u "$SERVICE" -n 80 --no-pager 2>/dev/null \
          | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1)
    if [ -n "$URL" ]; then
        echo "============================================================"
        echo "  组员用这个网址：$URL"
        echo "============================================================"
        echo
        echo "⚠️ 网址是临时的：服务器一重启就会变。变了就用这条命令再看一次："
        echo "     bash 看隧道网址.sh"
        echo
        echo "想让它固定不变？去 Cloudflare 建个隧道拿 token，然后："
        echo "     bash 配置隧道.sh <token>"
    else
        echo "还没拿到网址，可能还在连。过一会儿看日志："
        echo "     journalctl -u ${SERVICE} -n 50 --no-pager"
    fi
fi

cat <<EOF

------------------------------------------------------------
别忘了先去设两个东西（不设的话谁都进不来）：
  1. 采购入口密码      http://127.0.0.1:${PORT}/purchase
  2. 「分享给组员」里的组员口令
------------------------------------------------------------
EOF
