# -*- coding: utf-8 -*-
"""后台运行服务：无窗口启动 / 停止 / 查状态。

为什么要无窗口：队里那台电脑是常开的，一个黑窗口摆在桌面上很容易被人随手关掉，
服务就停了。用 `pythonw.exe`（无控制台的 Python）启动就没有窗口。

代价是**看不到输出也点不了叉**，所以这里配齐：

* 启动后会把输出写进 `data\\service.log`（用 pythonw 时 main.py 自动接管）
* 「停止服务.bat」按端口找进程并结束它
* 启动时会等它就绪，再告诉你成功还是失败（失败就去看日志）
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from app import config

DEFAULT_PORT = 8000
READY_TIMEOUT = 25.0


def python_exe(windowless: bool = True) -> Path:
    name = 'pythonw.exe' if windowless else 'python.exe'
    return Path(config.ROOT) / '.venv' / 'Scripts' / name


def port() -> int:
    try:
        return int(os.environ.get('CGSYS_PORT', DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _pids_on_port(p: int) -> list:
    """谁在监听这个端口（Windows 上用 netstat 查，不需要额外依赖）。"""
    try:
        out = subprocess.run(['netstat', '-ano'], capture_output=True, text=True,
                             timeout=20, errors='replace').stdout or ''
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(':%d' % p) and parts[3].upper() == 'LISTENING':
            if parts[4] not in pids:
                pids.append(parts[4])
    return pids


def is_running(p: int = None) -> bool:
    return bool(_pids_on_port(port() if p is None else p))


def _port_open(p: int, timeout: float = 0.4) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex(('127.0.0.1', p)) == 0
    finally:
        s.close()


def log_path() -> Path:
    return Path(config.DATA_DIR) / 'service.log'


def start(port_=None, wait: float = READY_TIMEOUT) -> dict:
    """无窗口启动服务，等它就绪。"""
    p = port() if port_ is None else int(port_)
    if is_running(p):
        return {'ok': True, 'already': True, 'port': p}

    exe = python_exe(windowless=True)
    if not exe.exists():
        return {'ok': False, 'error': '找不到 %s，请先运行一次「启动.bat」建好运行环境' % exe}

    config.ensure_dirs()
    flags = 0
    for name in ('DETACHED_PROCESS', 'CREATE_NO_WINDOW', 'CREATE_NEW_PROCESS_GROUP'):
        flags |= getattr(subprocess, name, 0)

    env = dict(os.environ)
    env['CGSYS_PORT'] = str(p)

    with open(str(log_path()), 'ab') as log:
        subprocess.Popen([str(exe), '-m', 'app.main', str(p)],
                         cwd=str(config.ROOT), env=env, close_fds=True,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=flags)

    deadline = time.time() + max(1.0, wait)
    while time.time() < deadline:
        if _port_open(p):
            return {'ok': True, 'already': False, 'port': p}
        time.sleep(0.4)
    return {'ok': False, 'error': '等了一会儿还没起来，看看日志：%s' % log_path(), 'port': p}


def stop(port_=None) -> dict:
    p = port() if port_ is None else int(port_)
    pids = _pids_on_port(p)
    if not pids:
        return {'ok': True, 'stopped': 0, 'port': p}
    stopped = 0
    for pid in pids:
        try:
            subprocess.run(['taskkill', '/F', '/PID', pid], capture_output=True, timeout=20)
            stopped += 1
        except Exception:
            pass
    for _ in range(20):
        if not _pids_on_port(p):
            break
        time.sleep(0.3)
    return {'ok': not _pids_on_port(p), 'stopped': stopped, 'port': p}


def status(port_=None) -> dict:
    p = port() if port_ is None else int(port_)
    pids = _pids_on_port(p)
    return {'running': bool(pids), 'port': p, 'pids': pids}


def lan_url(p: int) -> str:
    from app.core import netinfo
    return netinfo.share_url(p)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = (argv[0] if argv else 'status').lower()

    if action in ('start', 'on', '后台启动'):
        r = start()
        if r.get('ok'):
            print('服务已在后台运行。' if r.get('already') else '服务已在后台启动。')
            print('  组员用这个：%s' % lan_url(r['port']))
            print('  本机打开：  http://127.0.0.1:%d' % r['port'])
            print('  日志文件：  %s' % log_path())
            print()
            print('要停止服务，双击「停止服务.bat」。')
        else:
            print('启动失败：%s' % r.get('error'))
        return 0 if r.get('ok') else 1

    if action in ('stop', 'off', '停止'):
        r = stop()
        print('服务已停止（结束了 %d 个进程）。' % r['stopped'] if r['stopped']
              else '服务本来就没在运行。')
        return 0

    st = status()
    if st['running']:
        print('服务正在运行：端口 %d，进程 %s' % (st['port'], '、'.join(st['pids'])))
        print('  组员用这个：%s' % lan_url(st['port']))
    else:
        print('服务没有在运行（端口 %d 没人监听）。' % st['port'])
    print('  日志文件：%s' % log_path())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
