# -*- coding: utf-8 -*-
"""两个入口：`/` 给组员、`/purchase` 给采购人员；以及组员只看得到自己的条目。"""
import unittest
from pathlib import Path

from tests.support import EntryCase, TEST_PDF as PDF

ROOT = Path(__file__).resolve().parent.parent


class TestEntryPoints(EntryCase):

    def test_member_entry_serves_the_page(self):
        r = self.anon.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/html', r.headers['content-type'])
        self.assertIn('采购系统', r.text)

    def test_purchase_entry_serves_the_same_page(self):
        """两个入口是同一份 HTML，靠路径决定模式 —— 不维护两份界面。"""
        a = self.anon.get('/')
        b = self.anon.get('/purchase')
        self.assertEqual(b.status_code, 200)
        self.assertEqual(a.text, b.text)

    def test_purchase_entry_needs_no_login_to_load(self):
        """页面本身可以直接打开（未登录时前端显示登录门挡住工作台）。

        真正的拦截在接口层：藏页面是挡不住直接调接口的。
        """
        self.assertEqual(self.anon.get('/purchase').status_code, 200)

    def test_assets_still_served(self):
        for path in ('/app.js', '/styles.css'):
            self.assertEqual(self.anon.get(path).status_code, 200, path)

    def test_frontend_is_not_cached_hard(self):
        """静态资源本身也不该被硬缓存。"""
        for path in ('/app.js', '/styles.css'):
            r = self.anon.get(path)
            self.assertIn('no-cache', r.headers.get('cache-control', ''),
                          '%s 应当要求先验证缓存' % path)

    def test_page_references_are_versioned(self):
        """**真正管用的是这个**：css/js 的 URL 带上文件修改时间。

        浏览器握着旧副本时，URL 一变它就必然重新下载。
        「组员入口上却显示采购区块、还报需要登录」那个 bug 就是靠它根治的 ——
        光靠 Cache-Control 拦不住加这个头之前就已经缓存下来的副本。
        """
        import re

        r = self.anon.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('no-store', r.headers.get('cache-control', ''))
        for name in ('styles.css', 'app.js'):
            m = re.search(r'%s\?v=(\d+)' % re.escape(name), r.text)
            self.assertIsNotNone(m, '页面里的 %s 没带版本号' % name)
            stamp = int((ROOT / 'app/web' / name).stat().st_mtime)
            self.assertEqual(int(m.group(1)), stamp,
                             '%s 的版本号应当就是它的修改时间' % name)

    def test_both_entries_get_the_versioned_page(self):
        for path in ('/', '/purchase'):
            self.assertIn('app.js?v=', self.anon.get(path).text, path)


class TestMemberSeesTheWholeRound(EntryCase):
    """组员看得到全组的条目（省得买重、方便互相参考）。

    但**看得到 ≠ 改得了** —— 这条是这个类里最重要的一组断言。
    """

    def test_items_endpoint_shows_everyone(self):
        mine = self._add(self.alice_c, self.alice, '我填的')
        theirs = self._add(self.bob_c, self.bob, '别人填的')

        got = self.alice_c.get('/api/rounds/%d/items' % self.rid).json()
        self.assertEqual(sorted(i['id'] for i in got), sorted([mine, theirs]))

    def test_round_detail_shows_everyone_too(self):
        """轮次详情里也带着 items，这条旁路同样不能漏。"""
        self._add(self.alice_c, self.alice, '我填的')
        self._add(self.bob_c, self.bob, '别人填的')

        d = self.alice_c.get('/api/rounds/%d' % self.rid).json()
        self.assertEqual(sorted(i['name'] for i in d['items']), ['别人填的', '我填的'])

    def test_a_locked_client_sees_nothing(self):
        """没解锁就看不到任何东西 —— 服务可能挂在公网上，这是底线。"""
        self._add(self.alice_c, self.alice)
        self._add(self.bob_c, self.bob)
        r = self.anon.get('/api/rounds/%d/items' % self.rid)
        self.assertEqual(r.status_code, 401)

    def test_seeing_is_not_editing(self):
        """最要紧的一条：看得见，但动不了。"""
        theirs = self._add(self.bob_c, self.bob, '别人填的')
        self.assertEqual(
            self.alice_c.put('/api/items/%d' % theirs,
                             json={'name': '我改'}).status_code, 403)
        self.assertEqual(
            self.alice_c.delete('/api/items/%d' % theirs).status_code, 403)

    def test_cannot_upload_onto_someone_elses_item(self):
        theirs = self._add(self.bob_c, self.bob)
        r = self.alice_c.post('/api/items/%d/invoices' % theirs,
                              files={'file': ('x.pdf', PDF, 'application/pdf')})
        self.assertEqual(r.status_code, 403)

    def test_can_open_someone_elses_invoice(self):
        """发票是条目的一部分：既然条目看得见，发票也该点得开。"""
        theirs = self._add(self.bob_c, self.bob)
        r = self.bob_c.post('/api/items/%d/invoices' % theirs,
                            files={'file': ('x.pdf', PDF, 'application/pdf')})
        self.assertEqual(r.status_code, 200, r.text)
        inv = r.json()['invoice']['id']
        self.assertEqual(self.alice_c.get('/api/invoices/%d/file' % inv).status_code, 200)

    def test_but_cannot_delete_someone_elses_invoice(self):
        theirs = self._add(self.bob_c, self.bob)
        r = self.bob_c.post('/api/items/%d/invoices' % theirs,
                            files={'file': ('x.pdf', PDF, 'application/pdf')})
        inv = r.json()['invoice']['id']
        self.assertEqual(self.alice_c.delete('/api/invoices/%d' % inv).status_code, 403)

    def test_progress_still_works(self):
        self._add(self.alice_c, self.alice)
        self._add(self.bob_c, self.bob)
        p = self.alice_c.get('/api/rounds/%d/progress' % self.rid).json()
        self.assertEqual(p['total'], 2)


class TestFrontEndWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / 'app/web/index.html').read_text(encoding='utf-8')
        cls.js = (ROOT / 'app/web/app.js').read_text(encoding='utf-8')
        cls.css = (ROOT / 'app/web/styles.css').read_text(encoding='utf-8')

    def test_mode_is_decided_before_render(self):
        """模式必须在渲染前定下来，不然会先闪一下采购界面。"""
        self.assertIn('mode-purchase', self.html.split('</head>')[0])
        self.assertIn('document.documentElement.className', self.html)

    def test_blocks_are_tagged(self):
        for needle in ('card purchase-only', 'card member-only'):
            self.assertIn(needle, self.html, 'index.html 里少了 %s 区块' % needle)

    def test_css_hides_the_other_mode(self):
        self.assertIn('html.mode-member .purchase-only', self.css)
        self.assertIn('html.mode-purchase .member-only', self.css)

    def test_login_gate_exists(self):
        for needle in ('id="loginGate"', 'id="loginForm"', 'id="loginPw"',
                       'id="loginPw2"', 'id="logout"'):
            self.assertIn(needle, self.html, 'index.html 缺 %s' % needle)

    def test_js_knows_the_mode(self):
        self.assertIn("const IS_PURCHASE = MODE === 'purchase'", self.js)

    def test_js_has_both_start_paths(self):
        self.assertIn('async function startPurchase()', self.js)
        self.assertIn('async function startMember()', self.js)

    def test_member_mode_does_not_fetch_purchase_data(self):
        """组员入口不该去拉采购专用接口 —— 拉了也是 401，白白报错。"""
        member_part = self.js.split('async function startMember()')[1]
        member_part = member_part.split('/* ---')[0]
        for forbidden in ('loadPending', 'loadShare', 'loadMailConfig', 'loadBlock'):
            self.assertNotIn(forbidden, member_part,
                             '组员入口不该调用 %s' % forbidden)

    def test_block_is_only_loaded_for_purchase(self):
        self.assertIn('if (IS_PURCHASE) await loadBlock()', self.js)

    def test_member_identity_is_synced_to_the_server(self):
        self.assertIn("api('/api/me'", self.js)

    def test_member_progress_uses_the_numbers_endpoint(self):
        self.assertIn("'/progress'", self.js)

    def test_member_cannot_change_statuses(self):
        """下单与发票状态是采购的事，组员那里应当是只读文本。"""
        self.assertIn('if (!IS_PURCHASE) {', self.js)
        self.assertIn('cellselect readonly', self.js)
        self.assertIn('.cellselect.readonly', self.css)

    def test_member_wording(self):
        self.assertIn('function applyMemberWording()', self.js)
        self.assertIn('我要买什么', self.js)
        self.assertIn('applyMemberWording();', self.js)

    def test_member_gets_a_clear_empty_hint(self):
        self.assertIn('你还没填过东西', self.js)

    def test_member_can_still_upload_their_own_invoice(self):
        """用户要的：组员能传自己那笔的发票。"""
        self.assertIn('function invoiceCell(it)', self.js)
        self.assertIn("up.textContent = list.length ? '再传' : '传发票'", self.js)

    def test_edit_buttons_are_limited_to_your_own_rows(self):
        """组员看得到全组，但别人的行上不能出现改/删 —— 点了只会 403。"""
        self.assertIn('function isMine(it)', self.js)
        self.assertIn('const canChange = IS_PURCHASE || mine;', self.js)
        self.assertIn("别人填的", self.js)
        self.assertIn('if (open && (IS_PURCHASE || mine))', self.js)


if __name__ == '__main__':
    unittest.main(verbosity=2)
