# -*- coding: utf-8 -*-
"""填单时「这个名字以前买过什么」—— 一键沿用上次的规格/单价/链接。

背景：组员每轮都在重复填同一个器件（电阻、STM32、硅胶管…），
规格和单价每次重敲既慢又容易错。库里攒的历史直接拿来用。
"""
import unittest

from app.core import repo
from tests.support import EntryCase, TempDataTest


class HistoryCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '')
        self.r1 = repo.create_round('九月采购')
        self.r2 = repo.create_round('十月采购')

    def add(self, name='电容', rid=None, **over):
        data = {'name': name, 'spec': '22uF', 'qty': 1, 'unit': '个',
                'price': 10.0, 'purpose': '用', 'applicant': '张三'}
        data.update(over)
        return repo.add_item(rid or self.r1, self.mid, data)

    def names(self, query='电容'):
        return [h['name'] for h in repo.item_history(query)]


class TestWhatIsMatched(HistoryCase):

    def test_an_exact_name_hit_is_returned(self):
        self.add('电容')
        self.assertEqual(self.names(), ['电容'])

    def test_a_different_name_is_not_returned(self):
        self.add('电容')
        self.assertEqual(self.names('电阻'), [])

    def test_it_is_case_and_space_tolerant_on_input(self):
        self.add('STM32G474')
        self.assertEqual(len(repo.item_history('  STM32G474  ')), 1)

    def test_an_empty_query_gives_nothing(self):
        self.add('电容')
        self.assertEqual(repo.item_history(''), [])
        self.assertEqual(repo.item_history(None), [])

    def test_a_partial_name_does_not_match(self):
        """只认完整同名 —— 模糊匹配容易把「电容」和「大电容」混起来。"""
        self.add('电容')
        self.assertEqual(repo.item_history('电'), [])


class TestItCrossesRounds(HistoryCase):

    def test_history_from_an_older_round_is_included(self):
        self.add('电容', rid=self.r1)
        self.add('电容', rid=self.r2)
        got = repo.item_history('电容')
        self.assertEqual(len(got), 1, '同规格同价应该只留一条')
        self.assertEqual(got[0]['round_name'], '十月采购', '要留最近的那次')

    def test_the_round_name_comes_along(self):
        self.add('电容', rid=self.r1)
        self.assertEqual(repo.item_history('电容')[0]['round_name'], '九月采购')


class TestDeduplication(HistoryCase):

    def test_same_spec_and_price_collapses_to_one(self):
        self.add('电容')
        self.add('电容')
        self.add('电容')
        self.assertEqual(len(repo.item_history('电容')), 1)

    def test_a_different_spec_is_a_separate_entry(self):
        self.add('电容', spec='22uF')
        self.add('电容', spec='47uF')
        self.assertEqual(len(repo.item_history('电容')), 2)

    def test_a_different_price_is_a_separate_entry(self):
        self.add('电容', price=10.0)
        self.add('电容', price=12.5)
        self.assertEqual(len(repo.item_history('电容')), 2)


class TestRefundsAreExcluded(HistoryCase):

    def test_a_refunded_item_is_not_suggested(self):
        """退了说明那次买得不合适，不该再当推荐。"""
        iid = self.add('电容')
        repo.set_refund(iid)
        self.assertEqual(repo.item_history('电容'), [])


class TestLimitAndShape(HistoryCase):

    def test_the_limit_is_respected(self):
        for i in range(8):
            self.add('电容', spec='22uF-%d' % i, price=float(i + 1))
        self.assertEqual(len(repo.item_history('电容', limit=3)), 3)

    def test_it_carries_what_the_form_needs(self):
        self.add('电容', spec='22uF', price=10.0, unit='个',
                 url='https://e.tb.cn/h.abc?tk=xyz')
        row = repo.item_history('电容')[0]
        self.assertEqual(row['spec'], '22uF')
        self.assertEqual(row['price'], 10.0)
        self.assertEqual(row['unit'], '个')
        self.assertTrue(row['url'].startswith('https://e.tb.cn/'))


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestHistoryApi(EntryCase):
    """HTTP 层：**组员也要能用**（他们才是填单的人），只要求已登录。"""

    def seed(self, name='排线', spec='20AWG', price=5.0):
        r = self.alice_c.post('/api/rounds/%d/items' % self.rid,
                              json={'member_id': self.alice, 'name': name, 'spec': spec,
                                    'qty': 1, 'unit': '个', 'price': price,
                                    'purpose': '用', 'applicant': '李四'})
        assert r.status_code == 200, r.text
        return r.json()['item']['id']

    def test_a_member_can_query_history(self):
        self.seed()
        got = self.alice_c.get('/api/items/history', params={'name': '排线'}).json()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['spec'], '20AWG')
        self.assertEqual(got[0]['price'], 5.0)

    def test_purchase_can_query_history(self):
        self.seed()
        got = self.purchase.get('/api/items/history', params={'name': '排线'}).json()
        self.assertEqual(len(got), 1)

    def test_an_anonymous_caller_is_401(self):
        self.seed()
        self.assertEqual(
            self.anon.get('/api/items/history', params={'name': '排线'}).status_code, 401)

    def test_a_missing_name_returns_empty(self):
        self.assertEqual(self.alice_c.get('/api/items/history').json(), [])

    def test_a_non_matching_name_returns_empty(self):
        self.seed()
        self.assertEqual(
            self.alice_c.get('/api/items/history', params={'name': '电阻'}).json(), [])

    def test_the_limit_param_is_clamped(self):
        for i in range(8):
            self._add(self.alice_c, self.alice, name='排线-%d' % i)
        got = self.alice_c.get('/api/items/history',
                               params={'name': '排线-0', 'limit': 999}).json()
        self.assertLessEqual(len(got), 20)


class TestFrontendWiring(unittest.TestCase):
    """界面上得真的查、真的能带出来 —— 否则这功能等于没做。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')

    def test_the_hint_box_exists(self):
        self.assertIn('id="nameHistory"', self.html)

    def test_typing_the_name_triggers_a_lookup(self):
        self.assertIn("$('f_name').addEventListener('input', loadNameHistory)", self.js)
        self.assertIn("'/api/items/history?name='", self.js)

    def test_it_is_debounced_and_drops_stale_results(self):
        """每敲一下就发请求既浪费又乱；名字改了旧结果要丢掉。"""
        seg = self.js.split('function loadNameHistory()')[1].split('\n}')[0]
        self.assertIn('setTimeout', seg)
        self.assertIn('clearTimeout', seg)
        self.assertNotIn('await api(', seg.split('setTimeout')[0])   # 不在输入时同步请求

    def test_clicking_a_suggestion_fills_the_form(self):
        seg = self.js.split('function applyHistory(')[1].split('\n}')[0]
        for field in ("f_spec", "f_price", "f_unit", "f_url"):
            self.assertIn("$('%s').value" % field, seg)

    def test_it_does_not_overwrite_a_purpose_you_already_typed(self):
        seg = self.js.split('function applyHistory(')[1].split('\n}')[0]
        self.assertIn("!($('f_purpose').value || '').trim()", seg)

    def test_the_hint_is_cleared_with_the_form(self):
        seg = self.js.split('function resetForm()')[1].split('\n}')[0]
        self.assertIn('clearNameHistory()', seg)
