# -*- coding: utf-8 -*-
"""粘贴分享文案的解析。"""
import unittest

from app.core import shareparse


def parse(text):
    return shareparse.parse_share_text(text)


class TestUrl(unittest.TestCase):

    def test_plain_url(self):
        self.assertEqual(shareparse.find_url('https://e.tb.cn/h.8VOOpKHaRc'),
                         'https://e.tb.cn/h.8VOOpKHaRc')

    def test_url_keeps_its_query(self):
        url = 'https://m.tb.cn/h.gXkLmNo?tk=AbCdEfG'
        self.assertEqual(shareparse.find_url('【淘宝】%s 「东西」' % url), url)

    def test_url_stops_at_chinese_punctuation(self):
        self.assertEqual(shareparse.find_url('看这个https://e.tb.cn/h.abc，挺不错'),
                         'https://e.tb.cn/h.abc')

    def test_url_trailing_ascii_punctuation_is_trimmed(self):
        self.assertEqual(shareparse.find_url('https://e.tb.cn/h.abc.'), 'https://e.tb.cn/h.abc')

    def test_no_url(self):
        self.assertEqual(shareparse.find_url('钮子开关 9011'), '')


class TestPrice(unittest.TestCase):

    def test_none_when_absent(self):
        value, src = shareparse.find_price('【淘宝】https://e.tb.cn/h.abc 「钮子开关」')
        self.assertIsNone(value)
        self.assertEqual(src, '')

    def test_currency_symbol(self):
        self.assertEqual(shareparse.find_price('钮子开关 ¥23.5 「x」')[0], 23.5)

    def test_yuan_suffix(self):
        self.assertEqual(shareparse.find_price('运费 80 元')[0], 80.0)

    def test_label_wins_over_plain_symbol(self):
        """同时有原价和实付时，取实付。"""
        value, src = shareparse.find_price('原价 ¥99.00，实付款 ¥500.00')
        self.assertEqual((value, src), (500.0, '实付'))

    def test_after_coupon_label(self):
        value, src = shareparse.find_price('券后价 23.50')
        self.assertEqual((value, src), (23.5, '到手价'))

    def test_list_price_is_labelled_as_such(self):
        value, src = shareparse.find_price('价格 23.00')
        self.assertEqual((value, src), (23.0, '售价'))

    def test_bare_symbol_is_only_a_list_price(self):
        value, src = shareparse.find_price('钮子开关 ¥23.5 「x」')
        self.assertEqual((value, src), (23.5, '标价'))

    def test_paid_beats_coupon_price(self):
        """有实付就用实付，别拿券后价当实付。"""
        value, src = shareparse.find_price('券后价 23.50，实付款 25.00')
        self.assertEqual((value, src), (25.0, '实付'))

    def test_zero_is_ignored(self):
        self.assertIsNone(shareparse.find_price('免运费 ¥0')[0])


class TestTaobaoShare(unittest.TestCase):

    def test_standard_share(self):
        r = parse('【淘宝】https://m.tb.cn/h.gXkLmNo?tk=AbCdEfG 「不锈钢螺丝 M3*45 304」\n'
                  '点击链接直接打开 或者 淘宝搜索直接打开')
        self.assertEqual(r['url'], 'https://m.tb.cn/h.gXkLmNo?tk=AbCdEfG')
        self.assertEqual(r['name'], '不锈钢螺丝 M3*45 304')
        self.assertIsNone(r['price'])

    def test_short_link_without_query(self):
        r = parse('【淘宝】https://e.tb.cn/h.8VOOpKHaRc 「钮子开关 9011两脚两挡」')
        self.assertEqual(r['url'], 'https://e.tb.cn/h.8VOOpKHaRc')
        self.assertEqual(r['name'], '钮子开关 9011两脚两挡')

    def test_platform_tag_is_not_the_name(self):
        r = parse('【淘宝】https://e.tb.cn/h.abc')
        self.assertEqual(r['name'], '')
        self.assertTrue(any('名称' in n for n in r['notes']))

    def test_token_is_stripped(self):
        r = parse('7₴AbCdEfG₴ https://m.tb.cn/h.gXkLmNo 「钮子开关 9011两脚两挡」')
        self.assertEqual(r['url'], 'https://m.tb.cn/h.gXkLmNo')
        self.assertEqual(r['name'], '钮子开关 9011两脚两挡')

    def test_trailing_hint_is_not_part_of_the_name(self):
        r = parse('https://m.tb.cn/h.abc 「排线 0.1、0.2、0.3」 复制这条信息打开手机淘宝')
        self.assertEqual(r['name'], '排线 0.1、0.2、0.3')

    def test_title_without_quotes(self):
        r = parse('钮子开关 9011两脚两挡\nhttps://e.tb.cn/h.abc')
        self.assertEqual(r['name'], '钮子开关 9011两脚两挡')
        self.assertEqual(r['url'], 'https://e.tb.cn/h.abc')

    def test_full_width_spaces_are_handled(self):
        r = parse('【淘宝】\u3000https://e.tb.cn/h.abc\u3000「\u3000绝缘手套\u3000」')
        self.assertEqual(r['name'], '绝缘手套')

    def test_order_share_with_price(self):
        r = parse('我在淘宝买了「绝缘手套 高压 2 双」，实付款 ¥500.00\n'
                  'https://detail.tmall.com/item.htm?id=123456')
        self.assertEqual(r['name'], '绝缘手套 高压 2 双')
        self.assertEqual(r['price'], 500.0)
        self.assertEqual(r['url'], 'https://detail.tmall.com/item.htm?id=123456')


