# -*- coding: utf-8 -*-
"""POST /api/parse-share，以及前端接线没被误删。"""
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.server.api import app
from tests.support import TempDataTest, purchase_client

ROOT = Path(__file__).resolve().parent.parent


class TestParseShareApi(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)

    def post(self, text):
        return self.client.post('/api/parse-share', json={'text': text})

    def test_standard_taobao_share(self):
        r = self.post('【淘宝】https://m.tb.cn/h.gXkLmNo?tk=AbCdEfG '
                      '「不锈钢螺丝 M3*45 304」\n点击链接直接打开 或者 淘宝搜索直接打开')
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual(j['url'], 'https://m.tb.cn/h.gXkLmNo?tk=AbCdEfG')
        self.assertEqual(j['name'], '不锈钢螺丝 M3*45 304')
        self.assertIsNone(j['price'])
        self.assertTrue(any('价格' in n for n in j['notes']))

    def test_order_share_yields_paid_amount(self):
        j = self.post('我在淘宝买了「绝缘手套 高压 2 双」，实付款 ¥500.00\n'
                      'https://detail.tmall.com/item.htm?id=123456').json()
        self.assertEqual(j['name'], '绝缘手套 高压 2 双')
        self.assertEqual(j['price'], 500.0)
        self.assertEqual(j['price_from'], '实付')

    def test_list_price_is_marked_as_such(self):
        j = self.post('钮子开关 ¥23.5 「x」').json()
        self.assertEqual((j['price'], j['price_from']), (23.5, '标价'))

    def test_xianyu_nested_title(self):
        j = self.post('【闲鱼】https://m.tb.cn/h.gXXXX 「我在闲鱼发布了【15寸轮辋 铝合金】」').json()
        self.assertEqual(j['name'], '15寸轮辋 铝合金')
        self.assertEqual(j['url'], 'https://m.tb.cn/h.gXXXX')

    def test_empty_text_returns_200_not_500(self):
        r = self.post('')
        self.assertEqual(r.status_code, 200)
        self.assertIn('没有内容可解析', r.json()['notes'])

    def test_missing_field_is_treated_as_empty(self):
        r = self.client.post('/api/parse-share', json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['name'], '')

    def test_garbage_never_500s(self):
        for junk in ('￥￥￥', '【】', 'http://', '₴₴₴', 'x' * 300, '\n\n'):
            self.assertEqual(self.post(junk).status_code, 200, repr(junk))

    def test_raw_text_is_echoed_back(self):
        text = '【淘宝】https://e.tb.cn/h.abc 「排线」'
        self.assertEqual(self.post(text).json()['raw'], text)


class TestFrontendWiring(unittest.TestCase):
    """识别按钮真的接上了（防止以后误删）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')

    def test_paste_box_exists(self):
        for needle in ('id="shareText"', 'id="parseShare"', 'id="parseMsg"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_button_is_bound(self):
        self.assertIn('parseShare', self.js)
        self.assertIn("addEventListener('click', parseShare)", self.js)

    def test_calls_the_api(self):
        self.assertIn("'/api/parse-share'", self.js)

    def test_price_goes_to_price_or_paid_depending_on_source(self):
        self.assertIn("'f_paid'", self.js, '实付类价格应填到实付金额')
        self.assertIn("'f_price'", self.js, '标价类价格应填到预计单价')
        self.assertIn('到手价', self.js)

    def test_reset_clears_the_paste_box(self):
        self.assertIn("$('shareText').value = ''", self.js)


if __name__ == '__main__':
    unittest.main(verbosity=2)
