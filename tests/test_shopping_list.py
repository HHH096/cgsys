# -*- coding: utf-8 -*-
"""待购清单：采购人员照着它去淘宝下单。

筛的是「**还要买、而且该由采购去下单**」的那些：
待下单 + 没退货 + 没人垫付。垫付过就说明他自己已经买了，再列出来会重复采购。
"""
import unittest

from app.core import repo
from tests.support import TempDataTest

DIRTY_URL = ('https://e.tb.cn/h.88CPfnvJXJyPNSJ?tk=tGfJT217I8E CZ028')
CLEAN_URL = 'https://e.tb.cn/h.88CPfnvJXJyPNSJ?tk=tGfJT217I8E'


class ShoppingListCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('八月采购')
        self.mid = repo.add_member('张三', '')

    def add(self, name='电容', **over):
        data = {'name': name, 'spec': '22uF', 'qty': 1, 'unit': '个',
                'price': 10.0, 'purpose': '用', 'applicant': '张三'}
        data.update(over)
        return repo.add_item(self.rid, self.mid, data)

    def names(self):
        return [it['name'] for it in repo.shopping_list(self.rid)]


class TestWhoBelongsOnTheList(ShoppingListCase):

    def test_a_pending_item_is_listed(self):
        self.add('电容')
        self.assertEqual(self.names(), ['电容'])

    def test_an_already_ordered_item_is_not_listed(self):
        iid = self.add('电容')
        repo.set_item_status(iid, order_status='已下单')
        self.assertEqual(self.names(), [])

    def test_a_refunded_item_is_not_listed(self):
        """退了就是不要了，别再让人去买一遍。"""
        iid = self.add('电容')
        repo.set_refund(iid)
        self.assertEqual(self.names(), [])

    def test_a_paid_by_someone_else_item_is_not_listed(self):
        """垫付 = 他自己已经买了 —— 列出来会让采购重复下单。"""
        self.add('电容', payer='李四')
        self.assertEqual(self.names(), [])

    def test_a_blank_payer_does_not_exclude_it(self):
        self.add('电容', payer='   ')
        self.assertEqual(self.names(), ['电容'])

    def test_it_lists_only_this_round(self):
        other = repo.create_round('九月采购')
        repo.add_item(other, self.mid, {'name': '别的轮次', 'qty': 1, 'unit': '个',
                                        'price': 1, 'purpose': '用', 'applicant': '张三'})
        self.add('本轮的')
        self.assertEqual(self.names(), ['本轮的'])


class TestTheLinkIsCleaned(ShoppingListCase):

    def test_the_share_text_is_reduced_to_a_real_url(self):
        """实测 73 条条目的网址里存的是整段分享文本，带空格，浏览器打不开。"""
        self.add('电容', url=DIRTY_URL)
        self.assertEqual(repo.shopping_list(self.rid)[0]['link'], CLEAN_URL)

    def test_a_plain_url_is_kept_as_is(self):
        url = 'https://item.taobao.com/item.htm?id=123456'
        self.add('电容', url=url)
        self.assertEqual(repo.shopping_list(self.rid)[0]['link'], url)

    def test_a_supplier_name_gives_an_empty_link(self):
        """只填了商家名称的，没有链接可点 —— 给空串，别编一个。"""
        self.add('电容', url='中发电子市场')
        self.assertEqual(repo.shopping_list(self.rid)[0]['link'], '')

    def test_a_missing_url_gives_an_empty_link(self):
        self.add('电容')
        self.assertEqual(repo.shopping_list(self.rid)[0]['link'], '')

    def test_the_original_url_is_left_untouched(self):
        """只读不改库：清理后的链接是算出来的，不动用户填的原文。"""
        iid = self.add('电容', url=DIRTY_URL)
        repo.shopping_list(self.rid)
        self.assertEqual(repo.get_item(iid)['url'], DIRTY_URL)


class TestTheRowCarriesWhatYouNeedToBuy(ShoppingListCase):

    def test_spec_and_qty_are_kept_verbatim(self):
        """规格要**按原文**显示 —— 「红色20AWG」这种是唯一的下单依据。"""
        self.add('胜牌电线', spec='红色20AWG', qty=100)
        row = repo.shopping_list(self.rid)[0]
        self.assertEqual(row['spec'], '红色20AWG')
        self.assertEqual(row['qty'], 100)
        self.assertEqual(row['price'], 10.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestShoppingListApi(TempDataTest):
    """HTTP 层：采购人员能拿到清单，组员/匿名拿不到。"""

    def setUp(self):
        super().setUp()
        from tests.support import purchase_client
        self.client = purchase_client(self)
        self.rid = self.client.post('/api/rounds', json={'name': '八月采购'}).json()['id']
        self.mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']

    def add(self, name='电容', **over):
        body = {'member_id': self.mid, 'name': name, 'spec': '22uF', 'qty': 2,
                'unit': '个', 'price': 10.0, 'purpose': '用', 'applicant': '张三'}
        body.update(over)
        return self.client.post('/api/rounds/%d/items' % self.rid, json=body).json()['item']

    def test_it_returns_the_clean_link_and_the_spec(self):
        self.add('电容', url=DIRTY_URL)
        got = self.client.get('/api/rounds/%d/shopping-list' % self.rid).json()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['link'], CLEAN_URL)
        self.assertEqual(got[0]['spec'], '22uF')
        self.assertEqual(got[0]['qty'], 2)

    def test_a_paid_item_is_left_out(self):
        self.add('电容', payer='李四')
        self.assertEqual(self.client.get('/api/rounds/%d/shopping-list' % self.rid).json(), [])

    def test_a_missing_round_is_404(self):
        self.assertEqual(self.client.get('/api/rounds/9999/shopping-list').status_code, 404)

    def test_anonymous_is_401(self):
        from fastapi.testclient import TestClient
        from app.server.api import app as the_app
        anon = TestClient(the_app)
        self.assertEqual(anon.get('/api/rounds/%d/shopping-list' % self.rid).status_code, 401)


class TestFrontendWiring(unittest.TestCase):
    """界面上的两条路：逐条点（真链接，可 Ctrl+点批量）、复制清单。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')

    def test_the_card_exists(self):
        for needle in ('id="shopList"', 'id="shopHint"', 'id="shopCopy"', 'id="shopOpenAll"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_the_open_button_is_a_real_link(self):
        """真链接才能 Ctrl+点击 / 中键批量打开 —— 用 button 会被弹窗拦截卡成只开一个。"""
        seg = self.js.split("if (r.link) {")[1].split('} else {')[0]
        self.assertIn("go.href = r.link", seg)
        self.assertIn("go.target = '_blank'", seg)
        self.assertIn("createElement('a')", seg)
        self.assertNotIn('window.open(', seg)     # 只看调用，注释里提到不算

    def test_the_batch_button_does_not_pretend_to_open_everything(self):
        """浏览器一次手势只放行一个窗口，所以它只开第一条，且要说清楚。"""
        self.assertIn('打开第一条', self.html)
        seg = self.js.split('function openAllShop()')[1].split('\n}')[0]
        self.assertIn('按住 Ctrl', seg)
        self.assertNotIn('forEach', seg)      # 不再假装循环全开

    def test_copy_list_is_bound(self):
        self.assertIn("$('shopCopy').addEventListener('click', copyShopList)", self.js)

    def test_spec_is_shown_verbatim(self):
        self.assertIn("'规格：' + r.spec", self.js)
