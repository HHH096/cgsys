# -*- coding: utf-8 -*-
"""把本机的采购系统通过 Cloudflare 隧道发布出去（临时网址）。

为什么需要它：队里那台电脑在运营商的大内网后面（没有自己的公网 IP），
路由器上做端口转发也没用。而隧道是**服务器主动连出去**，所以能穿出去。

这是**临时隧道** —— 网址每次重启都变，适合"偶尔用一次"。
要固定网址见 `外网访问指南.md` 第 4 节。
"""
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from app import config

CF_DIR = config.ROOT / 'cloudflared'
CF_EXE = CF_DIR / 'cloudflared.exe'
DOWNLOAD = ('https://github.com/cloudflare/cloudflared/releases/latest/download/'
            'cloudflared-windows-amd64.exe')
URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
DEFAULT_PORT = 8000


def ensure_cloudflared() -> bool:
    if CF_EXE.exists():
        return True
    CF_DIR.mkdir(parents=True, exist_ok=True)
    print('第一次用，先下载 cloudflared（约 20MB，可能要一会儿）……')
    try:
        with urllib.request.urlopen(DOWNLOAD, timeout=300) as resp, \
                open(str(CF_EXE), 'wb') as out:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                out.write(chunk)
        print('下载好了。')
        return True
    except Exception as exc:
        print('自动下载失败：%s' % exc)
        print()
        print('请手动下载这个文件（浏览器打开就行）：')
        print('  %s' % DOWNLOAD)
        print('下载完放进这个文件夹：')
        print('  %s' % CF_DIR)
        print('并改名成 cloudflared.exe，然后重新双击 外网访问.bat。')
        return False


def check_member_password() -> None:
    """提醒一句：组员口令没设的话，组员进门进不来。"""
    from app.core import settings
    if settings.has_member_password():
        print('组员口令：已设过 ✓')
    else:
        print('⚠️ 你还没设组员口令 —— 组员打开网址会进不去。')
        print('   请先在 /purchase 的「分享给组员」里设一个。')


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[0]) if argv and argv[0].isdigit() else DEFAULT_PORT

    if not ensure_cloudflared():
        return 1

    print()
    check_member_password()
    print()
    print('正在开隧道（要连到 Cloudflare，稍等十几秒）……')
    print()

    try:
        proc = subprocess.Popen(
            [str(CF_EXE), 'tunnel', '--url', 'http://localhost:%d' % port,
             '--no-autoupdate'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1)
    except OSError as exc:
        print('起不来：%s' % exc)
        return 1

    shown = False
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not shown:
                hit = URL_RE.search(line)
                if hit:
                    shown = True
                    print('=' * 64)
                    print('  组员用这个网址：%s' % hit.group(0))
                    print('=' * 64)
                    print()
                    print('  把它连同组员口令一起发到群里。')
                    print('  这个窗口要一直开着 —— 关了隧道就断了。')
                    print('  网址是临时的，重启后会变（要固定的看 外网访问指南.md）。')
                    print()
                    continue
            if 'ERR' in line or 'error' in line.lower():
                print(line)
    except KeyboardInterrupt:
        print('收工。')
    finally:
        proc.terminate()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
