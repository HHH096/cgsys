# -*- coding: utf-8 -*-
"""多人同时用 + 「有发票却没实付金额」这两件事的回归测试。

两个背景都是真事：

* **组员是一起填的**，采购同时在另一头下单、收票。谁都可能正开着几分钟前的页面：
  两个人改同一条、或者一个人点了两下「保存条目」，以前都会留下烂账
  （后按的悄悄覆盖前面的；连点两下就是两条一样的）。
* **9 月那批发票是照片转的 PDF**（没有文字层），金额读不出来 ——
  票挂上了、实付金额一直空着，界面上也看不出少了什么，
  结果对账的「实付合计 / 差额」一直是糊的。
"""
import io
import unittest
from pathlib import Path

from app.core import db, repo
from app.core.db import session
from tests.support import EntryCase, TEST_PDF as PDF, TempDataTest


# ---------------------------------------------------------------- 并发：库这头

class TestWriteLockWaits(TempDataTest):
    """写锁要**等**，不能直接报错。

    自动收信线程 + 组员提交 + 采购改状态是同时在写的，
    默认 5 秒的等待在慢一点的机器上会偶发 `database is locked` ——
    对用户就是一条 500，填的东西白填。
    """

    def test_busy_timeout_is_long_enough(self):
        conn = db.connect()
        try:
            got = conn.execute('PRAGMA busy_timeout').fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(int(got), 10000)

    def test_foreign_keys_are_still_on(self):
        conn = db.connect()
        try:
            self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        finally:
            conn.close()


# ---------------------------------------------------------------- 并发：条目这头

class TestItemRev(TempDataTest):
    """`rev` 只跟着**内容**走，不跟状态走。

    状态（下单 / 发票）是采购在推进流程，组员补资料不该被这个拦下来。
    """

    def setUp(self):
        super().setUp()
        self.rid = repo.create_round('九月')
        self.mid = repo.add_member('李四', '')
        self.iid = repo.add_item(self.rid, self.mid, {
            'name': '电容', 'qty': 1, 'unit': '个', 'price': 22,
            'purpose': 'x', 'applicant': '李四'})

    def rev(self):
        return repo.get_item(self.iid)['rev']

    def test_a_new_item_starts_at_zero(self):
        self.assertEqual(self.rev(), 0)

    def test_saving_content_bumps_it(self):
        repo.update_item(self.iid, {'note': '改一下'})
        self.assertEqual(self.rev(), 1)
        repo.update_item(self.iid, {'note': '再改一下'})
        self.assertEqual(self.rev(), 2)

    def test_changing_only_the_status_does_not_bump_it(self):
        repo.set_item_status(self.iid, order_status='已下单')
        repo.set_item_status(self.iid, invoice_status='已收到')
        self.assertEqual(self.rev(), 0)


class TestConcurrentEditIsCaught(EntryCase):
    """我还在填，别人已经把这条改了 —— 保存要被拦一下，别悄悄覆盖。"""

    def setUp(self):
        super().setUp()
        self.iid = repo.add_item(self.rid, self.alice, {
            'name': '电容', 'qty': 1, 'unit': '个', 'price': 22,
            'purpose': 'x', 'applicant': '李四'})

    def save(self, client, rev, **kw):
        body = {'name': '电容', 'qty': 1, 'unit': '个', 'price': 22,
                'applicant': '李四'}
        body.update(kw)
        if rev is not None:
            body['base_rev'] = rev
        return client.put('/api/items/%d' % self.iid, json=body)

    def test_a_fresh_rev_saves(self):
        rev = repo.get_item(self.iid)['rev']
        r = self.save(self.alice_c, rev, note='我要这个')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.iid)['note'], '我要这个')

    def test_a_stale_rev_is_rejected(self):
        """第一次保存成功之后，第二个拿着旧页面的保存就该被挡。"""
        rev = repo.get_item(self.iid)['rev']
        self.assertEqual(self.save(self.alice_c, rev, note='先到的').status_code, 200)
        r = self.save(self.alice_c, rev, note='后到的（拿着旧页面）')
        self.assertEqual(r.status_code, 409, r.text)
        # 先到的那个没被覆盖
        self.assertEqual(repo.get_item(self.iid)['note'], '先到的')

    def test_without_a_rev_it_is_not_checked(self):
        """不传 base_rev 就不检查 —— 脚本、老页面照旧能用。"""
        self.save(self.alice_c, None, note='一')
        r = self.save(self.alice_c, None, note='二')
        self.assertEqual(r.status_code, 200, r.text)

    def test_purchase_changing_status_does_not_break_a_member_save(self):
        """采购在另一头点「已下单」，组员这边保存不该突然被 409 挡住。"""
        rev = repo.get_item(self.iid)['rev']
        self.assertEqual(
            self.purchase.post('/api/items/%d/status' % self.iid,
                               json={'order_status': '已下单'}).status_code, 200)
        r = self.save(self.alice_c, rev, note='组员补的备注')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.iid)['note'], '组员补的备注')

    def test_it_does_not_write_anything_when_rejected(self):
        rev = repo.get_item(self.iid)['rev']
        self.save(self.alice_c, rev, note='先到的')
        self.save(self.alice_c, rev, note='后到的', price=999)
        self.assertEqual(repo.get_item(self.iid)['price'], 22)


