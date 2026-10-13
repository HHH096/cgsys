# -*- coding: utf-8 -*-
"""P1 骨架：目录结构、依赖、启动脚本。"""
import unittest
from pathlib import Path

from app import config

ROOT = Path(__file__).resolve().parent.parent


class TestSkeleton(unittest.TestCase):

    def test_requirements_lists_the_three_dependencies(self):
        text = (ROOT / 'requirements.txt').read_text(encoding='ascii')
        for pkg in ('fastapi', 'uvicorn', 'openpyxl'):
            self.assertIn(pkg, text, 'requirements.txt 缺 %s' % pkg)

    def test_requirements_files_are_ascii_only(self):
        """pip 用系统 locale 编码读 requirements 文件（中文 Windows 是 GBK）。

        中文注释会让 pip 直接抛 UnicodeDecodeError 起不来 —— 所以这两个文件必须纯 ASCII。
        """
        for name in ('requirements.txt', 'requirements-dev.txt'):
            data = (ROOT / name).read_bytes()
            try:
                data.decode('ascii')
            except UnicodeDecodeError as exc:
                self.fail('%s 不是纯 ASCII，pip 会报 UnicodeDecodeError：%s' % (name, exc))

    def test_launcher_is_ascii_only(self):
        """同理：cmd.exe 按当前代码页边执行边重读 .bat，多字节字符会让它从错误的字节继续，
        表现为「'astapi' 不是内部或外部命令」这类诡异错误。"""
        data = (ROOT / '启动.bat').read_bytes()
        try:
            data.decode('ascii')
        except UnicodeDecodeError as exc:
            self.fail('启动.bat 不是纯 ASCII：%s' % exc)

    def test_launcher_does_not_switch_codepage(self):
        """注释里可以提到 chcp（解释为什么不能用），但不能真的执行它。"""
        text = (ROOT / '启动.bat').read_text(encoding='ascii')
        commands = [ln.strip().lower() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().lower().startswith('rem')]
        for ln in commands:
            self.assertFalse(ln.startswith('chcp'),
                             '启动.bat 不能执行 chcp：cmd 会因此错位读取自身。实际那行：%r' % ln)
        self.assertIn('-m venv', text)
        self.assertIn('pip install', text)
        self.assertIn('-m app.main', text)

    def test_launcher_exists_and_points_at_venv_and_entrypoint(self):
        bat = ROOT / '启动.bat'
        self.assertTrue(bat.is_file(), '缺少 启动.bat')
        text = bat.read_text(encoding='utf-8')
        self.assertIn(r'.venv\Scripts\python.exe', text)
        self.assertIn('app.main', text)
        self.assertIn('-m venv', text, '首次运行要能自建 venv')
        self.assertIn('pip install', text, '缺依赖时要能自动安装')

    def test_package_layout(self):
        for rel in ('app/__init__.py', 'app/config.py', 'app/main.py',
                    'app/core/__init__.py', 'app/core/db.py', 'app/core/repo.py',
                    'app/core/rules.py', 'app/core/exporter.py',
                    'app/server/__init__.py', 'app/server/api.py',
                    'app/web/index.html', 'app/web/app.js', 'app/web/styles.css'):
            self.assertTrue((ROOT / rel).is_file(), '缺文件 %s' % rel)

    def test_data_paths_live_under_project_root(self):
        for name in ('DATA_DIR', 'ATTACH_DIR', 'EXPORT_DIR', 'DB_PATH'):
            p = Path(getattr(config, name))
            self.assertTrue(str(p).startswith(str(ROOT)), '%s 不在项目目录下：%s' % (name, p))

    def test_third_party_importable(self):
        import fastapi          # noqa: F401
        import openpyxl         # noqa: F401
        import uvicorn          # noqa: F401

    def test_defaults_match_the_template(self):
        self.assertEqual(config.DEFAULT_TITLE, '2026年智能工程与未来学院方程式赛车竞赛设备与耗材采购表')
        self.assertEqual(config.DEFAULT_GROUP, '电气组')
        self.assertEqual(config.DEFAULT_SPLIT_THRESHOLD, 1000.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
