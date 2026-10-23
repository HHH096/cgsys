# -*- coding: utf-8 -*-
"""FastAPI 路由：组员 / 轮次 / 条目 / 状态 / 发票 / 导出 / 静态前端。"""
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app import config
from app.core import (blockexport, exporter, invoicepdf, invoicematch, invoicing,
                      mailfetch,
                      mailbox, netinfo, repo, roster, rules, settings,
                      shareparse, shareqr)
from app.core.db import init_db


# ------------------------------------------------------------------ 身份

def require_purchase(request: Request) -> None:
    """采购人员专用接口的门卫。

    这是**唯一一条硬边界**，而且必须在服务端执行 —— 前端把按钮藏起来挡不住 F12。
    """
    if not settings.verify_token(request.cookies.get(settings.COOKIE_NAME)):
        raise HTTPException(401, '需要采购人员登录')


def is_purchase(request: Request) -> bool:
    return settings.verify_token(request.cookies.get(settings.COOKIE_NAME))


def current_member_id(request: Request):
    """组员选的名字对应的 id。

    **验过签**才认（见 `settings.sign_member`）—— 以前 cookie 里就是明文 id，
    改一个数字就能冒充别人去改条目、改实付、删发票。
    """
    return settings.read_member(request.cookies.get(settings.MEMBER_COOKIE))


def _guard_own_item(request: Request, item) -> None:
    """组员只能动**自己**的条目；采购人员随意。

    组员入口按设计不设密码，所以这里的隔离是「君子协定」级别：
    防手滑改错别人的东西，不防存心使坏。真正的边界是上面那条登录校验。
    """
    if is_purchase(request):
        return
    mid = current_member_id(request)
    if not mid or int(item.get('member_id') or 0) != mid:
        raise HTTPException(403, '只能操作自己填的条目')

# 发票预览/下载用的 MIME
MEDIA_TYPES = {
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.bmp': 'image/bmp',
    '.tif': 'image/tiff',
    '.tiff': 'image/tiff',
    '.xml': 'application/xml',
    '.ofd': 'application/octet-stream',
}

# 单张发票大小上限（局域网自用，防手滑传巨大文件）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# JSON 请求体上限：正常表单（一两个条目）远小于这个，加个上限防止一个大 body 吃光内存
MAX_JSON_BYTES = 2 * 1024 * 1024

# 一次「上传合并」最多几个文件：每个文件本身还有 MAX_UPLOAD_BYTES 限制，
# 但文件数不设上限的话，几十个大文件一起照样能把内存打满
MAX_MERGE_FILES = 50


