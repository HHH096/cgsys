# -*- coding: utf-8 -*-
"""P1 Web 层：API 路由与静态前端。"""
import unittest

from fastapi.testclient import TestClient

from app.server.api import app
from tests.support import TempDataTest, purchase_client

ITEM = {
    'name': '充电机', 'spec': '3.3kwh', 'qty': 1, 'unit': '个', 'price': 6200,
    'freight': 0, 'purpose': '高压电池充电', 'applicant': '张三',
    'phone': '13800000000', 'url': '浩泰科技', 'group_name': '电气组', 'paid': 6200,
}


class ApiCase(TempDataTest):

    def setUp(self):
        super().setUp()
        self.client = purchase_client(self)

    def make_round(self, name='八月采购'):
        r = self.client.post('/api/rounds', json={'name': name})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['id']


class TestStatic(ApiCase):

    def test_index_is_served(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('采购系统', r.text)
        self.assertIn('app.js', r.text)

    def test_assets_are_served(self):
        for path in ('/app.js', '/styles.css'):
            self.assertEqual(self.client.get(path).status_code, 200, path)


class TestMembersApi(ApiCase):

    def test_empty_then_add(self):
        self.assertEqual(self.client.get('/api/members').json(), [])
        r = self.client.post('/api/members', json={'name': '张三', 'phone': '13800000000'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['name'], '张三')
        got = self.client.get('/api/members').json()
        self.assertEqual([m['name'] for m in got], ['张三'])

    def test_duplicate_is_rejected(self):
        self.client.post('/api/members', json={'name': '张三'})
        r = self.client.post('/api/members', json={'name': '张三'})
        self.assertEqual(r.status_code, 400)

    def test_delete(self):
        mid = self.client.post('/api/members', json={'name': '王五'}).json()['id']
        self.assertEqual(self.client.delete('/api/members/%d' % mid).status_code, 200)
        self.assertEqual(self.client.get('/api/members').json(), [])


class TestRoundsApi(ApiCase):

    def test_create_and_list(self):
        rid = self.make_round()
        detail = self.client.get('/api/rounds/%d' % rid).json()
        self.assertEqual(detail['round']['name'], '八月采购')
        self.assertEqual(detail['round']['group_name'], '电气组')
        self.assertEqual(detail['round']['status'], 'open')
        self.assertEqual(detail['items'], [])
        self.assertEqual(detail['split']['all_sum'], 0.0)
        self.assertEqual(self.client.get('/api/rounds').json()[0]['id'], rid)

    def test_blank_name_is_rejected(self):
        self.assertEqual(self.client.post('/api/rounds', json={'name': '  '}).status_code, 400)

    def test_missing_round_is_404(self):
        self.assertEqual(self.client.get('/api/rounds/9999').status_code, 404)

    def test_lock_then_reopen(self):
        rid = self.make_round()
        r = self.client.post('/api/rounds/%d/status' % rid, json={'status': 'locked'})
        self.assertEqual(r.json()['status'], 'locked')
        self.assertTrue(r.json()['locked_at'])
        self.assertEqual(self.client.get('/api/rounds/%d' % rid).json()['round']['status'], 'locked')

    def test_unknown_status_is_rejected(self):
        rid = self.make_round()
        self.assertEqual(
            self.client.post('/api/rounds/%d/status' % rid, json={'status': 'wat'}).status_code, 400)


class TestItemsApi(ApiCase):

    def setUp(self):
        super().setUp()
        self.rid = self.make_round()
        self.mid = self.client.post('/api/members', json={'name': '张三'}).json()['id']

    def add(self, **over):
        body = dict(ITEM)
        body.update(over)
        body['member_id'] = self.mid
        return self.client.post('/api/rounds/%d/items' % self.rid, json=body)

    def test_add_returns_item_and_amounts(self):
        r = self.add()
        self.assertEqual(r.status_code, 200, r.text)
        it = r.json()['item']
        self.assertEqual(it['subtotal'], 6200.0)
        self.assertEqual(it['total'], 6200.0)
        self.assertEqual(it['group_name'], '电气组')

    def test_validation_error_is_400_with_message(self):
        r = self.add(name='', unit='')
        self.assertEqual(r.status_code, 400)
        self.assertIn('物料名称', r.json()['detail'])

    def test_price_over_1000_returns_warning(self):
        r = self.add(price=1204, name='毫欧表')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any('合同' in w for w in r.json()['warnings']))

    def test_update_recomputes_amounts(self):
        iid = self.add().json()['item']['id']
        r = self.client.put('/api/items/%d' % iid, json=dict(ITEM, member_id=self.mid, qty=2))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['item']['subtotal'], 12400.0)

    def test_delete(self):
        iid = self.add().json()['item']['id']
        self.assertEqual(self.client.delete('/api/items/%d' % iid).status_code, 200)
        self.assertEqual(self.client.get('/api/rounds/%d/items' % self.rid).json(), [])

    def test_locked_round_rejects_add_update_delete(self):
        iid = self.add().json()['item']['id']
        self.client.post('/api/rounds/%d/status' % self.rid, json={'status': 'locked'})
        self.assertEqual(self.add(name='新东西').status_code, 400)
        self.assertEqual(self.client.put('/api/items/%d' % iid, json=dict(ITEM, member_id=self.mid)).status_code, 400)
        self.assertEqual(self.client.delete('/api/items/%d' % iid).status_code, 400)

    def test_split_preview_reflects_supplier_rule(self):
        self.add()                                                   # 浩泰科技 6200 -> 高
        self.add(name='绝缘手套', url='速迫', qty=2, price=250)        # 速迫 500 -> 低
        split = self.client.get('/api/rounds/%d/preview' % self.rid).json()
        self.assertEqual(split['high_count'], 1)
        self.assertEqual(split['low_count'], 1)
        self.assertEqual(split['high_sum'], 6200.0)
        self.assertEqual(split['low_sum'], 500.0)
        self.assertEqual(split['all_sum'], 6700.0)
        self.assertEqual(split['threshold'], 1000.0)

    def test_export_downloads_xlsx(self):
        self.add()
        r = self.client.get('/api/rounds/%d/export' % self.rid)
        self.assertEqual(r.status_code, 200)
        self.assertIn('spreadsheetml', r.headers['content-type'])
        self.assertEqual(r.content[:2], b'PK', 'xlsx 应是一个 zip 包')

    def test_export_empty_round_is_400(self):
        self.assertEqual(self.client.get('/api/rounds/%d/export' % self.rid).status_code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
