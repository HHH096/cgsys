# -*- coding: utf-8 -*-
"""开机自启：写进「启动」文件夹的那个小脚本。"""
import os
import unittest
from pathlib import Path
from unittest import mock

from app import config
from app.tools import autostart
from tests.support import TempDataTest

ROOT = Path(__file__).resolve().parent.parent
BATS = ('开启开机自启.bat', '取消开机自启.bat')


class TestAutostart(TempDataTest):
    """把 APPDATA 指到临时目录，绝不写用户真正的启动文件夹。"""

    def setUp(self):
        super().setUp()
        self.appdata = Path(self._tmp.name) / 'AppData' / 'Roaming'
        self.appdata.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.dict(os.environ, {'APPDATA': str(self.appdata)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_startup_dir_is_under_appdata(self):
        d = autostart.startup_dir()
        self.assertTrue(str(d).startswith(str(self.appdata)), d)
        self.assertTrue(str(d).endswith('Startup'), d)

    def test_enable_writes_the_launcher(self):
        p = autostart.enable()
        self.assertTrue(p.exists())
        self.assertEqual(p.name, 'procurement-system.bat')

        # 必须能被 GBK 读出来 —— cmd 是按系统代码页读脚本的
        text = p.read_bytes().decode('gbk')
        self.assertIn('app.main', text)
        self.assertIn(str(config.ROOT), text)
        self.assertIn('.venv', text)

    def test_enable_is_idempotent(self):
        first = autostart.enable()
        second = autostart.enable()
        self.assertEqual(first, second)
        self.assertEqual(len(list(autostart.startup_dir().iterdir())), 1)

    def test_status_and_disable(self):
        self.assertFalse(autostart.status()['enabled'])
        autostart.enable()
        self.assertTrue(autostart.status()['enabled'])
        self.assertTrue(autostart.disable())
        self.assertFalse(autostart.status()['enabled'])
        self.assertFalse(autostart.disable(), '再取消一次应当是 False')

    def test_launcher_switches_directory_first(self):
        """开机时的工作目录不一定是项目目录，必须先 cd 过去。"""
        text = autostart.launcher_text()
        self.assertIn('cd /d', text)
        self.assertIn('-m app.main', text)

    def test_cli_on_off_status(self):
        self.assertEqual(autostart.main(['on']), 0)
        self.assertTrue(autostart.status()['enabled'])
        self.assertEqual(autostart.main(['off']), 0)
        self.assertFalse(autostart.status()['enabled'])
        self.assertEqual(autostart.main([]), 0)


class TestLauncherScripts(unittest.TestCase):
    """两个 .bat 必须纯 ASCII、且不执行 chcp（这个坑踩过，见设计说明第 10 节）。"""

    def read(self, name):
        return (ROOT / name).read_bytes()

    def test_ascii_only(self):
        for name in BATS:
            try:
                self.read(name).decode('ascii')
            except UnicodeDecodeError as exc:
                self.fail('%s 不是纯 ASCII：%s' % (name, exc))

    def test_no_chcp(self):
        for name in BATS:
            text = self.read(name).decode('ascii')
            commands = [ln.strip().lower() for ln in text.splitlines()
                        if ln.strip() and not ln.strip().lower().startswith('rem')]
            for ln in commands:
                self.assertFalse(ln.startswith('chcp'), '%s 不该执行 chcp' % name)

    def test_calls_the_autostart_module(self):
        self.assertIn('app.tools.autostart on',
                      self.read('开启开机自启.bat').decode('ascii'))
        self.assertIn('app.tools.autostart off',
                      self.read('取消开机自启.bat').decode('ascii'))

    def test_they_check_for_the_runtime_first(self):
        for name in BATS:
            self.assertIn('.venv', self.read(name).decode('ascii'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
