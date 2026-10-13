/* 采购系统前端：选名字 -> 建轮次 -> 填条目 -> 下单/收发票 -> 导出 */
const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  members: [],
  rounds: [],
  round: null,
  items: [],
  invoices: [],
  invoiceCounts: {},
  pendingOrder: [],
  pendingInvoice: [],
  split: null,
  splitIds: { low: [], high: [] },
  me: null,
  editing: null,
  editRev: null,          // 打开这条时它的 rev：别人中途改过就拦下来（见 submitForm）
  busy: false,            // 有请求在路上：自动刷新和按钮都让开
  view: 'all',
  query: '',              // 搜索框里的关键词（空格分词，全都命中才显示）
  uploadFor: null,
  block: null,
  mailPresets: [],
  mailConfig: null,
  pending: [],
  share: null,
  hasPassword: false,
  memberHasPassword: false,
};

/* 两个入口：/purchase = 采购人员（要密码），其它路径 = 组员。
   这里只是**视图层**的分界线，真正的权限在服务端。 */
const MODE = document.documentElement.className === 'mode-purchase'
  ? 'purchase' : 'member';
const IS_PURCHASE = MODE === 'purchase';

async function api(path, opts = {}) {
  const res = await fetch(path, Object.assign(
    { headers: { 'Content-Type': 'application/json' } }, opts));
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try { const j = await res.json(); if (j.detail) detail = j.detail; } catch (e) { /* 非 JSON */ }
    const err = new Error(detail);
    err.status = res.status;      // 409 = 这条被别人改过，要单独处理
    throw err;
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res;
}

function toast(text, isError) {
  const t = $('toast');
  t.textContent = text;
  t.className = 'toast show' + (isError ? ' error' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = 'toast'; }, 2800);
}

function num(id) {
  const v = $(id).value.trim();
  return v === '' ? null : Number(v);
}

function fmt(v) {
  if (v === null || v === undefined || v === '') return '';
  const n = Number(v);
  return Number.isFinite(n) ? (Math.round(n * 100) / 100).toString() : String(v);
}

/* ---------------------------------------------------------------- 身份 */

async function loadMeta() {
  state.meta = await api('/api/meta');
}

async function loadMembers() {
  state.members = await api('/api/members');
  const sel = $('who');
  sel.innerHTML = '';
  state.members.forEach((m) => {
    const o = document.createElement('option');
    o.value = String(m.id);
    o.textContent = m.name + (m.is_owner ? '（负责人）' : '');
    sel.appendChild(o);
  });
  const saved = savedMeId();
  const known = !!saved && state.members.some((m) => String(m.id) === saved);
  if (known) sel.value = saved;
  // **只认「真认过的」那个名字**：光凭下拉里排第一个就当他是谁，
  // 名字门就白设了 —— 申购人、垫资人、导出都会悄悄记到第一个人头上，
  // 从名字门里出去加自己的人还会因为 state.me 非空而再也弹不出门来。
  state.me = known ? state.members.find((m) => String(m.id) === saved) : null;

  // 「谁垫的钱」那个框给个名单提示。
  // 用 datalist 而不是下拉：垫资的人偶尔可能在名单之外（比如外组同学代买）。
  const payers = $('payers');
  payers.innerHTML = '';
  state.members.forEach((m) => {
    const o = document.createElement('option');
    o.value = m.name;
    payers.appendChild(o);
  });

  renderMembers();          // 采购界面那张「组员管理」表
}

/* ---------------------------------------------------------------- 轮次 */

async function loadRounds() {
  state.rounds = await api('/api/rounds');
  const sel = $('roundSelect');
  sel.innerHTML = '';
  if (!state.rounds.length) {
    sel.innerHTML = '<option value="">（还没有轮次，点「新建轮次」）</option>';
    state.round = null;
    state.items = [];
    renderAll();
    return;
  }
  state.rounds.forEach((r) => {
    const o = document.createElement('option');
    o.value = String(r.id);
    o.textContent = r.name + (r.status === 'open' ? '' : '（已锁定）');
    sel.appendChild(o);
  });
  const saved = localStorage.getItem('round_id');
  if (saved && state.rounds.some((r) => String(r.id) === saved)) sel.value = saved;
  await loadRound(sel.value);
}

async function loadRound(id) {
  if (!id) { state.round = null; state.items = []; renderAll(); return; }
  const d = await api('/api/rounds/' + id);
  state.round = d.round;
  state.items = d.items;
  state.split = d.split;
  state.invoices = d.invoices || [];
  state.invoiceCounts = d.invoice_counts || {};
  state.pendingOrder = d.pending_order || [];
  state.pendingInvoice = d.pending_invoice || [];
  state.splitIds = d.split_ids || { low: [], high: [] };
  localStorage.setItem('round_id', String(id));
  if (IS_PURCHASE) await loadBlock();     // 总表行块是采购专用的
  renderAll();
  if (!IS_PURCHASE) refreshMemberProgress();
}

/* ---------------------------------------------------------------- 渲染 */

function renderAll() {
  const r = state.round;
  const open = r && r.status === 'open';

  const st = $('roundStatus');
  if (!r) { st.textContent = ''; st.className = 'pill'; }
  else if (open) { st.textContent = '填写中'; st.className = 'pill'; }
  else { st.textContent = '已锁定'; st.className = 'pill locked'; }

  // 没有轮次时也要能填、能识别 —— 否则一打开就是一片灰，像是坏了。
  // 只有「本轮已锁定」才真的不让改内容。
  const editable = !r || open;

  $('lockRound').disabled = !open;
  $('exportXlsx').disabled = !r;
  $('submitItem').disabled = !open;
  ['f_name', 'f_spec', 'f_qty', 'f_unit', 'f_price', 'f_freight', 'f_purpose',
   'f_url', 'f_note', 'f_paid'].forEach((id) => { $(id).disabled = !editable; });

  renderSplit();
  renderProgress();
  renderMerge();
  renderBlock();
  renderItems();
  updateCalc();
}

function renderSplit() {
  const bar = $('splitBar');
  if (!state.round) { bar.innerHTML = ''; return; }
  const s = state.split || { low_count: 0, high_count: 0, low_sum: 0, high_sum: 0, all_sum: 0 };
  bar.innerHTML =
    '共 <b>' + state.items.length + '</b> 条，合计 <b>' + fmt(s.all_sum) + '</b> 元 &nbsp;·&nbsp; ' +
    '低于一千 <b>' + s.low_count + '</b> 条 / <b>' + fmt(s.low_sum) + '</b> 元 &nbsp;·&nbsp; ' +
    '<span class="hi">高于一千 <b>' + s.high_count + '</b> 条 / <b>' + fmt(s.high_sum) + '</b> 元</span>' +
    '<span class="muted">（按「同一商家本轮合计 ≥ ' + fmt(s.threshold) + ' 元」自动拆）</span>';
}

function renderProgress() {
  const bar = $('progressBar');
  if (!state.round) { bar.innerHTML = ''; return; }
  const items = state.items || [];
  const total = items.length;
  // 退货的不进「发票 / 下单」这两个口径：票跟着退货删了，留着只会显得"还差好几张"
  const live = items.filter((i) => !i.refunded);
  const arrived = live.filter((i) => i.order_status === state.meta.order_arrived).length;
  const got = live.length - state.pendingInvoice.length;   // 已经有票的条目数
  const sheets = (state.invoices || []).length;            // **发票文件张数**
  const noPaid = live.filter(needsPaid).length;
  bar.innerHTML =
    '下单进度：<b>' + (live.length - state.pendingOrder.length) + ' / ' + live.length + '</b> 已下单' +
    '（<b>' + state.pendingOrder.length + '</b> 待下单） &nbsp;·&nbsp; ' +
    '到货 <b>' + arrived + '</b> &nbsp;·&nbsp; ' +
    // **张数就写张数**：以前这里拿"条目数"当发票数（20 条的轮次显示成 20 张票，
    // 实际只有 13 张），一票多条时差得更远。现在张数、覆盖条数分开说。
    '<span title="张数 = 发票文件数；一张票可能覆盖好几条采购（淘宝一个订单买几样）">' +
    '发票：<b>' + sheets + '</b> 张（<b>' + got + ' / ' + live.length + '</b> 条已有票）</span>' +
    (state.pendingInvoice.length
      ? ' &nbsp;<span class="warnpill">还有 ' + state.pendingInvoice.length + ' 笔没发票</span>'
      : ' &nbsp;<span class="okpill">采购都有票了</span>') +
    (total > live.length
      ? ' &nbsp;<span class="muted small">（退货 ' + (total - live.length) + ' 条不计）</span>'
      : '') +
    // 有票没金额：不点出来就没人管，对账差额永远对不准（9 月那批就是这样）
    (noPaid
      ? ' &nbsp;<span class="warnpill link" data-view="missing_paid" title="点一下只看这些行">'
        + noPaid + ' 条有发票但没填实付金额</span>'
      : '');
}

/* ---------------------------------------------------------------- 分享给组员 */

async function loadShare() {
  try {
    const info = await api('/api/share');
    state.share = info;
    $('shareUrl').textContent = info.url;
    $('shareLocal').textContent = '在这台电脑上打开：' + info.local_url;
    const img = $('shareQr');
    if (info.qrcode) {
      img.hidden = false;
      img.src = '/api/share/qrcode.png?t=' + Date.now();
      $('shareHint').textContent = '';
    } else {
      img.hidden = true;
      $('shareHint').textContent = '（没装 segno，二维码不可用；把地址发给组员就行）';
    }
  } catch (e) {
    $('shareHint').textContent = '取地址失败：' + e.message;
  }
}

