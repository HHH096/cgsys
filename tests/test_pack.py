# -*- coding: utf-8 -*-
"""打包给服务器：关键是**别把 .venv 打进去**。

Windows 的虚拟环境传到 Linux 上一点用都没有，还又大又多文件。
"""
import unittest
import zipfile
from pathlib import Path

from app.tools import pack
from tests.support import TempDataTest

ROOT = Path(__file__).resolve().parent.parent


class TestPack(TempDataTest):
    """在一棵假的项目树上打包，检查收谁不收谁。"""

    def setUp(self):
        super().setUp()
        root = Path(self._tmp.name) / '采购系统'
        (root / 'app').mkdir(parents=True)
        (root / 'app' / 'main.py').write_text('x', encoding='utf-8')
        (root / 'requirements.txt').write_text('fastapi', encoding='ascii')
        (root / '组员名单.txt').write_text('张三', encoding='utf-8')

        # data 要带上 —— 名单、已填的采购、发票都在里面
        (root / 'data').mkdir()
        (root / 'data' / 'procurement.db').write_bytes(b'SQLite')
        (root / 'data' / 'service.log').write_text('log', encoding='utf-8')

        # 下面这些都不该带
        (root / '.venv' / 'Scripts').mkdir(parents=True)
        (root / '.venv' / 'Scripts' / 'python.exe').write_bytes(b'MZ')
        (root / '__pycache__').mkdir()
        (root / '__pycache__' / 'x.pyc').write_bytes(b'\x00')
        (root / '.reasonix').mkdir()
        (root / '.reasonix' / 'note').write_text('x', encoding='utf-8')
        (root / '导出').mkdir()
        (root / '导出' / 'a.xlsx').write_bytes(b'x')

        self.root = root
        self.out, self.files, self.total = pack.pack(
            root=root, out_dir=Path(self._tmp.name))

    def _names(self):
        with zipfile.ZipFile(str(self.out)) as zf:
            return zf.namelist()

    def test_keeps_the_real_stuff(self):
        names = self._names()
        self.assertIn('采购系统/requirements.txt', names)
        self.assertIn('采购系统/app/main.py', names)
        self.assertIn('采购系统/组员名单.txt', names)

    def test_keeps_the_data_folder(self):
        """数据要迁过去，不然名单和已填的采购都丢了。"""
        self.assertIn('采购系统/data/procurement.db', self._names())

    def test_leaves_out_venv(self):
        """**最要紧的一条。**"""
        for name in self._names():
            self.assertNotIn('.venv', name)

    def test_leaves_out_junk(self):
        names = self._names()
        for junk in ('__pycache__', '.reasonix', '导出', '.pyc', 'service.log'):
            self.assertFalse(any(junk in n for n in names), '不该带上 %s' % junk)

    def test_has_one_top_level_folder(self):
        """zip 里套一层顶层目录，解压出来是个整文件夹，不会散一地。"""
        tops = {n.split('/', 1)[0] for n in self._names()}
        self.assertEqual(tops, {'采购系统'})

    def test_does_not_include_the_zip_itself(self):
        self.assertFalse(any(n.endswith(pack.OUT_NAME) for n in self._names()))

    def test_reports_counts(self):
        self.assertGreater(self.files, 0)
        self.assertGreater(self.total, 0)

    def test_human_size(self):
        self.assertEqual(pack.human(0), '0.0 B')
        self.assertEqual(pack.human(2048), '2.0 KB')


class TestPackBat(unittest.TestCase):

    def test_ascii_only_and_no_chcp(self):
        text = (ROOT / '打包给服务器.bat').read_bytes().decode('ascii')
        commands = [ln.strip().lower() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().lower().startswith('rem')]
        for ln in commands:
            self.assertFalse(ln.startswith('chcp'), '不该执行 chcp')
        self.assertIn('app.tools.pack', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
