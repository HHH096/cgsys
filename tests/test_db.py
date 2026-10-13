# -*- coding: utf-8 -*-
"""P1 数据层：members / rounds / items 的增删改查。"""
import unittest

from app.core import repo
from app.core.db import connect
from tests.support import TempDataTest


class TestMembers(TempDataTest):

    def test_add_and_list(self):
        repo.add_member('张三', '13800000000', is_owner=1)
        repo.add_member('李四')
        got = repo.list_members()
        self.assertEqual([m['name'] for m in got], ['张三', '李四'])
        self.assertEqual(got[0]['phone'], '13800000000')
        self.assertEqual(got[0]['is_owner'], 1)
        self.assertEqual(got[1]['is_owner'], 0)

    def test_duplicate_name_rejected(self):
        repo.add_member('王五')
        with self.assertRaises(Exception):
            repo.add_member('王五')

    def test_blank_name_rejected(self):
        with self.assertRaises(ValueError):
            repo.add_member('   ')


class TestRounds(TempDataTest):

    def test_create_and_get(self):
        rid = repo.create_round('八月采购')
        got = repo.get_round(rid)
        self.assertEqual(got['name'], '八月采购')
        self.assertEqual(got['group_name'], '电气组')
        self.assertEqual(got['status'], 'open')
        self.assertEqual(got['split_threshold'], 1000.0)
        self.assertIsNone(got['locked_at'])

    def test_lock_sets_timestamp(self):
        rid = repo.create_round('八月采购')
        repo.set_round_status(rid, 'locked')
        got = repo.get_round(rid)
        self.assertEqual(got['status'], 'locked')
        self.assertTrue(got['locked_at'])

    def test_blank_name_rejected(self):
        with self.assertRaises(ValueError):
            repo.create_round('')

    def test_list_is_newest_first(self):
        repo.create_round('七月采购')
        repo.create_round('八月采购')
        self.assertEqual([r['name'] for r in repo.list_rounds()], ['八月采购', '七月采购'])


class TestItems(TempDataTest):

    def setUp(self):
        super().setUp()
        self.mid = repo.add_member('张三', '13800000000')
        self.rid = repo.create_round('八月采购')

    def test_add_computes_amounts(self):
        iid = repo.add_item(self.rid, self.mid, {
            'name': '钮子开关', 'spec': '9011两脚两挡', 'qty': 2, 'unit': '个',
            'price': 23, 'freight': 0, 'purpose': '挡位开关',
            'applicant': '张三', 'url': 'https://e.tb.cn/h.8VOOpKHaRc', 'group_name': '电气组',
        })
        it = repo.get_item(iid)
        self.assertEqual(it['subtotal'], 46.0)
        self.assertEqual(it['total'], 46.0)

    def test_freight_is_added_to_total(self):
        iid = repo.add_item(self.rid, self.mid, {'name': '贝塔头', 'qty': 1, 'unit': '套',
                                                 'price': 8.67, 'freight': 3})
        it = repo.get_item(iid)
        self.assertEqual(it['subtotal'], 8.67)
        self.assertEqual(it['total'], 11.67)

    def test_missing_qty_or_price_leaves_amounts_empty(self):
        iid = repo.add_item(self.rid, self.mid, {'name': '待定', 'qty': 1})
        it = repo.get_item(iid)
        self.assertIsNone(it['subtotal'])
        self.assertIsNone(it['total'])

    def test_update_recomputes(self):
        iid = repo.add_item(self.rid, self.mid, {'name': '电阻包', 'qty': 1, 'unit': '套', 'price': 16.5})
        repo.update_item(iid, {'qty': 2})
        it = repo.get_item(iid)
        self.assertEqual(it['subtotal'], 33.0)
        self.assertEqual(it['total'], 33.0)

    def test_seq_increases_within_round(self):
        a = repo.add_item(self.rid, self.mid, {'name': 'A'})
        b = repo.add_item(self.rid, self.mid, {'name': 'B'})
        self.assertEqual([it['name'] for it in repo.list_items(self.rid)], ['A', 'B'])
        self.assertLess(repo.get_item(a)['seq'], repo.get_item(b)['seq'])

    def test_delete(self):
        iid = repo.add_item(self.rid, self.mid, {'name': 'A'})
        repo.delete_item(iid)
        self.assertEqual(repo.list_items(self.rid), [])

    def test_items_are_scoped_to_round(self):
        other = repo.create_round('九月采购')
        repo.add_item(self.rid, self.mid, {'name': '八月的东西'})
        repo.add_item(other, self.mid, {'name': '九月的东西'})
        self.assertEqual([i['name'] for i in repo.list_items(self.rid)], ['八月的东西'])
        self.assertEqual([i['name'] for i in repo.list_items(other)], ['九月的东西'])

    def test_deleting_round_cascades_to_items(self):
        repo.add_item(self.rid, self.mid, {'name': 'A'})
        with connect() as c:
            c.execute('DELETE FROM rounds WHERE id = ?', (self.rid,))
            c.commit()
        self.assertEqual(repo.list_items(self.rid), [])

    def test_group_name_defaults_to_shequ(self):
        iid = repo.add_item(self.rid, self.mid, {'name': 'A', 'qty': 1, 'price': 1})
        self.assertEqual(repo.get_item(iid)['group_name'], '电气组')


if __name__ == '__main__':
    unittest.main(verbosity=2)