async function copyShareUrl() {
  const url = (state.share && state.share.url) || '';
  if (!url) { toast('地址还没取到', true); return; }
  let ok = false;
  try {
    await navigator.clipboard.writeText(url);
    ok = true;
  } catch (e) {
    ok = false;
  }
  if (!ok) {
    // 局域网走的是 http，浏览器不给用剪贴板 API，退回到老办法
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    document.body.removeChild(ta);
  }
  toast(ok ? '地址已复制，发到群里就行' : '复制失败，请手动选中地址复制', !ok);
}

/* ---------------------------------------------------------------- 邮箱收发票 */

async function loadMailPresets() {
  state.mailPresets = await api('/api/mail/presets');
  const sel = $('m_preset');
  sel.innerHTML = '';
  state.mailPresets.forEach((p) => {
    const o = document.createElement('option');
    o.value = p.host;
    o.dataset.port = String(p.port);
    o.textContent = p.name;
    sel.appendChild(o);
  });
  sel.addEventListener('change', () => {
    const opt = sel.selectedOptions[0];
    if (opt && opt.value) {
      $('m_host').value = opt.value;
      $('m_port').value = opt.dataset.port || 993;
    }
  });
}

async function loadMailConfig() {
  const c = await api('/api/mail/config');
  state.mailConfig = c;
  $('m_host').value = c.host || '';
  $('m_port').value = c.port || 993;
  $('m_user').value = c.user || '';
  $('m_folder').value = c.folder || 'INBOX';
  $('m_days').value = (c.days === undefined || c.days === null) ? 30 : c.days;
  $('m_max').value = c.max_mails || 200;
  $('m_keywords').value = (c.keywords || []).join(',');
  $('m_interval').value = c.auto_interval || 0;
  $('m_unseen').checked = !!c.unseen_only;
  $('m_any').checked = !!c.any_attachment;
  $('m_password').value = '';
  $('m_password').placeholder = c.has_password ? '已保存（要改就重新填）' : '不是登录密码';
  const sel = $('m_preset');
  if (sel) sel.value = c.host || '';

  $('mailHint').textContent = c.host
    ? ((c.user || '(还没填账号)') + (c.has_password ? ' · 授权码已设置' : ' · 还没填授权码'))
    : '还没配置邮箱';
}

async function saveMailConfig() {
  const body = {
    host: $('m_host').value.trim(),
    port: Number($('m_port').value) || 993,
    user: $('m_user').value.trim(),
    folder: $('m_folder').value.trim() || 'INBOX',
    days: Number($('m_days').value) || 0,
    max_mails: Number($('m_max').value) || 200,
    unseen_only: $('m_unseen').checked,
    any_attachment: $('m_any').checked,
    auto_interval: Number($('m_interval').value) || 0,
    keywords: $('m_keywords').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
  };
  const pw = $('m_password').value;
  if (pw) body.password = pw;          // 没填就不动已保存的授权码
  try {
    await api('/api/mail/config', { method: 'PUT', body: JSON.stringify(body) });
    await loadMailConfig();
    toast('邮箱设置已保存');
  } catch (e) { toast('保存失败：' + e.message, true); }
}

async function mailTest() {
  const box = $('mailMsg');
  box.textContent = '正在连接…';
  box.className = 'msg';
  try {
    const r = await api('/api/mail/test', { method: 'POST' });
    box.textContent = r.message;
    box.className = 'msg' + (r.ok ? '' : ' error');
    toast(r.ok ? '连接成功' : '连接失败', !r.ok);
  } catch (e) {
    box.textContent = '测试失败：' + e.message;
    box.className = 'msg error';
  }
}

async function mailFetch() {
  const btn = $('mailFetch');
  const box = $('mailMsg');
  btn.disabled = true;
  box.textContent = '正在收取…（第一次会慢一点，取决于邮件多少）';
  box.className = 'msg';
  try {
    const r = await api('/api/mail/fetch', { method: 'POST' });
    box.textContent = (r.lines || []).join('\n');
    box.className = 'msg' + (r.ok ? '' : ' error');
    if (r.ok) toast('新收到 ' + r.new + ' 张发票');
    await loadPending();
    if (state.round) await loadRound(state.round.id);
  } catch (e) {
    box.textContent = '收取失败：' + e.message;
    box.className = 'msg error';
  } finally {
    btn.disabled = false;
  }
}

async function loadPending() {
  const rid = state.round && state.round.id;
  state.pending = await api('/api/mail/pending' + (rid ? '?round_id=' + rid : ''));
  renderPending();
}

function renderPending() {
  const box = $('mailPending');
  const rows = state.pending || [];
  box.innerHTML = '';
  if (!rows.length) {
    box.innerHTML = '<p class="muted small">待归档里没有东西。商家把发票发到邮箱后，'
      + '点「立即收取」就会出现在这里；确认无误再挂到对应条目。</p>';
    return;
  }
  const waiting = rows.filter((r) => r.status === 'pending').length;
  const head = document.createElement('p');
  head.className = 'muted small';
  head.textContent = '待归档 ' + waiting + ' 张' + (rows.length > waiting ? '（下面还列着已挂过的记录）' : '');
  box.appendChild(head);
  rows.forEach((r) => box.appendChild(mailCard(r)));
}

function mailCard(r) {
  const card = document.createElement('div');
  card.className = 'mailcard';

  const subj = document.createElement('div');
  subj.className = 'subj';
  subj.textContent = r.subject || '(无主题)';
  card.appendChild(subj);

  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = [r.stored_name, r.sender, r.mail_date].filter(Boolean).join(' · ');
  card.appendChild(meta);

  if (r.status === 'attached') {
    const done = document.createElement('div');
    done.className = 'meta';
    done.textContent = '已挂到条目 #' + (r.item_id || '?') + '（归档后已离开待归档）';
    card.appendChild(done);
    return card;
  }

  if (r.suggestions && r.suggestions.length) {
    const row = document.createElement('div');
    row.className = 'row';
    r.suggestions.forEach((s) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'sugg';
      b.textContent = '挂到「' + s.name + '」  ' + s.reason;
      b.onclick = () => attachMail(r.id, s.item_id);
      row.appendChild(b);
    });
    card.appendChild(row);
  }

  const row2 = document.createElement('div');
  row2.className = 'row';
  const sel = document.createElement('select');
  sel.innerHTML = '<option value="">— 手工选一个条目 —</option>';
  (state.items || []).forEach((it) => {
    const o = document.createElement('option');
    o.value = String(it.id);
    o.textContent = it.name + (it.applicant ? '（' + it.applicant + '）' : '');
    sel.appendChild(o);
  });
  const go = document.createElement('button');
  go.type = 'button';
  go.className = 'mini';
  go.textContent = '挂上';
  go.onclick = () => { if (sel.value) attachMail(r.id, Number(sel.value)); };
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'mini danger';
  del.textContent = '撤下';
  del.onclick = () => ignoreMail(r.id);
  row2.append(sel, go, del);
  card.appendChild(row2);
  return card;
}

async function attachMail(mailId, itemId) {
  try {
    const r = await api('/api/mail/attach', {
      method: 'POST', body: JSON.stringify({ mail_id: mailId, item_id: itemId }),
    });
    toast('已归档为 ' + r.stored_name);
    await loadPending();
    if (state.round) await loadRound(state.round.id);
  } catch (e) { toast('挂载失败：' + e.message, true); }
}

async function ignoreMail(mailId) {
  if (!confirm('从待归档里撤下这一张？它的文件也会删掉。')) return;
  try {
    await api('/api/mail/pending/' + mailId, { method: 'DELETE' });
    await loadPending();
    toast('已撤下');
  } catch (e) { toast('撤下失败：' + e.message, true); }
}

/* ---------------------------------------------------------------- 回填车队总表 */

async function loadBlock() {
  const rid = state.round && state.round.id;
  if (!rid) { state.block = null; renderBlock(); return; }
  try {
    const b = await api('/api/rounds/' + rid + '/block');
    if (!state.round || state.round.id !== rid) return;   // 期间切走了就丢弃
    state.block = b;
  } catch (e) {
    if (state.round && state.round.id === rid) state.block = null;
  }
  renderBlock();
}

function renderBlock() {
  const b = state.block;
  const rec = b && b.reconcile;
  const has = !!(rec && rec.count > 0);

  $('blockText').value = has ? (b.tsv || '') : '';
  $('blockHint').textContent = has ? ('共 ' + rec.count + ' 行') : '';
  $('blockCols').textContent = has ? ('列顺序：' + (b.headers || []).join(' | ')) : '';
  $('blockCopy').disabled = !has;
  $('blockXlsx').disabled = !has;

  if (!state.round) { $('blockRecon').innerHTML = '先建一个采购轮次。'; return; }
  if (!has) { $('blockRecon').innerHTML = '本轮还没有条目。'; return; }

  $('blockRecon').innerHTML =
    '预计合计 <b>' + fmt(rec.estimate) + '</b> 元 &nbsp;·&nbsp; ' +
    '实付合计 <b>' + fmt(rec.paid) + '</b> 元 &nbsp;·&nbsp; ' +
    '差额 <b>' + fmt(rec.diff) + '</b> 元 &nbsp;·&nbsp; ' +
    '运费 <b>' + fmt(rec.freight) + '</b> 元' +
    (rec.unpriced
      ? ' &nbsp;<span class="warnpill">还有 ' + rec.unpriced + ' 条没填实付金额，差额还不准</span>'
      : '');
}

function blockCopy() {
  const ta = $('blockText');
  if (!ta.value) return;
  ta.focus();
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  toast(ok ? '已复制，去在线表格里粘贴' : '已全选，请按 Ctrl+C 复制');
}

function blockXlsx() {
  if (!state.round) return;
  window.location = '/api/rounds/' + state.round.id + '/block.xlsx';
}

