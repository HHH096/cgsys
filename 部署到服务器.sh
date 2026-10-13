#!/bin/bash
# 采购系统 · 部署到 Linux 服务器（阿里云 ECS / Alibaba Cloud Linux 3 上试过）
#
# 怎么用：
#   1. 把整个项目文件夹传到服务器（比如 /opt/cgsys）
#   2. cd /opt/cgsys
#   3. bash 部署到服务器.sh
#
# 装完就开机自启了，日志看：journalctl -u cgsys -f

# 如果在 Windows 上编辑/传输过，行尾可能是 CRLF —— 那会让 bash 报
# "$'\r': command not found" 这种莫名其妙的错。这里先自愈一下。
if grep -q $'\r' "$0" 2>/dev/null; then
    sed -i 's/\r$//' "$0"
    exec bash "$0" "$@"
fi

set -e

SERVICE=cgsys
PORT=8000
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

say() { echo; echo "==> $*"; }

if [ ! -f "$APP_DIR/app/main.py" ]; then
    echo "这好像不是项目目录（找不到 app/main.py）。"
    echo "请先 cd 到项目文件夹再跑这个脚本。当前是：$APP_DIR"
    exit 1
fi

say "项目目录：$APP_DIR"

# ---------------------------------------------------------------- 1. 系统依赖
say "1/5 装系统依赖（python3）"
if command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip
elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3 python3-venv python3-pip
else
    echo "认不出这是哪种 Linux，请先自己装好 python3 和 pip。"
    exit 1
fi

# ---------------------------------------------------------------- 2. 运行环境
say "2/5 建运行环境（venv + 依赖，第一次要几分钟）"
cd "$APP_DIR"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

# ---------------------------------------------------------------- 3. systemd
say "3/5 配开机自启（systemd 服务）"
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=采购系统（电气组）
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python -m app.main ${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# ---------------------------------------------------------------- 4. 启动
say "4/5 启动服务"
systemctl enable --now ${SERVICE}
sleep 3

# ---------------------------------------------------------------- 5. 自检
say "5/5 自检"
if curl -s --max-time 5 -o /dev/null "http://127.0.0.1:${PORT}/api/meta"; then
    echo "✅ 服务起来了"
else
    echo "❌ 服务好像没起来，看看日志："
    journalctl -u ${SERVICE} -n 30 --no-pager
    exit 1
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

============================================================
  装好了，而且已经设成开机自启。
============================================================

  这台机器上打开：   http://127.0.0.1:${PORT}
  内网地址：         http://${IP}:${PORT}
  采购入口：         http://${IP}:${PORT}/purchase

  常用命令：
    看状态    systemctl status ${SERVICE}
    看日志    journalctl -u ${SERVICE} -f
    重启      systemctl restart ${SERVICE}
    停止      systemctl stop ${SERVICE}

  下一步：要发布到公网（组员不在同一个网也能用），跑
    bash 配置隧道.sh

  ⚠️ 记住先去 /purchase 设两个东西：
     1. 采购入口的密码
     2. 「分享给组员」里的组员口令（不设的话组员进不来）
EOF