async def _read_upload(file, limit):
    """边读边计数，超限立刻停。

    以前是 `await file.read()` 一次性读进来**再**判大小 —— 一个 500MB 的上传会先把
    内存吃满才被拒绝；服务在公网上时这就是个现成的内存炸弹。
    """
    chunks, total = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(400, '文件太大（上限 %d MB）'
                                % (limit // 1024 // 1024))
        chunks.append(chunk)
    return b''.join(chunks)


def _mail_loop(stop):
    """按配置里的间隔自动收信。

    `auto_interval <= 0` 时不做收信（只是定期看一眼配置）；每轮都重读配置，
    所以改了间隔不用重启服务，最多晚一个周期生效。
    """
    while not stop.wait(30):
        try:
            cfg = mailbox.load_config()
            interval = max(0, int(cfg.auto_interval or 0))
            if interval <= 0 or not cfg.ready:
                continue
            if stop.wait(interval * 60):
                return
            mailfetch.collect(cfg)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app):
    config.ensure_dirs()
    init_db()
    roster.import_roster()
    stop = threading.Event()
    threading.Thread(target=_mail_loop, args=(stop,), daemon=True).start()
    try:
        yield
    finally:
        stop.set()


# docs / redoc / openapi 全部关掉：这套系统跑在公网上，
# 没必要把「所有接口和参数」白送给路人 —— 中间件只管 `/api/`，管不到这几个页面。
app = FastAPI(title='采购系统', version='0.2.0', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


# 这些路径不用解锁：登录本身，以及页面初始化要用的状态
OPEN_WHEN_LOCKED = frozenset([
    '/api/meta',
    '/api/member/login', '/api/member/status',
    '/api/purchase/login', '/api/purchase/status',
    # 首次给采购入口设密码时必须能匿名调用（那时还没登录），
    # 否则就死锁了：进不去→设不了密码→设不了密码→进不去。
    # 这个路由自己会判断：改密码要求已登录并验旧密码。
    '/api/purchase/password',
])


@app.middleware('http')
async def _require_unlock(request, call_next):
    """**统一把关**：没解锁的接口请求一律 401。

    为什么放在中间件里、而不是逐个接口挂依赖：一旦服务通过隧道暴露到公网，
    「漏挂一个」就是敞开一扇门 —— 而逐个挂依赖迟早会漏。
    白名单里只有登录和状态这类必须能匿名访问的东西。

    两种人能过：
    * **采购人员**（登录过 `/purchase`）
    * **组员**（输对了全组共用的口令）

    **没设过组员口令时，组员入口一律不放行** —— 宁可提示「去设一个」，
    也不留一道敞开的门。
    """
    path = request.url.path
    if path.startswith('/api/') and path not in OPEN_WHEN_LOCKED:
        jar = request.cookies
        ok = (settings.verify_token(jar.get(settings.COOKIE_NAME),
                                    settings.KIND_PURCHASE)
              or settings.verify_token(jar.get(settings.MEMBER_PASS),
                                       settings.KIND_MEMBER))
        if not ok:
            return JSONResponse({'detail': '需要口令'}, status_code=401)
    return await call_next(request)


@app.middleware('http')
async def _limit_json_body(request, call_next):
    """JSON 请求体加个上限 —— 没上限时一个超大 body 就能在解析阶段把内存吃光。

    只盯 `content-type: json` 的请求：上传类端点走 multipart，它们各自有
    单文件大小和文件数的检查（`MAX_UPLOAD_BYTES` / `MAX_MERGE_FILES`）。
    """
    if request.method in ('POST', 'PUT', 'PATCH'):
        ctype = (request.headers.get('content-type') or '').lower()
        if 'json' in ctype:
            cl = request.headers.get('content-length')
            if cl and cl.isdigit() and int(cl) > MAX_JSON_BYTES:
                return JSONResponse(
                    {'detail': '请求体太大（上限 %d KB）' % (MAX_JSON_BYTES // 1024)},
                    status_code=413)
    return await call_next(request)


@app.middleware('http')
async def _no_cache_for_the_frontend(request, call_next):
    """页面和静态资源一律「先问服务器，再用缓存」。

    不这样的话，升级版本之后浏览器会接着用旧的 styles.css / app.js，
    表现就是「代码改了但界面没变」—— 联调时真踩过一次：
    明明加了按角色隐藏区块的规则，页面上却全都在。
    局域网里这些文件都很小，这点开销可以忽略。
    """
    resp = await call_next(request)
    path = request.url.path
    if path.endswith(('.js', '.css', '.html')):
        resp.headers['Cache-Control'] = 'no-cache'
    # 页面本身（/ 与 /purchase）由路由设成 no-store，这里不要覆盖它
    return resp


@app.middleware('http')
async def _security_headers(request, call_next):
    """几个基本的响应头，挡掉最常见的浏览器侧风险。

    * `nosniff`：别让浏览器把上传的文件猜成别的类型去执行
    * `SAMEORIGIN`：别被外部站点嵌进 iframe 里骗点击
    * `same-origin`：跨站请求别带 referer（地址里可能有单号、token）
    """
    resp = await call_next(request)
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


# ------------------------------------------------------------------ 模型

class MemberIn(BaseModel):
    name: str
    phone: str = ''
    is_owner: bool = False


class RoundIn(BaseModel):
    name: str
    title: Optional[str] = None
    group_name: Optional[str] = None
    threshold: Optional[float] = None


class StatusIn(BaseModel):
    status: str


class ItemStatusIn(BaseModel):
    order_status: Optional[str] = None
    invoice_status: Optional[str] = None


class ItemIn(BaseModel):
    member_id: Optional[int] = None
    name: str = ''
    spec: str = ''
    qty: Optional[float] = None
    unit: str = ''
    price: Optional[float] = None
    freight: Optional[float] = 0
    stock: str = ''
    purpose: str = ''
    applicant: str = ''
    payer: str = ''
    used_at: str = ''
    phone: str = ''
    image_path: str = ''
    url: str = ''
    group_name: str = ''
    note: str = ''
    paid: Optional[float] = None
    invoice: str = ''
    arrived: str = ''
    has_contract: str = ''
    split_override: str = ''
    # 并发用：提交时带上「我打开这条时它是什么版本」。
    # 对不上 = 我还在填的时候别人已经改过这一条了（见 api_update_item）。
    # 不传就不检查（老客户端、脚本调接口都不受影响）。
    base_rev: Optional[int] = None


class ReorderIn(BaseModel):
    ids: list


class ShareIn(BaseModel):
    text: str = ''


class MailConfigIn(BaseModel):
    """保存邮箱设置时允许改的字段。

    ⚠️ **这里漏一个字段，前端那个开关就是死的**：Model 里没有声明的字段会被静默丢掉，
    保存成功、界面看着也勾着，但后端一直是默认值。`link_download` / `scan_read` /
    `auto_attach` 就都在这里补过 —— 漏过一回，用户以为开了其实没开。
    """
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None      # 不传 = 不改（前端没动授权码框）
    folder: Optional[str] = None
    use_ssl: Optional[bool] = None
    days: Optional[int] = None
    unseen_only: Optional[bool] = None
    keywords: Optional[list] = None
    any_attachment: Optional[bool] = None
    mark_read: Optional[bool] = None
    max_mails: Optional[int] = None
    auto_interval: Optional[int] = None
    link_download: Optional[bool] = None    # 正文里只有链接的票（淘宝/阿里）
    scan_read: Optional[bool] = None        # 也扫已读邮件
    auto_attach: Optional[bool] = None      # 收到就自动挂到条目上


class MailAttachIn(BaseModel):
    mail_id: int
    item_id: int


class InvoiceItemsIn(BaseModel):
    item_ids: list = []


class InvoiceLineIn(BaseModel):
    item_id: int
    paid: Optional[float] = None


class InvoiceAssignIn(BaseModel):
    """人工核对后的分配结果：每条明细挂到哪条采购、实付填多少。"""
    lines: list = []


class RefundIn(BaseModel):
    refunded: bool = True
    refund_amount: Optional[float] = None


class PaidIn(BaseModel):
    """补填实付金额。空 = 清掉（比如发现填错了）。

    `base_rev` 是「你看到这条时它的版本号」。带上它，就会在写入前比对：
    中途被别人改过就拒绝，而不是默默把别人的数字盖掉。
    不传 = 不检查（脚本、老页面照旧能用）。
    """
    paid: Optional[float] = None
    base_rev: Optional[int] = None


# ------------------------------------------------------------------ 帮助

def _round_or_404(rid):
    r = repo.get_round(rid)
    if not r:
        raise HTTPException(404, '轮次不存在')
    return r


def _item_or_404(iid):
    it = repo.get_item(iid)
    if not it:
        raise HTTPException(404, '条目不存在')
    return it


def _split_preview(round_, items):
    low, high, group_totals = rules.split_items(items, round_.get('split_threshold'))
    return {
        'low_count': len(low),
        'high_count': len(high),
        'low_sum': rules.r2(sum(i.get('total') or 0 for i in low)) or 0.0,
        'high_sum': rules.r2(sum(i.get('total') or 0 for i in high)) or 0.0,
        'all_sum': rules.r2(sum(i.get('total') or 0 for i in items)) or 0.0,
        'threshold': round_.get('split_threshold'),
        'groups': [{'supplier': k.split(':', 1)[1] or '(未填商家)', 'sum': v,
                    'high': v >= (round_.get('split_threshold') or 0)}
                   for k, v in group_totals.items()],
    }


def _rename_invoice_by_links(inv_id):
    """按这张票**当前挂载的条目合计**给归档文件重命名。

    一票多条时名字里要用总额（`...-22.14.pdf`），而不是主条目自己的 9.24 ——
    否则一眼看去像"这张票只值 9.24"。归属一变（改挂、核对落库）就调一次；
    名字已经对就不动（`rename_to_amount` 自己会判断）。
    """
    inv = repo.get_invoice(inv_id)
    items = repo.items_of_invoice(inv_id)
    if not inv or not items:
        return
    amount = rules.r2(sum(rules.to_num(i.get('paid')) or 0 for i in items))
    rec = {
        'abs_path': config.DATA_DIR / inv['rel_path'],
        'orig_name': inv['orig_name'],
        'stored_name': inv['stored_name'],
        'rel_path': inv['rel_path'],
        'size': inv['size'],
    }
    new = invoicing.rename_to_amount(rec, items[0], amount=amount)
    if new['rel_path'] != inv['rel_path']:
        repo.update_invoice_file(inv_id, new['stored_name'], new['rel_path'])


def _invoices_with_links(rid):
    """本轮的发票，每张带上**完整归属**。

    ⚠️ 不能只靠 `invoices.item_id` —— 那是「主条目」，一票多条时只指向第一条，
    于是另外几条的发票列会是空的（"我明明挂了票，界面上却什么都没有"）。
    """
    out = []
    links = repo.links_for_invoices(rid)      # 一次查完，不逐票查
    for inv in repo.list_invoices(round_id=rid):
        inv['item_ids'] = links.get(inv['id'], [])
        out.append(inv)
    return out


def _round_detail(rid):
    r = _round_or_404(rid)
    items = repo.list_items(rid)
    low, high, _ = rules.split_items(items, r.get('split_threshold'))
    return {
        'round': r,
        'items': items,
        'split': _split_preview(r, items),
        'invoices': _invoices_with_links(rid),
        'invoice_counts': repo.invoice_counts(rid),
        'pending_order': [i['id'] for i in repo.items_pending_order(rid)],
        'pending_invoice': [i['id'] for i in repo.items_pending_invoice(rid)],
        'split_ids': {'low': [i['id'] for i in low], 'high': [i['id'] for i in high]},
    }


# ------------------------------------------------------------------ 组员

@app.get('/api/meta')
def api_meta():
    """前端不硬编码枚举，统一从这里取。"""
    return {
        'order_statuses': config.ORDER_STATUSES,
        'invoice_statuses': config.INV_STATUSES,
        'order_pending': config.ORDER_PENDING,
        'order_arrived': config.ORDER_ARRIVED,
        'inv_received': config.INV_RECEIVED,
        'group': config.DEFAULT_GROUP,
        'threshold': config.DEFAULT_SPLIT_THRESHOLD,
    }


@app.post('/api/parse-share')
def api_parse_share(body: ShareIn):
    """解析粘贴进来的分享文案（淘宝 / 闲鱼 / 拼多多 …）。

    纯文本解析、不联网 —— 实测直接抓淘宝链接只能拿到唤起 App 的空壳页，
    既没有商品名也没有价格。
    """
    return shareparse.parse_share_text(body.text)


@app.get('/api/members')
def api_members(request: Request):
    """组员列表。

    **组员只拿到 id 和名字**：手机号是别人的隐私，界面上「我是谁」的下拉也用不到它。
    采购人员拿完整信息（要联系组员、要标负责人）。
    """
    rows = repo.list_members()
    if is_purchase(request):
        return rows
    # 组员只要 id/名字/是否负责人（下拉里要显示「（负责人）」）；
    # **手机号不给他们** —— 那是别人的隐私，界面也用不到。
    return [{'id': m['id'], 'name': m['name'], 'is_owner': m.get('is_owner')}
            for m in rows]


@app.post('/api/members', dependencies=[Depends(require_purchase)])
def api_add_member(m: MemberIn):
    try:
        mid = repo.add_member(m.name, m.phone, 1 if m.is_owner else 0)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, '这个姓名已经存在了')
    return {'id': mid, 'name': m.name.strip(), 'phone': m.phone.strip(),
            'is_owner': 1 if m.is_owner else 0}


@app.put('/api/members/{mid}', dependencies=[Depends(require_purchase)])
def api_update_member(mid: int, m: MemberIn):
    repo.update_member(mid, name=m.name.strip(), phone=m.phone.strip(),
                       is_owner=1 if m.is_owner else 0)
    return {'ok': True}


@app.delete('/api/members/{mid}', dependencies=[Depends(require_purchase)])
def api_delete_member(mid: int):
    repo.delete_member(mid)
    return {'ok': True}


# ------------------------------------------------------------------ 轮次

@app.get('/api/rounds')
def api_rounds():
    return repo.list_rounds()


@app.post('/api/rounds', dependencies=[Depends(require_purchase)])
def api_create_round(r: RoundIn):
    try:
        rid = repo.create_round(r.name, r.title, r.group_name, r.threshold)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return repo.get_round(rid)


@app.get('/api/rounds/{rid}')
def api_round_detail(rid: int):
    return _round_detail(rid)


@app.put('/api/rounds/{rid}', dependencies=[Depends(require_purchase)])
def api_update_round(rid: int, r: RoundIn):
    _round_or_404(rid)
    repo.update_round(rid, name=r.name, title=r.title,
                      group_name=r.group_name, split_threshold=r.threshold)
    return repo.get_round(rid)


@app.post('/api/rounds/{rid}/status', dependencies=[Depends(require_purchase)])
def api_round_status(rid: int, s: StatusIn):
    _round_or_404(rid)
    if s.status not in (config.STATUS_OPEN, config.STATUS_LOCKED, config.STATUS_DONE):
        raise HTTPException(400, '未知状态：%s' % s.status)
    repo.set_round_status(rid, s.status)
    return repo.get_round(rid)


# ------------------------------------------------------------------ 条目

@app.get('/api/rounds/{rid}/items')
def api_items(rid: int):
    """本轮条目 —— **组员也看得到全组的**：省得买重、也方便互相参考。

    能看 ≠ 能改。改、删、传发票都只放行自己那一条，逐个接口把关
    （见 `_guard_own_item`）。
    """
    _round_or_404(rid)
    return repo.list_items(rid)


@app.post('/api/rounds/{rid}/items')
def api_add_item(rid: int, item: ItemIn, request: Request):
    r = _round_or_404(rid)
    if r['status'] != config.STATUS_OPEN:
        raise HTTPException(400, '本轮已锁定，不能再添加条目')
    # 组员只能以**自己**的名义提交，不能替别人填
    if not is_purchase(request):
        mid = current_member_id(request)
        if not mid or int(item.member_id or 0) != mid:
            raise HTTPException(403, '只能以你自己的名义提交')
    data = item.model_dump()
    errors, warnings = rules.validate(data)
    if errors:
        raise HTTPException(400, '；'.join(errors))
    iid = repo.add_item(rid, item.member_id, data)
    return {'item': repo.get_item(iid), 'warnings': warnings}


@app.put('/api/items/{iid}')
def api_update_item(iid: int, item: ItemIn, request: Request):
    old = _item_or_404(iid)
    _guard_own_item(request, old)
    r = repo.get_round(old['round_id'])
    if r['status'] != config.STATUS_OPEN:
        raise HTTPException(400, '本轮已锁定，不能再修改条目')
    # exclude_unset：只改调用方真的传了的字段。
    # 否则表单没带的 invoice / arrived / has_contract 会被当成空串写回去，
    # 等于「改一次条目就把下单与发票状态抹掉」。
    data = item.model_dump(exclude_unset=True)
    base_rev = data.pop('base_rev', None)
    if not data:
        raise HTTPException(400, '没有要修改的内容')
    # 并发：这条在你填的时候被别人改过 → 拦下来，别悄悄覆盖。
    # 只认「内容」的版本号，采购改下单/发票状态不算（不然边下单边有人补资料就老是弹错）。
    if base_rev is not None and int(old.get('rev') or 0) != int(base_rev):
        raise HTTPException(409, '这条在你填的时候已经被改过了（现在的内容更新），'
                                 '请刷新后对照着重新填一次，别把别人填的覆盖掉')
    # 用「库里的旧值 + 本次补丁」合并后再校验，
    # 这样只改一个字段时不会被别的必填项误伤
    merged = dict(old)
    merged.update(data)
    errors, warnings = rules.validate(merged)
    if errors:
        raise HTTPException(400, '；'.join(errors))
    # 真正的保护在 repo 里（事务内读旧值 + 条件更新）；上面的前置检查只是为了少走一趟
    try:
        repo.update_item(iid, data, base_rev=base_rev)
    except repo.RevConflict:
        raise HTTPException(409, '这条在你填的时候已经被改过了（现在的内容更新），'
                                 '请刷新后对照着重新填一次，别把别人填的覆盖掉')
    return {'item': repo.get_item(iid), 'warnings': warnings}


@app.delete('/api/items/{iid}')
def api_delete_item(iid: int, request: Request):
    old = _item_or_404(iid)
    _guard_own_item(request, old)
    r = repo.get_round(old['round_id'])
    if r['status'] != config.STATUS_OPEN:
        raise HTTPException(400, '本轮已锁定，不能删除条目')
    repo.delete_item(iid)
    return {'ok': True}


@app.post('/api/rounds/{rid}/reorder', dependencies=[Depends(require_purchase)])
def api_reorder(rid: int, body: ReorderIn):
    _round_or_404(rid)
    repo.reorder_items(rid, [int(x) for x in body.ids])
    return {'ok': True}


# ------------------------------------------------------------------ 状态与清单

@app.post('/api/items/{iid}/refund', dependencies=[Depends(require_purchase)])
def api_refund(iid: int, body: RefundIn):
    """整单退货 / 取消退货。

    注意：退货会**把这条上的发票删掉**（含电子发票），所以界面上要先问一句。
    条目本身不删，只是标记 —— 留痕。
    """
    try:
        return repo.set_refund(iid, refunded=body.refunded,
                               refund_amount=body.refund_amount)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post('/api/items/{iid}/status', dependencies=[Depends(require_purchase)])
def api_item_status(iid: int, body: ItemStatusIn):
    """改下单/发票状态。

    注意：**锁定轮次之后依然允许** —— 锁定只是不让组员再改条目内容，
    而下单、收发票这些事都发生在锁定（汇总）之后。
    """
    _item_or_404(iid)
    try:
        repo.set_item_status(iid, body.order_status, body.invoice_status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return repo.get_item(iid)


@app.get('/api/items/history')
def api_item_history(name: str = '', limit: int = 5):
    """「这个物料以前买过什么」—— 填单时一键沿用上次的规格/单价/链接。

    **组员也要能用**（他们才是填单的人），所以这条不挂 `require_purchase`，
    只靠全局中间件要求已登录（组员口令或采购登录都行）。

    只读、只查同名（不做模糊匹配）：模糊匹配容易把「电容」和「大电容」
    混成一堆，反而要人再挑一遍。
    """
    if not (name or '').strip():
        return []
    limit = max(1, min(20, int(limit or 5)))
    return repo.item_history(name, limit=limit)


@app.post('/api/items/{iid}/paid')
def api_set_paid(iid: int, body: PaidIn, request: Request):
    """补填「实付金额」。

    **为什么单开一条接口**，而不是让人去改条目：改条目（`PUT /api/items/{id}`）
    在「本轮已锁定」之后一律拒绝，而付款、收发票本来就发生在汇总锁定**之后**——
    9 月那批发票就是卡在这儿：票挂上了，实付金额一直空着，对账的差额跟着不准。
    所以这条接口不看轮次状态，只看「是不是你的条目」。

    谁能填：采购人员随便填；组员只能填自己那条（和传发票一样的界线）。
    """
    item = _item_or_404(iid)
    _guard_own_item(request, item)
    # 并发：这条在你填的时候被别人改过 → 拦下来，别悄悄把人家的数字盖掉。
    # 只认「内容」的版本号（和改条目那条接口一个口径）。
    if body.base_rev is not None and int(item.get('rev') or 0) != int(body.base_rev):
        raise HTTPException(409, '这条在你填的时候已经被改过了（现在的内容更新），'
                                 '请刷新后对照着重新填一次，别把别人填的覆盖掉')
    value = rules.to_num(body.paid)
    if value is not None:
        if value < 0:
            raise HTTPException(400, '实付金额不能是负数')
        value = round(value, 2)
    # 真正的保护在 repo 里（事务内条件更新）；上面的前置检查只是为了少走一趟
    try:
        repo.update_item(iid, {'paid': value}, base_rev=body.base_rev)
    except repo.RevConflict:
        raise HTTPException(409, '这条在你填的时候已经被改过了（现在的内容更新），'
                                 '请刷新后对照着重新填一次，别把别人填的覆盖掉')
    # 退货时实付还是空的（退款额因此落成 0），现在才补上 —— 退款额要跟着补记成全额退
    repo.bump_refund_if_paid_was_empty(iid, value)
    return repo.get_item(iid)


@app.get('/api/rounds/{rid}/pending-order', dependencies=[Depends(require_purchase)])
def api_pending_order(rid: int):
    """还没下单的清单。"""
    _round_or_404(rid)
    return repo.items_pending_order(rid)


@app.get('/api/rounds/{rid}/pending-invoice', dependencies=[Depends(require_purchase)])
def api_pending_invoice(rid: int):
    """还没拿到发票的清单 —— 「要确保每个采购都有发票」。"""
    _round_or_404(rid)
    return repo.items_pending_invoice(rid)


@app.get('/api/rounds/{rid}/shopping-list', dependencies=[Depends(require_purchase)])
def api_shopping_list(rid: int):
    """待购清单：采购照着它去淘宝下单。

    只给「待下单 + 没退货 + 没人垫付」的条目；每条附一个**清理过的** `link`，
    因为实测有七成条目的「网址链接」里存的是整段分享文本
    （`https://e.tb.cn/xxx?tk=yyy CZ028`），带空格，浏览器点不开。
    """
    _round_or_404(rid)
    return repo.shopping_list(rid)


# ------------------------------------------------------------------ 发票

def _media_type(name):
    import os
    return MEDIA_TYPES.get(os.path.splitext(name or '')[1].lower(),
                           'application/octet-stream')


@app.get('/api/rounds/{rid}/invoices')
def api_round_invoices(rid: int):
    _round_or_404(rid)
    return repo.list_invoices(round_id=rid)


@app.post('/api/items/{iid}/invoices')
async def api_upload_invoice(iid: int, request: Request, file: UploadFile = File(...)):
    """上传发票：归档成 `DQ-物品名-金额.pdf`，并把状态推进到「已收到」。

    **图片（jpg/png/bmp/tif…）会自动转成 PDF 再存档**（`invoicing.store_invoice`），
    所以不管传的是电子发票 PDF 还是手机拍的照片，落盘之后都是同一套东西 ——
    合并打印、预览、命名不用分两条路走。
    """
    item = _item_or_404(iid)
    _guard_own_item(request, item)
    r = repo.get_round(item['round_id'])
    data = await _read_upload(file, MAX_UPLOAD_BYTES)
    try:
        # 图片转 PDF 是纯 CPU 活（一张手机照片 300ms-1s）—— 在这个 async 端点里
        # 直接同步跑会把**整个事件循环**堵住，另一个人这段时间的所有请求都得排队。
        rec = await run_in_threadpool(
            invoicing.store_invoice, item, r['name'], file.filename, data)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 重复上传：同一个归档文件已经登记过就复用它，不再多建一条记录
    if rec['state'] == 'duplicate':
        existing = repo.find_invoice_by_path(iid, rec['rel_path'])
        if existing:
            repo.set_item_status(iid, invoice_status=config.INV_RECEIVED)
            item = repo.get_item(iid)
            got, source = None, ''
            # 老票重传一遍：正好补上一直空着的实付金额
            # （当初是照片票读不出来，现在有 OCR 了，试一次）
            if rules.to_num(item.get('paid')) is None:
                got = await run_in_threadpool(
                    invoicepdf.read_amount_ocr,
                    config.DATA_DIR / rec['rel_path'],
                    expect=rules.to_num(item.get('total')))
                if got is not None:
                    repo.update_item(iid, {'paid': got})
                    item, source = repo.get_item(iid), 'ocr'
            return {'invoice': existing, 'state': 'duplicate', 'item': item,
                    'paid_filled': got,
                    'paid_source': source,
                    'converted': rec.get('converted', False),
                    # 重传一遍时也要提醒：这条还没填实付金额
                    'need_paid': rules.to_num(item.get('paid')) is None}

    # 从发票 PDF 的**文字层**读明细（名称 + 金额 + 税额）。
    # **优先认「价税合计（小写）」** —— 那是票面自己算好的总额，最可靠；
    # 「金额 + 税额」两列相加是退路：只要税额那一列读丢/读偏一位，
    # 总额就少了（用户报过：¥29.00 被算成 ¥28.71，就是税额 0.29 没读出来）。
    # 一票多单：总额对不上任何一条，照样得让人核对怎么分。
    path = config.DATA_DIR / rec['rel_path']
    lines = invoicepdf.read_lines(path)
    got = invoicepdf.read_amount(path)
    if got is None and len(lines) == 1:
        got = lines[0]['total']
    source = 'pdf' if got is not None else ''
    if got is None:
        # 照片 / 扫描件：**没有文字层**，读不出是必然的，只能再上 OCR 试一次。
        # 认出来也只当**建议值**（前端会提示「请核对票面」），认不出照旧让人手填。
        # 拿条目自己的合计去消歧：OCR 常把「¥」认成数字粘上去（¥174 → 4174）。
        # OCR 是**同步的 CPU 活**（一张照片 2~3 秒），直接在事件循环里跑会把整站卡住 ——
        # 丢到线程池，别人还能正常刷页面。
        ocr = await run_in_threadpool(
            invoicepdf.read_amount_ocr,
            path, expect=rules.to_num(item.get('total')) or rules.to_num(item.get('paid')))
        if ocr is not None:
            got, source = ocr, 'ocr'

    # **先把钱认准，再按钱命名**（识别 → 填实付 → 改名 → 登记）：
    # 归档那一步发生时条目上还没有金额，文件名会缺金额段；识别出来之后补上，
    # 文件名才跟票面一致（`DQ-硅胶管-29.pdf` 而不是 `DQ-硅胶管.pdf`）。
    if got is not None:
        # **只在实付金额还空着时才预填** —— 人已经填过的数字不能被机器覆盖。
        # OCR 常把「¥174」认成 4174（¥ 被粘成数字），覆盖之后很难发现，
        # 还会顺带 rev+1 让别人正在填的保存被 409 拦下。
        if rules.to_num(item.get('paid')) is None:
            repo.update_item(iid, {'paid': got})
        rec = invoicing.rename_to_amount(rec, repo.get_item(iid), amount=got)

    inv_id = repo.add_invoice(
        iid, item['round_id'], rec['orig_name'], rec['stored_name'],
        rec['rel_path'], rec['size'],
        amount=got if got is not None else invoicing.invoice_amount(item))
    fresh = repo.get_item(iid)

    # 一票多单：算好预选（名称优先、金额辅助），让前端弹框给人核对。
    # 候选只算**还没票的条目**再加当前这条 —— 否则金额一接近就会去抢
    # 那些已经有票的（真发生过：29 块的票配到 28.74 的折叠椅上）。
    need_review = len(lines) > 1
    suggest = []
    if need_review:
        cand = [i for i in repo.list_items(item['round_id'])
                if i['id'] == iid or not repo.invoices_of_item(i['id'])]
        suggest = invoicematch.match_lines(lines, cand)

    return {
        'invoice': repo.get_invoice(inv_id),
        'state': rec['state'],          # new / duplicate
        'item': fresh,
        'paid_filled': got,             # 前端可以提示「已自动填了实付金额」
        # 这个金额是哪来的：'pdf' = 电子发票文字层读的（准）；
        # 'ocr' = 照片/扫描件认的（**要人核对**）；'' = 没读到
        'paid_source': source,
        # 这次上传的是图片，已于归档时转成 PDF（`converted` 让前端说一声）
        'converted': rec.get('converted', False),
        # 读不出金额（照片、扫描件、没有文字层的 PDF）**而且这条还没有实付金额**：
        # 别静悄悄放过 —— 早先就是这里不出声，9 月那批票一直空着实付金额没人管。
        # 金额已经有了就不再问（人自己填过，或者上一张票已经填过）。
        'need_paid': got is None and rules.to_num(fresh.get('paid')) is None,
        'lines': lines,                 # 明细各行的名称与金额
        'suggest': suggest,             # 预选：每行该配哪条
        'need_review': need_review,     # 一票多单 → 前端弹核对框
    }


@app.get('/api/invoices/{inv_id}/file')
def api_invoice_file(inv_id: int, download: int = 0):
    """看发票文件。**这条不限制归属** —— 组员要能看到全组的条目，
    发票是条目的一部分。删除和上传仍只有本人（或采购人员）能做。
    """
    inv = repo.get_invoice(inv_id)
    if not inv:
        raise HTTPException(404, '发票不存在')
    path = config.DATA_DIR / inv['rel_path']
    if not path.exists():
        raise HTTPException(404, '发票文件找不到了：%s' % inv['rel_path'])
    return FileResponse(str(path), filename=inv['stored_name'],
                        media_type=_media_type(inv['stored_name']),
                        content_disposition_type='attachment' if download else 'inline')


@app.delete('/api/invoices/{inv_id}')
def api_delete_invoice(inv_id: int, request: Request):
    """删掉一张发票（记录 + 归档文件）。

    **权限跟「改挂」一个规矩**：组员只能删「整张票都挂在自己条目上」的票。
    以前这里只看 `invoices.item_id`（主条目）—— 一张票盖着 A、B 两条时，
    只要 A 是他的就能把整张删掉，**连带把 B 那条的票也带走**，
    而 B 只会看到"我的票没了"。改挂那边本来就查的是全部归属，两边得一致。
    """
    inv = repo.get_invoice(inv_id)
    if not inv:
        raise HTTPException(404, '发票不存在')
    if not is_purchase(request):
        mid = current_member_id(request)
        linked = repo.items_of_invoice(inv_id)
        if mid is None or any(x.get('member_id') != mid for x in linked):
            raise HTTPException(403, '这张发票还挂在别人的采购上，不能整个删掉')
    if not repo.delete_invoice(inv_id):
        raise HTTPException(404, '发票不存在')
    return {'ok': True}


# ------------------------------------------------------------------ 导出

@app.get('/api/rounds/{rid}/export', dependencies=[Depends(require_purchase)])
def api_export(rid: int):
    r = _round_or_404(rid)
    items = repo.list_items(rid)
    if not items:
        raise HTTPException(400, '本轮还没有任何条目')
    config.ensure_dirs()
    safe = ''.join(ch for ch in r['name'] if ch not in '\\/:*?"<>|').strip() or '采购表'
    path = config.EXPORT_DIR / ('采购表_%s.xlsx' % safe)
    exporter.save_round_xlsx(r, items, path)
    return FileResponse(str(path), filename=path.name,
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.get('/api/rounds/{rid}/merged.pdf', dependencies=[Depends(require_purchase)])
def api_merged_pdf(rid: int, per_page: int = 2, back: int = 1, frame: int = 1):
    """把本轮收到的发票合并成一份可双面打印的 PDF（正面发票、背面采购信息）。"""
    r = _round_or_404(rid)
    invoices = repo.list_invoices(round_id=rid)
    if not invoices:
        raise HTTPException(400, '本轮还没有发票，先在条目上「传发票」')

    items = repo.list_items(rid)
    by_id = {it['id']: it for it in items}
    order = {it['id']: it['seq'] for it in items}
    # 按条目顺序排，同一条目的多张发票按登记顺序
    invoices.sort(key=lambda v: (order.get(v['item_id'], 0), v['id']))

    links = repo.links_for_invoices(rid)      # 一次查完，别逐票查
    entries, missing = [], []
    for inv in invoices:
        path = config.DATA_DIR / inv['rel_path']
        if path.exists():
            # 一张票可能覆盖好几条采购（淘宝一个订单买几样）。
            # 打印仍然**按发票去重**（遍历的就是 invoices 表，一张票一次），
            # 但背面要把涉及的每一样都列出来，不然对不上账。
            linked = [by_id[i] for i in links.get(inv['id'], []) if i in by_id]
            main = by_id.get(inv['item_id']) or (linked[0] if linked else {})
            others = [x['name'] for x in linked if x['id'] != main.get('id')]
            entries.append({'item': main, 'more_names': others,
                            'invoice': inv, 'path': path})
        else:
            missing.append(inv['stored_name'])
    if missing:
        raise HTTPException(400, '这些发票的归档文件找不到了，请重新上传：%s' % '、'.join(missing))

    opts = invoicepdf.MergeOptions(
        per_page=max(1, min(4, per_page)),
        make_back=bool(back),
        show_back_frame=bool(frame),
        show_back_line=bool(frame),
        group=r.get('group_name') or config.DEFAULT_GROUP,
    )

    config.ensure_dirs()
    safe = ''.join(ch for ch in r['name'] if ch not in '\\/:*?"<>|').strip() or '采购轮次'
    out = config.EXPORT_DIR / ('发票合并_%s.pdf' % safe)
    path, _stats = invoicepdf.build_merged_pdf(entries, out, opts)
    if missing:
        # 有文件缺失时仍然出成品，但把缺的写进响应头，便于前端提示
        path = path
    return FileResponse(str(path), filename=path.name, media_type='application/pdf')


def _parse_round_ids(round_ids, round_id=None):
    """把前端的「多选轮次」解析成去重后的 id 列表。

    前端沿用 `<select multiple>` 的习惯传法：`round_ids` 是逗号分隔的字符串。
    为兼容老调用，仍接受单个 `round_id`。
    """
    ids = []
    for chunk in str(round_ids or '').split(','):
        chunk = chunk.strip()
        if chunk.lstrip('-').isdigit():
            v = int(chunk)
            if v not in ids:
                ids.append(v)
    if not ids and round_id:
        ids.append(int(round_id))
    return ids


def _merge_group(rids, fallback=None):
    """背面「组别」的兜底值：取所选轮次里第一个有组名的。

    条目自己的 `group_name` 优先（`back_fields` 里就是这么用的），
    这里只是条目没填时的兜底，所以不必纠结用哪一轮的。
    """
    for rid in (rids or []):
        r = repo.get_round(rid)
        if r and (r.get('group_name') or '').strip():
            return r['group_name'].strip()
    return fallback or config.DEFAULT_GROUP


@app.post('/api/merge/custom.pdf', dependencies=[Depends(require_purchase)])
async def api_merge_custom(
    request: Request,
    files: List[UploadFile] = File(...),
    per_page: int = Form(2),
    back: int = Form(1),
    frame: int = Form(1),
    round_ids: Optional[str] = Form(None),
    round_id: Optional[int] = Form(None),
):
    """从**本机上传**一批发票直接合并成 PDF —— 不走 `invoices` 表，用完即弃。

    给谁用：商家把票开错了、退回去重开，回来的是**几张新的 PDF / 照片**，
    这时候它们还没进任何一轮的账，但人当场就要合起来打印。

    几个刻意的取舍：
    * **不落库、不归档**（你说过这类票是返工重开的）—— 转换/合并都在
      临时目录里做，响应发完立刻删掉。所以重复上传也不会在系统里留痕。
    * **背面信息靠自动匹配**：拿票面金额（读得到就用）去**所选轮次**的采购条目里配，
      配上了就印那条的信息，配不上就留空手写（付款日期本来就手写）。
      轮次可以**多选**（`round_ids=1,2,3`）—— 返工的票常常是上个月那批，
      所以别把自己锁死在当前这一轮；不选就是全不配、背面留空。
      匹配**只读**，不会改任何条目的金额或状态。
    * 图片（jpg/png/bmp/tif…）先转 PDF 再合，跟系统内那套一致。
    """
    if not files:
        raise HTTPException(400, '没有收到文件')
    if len(files) > MAX_MERGE_FILES:
        raise HTTPException(400, '一次最多合并 %d 个文件（现在 %d 个）'
                            % (MAX_MERGE_FILES, len(files)))

    tmp = Path(tempfile.mkdtemp(prefix='cgsys-merge-'))
    try:
        entries = []
        for i, f in enumerate(files):
            data = await f.read()
            if not data:
                continue
            orig = f.filename or ('file%d' % (i + 1))
            ext = Path(orig).suffix.lower()
            if ext not in config.INVOICE_EXTS:
                raise HTTPException(400, '不支持的文件类型：%s（支持 %s）'
                                    % (ext or '(无后缀)', '、'.join(sorted(config.INVOICE_EXTS))))
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(400, '文件太大（上限 %d MB）：%s'
                                    % (MAX_UPLOAD_BYTES // 1024 // 1024, orig))
            if ext in invoicing.IMAGE_EXTS:
                try:
                    data = invoicing.image_to_pdf(data)
                except ValueError as e:
                    raise HTTPException(400, '%s：%s' % (orig, e))
            dst = tmp / ('%03d.pdf' % i)
            dst.write_bytes(data)
            # 票面名 / 金额读得到就读，用于**自动匹配**和背面小字；读不到也不拦着。
            amount = invoicepdf.read_amount(dst)
            lines = invoicepdf.read_lines(dst)
            name = lines[0]['name'] if lines else ''
            entries.append({'key': '%d' % i, 'orig': orig, 'path': dst,
                            'amount': amount, 'name': name})

        if not entries:
            raise HTTPException(400, '上传的文件都是空的')

        # 拿**所选轮次**的条目自动匹配（只读）。一轮没选、或选了但没条目，也能合，只是背面留空。
        rids = _parse_round_ids(round_ids, round_id)
        items = []
        for rid in rids:
            items.extend(repo.list_items(rid))
        by_id = {it['id']: it for it in items}
        taken = invoicematch.match_invoices_to_items(
            [{'key': e['key'], 'name': e['name'], 'amount': e['amount']} for e in entries],
            items)

        merged = []
        for e in entries:
            it = by_id.get(taken.get(e['key']))
            merged.append({
                'item': it or {},
                'more_names': [],
                'invoice': {'amount': e['amount'], 'orig_name': e['orig']},
                'path': e['path'],
            })

        opts = invoicepdf.MergeOptions(
            per_page=max(1, min(4, per_page)),
            make_back=bool(back),
            show_back_frame=bool(frame),
            show_back_line=bool(frame),
            group=_merge_group(rids),
        )
        out = tmp / 'merged.pdf'
        invoicepdf.build_merged_pdf(merged, out, opts)

        # 文件名带上日期，方便跟系统内合出来的区分
        stamp = time.strftime('%Y%m%d-%H%M%S')
        return FileResponse(str(out), filename='发票合并_外部上传_%s.pdf' % stamp,
                            media_type='application/pdf',
                            background=BackgroundTask(shutil.rmtree, str(tmp), True))
    except HTTPException:
        shutil.rmtree(str(tmp), ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(str(tmp), ignore_errors=True)
        raise


# ------------------------------------------------------------------ 发票打包下载

def _unique_zip_name(name, used):
    """zip 里不许重名（重名会被后一个顶掉）。正常不会撞，撞了加 `-2`。"""
    name = (name or '').strip() or 'invoice.pdf'
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition('.')
    if not dot:
        stem, ext = name, ''
    n = 2
    while True:
        cand = '%s-%d%s' % (stem, n, ('.' + ext) if ext else '')
        if cand not in used:
            used.add(cand)
            return cand
        n += 1


@app.get('/api/rounds/{rid}/invoices.zip', dependencies=[Depends(require_purchase)])
def api_invoices_zip(rid: int):
    """把**本轮所有发票**打包成一个 zip 下载。

    为什么要它：发票原件除了「合并打印」，有时还得单独交出去（给财务、给商家、
    存档、发群里）—— 一张张点「存」太费手。zip 里就是归档时的文件名
    （`DQ-品项-金额.pdf`），**按发票记录打包**，所以一票多条时那张票只出现一次。
    """
    r = _round_or_404(rid)
    invoices = repo.list_invoices(round_id=rid)
    if not invoices:
        raise HTTPException(400, '本轮还没有发票，先给条目「传发票」')

    # 归档文件不在就别装糊涂 —— 跟合并打印一个规矩，先把缺的报出来
    missing = [inv['stored_name'] for inv in invoices
               if not (config.DATA_DIR / inv['rel_path']).exists()]
    if missing:
        raise HTTPException(400, '这些发票的归档文件找不到了，请重新上传：%s'
                                 % '、'.join(missing))

    config.ensure_dirs()
    safe = ''.join(ch for ch in r['name'] if ch not in '\\/:*?"<>|').strip() or '采购轮次'
    out = config.EXPORT_DIR / ('发票_%s.zip' % safe)
    used = set()
    with zipfile.ZipFile(str(out), 'w', zipfile.ZIP_DEFLATED) as zf:
        for inv in invoices:
            zf.write(str(config.DATA_DIR / inv['rel_path']),
                     arcname=_unique_zip_name(inv['stored_name'], used))
    return FileResponse(str(out), filename=out.name, media_type='application/zip')


@app.get('/api/rounds/{rid}/block', dependencies=[Depends(require_purchase)])
def api_block(rid: int):
    """回填车队总表用的行块 + 对账数字。

    列序与总表「7月下旬采购」起那套新表头逐列一致；**不含表头**，第一行就是数据，
    方便整段复制后直接粘到在线表格里电气组那一块的末尾。
    """
    r = _round_or_404(rid)
    items = repo.list_items(rid)
    group = r.get('group_name') or config.DEFAULT_GROUP
    return {
        'group': group,
        'headers': list(blockexport.HEADERS),
        'rows': blockexport.block_rows(items, group),
        'tsv': blockexport.block_tsv(items, group),
        'reconcile': blockexport.reconcile(items, r),
        'note': '不含表头。贴到车队总表对应月份 sheet 里「电气组」那一块的末尾；'
                '请注意目标月份要用新表头（7月下旬起），更早的月份列不一样。',
    }


@app.get('/api/rounds/{rid}/block.xlsx', dependencies=[Depends(require_purchase)])
def api_block_xlsx(rid: int):
    r = _round_or_404(rid)
    items = repo.list_items(rid)
    if not items:
        raise HTTPException(400, '本轮还没有条目')
    config.ensure_dirs()
    safe = ''.join(ch for ch in r['name'] if ch not in '\\/:*?"<>|').strip() or '采购轮次'
    path = config.EXPORT_DIR / ('行块_%s.xlsx' % safe)
    blockexport.save_block_xlsx(items, path,
                               group=r.get('group_name') or config.DEFAULT_GROUP,
                               recon=blockexport.reconcile(items, r))
    return FileResponse(str(path), filename=path.name,
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ------------------------------------------------------------------ 邮箱收发票

def _mail_public(cfg) -> dict:
    """给前端的配置：**不带授权码**，只告诉它「有没有设过」。"""
    d = cfg.to_dict()
    d.pop('password', None)
    d['has_password'] = bool(cfg.password)
    return d


@app.get('/api/mail/presets', dependencies=[Depends(require_purchase)])
def api_mail_presets():
    return [{'name': n, 'host': h, 'port': p} for n, h, p in mailbox.PRESETS]


@app.get('/api/mail/config', dependencies=[Depends(require_purchase)])
def api_mail_config():
    return _mail_public(mailbox.load_config())


@app.put('/api/mail/config', dependencies=[Depends(require_purchase)])
def api_mail_save(body: MailConfigIn):
    cfg = mailbox.load_config()
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            continue                      # 没传或显式 null 都不动
        setattr(cfg, key, value)
    if isinstance(cfg.keywords, list):
        cfg.keywords = [str(x).strip() for x in cfg.keywords if str(x).strip()]
    mailbox.save_config(cfg)
    return _mail_public(cfg)


@app.post('/api/mail/test', dependencies=[Depends(require_purchase)])
def api_mail_test():
    return mailbox.test_connection(mailbox.load_config())


@app.post('/api/mail/fetch', dependencies=[Depends(require_purchase)])
def api_mail_fetch():
    result = mailfetch.collect(mailbox.load_config())
    result['lines'] = mailfetch.report_lines(result)
    return result


@app.get('/api/mail/pending', dependencies=[Depends(require_purchase)])
def api_mail_pending(round_id: Optional[int] = None):
    return mailfetch.list_pending(round_id)


@app.post('/api/mail/attach', dependencies=[Depends(require_purchase)])
def api_mail_attach(body: MailAttachIn):
    try:
        return mailfetch.attach_to_item(body.mail_id, body.item_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get('/api/mail/pending/{mid}/file', dependencies=[Depends(require_purchase)])
def api_mail_file(mid: int, download: int = 0):
    """看**还没归档**的那张发票 —— 挂之前得先看清票面，才知道该挂到哪一笔上。

    为什么单开一条：归档后的票走 `/api/invoices/{id}/file`，
    而待归档的票还没进 `invoices` 表，那条路取不到它。
    """
    row = repo.get_mail_attachment(mid)
    if not row:
        raise HTTPException(404, '这条待归档记录不存在')
    path = (config.DATA_DIR / row['rel_path']).resolve()
    # 防路径穿越：rel_path 是库里记的，正常都在 data/ 下；
    # 万一被改过，别让它读到 data 之外去。
    try:
        path.relative_to(Path(config.DATA_DIR).resolve())
    except ValueError:
        raise HTTPException(400, '这条记录的路径不对')
    if not path.exists():
        raise HTTPException(404, '发票文件找不到了：%s' % row['rel_path'])
    return FileResponse(str(path), filename=row['stored_name'],
                        media_type=_media_type(row['stored_name']),
                        content_disposition_type='attachment' if download else 'inline')


@app.delete('/api/mail/pending/{mid}', dependencies=[Depends(require_purchase)])
def api_mail_ignore(mid: int):
    if not mailfetch.ignore(mid):
        raise HTTPException(404, '这条待归档记录不存在')
    return {'ok': True}


@app.put('/api/invoices/{iid}/items')
def api_set_invoice_items(iid: int, body: InvoiceItemsIn, request: Request):
    """改挂：把这张发票换到别的采购条目上（可以挂多条）。

    以前挂错只能删发票，而删掉之后回不到「待归档」——
    等于要去邮箱重新收一遍。现在点两下就换过来了。

    **谁可以改**：
    - 采购人员：随便改。
    - 组员：只能把**自己的**票挂到**自己的**条目上 —— 一张票覆盖他自己
      买的几样很方便，又不会碰到别人的账。
    """
    inv = repo.get_invoice(iid)
    if not inv:
        raise HTTPException(404, '发票不存在')
    targets = [repo.get_item(x) for x in body.item_ids]
    if any(t is None for t in targets):
        raise HTTPException(400, '选的条目不存在')
    # 一张票只能挂在**同轮次**的条目上。跨轮挂的话，合并打印、打包 zip、
    # 「缺发票」清单都是按 `invoices.round_id` 查的 —— 那张票会完全查不到，
    # 等于凭空消失（而且不报错，最难发现）。
    if any(t['round_id'] != inv['round_id'] for t in targets):
        rnd = repo.get_round(inv['round_id']) or {}
        raise HTTPException(409, '这张票属于「%s」，只能挂到同一轮次的条目上'
                                 % (rnd.get('name') or '另一个轮次'))

    if not is_purchase(request):
        mid = current_member_id(request)
        if mid is None:
            raise HTTPException(401, '需要口令')
        if any(x.get('member_id') != mid for x in repo.items_of_invoice(iid)):
            raise HTTPException(403, '这不是你的发票')
        if any(t.get('member_id') != mid for t in targets):
            raise HTTPException(403, '只能挂到自己填的条目上')

    repo.set_invoice_items(iid, body.item_ids)
    # 归属变了 → 名字跟着改（一票多条要用「覆盖条目的合计」）
    _rename_invoice_by_links(iid)
    return {'ok': True, 'item_ids': [i['id'] for i in repo.items_of_invoice(iid)]}


@app.get('/api/invoices/{iid}/items')
def api_invoice_items(iid: int):
    """这张发票挂了哪几条采购。"""
    return [{'id': i['id'], 'name': i['name']} for i in repo.items_of_invoice(iid)]


@app.post('/api/invoices/{iid}/assign')
def api_assign_invoice(iid: int, body: InvoiceAssignIn, request: Request):
    """一票多单：把人核对过的「哪条明细挂哪条采购」落下来。

    前端弹核对框，预选是 `invoicematch` 算出来的；人改完点确定调这里。
    顺带把每条的「实付金额」按核对的数填上 —— 明细金额本来就是按条目分的，
    不填的话那条还是空的。
    """
    inv = repo.get_invoice(iid)
    if not inv:
        raise HTTPException(404, '发票不存在')

    rows = [r for r in body.lines if r.get('item_id')]
    targets = []
    for row in rows:
        try:
            target_id = int(row['item_id'])
        except (TypeError, ValueError):
            raise HTTPException(400, '条目 id 不是数字：%s' % row['item_id'])
        it = repo.get_item(target_id)
        if it is None:
            raise HTTPException(400, '选的条目不存在：%s' % row['item_id'])
        targets.append(it)
    # 和「改挂」同一条规矩：一张票只能挂在同一轮次的条目上
    if any(t['round_id'] != inv['round_id'] for t in targets):
        rnd = repo.get_round(inv['round_id']) or {}
        raise HTTPException(409, '这张票属于「%s」，只能挂到同一轮次的条目上'
                                 % (rnd.get('name') or '另一个轮次'))

    if not is_purchase(request):
        mid = current_member_id(request)
        if mid is None:
            raise HTTPException(401, '需要口令')
        if any(x.get('member_id') != mid for x in repo.items_of_invoice(iid)):
            raise HTTPException(403, '这不是你的发票')
        if any(t.get('member_id') != mid for t in targets):
            raise HTTPException(403, '只能挂到自己填的条目上')

    # 先把金额都校验完再动库 —— 免得写了一半才发现某个是负数
    checked = []
    for row in rows:
        paid = rules.to_num(row.get('paid'))
        if paid is not None and paid < 0:
            raise HTTPException(400, '实付金额不能是负数')
        checked.append(paid)

    keep = [t['id'] for t in targets]
    # 没被提到的就摘掉（这一步带"改挂"的语义）
    for x in repo.items_of_invoice(iid):
        if x['id'] not in keep:
            repo.unlink_invoice(iid, x['id'])
    for t, paid in zip(targets, checked):
        repo.link_invoice(iid, t['id'])
        if paid is not None:
            repo.update_item(t['id'], {'paid': round(paid, 2)})

    # 核对落库后归属可能变了 → 名字跟着改
    _rename_invoice_by_links(iid)
    return {'ok': True, 'item_ids': keep}


# ------------------------------------------------------------------ 采购入口：登录

class LoginIn(BaseModel):
    password: str = ''


class PasswordIn(BaseModel):
    old: str = ''
    new: str = ''
    setup: str = ''      # 首次设密码时的初始化码（见 settings.setup_code）


class MeIn(BaseModel):
    member_id: Optional[int] = None


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else '') or ''


def _set_purchase_cookie(response: Response, token: str) -> None:
    response.set_cookie(settings.COOKIE_NAME, token, httponly=True,
                        samesite='lax', max_age=settings.TOKEN_TTL, path='/')


@app.get('/api/purchase/status')
def api_purchase_status(request: Request):
    """前端据此决定：显示登录框、还是显示采购工作台。"""
    return {'has_password': settings.has_password(), 'logged_in': is_purchase(request)}


@app.post('/api/purchase/login')
def api_purchase_login(body: LoginIn, request: Request, response: Response):
    ip = _client_ip(request)
    left = settings.locked_for(ip)
    if left:
        raise HTTPException(429, '试错太多次，请 %d 秒后再试' % left)
    if not settings.has_password():
        raise HTTPException(400, '还没设置采购入口密码，请先设置一个')
    if not settings.check_password(body.password):
        settings.note_failure(ip)
        raise HTTPException(401, '密码不对')
    settings.clear_failures(ip)
    _set_purchase_cookie(response, settings.issue_token())
    return {'ok': True}


@app.post('/api/purchase/logout')
def api_purchase_logout(request: Request, response: Response):
    settings.revoke_token(request.cookies.get(settings.COOKIE_NAME))
    response.delete_cookie(settings.COOKIE_NAME, path='/')
    return {'ok': True}


@app.post('/api/purchase/password')
def api_purchase_password(body: PasswordIn, request: Request, response: Response):
    """还没设过密码时允许直接设（否则没人能进去设）；改密码则必须已登录 + 验旧密码。

    **首次设密码要带初始化码**：这个路由必须能匿名调用（不然死锁），
    但服务一旦暴露到公网，路人也能抢先设密码把采购权限拿走。
    初始化码只写在服务器的 `data/setup-code.txt` 和启动日志里。
    """
    if settings.has_password():
        require_purchase(request)
        if not settings.check_password(body.old):
            raise HTTPException(401, '原密码不对')
    else:
        settings.setup_code()          # 保证码文件存在（首次部署/从老版本升级上来时）
        if not settings.verify_setup_code(body.setup):
            raise HTTPException(403, '初始化码不对（或没填）。它写在服务器的 '
                                     'data/setup-code.txt 里，启动日志也会打印一行'
                                     '「采购入口初始化码」；用 SSH 登录服务器就能看到。')
    if len(body.new or '') < 4:
        raise HTTPException(400, '密码至少 4 位')
    settings.set_password(body.new)
    settings.clear_setup_code()                 # 设好了就把初始化码删掉
    settings.reset_state()                      # 旧的登录全部失效
    _set_purchase_cookie(response, settings.issue_token())
    return {'ok': True, 'relogin': True}


# ------------------------------------------------------------------ 组员入口的口令

class MemberLoginIn(BaseModel):
    password: str = ''


class MemberPasswordIn(BaseModel):
    password: str = ''


def is_member_unlocked(request: Request) -> bool:
    return settings.verify_token(request.cookies.get(settings.MEMBER_PASS),
                                 settings.KIND_MEMBER)


@app.get('/api/member/status')
def api_member_status(request: Request):
    """组员入口要不要显示口令门。"""
    return {'has_password': settings.has_member_password(),
            'unlocked': is_member_unlocked(request) or is_purchase(request)}


@app.post('/api/member/login')
def api_member_login(body: MemberLoginIn, request: Request, response: Response):
    ip = _client_ip(request)
    left = settings.locked_for(ip)
    if left:
        raise HTTPException(429, '试错太多次，请 %d 秒后再试' % left)
    if not settings.has_member_password():
        raise HTTPException(400, '还没设组员口令 —— 让负责采购的同学在采购入口里设一个')
    if not settings.check_member_password(body.password):
        settings.note_failure(ip)
        raise HTTPException(401, '口令不对')
    settings.clear_failures(ip)
    response.set_cookie(settings.MEMBER_PASS,
                        settings.issue_token(settings.KIND_MEMBER),
                        httponly=True, samesite='lax',
                        max_age=settings.MEMBER_TTL, path='/')
    return {'ok': True}


@app.post('/api/purchase/member-password', dependencies=[Depends(require_purchase)])
def api_set_member_password(body: MemberPasswordIn):
    """采购人员给组员入口设全组共用的口令（留空 = 把组员入口关掉）。"""
    if body.password and len(body.password) < 4:
        raise HTTPException(400, '口令至少 4 位')
    settings.set_member_password(body.password)
    # 口令改了/关了，老组员手里的 cookie 必须当场失效 ——
    # 不然「关掉入口」只是关了个门，钥匙还能开 30 天。
    revoked = settings.revoke_kind(settings.KIND_MEMBER)
    return {'ok': True, 'has_password': settings.has_member_password(),
            'revoked': revoked}


# ------------------------------------------------------------------ 组员身份

@app.get('/api/me')
def api_get_me(request: Request):
    mid = current_member_id(request)
    name = None
    if mid:
        m = repo.get_member(mid)
        name = m['name'] if m else None
    return {'member_id': mid, 'name': name, 'is_purchase': is_purchase(request)}


@app.post('/api/me')
def api_set_me(body: MeIn, response: Response):
    """组员选了自己的名字 —— 记进 cookie。

    后面判断「这条是不是你填的」要用它。注意这是**弱身份**：不设密码，
    只用来防手滑，不防有心人（真正拦得住的是采购接口的登录校验）。
    """
    if body.member_id is None:
        response.delete_cookie(settings.MEMBER_COOKIE, path='/')
        return {'member_id': None, 'name': None}
    m = repo.get_member(body.member_id)
    if not m:
        raise HTTPException(404, '没有这个组员')
    response.set_cookie(settings.MEMBER_COOKIE, settings.sign_member(m['id']),
                        httponly=True, samesite='lax', path='/')
    return {'member_id': m['id'], 'name': m['name']}


@app.get('/api/rounds/{rid}/progress')
def api_progress(rid: int):
    """组员也能看的进度数字。

    只回计数与总额，**不列出别人的条目内容** —— 组员界面要的是「现在到哪一步了」。

    **发票的分母要排除退货的**：退了的单发票跟着删了，本来就不该有票 ——
    留在分母里会显得"还差好几张"，其实只差没退的那几条。（真踩过：20 条里
    4 条已退货，界面上显示 15/20，让人以为还差 5 张，实际只差 1 张。）
    """
    _round_or_404(rid)
    items = repo.list_items(rid)
    # 发票 / 下单进度看的这一批：**不退货的**（票跟着退货删了，
    # 留在分母里只会显得"还差好几张"，实际只差没退的那几条）
    live = [i for i in items if not i.get('refunded')]
    total = len(items)
    ordered = sum(1 for i in live if (i.get('order_status') or config.ORDER_PENDING)
                  != config.ORDER_PENDING)
    invoiced = sum(1 for i in live
                   if i.get('invoice_status') == config.INV_RECEIVED)
    # 发票**张数**：一张票可能覆盖好几条采购（淘宝一个订单买几样），
    # 跟「几条采购已经有票」是两回事 —— 界面上分开说，
    # 否则 20 条采购的轮次会被看成"20 张发票"（用户真这么报过）。
    sheets = len(repo.list_invoices(round_id=rid))
    return {
        'total': total,
        'live_total': len(live),            # 不退货的条目数（下单 / 发票的分母）
        'invoiced_total': len(live),        # 老字段，留着兼容
        'ordered': ordered,
        'need_order': len(live) - ordered,
        'invoiced': invoiced,
        'need_invoice': len(live) - invoiced,
        'invoice_sheets': sheets,           # 发票文件张数
        # items 表**没有 amount 列**（只有 qty/price/freight/paid），
        # 以前这行永远算出 0。用「合计金额」汇总才是有意义的钱数。
        'amount': round(sum(float(i.get('total') or 0) for i in items), 2),
    }


# ------------------------------------------------------------------ 分享给组员

def _current_port() -> int:
    import os
    try:
        return int(os.environ.get('CGSYS_PORT', '8000'))
    except (TypeError, ValueError):
        return 8000


@app.get('/api/share')
def api_share():
    """组员扫码用的地址，以及二维码能不能生成。"""
    return shareqr.share_info(_current_port())


@app.get('/api/share/qrcode.png')
def api_share_qrcode():
    if not shareqr.available():
        raise HTTPException(503, '没装 segno，生成不了二维码（不影响用地址访问）')
    png = shareqr.qr_png(netinfo.share_url(_current_port()))
    return Response(content=png, media_type='image/png')


@app.get('/api/rounds/{rid}/preview', dependencies=[Depends(require_purchase)])
def api_preview(rid: int):
    r = _round_or_404(rid)
    return _split_preview(r, repo.list_items(rid))


# 前端（放在最后，避免覆盖 /api）

def _page_html() -> str:
    """读 index.html，并给 css/js 的引用带上版本号（取文件修改时间）。

    为什么非这样不可：浏览器会把 `styles.css` / `app.js` 缓存起来，
    升级之后你看到的就是「界面改了但没生效」—— 曾经真踩过：组员入口上
    居然显示着采购区块，还报「需要采购人员登录」。

    光靠 `Cache-Control: no-cache` **不够**：对那些在加这个头之前就已经
    缓存下来的副本，浏览器在自己的有效期里照样直接用。加上版本号以后，
    文件一改 URL 就跟着变，浏览器必然重新下载。而且是自动的，不用记着改。
    """
    html = (config.WEB_DIR / 'index.html').read_text(encoding='utf-8')
    for name in ('styles.css', 'app.js'):
        path = config.WEB_DIR / name
        try:
            stamp = int(path.stat().st_mtime)
        except OSError:
            stamp = 0
        html = html.replace('"%s"' % name, '"%s?v=%d"' % (name, stamp))
    return html


@app.get('/', include_in_schema=False)
@app.get('/purchase', include_in_schema=False)
def api_page():
    """两个入口返回同一份页面，前端按路径切模式。

    未登录时前端显示登录门；但**真正拦得住的是接口层的登录校验** ——
    藏页面挡不住直接调接口。
    """
    return HTMLResponse(_page_html(), headers={'Cache-Control': 'no-store'})


@app.get('/api/rounds/{rid}/payers', dependencies=[Depends(require_purchase)])
def api_payers(rid: int):
    """垫付结算：这轮「谁自己先掏了多少」，按金额从大到小。

    `payer` 一直只标在行上，从没汇总过。可钱的事总要有人问一句
    「这轮谁垫了多少、该给谁转账」—— 一条条翻列表不现实。
    退货按净额算（`rules.item_net_paid`）。
    """
    _round_or_404(rid)
    return repo.payer_summary(rid)


app.mount('/', StaticFiles(directory=str(config.WEB_DIR), html=True), name='web')
