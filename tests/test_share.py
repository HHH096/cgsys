# -*- coding: utf-8 -*-
"""分享给组员：局域网地址与二维码。"""
import os
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.core import netinfo, shareqr
from app.server.api import app
from tests.support import TempDataTest, purchase_client

ROOT = Path(__file__).resolve().parent.parent
PNG_HEAD = b'\x89PNG\r\n\x1a\n'


class TestNetInfo(unittest.TestCase):

    def test_lan_ip_is_a_nonempty_string(self):
        ip = netinfo.lan_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(ip)

    def test_share_url_shape(self):
        url = netinfo.share_url(8000)
        self.assertTrue(url.startswith('http://'))
        self.assertTrue(url.endswith(':8000'))
        self.assertIn(netinfo.lan_ip(), url)

    def test_local_url(self):
        self.assertEqual(netinfo.local_url(8000), 'http://127.0.0.1:8000')

    def test_custom_port(self):
        self.assertTrue(netinfo.share_url(8123).endswith(':8123'))


class TestQr(unittest.TestCase):

    def test_returns_png(self):
        data = shareqr.qr_png('http://192.168.1.5:8000')
        self.assertEqual(data[:8], PNG_HEAD)
        self.assertGreater(len(data), 200)

    def test_available(self):
        self.assertTrue(shareqr.available())

    def test_share_info(self):
        info = shareqr.share_info(8000)
        self.assertEqual(info['port'], 8000)
        self.assertIn('url', info)
        self.assertIn('local_url', info)
        self.assertTrue(info['qrcode'])


class TestShareApi(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)

    def test_share_info(self):
        r = self.client.get('/api/share')
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertTrue(j['url'].startswith('http://'))
        self.assertTrue(j['local_url'].startswith('http://127.0.0.1'))
        self.assertTrue(j['qrcode'])

    def test_qrcode_is_a_png(self):
        r = self.client.get('/api/share/qrcode.png')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['content-type'], 'image/png')
        self.assertEqual(r.content[:8], PNG_HEAD)

    def test_port_comes_from_the_env(self):
        """端口改了以后二维码里的地址也得跟着改。"""
        with mock.patch.dict(os.environ, {'CGSYS_PORT': '8123'}):
            j = self.client.get('/api/share').json()
        self.assertEqual(j['port'], 8123)
        self.assertTrue(j['url'].endswith(':8123'))


class TestFrontendWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')

    def test_controls_exist(self):
        for needle in ('id="shareQr"', 'id="shareUrl"', 'id="shareCopy"',
                       'id="shareRefresh"', 'id="shareHint"', 'id="shareLocal"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_buttons_are_bound(self):
        self.assertIn("$('shareCopy').addEventListener('click', copyShareUrl)", self.js)
        self.assertIn("$('shareRefresh').addEventListener('click', loadShare)", self.js)

    def test_points_at_the_api(self):
        self.assertIn("'/api/share'", self.js)
        self.assertIn("'/api/share/qrcode.png", self.js)

    def test_loaded_on_startup(self):
        self.assertIn('await loadShare();', self.js)

    def test_copy_has_a_fallback_for_http(self):
        """局域网走 http，navigator.clipboard 会被浏览器拒绝，必须有退路。"""
        self.assertIn('navigator.clipboard.writeText', self.js)
        self.assertIn("execCommand('copy')", self.js)


if __name__ == '__main__':
    unittest.main(verbosity=2)
