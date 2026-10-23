# -*- coding: utf-8 -*-
"""P1 规则：金额计算、字段校验、按商家拆分。"""
import unittest

from app.core import rules


class TestAmounts(unittest.TestCase):

    def test_subtotal_and_total(self):
        self.assertEqual(rules.compute_amounts({'qty': 2, 'price': 23}), (46.0, 46.0))

    def test_freight_included_in_total(self):
        self.assertEqual(rules.compute_amounts({'qty': 1, 'price': 8.67, 'freight': 3}), (8.67, 11.67))

    def test_missing_qty_or_price_gives_nothing(self):
        self.assertEqual(rules.compute_amounts({'qty': 2}), (None, None))
        self.assertEqual(rules.compute_amounts({'price': 2}), (None, None))

    def test_no_float_noise(self):
        self.assertEqual(rules.compute_amounts({'qty': 3, 'price': 0.1})[0], 0.3)
        self.assertEqual(rules.compute_amounts({'qty': 3, 'price': 35.44})[0], 106.32)

    def test_text_numbers_are_accepted(self):
        self.assertEqual(rules.compute_amounts({'qty': '2', 'price': '23'}), (46.0, 46.0))

    def test_garbage_is_not_a_number(self):
        self.assertIsNone(rules.to_num('abc'))
        self.assertIsNone(rules.to_num(''))
        self.assertIsNone(rules.to_num(None))
        self.assertIsNone(rules.to_num(float('nan')))


class TestValidate(unittest.TestCase):

    def base(self, **kw):
        d = {'name': '钮子开关', 'unit': '个', 'qty': 2, 'price': 23,
             'purpose': '挡位开关', 'applicant': '张三'}
        d.update(kw)
        return d

    def test_valid_item_has_no_errors(self):
        errors, _ = rules.validate(self.base())
        self.assertEqual(errors, [])

    def test_required_fields(self):
        errors, _ = rules.validate({'qty': 1, 'price': 1})
        joined = '；'.join(errors)
        for kw in ('物料名称', '单位', '申购人'):
            self.assertIn(kw, joined)

    def test_qty_must_be_positive(self):
        self.assertTrue(rules.validate(self.base(qty=0))[0])
        self.assertTrue(rules.validate(self.base(qty=-1))[0])
        self.assertTrue(rules.validate(self.base(qty='abc'))[0])

    def test_price_must_be_a_number(self):
        self.assertTrue(rules.validate(self.base(price='abc'))[0])
        self.assertEqual(rules.validate(self.base(price=0))[0], [])

    def test_price_over_1000_warns_about_contract(self):
        errors, warns = rules.validate(self.base(price=1204))
        self.assertEqual(errors, [])
        self.assertTrue(any('合同' in w for w in warns))

    def test_phone_must_be_digits(self):
        self.assertTrue(rules.validate(self.base(phone='abc123'))[0])
        self.assertEqual(rules.validate(self.base(phone='13800000000'))[0], [])
        self.assertEqual(rules.validate(self.base(phone=''))[0], [])

    def test_broken_url_only_warns(self):
        errors, warns = rules.validate(self.base(url='https://nope'))
        self.assertEqual(errors, [])
        self.assertTrue(any('链接' in w for w in warns))

    def test_supplier_name_is_accepted_as_url(self):
        errors, warns = rules.validate(self.base(url='速迫'))
        self.assertEqual((errors, warns), ([], []))


class TestExportNote(unittest.TestCase):
    """备注列要把垫资人带上 —— 车队总表那边没有单独的垫资列。"""

    def test_note_untouched_without_payer(self):
        self.assertEqual(rules.export_value({'note': '急用'}, 'note'), '急用')
        self.assertEqual(rules.export_value({}, 'note'), '')

    def test_payer_is_appended_into_the_note(self):
        self.assertEqual(rules.export_value({'payer': '张三'}, 'note'), '张三垫资')
        self.assertEqual(
            rules.export_value({'note': '急用', 'payer': '张三'}, 'note'),
            '急用 张三垫资')

    def test_refund_mark_still_works_alongside_payer(self):
        it = {'refunded': 1, 'refund_amount': 0, 'paid': 40, 'payer': '张三'}
        self.assertEqual(rules.export_value(it, 'note'), '已退货 张三垫资')
        self.assertEqual(
            rules.export_value({'refunded': 1, 'note': '急用'}, 'note'), '急用（已退货）')

    def test_other_columns_are_unaffected(self):
        it = {'name': '钮子开关', 'payer': '张三', 'paid': 40}
        self.assertEqual(rules.export_value(it, 'name'), '钮子开关')
        self.assertEqual(rules.export_value(it, 'paid'), 40)


