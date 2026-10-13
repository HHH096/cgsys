# -*- coding: utf-8 -*-
"""发票合并打印：A4 每页放 N 张发票，背面按条目排采购信息版面。

排版引擎移植自现有「发票合并助手」的 `app/core/pdf_builder.py`，
但数据直接取本系统的 **条目 + 发票记录**，不再走它那套「采购表匹配」。

版面规则（与工作区模板一致）：
  * 正面：A4 纵向，一页放 `per_page` 张（默认 2，上下排列、等比缩放居中），
    四周可画淡色裁切虚线
  * 背面：与正面一一对应，每张发票位置放一块版面，5 个字段：
    物品名称 / 用途 / 组别 / 付款日期（**留空手写**） / 归属表格（固定「月」，左侧留白手写月份）
  * 双面打印（长边翻转）后，每张发票的背面正好是它自己在采购表里的信息
  * 发票张数为奇数时，最后一张单独占半页，背面也只放一个信息块

中文字体做子集化嵌入，换电脑、换打印机都能正常显示与打印。
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import fitz

A4_W, A4_H = 595.276, 841.89
GRAY = (0.45, 0.45, 0.45)
LIGHT = (0.72, 0.72, 0.72)
DARK = (0.12, 0.12, 0.12)
FONT_NAME = 'cn'

_FONT_PATH: Optional[str] = None
_MEASURE = None


@dataclass
class MergeOptions:
    """合并参数。默认值就是工作区一直在用的那套。"""
    per_page: int = 2
    make_back: bool = True
    show_cut_line: bool = True
    show_back_frame: bool = True
    show_back_line: bool = True
    show_footer: bool = False
    show_invoice_meta: bool = True
    page_margin: float = 18.0
    cell_gap: float = 6.0
    font_size: float = 12.0
    title: str = ''
    group: str = '电气组'
    pay_date_text: str = ''          # 付款日期栏预填内容，默认留空手写
    belong_text: str = '月'          # 归属表格栏固定文字
    belong_indent: float = 66.0      # 归属表格栏左侧留白（手写月份数字处，pt）


# ------------------------------------------------------------------ 字体

def font_file() -> str:
    """可嵌入的中文字体文件路径（优先用 PyMuPDF 自带字体）。"""
    global _FONT_PATH
    if _FONT_PATH is not None:
        return _FONT_PATH
    try:
        data = fitz.Font('china-s').buffer
        import os
        import tempfile
        path = os.path.join(tempfile.gettempdir(), 'cgsys_cjk_font.ttf')
        if not os.path.isfile(path) or os.path.getsize(path) != len(data):
            with open(path, 'wb') as fh:
                fh.write(data)
        _FONT_PATH = path
        return path
    except Exception:
        pass
    import os
    windir = os.environ.get('WINDIR', r'C:\Windows')
    for name in ('msyh.ttc', 'simsun.ttc', 'simhei.ttf', 'simkai.ttf'):
        p = os.path.join(windir, 'Fonts', name)
        if os.path.isfile(p):
            _FONT_PATH = p
            return p
    _FONT_PATH = ''
    return ''


def measure_font():
    global _MEASURE
    if _MEASURE is None:
        path = font_file()
        try:
            _MEASURE = fitz.Font(fontfile=path) if path else fitz.Font('china-s')
        except Exception:
            _MEASURE = fitz.Font('china-s')
    return _MEASURE


def _text_width(text: str, size: float) -> float:
    try:
        return measure_font().text_length(text, fontsize=size)
    except Exception:
        return len(text) * size * 0.95


def _put_text(page, point, text: str, size: float, color=DARK) -> None:
    if not text:
        return
    path = font_file()
    try:
        if path:
            page.insert_text(point, text, fontname=FONT_NAME, fontfile=path,
                             fontsize=size, color=color)
        else:
            page.insert_text(point, text, fontname='china-s', fontsize=size, color=color)
    except Exception:
        page.insert_text(point, text, fontsize=size, color=color)


# ------------------------------------------------------------------ 版面工具

class _SrcCache:
    """缓存已打开的发票 PDF，避免同一文件重复打开。"""

    def __init__(self):
        self._docs: Dict[str, Optional[fitz.Document]] = {}

    def get(self, path: str):
        if path not in self._docs:
            try:
                self._docs[path] = fitz.open(path)
            except Exception:
                self._docs[path] = None
        return self._docs[path]

    def close(self):
        for doc in self._docs.values():
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
        self._docs.clear()


def _fit_rect(cell, w: float, h: float, pad: float = 2.0):
    avail_w = max(1.0, cell.width - 2 * pad)
    avail_h = max(1.0, cell.height - 2 * pad)
    if w <= 0 or h <= 0:
        return cell
    scale = min(avail_w / w, avail_h / h)
    tw, th = w * scale, h * scale
    cx, cy = (cell.x0 + cell.x1) / 2.0, (cell.y0 + cell.y1) / 2.0
    return fitz.Rect(cx - tw / 2, cy - th / 2, cx + tw / 2, cy + th / 2)


def _cells(page_rect, count: int, margin: float, gap: float):
    """把页面纵向等分为 count 个单元格。"""
    count = max(1, count)
    top = page_rect.y0 + margin
    bottom = page_rect.y1 - margin
    usable = bottom - top - gap * (count - 1)
    cell_h = usable / count
    out = []
    for i in range(count):
        y0 = top + i * (cell_h + gap)
        out.append(fitz.Rect(page_rect.x0 + margin, y0, page_rect.x1 - margin, y0 + cell_h))
    return out


def _wrap(text: str, size: float, max_width: float) -> List[str]:
    """按宽度折行（中英文混排，逐字符累加）。"""
    text = (text or '').replace('\r', '')
    lines: List[str] = []
    for para in str(text).split('\n'):
        if not para:
            lines.append('')
            continue
        cur = ''
        for ch in para:
            trial = cur + ch
            if _text_width(trial, size) > max_width and cur:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        lines.append(cur)
    return lines


def _draw_textbox(page, rect, text: str, size: float, color=DARK) -> float:
    """绘制自动折行的文本，返回实际高度。"""
    if not text:
        return 0.0
    line_h = size * 1.45
    max_lines = max(1, int(rect.height // line_h))
    lines = _wrap(text, size, rect.width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if lines:
            lines[-1] = lines[-1][:-1] + '…'
    y = rect.y0 + size
    for ln in lines:
        _put_text(page, (rect.x0, y), ln, size, color)
        y += line_h
    return y - rect.y0


def _fmt_num(value) -> str:
    if value is None or value == '':
        return ''
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return ('%.2f' % f).rstrip('0').rstrip('.')


# ------------------------------------------------------------------ 背面字段

BACK_LABELS = ('物品名称', '用途', '组别', '付款日期', '归属表格')


def back_fields(item, opts: MergeOptions, more_names=None) -> List:
    """背面 5 个字段：[(标签, 内容, 左侧留白pt)]。

    「物品名称」只取采购表「物料名称/品牌」那一栏的原值，不附加规格或「等N项」；
    「付款日期」默认留空供手写；「归属表格」固定印「月」，左侧留白写月份数字。

    一张发票覆盖好几条采购时（淘宝一个订单买几样、商家只开一张票），
    名称要**逐行都列出来** —— 只印主条目那一样，对不上账。
    """
    item = item or {}
    group = (item.get('group_name') or '').strip() or opts.group
    names = [(item.get('name') or '').strip()]
    for extra in (more_names or []):
        extra = (extra or '').strip()
        if extra and extra not in names:
            names.append(extra)
    return [
        ('物品名称', '\n'.join(n for n in names if n), 0.0),
        ('用途', (item.get('purpose') or '').strip(), 0.0),
        ('组别', group, 0.0),
        ('付款日期', opts.pay_date_text, 0.0),
        ('归属表格', opts.belong_text, opts.belong_indent),
    ]


def invoice_meta_text(inv, item) -> str:
    """背面底部的发票小字：有就印，没有就空着。"""
    inv = inv or {}
    item = item or {}
    parts = []
    if inv.get('invoice_no'):
        parts.append('发票号码 ' + str(inv['invoice_no']))
    if inv.get('invoice_date'):
        parts.append('开票日期 ' + str(inv['invoice_date']))
    amount = inv.get('amount')
    if amount is None:
        amount = item.get('paid')
    if amount is None:
        amount = item.get('total')
    if amount is not None:
        parts.append('价税合计 ¥' + _fmt_num(amount))
    if inv.get('seller'):
        parts.append('销售方 ' + str(inv['seller']))
    return '    '.join(parts)


def _draw_back_block(page, cell, fields: Sequence, meta: str, opts: MergeOptions) -> None:
    """在 cell 内绘制一块「版面内容」。"""
    box = fitz.Rect(cell.x0 + 10, cell.y0 + 6, cell.x1 - 10, cell.y1 - 6)
    if opts.show_back_frame:
        page.draw_rect(box, color=LIGHT, width=0.7)

    inner = fitz.Rect(box.x0 + 14, box.y0 + 12, box.x1 - 14, box.y1 - 10)
    y = inner.y0

    if opts.title:
        h = _draw_textbox(page, fitz.Rect(inner.x0, y, inner.x1, y + 26),
                          opts.title, opts.font_size + 3, DARK)
        y += max(h, 18) + 6

    label_size = value_size = opts.font_size
    label_w = 0.0
    for item in fields:
        label_w = max(label_w, _text_width(item[0] + '：', label_size))
    label_w = min(label_w, inner.width * 0.55)

    meta_size = max(7.5, opts.font_size - 3.5)
    meta_h = (meta_size * 1.45 * 2 + 10) if opts.show_invoice_meta else 0.0
    rows_area = fitz.Rect(inner.x0, y, inner.x1, inner.y1 - meta_h)

    n = max(1, len(fields))
    row_h = max(26.0, min(58.0, rows_area.height / n))
    for i, item in enumerate(fields):
        label, value = item[0], item[1]
        indent = item[2] if len(item) > 2 else 0.0
        ry0 = rows_area.y0 + i * row_h
        ry1 = min(rows_area.y1, ry0 + row_h)
        if ry0 >= rows_area.y1:
            break
        base = ry0 + min(row_h - 6, value_size * 1.9)
        _put_text(page, (inner.x0, base), label + '：', label_size, DARK)
        vrect = fitz.Rect(inner.x0 + label_w + 6, base - value_size, inner.x1, ry1 - 3)
        if indent > 0:
            _draw_textbox(page, fitz.Rect(vrect.x0 + indent, vrect.y0, vrect.x1, vrect.y1),
                          value or '', value_size, DARK)
        else:
            _draw_textbox(page, vrect, value or '', value_size, DARK)
        if opts.show_back_line:
            page.draw_line(fitz.Point(vrect.x0, ry1 - 3), fitz.Point(inner.x1, ry1 - 3),
                           color=LIGHT, width=0.6)

    if opts.show_invoice_meta and meta:
        _draw_textbox(page, fitz.Rect(inner.x0, inner.y1 - meta_h + 6, inner.x1, inner.y1),
                      meta, meta_size, GRAY)


def _kind_of(path: Path) -> str:
    return 'pdf' if path.suffix.lower() == '.pdf' else 'image'


def _place_invoice(page, cell, path: Path, cache: _SrcCache, border: bool = True) -> None:
    if border:
        page.draw_rect(cell, color=(0.85, 0.85, 0.85), width=0.4, dashes='[2 2] 0')
    if _kind_of(path) == 'pdf':
        doc = cache.get(str(path))
        if doc is None or doc.page_count == 0:
            _draw_placeholder(page, cell, path, 'PDF 无法打开')
            return
        src_rect = doc[0].rect
        rect = _fit_rect(cell, src_rect.width, src_rect.height)
        try:
            page.show_pdf_page(rect, doc, 0)
        except Exception as exc:
            _draw_placeholder(page, cell, path, 'PDF 嵌入失败：%s' % exc)
    else:
        # 用 PyMuPDF 自己读图片尺寸，不需要额外装 Pillow
        try:
            rect = None
            try:
                pix = fitz.Pixmap(str(path))
                rect = _fit_rect(cell, float(pix.width), float(pix.height))
                pix = None
            except Exception:
                rect = None
            if rect is None:
                page.insert_image(cell, filename=str(path))
            else:
                page.insert_image(rect, filename=str(path))
        except Exception as exc:
            _draw_placeholder(page, cell, path, '图片插入失败：%s' % exc)


def _draw_placeholder(page, cell, path: Path, msg: str) -> None:
    page.draw_rect(cell, color=(0.8, 0.2, 0.2), width=0.8)
    _draw_textbox(page, fitz.Rect(cell.x0 + 10, cell.y0 + 10, cell.x1 - 10, cell.y1 - 10),
                  '%s\n%s' % (path.name, msg), 10, (0.7, 0.1, 0.1))


def _page_footer(page, text: str, opts: MergeOptions) -> None:
    _draw_textbox(page, fitz.Rect(opts.page_margin, A4_H - opts.page_margin + 2,
                                  A4_W - opts.page_margin, A4_H - 4),
                  text, 7.5, GRAY)


# ------------------------------------------------------------------ 组装

def read_amount(path):
    """从发票 PDF 的**文字层**里读「价税合计」。读不到就返回 None。

    电子发票的标准写法是「价税合计（小写）¥16.00」，优先认它；
    扫描件 / 图片转出来的票**没有文字层**，这时候返回 None，
    让调用方留空手填 —— 别瞎猜一个数字填进"实付金额"。

    ⚠️ 这只用来**预填**，不能用来**认亲**：实测票面金额和条目金额经常差几毛到几块
    （16.00 vs 17.0、192 vs 182），拿金额去自动配对一定会配错。
    """
    try:
        doc = fitz.open(str(path))
    except Exception:
        return None
    try:
        text = ''
        for page in doc:
            text += page.get_text() or ''
    finally:
        doc.close()
    if not text.strip():
        return None

    # ① 明细写法：「价税合计（小写）¥16.00」及其变体
    for pat in (
        r'价税合计\s*[（(]\s*小写\s*[）)]\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)',
        r'[（(]\s*小写\s*[）)]\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)',
        r'价税合计[^0-9¥￥]{0,14}[¥￥]?\s*(\d+(?:\.\d{1,2})?)',
    ):
        m = re.search(pat, text)
        if m:
            return round(float(m.group(1)), 2)

    # ② 退回：发票上带 ¥ 的数字里**最大**的那个，通常就是价税合计
    from app.core.mailfetch import extract_amounts
    nums = extract_amounts(text)
    return max(nums) if nums else None


# ---------------------------------------------------------------- OCR（照片 / 扫描件的兜底）

# 「价税合计」那一块的关键词。OCR 经常把它认残（「合计」认成「吐 计」），
# 所以宁可多列几个，也不指望单个词。
_TOTAL_KEYS = ('价税合计', '小写', '含税合计', '合计金额')

_MONEY_YEN = re.compile(r'[¥￥Y]\s*(\d{1,7}(?:\s*\.\s*\d{1,2})?)')
# OCR 常在「小数点后」插个空格（`172. 28`、`4174. 00`），所以小数部分允许中间有空白；
# 反过来，`172. 27722772`（单价那一长串）后面还跟着数字，靠 `(?!\d)` 排掉。
_MONEY_DEC = re.compile(r'(?<![\d.])(\d{1,7}(?:\s*\.\s*\d{2}))(?!\s*\d)')

def ocr_available() -> bool:
    """这台机器装了 tesseract 没有。没装就别试 —— 认不出比认错好。

    本地开发机一般没装，线上装了，**这是正常状态**：
    代码要能安静地退回「让人手填」。
    """
    import shutil
    return bool(shutil.which('tesseract'))


def _ocr_text(path, dpi=300, language='chi_sim+eng', timeout=60):
    """把第一页渲染成图，交给 **tesseract 命令行**认字，返回认到的文字。

    为什么不用 PyMuPDF 自带的 `page.get_textpage_ocr()`：它只认**模块 import
    那一刻**的环境变量 `TESSDATA_PREFIX`，之后再设也没用 —— 直接
    `RuntimeError: No OCR support: TESSDATA_PREFIX not set`
    （线上真踩过：命令行先设好变量就成功，代码里设就炸）。
    而 tesseract 自己知道语言包在哪，走命令行反而少一个坑，
    也方便给单张图加超时。

    认不出来（没装 tesseract、图打不开、超时）一律返回 None，交给人工。
    """
    import os
    import shutil
    import subprocess
    import tempfile

    exe = shutil.which('tesseract')
    if not exe:
        return None
    try:
        doc = fitz.open(str(path))
    except Exception:
        return None
    try:
        if doc.page_count == 0:
            return None
        zoom = max(1.0, dpi / 72.0)
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    except Exception:
        return None
    finally:
        doc.close()

    tmpdir = tempfile.mkdtemp(prefix='cgsys-ocr-')
    try:
        img = os.path.join(tmpdir, 'page.png')
        pix.save(img)
        proc = subprocess.run([exe, img, 'stdout', '-l', language],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout)
        return proc.stdout.decode('utf-8', 'replace') or None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _moneys(line):
    """一行里所有像金额的数：带 ¥ 的优先，否则退到「两位小数」的数。

    票号、日期那种一长串数字不会被认成金额（没有小数点），
    这也是为什么整数字一律不收 —— 免得把「2026」当成钱。
    """
    def num(s):
        return float(s.replace(' ', ''))
    vals = [num(x) for x in _MONEY_YEN.findall(line or '')]
    if not vals:
        vals = [num(x) for x in _MONEY_DEC.findall(line or '')]
    return [v for v in vals if v > 0]


def _trim_leading(v):
    """去掉整数部分的第一位。

    OCR 常把「¥」认成数字并和金额粘起来：`¥174.00` → `4174.00`。
    整数部分**至少 4 位**才谈得上"多认了一位"——
    3 位的 174 去掉首位成了 74，那是瞎猜。
    """
    whole, _, frac = ('%.2f' % v).partition('.')
    if len(whole) < 4:
        return None
    return round(float(whole[1:] + '.' + (frac or '0')), 2)


def _squash(line):
    """压掉一行里所有空白。

    OCR 爱在**中文字之间**插空格：`价 税 合 计`、`小 写`。关键词得先压干净再比，
    否则整行都匹配不上（真踩过：那张电脑桌的票就是这么解析成 None 的）。
    """
    return re.sub(r'\s+', '', line or '')


def amount_from_ocr_text(text, expect=None):
    """从 OCR 出来的文字里挑出「价税合计」；认不出返回 None（**不猜**）。

    `expect` 是这条采购自己算出来的合计，只用来**消歧**：
    候选里同时有 `4174.00` 和它去掉首位后的 `174.00` 时，
    哪个更接近 179 就是哪个。票面和条目差几毛几块是常态，
    差一个数量级一定是认错了 —— 认错宁可不填：人对着票填一次，
    总比把一个错的数写进财务数据强。
    """
    lines = [l.strip() for l in (text or '').splitlines() if l.strip()]
    if not lines:
        return None

    cands, sums = [], []
    for i, line in enumerate(lines):
        flat = _squash(line)          # 关键词先压掉空格再比（「价 税 合 计」）
        if any(k in flat for k in _TOTAL_KEYS):
            vals = _moneys(line)
            if not vals and i + 1 < len(lines):
                vals = _moneys(lines[i + 1])
            for v in vals:
                cands.append(v)
                trimmed = _trim_leading(v)
                if trimmed is not None:
                    cands.append(trimmed)
        elif '合计' in flat:
            sums += _moneys(line)

    # ① 票面自己的关系：价税合计 = 金额合计 + 税额。对得上就没得说，最可信。
    if len(sums) >= 2:
        want = round(sums[0] + sums[1], 2)
        for c in cands:
            if abs(c - want) < 0.02:
                return c
        if not cands:
            return want

    if not cands:
        return None
    cands = sorted(set(cands))
    if expect:
        best = min(cands, key=lambda x: abs(x - expect))
        if abs(best - expect) <= max(20.0, 0.3 * expect):
            return best
        return None
    # 没有能对照的数：只有一个候选才敢用（多了就是拿不准）
    return cands[0] if len(cands) == 1 else None


def read_amount_ocr(path, expect=None, dpi=300, language='chi_sim+eng'):
    """照片 / 扫描件的兜底：OCR 认票面金额，认不出返回 None。

    **只在读不出文字层时才值得跑** —— 电子发票有文字层，`read_amount()` 又准又快；
    OCR 慢（一张 A4 两三秒）而且会认错字（`¥` 认成 `4`、`柒` 认成 `染`）。
    所以认出来也只当**建议值**（界面上要人核一眼），认不出就照旧手填。
    """
    text = _ocr_text(path, dpi=dpi, language=language)
    if not text:
        return None
    return amount_from_ocr_text(text, expect)


def _column_x(words, label_a, label_b):
    """找列头（两个字，如「金」「额」）的 x 范围，返回 (x0, x1, y)。"""
    for i, w in enumerate(words):
        if w[4] == label_a and i + 1 < len(words) and words[i + 1][4] == label_b:
            return w[0], words[i + 1][2], w[1]
        if w[4] == label_a + label_b:
            return w[0], w[2], w[1]
    return None, None, None


def _column_span(words, labels):
    """找列头的 x 起点。labels 是若干个可能写法（如 ('规格型号', '规格')）。"""
    for w in words:
        for lab in labels:
            if w[4].replace(' ', '') == lab:
                return w[0]
    return None


def _read_detail_lines(doc):
    """把明细区按 y 分行，读成 [{'name', 'amount', 'tax', 'total'}]。

    难点：「项目名称」那一列很宽，会被 PDF 切成好几行（一个商品名占 4 行）。
    所以先按 y 把词归行、再按 y 顺序把属于同一商品的分片拼起来。
    """
    lines = []
    for page in doc:
        words = page.get_text('words')      # x0, y0, x1, y1, word, ...
        if not words:
            continue

        # 列头位置
        amount_x0, amount_x1, head_y = _column_x(words, '金', '额')
        tax_x0, tax_x1, _ = _column_x(words, '税', '额')
        if amount_x0 is None:
            continue
        # 「项目名称」列的右边界 = 下一个列头（规格型号）的左边界
        name_x1 = _column_span(words, ('规格型号', '规格')) or (amount_x0 - 40)

        # 数据区里，按 y 归档
        rows = {}
        for w in words:
            if w[1] <= head_y:
                continue
            y = round(w[1], 1)
            rows.setdefault(y, []).append(w)

        # 先挑出"有金额"的那些 y —— 那就是每个商品的最后一行
        anchors = []
        for y in sorted(rows):
            for w in rows[y]:
                cx = (w[0] + w[2]) / 2.0
                if amount_x0 - 6 <= cx <= amount_x1 + 6:
                    t = (w[4].replace('¥', '').replace('￥', '')
                         .replace(',', '').strip())
                    if re.fullmatch(r'\d+(?:\.\d{1,2})?', t):
                        anchors.append((y, round(float(t), 2)))
        if not anchors:
            continue

        # 去掉「合计」那一行（它等于前面各行之和）
        amounts = [a for _, a in anchors]
        if len(amounts) > 1 and abs(amounts[-1] - round(sum(amounts[:-1]), 2)) < 0.02:
            anchors = anchors[:-1]

        # 每个锚点往上收名称碎片，直到上一个锚点为止
        prev_y = head_y
        for i, (y, amount) in enumerate(anchors):
            chunk = []
            for yy in sorted(rows):
                if not (prev_y < yy <= y):
                    continue
                for w in rows[yy]:
                    if w[0] < name_x1 and not w[4].startswith('*'):
                        chunk.append(w[4])
                    elif w[0] < name_x1:
                        chunk.append(w[4])      # 明细行常以 *类目* 开头，留着
            name = ''.join(chunk).strip()
            name = re.sub(r'^\*[^*]*\*', '', name)      # 去掉 *电子元件* 这类前缀
            # 同一行的税额
            tax = 0.0
            if tax_x0 is not None:
                for w in rows.get(y, []):
                    cx = (w[0] + w[2]) / 2.0
                    if tax_x0 - 6 <= cx <= tax_x1 + 6:
                        t = (w[4].replace('¥', '').replace('￥', '')
                             .replace(',', '').strip())
                        if re.fullmatch(r'\d+(?:\.\d{1,2})?', t):
                            tax = round(float(t), 2)
            lines.append({'name': name, 'amount': amount, 'tax': tax,
                          'total': round(amount + tax, 2)})
            prev_y = y
    return lines


def read_lines(path) -> List[Dict]:
    """取发票明细的**每一行**：名称 + 「金额」（不含税）+「税额」→ 含税合计。

    [{'name': '贴片电容1u2.2u...', 'amount': 21.78, 'tax': 0.22, 'total': 22.0}]

    为什么一票多单非要这个：一张票买了好几样，每条采购对应**某一行**，
    价税合计（总额）对不上任何一条。而「实付金额」要填**含税**的。

    为什么按**坐标**取而不是按文本流：真票里「单价」和「金额」经常是同一个数
    （数量 = 1 时都是 204.95），而且 get_text() 出来的词序是乱的。

    读不到文字层（扫描件/图片票）返回空列表 —— 宁可让人手填，不瞎猜。
    """
    try:
        doc = fitz.open(str(path))
    except Exception:
        return []
    try:
        return _read_detail_lines(doc)
    finally:
        doc.close()


def read_lines_total(path):
    """明细各行的**含税**金额之和；读不到返回 None。"""
    lines = read_lines(path)
    if not lines:
        return None
    return round(sum(x['total'] for x in lines), 2)


def build_merged_pdf(entries: Sequence, out_path, opts: Optional[MergeOptions] = None):
    """把 entries 合并成一份 PDF。

    entries: [{'item': 条目 dict, 'invoice': 发票 dict, 'path': 发票文件 Path}]
    返回 (输出路径, 统计信息 dict)。
    """
    opts = opts or MergeOptions()
    entries = list(entries)
    if not entries:
        raise ValueError('没有可合并的发票')

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    cache = _SrcCache()
    per_page = max(1, min(4, int(opts.per_page)))
    total_groups = (len(entries) + per_page - 1) // per_page

    try:
        for g in range(total_groups):
            chunk = entries[g * per_page:(g + 1) * per_page]

            front = doc.new_page(width=A4_W, height=A4_H)
            cells = _cells(front.rect, per_page, opts.page_margin, opts.cell_gap)
            for i, e in enumerate(chunk):
                _place_invoice(front, cells[i], Path(e['path']), cache,
                               border=opts.show_cut_line)
            if opts.show_footer:
                _page_footer(front, '第 %d 组 / 共 %d 组   正面（发票）' % (g + 1, total_groups), opts)

            if opts.make_back:
                back = doc.new_page(width=A4_W, height=A4_H)
                bcells = _cells(back.rect, per_page, opts.page_margin, opts.cell_gap)
                for i, e in enumerate(chunk):
                    _draw_back_block(back, bcells[i],
                                     back_fields(e.get('item'), opts, e.get('more_names')),
                                     invoice_meta_text(e.get('invoice'), e.get('item')), opts)
                if opts.show_footer:
                    _page_footer(back, '第 %d 组 / 共 %d 组   背面（采购信息版面）' % (g + 1, total_groups), opts)
    finally:
        cache.close()

    # 中文字体子集化嵌入，任何电脑 / 打印机都能正常显示与打印
    try:
        doc.subset_fonts()
    except Exception:
        pass
    doc.set_metadata({'title': '发票合并结果', 'producer': '采购系统'})
    doc.save(str(out), garbage=4, deflate=True, clean=True)
    pages = doc.page_count
    doc.close()

    return out, {
        'invoices': len(entries),
        'groups': total_groups,
        'pages': pages,
        'per_page': per_page,
        'make_back': bool(opts.make_back),
    }
