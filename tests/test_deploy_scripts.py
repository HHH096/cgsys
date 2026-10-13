# -*- coding: utf-8 -*-
"""Linux 部署脚本的边角（在 Windows 上也能验的部分）。

真在 ECS 上跑一遍要等机器就绪，但这两件事必须提前查死：

* **换行符**：Windows 上编辑过的 .sh 会带 CRLF，Linux 上 bash 会报
  `$'\\r': command not found`，而且报错位置往往莫名其妙。
* **语法**：`bash -n` 能提前把笔误揪出来。
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ('部署到服务器.sh', '配置隧道.sh', '看隧道网址.sh')


class TestDeployScripts(unittest.TestCase):

    def test_scripts_exist(self):
        for name in SCRIPTS:
            self.assertTrue((ROOT / name).is_file(), '缺 %s' % name)

    def test_no_carriage_returns(self):
        """**必须是 LF** —— CRLF 会让 Linux 上的 bash 满屏怪错。"""
        for name in SCRIPTS:
            data = (ROOT / name).read_bytes()
            self.assertNotIn(
                b'\r', data,
                '%s 里有 CR —— 传上去会报 "$\'\\r\': command not found"' % name)

    def test_starts_with_a_shebang(self):
        for name in SCRIPTS:
            first = (ROOT / name).read_bytes().split(b'\n', 1)[0]
            self.assertEqual(first, b'#!/bin/bash', name)

    @unittest.skipUnless(shutil.which('bash'), '这台机器没有 bash')
    def test_bash_syntax(self):
        for name in SCRIPTS:
            r = subprocess.run(['bash', '-n', str(ROOT / name)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, '%s 语法有问题：%s' % (name, r.stderr))

    def test_has_a_crlf_self_heal(self):
        """脚本要能自己清掉 CRLF 再重跑 —— 用户多半在 Windows 上碰过它。"""
        text = (ROOT / '部署到服务器.sh').read_text(encoding='utf-8')
        self.assertIn("sed -i 's/\\r$//'", text)
        self.assertIn('exec bash', text)

    def test_installs_and_autostarts(self):
        text = (ROOT / '部署到服务器.sh').read_text(encoding='utf-8')
        for needle in ('python3 -m venv', 'requirements.txt', 'pip install',
                       'systemctl enable', 'systemd/system', 'Restart=always',
                       'journalctl'):
            self.assertIn(needle, text, '部署脚本里少了 %s' % needle)

    def test_handles_the_three_families_of_linux(self):
        """Alibaba Cloud Linux 用的是 dnf/yum；换 Ubuntu 也得能装。"""
        text = (ROOT / '部署到服务器.sh').read_text(encoding='utf-8')
        for pm in ('dnf', 'yum', 'apt-get'):
            self.assertIn(pm, text, '没考虑用 %s 的系统' % pm)

    def test_refuses_to_run_outside_the_project(self):
        text = (ROOT / '部署到服务器.sh').read_text(encoding='utf-8')
        self.assertIn('app/main.py', text)

    def test_reminds_you_to_set_the_two_passwords(self):
        """装完要提醒设采购密码和组员口令 —— 忘了就是敞开的大门。"""
        text = (ROOT / '部署到服务器.sh').read_text(encoding='utf-8')
        self.assertIn('组员口令', text)
        self.assertIn('/purchase', text)


class TestTunnelScripts(unittest.TestCase):
    """隧道脚本：这是「服务器没有公网 IP」也能对外服务的唯一办法。"""

    def _text(self, name):
        return (ROOT / name).read_text(encoding='utf-8')

    def test_tunnel_script_installs_a_service(self):
        text = self._text('配置隧道.sh')
        self.assertIn('cloudflared', text)
        # 固定隧道用 cloudflared 自带的 service install（省得自己写 unit）
        self.assertIn('service install', text)
        self.assertIn('systemctl enable', text)

    def test_tunnel_script_handles_both_arches(self):
        """ECS 是 x86_64，但换 arm 机器也得能装。"""
        text = self._text('配置隧道.sh')
        self.assertIn('amd64', text)
        self.assertIn('arm64', text)

    def test_tunnel_script_supports_both_modes(self):
        text = self._text('配置隧道.sh')
        self.assertIn('trycloudflare', text)      # 临时
        self.assertIn('TOKEN="$1"', text)          # 固定

    def test_tunnel_script_points_at_the_local_port(self):
        self.assertIn('localhost:${PORT}', self._text('配置隧道.sh'))

    def test_url_helper_script_exists(self):
        text = self._text('看隧道网址.sh')
        self.assertIn('trycloudflare', text)
        self.assertIn('journalctl', text)

    def test_both_tunnel_scripts_self_heal_crlf(self):
        for name in ('配置隧道.sh', '看隧道网址.sh'):
            text = self._text(name)
            self.assertIn("sed -i 's/\\r$//'", text, '%s 少了 CRLF 自愈' % name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