# ---------------------------------------------------------------- 实付金额

class TestPaidEndpoint(EntryCase):
    """补填实付金额：**付款发生在锁定之后**，所以这条接口不看轮次状态。"""

    def setUp(self):
        super().setUp()
        self.mine = self._item(self.alice, '我的电容', 22)
        self.theirs = self._item(self.bob, '别人的芯片', 99)

    def _item(self, member_id, name, price):
        iid = repo.add_item(self.rid, member_id, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': 'x'})
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def test_purchase_can_fill_it(self):
        r = self.purchase.post('/api/items/%d/paid' % self.mine, json={'paid': 22.5})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.mine)['paid'], 22.5)

    def test_a_member_can_fill_his_own(self):
        r = self.alice_c.post('/api/items/%d/paid' % self.mine, json={'paid': 22})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.mine)['paid'], 22.0)

    def test_a_member_cannot_fill_someone_elses(self):
        r = self.alice_c.post('/api/items/%d/paid' % self.theirs, json={'paid': 1})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIsNone(repo.get_item(self.theirs)['paid'])

    def test_an_anonymous_caller_is_rejected(self):
        r = self.anon.post('/api/items/%d/paid' % self.mine, json={'paid': 1})
        self.assertIn(r.status_code, (401, 403), r.text)

    def test_it_still_works_after_the_round_is_locked(self):
        """**这条就是 9 月那批票卡住的地方。**

        收票、付款都在汇总锁定之后，普通保存（PUT /api/items/{id}）那时一律 400，
        金额就再也填不进去了。
        """
        repo.set_round_status(self.rid, 'locked')
        r = self.purchase.post('/api/items/%d/paid' % self.mine, json={'paid': 28.74})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.mine)['paid'], 28.74)
        # 反过来确认：普通保存这时确实是被拒的（说明这条接口不是多余的）
        blocked = self.purchase.put('/api/items/%d' % self.mine,
                                    json={'name': '我的电容', 'note': '锁定后想改'})
        self.assertEqual(blocked.status_code, 400, blocked.text)

    def test_a_negative_amount_is_refused(self):
        r = self.purchase.post('/api/items/%d/paid' % self.mine, json={'paid': -1})
        self.assertEqual(r.status_code, 400, r.text)

    def test_an_empty_value_clears_it(self):
        self.purchase.post('/api/items/%d/paid' % self.mine, json={'paid': 22})
        r = self.purchase.post('/api/items/%d/paid' % self.mine, json={'paid': None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(repo.get_item(self.mine)['paid'])

    def test_a_missing_item_is_a_404(self):
        r = self.purchase.post('/api/items/999999/paid', json={'paid': 1})
        self.assertEqual(r.status_code, 404, r.text)


class TestUploadWithoutAReadableAmount(EntryCase):
    """发票读不出金额（照片 / 扫描件）时要**明说**，别静悄悄留个空。"""

    def setUp(self):
        super().setUp()
        self.iid = repo.add_item(self.rid, self.alice, {
            'name': '折叠椅', 'qty': 1, 'unit': '个', 'price': 28.74,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(self.iid, order_status='已下单')

    def upload(self, client, name='票.pdf', data=PDF):
        return client.post('/api/items/%d/invoices' % self.iid,
                           files={'file': (name, io.BytesIO(data), 'application/pdf')})

    def test_it_asks_for_the_amount(self):
        j = self.upload(self.alice_c).json()
        self.assertTrue(j['need_paid'], j)
        self.assertIsNone(j['item']['paid'])
        # 票还是照常挂上、状态也照常推进
        self.assertEqual(repo.get_item(self.iid)['invoice_status'], '已收到')

    def test_the_amount_can_be_filled_in_right_after(self):
        self.upload(self.alice_c)
        r = self.alice_c.post('/api/items/%d/paid' % self.iid, json={'paid': 28.74})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.iid)['paid'], 28.74)

    def test_re_uploading_an_old_ticket_warns_again(self):
        """同一张票再传一次（老票补传），也别忘了提醒金额还空着。"""
        self.upload(self.alice_c)
        j = self.upload(self.alice_c).json()
        self.assertEqual(j['state'], 'duplicate')
        self.assertTrue(j['need_paid'], j)

    def test_it_does_not_warn_once_the_amount_is_there(self):
        self.alice_c.post('/api/items/%d/paid' % self.iid, json={'paid': 28.74})
        j = self.upload(self.alice_c).json()
        self.assertFalse(j['need_paid'], j)


class TestAssignKeepsBlanksBlank(EntryCase):
    """核对框里把金额留空 = 「还不知道」，不能写成 0。

    0 看着像"这单没花钱"，对账时也分不出「填了 0」和「没填」。
    """

    def setUp(self):
        super().setUp()
        self.a = self._item('电容22uF', 22)
        self.b = self._item('电容10uF', 11)
        self.inv = repo.add_invoice(self.a, self.rid, '票.pdf', '票.pdf',
                                    'invoices/票.pdf', 100)

    def _item(self, name, price):
        iid = repo.add_item(self.rid, self.alice, {
            'name': name, 'qty': 1, 'unit': '个', 'price': price,
            'purpose': 'x', 'applicant': '李四'})
        repo.set_item_status(iid, order_status='已下单')
        return iid

    def test_an_explicit_null_leaves_it_empty(self):
        r = self.purchase.post('/api/invoices/%d/assign' % self.inv, json={'lines': [
            {'item_id': self.a, 'paid': 22.0},
            {'item_id': self.b, 'paid': None},
        ]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(repo.get_item(self.a)['paid'], 22.0)
        self.assertIsNone(repo.get_item(self.b)['paid'])
        # 但两条都挂上了 —— 空金额不等于没挂
        self.assertEqual(sorted(x['id'] for x in repo.items_of_invoice(self.inv)),
                         sorted([self.a, self.b]))


class TestFrontendWiring(unittest.TestCase):
    """前端那几处接线的回归（这些是运行时问题，`node --check` 查不出来）。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / 'app/web/app.js').read_text(encoding='utf-8')
        cls.html = (root / 'app/web/index.html').read_text(encoding='utf-8')
        cls.css = (root / 'app/web/styles.css').read_text(encoding='utf-8')

    def seg(self, name):
        return self.js.split(name)[1].split('\nfunction ')[0]

    def test_submitting_keeps_a_busy_flag(self):
        """点两下「保存条目」不能变成两条一样的。"""
        seg = self.seg('async function submitForm(')
        self.assertIn('state.busy', seg)
        self.assertIn("btn.disabled = true", seg)

    def test_editing_sends_the_rev_it_opened_with(self):
        self.assertIn('state.editRev = Number(it.rev)', self.js)
        seg = self.seg('async function submitForm(')
        self.assertIn('body.base_rev = state.editRev', seg)

    def test_a_conflict_is_explained_and_keeps_what_was_typed(self):
        seg = self.seg('async function onSaveFailed(')
        self.assertIn('409', seg)
        self.assertIn('你填的都还在', seg)

    def test_it_auto_refreshes_but_never_while_typing(self):
        self.assertIn('function autoRefresh(', self.js)
        self.assertIn('AUTO_REFRESH_MS', self.js)
        seg = self.seg('async function autoRefresh(')
        self.assertIn('formDirty()', seg)
        self.assertIn('state.busy', seg)

    def test_the_dirty_check_ignores_the_default_freight_zero(self):
        seg = self.seg('function formDirty(')
        self.assertNotIn("'f_freight'", seg)

    def test_missing_amount_rows_are_flagged_and_fillable(self):
        self.assertIn('function needsPaid(', self.js)
        self.assertIn('function askPaid(', self.js)
        seg = self.seg('function renderItems(')
        self.assertIn('needsPaid(it)', seg)
        self.assertIn("'填金额'", seg)

    def test_the_progress_bar_points_at_them(self):
        seg = self.seg('function renderProgress(')
        self.assertIn('needsPaid', seg)
        self.assertIn('data-view="missing_paid"', seg)

    def test_the_view_button_exists(self):
        self.assertIn('data-view="missing_paid"', self.html)
        self.assertIn('id="c_mp"', self.html)

    def test_it_asks_for_the_amount_after_an_unreadable_upload(self):
        seg = self.seg('async function uploadInvoiceFile(')
        self.assertIn('j.need_paid', seg)
        self.assertIn('askPaidForUpload', seg)

    def test_the_fill_button_has_its_own_style(self):
        self.assertIn('.mini.warnmini', self.css)


if __name__ == '__main__':
    unittest.main(verbosity=2)
