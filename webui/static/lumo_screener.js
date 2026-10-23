/* 条件选股页增强(2026-10-23)
 *
 * 1) 高级条件折叠:默认收起,状态记 localStorage;形态标签(chips)常驻页面顶部。
 * 2) 形态标签:18 个预设(连续爆量/放量突破MA20/超跌反弹/…)多选取交集,参数留空走服务端默认。
 * 3) 结果列表:扩展列(MA20偏离/5·20·60日涨幅/量能倍数/单日量倍/量能环比/峰倍比/换手/5日均额/
 *    20日涨停/距线/触线)+ 本地排序 + 代码名称搜索 + 分页 + 导出 CSV + 双击进个股分析。
 *
 * 该脚本在 lumo_desktop_app.js 之后同步加载:压缩包内已有的选股 IIFE 会先绑定事件,
 * 这里用 cloneNode 换掉三个节点接管交互,避免一次点击触发两次请求。
 */
(function () {
  'use strict';
  if (document.body.dataset.page !== 'screener') return;

  var $ = function (id) { return document.getElementById(id); };
  if (!$('scrRunBtn') || typeof window.fetchJson !== 'function') return;

  var ADV_KEY = 'lumo.screener.advOpen';
  var state = { payload: null, page: 1, pageSize: 50, sortKey: '', sortDir: -1, query: '' };
  var PRESET_TEXT = {};

  // ---------- 接管旧事件(避免双绑定) ----------
  ['scrRunBtn', 'scrResetBtn', 'scrResultTable'].forEach(function (id) {
    var el = $(id);
    if (el && el.parentNode) el.parentNode.replaceChild(el.cloneNode(true), el);
  });

  // ---------- 小工具 ----------
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function nv(id) {
    var el = $(id);
    if (!el) return null;
    var raw = el.value;
    if (raw === '' || raw === null) return null;
    var n = Number(raw);
    return isNaN(n) ? null : n;
  }
  function checked(id) { var el = $(id); return !!(el && el.checked); }
  function selVal(id) { var el = $(id); return el ? el.value : ''; }
  function putNum(obj, key, id) { var v = nv(id); if (v !== null) obj[key] = v; }
  function isNum(v) { return typeof v === 'number' && !isNaN(v); }
  function toast(msg) { if (typeof window.showToast === 'function') window.showToast(msg); }

  function fmtNum(v, d) {
    return isNum(v) ? Number(v).toFixed(d == null ? 2 : d) : '--';
  }
  function fmtPct(v) {
    if (!isNum(v)) return '--';
    return '<span class="' + (v >= 0 ? 'up' : 'down') + '">' +
      (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%</span>';
  }
  function fmtX(v) { return isNum(v) ? Number(v).toFixed(2) + 'x' : '--'; }
  function fmtInt(v) { return isNum(v) ? String(Math.round(v)) : '--'; }
  function fmtMoney(v) {
    if (!isNum(v)) return '--';
    return Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(2) + '亿' : Math.round(v) + '万';
  }

  // ---------- 折叠 ----------
  function setAdv(open) {
    var adv = $('scrAdv');
    var btn = $('scrToggleAdv');
    if (!adv || !btn) return;
    adv.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.textContent = open ? '收起高级条件 ▴' : '展开高级条件 ▾';
    try { localStorage.setItem(ADV_KEY, open ? '1' : '0'); } catch (err) { /* 隐私模式忽略 */ }
  }
  function initAdv() {
    var open = false;
    try { open = localStorage.getItem(ADV_KEY) === '1'; } catch (err) { open = false; }
    setAdv(open);
    var btn = $('scrToggleAdv');
    if (btn) btn.addEventListener('click', function () { setAdv(!!$('scrAdv') && $('scrAdv').hidden); });
  }

  // ---------- 形态标签 ----------
  function initChips() {
    var chips = document.querySelectorAll('.scr-chip');
    Array.prototype.forEach.call(chips, function (chip) {
      PRESET_TEXT[chip.dataset.preset] = chip.textContent.trim();
      chip.addEventListener('click', function () {
        chip.classList.toggle('active');
        updateAppliedHint();
      });
    });
    var clear = $('scrPresetClear');
    if (clear) clear.addEventListener('click', function () {
      Array.prototype.forEach.call(document.querySelectorAll('.scr-chip.active'), function (c) {
        c.classList.remove('active');
      });
      updateAppliedHint();
    });
  }
  function activeLabels() {
    return Array.prototype.slice.call(document.querySelectorAll('.scr-chip.active'))
      .map(function (c) { return c.dataset.preset; });
  }
  function updateAppliedHint() {
    var box = $('scrApplied');
    if (!box) return;
    var labels = activeLabels().map(function (k) { return PRESET_TEXT[k] || k; });
    box.textContent = labels.length ? '形态标签: ' + labels.join(' + ') : '';
  }

  // ---------- 条件组装 ----------
  function buildPayload() {
    var cond = {};
    var mkt = {};
    putNum(mkt, 'pct_min', 'scrPctMin');
    putNum(mkt, 'pct_max', 'scrPctMax');
    putNum(mkt, 'price_min', 'scrPriceMin');
    putNum(mkt, 'price_max', 'scrPriceMax');
    putNum(mkt, 'turnover_min', 'scrTurnoverMin');
    if (checked('scrExcludeST')) mkt.exclude_st = true;
    if (checked('scrExcludeLimitUp')) mkt.exclude_limit_up = true;
    var boards = Array.prototype.slice.call(document.querySelectorAll('.scr-board:checked'))
      .map(function (el) { return el.value; });
    if (boards.length) mkt.boards = boards;
    if (Object.keys(mkt).length) cond.market = mkt;

    var flow = {};
    putNum(flow, 'main_net_min', 'scrMainNetMin');
    putNum(flow, 'net_rate_min', 'scrNetRateMin');
    putNum(flow, 'streak_days', 'scrStreakDays');
    if (Object.keys(flow).length) cond.flow = flow;

    var radar = {};
    putNum(radar, 'activity_min', 'scrActivityMin');
    putNum(radar, 'changes_min', 'scrChangesMin');
    var direction = selVal('scrDirection');
    if (direction) radar.direction = direction;
    if (Object.keys(radar).length) cond.radar = radar;

    var accumOn = selVal('scrAccumEnable');
    var accumScore = nv('scrAccumScoreMin');
    if (accumOn || accumScore !== null) {
      cond.accum = { window: Number(selVal('scrAccumWindow') || 40) };
      if (accumOn) cond.accum.qualified_only = true;
      if (accumScore !== null) cond.accum.score_min = accumScore;
    }

    var dragonDays = selVal('scrDragonDays');
    var dragonNet = nv('scrDragonNetMin');
    var dragonSeat = checked('scrDragonQuantSeat');
    if (dragonDays || dragonNet !== null || dragonSeat) {
      cond.dragon = { days: Number(dragonDays || 5) };
      if (dragonNet !== null) cond.dragon.net_min_wan = dragonNet;
      if (dragonSeat) cond.dragon.quant_seat = true;
    }

    var oppScore = nv('scrOppScoreMin');
    if (oppScore !== null) cond.opportunity = { score_min: oppScore };

    var tech = {};
    if (checked('scrTechMa20')) tech.above_ma20 = true;
    if (checked('scrTechMaBull')) tech.ma_bull = true;
    if (checked('scrTechNewHigh')) tech.new_high_20 = true;
    if (checked('scrTechMacdGolden')) tech.macd_golden = true;
    if (checked('scrTechKdjGolden')) tech.kdj_golden = true;
    putNum(tech, 'rsi_min', 'scrTechRsiMin');
    putNum(tech, 'rsi_max', 'scrTechRsiMax');
    putNum(tech, 'vol_ratio_min', 'scrTechVolRatio');
    if (Object.keys(tech).length) cond.tech = tech;

    var quant = {};
    putNum(quant, 'buy_min', 'scrQuantBuyMin');
    putNum(quant, 'sell_max', 'scrQuantSellMax');
    if (Object.keys(quant).length) cond.quant = quant;

    var labels = activeLabels();
    if (labels.length) {
      var preset = { labels: labels };
      putNum(preset, 'vol_mult', 'scrVolMult');
      putNum(preset, 'ret5_min', 'scrRet5Min');
      putNum(preset, 'amount_min', 'scrAmountMin');
      putNum(preset, 'spike_mult', 'scrSpikeMult');
      putNum(preset, 'dod_mult', 'scrDodMult');
      putNum(preset, 'shrink_ratio', 'scrShrinkRatio');
      var hn = nv('scrHighN');
      if (hn !== null) { preset.high_n = hn; preset.low_n = hn; }
      cond.preset = preset;
    }

    cond.sort = selVal('scrSort') || 'main_net_wan';
    cond.limit = 0;  // 全量返回,前端分页
    return cond;
  }

  // ---------- 列定义 ----------
  var COLUMNS = [
    { key: 'code', label: '代码', cls: 'scr-col-code' },
    { key: 'name', label: '名称', cls: 'scr-col-name' },
    { key: 'price', label: '现价', fmt: function (v) { return fmtNum(v, 2); }, num: true },
    { key: 'change_pct', label: '今日%', fmt: fmtPct, num: true },
    { key: 'ma20_dev', label: 'MA20偏离%', fmt: fmtPct, num: true, sort: true },
    { key: 'ret5', label: '5日%', fmt: fmtPct, num: true, sort: true },
    { key: 'ret20', label: '20日%', fmt: fmtPct, num: true, sort: true },
    { key: 'ret60', label: '60日%', fmt: fmtPct, num: true, sort: true },
    { key: 'vol_energy', label: '量能倍数', fmt: fmtX, num: true, sort: true },
    { key: 'vol_ratio', label: '单日量倍', fmt: fmtX, num: true, sort: true },
    { key: 'vol_step', label: '量能环比', fmt: fmtX, num: true, sort: true },
    { key: 'vol_peak_ratio', label: '峰倍比', fmt: fmtX, num: true },
    { key: 'turnover', label: '换手%', fmt: function (v) { return fmtNum(v, 1); }, num: true, sort: true },
    { key: 'amount_ma5_yi', label: '5日均额(亿)', fmt: function (v) { return fmtNum(v, 2); }, num: true, sort: true },
    { key: 'limit_up_20', label: '20日涨停', fmt: fmtInt, num: true, sort: true },
    { key: 'dist_pct', label: '距线%', fmt: fmtPct, num: true },
    { key: 'touch_pct', label: '触线%', fmt: fmtPct, num: true },
    { key: 'main_net_wan', label: '主力净流入', fmt: fmtMoney, num: true, sort: true },
    { key: 'hits', label: '命中条件', cls: 'screener-hits' },
    { key: '__ops', label: '操作', cls: 'capital-actions-col' }
  ];

  function cellHtml(col, row) {
    if (col.key === '__ops') {
      return '<span class="row-cell-actions">' +
        '<button class="button secondary compact" type="button" data-scr-add="' + esc(row.code) +
        '" data-scr-name="' + esc(row.name || '') + '">加自选</button>' +
        '<button class="button compact" type="button" data-scr-ana="' + esc(row.code) +
        '" data-scr-name="' + esc(row.name || '') + '">个股分析</button></span>';
    }
    if (col.key === 'hits') {
      var tags = (row.hits || []).map(function (h) {
        return '<span class="quant-type-tag">' + esc(h) + '</span>';
      }).join('');
      return tags || '--';
    }
    if (col.key === 'code') {
      var extra = row.line_ref ? '<small>·' + esc(row.line_ref) + '</small>' : '';
      return '<b>' + esc(row.code) + '</b>' + extra;
    }
    var raw = row[col.key];
    return col.fmt ? col.fmt(raw) : esc(raw == null || raw === '' ? '--' : raw);
  }

  function rowsForView() {
    var rows = ((state.payload || {}).stocks || []).slice();
    var q = state.query.trim().toLowerCase();
    if (q) {
      rows = rows.filter(function (r) {
        return String(r.code || '').toLowerCase().indexOf(q) >= 0 ||
          String(r.name || '').toLowerCase().indexOf(q) >= 0;
      });
    }
    if (state.sortKey) {
      var key = state.sortKey;
      var dir = state.sortDir;
      rows.sort(function (a, b) {
        var va = a[key];
        var vb = b[key];
        var na = isNum(va);
        var nb = isNum(vb);
        if (!na && !nb) return 0;
        if (!na) return 1;   // 缺值恒定沉底
        if (!nb) return -1;
        return (va - vb) * dir;
      });
    }
    return rows;
  }

  function headHtml() {
    return '<thead><tr><th class="scr-col-idx">#</th>' + COLUMNS.map(function (col) {
      var cls = col.cls || '';
      var attrs = '';
      if (col.sort) {
        cls += ' sortable-col';
        attrs = ' data-scr-sort="' + col.key + '"';
        if (state.sortKey === col.key) {
          cls += ' sorted';
          attrs += ' data-dir="' + state.sortDir + '"';
        }
      }
      if (col.num) cls += ' scr-num';
      var mark = state.sortKey === col.key ? (state.sortDir === -1 ? ' ▼' : ' ▲') : '';
      return '<th class="' + cls + '"' + attrs + '>' + esc(col.label) + mark + '</th>';
    }).join('') + '</tr></thead>';
  }

  function pagerHtml(page, pages, total) {
    if (pages <= 1) return '';
    return '<div class="quant-pager">' +
      '<button class="button secondary compact" type="button" data-scr-page="1"' + (page <= 1 ? ' disabled' : '') + '>首页</button>' +
      '<button class="button secondary compact" type="button" data-scr-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>' +
      '<span class="quant-pager-info">第 ' + page + ' / ' + pages + ' 页 · 共 ' + total + ' 只</span>' +
      '<button class="button secondary compact" type="button" data-scr-page="' + (page + 1) + '"' + (page >= pages ? ' disabled' : '') + '>下一页</button>' +
      '<button class="button secondary compact" type="button" data-scr-page="' + pages + '"' + (page >= pages ? ' disabled' : '') + '>末页</button>' +
      '</div>';
  }

  function render() {
    var wrap = $('scrResultTable');
    var meta = $('scrResultMeta');
    var notes = $('scrNotes');
    var payload = state.payload;
    if (!wrap || !payload) return;

    var rows = rowsForView();
    var pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
    state.page = Math.min(Math.max(1, state.page), pages);
    var start = (state.page - 1) * state.pageSize;
    var slice = rows.slice(start, start + state.pageSize);

    if (meta) {
      var bits = ['基座 ' + (payload.base_count || 0) + ' 只', '命中 ' + (payload.total_matched || 0) + ' 只'];
      if (state.query) bits.push('筛选后 ' + rows.length + ' 只');
      bits.push(String(payload.updated_at || '').replace('T', ' '));
      meta.textContent = bits.join(' · ');
    }
    if (notes) {
      notes.innerHTML = (payload.notes || []).map(function (n) {
        return '<p class="muted screener-note">· ' + esc(n) + '</p>';
      }).join('');
    }

    if (!rows.length) {
      wrap.innerHTML = '<p class="muted">' + (state.query ? '没有匹配「' + esc(state.query) + '」的股票。' : '无命中股票,试着放宽条件或去掉部分形态标签。') + '</p>';
      return;
    }

    var body = slice.map(function (row, i) {
      return '<tr data-scr-row="' + esc(row.code) + '" data-scr-name="' + esc(row.name || '') + '">' +
        '<td class="scr-col-idx">' + (start + i + 1) + '</td>' +
        COLUMNS.map(function (col) {
          return '<td class="' + (col.cls || '') + (col.num ? ' scr-num' : '') + '">' + cellHtml(col, row) + '</td>';
        }).join('') + '</tr>';
    }).join('');

    wrap.innerHTML = '<table class="capital-data-table scr-result-table">' + headHtml() +
      '<tbody>' + body + '</tbody></table>' + pagerHtml(state.page, pages, rows.length) +
      '<p class="muted screener-note">' + esc(payload.disclaimer || '') + '</p>';
  }

  function renderError(msg) {
    var wrap = $('scrResultTable');
    if (wrap) wrap.innerHTML = '<p class="muted">' + esc(msg) + '</p>';
  }

  // ---------- 运行 ----------
  async function run() {
    var btn = $('scrRunBtn');
    if (btn) { btn.disabled = true; btn.textContent = '选股中…'; }
    renderLoading();
    try {
      var payload = await window.fetchJson('/api/screener/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPayload()),
        timeout: 300000
      });
      if (!payload || payload.ok === false) {
        state.payload = null;
        renderError('选股失败: ' + ((payload && payload.error) || '未知错误'));
        return;
      }
      state.payload = payload;
      state.page = 1;
      state.sortKey = '';
      render();
      toast('选股完成:命中 ' + (payload.total_matched || 0) + ' 只');
    } catch (err) {
      renderError('选股失败: ' + (err && err.message ? err.message : err));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '开始选股'; }
    }
  }

  function renderLoading() {
    var wrap = $('scrResultTable');
    if (wrap) {
      wrap.innerHTML = '<p class="muted">正在按条件筛选(形态标签需逐股拉日K与均线,稍慢)…</p>';
    }
  }

  // ---------- 重置 ----------
  var RESET_INPUTS = ['scrPctMin', 'scrPctMax', 'scrPriceMin', 'scrPriceMax', 'scrTurnoverMin',
    'scrMainNetMin', 'scrNetRateMin', 'scrStreakDays', 'scrActivityMin', 'scrChangesMin',
    'scrAccumScoreMin', 'scrDragonNetMin', 'scrOppScoreMin', 'scrTechVolRatio',
    'scrTechRsiMin', 'scrTechRsiMax', 'scrQuantBuyMin', 'scrQuantSellMax', 'scrSearch'];
  var RESET_CHECKS = ['scrExcludeLimitUp', 'scrDragonQuantSeat', 'scrTechMa20', 'scrTechMaBull',
    'scrTechNewHigh', 'scrTechMacdGolden', 'scrTechKdjGolden'];
  var RESET_SELECTS = ['scrDirection', 'scrAccumEnable', 'scrDragonDays'];
  var PRESET_DEFAULTS = {
    scrVolMult: '2', scrRet5Min: '15', scrAmountMin: '2', scrHighN: '20',
    scrSpikeMult: '2', scrDodMult: '1.5', scrShrinkRatio: '0.4'
  };

  function resetAll() {
    RESET_INPUTS.forEach(function (id) { var el = $(id); if (el) el.value = ''; });
    RESET_CHECKS.forEach(function (id) { var el = $(id); if (el) el.checked = false; });
    Array.prototype.forEach.call(document.querySelectorAll('.scr-board'), function (el) { el.checked = false; });
    RESET_SELECTS.forEach(function (id) { var el = $(id); if (el) el.value = ''; });
    Object.keys(PRESET_DEFAULTS).forEach(function (id) {
      var el = $(id);
      if (el) el.value = PRESET_DEFAULTS[id];
    });
    var st = $('scrExcludeST');
    if (st) st.checked = true;
    var sort = $('scrSort');
    if (sort) sort.value = 'main_net_wan';
    Array.prototype.forEach.call(document.querySelectorAll('.scr-chip.active'), function (c) {
      c.classList.remove('active');
    });
    state.query = '';
    state.sortKey = '';
    updateAppliedHint();
  }

  // ---------- 导出 CSV ----------
  function exportCsv() {
    var rows = rowsForView();
    if (!rows.length) { toast('没有可导出的数据'); return; }
    var header = ['代码', '名称', '现价', '今日%', 'MA20偏离%', '5日%', '20日%', '60日%',
      '量能倍数', '单日量倍', '量能环比', '峰倍比', '换手%', '5日均额(亿)', '20日涨停',
      '距线%', '触线%', '主力净流入(万)', '命中条件'];
    var lines = [header.join(',')];
    rows.forEach(function (r) {
      var cells = [r.code, r.name || '', r.price, r.change_pct, r.ma20_dev, r.ret5, r.ret20,
        r.ret60, r.vol_energy, r.vol_ratio, r.vol_step, r.vol_peak_ratio, r.turnover,
        r.amount_ma5_yi, r.limit_up_20, r.dist_pct, r.touch_pct, r.main_net_wan];
      var text = cells.map(function (v) {
        if (v === null || v === undefined || v === '') return '';
        return String(v).replace(/"/g, '""');
      }).map(function (v) { return /[",\n]/.test(v) ? '"' + v + '"' : v; });
      text.push('"' + (r.hits || []).join(' ') + '"');
      lines.push(text.join(','));
    });
    var blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    var d = new Date();
    var stamp = d.getFullYear() + String(d.getMonth() + 1).padStart(2, '0') +
      String(d.getDate()).padStart(2, '0') + '_' + String(d.getHours()).padStart(2, '0') +
      String(d.getMinutes()).padStart(2, '0');
    a.href = url;
    a.download = '选股结果_' + stamp + '.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // ---------- 事件 ----------
  function bind() {
    var runBtn = $('scrRunBtn');
    if (runBtn) runBtn.addEventListener('click', run);
    var resetBtn = $('scrResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', resetAll);
    var exportBtn = $('scrExportCsv');
    if (exportBtn) exportBtn.addEventListener('click', exportCsv);

    var pageSize = $('scrPageSize');
    if (pageSize) pageSize.addEventListener('change', function () {
      state.pageSize = Number(pageSize.value) || 50;
      state.page = 1;
      render();
    });
    var search = $('scrSearch');
    if (search) search.addEventListener('input', function () {
      state.query = search.value || '';
      state.page = 1;
      render();
    });

    var table = $('scrResultTable');
    if (table) {
      table.addEventListener('click', async function (ev) {
        var pageBtn = ev.target.closest ? ev.target.closest('[data-scr-page]') : null;
        if (pageBtn && !pageBtn.disabled) {
          state.page = Number(pageBtn.dataset.scrPage) || 1;
          render();
          return;
        }
        var sortTh = ev.target.closest ? ev.target.closest('[data-scr-sort]') : null;
        if (sortTh) {
          var key = sortTh.dataset.scrSort;
          if (state.sortKey === key) {
            state.sortDir = state.sortDir === -1 ? 1 : -1;
          } else {
            state.sortKey = key;
            state.sortDir = -1;
          }
          state.page = 1;
          render();
          return;
        }
        var addBtn = ev.target.closest ? ev.target.closest('[data-scr-add]') : null;
        if (addBtn) {
          try {
            await window.fetchJson('/api/watchlist/add', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ code: addBtn.dataset.scrAdd, name: addBtn.dataset.scrName || '' })
            });
            toast('已加入自选: ' + addBtn.dataset.scrAdd);
          } catch (err) {
            toast('加自选失败: ' + (err && err.message ? err.message : err));
          }
          return;
        }
        var anaBtn = ev.target.closest ? ev.target.closest('[data-scr-ana]') : null;
        if (anaBtn && typeof window.openStockContext === 'function') {
          window.openStockContext({
            stock_code: anaBtn.dataset.scrAna,
            stock_name: anaBtn.dataset.scrName || ''
          });
        }
      });

      table.addEventListener('dblclick', function (ev) {
        var tr = ev.target.closest ? ev.target.closest('[data-scr-row]') : null;
        if (!tr || !tr.dataset.scrRow) return;
        if (typeof window.openStockContext === 'function') {
          window.openStockContext({ stock_code: tr.dataset.scrRow, stock_name: tr.dataset.scrName || '' });
        }
      });
    }
  }

  initAdv();
  initChips();
  bind();
  updateAppliedHint();
})();
