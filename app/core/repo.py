# -*- coding: utf-8 -*-
"""数据访问层：SQLite 行 -> dict，前端与导出都走这一层。"""
from datetime import datetime

from app.core import rules
from app.core.db import session

ITEM_TEXT_FIELDS = [
    'name', 'spec', 'unit', 'stock', 'purpose', 'applicant', 'payer', 'used_at',
    'phone', 'image_path', 'url', 'group_name', 'note', 'invoice', 'arrived',
    'has_contract', 'split_override',
]
ITEM_NUM_FIELDS = ['qty', 'price', 'freight', 'paid']


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- 组员

def list_members():
    with session() as c:
        return _rows(c.execute('SELECT * FROM members ORDER BY sort, id'))


def get_member(mid):
    with session() as c:
        rows = _rows(c.execute('SELECT * FROM members WHERE id = ?', (mid,)))
    return rows[0] if rows else None


def add_member(name, phone='', is_owner=0):
    name = (name or '').strip()
    if not name:
        raise ValueError('姓名不能为空')
    with session() as c:
        cur = c.execute(
            'INSERT INTO members(name, phone, is_owner, sort) '
            'VALUES (?, ?, ?, COALESCE((SELECT MAX(sort) + 1 FROM members), 0))',
            (name, (phone or '').strip(), 1 if is_owner else 0))
        return cur.lastrowid


def update_member(mid, **kw):
    sets, vals = [], []
    for f in ('name', 'phone', 'is_owner'):
        if f in kw:
            sets.append('%s = ?' % f)
            vals.append(kw[f])
    if not sets:
        return
    vals.append(mid)
    with session() as c:
        c.execute('UPDATE members SET %s WHERE id = ?' % ', '.join(sets), vals)


def delete_member(mid):
    with session() as c:
        c.execute('DELETE FROM members WHERE id = ?', (mid,))


# ---------------------------------------------------------------- 轮次

def list_rounds():
    with session() as c:
        return _rows(c.execute('SELECT * FROM rounds ORDER BY id DESC'))


def get_round(rid):
    with session() as c:
        row = c.execute('SELECT * FROM rounds WHERE id = ?', (rid,)).fetchone()
        return dict(row) if row else None


def create_round(name, title=None, group_name=None, threshold=None):
    from app import config
    name = (name or '').strip()
    if not name:
        raise ValueError('轮次名称不能为空，例如「八月采购」')
    title = (title or config.DEFAULT_TITLE).strip()
    group_name = (group_name or config.DEFAULT_GROUP).strip()
    th = config.DEFAULT_SPLIT_THRESHOLD if threshold is None else float(threshold)
    with session() as c:
        cur = c.execute(
            'INSERT INTO rounds(name, title, group_name, status, split_threshold, created_at) '
            "VALUES (?, ?, ?, 'open', ?, ?)",
            (name, title, group_name, th, _now()))
        return cur.lastrowid


def update_round(rid, **kw):
    sets, vals = [], []
    for f in ('name', 'title', 'group_name', 'split_threshold'):
        if f in kw and kw[f] is not None:
            sets.append('%s = ?' % f)
            vals.append(kw[f])
    if not sets:
        return
    vals.append(rid)
    with session() as c:
        c.execute('UPDATE rounds SET %s WHERE id = ?' % ', '.join(sets), vals)


def set_round_status(rid, status):
    with session() as c:
        if status == 'locked':
            c.execute('UPDATE rounds SET status = ?, locked_at = ? WHERE id = ?', (status, _now(), rid))
        else:
            c.execute('UPDATE rounds SET status = ? WHERE id = ?', (status, rid))


# ---------------------------------------------------------------- 条目

def _clean(data):
    """只挑可写字段，做宽松归一。"""
    out = {}
    for f in ITEM_TEXT_FIELDS:
        if f in data:
            out[f] = '' if data[f] is None else str(data[f]).strip()
    for f in ITEM_NUM_FIELDS:
        if f in data:
            out[f] = rules.to_num(data[f])
    return out