function renderMerge() {
  const n = state.invoices.length;
  const per = parseInt($('mergePerPage').value, 10) || 2;
  const withBack = $('mergeBack').checked;
  const groups = n ? Math.ceil(n / per) : 0;
  const pages = withBack ? groups * 2 : groups;
  $('mergeHint').textContent = n
    ? ('本轮 ' + n + ' 张发票 → ' + groups + ' 组，共 ' + pages + ' 页' + (withBack ? '（正+背）' : ''))
    : '本轮还没有发票';
  $('mergePdf').disabled = !state.round || n === 0;
}

function mergePdf() {
  if (!state.round || !state.invoices.length) return;
  const q = new URLSearchParams({
    per_page: $('mergePerPage').value,
    back: $('mergeBack').checked ? '1' : '0',
    frame: $('mergeFrame').checked ? '1' : '0',
  });
  window.open('/api/rounds/' + state.round.id + '/merged.pdf?' + q.toString(), '_blank');
}

/** 把本轮所有发票打包成 zip 下载（一票多条时那张票只出现一次）。

 * 先自己看一眼有没有票 —— 没票就直接说，别跳过去让人看一页 JSON 报错。
 */
function invoiceZip() {
  if (!state.round) return;
  if (!state.invoices.length) {
    toast('本轮还没有发票，先在条目上「传发票」', true);
    return;
  }
  window.location = '/api/rounds/' + state.round.id + '/invoices.zip';
}

function visibleItems() {
  let rows = state.items;
  if (state.view === 'pending_order') {
    const ids = new Set(state.pendingOrder);
    rows = rows.filter((i) => ids.has(i.id));
  } else if (state.view === 'pending_invoice') {
    const ids = new Set(state.pendingInvoice);
    rows = rows.filter((i) => ids.has(i.id));
  } else if (state.view === 'missing_paid') {
    // 有发票却没填实付金额 —— 这些不补上，对账的差额永远对不准
    rows = rows.filter(needsPaid);
  } else if (state.view === 'advanced') {
    // 「垫资」：这钱是某个人自己先垫的
    rows = rows.filter((i) => (i.payer || '').trim() !== '');
  }
  if ($('onlyMine').checked && state.me) {
    // 按**选中的那个人（id）**筛，别按姓名文字 ——
    // 改了名字、或者采购代填时写的是别人的名字，按文字比就会漏。
    rows = rows.filter((i) => isMine(i));
  }
  // 搜索框：空格分开的几个词要**都**命中（「电容 22」一次就能筛出来）
  const words = String(state.query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length) {
    rows = rows.filter((i) => {
      const hay = [i.name, i.spec, i.unit, i.purpose, i.applicant, i.note, i.url, i.payer]
        .map((v) => String(v == null ? '' : v).toLowerCase()).join(' ');
      return words.every((w) => hay.indexOf(w) >= 0);
    });
  }
  return rows;
}

/** 这一条在**全表**里的序号（从 1 起）。

 * 表格第一列和「改挂」弹窗里的 `#N` 必须用同一个号 ——
 * 以前表格显示的是"当前视图里的第几个"、弹窗显示的是"全表第几个"，
 * 切换过视图（缺发票 / 缺金额 / 垫资）之后，同一个 `#5` 指的不是同一条，
 * 电话里说"第 5 条那个"就对不上了。
 */
function itemNo(it) {
  const i = (state.items || []).indexOf(it);
  return i >= 0 ? i + 1 : 0;
}

function invoicesOf(itemId) {
  // 一张票可以挂多条（淘宝一个订单买几样）。优先看完整归属 item_ids；
  // 老数据没有这个字段时，退回只认主条目。
  return state.invoices.filter((v) => (
    (v.item_ids && v.item_ids.length)
      ? v.item_ids.indexOf(itemId) >= 0
      : v.item_id === itemId
  ));
}

/** 「有发票，却没填实付金额」——对账的差额就是被这些行带偏的。
 *
 * 票是照片或扫描件的时候（没有文字层），系统读不出金额，只能人工补；
 * 这件事必须在界面上看得见，不然就一直是笔糊涂账。
 */
function needsPaid(it) {
  const empty = it.paid === null || it.paid === undefined || it.paid === '';
  if (!empty) return false;
  if (invoicesOf(it.id).length) return true;
  return Number(state.invoiceCounts[it.id] || 0) > 0;
}

/* 一票多条时用来分组的淡色 —— 只要求"能互相区分"，不抢内容 */
const NAME_KEY = 'cgsys_me';        // 上次认的名字，下次打开自动认

/** 上次认的名字（id 字符串）。
 *
 * 两个 key 都看：老版本在顶部下拉选的时候写的是 `me_id`，而名字门写的是
 * `cgsys_me` —— 各写各的，于是"下拉改过名字"的人下次进来还是会被名字门拦一次。
 * 读的时候兼容旧的，写的时候统一到 `NAME_KEY`。
 */
function savedMeId() {
  try {
    return localStorage.getItem(NAME_KEY) || localStorage.getItem('me_id') || '';
  } catch (e) {
    return '';                       // 无痕模式
  }
}

function rememberMeId(id) {
  try {
    localStorage.setItem(NAME_KEY, String(id));
    localStorage.removeItem('me_id');
  } catch (e) { /* 无痕模式：记不住就下次再问一遍 */ }
}

const INV_GROUP_COLORS = [
  '#eef6ff', '#eefaf1', '#fff8e6', '#f7efff',
  '#e9f7f7', '#fdeff3', '#f4f7ea', '#f0f1f6',
];

/** 算出每条采购该涂什么色：同一张票覆盖的条目同色，不同票不同色。
 *
 * 只给**一张票挂多条**的才上色 —— 一票一条的话整张表都会花，反而看不出重点。
 * 一条同时属于两张票时，用先遇到的那张的颜色（够用了，重点是"看得出是一伙的"）。
 */
function invoiceGroupColors() {
  const out = {};
  let n = 0;
  (state.invoices || []).forEach((inv) => {
    const ids = (inv.item_ids && inv.item_ids.length)
      ? inv.item_ids : [inv.item_id];
    if (ids.length < 2) return;
    const c = INV_GROUP_COLORS[n % INV_GROUP_COLORS.length];
    n += 1;
    ids.forEach((id) => { if (out[id] === undefined) out[id] = c; });
  });
  return out;
}


function renderItems() {
  const rows = visibleItems();

  $('c_all').textContent = state.items.length;
  $('c_po').textContent = state.pendingOrder.length;
  $('c_pi').textContent = state.pendingInvoice.length;
  $('c_mp').textContent = state.items.filter(needsPaid).length;
  $('c_adv').textContent = state.items.filter(
    (i) => (i.payer || '').trim() !== '').length;

  const tb = $('itemsBody');
  tb.innerHTML = '';
  const groupColor = invoiceGroupColors();     // 一票多条 → 涂同一个淡色
  rows.forEach((it) => {
    const tr = document.createElement('tr');
    if (Number(it.price) > 1000) tr.className = 'needs-contract';
    if (it.refunded) tr.className += ' refunded';
    if (groupColor[it.id]) tr.style.background = groupColor[it.id];

    const plain = [
      itemNo(it), it.name, it.spec, fmt(it.qty), it.unit, fmt(it.price), fmt(it.freight),
      fmt(it.subtotal), fmt(it.total), it.purpose, it.applicant, it.url, it.group_name,
      it.note, fmt(it.paid),
    ];
    plain.forEach((v, i) => {
      const td = document.createElement('td');
      td.textContent = v === null || v === undefined ? '' : v;
      if ([1, 2, 9, 11, 13].includes(i)) td.className = 'left';
      // 实付金额列：**有发票却没填金额**的行，就地补一个入口。
      // 不然只能进「改」表单填，而本轮锁定之后表单根本保存不了
      // （付款、收票本来就发生在锁定之后，9 月那批票就是卡在这）。
      if (i === 14 && needsPaid(it)) {
        td.textContent = '';
        td.className = 'left';
        const can = IS_PURCHASE || isMine(it);
        if (can) {
          const fill = document.createElement('button');
          fill.type = 'button';
          fill.className = 'mini warnmini';
          fill.textContent = '填金额';
          fill.title = '这张发票读不出金额（照片/扫描件），按票面把实付金额填上';
          fill.onclick = () => askPaid(it);
          td.appendChild(fill);
        } else {
          const s = document.createElement('span');
          s.className = 'warnpill';
          s.textContent = '有票没金额';
          s.title = '这张发票挂上了，但实付金额还空着';
          td.appendChild(s);
        }
      }
      // 「申购人」那列下面挂个垫资标记（谁先掏的钱）
      if (i === 10 && (it.payer || '').trim()) {
        const pay = document.createElement('span');
        pay.className = 'payertag';
        pay.textContent = '垫资 ' + it.payer;
        pay.title = '这钱是 ' + it.payer + ' 先垫的，回头要找车队报';
        td.appendChild(pay);
      }
      // 退了的那条：名称后面挂个标记，一眼能看出来
      if (i === 1 && it.refunded) {
        const tag = document.createElement('span');
        tag.className = 'refundtag';
        tag.textContent = '已退货';
        tag.title = '实付 ' + fmt(it.paid) + '，退回 ' + fmt(it.refund_amount)
                  + '；导出时按净额算';
        td.appendChild(tag);
      }
      // 链接太长会把整张表撑爆（淘宝分享的链接能有 100 多字符），
      // 所以只显示域名短标签，完整地址放悬浮提示，点开是新标签页。
      if (i === 11 && it.url) {
        const a = document.createElement('a');
        a.href = it.url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = shortUrl(it.url);
        a.title = it.url;
        td.textContent = '';
        td.appendChild(a);
      }
      tr.appendChild(td);
    });

    tr.appendChild(statusCell(it, 'order_status', state.meta.order_statuses));
    tr.appendChild(statusCell(it, 'invoice_status', state.meta.invoice_statuses));
    tr.appendChild(invoiceCell(it));
    tr.appendChild(actionCell(it));
    tb.appendChild(tr);
  });

  $('emptyHint').textContent = rows.length ? '' : (
    state.query
      ? '没搜到「' + state.query + '」相关的条目 —— 换个词，或按 Esc 清掉搜索。'
      : state.items.length === 0
        ? (IS_PURCHASE ? '本轮还没有条目。' : '你还没填过东西 —— 上面写好，点「保存条目」就行。')
        : '这个清单里没有东西了' + (state.view === 'pending_invoice' ? '——发票都齐了。' : '。'));

  // 当前视图合计：贴在表格**下面**的独立一块，不占表格里的一行。
  // （以前是 tfoot，跟数据行连成一片，看着像表格还有一条。）
  const sum = (f) => rows.reduce((a, b) => a + (Number(b[f]) || 0), 0);
  const sumBox = $('itemsSum');
  if (!rows.length) {
    sumBox.hidden = true;
    sumBox.innerHTML = '';
  } else {
    sumBox.hidden = false;
    sumBox.innerHTML =
      '<span class="sumrow">当前视图合计（' + rows.length + ' 条）</span>' +
      '<span class="sumitem">小计金额 <b>¥' + fmt(sum('subtotal')) + '</b></span>' +
      '<span class="sumitem">合计金额 <b>¥' + fmt(sum('total')) + '</b></span>';
  }
}