class TestSplitBySupplier(unittest.TestCase):
    """用户确认的规则：按一次采购同一商家的总金额。"""

    def it(self, iid, supplier, total, **kw):
        d = {'id': iid, 'url': supplier, 'total': total, 'name': 'x'}
        d.update(kw)
        return d

    def test_single_row_over_threshold_goes_high(self):
        items = [self.it(1, '浩泰科技', 6200)]
        low, high, _ = rules.split_items(items)
        self.assertEqual([i['id'] for i in high], [1])
        self.assertEqual(low, [])

    def test_single_row_under_threshold_goes_low(self):
        items = [self.it(1, '淘宝商家', 46)]
        low, high, _ = rules.split_items(items)
        self.assertEqual([i['id'] for i in low], [1])

    def test_rows_of_one_supplier_stay_together_and_sum(self):
        """两行各 400，同一商家合计 800 < 1000 -> 两行都进「低于一千」。"""
        items = [self.it(1, '速迫', 400), self.it(2, '速迫', 400)]
        low, high, groups = rules.split_items(items)
        self.assertEqual([i['id'] for i in low], [1, 2])
        self.assertEqual(high, [])
        self.assertEqual(groups['url:速迫'], 800.0)

    def test_two_rows_of_one_supplier_cross_the_threshold(self):
        """两行各 600，同一商家合计 1200 >= 1000 -> 两行都进「高于一千」。"""
        items = [self.it(1, '速迫', 600), self.it(2, '速迫', 600)]
        low, high, groups = rules.split_items(items)
        self.assertEqual(high and [i['id'] for i in high], [1, 2])
        self.assertEqual(low, [])
        self.assertEqual(groups['url:速迫'], 1200.0)

    def test_threshold_is_configurable(self):
        items = [self.it(1, 'A', 600)]
        self.assertEqual(len(rules.split_items(items, 1000)[0]), 1)
        self.assertEqual(len(rules.split_items(items, 500)[1]), 1)

    def test_rows_without_supplier_are_not_lumped_together(self):
        """没填商家的行各自独立，不能被加到一起误判成高于一千。"""
        items = [self.it(1, '', 600), self.it(2, '', 600)]
        low, high, _ = rules.split_items(items)
        self.assertEqual([i['id'] for i in low], [1, 2])

    def test_manual_override_wins(self):
        items = [self.it(1, '浩泰科技', 6200, split_override='low'),
                 self.it(2, '淘宝商家', 46, split_override='high')]
        low, high, _ = rules.split_items(items)
        self.assertEqual([i['id'] for i in low], [1])
        self.assertEqual([i['id'] for i in high], [2])

    def test_order_is_preserved(self):
        items = [self.it(1, 'A', 5000), self.it(2, 'B', 10), self.it(3, 'C', 7000)]
        low, high, _ = rules.split_items(items)
        self.assertEqual([i['id'] for i in low], [2])
        self.assertEqual([i['id'] for i in high], [1, 3])

    def test_electrical_group_august_case(self):
        """8 月那份实测数据：按商家聚合后，速迫 500 元那行会落到「低于一千」。

        这与当年的实际归档（它进了「高于一千」）不一致 —— 只有「速迫」在本轮
        跨所有组的合计 >= 1000 时才吻合。这条偏差在设计说明里留了档。
        """
        items = [
            self.it(1, '商家诺可得商家', 2100),
            self.it(2, '淘宝商家', 1204),
            self.it(3, '速迫', 500),
            self.it(4, '浩泰科技', 6200),
        ]
        low, high, groups = rules.split_items(items)
        self.assertEqual([i['id'] for i in high], [1, 2, 4])
        self.assertEqual([i['id'] for i in low], [3])
        self.assertEqual(groups['url:速迫'], 500.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestDangerousUrlScheme(TestValidate):
    """「网址链接」组员能随手填、采购会点开 —— 危险协议必须在入口就拒掉。

    `javascript:` 开头的链接会被渲染成 <a href>，采购一点就在自己电脑上执行脚本。
    """

    def errs(self, url):
        return rules.validate(self.base(url=url))[0]

    def test_javascript_scheme_is_rejected(self):
        self.assertTrue(self.errs('javascript:alert(1)'))

    def test_uppercase_javascript_scheme_is_rejected(self):
        self.assertTrue(self.errs('JavaScript:alert(1)'))

    def test_data_scheme_is_rejected(self):
        self.assertTrue(self.errs('data:text/html,<script>alert(1)</script>'))

    def test_vbscript_and_file_are_rejected(self):
        self.assertTrue(self.errs('vbscript:msgbox'))
        self.assertTrue(self.errs('file:///C:/Windows/notepad.exe'))

    def test_a_normal_http_link_is_still_fine(self):
        self.assertEqual(self.errs('https://item.taobao.com/x'), [])
        self.assertEqual(self.errs('http://m.tb.cn/abc'), [])

    def test_a_supplier_name_is_still_fine(self):
        """不填链接、直接写商家名称是允许的（一直如此）。"""
        self.assertEqual(self.errs('中发电子市场'), [])

    def test_a_colon_in_a_plain_name_is_not_a_scheme_complaint(self):
        self.assertEqual(self.errs('机箱:先马'), [])


class TestRefundedRowsDoNotAffectTheSplit(unittest.TestCase):
    """拆单阈值按**净额**算：退掉的行不该把同商家推过一千。"""

    def it(self, name, total, refunded=False, refund=0.0, url='同商家'):
        return {'name': name, 'url': url, 'total': total,
                'refunded': refunded, 'refund_amount': refund}

    def test_a_full_refund_frees_the_other_rows(self):
        items = [self.it('退掉的', 900, True, 900), self.it('新买的', 300)]
        low, high, totals = rules.split_items(items, threshold=1000)
        self.assertEqual([x['name'] for x in high], [])
        self.assertEqual([x['name'] for x in low], ['退掉的', '新买的'])
        self.assertEqual(list(totals.values())[0], 300.0)

    def test_a_partial_refund_only_subtracts_the_returned_part(self):
        items = [self.it('部分退', 600, True, 200), self.it('新买的', 300)]
        low, high, totals = rules.split_items(items, threshold=1000)
        self.assertEqual(list(totals.values())[0], 700.0, '600-200+300')
        self.assertEqual(len(low), 2, '700 还没到一千')

    def test_without_refunds_behaviour_is_unchanged(self):
        items = [self.it('甲', 600), self.it('乙', 500)]
        _, high, totals = rules.split_items(items, threshold=1000)
        self.assertEqual(list(totals.values())[0], 1100.0)
        self.assertEqual(len(high), 2)
