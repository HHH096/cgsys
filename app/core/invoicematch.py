# -*- coding: utf-8 -*-
"""把发票明细的每一行配到一条采购上 —— 名称优先，金额辅助。

**为什么不能全自动**：实测票面名和条目名经常对不上 ——

    条目写「安费诺」（品牌）      票面写「连接器PT06E-14-19P(SR)」
    条目写「航空插头」（通用名）   票面写「航空插头DS20J12TQ+DS20」

后者一沾就中，前者一个字都不沾。所以：名称当主力、金额兜底、
最后**一定要让人核对**（这是设计的一部分，不是妥协）。
"""
import re

# 名称不沾边时，金额差多少以内才敢自动配（差太多就是配错了）
MAX_GAP_WHEN_NO_NAME = 20.0


def _norm(text):
    """去掉符号和空白、统一小写，便于比对。"""
    return re.sub(r'[\s\-_*（）()/、,，.。:：+]+', '', (text or '')).lower()


def name_hit(line_name, item_name):
    """票面名 vs 条目名的吻合度：2 = 包含，1 = 部分重合，0 = 不沾边。

    条目名常是**通用名**（电容 / 航空插头 / 水管），票面名长得多
    （「贴片电容1u2.2u4.7u100uF50V」）—— 所以「包含」是最强的信号。
    """
    a, b = _norm(line_name), _norm(item_name)
    if not a or not b:
        return 0
    if b in a or a in b:
        return 2
    # 中文没有词边界，用 2 字滑窗：只要有一处连续两字重合就算「相近」。
    #
    # ⚠️ **只用中文**做滑窗。真踩过：拿整串比对时，「连接器PT06E-14-19P」和
    # 「HK4100F-DC24V」能凑出 "24"、"d0"，「硅胶管16-23」和「AO3400A」能凑出
    # "00" —— 全是巧合，却让三条不相干的条目被"配"上了。字母数字是型号规格，
    # 重合不代表是同一样东西。
    cn = re.sub(r'[^\u4e00-\u9fff]+', '', b)
    grams = {cn[i:i + 2] for i in range(max(0, len(cn) - 1))}
    if not grams:
        return 0
    return 1 if any(g in a for g in grams) else 0


def match_lines(lines, items):
    """给每一行明细挑一条采购。

    返回 `[{'line': 行号, 'item_id': 采购id或None, 'why': 依据, 'gap': 金额差}]`，
    顺序跟 lines 一致。规则：名称命中优先 → 同档比金额 → 一条采购只配一次。
    """
    pairs = []
    for li, line in enumerate(lines):
        for it in items:
            hit = name_hit(line.get('name'), it.get('name'))
            gap = abs(round(float(line.get('total') or 0), 2)
                      - round(float(it.get('total') or 0), 2))
            pairs.append((-hit, gap, li, it['id']))
    pairs.sort()

    taken = {}
    used = set()
    for neg_hit, gap, li, iid in pairs:
        if li in taken or iid in used:
            continue
        # 名字不沾边、金额又差得远 —— 别硬配，留给人工
        if neg_hit == 0 and gap > MAX_GAP_WHEN_NO_NAME:
            continue
        taken[li] = (iid, -neg_hit, gap)
        used.add(iid)

    out = []
    for li in range(len(lines)):
        if li in taken:
            iid, hit, gap = taken[li]
            why = {2: '名称吻合', 1: '名称相近', 0: '金额接近'}[hit]
            out.append({'line': li, 'item_id': iid, 'why': why, 'gap': gap})
        else:
            out.append({'line': li, 'item_id': None,
                        'why': '没能自动认出来，请手选', 'gap': None})
    return out


def match_invoices_to_items(uploads, items):
    """把「一批上传的发票」各自配到一条采购上 —— 名称优先，金额兜底，一条采购只配一次。

    uploads: [{'key': 标识（一般是文件名）, 'name': 票面名（可能为空）, 'amount': 金额或 None}]
    items:   本轮采购条目（含 id / name / total / paid）

    为什么单独写一个、不复用 `match_lines`：那边是「一张票的多行明细」配多条采购，
    行有名称可以比。这边是**整张票配一条采购**，而且返工重开的票多半是照片/扫描件
    （没有文字层，读不出名称），**金额才是稳的信号** —— 名称只在金额接近时用来挑。

    返回 `{key: item_id}`，配不上的那一项**不带 key**（调用方按「背面留空」处理）。
    """
    cands = []
    for u in uploads:
        for it in items:
            hit = name_hit(u.get('name'), it.get('name')) if u.get('name') else 0
            amt = u.get('amount')
            if amt is None:
                # 金额都读不出来的票（照片票且 OCR 也没认出来）：
                # 只认名字完全吻合的，还要金额对得上——否则宁可不配。
                ihave = it.get('paid') if it.get('paid') is not None else it.get('total')
                if hit != 2 or ihave is None:
                    continue
                gap = 0.0
            else:
                ihave = it.get('paid') if it.get('paid') is not None else it.get('total')
                if ihave is None:
                    continue
                gap = abs(round(float(amt), 2) - round(float(ihave), 2))
                if hit == 0 and gap > MAX_GAP_WHEN_NO_NAME:
                    continue
            cands.append((-hit, gap, u.get('key'), it['id']))

    cands.sort()
    taken, used = {}, set()
    for neg_hit, gap, key, iid in cands:
        if key in taken or iid in used:
            continue
        taken[key] = iid
        used.add(iid)
    return taken