class TestXianyuShare(unittest.TestCase):

    def test_nested_template(self):
        r = parse('【闲鱼】https://m.tb.cn/h.gXXXX 「我在闲鱼发布了【15寸轮辋 铝合金】」')
        self.assertEqual(r['name'], '15寸轮辋 铝合金')
        self.assertEqual(r['url'], 'https://m.tb.cn/h.gXXXX')

    def test_template_without_nesting(self):
        r = parse('【闲鱼】https://m.tb.cn/h.gXXXX 「我在闲鱼发布了【碳纤维防撞块】」')
        self.assertEqual(r['name'], '碳纤维防撞块')

    def test_manual_copy_without_quotes(self):
        r = parse('15寸轮辋 铝合金 8J\n【闲鱼】https://m.tb.cn/h.gXXXX')
        self.assertEqual(r['name'], '15寸轮辋 铝合金 8J')


class TestOtherPlatforms(unittest.TestCase):

    def test_pinduoduo_only_url(self):
        r = parse('【拼多多】https://mobile.yangkeduo.com/goods.html?goods_id=123456')
        self.assertEqual(r['url'], 'https://mobile.yangkeduo.com/goods.html?goods_id=123456')
        self.assertEqual(r['name'], '')
        self.assertTrue(any('名称' in n for n in r['notes']))

    def test_jd_with_title(self):
        r = parse('【京东】https://item.jd.com/100012043978.html 「棘轮扳手套筒套装」')
        self.assertEqual(r['name'], '棘轮扳手套筒套装')


class TestDegenerateInput(unittest.TestCase):

    def test_empty(self):
        r = parse('')
        self.assertEqual(r['name'], '')
        self.assertEqual(r['url'], '')
        self.assertEqual(r['price'], None)
        self.assertIn('没有内容可解析', r['notes'])

    def test_none(self):
        self.assertEqual(parse(None)['name'], '')

    def test_whitespace_only(self):
        self.assertEqual(parse('   \n  ')['name'], '')

    def test_plain_supplier_name(self):
        """直接填商家名（不是链接）也要能用。"""
        r = parse('河北速迫科技有限公司')
        self.assertEqual(r['name'], '河北速迫科技有限公司')
        self.assertEqual(r['url'], '')
        self.assertTrue(any('链接' in n for n in r['notes']))

    def test_never_raises_on_garbage(self):
        for junk in ('￥￥￥', '【】', 'http://', '。。。。', '₴₴₴', '\x00\x01', 'a' * 500):
            parse(junk)      # 不抛异常即可


class TestNameCleanup(unittest.TestCase):

    def test_noise_phrases_removed(self):
        self.assertEqual(shareparse.clean_name('排线 点击链接直接打开 或者 淘宝搜索直接打开'),
                         '排线')

    def test_quotes_removed(self):
        self.assertEqual(shareparse.clean_name('「钮子开关」'), '钮子开关')

    def test_whitespace_collapsed(self):
        self.assertEqual(shareparse.clean_name('钮子  开关   9011'), '钮子 开关 9011')

    def test_leading_trailing_punctuation_trimmed(self):
        self.assertEqual(shareparse.clean_name('，钮子开关。'), '钮子开关')

    def test_length_is_capped(self):
        out = shareparse.clean_name('钮' * 500)
        self.assertEqual(len(out), shareparse.MAX_NAME_LEN)

    def test_picked_name_is_cleaned_too(self):
        r = parse('【淘宝】https://e.tb.cn/h.abc 「钮子开关」 复制这条信息')
        self.assertEqual(r['name'], '钮子开关')
        self.assertFalse(r['name'].startswith('【'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
