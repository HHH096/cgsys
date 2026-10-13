# -*- coding: utf-8 -*-
"""后台运行服务：无窗口启动 / 停止 / 查状态。"""
import unittest
from pathlib import Path
from unittest import mock

from app import config
from app.tools import service
from tests.support import TempDataTest

ROOT = Path(__file__).resolve().parent.parent
BATS = ('后台启动.bat', '停止服务.bat')


class TestHelpers(unittest.TestCase):

    def test_pythonw_exists(self):
        self.assertTrue(service.python_exe(True).name == 'pythonw.exe')
        self.assertTrue(service.python_exe(False).name == 'python.exe')
        self.assertTrue(service.python_exe(True).exists(), 'venv 里应当有 pythonw.exe')

    def test_port_from_env(self):
        with mock.patch.dict('os.environ', {'CGSYS_PORT': '8123'}):
            self.assertEqual(service.port(), 8123)

    def test_port_default(self):
        import os
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CGSYS_PORT', None)
            self.assertEqual(service.port(), 8000)

    def test_log_path_is_under_data(self):
        self.assertEqual(service.log_path().name, 'service.log')


class TestListeners(TempDataTest):
    """查端口上有没有人在听 —— 用一个真起过的临时服务来验。"""

    def test_nobody_is_listening_on_a_random_port(self):
        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        free = s.getsockname()[1]
        s.close()
        self.assertFalse(service.is_running(free))
        self.assertEqual(service.stop(free), {'ok': True, 'stopped': 0, 'port': free})

    def test_status_shape(self):
        st = service.status(59999)
        self.assertIn('running', st)
        self.assertIn('port', st)
        self.assertFalse(st['running'])


class TestCli(TempDataTest):

    def test_status_does_not_crash(self):
        self.assertEqual(service.main(['status']), 0)

    def test_stop_when_nothing_running_is_fine(self):
        with mock.patch.dict('os.environ', {'CGSYS_PORT': '59998'}):
            self.assertEqual(service.main(['stop']), 0)

    def test_unknown_action_falls_back_to_status(self):
        self.assertEqual(service.main(['什么']), 0)


class TestStartGuards(TempDataTest):

    def test_start_reports_already_running(self):
        with mock.patch.object(service, 'is_running', return_value=True):
            r = service.start(port_=59997)
        self.assertTrue(r['ok'])
        self.assertTrue(r['already'])

    def test_start_without_runtime_is_reported(self):
        with mock.patch.object(service, 'is_running', return_value=False), \
                mock.patch.object(service, 'python_exe',
                                  return_value=Path('不存在/pythonw.exe')):
            r = service.start(port_=59997)
        self.assertFalse(r['ok'])
        self.assertIn('找不到', r['error'])


class TestBats(unittest.TestCase):

    def test_ascii_only_and_no_chcp(self):
        for name in BATS:
            try:
                text = (ROOT / name).read_bytes().decode('ascii')
            except UnicodeDecodeError as exc:
                self.fail('%s 不是纯 ASCII：%s' % (name, exc))
            commands = [ln.strip().lower() for ln in text.splitlines()
                        if ln.strip() and not ln.strip().lower().startswith('rem')]
            for ln in commands:
                self.assertFalse(ln.startswith('chcp'), '%s 不该执行 chcp' % name)

    def test_calls_the_service_module(self):
        self.assertIn('app.tools.service start',
                      (ROOT / '后台启动.bat').read_text(encoding='ascii'))
        self.assertIn('app.tools.service stop',
                      (ROOT / '停止服务.bat').read_text(encoding='ascii'))


class TestAutostartUsesPythonw(unittest.TestCase):

    def test_launcher_is_windowless(self):
        from app.tools import autostart
        self.assertIn('pythonw.exe', autostart.launcher_text(),
                      '开机自启也不该弹黑框')


if __name__ == '__main__':
    unittest.main(verbosity=2)
