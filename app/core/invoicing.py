# -*- coding: utf-8 -*-
"""发票归档：存盘 + 按工作区既有格式重命名。

沿用现有发票软件的命名规则：`{前缀}-{物品名称}-{金额}.pdf`，例如
`DQ-PCB物料-112.92.pdf`。金额去尾零（112.92 / 23 / 5.1 / 2060），
优先取「实付金额」，没填则取「合计金额」。
"""
import hashlib
import re
from functools import reduce
from pathlib import Path

from app import config

INVALID = re.compile(r'[\\/:*?"<>|\r\n\t]+')
DEFAULT_PREFIX = 'DQ'


def clean_name(text, max_len=40):
    """清掉文件名非法字符，压掉空白。"""
    if text is None:
        return ''
    s = INVALID.sub('', str(text))
    s = re.sub(r'\s+', '', s).strip('.-_')
    return s[:max_len]


def amount_text(value):
    """金额写成工作区那种最简写法：112.92 / 23 / 5.1 / 2060。"""
    if value is None or value == '':
        return ''
    try:
        s = ('%.2f' % float(value)).rstrip('0').rstrip('.')
    except (TypeError, ValueError):
        return ''
    return s or '0'


def invoice_amount(item):
    """优先实付金额，其次合计金额。"""
    for key in ('paid', 'total'):
        v = (item or {}).get(key)
        if v is not None and v != '':
            return v
    return None


def invoice_filename(item, ext, prefix=DEFAULT_PREFIX, amount=None):
    """生成 `DQ-物品名-金额.pdf` 形式的目标文件名。

    `amount` 显式给了就用它 —— **一票多条**时传「这张票覆盖的所有条目合计」，
    而不是主条目自己的金额（否则名字会是 `...-9.24.pdf` 那种，
    看着像整张票只值 9.24，其实票面是 22.14）。
    """
    name = clean_name((item or {}).get('name')) or '发票'
    parts = [clean_name(prefix) or 'FP', name]
    amt = amount_text(invoice_amount(item) if amount is None else amount)
    if amt:
        parts.append(amt)
    if not ext.startswith('.'):
        ext = '.' + ext
    return '-'.join(p for p in parts if p) + ext.lower()


def safe_folder_name(text, max_len=30):
    return clean_name(text, max_len) or '未命名轮次'


def _sha1(data):
    return hashlib.sha1(data).hexdigest()


def unique_target(folder, filename, data):
    """在 folder 里给 data 找一个落点。

    返回 (Path, state)；state 为 `new` 或 `duplicate`（同名且内容一致）。
    """
    folder = Path(folder)
    stem, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
    ext = ('.' + ext) if ext else ''
    target = folder / filename
    n = 1
    while target.exists():
        if target.stat().st_size == len(data) and _sha1(target.read_bytes()) == _sha1(data):
            return target, 'duplicate'
        n += 1
        target = folder / ('%s-%d%s' % (stem, n, ext))
    return target, 'new'


def store_invoice(item, round_name, orig_filename, data):
    """把上传的发票按规则命名后存到 `data/invoices/<轮次名>/`。

    **图片（jpg/png/bmp/tif…）先转成 PDF 再存** —— 见 `image_to_pdf`。
    落盘之后一律是 `.pdf`，`orig_name` 里留着原来的文件名备查。

    返回 dict：stored_name / abs_path / rel_path / size / state / orig_name /
    converted（这次是不是由图片转来的）。
    重复上传同一份文件不会产生副本（state = duplicate）——
    转换是确定性的（同一张图转两次字节一样），所以去重照样有效。
    """
    ext = Path(orig_filename or '').suffix.lower()
    if ext not in config.INVOICE_EXTS:
        raise ValueError('不支持的文件类型：%s（支持 %s）'
                         % (ext or '(无后缀)', '、'.join(sorted(config.INVOICE_EXTS))))
    if not data:
        raise ValueError('文件是空的')

    converted = ext in IMAGE_EXTS
    if converted:
        data = image_to_pdf(data)
        ext = '.pdf'                     # 转完就是 PDF 了，命名也跟着用 .pdf

    folder = Path(config.INVOICE_DIR) / safe_folder_name(round_name)
    folder.mkdir(parents=True, exist_ok=True)
    target, state = unique_target(folder, invoice_filename(item, ext), data)
    if state == 'new':
        with open(str(target), 'wb') as fh:
            fh.write(data)
    return {
        'orig_name': orig_filename or '',
        'stored_name': target.name,
        'abs_path': target,
        'rel_path': target.relative_to(config.DATA_DIR).as_posix(),
        'size': target.stat().st_size,
        'state': state,
        'converted': converted,
    }


# 能自动转成 PDF 的图片格式。商家发的截图、手机拍的照片大多是这几种。
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'}


def image_to_pdf(data):
    """把图片发票转成 PDF 字节；不是图片、或者图坏了就抛 ValueError。

    为什么非要转：**合并打印、命名、预览、归档都只认 PDF 一条路**。
    同一批票里混着 jpg 和 pdf，排版代码就得两套分支，文件名也看不出金额；
    统一成 `DQ-物品名-金额.pdf` 之后，看、存、合并都一样处理。

    注意：**图片没有文字层，转出来的 PDF 照样读不出金额**
    （`invoicepdf.read_lines()` 返回空），所以上传完还是会让人补实付金额 ——
    那是预期的，不是转换失败。

    页面尺寸沿用图片自带的 DPI（截图/照片一般 96dpi），也就是
    「一页 = 这张图的物理大小」；合并打印时再按 A4 等比缩放，只重采样一次。
    """
    import fitz
    try:
        doc = fitz.open(stream=data, filetype=None)
    except Exception as exc:
        raise ValueError('这张图片打不开（%s）—— 可能不是真正的图片文件' % exc)
    try:
        if not doc.page_count:
            raise ValueError('这张图片里没有内容')
        return doc.convert_to_pdf()
    finally:
        doc.close()


def delete_file(rel_path):
    """按相对路径删掉归档文件；文件不在也不报错。"""
    if not rel_path:
        return
    try:
        p = Path(config.DATA_DIR) / rel_path
        p.unlink()
    except (OSError, ValueError):
        pass


def rename_to_amount(record, item, amount=None):
    """按 `DQ-物品名-金额.ext` **重新命名**归档文件（识别出金额之后调一次）。

    为什么需要：上传时先归档、后识别金额 —— 归档那会儿条目上还没有金额，
    文件名里就没有金额段（或者用的是旧值）。识别完再改一次名，文件名才跟钱对得上。

    `amount` 显式给了就用它（**一票多条**时传「该票覆盖的所有条目合计」）。
    没识别出金额、或者名字已经对了，就原样返回，不做无谓的改名。
    """
    old = Path(record['abs_path'])
    if not old.exists():
        return record
    want = invoice_filename(item, old.suffix, amount=amount)
    if old.name == want:
        return record
    target = old.parent / want
    n = 2
    while target.exists():
        stem, dot, ext = want.rpartition('.')
        target = old.parent / ('%s-%d%s' % (stem, n, ('.' + ext) if dot else ''))
        n += 1
    try:
        old.rename(target)
    except OSError:
        return record
    return dict(record, stored_name=target.name,
                rel_path=target.relative_to(config.DATA_DIR).as_posix(),
                size=target.stat().st_size)


def total_size(records):
    return reduce(lambda a, r: a + (r.get('size') or 0), records, 0)