function statusCell(it, field, options) {
  const td = document.createElement('td');

  // 下单、发票状态是采购人员的事。组员这里只看结果、不给改
  // （后端本来也不让改：/api/items/{id}/status 是采购专用的）。
  if (!IS_PURCHASE) {
    const span = document.createElement('span');
    span.className = 'cellselect readonly';
    span.textContent = it[field] || '';
    if (field === 'invoice_status' && it.invoice_status === state.meta.inv_received) {
      span.classList.add('done');
    }
    td.appendChild(span);
    return td;
  }

  const sel = document.createElement('select');
  sel.className = 'cellselect';
  options.forEach((v) => {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = v;
    sel.appendChild(o);
  });
  sel.value = it[field];
  if (field === 'order_status' && it.order_status === state.meta.order_arrived) sel.classList.add('done');
  if (field === 'invoice_status' && it.invoice_status === state.meta.inv_received) sel.classList.add('done');
  sel.onchange = () => setStatus(it, field, sel.value, sel);
  td.appendChild(sel);
  return td;
}

function isMine(it) {
  return !!(state.me && Number(it.member_id) === Number(state.me.id));
}

function invoiceCell(it) {
  const td = document.createElement('td');
  td.className = 'invcell';
  const mine = isMine(it);
  const canChange = IS_PURCHASE || mine;
  const list = invoicesOf(it.id);
  list.forEach((inv) => {
    const box = document.createElement('span');
    box.className = 'invfile';

    const a = document.createElement('a');
    a.href = '/api/invoices/' + inv.id + '/file';
    a.target = '_blank';
    a.rel = 'noreferrer';
    a.textContent = '看';
    a.title = inv.stored_name;
    box.appendChild(a);

    const b = document.createElement('a');
    b.href = '/api/invoices/' + inv.id + '/file?download=1';
    b.textContent = '存';
    b.title = '下载 ' + inv.stored_name;
    box.appendChild(b);

    // 「改挂」：采购随便挂；组员也能用，但**只能挂到自己填的条目上**
    // （后端同样这么卡）。所以这个按钮对"自己那笔"也放出来。
    if (canChange) {
      const move = document.createElement('button');
      move.type = 'button';
      move.className = 'mini';
      move.textContent = '改挂';
      move.title = IS_PURCHASE
        ? '把这张发票换到别的采购条目上（可以选多条）'
        : '把这张发票也挂到你填的其它条目上';
      move.onclick = () => reassignInvoice(inv);
      box.appendChild(move);
    }

    if (canChange) {
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'mini danger';
      del.textContent = '×';
      del.title = '删掉这张发票';
      del.onclick = () => removeInvoice(inv);
      box.appendChild(del);
    }

    td.appendChild(box);
  });

  // 组员只能往自己那条上挂发票（后端也这么卡）。
  if (canChange) {
    const up = document.createElement('button');
    up.type = 'button';
    up.className = 'mini';
    up.textContent = list.length ? '再传' : '传发票';
    up.onclick = () => {
      state.uploadFor = it.id;
      $('invoiceInput').click();
    };
    td.appendChild(up);
  }
  return td;
}

function actionCell(it) {
  const td = document.createElement('td');
  const open = state.round && state.round.status === 'open';
  const mine = isMine(it);

  // 组员现在看得到全组的条目，但**只有自己那行**才有改/删 ——
  // 别人的行上放按钮只会点出 403。
  if (open && (IS_PURCHASE || mine)) {
    const b1 = document.createElement('button');
    b1.textContent = '改';
    b1.className = 'mini';
    b1.onclick = () => startEdit(it);
    const b2 = document.createElement('button');
    b2.textContent = '删';
    b2.className = 'mini';
    b2.onclick = () => removeItem(it);
    td.append(b1, b2);
  } else if (!IS_PURCHASE && !mine) {
    td.innerHTML = '<span class="muted small">别人填的</span>';
  } else {
    td.innerHTML = '<span class="muted small">已锁定</span>';
  }

  // 退货是采购人员的事；和下单、收发票一样，**锁定本轮之后照样能退**
  if (IS_PURCHASE) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'mini' + (it.refunded ? '' : ' danger');
    b.textContent = it.refunded ? '取消退货' : '退货';
    b.title = it.refunded
      ? '把这条恢复成正常采购'
      : '整单退货：这条的发票会被删掉，记录保留';
    b.onclick = () => toggleRefund(it);
    td.appendChild(b);
  }
  return td;
}

async function toggleRefund(it) {
  const goingBack = !!it.refunded;
  if (!goingBack) {
    const n = invoicesOf(it.id).length;
    const lines = [
      '确定「' + it.name + '」整单退货？',
      '',
      '· 金额按净额算：实付 ' + fmt(it.paid) + '，全退 → 导出时填 0',
      n ? '· 这条上的 ' + n + ' 张发票会被删掉（删了就找不回来）'
        : '· 这条上还没有发票',
      '· 记录会留着（标成「已退货」），不是删掉',
    ];
    if (!confirm(lines.join('\n'))) return;
  }
  try {
    await api('/api/items/' + it.id + '/refund', {
      method: 'POST',
      body: JSON.stringify({ refunded: !goingBack }),
    });
    await loadRound(state.round.id);
    toast(goingBack ? '已取消退货' : '已标记「已退货」');
  } catch (e) {
    toast('操作失败：' + e.message, true);
  }
}

function updateCalc() {
  const q = num('f_qty'), p = num('f_price'), f = num('f_freight') || 0;
  if (q === null || p === null) { $('calcHint').textContent = ''; return; }
  const sub = Math.round(q * p * 100) / 100;
  $('calcHint').textContent = '小计 ' + fmt(sub) + ' + 运费 ' + fmt(f) + ' = 合计 ' + fmt(sub + f) + ' 元';
}

/* ---------------------------------------------------------------- 表单 */

function readForm() {
  return {
    member_id: state.me ? state.me.id : null,
    name: $('f_name').value, spec: $('f_spec').value,
    qty: num('f_qty'), unit: $('f_unit').value,
    price: num('f_price'), freight: num('f_freight') || 0,
    purpose: $('f_purpose').value, url: $('f_url').value, note: $('f_note').value,
    paid: num('f_paid'),
    payer: $('f_payer').value,
    applicant: state.me ? state.me.name : '',
    phone: state.me ? state.me.phone : '',
    group_name: state.meta.group,
  };
}

async function parseShare() {
  const box = $('shareText');
  const msg = $('parseMsg');
  const text = box.value.trim();
  if (!text) { toast('先把分享文案粘进来', true); return; }
  try {
    const r = await api('/api/parse-share', {
      method: 'POST', body: JSON.stringify({ text }),
    });
    const filled = [];
    const skipped = [];

    if (r.name) {
      if ($('f_name').value.trim()) skipped.push('名称');
      else { $('f_name').value = r.name; filled.push('名称「' + r.name + '」'); }
    }
    if (r.url) {
      if ($('f_url').value.trim()) skipped.push('链接');
      else { $('f_url').value = r.url; filled.push('链接'); }
    }
    if (r.price !== null && r.price !== undefined) {
      const isPaid = (r.price_from === '实付' || r.price_from === '到手价');
      const id = isPaid ? 'f_paid' : 'f_price';
      const label = isPaid ? '实付金额' : '预计单价';
      if ($(id).value.trim()) skipped.push(label);
      else {
        $(id).value = r.price;
        filled.push(label + ' ' + r.price + '（' + r.price_from + '）');
      }
    }
    updateCalc();

    const lines = [filled.length ? '已填入：' + filled.join('、') : '没有可填的字段'];
    if (skipped.length) lines.push('已有内容、未覆盖：' + skipped.join('、'));
    (r.notes || []).forEach((n) => lines.push(n));
    msg.textContent = lines.join('；');
    msg.className = 'msg' + (filled.length ? ' warn' : ' error');

    if (filled.length) {
      box.value = '';
      toast('已识别，请核对名称');
    }
  } catch (e) {
    msg.textContent = '识别失败：' + e.message;
    msg.className = 'msg error';
  }
}

function resetForm() {
  state.editing = null;
  state.editRev = null;
  $('itemForm').reset();
  $('f_freight').value = '0';
  $('itemId').value = '';
  $('formTitle').textContent = '新增采购条目';
  $('submitItem').textContent = '保存条目';
  $('formMsg').textContent = '';
  $('formMsg').className = 'msg';
  $('shareText').value = '';
  $('parseMsg').textContent = '';
  $('parseMsg').className = 'msg';
  updateCalc();
}

