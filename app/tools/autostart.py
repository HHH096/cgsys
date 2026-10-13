# -*- coding: utf-8 -*-
"""开机自启：往 Windows 的「启动」文件夹放一个小脚本。

两个必须说清楚的点：

1. **Windows 只在有人登录之后**才运行「启动」里的东西 —— 所以队里那台电脑
   要么一直保持登录，要么设成自动登录，否则重启后服务不会自己起来。
2. 写出去的那个 `.bat` **按 GBK 编码**写。cmd.exe 是按系统代码页读脚本文件的，
   而项目路径里就有中文（`D:\\采购系统`），用 UTF-8 写会变成乱码、路径找不到。
"""
import os
import sys
from pathlib import Path

from app import config

STARTUP_NAME = 'procurement-system.bat'


def startup_dir() -> Path:
    appdata = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
    return (Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu'
            / 'Programs' / 'Startup')


def target_path() -> Path:
    return startup_dir() / STARTUP_NAME


def launcher_text() -> str:
    """开机要跑的那段。直接用 pythonw（无窗口），免得开机弹个黑框出来。"""
    root = config.ROOT
    python = root / '.venv' / 'Scripts' / 'pythonw.exe'
    return ('@echo off\r\n'
            'cd /d "%s"\r\n'
            '"%s" -m app.main\r\n' % (root, python))


def enable() -> Path:
    d = startup_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = target_path()
    p.write_text(launcher_text(), encoding='gbk', errors='replace')
    return p


def disable() -> bool:
    p = target_path()
    if p.exists():
        p.unlink()
        return True
    return False


def status() -> dict:
    p = target_path()
    return {'enabled': p.exists(), 'path': str(p)}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = (argv[0] if argv else 'status').lower()

    if action in ('on', 'enable'):
        p = enable()
        print('已开启开机自启：%s' % p)
        print('说明：Windows 只在有人登录后才运行启动项，')
        print('     队里那台电脑要么保持登录，要么设成自动登录。')
        return 0
    if action in ('off', 'disable'):
        print('已取消开机自启。' if disable() else '本来就没有开启开机自启。')
        return 0

    st = status()
    print('开机自启：%s' % ('已开启' if st['enabled'] else '未开启'))
    print('位置：%s' % st['path'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
