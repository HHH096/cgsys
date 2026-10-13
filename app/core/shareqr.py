# -*- coding: utf-8 -*-
"""把访问地址生成二维码（组员扫一下就进）。"""
import io

from app.core import netinfo


def qr_png(text: str) -> bytes:
    """把文本生成 PNG 二维码。没装 segno 时抛 RuntimeError（调用方自己降级）。"""
    import segno
    qr = segno.make(text, error='m')
    buf = io.BytesIO()
    qr.save(buf, kind='png', scale=6, border=2)
    return buf.getvalue()


def available() -> bool:
    try:
        import segno        # noqa: F401
        return True
    except Exception:
        return False


def share_info(port: int = 8000) -> dict:
    """给界面用的访问信息。"""
    url = netinfo.share_url(port)
    return {
        'url': url,
        'local_url': netinfo.local_url(port),
        'ip': netinfo.lan_ip(),
        'port': port,
        'qrcode': available(),
    }
