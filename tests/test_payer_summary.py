# -*- coding: utf-8 -*-
"""垫付结算：谁自己先掏了多少钱、该给谁转账。

`payer` 一直只是行上的一个小标签，从来没汇总过 ——
可钱的事总得有人问一句「这轮谁垫了多少」。这里按人聚合。
"""
import unittest

from app.core import repo
from tests.support import TempDataTest


class PayerCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('九月采购')
        self.mid = repo.add_member('张三', '')

    def add(self, price, payer='', paid=None, **over):
        data = {'name': '电容', 'qty': 1, 'unit': '个', 'price': price,
                'purpose': '用', 'applicant': '张三', 'payer': payer}
        if paid is not None:
            data['paid'] = paid
        data.update(over)
        return repo.add_item(self.rid, self.mid, data)

    def summary(self):
        return {r['payer']: r for r in repo.payer_summary(self.rid)}


class TestWhoIsAggregated(PayerCase):

    def test_one_person_one_row(self):
        self.add(22, payer='李四', paid=22)
        self.add(9, payer='李四', paid=9)
        got = self.summary()['李四']
        self.assertEqual(got['count'], 2)
        self.assertEqual(got['amount'], 31.0)

    def test_two_people_are_separate(self):
        self.add(22, payer='李四', paid=22)
        self.add(9, payer='王五', paid=9)
        got = self.summary()
        self.assertEqual(set(got), {'李四', '王五'})

    def test_items_without_a_payer_are_ignored(self):
        self.add(22, payer='')
        self.add(9, payer='   ')
        self.assertEqual(repo.payer_summary(self.rid), [])

    def test_it_counts_the_rows_each_person_paid(self):
        self.add(1, payer='李四', paid=1)
        self.add(2, payer='李四', paid=2)
        self.add(3, payer='王五', paid=3)
        got = self.summary()
        self.assertEqual(got['李四']['count'], 2)
        self.assertEqual(got['王五']['count'], 1)


class TestAmounts(PayerCase):

    def test_it_uses_paid_not_the_estimate(self):
        """垫的是**实际掏的钱**，不是预计单价。"""
        self.add(price=100, payer='李四', paid=88.5)
        self.assertEqual(self.summary()['李四']['amount'], 88.5)

    def test_a_refund_is_netted_out(self):
        """退了货的按净额算 —— 不然会把退回来的钱也算成垫付。"""
        iid = self.add(price=22, payer='李四', paid=22)
        repo.set_refund(iid, refund_amount=22)
        self.assertEqual(self.summary()['李四']['amount'], 0.0)

    def test_a_partial_refund_keeps_the_rest(self):
        iid = self.add(price=100, payer='李四', paid=100)
        repo.set_refund(iid, refund_amount=30)
        self.assertEqual(self.summary()['李四']['amount'], 70.0)

    def test_no_paid_yet_counts_as_zero(self):
        self.add(price=22, payer='李四')
        self.assertEqual(self.summary()['李四']['amount'], 0.0)

    def test_it_does_not_mix_rounds(self):
        other = repo.create_round('十月采购')
        repo.add_item(other, self.mid, {'name': '别的', 'qty': 1, 'unit': '个',
                                        'price': 50, 'purpose': '用',
                                        'applicant': '张三', 'payer': '李四',
                                        'paid': 50})
        self.add(price=22, payer='李四', paid=22)
        self.assertEqual(self.summary()['李四']['amount'], 22.0)


class TestOrdering(PayerCase):

    def test_the_biggest_payer_comes_first(self):
        self.add(10, payer='小额', paid=10)
        self.add(300, payer='大额', paid=300)
        self.add(50, payer='中等', paid=50)
        self.assertEqual([r['payer'] for r in repo.payer_summary(self.rid)],
                         ['大额', '中等', '小额'])

    def test_equal_amounts_sort_by_name(self):
        self.add(10, payer='乙', paid=10)
        self.add(10, payer='甲', paid=10)
        names = [r['payer'] for r in repo.payer_summary(self.rid)]
        self.assertEqual(names, sorted(names), '同额时顺序要稳定')


if __name__ == '__main__':
    unittest.main(verbosity=2)
