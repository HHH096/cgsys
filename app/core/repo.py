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


def update_item(iid, data):
    """改内容。**每次都会把 `rev` +1** —— 并发时的版本号，见 api_update_item。

    只改「下单/发票状态」的不走这里（`set_item_status`），所以不会无端拦人。
    """
    patch = _clean(data)
    if not patch:
        return
    old = get_item(iid)
    if old is None:
        raise ValueError('条目不存在：%s' % iid)
    values = _with_amounts(patch, base=old)
    cols = list(values.keys())
    with session() as c:
        c.execute('UPDATE items SET %s, rev = rev + 1, updated_at = ? WHERE id = ?'
                  % ', '.join('%s = ?' % k for k in cols),
                  [values[k] for k in cols] + [_now(), iid])


def delete_item(iid):
    with session() as c:
        c.execute('DELETE FROM items WHERE id = ?', (iid,))


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
    _update_raw(iid, {'refunded': 1, 'refund_amount': amount})

    for inv in list_invoices(item_id=iid):
        delete_invoice(inv['id'])
    return get_item(iid)


def items_pending_order(round_id):
    """还没下单的条目。**退了的当然不算**。"""
    from app import config
    return [it for it in list_items(round_id)
            if it['order_status'] == config.ORDER_PENDING and not it.get('refunded')]


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

def add_invoice(item_id, round_id, orig_name, stored_name, rel_path, size, **meta):
    with session() as c:
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
    """同一个条目的同一个归档文件是否已经登记过。"""
    if not rel_path:
        return None
    with session() as c:
        row = c.execute('SELECT * FROM invoices WHERE item_id = ? AND rel_path = ?',
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


def link_invoice(inv_id, item_id):
    """把发票挂到某条采购上。已经挂过就什么都不做。"""
    with session() as c:
        c.execute(
            'INSERT OR IGNORE INTO invoice_items (invoice_id, item_id, created_at) '
            'VALUES (?, ?, ?)', (inv_id, item_id, _now()))
        _sync_invoice_flag(c, item_id)


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
    """
    from app import config
    from app.core import invoicing

    inv = get_invoice(inv_id)
    if not inv:
        return False
    with session() as c:
        c.execute('DELETE FROM invoices WHERE id = ?', (inv_id,))
    invoicing.delete_file(inv['rel_path'])

    if not list_invoices(item_id=inv['item_id']):
        set_item_status(inv['item_id'], invoice_status=config.INV_NONE)
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


def update_mail_attachment(mid, **kw):
    sets, vals = [], []
    for f in ('status', 'item_id', 'invoice_id'):
        if f in kw:
            sets.append('%s = ?' % f)
            vals.append(kw[f])
    if not sets:
        return
    vals.append(mid)
    with session() as c:
        c.execute('UPDATE mail_inbox SET %s WHERE id = ?' % ', '.join(sets), vals)


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