function startEdit(it) {
  state.editing = it.id;
  state.editRev = Number(it.rev) || 0;
  $('itemId').value = String(it.id);
  $('f_name').value = it.name || '';
  $('f_spec').value = it.spec || '';
  $('f_qty').value = it.qty === null ? '' : it.qty;
  $('f_unit').value = it.unit || '';
  $('f_price').value = it.price === null ? '' : it.price;
  $('f_freight').value = it.freight === null ? 0 : it.freight;
  $('f_purpose').value = it.purpose || '';
  $('f_url').value = it.url || '';
  $('f_note').value = it.note || '';
  $('f_paid').value = it.paid === null ? '' : it.paid;
  $('f_payer').value = it.payer || '';
  $('formTitle').textContent = '修改条目：' + (it.name || '');
  // 手机上条目表在下面，点了「改」如果不把表单滚上来，看着像没反应。
  $('formTitle').scrollIntoView({ behavior: 'smooth', block: 'center' });
  $('submitItem').textContent = '保存修改';
  updateCalc();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function submitForm(ev) {
  ev.preventDefault();
  if (!state.round) { toast('先建一个采购轮次', true); return; }
  if (!state.me) { toast('先在右上角选一个名字', true); return; }
  // 防连点：手机上网络慢，点两下就是两条一样的条目。
  // （服务端的并发检查管不了这个 —— 那是两次都是"新增"。）
  if (state.busy) return;
  const btn = $('submitItem');
  const editing = state.editing;
  const body = readForm();
  // 带上「我打开这条时它是什么版本」，被别人改过服务端会拦（409）
  if (editing) body.base_rev = state.editRev;
  state.busy = true;
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    let res;
    if (editing) res = await api('/api/items/' + editing, { method: 'PUT', body: JSON.stringify(body) });
    else res = await api('/api/rounds/' + state.round.id + '/items', { method: 'POST', body: JSON.stringify(body) });
    const warns = res.warnings || [];
    resetForm();
    await loadRound(state.round.id);
    if (warns.length) {
      $('formMsg').textContent = '已保存，但注意：' + warns.join('；');
      $('formMsg').className = 'msg warn';
      toast('已保存（有提醒）');
    } else {
      toast('已保存');
    }
  } catch (e) {
    await onSaveFailed(e, editing);
  } finally {
    state.busy = false;
    btn.disabled = false;
    btn.textContent = state.editing ? '保存修改' : '保存条目';
  }
}

/** 保存没成功时，把话说清楚：**填的内容一律不丢**。 */
async function onSaveFailed(e, editing) {
  const msg = $('formMsg');
  if (e.status === 409) {
    // 这条在别人手上也被改过。内容留着，让本人对着最新的看一眼再决定。
    msg.textContent = '这条在你填的时候被别人改过了（内容已经不是你打开时的样子）。'
      + '你填的都还在，没有丢；想稳妥就先点那一行的「改」重新打开对照一下，'
      + '再点一次「保存修改」就按你现在填的存。';
    msg.className = 'msg warn';
    toast('这条刚被别人改过，请先看一眼再存', true);
    try {
      await loadRound(state.round.id);
      const fresh = (state.items || []).find((x) => x.id === editing);
      if (fresh) state.editRev = Number(fresh.rev) || 0;   // 再按一次就能存
    } catch (err) { /* 拉不动就算了，内容还在表单里 */ }
    return;
  }
  msg.textContent = '保存失败：' + e.message
    + (/锁定/.test(e.message) ? '（你填的内容还在这个表单里，等采购解锁后再点一次保存即可）' : '');
  msg.className = 'msg error';
  // 把没填的那个框直接指出来 —— 光说「用途 不能为空」还得自己一个个找。
  const bad = document.querySelector('.card form :invalid');
  if (bad) {
    bad.focus();
    bad.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

async function removeItem(it) {
  if (!confirm('确定删除「' + it.name + '」？')) return;
  try {
    await api('/api/items/' + it.id, { method: 'DELETE' });
    await loadRound(state.round.id);
    toast('已删除');
  } catch (e) { toast('删除失败：' + e.message, true); }
}

/* ---------------------------------------------------------------- 状态与发票 */

async function setStatus(it, field, value, sel) {
  sel.disabled = true;
  state.busy = true;          // 别让自动刷新在同一时刻重绘这张表
  try {
    await api('/api/items/' + it.id + '/status', {
      method: 'POST',
      body: JSON.stringify(field === 'order_status'
        ? { order_status: value } : { invoice_status: value }),
    });
    await loadRound(state.round.id);
  } catch (e) {
    toast('改状态失败：' + e.message, true);
    sel.disabled = false;
    await loadRound(state.round.id);
  } finally {
    state.busy = false;
  }
}

/** 补填「实付金额」。
 *
 * 走的是专用接口，**不看轮次是否锁定** —— 付款、收发票都在锁定（汇总）之后，
 * 而普通保存（PUT /api/items/{id}）锁定后是一律拒绝的。
 * 票面是照片/扫描件时读不出金额，只能这样人工补。
 */
async function setPaid(it, value) {
  try {
    await api('/api/items/' + it.id + '/paid', {
      method: 'POST', body: JSON.stringify({ paid: value }),
    });
    await loadRound(state.round.id);
    return true;
  } catch (e) {
    toast('实付金额没填上：' + e.message, true);
    return false;
  }
}

async function askPaid(it) {
  const v = await askText('「' + (it.name || '') + '」实际付了多少钱？（元）', fmt(it.paid));
  if (v === null) return;
  const n = Number(v);
  if (v !== '' && !Number.isFinite(n)) { toast('请填数字', true); return; }
  if (await setPaid(it, v === '' ? null : n)) toast(v === '' ? '已清空实付金额' : '实付金额已填上');
}

/** 上传后读不出金额时，当场问一句实付金额（填了就落到条目上）。
 *
 * 一票多条的情况在这儿只填当前这条；其余各条在核对框里或事后按「填金额」补。
 */
async function askPaidForUpload(iid) {
  const it = (state.items || []).find((x) => x.id === iid) || { id: iid, name: '' };
  const v = await askText(
    '「' + (it.name || '这条采购') + '」的发票读不出金额'
    + '（照片 / 扫描件没有文字层，系统只读得懂电子发票 PDF）。'
    + '按票面填一下实付金额？填不了也没关系，之后能在这一行的「填金额」里补。', '');
  if (v === null || v === '') return;
  const n = Number(v);
  if (!Number.isFinite(n)) { toast('要填数字才行，稍后在那一行的「填金额」里补', true); return; }
  if (await setPaid({ id: iid }, n)) toast('实付金额已填上 ¥' + fmt(n));
}

async function uploadInvoiceFile(file) {
  const iid = state.uploadFor;
  if (!iid || !file) return;
  const fd = new FormData();
  fd.append('file', file);
  state.busy = true;
  try {
    const res = await fetch('/api/items/' + iid + '/invoices', { method: 'POST', body: fd });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.detail || (res.status + ' ' + res.statusText));
    await loadRound(state.round.id);
    // 一句话说清「存成什么名 / 有没有转 PDF / 金额是哪来的」
    const bits = [j.state === 'duplicate'
      ? '这张发票之前已经传过，没有重复存一份'
      : (j.converted ? '图片已自动转成 PDF：' : '已存为 ') + j.invoice.stored_name];
    if (j.paid_filled != null) {
      bits.push(j.paid_source === 'ocr'
        // OCR 认出来的只是**建议值**：票面小字认错一位就差好几倍，必须人核一眼
        ? 'OCR 认出来 ¥' + fmt(j.paid_filled) + '，请对一下票面（不对就点这一行的「填金额」改）'
        : '实付金额已自动填上 ¥' + fmt(j.paid_filled));
    }
    toast(bits.join('；'));

    // 一票多单：金额对不上任何一条，弹出核对框让人确认怎么分
    if (j.need_review) {
      const picked = await showAssignBox(j);
      if (picked) {
        try {
          await api('/api/invoices/' + j.invoice.id + '/assign', {
            method: 'POST', body: JSON.stringify({ lines: picked }),
          });
          await loadRound(state.round.id);
          toast('已按核对结果挂到 ' + picked.length + ' 条采购上');
        } catch (e2) {
          toast('分配失败：' + e2.message, true);
        }
      }
    } else if (j.need_paid) {
      // 读不出金额（照片、扫描件、没有文字层）：**当场问一句**。
      // 以前这里不出声，票挂上了、实付金额空着，谁也不知道要回来补。
      await askPaidForUpload(iid, j);
    }
  } catch (e) {
    toast('上传失败：' + e.message, true);
  } finally {
    state.busy = false;
    state.uploadFor = null;
    $('invoiceInput').value = '';
  }
}

async function removeInvoice(inv) {
  if (!confirm('删掉发票「' + inv.stored_name + '」？归档文件也会一起删。')) return;
  try {
    await api('/api/invoices/' + inv.id, { method: 'DELETE' });
    await loadRound(state.round.id);
    toast('已删除');
  } catch (e) { toast('删除失败：' + e.message, true); }
}

/* ---------------------------------------------------------------- 动作 */

async function newRound() {
  const name = await askText('这一轮叫什么？（例如：八月采购）');
  if (!name) return;
  try {
    const r = await api('/api/rounds', { method: 'POST', body: JSON.stringify({ name }) });
    await loadRounds();
    $('roundSelect').value = String(r.id);
    await loadRound(r.id);
    toast('已新建：' + r.name);
  } catch (e) { toast('新建失败：' + e.message, true); }
}

async function lockRound() {
  if (!state.round) return;
  if (!confirm('锁定后组员就不能再改条目了（下单和收发票不受影响），确定？')) return;
  try {
    await api('/api/rounds/' + state.round.id + '/status',
      { method: 'POST', body: JSON.stringify({ status: 'locked' }) });
    await loadRound(state.round.id);
    toast('已锁定');
  } catch (e) { toast('锁定失败：' + e.message, true); }
}

async function addMember() {
  const name = await askText('组员姓名');
  if (!name) return;
  const phone = (await askText('手机号（可留空）')) || '';
  try {
    const m = await api('/api/members', { method: 'POST', body: JSON.stringify({ name, phone }) });
    await loadMembers();
    $('who').value = String(m.id);
    state.me = state.members.find((x) => x.id === m.id) || null;
    localStorage.setItem('me_id', String(m.id));
    toast('已添加：' + m.name);
  } catch (e) { toast('添加失败：' + e.message, true); }
}

function exportXlsx() {
  if (!state.round) return;
  window.location = '/api/rounds/' + state.round.id + '/export';
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll('.viewbtn').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === view);
  });
  renderItems();
}