def _with_amounts(values, base=None):
    """按规则算好 小计金额 / 合计金额 再落库。

    局部更新时必须把「库里已有的值」和「这次提交的值」合起来算，
    否则只改数量会把价格丢掉、金额被算成空。
    """
    merged = dict(base or {})
    merged.update(values)
    subtotal, total = rules.compute_amounts(merged)
    out = dict(values)
    out['subtotal'] = subtotal
    out['total'] = total
    return out


def list_items(round_id):
    with session() as c:
        return _rows(c.execute(
            'SELECT * FROM items WHERE round_id = ? ORDER BY seq, id', (round_id,)))


def get_item(iid):
    with session() as c:
        row = c.execute('SELECT * FROM items WHERE id = ?', (iid,)).fetchone()
        return dict(row) if row else None


def item_history(name, limit=5):
    """「这个名字以前买过什么」—— 填单时一键沿用上次的规格/单价/链接。

    为什么要有：组员每轮都在重复填同一个器件（电阻、STM32、硅胶管…），
    规格和单价每次重新敲，既慢又容易敲错。库里已经攒了几百条历史，
    直接拿来用比让人重打一遍靠谱。

    几个取舍：

    * **跨轮次查**（不限本轮）：同一个器件往往隔几轮就要补一次。
    * **退过的不算**：退了说明那次买得不合适，不该再被当成推荐。
    * **按（规格, 单价）去重，只留最近一次**：不然同一规格买过十次，
      就摊出十条一模一样的建议。
    * 不排除当前轮 —— 本轮已经有人要过同一个东西，这本身就是有用的提示。
    """
    key = (name or '').strip()
    if not key:
        return []
    with session() as c:
        rows = _rows(c.execute(
            'SELECT i.*, r.name AS round_name FROM items i '
            'JOIN rounds r ON r.id = i.round_id '
            'WHERE i.name = ? AND COALESCE(i.refunded, 0) = 0 '
            'ORDER BY i.id DESC LIMIT 200', (key,)))
    out = []
    seen = set()
    for row in rows:
        sig = ((row.get('spec') or '').strip(), row.get('price'))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def add_item(round_id, member_id, data):
    from app import config
    values = _with_amounts(_clean(data))
    values.setdefault('group_name', config.DEFAULT_GROUP)
    cols = list(values.keys())
    with session() as c:
        cur = c.execute(
            'INSERT INTO items(round_id, member_id, seq, %s, created_at, updated_at) '
            'VALUES (?, ?, COALESCE((SELECT MAX(seq) + 1 FROM items WHERE round_id = ?), 0), %s, ?, ?)'
            % (', '.join(cols), ', '.join('?' * len(cols))),
            [round_id, member_id, round_id] + [values[k] for k in cols] + [_now(), _now()])
        return cur.lastrowid


class RevConflict(Exception):
    """这条在你填的时候已经被改过了（内容版本号对不上）。"""


def update_item(iid, data, base_rev=None):
    """改内容。**每次都会把 `rev` +1** —— 并发时的版本号，见 api_update_item。

    只改「下单/发票状态」的不走这里（`set_item_status`），所以不会无端拦人。

    **读旧值、算金额、写回去都在同一个事务里**：分开做的话，两个人同时改
    数量/单价时，后提交的那个会用**自己先前读到的旧值**重算金额，
    落库的 subtotal/total 就是两种修改拼出来的错误数字 —— 而且一声不响。

    传了 `base_rev` 就走乐观锁（`WHERE id = ? AND rev = ?`）：
    中途被别人改过就抛 `RevConflict`，让调用方回去刷新，而不是把人家盖掉。
    """
    patch = _clean(data)
    if not patch:
        return
    with session() as c:
        row = c.execute('SELECT * FROM items WHERE id = ?', (iid,)).fetchone()
        if row is None:
            raise ValueError('条目不存在：%s' % iid)
        old = dict(row)
        if base_rev is not None and int(old.get('rev') or 0) != int(base_rev):
            raise RevConflict('条目已被改过')
        values = _with_amounts(patch, base=old)
        cols = list(values.keys())
        sql = ('UPDATE items SET %s, rev = rev + 1, updated_at = ? WHERE id = ?'
               % ', '.join('%s = ?' % k for k in cols))
        args = [values[k] for k in cols] + [_now(), iid]
        if base_rev is not None:
            sql += ' AND rev = ?'
            args.append(int(base_rev))
        cur = c.execute(sql, args)
        # 走到这儿还可能是「读完之后、写进去之前」被人改了 —— 条件更新兜住
        if base_rev is not None and cur.rowcount == 0:
            raise RevConflict('条目已被改过')


