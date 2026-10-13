# -*- coding: utf-8 -*-
"""垫资人：一笔采购是谁先掏的钱。

需求很轻 —— **只是标一下**，不管报销流程（车队/学校统一报，报下来再发给人）。
所以只有一个字段，而且**不进车队总表**（那边列是固定的）。
"""
import unittest
from pathlib import Path

from app.core import db, repo
from tests.support import EntryCase

ROOT = Path(__file__).resolve().parent.parent


class TestPayerField(EntryCase):

    def test_a_new_item_has_no_payer(self):
        iid = self._add(self.alice_c, self.alice, '不用垫的')
        self.assertEqual(repo.get_item(iid)['payer'], '')

    def test_the_migration_adds_the_column(self):
        with db.session() as c:
            cols = {row[1] for row in c.execute('PRAGMA table_info(items)')}
        self.assertIn('payer', cols)

    def test_can_record_who_paid(self):
        """关键是**存得进去** —— `_clean` 是白名单，字段漏了就等于写了个寂寞。"""
        iid = self._add(self.alice_c, self.alice, '我垫的')
        repo.update_item(iid, {'payer': '李四'})
        self.assertEqual(repo.get_item(iid)['payer'], '李四')

    def test_payer_is_trimmed(self):
        iid = self._add(self.alice_c, self.alice)
        repo.update_item(iid, {'payer': '  张三  '})
        self.assertEqual(repo.get_item(iid)['payer'], '张三')

    def test_can_clear_it(self):
        iid = self._add(self.alice_c, self.alice)
        repo.update_item(iid, {'payer': '李四'})
        repo.update_item(iid, {'payer': ''})
        self.assertEqual(repo.get_item(iid)['payer'], '')

    def test_it_survives_a_partial_edit(self):
        """改别的字段时不能把垫资人抹掉。"""
        iid = self._add(self.alice_c, self.alice)
        repo.update_item(iid, {'payer': '李四'})
        repo.update_item(iid, {'note': '改了备注'})
        self.assertEqual(repo.get_item(iid)['payer'], '李四')

    def test_it_is_not_in_the_exported_block(self):
        """车队总表那 20 列是死的，垫资人**不进**那一行。

        名字要挑一个不会和「申购人」撞的 —— `_add` 里申购人固定写的是
        「李四」，拿它来断言会误判成失败。
        """
        from app.core import blockexport
        iid = self._add(self.alice_c, self.alice, '我垫的')
        repo.update_item(iid, {'payer': '垫资测试员'})
        row = blockexport.block_rows([repo.get_item(iid)])[0]
        self.assertNotIn('垫资测试员', row)

    def test_it_is_not_in_the_exported_sheet_either(self):
        """三张 sheet 也不能多出列来（列数变了车队那边就贴不进去）。"""
        from app.core import exporter
        iid = self._add(self.alice_c, self.alice, '我垫的')
        repo.update_item(iid, {'payer': '李四'})
        wb, _ = exporter.build_workbook(repo.get_round(self.rid), [repo.get_item(iid)])
        ws = wb[exporter.SHEET_MAIN]
        headers = [c.value for c in ws[2]]
        self.assertNotIn('垫资人', headers)
        self.assertEqual(len(headers), len(exporter.MAIN_COLUMNS))

    def test_api_accepts_it(self):
        iid = self._add(self.alice_c, self.alice, 'API 垫资')
        r = self.purchase.put('/api/items/%d' % iid, json={'payer': '赵六'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['item']['payer'], '赵六')

    def test_a_member_can_mark_their_own(self):
        """组员自己垫的钱，他自己当然要能标上。"""
        iid = self._add(self.alice_c, self.alice)
        r = self.alice_c.put('/api/items/%d' % iid, json={'payer': '李四'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['item']['payer'], '李四')

    def test_a_member_still_cannot_mark_someone_elses(self):
        """但别人的条目还是动不了（归属校验没被这条新字段绕过）。"""
        theirs = self._add(self.bob_c, self.bob)
        r = self.alice_c.put('/api/items/%d' % theirs, json={'payer': '李四'})
        self.assertEqual(r.status_code, 403)


class TestPayerFrontEndWiring(unittest.TestCase):
    """界面接线：填单能填、列表能看、能筛。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')
        cls.css = (ROOT / 'app/web/styles.css').read_text(encoding='utf-8')

    def test_form_has_the_field(self):
        self.assertIn('id="f_payer"', self.html)
        self.assertIn('谁垫的钱', self.html)

    def test_the_field_offers_member_names(self):
        """用 datalist 而不是下拉 —— 垫资的人可能不在名单里（比如外组代买）。"""
        self.assertIn('id="payers"', self.html)
        self.assertIn("$('payers')", self.js)

    def test_form_reads_and_restores_it(self):
        self.assertIn("payer: $('f_payer').value", self.js)
        self.assertIn("$('f_payer').value = it.payer || ''", self.js)

    def test_the_list_shows_a_tag(self):
        self.assertIn('payertag', self.js)
        self.assertIn('.payertag', self.css)
        self.assertIn('垫资 ', self.js)

    def test_there_is_a_filter_for_it(self):
        """标了却没法筛，等于白标。"""
        self.assertIn('data-view="advanced"', self.html)
        self.assertIn("state.view === 'advanced'", self.js)
        self.assertIn('id="c_adv"', self.html)

    def test_the_filter_is_not_purchase_only(self):
        """组员也要能筛出「我垫的那些」。"""
        seg = self.html.split('data-view="advanced"')[1].split('>')[0]
        self.assertNotIn('purchase-only', seg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
