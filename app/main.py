# -*- coding: utf-8 -*-
"""启动入口：初始化数据库、起 HTTP 服务、打印局域网访问地址。"""
import os
import socket
import sys

import uvicorn

from app import config
from app.core import db, netinfo, roster

PORT = 8000


def _setup_logging():
    """用 pythonw（无控制台）启动时 stdout/stderr 是空的 —— 把输出接进日志文件。

    不然「后台启动」万一失败，你什么都看不到。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    config.ensure_dirs()
    try:
        fh = open(str(config.DATA_DIR / 'service.log'), 'a',
                  encoding='utf-8', errors='replace')
    except Exception:
        return None
    sys.stdout = fh
    sys.stderr = fh
    return fh


def main():
    _setup_logging()
    # 老 Windows 的控制台是 GBK；中文打不出来时用 ? 顶替，别让程序直接崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass

    port = PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    # 让接口层知道自己在哪个端口（生成组员扫码用的二维码要用）
    os.environ['CGSYS_PORT'] = str(port)

    config.ensure_dirs()
    db.init_db()
    roster_summary = roster.import_roster()

    ip = netinfo.lan_ip()
    print()
    print('=' * 58)
    print('  采购系统已启动（关掉这个窗口即停止服务）')
    print()
    print('  本机打开：   http://127.0.0.1:%d' % port)
    print('  组员用这个： http://%s:%d' % (ip, port))
    print('=' * 58)
    if roster_summary['added']:
        print('  已从 组员名单.txt 预置组员：%s' % '、'.join(roster_summary['added']))
    if roster_summary['updated']:
        print('  已补全已存在的组员：%s' % '、'.join(roster_summary['updated']))
    for b in roster_summary['bad']:
        print('  组员名单.txt 第 %s 行没认出来（%s）：%s' % (b['line'], b['why'], b['text']))
    print()

    from app.server.api import app as asgi_app
    uvicorn.run(asgi_app, host='0.0.0.0', port=port, log_level='warning')


if __name__ == '__main__':
    main()
