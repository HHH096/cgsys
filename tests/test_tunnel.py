# -*- coding: utf-8 -*-
"""外网访问：隧道脚本的边角，以及那个 .bat 的老规矩。"""
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from app.core import settings
from app.tools import tunnel
from tests.support import TempDataTest

ROOT = Path(__file__).resolve().parent.parent


class TestUrlRe(unittest.TestCase):

    def test_picks_the_quick_tunnel_url(self):
        line = ('2026-10-07 INF +----------------------------+\n'
                '2026-10-07 INF |  https://calm-river-1234.trycloudflare.com  |\n')
        hit = tunnel.URL_RE.search(line)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.group(0), 'https://calm-river-1234.trycloudflare.com')

    def test_ignores_other_urls(self):
        self.assertIsNone(tunnel.URL_RE.search('see https://dash.cloudflare.com'))


class TestEnsureCloudflared(TempDataTest):
    """注意：CF_DIR / CF_EXE 是**模块级常量**，只能直接 patch 它们。

    光 patch `config.DATA_DIR` 是改不动它们的 —— 这个坑在这个项目里踩过两次
    （先是 settings.json，再是这里）。不 patch 的话测试会往真实的项目目录里写假文件。
    """

    def setUp(self):
        super().setUp()
        self.dir = Path(self._tmp.name) / 'cloudflared'
        p1 = mock.patch.object(tunnel, 'CF_DIR', self.dir)
        p2 = mock.patch.object(tunnel, 'CF_EXE', self.dir / 'cloudflared.exe')
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_existing_binary_is_reused(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tunnel.CF_EXE.write_bytes(b'MZ fake')
        self.assertTrue(tunnel.ensure_cloudflared())
        self.assertEqual(tunnel.CF_EXE.read_bytes(), b'MZ fake', '不该重复下载')

    def test_download_failure_says_where_to_put_it(self):
        """下载不动的时候必须说清楚「手动下、放哪儿」，不然用户就卡在这儿了。"""
        buf = io.StringIO()
        with mock.patch.object(tunnel.urllib.request, 'urlopen',
                               side_effect=OSError('没网')):
            with contextlib.redirect_stdout(buf):
                ok = tunnel.ensure_cloudflared()
        self.assertFalse(ok)
        out = buf.getvalue()
        self.assertIn('cloudflared-windows-amd64.exe', out)
        self.assertIn('cloudflared.exe', out)

    def test_the_patch_actually_redirects(self):
        """要验的是**测试自己被重定向到了临时目录**。

        而不能去断言「项目里没有 cloudflared.exe」—— 用户双击
        `外网访问.bat` 时它会真的下载一个到那儿，那是正常行为。
        （这个错我犯过一次：真实项目里出现了 cloudflared.exe，
        我第一反应是"测试污染了项目"，其实那是用户在用。）
        """
        self.assertEqual(self.dir, tunnel.CF_DIR)
        self.assertEqual(self.dir / 'cloudflared.exe', tunnel.CF_EXE)


class TestMemberPasswordReminder(TempDataTest):

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tunnel.check_member_password()
        return buf.getvalue()

    def test_warns_when_not_set(self):
        settings.set_member_password('')
        self.assertIn('还没设组员口令', self._run())

    def test_quiet_when_set(self):
        settings.set_member_password('dianqi')
        self.assertIn('已设过', self._run())


class TestTunnelBat(unittest.TestCase):

    def test_ascii_only_and_no_chcp(self):
        text = (ROOT / '外网访问.bat').read_bytes().decode('ascii')
        commands = [ln.strip().lower() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().lower().startswith('rem')]
        for ln in commands:
            self.assertFalse(ln.startswith('chcp'), '不该执行 chcp')
        self.assertIn('app.tools.tunnel', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
