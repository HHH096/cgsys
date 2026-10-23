# -*- coding: utf-8 -*-
"""按在线表格模板生成 .xlsx：`总表` / `低于一千` / `高于一千`。

模板的两套列布局（实测自 `电气组-8月第一次采购表1.xlsx`）：
  * `总表`   20 列 A–T，「申购人电话」在第 13 列
  * 两个分表 21 列 A–U，在「领用时间」后多插了一列「实际金额」(第 13 列)，
    把「申购人电话」挤到第 14 列；第 19 列也叫「实际金额」，真正填的是第 19 列
    （第 13 列留空，与模板实际用法一致）；`高于一千` 末列是「是否有合同」
"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app import config
from app.core import rules

SHEET_MAIN = '总表'
SHEET_LOW = '低于一千'
SHEET_HIGH = '高于一千'

THIN = Side(style='thin', color='FF000000')
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FILL = PatternFill('solid', fgColor='FF006600')
TOTAL_FILL = PatternFill('solid', fgColor='FFFFFF00')
HEADER_FONT = Font(name='等线', size=11, bold=True, color='FFFFFFFF')
TITLE_FONT = Font(name='等线', size=16, bold=True)
TOTAL_FONT = Font(name='等线', size=11, bold=True)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)

# 需要靠左显示的列（名称、规格、用途、网址、备注）
LEFT_COLS_MAIN = {1, 2, 10, 15, 17}
LEFT_COLS_SPLIT = {1, 2, 10, 16, 18}

# (字段名, 表头) —— 总表 20 列
MAIN_COLUMNS = [
    ('name', '物料名称/品牌'), ('spec', '规格型号'), ('qty', '申请数量'), ('unit', '单位'),
    ('price', '预计单价'), ('subtotal', '小计金额'), ('freight', '运费'), ('total', '合计金额'),
    ('stock', '库存'), ('purpose', '用途'), ('applicant', '申购人'), ('used_at', '领用时间'),
    ('phone', '申购人电话'), ('image_path', '样式/图片'), ('url', '网址链接'),
    ('group_name', '组别'), ('note', '备注'), ('paid', '实付金额'), ('invoice', '发票'),
    ('arrived', '是否到货'),
]

# (字段名, 表头) —— 两个分表 21 列；字段名为 None 表示该列留空
SPLIT_COLUMNS = [
    ('name', '物料名称/品牌'), ('spec', '规格型号'), ('qty', '申请数量'), ('unit', '单位'),
    ('price', '预计单价'), ('subtotal', '小计金额'), ('freight', '运费'), ('total', '合计金额'),
    ('stock', '库存'), ('purpose', '用途'), ('applicant', '申购人'), ('used_at', '领用时间'),
    (None, '实际金额'),                       # 第 13 列：模板里的空列
    ('phone', '申购人电话'), ('image_path', '样式/图片'), ('url', '网址链接'),
    ('group_name', '组别'), ('note', '备注'),
    ('paid', '实际金额'),                     # 第 19 列：真正填的「实际金额」
    ('invoice', '发票'),
    None,                                     # 第 21 列：由调用方按 sheet 决定
]

MAIN_WIDTHS = [14.2, 10.6, 8, 6, 9, 9, 7, 9, 7, 22, 9, 9, 13, 9, 34, 8, 18, 9, 7, 8]
SPLIT_WIDTHS = [18.4, 10.6, 8, 6, 9, 9, 7, 9, 7, 22, 9, 9, 9, 13, 9, 34, 8, 18, 9, 7, 8]
HIGH_WIDTHS = list(SPLIT_WIDTHS[:-1]) + [11.4]


def _blank(v):
    """空字符串写 None，保持表格干净；**以 = + - @ 开头的要防公式注入**。

    openpyxl 会把 `=` 开头的字符串当公式写进单元格。组员在物料名称或备注里
    填 `=HYPERLINK("http://…","点我")`，采购一打开导出的表格就可能被带到外链、
    或者触发 Excel 的外部数据请求。

    前面加个单引号，Excel 就按普通文本处理（单元格显示时不带引号）。
    """
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        if v[0] in '=+-@':
            return "'" + v
        return v
    return v


def _write_title(ws, title, span):
    ws['A1'] = title
    ws['A1'].font = TITLE_FONT
    ws['A1'].alignment = CENTER
    ws.merge_cells(span)
    ws.row_dimensions[1].height = 30


def _write_header(ws, columns):
    for idx, (_, header) in enumerate(columns, start=1):
        c = ws.cell(row=2, column=idx, value=header)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = CENTER
        c.border = BORDER
    ws.row_dimensions[2].height = 30


def _set_widths(ws, widths):
    for idx, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = w


def _write_rows(ws, columns, items, left_cols):
    for offset, item in enumerate(items):
        r = 3 + offset
        for idx, col in enumerate(columns, start=1):
            field = col[0]
            if field is None:
                continue
            c = ws.cell(row=r, column=idx,
                        value=_blank(rules.export_value(item, field)))
            c.border = BORDER
            c.alignment = LEFT if idx in left_cols else CENTER


def _write_total(ws, nrows, columns, label_in_col_one):
    """合计行：H 列写 `=SUM(H3:Hn)`；总表/高于一千 的「合计」在 A 列并合并 A:G。"""
    if nrows == 0:
        return None
    r = 3 + nrows
    for idx in range(1, len(columns) + 1):
        c = ws.cell(row=r, column=idx)
        c.border = BORDER
    label_col = 1 if label_in_col_one else 7
    ws.cell(row=r, column=label_col, value='合计').font = TOTAL_FONT
    ws.cell(row=r, column=label_col).alignment = CENTER
    h = ws.cell(row=r, column=8, value='=SUM(H3:H%d)' % (2 + nrows))
    h.font = TOTAL_FONT
    h.alignment = CENTER
    for idx in range(1, 9):
        ws.cell(row=r, column=idx).fill = TOTAL_FILL
    if label_in_col_one:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
    return r


def build_workbook(round_, items):
    """返回 (Workbook, 明细 dict)。items 需已含算好的 total。"""
    low, high, group_totals = rules.split_items(items, round_.get('split_threshold'))
    title = round_.get('title') or config.DEFAULT_TITLE

    wb = Workbook()

    ws = wb.active
    ws.title = SHEET_MAIN
    _write_title(ws, title, 'A1:O1')
    _write_header(ws, MAIN_COLUMNS)
    _set_widths(ws, MAIN_WIDTHS)
    _write_rows(ws, MAIN_COLUMNS, items, LEFT_COLS_MAIN)
    _write_total(ws, len(items), MAIN_COLUMNS, label_in_col_one=True)

    for sheet_name, rows, last, widths in (
            (SHEET_LOW, low, ('arrived', '是否到货'), SPLIT_WIDTHS),
            (SHEET_HIGH, high, ('has_contract', '是否有合同'), HIGH_WIDTHS)):
        ws = wb.create_sheet(sheet_name)
        columns = list(SPLIT_COLUMNS[:-1]) + [last]
        _write_title(ws, title, 'A1:P1')
        _write_header(ws, columns)
        _set_widths(ws, widths)
        _write_rows(ws, columns, rows, LEFT_COLS_SPLIT)
        _write_total(ws, len(rows), columns, label_in_col_one=(sheet_name == SHEET_HIGH))

    detail = {
        'low': len(low),
        'high': len(high),
        'group_totals': group_totals,
        'sum_low': rules.r2(sum(r.get('total') or 0 for r in low)) or 0.0,
        'sum_high': rules.r2(sum(r.get('total') or 0 for r in high)) or 0.0,
        'sum_all': rules.r2(sum(r.get('total') or 0 for r in items)) or 0.0,
    }
    return wb, detail


def save_round_xlsx(round_, items, path):
    """写出 xlsx，返回 (path, detail)。"""
    wb, detail = build_workbook(round_, items)
    wb.save(str(path))
    return path, detail