/* ------------------------------------------------------- 多人同时填：自动同步 */

/** 别人刚填的、采购刚改的状态，得自己"漂"过来。
 *
 * 以前全靠各人手动刷新页面 —— 谁不刷就看不到别人填了什么，容易买重；
 * 采购那边也一样，组员陆陆续续提交，不刷新就以为没人填。
 * 20 秒一次足够：这里要的不是"实时"，是"别漏看"。
 */
const AUTO_REFRESH_MS = 20000;

/** 表单里已经堆了内容？有就别刷新，免得把正在写的东西冲掉。 */
function formDirty() {
  if (state.editing) return true;
  const ids = ['f_name', 'f_spec', 'f_qty', 'f_unit', 'f_price',
               'f_url', 'f_note', 'f_paid', 'f_payer', 'f_purpose'];
  return ids.some((id) => ($(id).value || '').trim() !== '');
}

function anyPopupOpen() {
  return ['askBar', 'pickBar', 'assignBar'].some((id) => !$(id).hidden);
}

async function autoRefresh() {
  if (document.visibilityState === 'hidden') return;      // 页面在后台，先歇着
  if (state.busy || anyPopupOpen()) return;               // 有请求在路上 / 弹窗开着
  if (formDirty()) return;                                // 有人在写，别动页面
  if (!$('loginGate').hidden || !$('memberGate').hidden || !$('nameGate').hidden) return;
  if (!state.round) return;
  const active = document.activeElement;
  // 光标正停在某个输入/下拉上：这一轮先跳过，免得重绘把焦点弄丢
  if (active && ['INPUT', 'SELECT', 'TEXTAREA'].includes(active.tagName)) return;
  try {
    await loadRound(state.round.id);
  } catch (e) { /* 网络抖一下就算了，别弹错误打扰人 */ }
}

function startAutoRefresh() {
  clearInterval(startAutoRefresh._t);
  startAutoRefresh._t = setInterval(autoRefresh, AUTO_REFRESH_MS);
  // 从后台切回来时立刻对一次：这时的数据多半已经旧了
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') autoRefresh();
  });
}

/* ---------------------------------------------------------------- 采购入口：登录门 */

function showLoginGate() {
  const setting = !state.hasPassword;
  $('loginGate').hidden = false;
  $('loginTitle').textContent = setting ? '给采购入口设个密码' : '采购人员登录';
  $('loginHint').textContent = setting
    ? '第一次进来，先设一个密码。只有负责采购的同学需要知道它。'
    : '组员请用不带 /purchase 的那个地址。忘了密码就删掉 data\\settings.json 重设。';
  $('loginPw2').hidden = !setting;
  $('loginBtn').textContent = setting ? '设置并进入' : '进入采购工作台';
  $('loginPw').focus();
}

async function doLogin(e) {
  if (e) e.preventDefault();
  const pw = $('loginPw').value;
  const setting = !state.hasPassword;
  $('loginMsg').textContent = '';
  try {
    if (setting) {
      if (pw !== $('loginPw2').value) throw new Error('两次输入的密码不一样');
      await api('/api/purchase/password', {
        method: 'POST', body: JSON.stringify({ new: pw }),
      });
    } else {
      await api('/api/purchase/login', {
        method: 'POST', body: JSON.stringify({ password: pw }),
      });
    }
    $('loginPw').value = '';
    $('loginPw2').value = '';
    $('loginGate').hidden = true;
    await loadAllPurchase();
    // 登录门过了接着认名字 —— 跟直接进来（已登录）那条路一个规矩
    ensureMyName();
    toast(setting ? '密码设好了，以后进这个地址都要输它' : '欢迎回来');
  } catch (err) {
    $('loginMsg').textContent = err.message;
    $('loginMsg').className = 'msg error';
  }
}

async function doLogout() {
  try { await api('/api/purchase/logout', { method: 'POST' }); } catch (e) { /* 无所谓 */ }
  location.reload();
}

/** 采购人员入口：**先看登录状态**，没登录就不去拉采购数据（拉了也是 401）。 */
async function startPurchase() {
  const st = await api('/api/purchase/status');
  state.hasPassword = st.has_password;
  if (!st.logged_in) { showLoginGate(); return; }
  await loadAllPurchase();
  // 登录只说明「你能管全组」，不说明「你是谁」——
  // 采购自己上手买的条目、垫的钱同样要挂在具体人名下，所以这儿也要认名字。
  ensureMyName();
}

async function loadAllPurchase() {
  await loadMeta();
  await loadMembers();
  await loadMailPresets();
  await loadMailConfig();
  await loadRounds();
  await loadPending();
  await loadShare();
  await loadMemberPasswordState();
}

/* ---------------------------------------------------------------- 组员口令（采购人员设） */

async function loadMemberPasswordState() {
  try {
    const st = await api('/api/member/status');
    $('mp_hint').textContent = st.has_password
      ? '已设过 —— 留空保存可以关掉组员入口'
      : '还没设过，组员入口现在是关着的';
  } catch (e) {
    $('mp_hint').textContent = '';
  }
}

async function saveMemberPassword() {
  $('mp_msg').textContent = '';
  try {
    const r = await api('/api/purchase/member-password', {
      method: 'POST',
      body: JSON.stringify({ password: $('mp_password').value }),
    });
    $('mp_password').value = '';
    $('mp_msg').textContent = r.has_password
      ? '口令已保存，告诉组员就行（改过之后他们得重新输一次）'
      : '已关掉组员入口';
    $('mp_msg').className = 'msg';
    await loadMemberPasswordState();
  } catch (e) {
    $('mp_msg').textContent = '保存失败：' + e.message;
    $('mp_msg').className = 'msg error';
  }
}

/* ---------------------------------------------------------------- 组员入口 */

/** 名字门：**不选名字不让用**。
 *
 * 以前只在点「保存条目」时提示一句「先在右上角选一个名字」——
 * 可那时人已经填完一整条了；而且不选名字也能在页面里随便点。
 * 采购要挂在具体人名下，不选就等于没主。
 */
function showNameGate() {
  const sel = $('nameGateSel');
  sel.innerHTML = '';
  (state.members || []).forEach((m) => {
    const o = document.createElement('option');
    o.value = String(m.id);
    o.textContent = m.name + (m.is_owner ? '（负责人）' : '');
    sel.appendChild(o);
  });
  // 两个入口都认名字，但说法不一样：组员是「你要买的东西记谁名下」，
  // 采购是「你自己上手买的那几条、垫的钱记谁名下」。
  $('nameGateTitle').textContent = IS_PURCHASE ? '你是哪位？' : '你是谁？';
  $('nameGateHint').textContent = IS_PURCHASE
    ? '采购也要认名字 —— 你自己上手买的那几条、垫的钱都挂在这个名字下，'
      + '导出和垫资统计才分得清。选一次就记住了。'
    : '选一个名字才能开始 —— 每条采购都要记在具体人名下，'
      + '不选的话那笔账不知道是谁的。选一次就记住了。';
  $('nameGate').hidden = false;
}

/** 认名字：认过就过，没认过就挡住 —— **两个入口共用这一条路**。
 *
 * 采购入口以前不挡（只在点「保存条目」时提示一句），结果是采购人员
 * 一进来就能下单、改状态、跑导出，自己填的条目却挂在「没选名字」上。
 */
function ensureMyName() {
  if (restoreMyName()) return true;
  showNameGate();
  return false;
}

/** 认下了：记进 state 和 localStorage，下次打开自动认，不用再选。 */
async function pickMyName() {
  const id = Number($('nameGateSel').value);
  const m = (state.members || []).find((x) => Number(x.id) === id);
  if (!m) {
    toast((state.members || []).length
      ? '请选一个名字'
      : (IS_PURCHASE ? '名单里还没有人 —— 点下面的按钮先去加一个'
                     : '名单里还没有人，让采购同学先加一下'), true);
    return;
  }
  state.me = m;
  rememberMeId(id);
  const pick = $('who');
  if (pick) pick.value = String(id);
  $('nameGate').hidden = true;
  // 先把「我是谁」告诉服务端（条目归属、条目列表都看它），
  // 再按入口重新拉一遍 —— 采购连行块一起重算，组员只拉自己那部分。
  await syncMeToServer();
  await (IS_PURCHASE ? loadAllPurchase() : loadAllMember());
}

/** 试着用上次记住的名字。记住的那个已经不在了（退队了）就返回 false。 */
function restoreMyName() {
  const saved = savedMeId();
  if (!saved) return false;
  const m = (state.members || []).find((x) => String(x.id) === saved);
  if (!m) return false;
  state.me = m;
  const pick = $('who');
  if (pick) pick.value = String(m.id);
  return true;
}

/** 组员入口：**先过口令门**；没解锁就不去调别的接口（调了也是 401）。 */
async function startMember() {
  applyMemberWording();
  const st = await api('/api/member/status');
  state.memberHasPassword = st.has_password;
  if (!st.unlocked) { showMemberGate(); return; }
  await loadAllMember();
  // 口令门过了还要认名字 —— 不选名字不让用（申购人空着的采购等于没主）
  ensureMyName();
}

