# -*- coding: utf-8 -*-
"""测试脚手架：把 config 里所有数据路径都指到临时目录，绝不碰真实数据。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config
from app.core import db, repo


def temp_config_patches(root):
    """集中列出所有跟着 DATA_DIR 走的路径。

    只在 `config.DATA_DIR` 上打补丁是**不够的**：`MAIL_INBOX_DIR`、`MAIL_CONFIG`、
    `DB_PATH` 这些是在模块加载时用 DATA_DIR 拼出来的**常量**，patch 了 DATA_DIR
    它们并不会跟着变。曾经就是因为另一处脚手架漏了这几个，导致附件落到真实目录、
    又拿临时目录去算相对路径而报错。所以这里只留一份，大家都用它。
    """
    root = Path(root)
    return [
        ('DATA_DIR', root),
        ('ATTACH_DIR', root / 'attachments'),
        ('INVOICE_DIR', root / 'invoices'),
        ('MAIL_INBOX_DIR', root / 'mail_inbox'),
        ('MAIL_CONFIG', root / 'mail.json'),
        ('EXPORT_DIR', root / 'export'),
        ('DB_PATH', root / 'procurement.db'),
        ('ROSTER_FILE', root / '组员名单.txt'),
    ]


def patch_config(testcase, root):
    """按上面的清单打补丁，并在测试结束时自动还原。"""
    for name, value in temp_config_patches(root):
        patcher = mock.patch.object(config, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)


class TempDataTest(unittest.TestCase):
    """每个测试用独立的临时数据目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patch_config(self, Path(self._tmp.name))
        db.init_db()


def purchase_client(testcase, password='test-pw'):
    """建一个**已登录**（采购人员）的 TestClient，测试结束自动清理。

    接口加了登录校验之后，凡是调采购专用接口的测试都要用这个 ——
    「没登录会被 401」本身是对的（tests/test_auth.py 专门验它），
    只是老测试得拿着正确身份去调。
    """
    from fastapi.testclient import TestClient

    from app.core import settings
    from app.server.api import app

    settings.reset_state()
    testcase.addCleanup(settings.reset_state)
    settings.set_password(password)

    client = TestClient(app)
    client.__enter__()
    testcase.addCleanup(client.__exit__, None, None, None)

    r = client.post('/api/purchase/login', json={'password': password})
    assert r.status_code == 200, '测试脚手架登录失败：%s' % r.text
    return client


TEST_PASSWORD = 'cgsys-test-pw'
TEST_MEMBER_PW = 'cgsys-member-pw'
TEST_PDF = (b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n'
            b'trailer<</Root 1 0 R>>\n%%EOF\n')


class EntryCase(TempDataTest):
    """双入口的测试底座。

    * `purchase` —— 已登录的采购端
    * `alice_c` / `bob_c` —— 已过组员口令门、且各自选好了名字
    * `anon` —— **什么都没解锁**，专门用来验「没解锁一律 401」

    组员入口现在要全组共用的口令（服务可能被隧道暴露到公网），
    所以这两类客户端各有各的门要过。
    """

    def setUp(self):
        from app.core import settings

        super().setUp()
        settings.reset_state()
        self.addCleanup(settings.reset_state)
        settings.set_member_password(TEST_MEMBER_PW)

        self.owner = repo.add_member('张三', '13800000000', 1)
        self.alice = repo.add_member('李四', '', 0)
        self.bob = repo.add_member('钱七', '', 0)

        self.anon = self._client()
        self.purchase = self._client()
        self.alice_c = self._client()
        self.bob_c = self._client()

        # 第一次设密码不需要旧密码，**但要带初始化码**（防止公网上被抢先夺权），
        # 设完顺手就登录了
        r = self.purchase.post('/api/purchase/password',
                               json={'new': TEST_PASSWORD,
                                     'setup': settings.setup_code()})
        assert r.status_code == 200, r.text

        # 组员客户端：先过口令门，再各自选名字
        for client, mid in ((self.alice_c, self.alice), (self.bob_c, self.bob)):
            r = client.post('/api/member/login', json={'password': TEST_MEMBER_PW})
            assert r.status_code == 200, r.text
            assert client.post('/api/me',
                               json={'member_id': mid}).status_code == 200

        self.rid = self.purchase.post(
            '/api/rounds', json={'name': '十月采购'}).json()['id']

    def _client(self):
        from fastapi.testclient import TestClient

        from app.server.api import app

        c = TestClient(app)
        c.__enter__()
        self.addCleanup(c.__exit__, None, None, None)
        return c

    def _add(self, client, mid, name='排线'):
        r = client.post('/api/rounds/%d/items' % self.rid,
                        json={'member_id': mid, 'name': name, 'qty': 1,
                              'unit': '个', 'price': 5, 'purpose': '用',
                              'applicant': '李四'})
        assert r.status_code == 200, r.text
        return r.json()['item']['id']