def delete_item(iid):
    """删条目 —— **连同只属于它的发票一起删**。

    以前只 `DELETE FROM items`：`invoices` 记录靠外键级联没了，
    但 `data/invoices/` 下的 PDF 永远留着（没人知道它属于谁）——
    用久了就是一堆占空间又查不出来处的垃圾。

    一票多条的情况要分开处理：

    * 票**只挂在这一条**上 → 记录和文件都删掉
    * 票还挂在别的条目上 → 只摘掉这一条；并且把 `invoices.item_id`（主条目）
      改指到另一条 —— 否则主条目被删时会顺着外键把整张票级联删掉，
      别人那条的发票就凭空消失了。
    """
    from app.core import invoicing

    dead = []
    with session() as c:
        rows = _rows(c.execute(
            'SELECT v.id AS inv_id, v.rel_path AS rel_path, '
            '  (SELECT COUNT(*) FROM invoice_items y WHERE y.invoice_id = v.id) AS n '
            'FROM invoice_items x JOIN invoices v ON v.id = x.invoice_id '
            'WHERE x.item_id = ?', (iid,)))
        for row in rows:
            if row['n'] <= 1:
                dead.append(row['rel_path'])
                c.execute('DELETE FROM invoices WHERE id = ?', (row['inv_id'],))
            else:
                other = c.execute(
                    'SELECT item_id FROM invoice_items '
                    'WHERE invoice_id = ? AND item_id != ? LIMIT 1',
                    (row['inv_id'], iid)).fetchone()
                if other:
                    c.execute('UPDATE invoices SET item_id = ? WHERE id = ?',
                              (other['item_id'], row['inv_id']))
        # 关联行随外键级联清掉
        c.execute('DELETE FROM items WHERE id = ?', (iid,))
    # 文件删除放在**提交之后**：事务回滚时文件不能先没了
    for rel in dead:
        invoicing.delete_file(rel)


def reorder_items(round_id, ordered_ids):
    with session() as c:
        for seq, iid in enumerate(ordered_ids):
            c.execute('UPDATE items SET seq = ? WHERE id = ? AND round_id = ?', (seq, iid, round_id))


# ---------------------------------------------------------------- 状态与清单

def set_item_status(iid, order_status=None, invoice_status=None):
    """改下单/发票状态，并同步模板里的「发票」「是否到货」两列。"""
    from app import config
    old = get_item(iid)
    if old is None:
        raise ValueError('条目不存在：%s' % iid)

    patch = {}
    if order_status is not None:
        if order_status not in config.ORDER_STATUSES:
            raise ValueError('未知下单状态：%s' % order_status)
        patch['order_status'] = order_status
    if invoice_status is not None:
        if invoice_status not in config.INV_STATUSES:
            raise ValueError('未知发票状态：%s' % invoice_status)
        patch['invoice_status'] = invoice_status
    if not patch:
        return

    merged = dict(old)
    merged.update(patch)
    # 同步导出用的两列
    if 'order_status' in patch:
        patch['arrived'] = '是' if merged['order_status'] == config.ORDER_ARRIVED else ''
    if 'invoice_status' in patch:
        patch['invoice'] = '有' if merged['invoice_status'] == config.INV_RECEIVED else ''

    assignments = ['%s = ?' % k for k in patch] + ['updated_at = ?']
    params = list(patch.values()) + [_now(), iid]
    with session() as c:
        c.execute('UPDATE items SET %s WHERE id = ?' % ', '.join(assignments), params)