/** 解锁之后真正要加载的东西。**不碰任何采购专用接口。** */
async function loadAllMember() {
  await loadMeta();
  await loadMembers();
  await syncMeToServer();
  await loadRounds();
}

/** 组员看到的说法和采购不一样，别让他以为自己在管全组。 */
function applyMemberWording() {
  $('formTitle').textContent = '我要买什么';
  document.title = '采购申请 · 电气组';
}

/** 把「我是谁」告诉服务端（写 cookie）—— 后面判断条目归属要用。 */
async function syncMeToServer() {
  const id = state.me ? state.me.id : null;
  try {
    await api('/api/me', { method: 'POST', body: JSON.stringify({ member_id: id }) });
  } catch (e) { /* 没选名字也不该拦着人用 */ }
}

function prog(label, value) {
  return '<div class="prog"><b>' + value + '</b><span>' + label + '</span></div>';
}

/** 组员的「本轮进度」：只有数字，没有别人的条目。 */
async function refreshMemberProgress() {
  const box = $('memberProgress');
  if (!state.round) {
    box.innerHTML = '<span class="muted">还没有采购轮次，等负责人建一个。</span>';
    return;
  }
  try {
    const p = await api('/api/rounds/' + state.round.id + '/progress');
    // 分母用「不退货的条目数」；发票那一格写的是**张数** ——
    // 一张票可能覆盖好几条采购（淘宝一个订单买几样），
    // 拿条目数当张数会让人以为"16 张发票"（用户真这么以为过）。
    const liveTotal = p.live_total || p.invoiced_total || p.total;
    box.innerHTML = [
      prog('本轮条目', p.total + ' 条'),
      prog('已下单', p.ordered + ' / ' + liveTotal),
      prog('已收到发票', (p.invoice_sheets || 0) + ' 张'),
      prog('有票的采购', p.invoiced + ' / ' + liveTotal),
    ].join('');
  } catch (e) {
    box.innerHTML = '<span class="muted">取进度失败：' + e.message + '</span>';
  }
}

/* ---------------------------------------------------------------- 组员入口：口令门 */

function showMemberGate() {
  const gate = $('memberGate');
  gate.hidden = false;
  $('memberHint').textContent = state.memberHasPassword
    ? '找负责采购的同学要口令。输一次就记住了，不用天天填。'
    : '还没设过组员口令 —— 请负责采购的同学先进采购入口设一个。'
      + '（没设口令时组员入口是关着的，这样别人拿到网址也进不来。）';
  $('memberPw').focus();
}

async function doMemberLogin(e) {
  if (e) e.preventDefault();
  $('memberMsg').textContent = '';
  try {
    await api('/api/member/login', {
      method: 'POST',
      body: JSON.stringify({ password: $('memberPw').value }),
    });
    $('memberPw').value = '';
    $('memberGate').hidden = true;
    await loadAllMember();
    toast('进来了，把你这个月要买的填上就行');
  } catch (err) {
    $('memberMsg').textContent = err.message;
    $('memberMsg').className = 'msg error';
  }
}

/** 把长链接缩成看得懂的短标签，完整地址放悬浮提示。
 *
 * 淘宝「分享 → 复制链接」出来的地址能有 100 多字符，直接铺在表格里
 * 会把整张表撑宽几倍，手机上更是没法看。
 */
