# -*- coding: utf-8 -*-
"""发票明细 → 采购条目 的匹配：名称优先、金额兜底、配不上留空。"""
import unittest

from app.core import invoicematch as im


class TestNameHit(unittest.TestCase):

    def test_item_name_inside_the_line_name(self):
        """条目名是通用名、票面名长得多的那种 —— 一沾就中。"""
        self.assertEqual(im.name_hit('航空插头DS20J12TQ+DS20', '航空插头'), 2)
        self.assertEqual(im.name_hit('贴片电容1u2.2u4.7u100uF50V', '电容'), 2)

    def test_symbols_and_spaces_do_not_matter(self):
        self.assertEqual(im.name_hit('连接器 PT06E+14', '连接器PT06E'), 2)

    def test_a_brand_name_does_not_match_a_part_number(self):
        """反例：条目写「安费诺」，票面写「连接器PT06E」—— 一个字都不沾。"""
        self.assertEqual(im.name_hit('连接器PT06E-14-19P(SR)', '安费诺'), 0)

    def test_a_single_shared_character_is_not_enough(self):
        """「硅胶管」vs「水管」只有「管」一个字重合 —— 不该算命中。

        「管」太泛（水管 / 气管 / 导管 / 油管），宁可交给人工选，
        也别凭一个字硬配。算法保守是刻意的。
        """
        self.assertEqual(im.name_hit('硅胶管16-23*150', '水管'), 0)

    def test_two_character_overlap_counts_as_partial(self):
        self.assertEqual(im.name_hit('插头插座HVC2P60', '高压插头'), 1)

    def test_model_numbers_do_not_create_false_matches(self):
        """型号里的字母数字凑出来的"重合"是巧合，不能当命中。

        真踩过：「连接器PT06E-14-19P」和「HK4100F-DC24V」能凑出 "24"、"d0"，
        「硅胶管16-23」和「AO3400A」能凑出 "00" —— 于是三条不相干的条目被配上。
        """
        self.assertEqual(im.name_hit('连接器PT06E-14-19P(SR)', 'HK4100F-DC24V-SHG'), 0)
        self.assertEqual(im.name_hit('硅胶管16-23*150', 'AO3400A SOT-23'), 0)
        self.assertEqual(im.name_hit('连接器HVC2P60FV125', 'HK4100F-DC24V-SHG'), 0)

    def test_empty_sides_never_match(self):
        self.assertEqual(im.name_hit('', '电容'), 0)
        self.assertEqual(im.name_hit('电容', ''), 0)


class TestMatchLines(unittest.TestCase):

    def _items(self, *triples):
        return [{'id': i, 'name': n, 'total': t} for n, t, i in triples]

    def test_name_beats_amount(self):
        """名字对得上就赢，哪怕金额差那么一点。"""
        lines = [{'name': '航空插头DS20J12TQ', 'total': 16.2}]
        items = self._items(('航空插头', 15.2, 40), ('电容', 16.2, 44))
        got = im.match_lines(lines, items)
        self.assertEqual(got[0]['item_id'], 40)
        self.assertEqual(got[0]['why'], '名称吻合')

    def test_amount_breaks_a_tie(self):
        """两个都叫「电容」时，金额近的那个赢。"""
        lines = [{'name': '贴片电容1u', 'total': 22.0}]
        items = self._items(('电容', 11.0, 45), ('电容', 22.0, 44))
        self.assertEqual(im.match_lines(lines, items)[0]['item_id'], 44)

    def test_amount_alone_can_match_when_names_do_not(self):
        """名字不沾边但金额很接近 —— 配上，并如实注明依据是金额。"""
        lines = [{'name': '连接器PT06E-14-19P(SR)', 'total': 207.0}]
        items = self._items(('安费诺', 207.0, 36))
        got = im.match_lines(lines, items)
        self.assertEqual(got[0]['item_id'], 36)
        self.assertEqual(got[0]['why'], '金额接近')

    def test_a_wildly_off_amount_is_left_for_a_human(self):
        """名字不沾边、金额又差得远 —— 不硬配，免得填错。"""
        lines = [{'name': '连接器PT06E', 'total': 207.0}]
        items = self._items(('便携式电脑桌', 179.0, 50))
        got = im.match_lines(lines, items)
        self.assertIsNone(got[0]['item_id'])
        self.assertIn('手选', got[0]['why'])

    def test_one_item_is_never_taken_twice(self):
        lines = [{'name': '电容A', 'total': 22.0}, {'name': '电容B', 'total': 22.0}]
        items = self._items(('电容', 22.0, 44))
        got = im.match_lines(lines, items)
        self.assertEqual(got[0]['item_id'], 44)
        self.assertIsNone(got[1]['item_id'])

    def test_a_multi_item_invoice_pairs_each_line(self):
        """一票多单的典型样子：两行各配一条。"""
        lines = [{'name': '贴片电容1u2.2u', 'total': 22.0},
                 {'name': '贴片电容10u', 'total': 11.0}]
        items = self._items(('电容', 11.0, 45), ('电容', 22.0, 44))
        got = im.match_lines(lines, items)
        self.assertEqual([g['item_id'] for g in got], [44, 45])

    def test_no_candidates_means_nothing_matched(self):
        got = im.match_lines([{'name': '电容', 'total': 22.0}], [])
        self.assertIsNone(got[0]['item_id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