def _update_raw(iid, patch):
    """不走 _clean、不重算金额的裸更新（退款这类派生标记用）。"""
    if not patch:
        return
    assignments = ['%s = ?' % k for k in patch] + ['updated_at = ?']
    params = list(patch.values()) + [_now(), iid]
    with session() as c:
        c.execute('UPDATE items SET %s WHERE id = ?' % ', '.join(assignments), params)


def item_net_paid(item):
    """这笔实际花了多少：实付减去退回的。没退就是实付。"""
    item = item or {}
    paid = rules.to_num(item.get('paid')) or 0
    if not item.get('refunded'):
        return paid
    refund = rules.to_num(item.get('refund_amount')) or 0
    return round(paid - refund, 2)


def set_refund(iid, refunded=True, refund_amount=None):
    """整单退货 / 取消退货。

    退货做三件事（按定的规则）：

    * 记下退回来的钱 —— 不指定就按**实付全额退**（「全退就是 0」）
    * **把这条上的发票删掉**（含电子发票）
    * 发票状态随之回到「未要」；但退货的条目**不会再进「缺发票」清单**，
      否则那份清单永远清不干净（见 items_pending_invoice）
    """
    item = get_item(iid)
    if item is None:
        raise ValueError('条目不存在：%s' % iid)

    if not refunded:
        _update_raw(iid, {'refunded': 0, 'refund_amount': 0})
        return get_item(iid)

    paid = rules.to_num(item.get('paid')) or 0
    amount = paid if refund_amount is None else max(0.0, rules.to_num(refund_amount) or 0)

    from app.core import invoicing
    dead_files = []
    # 标记退货 + 摘/删票 **全在一个事务里**：分成几次提交的话，中途被打断会留下
    # 「已标退货、票还在、状态还是已收到」这种自相矛盾的半成品。
    with session() as c:
        c.execute('UPDATE items SET refunded = 1, refund_amount = ?, updated_at = ?'
                  ' WHERE id = ?', (amount, _now(), iid))
        rows = _rows(c.execute(
            'SELECT v.id, v.rel_path FROM invoice_items x '
            'JOIN invoices v ON v.id = x.invoice_id WHERE x.item_id = ?', (iid,)))
        for inv in rows:
            others = c.execute(
                'SELECT COUNT(*) FROM invoice_items WHERE invoice_id = ? AND item_id != ?',
                (inv['id'], iid)).fetchone()[0]
            if others:
                # 这张票还挂在别的条目上（别人那条没退货）—— 只摘掉本条，票留着
                c.execute('DELETE FROM invoice_items WHERE invoice_id = ? AND item_id = ?',
                          (inv['id'], iid))
            else:
                # 确实没有任何归属了才删票
                c.execute('DELETE FROM invoices WHERE id = ?', (inv['id'],))
                dead_files.append(inv['rel_path'])
            _sync_invoice_flag(c, iid)
    # 文件删除放在**提交之后**：事务万一回滚，文件不能先没了
    for rel in dead_files:
        invoicing.delete_file(rel)
    return get_item(iid)


def bump_refund_if_paid_was_empty(iid, paid):
    """退货时实付还是空的，之后才补上 —— 退款额要跟着补记成「全额退」。

    场景：退了货，但当时实付金额没填（票是照片、金额读不出），
    `refund_amount` 就落成 0。事后补了实付 100，净额仍算 100 ——
    可那 100 明明退回来了，对账会多算一笔。

    只在「已退货且退款额还是 0」时补，人工填过的退款额不动。
    """
    if paid is None:
        return
    with session() as c:
        c.execute(
            'UPDATE items SET refund_amount = ?, updated_at = ? '
            'WHERE id = ? AND refunded = 1 AND COALESCE(refund_amount, 0) = 0',
            (round(float(paid), 2), _now(), iid))