function shortUrl(raw) {
  const text = String(raw || '').trim();
  // 没填链接、只填了商家名（比如「速迫」）时，原样显示
  if (!/^https?:\/\//i.test(text)) return text || '—';
  try {
    const u = new URL(text);
    const host = u.hostname.replace(/^www\./, '');
    if (/taobao\.com$/.test(host) && u.pathname.indexOf('item') >= 0) return '淘宝商品';
    if (u.hostname === 'e.tb.cn') return '淘宝链接';
    return host;
  } catch (e) {
    return '打开链接';
  }
}

/* ------------------------------------------------------------ 一票多单核对 */

/** 弹核对框：每行明细挑一条采购、金额可改，底部实时比对票面合计。
 *
 * 预选是后端算的（名称优先、金额辅助）。**必须让人过一眼** ——
 * 实测单靠算法会错：29 块的票离「折叠椅 28.74」只差 0.26，就被配过去了。
 */
function showAssignBox(result) {
  return new Promise((resolve) => {
    const lines = result.lines || [];
    const suggest = result.suggest || [];
    const ticketTotal = lines.reduce((a, b) => a + (Number(b.total) || 0), 0);
    // 候选：还没票的条目 + 当前这条（跟后端筛法一致）
    const cand = (state.items || []).filter(
      (it) => it.id === result.item.id || !invoicesOf(it.id).length);

    const body = $('assignBody');
    body.innerHTML = '';
    $('assignMsg').textContent = '这张票有 ' + lines.length
      + ' 行。预选是按名称和金额猜的，请核对。';

    const rows = [];
    lines.forEach((L, i) => {
      const tr = document.createElement('tr');

      const td1 = document.createElement('td');
      td1.textContent = L.name || ('第 ' + (i + 1) + ' 行');
      td1.title = L.name || '';
      tr.appendChild(td1);

      const td2 = document.createElement('td');
      const sel = document.createElement('select');
      const none = document.createElement('option');
      none.value = '';
      none.textContent = '（不挂）';
      sel.appendChild(none);
      cand.forEach((it) => {
        const o = document.createElement('option');
        o.value = String(it.id);
        o.textContent = '#' + (state.items.indexOf(it) + 1) + ' '
          + (it.name || '') + '  ¥' + fmt(it.total);
        sel.appendChild(o);
      });
      const pre = suggest[i] && suggest[i].item_id;
      sel.value = pre ? String(pre) : '';
      td2.appendChild(sel);
      tr.appendChild(td2);

      const td3 = document.createElement('td');
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = '0.01';
      inp.value = fmt(L.total);
      td3.appendChild(inp);
      tr.appendChild(td3);

      rows.push({ sel, inp });
      body.appendChild(tr);
    });

    const update = () => {
      const sum = rows.reduce((a, r) => a + (Number(r.inp.value) || 0), 0);
      const diff = Math.round((sum - ticketTotal) * 100) / 100;
      const box = $('assignSum');
      box.className = Math.abs(diff) < 0.01 ? 'good' : 'bad';
      box.textContent = '合计 ¥' + fmt(sum) + '　票面 ¥' + fmt(ticketTotal)
        + (Math.abs(diff) < 0.01 ? '　✓ 对得上' : '　✗ 差 ' + fmt(diff));
    };
    rows.forEach((r) => { r.inp.oninput = update; r.sel.onchange = update; });
    update();

    const finish = (v) => {
      $('assignBar').hidden = true;
      $('assignOk').onclick = null;
      $('assignCancel').onclick = null;
      resolve(v);
    };
    $('assignOk').onclick = () => finish(rows.map((r) => {
      const raw = r.inp.value.trim();
      const n = Number(raw);
      return {
        item_id: r.sel.value ? Number(r.sel.value) : null,
        // 空着就表示"还不知道"，别写成 0 —— 0 看着像"这单没花钱"，
        // 之后在对账里也没法区分"填了 0"和"没填"。
        paid: (raw === '' || !Number.isFinite(n)) ? null : n,
      };
    }).filter((x) => x.item_id));
    $('assignCancel').onclick = () => finish(null);
    $('assignBar').hidden = false;
  });
}

/* ---------------------------------------------------------------- 页面内输入 */

/** 问一句话并返回 Promise<string|null>，取消返回 null。
 *
 * 不用 window.prompt：**部分内嵌浏览器（微信、某些 App 的内置浏览器）不弹它**，
 * 表现就是"点了按钮毫无反应"。真遇到过，所以关键输入一律用这个。
 */
function askText(msg, fallback) {
  return new Promise((resolve) => {
    const bar = $('askBar');
    $('askMsg').textContent = msg;
    const inp = $('askInput');
    inp.value = fallback || '';
    bar.hidden = false;
    inp.focus();
    inp.select();

    const finish = (value) => {
      bar.hidden = true;
      $('askOk').onclick = null;
      $('askCancel').onclick = null;
      inp.onkeydown = null;
      resolve(value);
    };
    $('askOk').onclick = () => finish(inp.value.trim());
    $('askCancel').onclick = () => finish(null);
    inp.onkeydown = (e) => {
      if (e.key === 'Enter') finish(inp.value.trim());
      if (e.key === 'Escape') finish(null);
    };
  });
}

/** 弹出多选列表，返回 Promise<number[]|null>。取消返回 null。 */
function pickItems(msg, options, selected) {
  return new Promise((resolve) => {
    const bar = $('pickBar');
    const sel = $('pickList');
    $('pickMsg').textContent = msg;
    sel.innerHTML = '';
    options.forEach((o) => {
      const opt = document.createElement('option');
      opt.value = String(o.id);
      opt.textContent = o.label;
      opt.selected = (selected || []).indexOf(o.id) >= 0;
      sel.appendChild(opt);
    });
    bar.hidden = false;
    const finish = (v) => {
      bar.hidden = true;
      $('pickOk').onclick = null;
      $('pickCancel').onclick = null;
      resolve(v);
    };
    $('pickOk').onclick = () => finish(
      Array.from(sel.selectedOptions).map((o) => Number(o.value)));
    $('pickCancel').onclick = () => finish(null);
  });
}

/** 改挂：把这张发票换到别的采购条目上（可多选）。
 *
 * 以前挂错了只能把发票删掉重来，而删掉之后它**回不到「待归档」**——
 * 等于要去邮箱重新收一遍。现在点两下就换过来了。
 */
async function reassignInvoice(inv) {
  // 组员只能把票挂到**自己填的**条目上（后端也这么卡），所以这里就只列自己的；
  // 采购则能看到本轮全部条目。
  const pool = (state.items || []).filter((it) => IS_PURCHASE || isMine(it));
  if (!pool.length) { toast('你还没有填过条目', true); return; }
  let current = [];
  try {
    current = (await api('/api/invoices/' + inv.id + '/items')).map((x) => x.id);
  } catch (e) { /* 查不到就当没挂过 */ }
  const picked = await pickItems(
    '把「' + inv.stored_name + '」挂到哪些条目上？',
    pool.map((it) => ({
      id: it.id,
      label: '#' + (state.items.indexOf(it) + 1) + '  ' + (it.name || '')
             + '   ¥' + fmt(it.total) + '   ' + (it.applicant || ''),
    })),
    current);
  if (picked === null) return;
  try {
    await api('/api/invoices/' + inv.id + '/items', {
      method: 'PUT', body: JSON.stringify({ item_ids: picked }),
    });
    await loadRound(state.round.id);
    toast(picked.length ? ('已改挂到 ' + picked.length + ' 条') : '已摘下来');
  } catch (e) {
    toast('改挂失败：' + e.message, true);
  }
}

/* ---------------------------------------------------------------- 组员管理 */

function memberRow(m) {
  const tr = document.createElement('tr');
  if (!m) tr.className = 'newrow';

  ['name', 'phone'].forEach((f) => {
    const td = document.createElement('td');
    const inp = document.createElement('input');
    inp.value = m ? (m[f] || '') : '';
    inp.placeholder = f === 'name' ? '姓名' : '手机号（可空）';
    inp.dataset.field = f;
    td.appendChild(inp);          // 宽度交给 CSS，别在这里写死 —— 写死了会把手机上
    tr.appendChild(td);           // 的表格撑爆，「存/删」就被挤出屏幕点不到了
  });

  const td3 = document.createElement('td');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = !!(m && m.is_owner);
  cb.dataset.field = 'is_owner';
  cb.title = m ? '勾上/取消立刻生效，不用再点「存」' : '';
  // 「负责人」这种开关**勾了就立刻存** —— 不该还等人去点「存」，
  // 忘了点就会以为"取消了没用"（用户真这么报过）。
  if (m) cb.onchange = () => saveOwnerFlag(m, cb);
  td3.appendChild(cb);
  tr.appendChild(td3);

  const td4 = document.createElement('td');
  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'mini';
  save.textContent = '存';
  save.onclick = () => saveMemberRow(tr, m ? m.id : null);
  td4.appendChild(save);
  if (m) {
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'mini danger';
    del.textContent = '删';
    del.onclick = () => removeMemberRow(m);
    td4.appendChild(del);
  }
  tr.appendChild(td4);
  return tr;
}

function renderMembers() {
  const tb = $('memBody');
  if (!tb) return;
  tb.innerHTML = '';
  $('memHint').textContent = '共 ' + state.members.length + ' 人';
  state.members.forEach((m) => tb.appendChild(memberRow(m)));
}

function rowFields(tr) {
  const out = {};
  tr.querySelectorAll('input').forEach((el) => {
    if (el.dataset.field === 'is_owner') out.is_owner = el.checked;
    else if (el.dataset.field) out[el.dataset.field] = el.value;
  });
  return out;
}

async function saveMemberRow(tr, id) {
  const body = rowFields(tr);
  if (!String(body.name || '').trim()) { toast('姓名不能空', true); return; }
  try {
    if (id) {
      await api('/api/members/' + id, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      await api('/api/members', { method: 'POST', body: JSON.stringify(body) });
    }
    await loadMembers();
    toast('已保存');
    // 从名字门里出来加自己的（那会儿还没认出你是谁）：加好了就再认一次名字
    if (!state.me) ensureMyName();
  } catch (e) {
    toast('保存失败：' + e.message, true);
  }
}

/** 「采购负责人」那个勾选框：勾上/取消**立刻存**，不等人点「存」。

 * 只提交 is_owner —— **不带上这一行里还没保存的姓名/手机号**，
 * 那两栏有自己的「存」按钮，别顺手替人做决定。
 *
 * 为什么要有这个：用户报过"负责人老是取消不了"。除了名单文件会把标记补回来
 * （见 `roster.import_roster`），还有一个可能就是**改完忘了点「存」**——
 * 刷新一看当然还是老样子。开关类的字段，改了就该当场生效。
 */
async function saveOwnerFlag(m, cb) {
  try {
    await api('/api/members/' + m.id, {
      method: 'PUT',
      body: JSON.stringify({ name: m.name, phone: m.phone || '',
                             is_owner: cb.checked }),
    });
    toast(m.name + (cb.checked ? ' 已设为采购负责人' : ' 已取消负责人'));
    await loadMembers();
  } catch (e) {
    cb.checked = !cb.checked;          // 没存上就把界面拨回去，别骗人
    toast('改不了：' + e.message, true);
  }
}

async function removeMemberRow(m) {
  if (!confirm('确定删掉「' + m.name + '」？\n\n'
    + '他填过的采购条目不会消失，只是以后不能再以这个名字填了。')) return;
  try {
    await api('/api/members/' + m.id, { method: 'DELETE' });
    await loadMembers();
    toast('已删除');
  } catch (e) {
    toast('删除失败：' + e.message, true);
  }
}

/** 点「+ 加组员」在表格末尾插一个空行 —— 不用浏览器弹窗（有些环境不弹）。 */
function addMemberRow() {
  const tb = $('memBody');
  const pending = tb.querySelector('.newrow');
  if (pending) {
    // 说得具体点：原来只说「先把上面那行填完」，人会以为打完字就算填完了，
    // 其实**得点那一行的「存」**。手机窄屏下「存」还可能被挤出屏幕。
    toast('上面那行还没存 —— 点它右边的「存」', true);
    pending.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return;
  }
  const tr = memberRow(null);
  tb.appendChild(tr);
  const first = tr.querySelector('input');
  if (first) first.focus();
}

/** 名字门里的兜底出口（按钮只对采购显示）。
 *
 * 名单空了、或者自己的名字打错了，名字门就成了"管理入口把管理员锁在门外"——
 * 所以给他一条路：先放进去把自己加上，加完再认名字。
 */
function openMemberAdmin() {
  $('nameGate').hidden = true;
  addMemberRow();
  const card = $('memberCard');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  toast('把自己加进去，点那一行的「存」；认不出你是谁之前会再问一次');
}

/* ---------------------------------------------------------------- 启动 */

function bind() {
  $('itemForm').addEventListener('submit', submitForm);
  $('nameGateOk').addEventListener('click', pickMyName);
  $('nameGateAdd').addEventListener('click', openMemberAdmin);
  $('resetForm').addEventListener('click', resetForm);
  $('parseShare').addEventListener('click', parseShare);
  $('newRound').addEventListener('click', newRound);
  $('lockRound').addEventListener('click', lockRound);
  $('addMember').addEventListener('click', addMemberRow);
  $('memAdd').addEventListener('click', addMemberRow);
  $('exportXlsx').addEventListener('click', exportXlsx);
  $('mergePdf').addEventListener('click', mergePdf);
  $('invoicesZip').addEventListener('click', invoiceZip);
  ['mergePerPage', 'mergeBack', 'mergeFrame'].forEach((id) => {
    $(id).addEventListener('change', renderMerge);
  });
  $('blockCopy').addEventListener('click', blockCopy);
  $('blockXlsx').addEventListener('click', blockXlsx);
  $('mailSave').addEventListener('click', saveMailConfig);
  $('mailTest').addEventListener('click', mailTest);
  $('mailFetch').addEventListener('click', mailFetch);
  $('shareRefresh').addEventListener('click', loadShare);
  $('shareCopy').addEventListener('click', copyShareUrl);
  $('loginForm').addEventListener('submit', doLogin);
  $('logout').addEventListener('click', doLogout);
  $('memberForm').addEventListener('submit', doMemberLogin);
  $('mp_save').addEventListener('click', saveMemberPassword);
  $('onlyMine').addEventListener('change', renderItems);
  // 搜索框：边打边筛（本轮条目就几十条，不用防抖）；Esc 清空
  $('itemSearch').addEventListener('input', (e) => {
    state.query = e.target.value;
    renderItems();
  });
  $('itemSearch').addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.target.value = '';
      state.query = '';
      renderItems();
    }
  });
  // 进度条里「N 条有发票但没填实付金额」点一下 → 只看这些行
  $('progressBar').addEventListener('click', (e) => {
    const v = e.target.dataset ? e.target.dataset.view : '';
    if (v) switchView(v);
  });
  $('invoiceInput').addEventListener('change', (e) => uploadInvoiceFile(e.target.files[0]));
  document.querySelectorAll('.viewbtn').forEach((b) => {
    b.addEventListener('click', () => switchView(b.dataset.view));
  });
  ['f_qty', 'f_price', 'f_freight'].forEach((id) => $(id).addEventListener('input', updateCalc));
  $('who').addEventListener('change', () => {
    state.me = state.members.find((m) => String(m.id) === $('who').value) || null;
    if (state.me) rememberMeId(state.me.id);
    // 换了名字：先把「我是谁」告诉服务端（条目归属、垫资、导出都看它），再重新拉一遍。
    // **两个入口走同一条路** —— 采购换名字之后，他能改哪些、导出的申购人也跟着变。
    syncMeToServer().then(() => {
      if (state.round) loadRound(state.round.id); else renderItems();
    });
  });
  $('roundSelect').addEventListener('change', () => loadRound($('roundSelect').value));
}

(async function start() {
  bind();
  try {
    if (IS_PURCHASE) {
      await startPurchase();
    } else {
      await startMember();
    }
    // 进来之后就开始跟服务器对时 —— 多人同时填的时候，
    // 别人的条目、采购改的状态都靠它自己漂过来
    startAutoRefresh();
  } catch (e) { toast('加载失败：' + e.message, true); }
})();
