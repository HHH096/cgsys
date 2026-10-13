# -*- coding: utf-8 -*-
"""本机在局域网里的地址 —— 组员用它访问。"""
import socket


def lan_ip() -> str:
    """取本机在局域网里的地址。

    做法是往一个外网地址「连一下」UDP（不会真的发数据），
    只为让系统挑出默认网卡的地址。没有网时回退到 127.0.0.1。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def share_url(port: int = 8000) -> str:
    """给组员用的地址。"""
    return 'http://%s:%d' % (lan_ip(), port)


def local_url(port: int = 8000) -> str:
    """本机自己打开用的地址。"""
    return 'http://127.0.0.1:%d' % port