def items_pending_order(round_id):
    """还没下单的条目。**退了的当然不算**。"""
    from app import config
    return [it for it in list_items(round_id)
            if it['order_status'] == config.ORDER_PENDING and not it.get('refunded')]


def shopping_list(round_id):
    """「待购清单」：还要买、而且**该由采购去淘宝下单**的那些条目。

    三个筛子，每个都有明确理由：

    * **待下单** —— 已经买过的、已发货的不再列，免得重复买。
    * **退货的不列** —— 退了就是不要了。
    * **垫付的不列** —— 谁垫的钱就说明他自己已经买了（`payer` 非空），
      再让采购去下单就成了重复采购。

    链接会用 `shareparse.find_url` 抽成干净的 URL ——
    实测有 73 条条目的「网址链接」里存的是整段分享文本
    （`https://e.tb.cn/xxx?tk=yyy CZ028`），带空格，浏览器根本打不开。
    """
    from app.core import shareparse
    out = []
    for it in items_pending_order(round_id):
        if (it.get('payer') or '').strip():
            continue                       # 别人垫付过 = 已经买了
        entry = dict(it)
        entry['link'] = shareparse.find_url(it.get('url') or '')
        out.append(entry)
    return out


def items_pending_invoice(round_id):
    """还没拿到发票的条目 —— 就是「要确保每个采购都有发票」那份清单。

    **以关联表为准**：只要挂上了票（哪怕是一票多条里的一行）就算有。
    以前是看 `invoice_status` 这个手工字段，所以一张票只能挂一条时，
    另外几条永远显示「未要」，这份清单永远清不掉。

    顺带照顾手工标过「已收到」的老数据；**退货的排除**（票跟着退货删了，
    再算进来就永远清不干净）。
    """
    from app import config
    with session() as c:
        have = {r[0] for r in c.execute(
            'SELECT DISTINCT x.item_id FROM invoice_items x '
            'JOIN items i ON i.id = x.item_id WHERE i.round_id = ?', (round_id,))}
    return [it for it in list_items(round_id)
            if not it.get('refunded')
            and it['id'] not in have
            and it['invoice_status'] != config.INV_RECEIVED]


# ---------------------------------------------------------------- 发票

def add_invoice(item_id, round_id, orig_name, stored_name, rel_path, size,
                conn=None, **meta):
    """登记一张发票。`conn` 传了就复用同一个事务（见 mailfetch 的挂载）。"""
    def _do(c):
        cur = c.execute(
            'INSERT INTO invoices(item_id, round_id, orig_name, stored_name, rel_path, size,'
            ' amount, invoice_no, invoice_date, seller, created_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (item_id, round_id, orig_name, stored_name, rel_path, size,
             meta.get('amount'), meta.get('invoice_no', ''), meta.get('invoice_date', ''),
             meta.get('seller', ''), _now()))
        inv_id = cur.lastrowid
        # 归属同时也写进关联表 —— 新逻辑一律以 invoice_items 为准，
        # 写在这里而不是让每个调用方自己补一句，免得哪天漏掉一处。
        if item_id:
            c.execute(
                'INSERT OR IGNORE INTO invoice_items (invoice_id, item_id, created_at) '
                'VALUES (?, ?, ?)', (inv_id, item_id, _now()))
            _sync_invoice_flag(c, item_id)
        return inv_id

    if conn is not None:
        return _do(conn)
    with session() as c:
        return _do(c)


def list_invoices(round_id=None, item_id=None):
    sql = 'SELECT * FROM invoices'
    where, args = [], []
    if round_id is not None:
        where.append('round_id = ?')
        args.append(round_id)
    if item_id is not None:
        where.append('item_id = ?')
        args.append(item_id)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY id'
    with session() as c:
        return _rows(c.execute(sql, args))


