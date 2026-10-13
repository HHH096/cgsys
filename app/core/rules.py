# -*- coding: utf-8 -*-
"""金额计算、字段校验、拆分规则。

拆分规则（用户确认）：**按一次采购同一商家的总金额**——
同一商家在本轮的所有行合计 >= 阈值（默认 1000）时，这些行进「高于一千」，否则进「低于一千」。
逐行的人工指定（`split_override` = 'low' / 'high'）优先于自动规则。
"""
import math
from collections import OrderedDict

from app import config

MONEY = 2


def to_num(v):
    """宽松转数字：空 / 非数字 -> None。"""
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def r2(v):
    """保留 2 位小数（与 Excel 显示一致）。"""
    return None if v is None else round(v + 0.0, MONEY)


def compute_amounts(item):
    """返回 (小计金额, 合计金额)。数量或单价缺一，小计为空。"""
    qty = to_num(item.get('qty'))
    price = to_num(item.get('price'))
    freight = to_num(item.get('freight'))
    if qty is None or price is None:
        return None, None
    subtotal = r2(qty * price)
    return subtotal, r2(subtotal + (freight or 0.0))


def supplier_key(item):
    """同一「商家」的分组键：优先用「网址链接」列，空则按行独立（避免把无关行并到一起）。"""
    s = (item.get('url') or '').strip()
    return 'url:' + s if s else 'item:%s' % item.get('id')


def split_items(items, threshold=None):
    """把条目分到 (低于一千, 高于一千, 各商家合计)。

    返回 (low, high, group_totals)；low/high 保持传入顺序。
    """
    th = config.DEFAULT_SPLIT_THRESHOLD if threshold is None else float(threshold)
    buckets = [None] * len(items)
    groups = OrderedDict()
    for i, it in enumerate(items):
        ov = (it.get('split_override') or '').strip()
        if ov in ('low', 'high'):
            buckets[i] = ov
        else:
            groups.setdefault(supplier_key(it), []).append(i)

    group_totals = OrderedDict()
    for key, idxs in groups.items():
        s = r2(sum((to_num(items[i].get('total')) or 0.0) for i in idxs)) or 0.0
        group_totals[key] = s
        bucket = 'high' if s >= th else 'low'
        for i in idxs:
            buckets[i] = bucket

    low = [items[i] for i in range(len(items)) if buckets[i] == 'low']
    high = [items[i] for i in range(len(items)) if buckets[i] == 'high']
    return low, high, group_totals


# ---------------------------------------------------------------- 退货

REFUND_MARK = '已退货'


def item_net_paid(item):
    """这笔实际花了多少：实付减去退回的。没退就是实付。"""
    item = item or {}
    paid = to_num(item.get('paid')) or 0
    if not item.get('refunded'):
        return paid
    return round(paid - (to_num(item.get('refund_amount')) or 0), MONEY)


def export_value(item, field):
    """导出时某一列该填什么。

    两条规则：

    * 「实付金额」退货时填**净额**（全退就是 0）
    * 「备注」= 原备注 +（退货标记）+（垫资人）

    垫资为什么并进备注：车队总表那边**列是固定的 20 列**，多一列就贴不进去，
    所以系统里那个独立的 `payer` 字段没有自己的落点；而备注本来就会跟着粘过去，
    写在这儿既不会撑破列，也让回填总表的人看得出是谁垫的钱。

    导出的三张 sheet 和「回填车队总表」的行块都走这一个函数 ——
    免得两处各写一遍，日子久了走样。
    """
    item = item or {}
    if field == 'note':
        return export_note(item)
    if field == 'paid' and item.get('refunded'):
        return item_net_paid(item)
    v = item.get(field)
    return v.strip() if isinstance(v, str) else v


def export_note(item):
    """备注列的内容：原备注 +（退货标记）+（垫资人）。"""
    item = item or {}
    note = str(item.get('note') or '').strip()
    if item.get('refunded'):
        note = '%s（%s）' % (note, REFUND_MARK) if note else REFUND_MARK
    payer = str(item.get('payer') or '').strip()
    if payer:
        note = '%s %s垫资' % (note, payer) if note else '%s垫资' % payer
    return note


def validate(item):
    """返回 (errors, warnings)。errors 非空则该条不能提交。"""
    errors = []
    warnings = []

    if not (item.get('name') or '').strip():
        errors.append('物料名称/品牌 必填')
    if not (item.get('unit') or '').strip():
        errors.append('单位 必填')
    if not (item.get('applicant') or '').strip():
        errors.append('申购人 必填')

    qty = to_num(item.get('qty'))
    if qty is None or qty <= 0:
        errors.append('申请数量 必须是大于 0 的数字')

    price = to_num(item.get('price'))
    if price is None or price < 0:
        errors.append('预计单价 必须是数字')

    freight = to_num(item.get('freight'))
    if item.get('freight') not in (None, '') and freight is None:
        errors.append('运费 必须是数字')

    phone = (item.get('phone') or '').strip()
    if phone and not all(ch.isdigit() or ch in '+-' for ch in phone):
        errors.append('申购人电话 只能是数字（可带 + / -）')
    elif phone and not (5 <= len(phone) <= 20):
        errors.append('申购人电话 长度不对')

    url = (item.get('url') or '').strip()
    if url.startswith(('http://', 'https://')):
        rest = url.split('://', 1)[1]
        if not rest or '.' not in rest.split('/', 1)[0]:
            warnings.append('网址链接 看起来不像有效链接（也可以直接填商家名称）')

    if price is not None and price > config.CONTRACT_PRICE_HINT:
        warnings.append('单价超过 %g 元，记得标黄并准备合同' % config.CONTRACT_PRICE_HINT)

    subtotal, total = compute_amounts(item)
    if subtotal is not None and total is not None and freight and freight > 0:
        warnings.append('本行含运费 %g 元，合计金额 = 小计 + 运费' % freight)

    return errors, warnings