def get_invoice(inv_id):
    with session() as c:
        row = c.execute('SELECT * FROM invoices WHERE id = ?', (inv_id,)).fetchone()
        return dict(row) if row else None


def find_invoice_by_path(item_id, rel_path):
    """这张票在该条目上是否已经登记过。

    **按归属（关联表）判断，不能只看 `invoices.item_id`**：
    一票多条时，在**非主条目**上重传同一张票会被当成新票、再插一条 invoices 记录，
    两条记录指向同一个文件 —— 合并打印会印两遍（等于重复报销），
    而且删掉其中一条会把文件删掉，另一条点开就 404。
    """
    if not rel_path:
        return None
    with session() as c:
        row = c.execute(
            'SELECT v.* FROM invoices v '
            'JOIN invoice_items x ON x.invoice_id = v.id '
            'WHERE x.item_id = ? AND v.rel_path = ? LIMIT 1',
            (item_id, rel_path)).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------- 发票 ↔ 条目（多对多）

def items_of_invoice(inv_id):
    """这张发票挂了哪几条采购。"""
    with session() as c:
        rows = c.execute(
            'SELECT i.* FROM invoice_items x JOIN items i ON i.id = x.item_id '
            'WHERE x.invoice_id = ? ORDER BY i.seq, i.id', (inv_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def links_for_invoices(round_id):
    """这一轮每张票挂了哪些条目：`{invoice_id: [item_id, ...]}`。

    **一次查完**，别在循环里逐票 `items_of_invoice` ——28 张票就是 28 次连接，
    而轮次详情每次刷新（含 20 秒一次自动刷新）都要跑一遍。
    """
    with session() as c:
        rows = c.execute(
            'SELECT x.invoice_id AS invoice_id, x.item_id AS item_id '
            'FROM invoice_items x JOIN invoices v ON v.id = x.invoice_id '
            'WHERE v.round_id = ? ORDER BY x.item_id', (round_id,)).fetchall()
    out = {}
    for row in rows:
        out.setdefault(row['invoice_id'], []).append(row['item_id'])
    return out


def links_map():
    """**全部**发票的归属：`{invoice_id: [item_id, ...]}`，一次查完。

    给「待归档列表」用：那里的记录跨轮次，`links_for_invoices(round_id)` 用不上。
    以前是在循环里逐条 `items_of_invoice` —— 200 张已挂就是 200 次连接，
    而且每 20 秒自动刷新都要重跑一遍。
    """
    with session() as c:
        rows = c.execute(
            'SELECT invoice_id, item_id FROM invoice_items ORDER BY item_id').fetchall()
    out = {}
    for row in rows:
        out.setdefault(row['invoice_id'], []).append(row['item_id'])
    return out


def invoices_of_item(item_id):
    """这条采购有哪些发票（一票多条之后可能不止一张）。"""
    with session() as c:
        rows = c.execute(
            'SELECT v.* FROM invoice_items x JOIN invoices v ON v.id = x.invoice_id '
            'WHERE x.item_id = ? ORDER BY v.id', (item_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _sync_invoice_flag(c, item_id):
    """把某条采购的发票状态**对齐事实**：挂了票 = 已收到，一张都没有 = 未要。

    以前只把「主条目」设成已收到，一票多条时另外几条永远显示「未要」——
    界面上就成了「明明有发票，却写着未要」，对账也会少算。
    """
    from app import config
    n = c.execute('SELECT COUNT(*) FROM invoice_items WHERE item_id = ?',
                  (item_id,)).fetchone()[0]
    c.execute('UPDATE items SET invoice_status = ?, invoice = ?, updated_at = ? '
              'WHERE id = ?',
              (config.INV_RECEIVED if n else config.INV_NONE,
               '有' if n else '', _now(), item_id))


def link_invoice(inv_id, item_id, conn=None):
    """把发票挂到某条采购上。已经挂过就什么都不做。"""
    def _do(c):
        c.execute(
            'INSERT OR IGNORE INTO invoice_items (invoice_id, item_id, created_at) '
            'VALUES (?, ?, ?)', (inv_id, item_id, _now()))
        _sync_invoice_flag(c, item_id)

    if conn is not None:
        return _do(conn)
    with session() as c:
        _do(c)


def unlink_invoice(inv_id, item_id):
    with session() as c:
        c.execute('DELETE FROM invoice_items WHERE invoice_id = ? AND item_id = ?',
                  (inv_id, item_id))
        _sync_invoice_flag(c, item_id)


def set_invoice_items(inv_id, item_ids):
    """改挂：整批换掉这张发票的归属。传空列表 = 谁都不挂。

    `invoices.item_id` 同步成第一条，让仍在看那个字段的老代码保持一致。
    """
    ids = [int(x) for x in item_ids]
    with session() as c:
        # 先记下原本挂着谁，摘掉之后要一起对齐状态
        before = [r[0] for r in c.execute(
            'SELECT item_id FROM invoice_items WHERE invoice_id = ?', (inv_id,))]
        c.execute('DELETE FROM invoice_items WHERE invoice_id = ?', (inv_id,))
        for item_id in ids:
            c.execute(
                'INSERT OR IGNORE INTO invoice_items (invoice_id, item_id, created_at) '
                'VALUES (?, ?, ?)', (inv_id, item_id, _now()))
        # 只在有归属时才同步主条目 —— invoices.item_id 是 NOT NULL，写 NULL 会直接崩
        if ids:
            c.execute('UPDATE invoices SET item_id = ? WHERE id = ?', (ids[0], inv_id))
        for item_id in set(before) | set(ids):
            _sync_invoice_flag(c, item_id)


def update_invoice_file(inv_id, stored_name, rel_path):
    """改名之后同步发票记录里的文件名与路径。

    界面上的「看 / 存」都按 `rel_path` 找文件，不同步就会点了打不开。
    """
    with session() as c:
        c.execute('UPDATE invoices SET stored_name = ?, rel_path = ? WHERE id = ?',
                  (stored_name, rel_path, inv_id))


def delete_invoice(inv_id):
    """删掉发票记录，并连同归档文件一起删。

    若删完之后这个条目一张发票都不剩，状态要退回「未要」并同步「发票」列，
    否则会出现「显示已收到、实际没有发票」——正是「每个采购都要有发票」要防的事。

    **删记录与复位状态在同一个事务里**：分成两次提交的话，中途被打断
    （磁盘满、服务重启、DB 忙）就会留下「状态说已收到、库里其实没票」的半成品。
    """
    from app.core import invoicing

    inv = get_invoice(inv_id)
    if not inv:
        return False
    # 先记下这张票挂过的**所有**条目（一票可能挂在好几条上）
    touched = [it['id'] for it in items_of_invoice(inv_id)]
    with session() as c:
        c.execute('DELETE FROM invoices WHERE id = ?', (inv_id,))
        # 关联表由外键级联清掉；这里按级联后的现状逐个对齐状态
        for iid in touched:
            _sync_invoice_flag(c, iid)
    # 文件删除放在**提交之后**：事务万一回滚，文件不能先没了
    invoicing.delete_file(inv['rel_path'])
    return True


def invoice_counts(round_id):
    """每个条目挂了几张发票：{item_id: 张数}。

    按**关联表**算 —— 一张票覆盖多条时，那几条都要各算一张，
    不然界面上会显示成「没有发票」。
    """
    with session() as c:
        rows = c.execute(
            'SELECT x.item_id AS item_id, COUNT(*) AS n '
            'FROM invoice_items x JOIN invoices v ON v.id = x.invoice_id '
            'WHERE v.round_id = ? GROUP BY x.item_id', (round_id,)).fetchall()
    return {r['item_id']: r['n'] for r in rows}


# ---------------------------------------------------------------- 邮箱待归档

MAIL_PENDING = 'pending'
MAIL_ATTACHED = 'attached'
MAIL_IGNORED = 'ignored'


def add_mail_attachment(mail_uid, subject, sender, mail_date, orig_name,
                        stored_name, rel_path, size, sha1):
    with session() as c:
        cur = c.execute(
            'INSERT INTO mail_inbox(mail_uid, subject, sender, mail_date, orig_name,'
            ' stored_name, rel_path, size, sha1, status, created_at)'
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (mail_uid, subject, sender, mail_date, orig_name, stored_name,
             rel_path, size, sha1, _now()))
        return cur.lastrowid


def list_mail_attachments(status=None):
    sql = 'SELECT * FROM mail_inbox'
    args = []
    if status:
        sql += ' WHERE status = ?'
        args.append(status)
    sql += ' ORDER BY id DESC'
    with session() as c:
        return _rows(c.execute(sql, args))


def get_mail_attachment(mid):
    with session() as c:
        row = c.execute('SELECT * FROM mail_inbox WHERE id = ?', (mid,)).fetchone()
        return dict(row) if row else None


def update_mail_attachment(mid, conn=None, **kw):
    sets, vals = [], []
    for f in ('status', 'item_id', 'invoice_id', 'auto_attached'):
        if f in kw:
            sets.append('%s = ?' % f)
            vals.append(kw[f])
    if not sets:
        return
    vals.append(mid)

    def _do(c):
        c.execute('UPDATE mail_inbox SET %s WHERE id = ?' % ', '.join(sets), vals)

    if conn is not None:
        return _do(conn)
    with session() as c:
        _do(c)


def delete_mail_attachment(mid):
    """删掉待归档记录，连文件一起删（已挂到条目的不动，那是条目自己的发票了）。"""
    from app.core import invoicing
    row = get_mail_attachment(mid)
    if not row:
        return False
    with session() as c:
        c.execute('DELETE FROM mail_inbox WHERE id = ?', (mid,))
    if row['status'] != MAIL_ATTACHED:
        invoicing.delete_file(row['rel_path'])
    return True


def mail_seen_uids():
    """已经收过的邮件 UID —— 下次收取跳过它们。"""
    with session() as c:
        rows = c.execute("SELECT DISTINCT mail_uid FROM mail_inbox"
                         " WHERE mail_uid <> ''").fetchall()
    return {r['mail_uid'] for r in rows}


def mail_known_sha1():
    """已经收过的附件内容哈希 —— 同一张发票被转发多次也只留一份。"""
    with session() as c:
        rows = c.execute("SELECT DISTINCT sha1 FROM mail_inbox WHERE sha1 <> ''").fetchall()
    return {r['sha1'] for r in rows}


def mail_counts():
    with session() as c:
        rows = c.execute('SELECT status, COUNT(*) AS n FROM mail_inbox'
                         ' GROUP BY status').fetchall()
    got = {r['status']: r['n'] for r in rows}
    got['total'] = sum(got.values())
    return got


def payer_summary(round_id):
    """「谁自己先垫了多少钱」—— 按垫付人汇总，退货按净额算。

    为什么要汇总：`payer` 一直只标在行上（表格里一个小标签），从来没算过总数。
    可钱的事总得有人问一句「这轮谁垫了多少、该给谁转账」，
    一条条翻列表不现实。

    按金额从大到小排（谁垫得多谁在前面），同额按名字，保证顺序稳定。
    """
    from app.core import rules
    acc = {}
    for it in list_items(round_id):
        who = (it.get('payer') or '').strip()
        if not who:
            continue
        rec = acc.setdefault(who, {'payer': who, 'count': 0, 'amount': 0.0})
        rec['count'] += 1
        rec['amount'] = round(rec['amount'] + rules.item_net_paid(it), 2)
    rows = [r for r in acc.values() if r['count']]
    rows.sort(key=lambda r: (-r['amount'], r['payer']))
    return rows
