"""运行选股页真实脚本,验证默认条件、标签作用范围、校验及空结果提示。"""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 运行前端回归")
def test_screener_controls_and_request_contract():
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const template = fs.readFileSync('webui/templates/desktop.html', 'utf8');
const start = template.indexOf("{% if active_page == 'screener' %}");
const markup = template.slice(start, template.indexOf('{% endif %}', start));
const elements = {}, chips = [], bulls = [], boards = [], labels = [], requests = [], timers = [];
function attrs(text) {
  return Object.fromEntries([...text.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
}
class Element {
  constructor(id, properties = {}) {
    this.id = id; this.properties = properties; this.value = properties.value || '';
    this.checked = false; this.disabled = false; this.hidden = false;
    this.textContent = ''; this.innerHTML = ''; this.listeners = {}; this.dataset = {};
    this.classes = new Set((properties.class || '').split(' '));
    this.classList = {contains: k => this.classes.has(k), remove: k => this.classes.delete(k),
      toggle: k => this.classes.has(k) ? this.classes.delete(k) : this.classes.add(k)};
    for (const [key, value] of Object.entries(properties)) {
      if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
    this.parentNode = {replaceChild: replacement => {elements[this.id] = replacement;}};
    this.validationMessage = '';
  }
  cloneNode() {const copy = new Element(this.id, this.properties); copy.value = this.value; return copy;}
  setAttribute(key, value) {this.properties[key] = value;}
  getAttribute(key) {return this.properties[key];}
  addEventListener(type, fn) {(this.listeners[type] ||= []).push(fn);}
  async fire(type = 'click') {for (const fn of this.listeners[type] || []) await fn({target: this});}
  querySelector() {return this.input || null;}
  closest(selector) {return selector === '#scrAdv' && this.properties.type === 'number' && !this.dataset.scrParam ? elements.scrAdv : null;}
  setCustomValidity(message) {this.validationMessage = message;}
  checkValidity() {
    if (this.validationMessage) return false;
    if (this.value === '') return true;
    const n = Number(this.value), p = this.properties;
    const valid = Number.isFinite(n) && (p.min === undefined || n >= Number(p.min)) &&
      (p.max === undefined || n <= Number(p.max)) &&
      (!p.step || Math.abs((n - Number(p.min || 0)) / Number(p.step) - Math.round((n - Number(p.min || 0)) / Number(p.step))) < 1e-6);
    if (!valid) this.validationMessage = '条件参数无效';
    return valid;
  }
  reportValidity() {return this.checkValidity();}
}
for (const match of markup.matchAll(/<(?:input|select|button|div|span)[^>]*\bid="[^"]+"[^>]*>/g)) {
  const p = attrs(match[0]); elements[p.id] = new Element(p.id, p);
  elements[p.id].checked = /\bchecked\b/.test(match[0]);
}
for (const match of markup.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
  const p = attrs(match[1]), options = [...match[2].matchAll(/<option\b([^>]*)>/g)];
  const option = options.find(m => /\bselected\b/.test(m[1])) || options[0];
  if (p.id && option) elements[p.id].value = attrs(option[1]).value || '';
}
for (const match of markup.matchAll(/<button\b([^>]*data-preset="[^"]+"[^>]*)>([^<]*)<\/button>/g)) {
  const p = attrs(match[1]), chip = new Element('', p); chip.textContent = match[2]; chips.push(chip);
}
for (const match of markup.matchAll(/<button\b([^>]*data-bull="[^"]+"[^>]*)>([^<]*)<\/button>/g)) {
  const p = attrs(match[1]), chip = new Element('', p); chip.textContent = match[2]; bulls.push(chip);
}
for (const match of markup.matchAll(/<input\b([^>]*class="scr-board"[^>]*)>/g)) boards.push(new Element('', attrs(match[1])));
for (const match of markup.matchAll(/<label\b([^>]*data-scr-presets="[^"]+"[^>]*)>([\s\S]*?)<\/label>/g)) {
  const label = new Element('', attrs(match[1])); label.input = elements[attrs(match[2]).id]; labels.push(label);
}
const params = () => Object.values(elements).filter(el => el.dataset.scrParam);
const document = {body: {dataset: {page: 'screener'}}, getElementById: id => elements[id] || null,
  querySelectorAll(selector) {
    if (selector === '.scr-chip') return chips;
    if (selector === '.scr-chip.active') return chips.filter(c => c.classList.contains('active'));
    if (selector === '.scr-bull-chip') return bulls;
    if (selector === '.scr-bull-chip.active') return bulls.filter(c => c.classList.contains('active'));
    if (selector === '.scr-board') return boards;
    if (selector === '.scr-board:checked') return boards.filter(c => c.checked);
    if (selector === '[data-scr-presets]') return labels;
    if (selector === '[data-scr-param]') return params();
    if (selector === '.screener-layout input[type="number"]') return Object.values(elements).filter(el => el.properties.type === 'number');
    if (selector.startsWith('#scrAdv input,')) return Object.values(elements);
    return [];
  }};
let response = {ok: true, base_count: 4, total_matched: 0, stocks: [], notes: []};
const window = {async fetchJson(url, options) {
  if (url.endsWith('/meta')) return {dims: {}, presets: {defaults: {spike_mult: 1.8, amount_min: 0}},
    bull: {date: '2026-11-10', counts: {ambush_balanced: 26}, strategies: [
      {key: 'ambush_balanced', summary: '低位未启动', risk: 'mid'},
      {key: 'ambush_aggressive', summary: '波动最高的两成', risk: 'high'}]}};
  assert.equal(url, '/api/screener/run'); requests.push(JSON.parse(options.body)); return response;
}};
elements.scrRunBtn.addEventListener('click', () => {throw new Error('旧事件不应重复执行');});
const context = vm.createContext({document, window, localStorage: {getItem: () => null, setItem() {}},
  setTimeout: fn => timers.push(fn)});
vm.runInContext(fs.readFileSync('webui/static/lumo_screener.js', 'utf8'), context);
const chip = key => chips.find(c => c.dataset.preset === key);
const bull = key => bulls.find(c => c.dataset.bull === key);
const run = () => elements.scrRunBtn.fire();
(async () => {
  assert(params().every(p => p.disabled && p.value === ''), '未选标签的参数不能默认生效');
  await run();
  assert.deepEqual(requests[0], {market: {exclude_st: true}, sort: 'main_net_wan', limit: 2000});
  await chip('new_high_n').fire(); await chip('new_low_n').fire();
  elements.scrHighN.value = '31'; elements.scrSpikeMult.value = '-2';
  await run();
  assert.deepEqual(requests.at(-1).preset, {labels: ['new_high_n', 'new_low_n'], mode: 'any', high_n: 31, low_n: 31});
  assert.equal(chip('new_high_n').getAttribute('aria-pressed'), 'true');
  await elements.scrPresetClear.fire();
  await chip('vol_burst').fire(); await chip('break_ma20').fire();
  elements.scrVolMult.value = '1.6'; elements.scrPresetMode.value = 'all';
  await elements.scrPresetMode.fire('change'); await run();
  assert.deepEqual(requests.at(-1).preset, {labels: ['vol_burst', 'break_ma20'], mode: 'all', vol_mult: 1.6});
  assert(elements.scrApplied.textContent.includes('全部满足'));
  assert(!elements.scrBreakMult.disabled && elements.scrSpikeMult.disabled);
  const count = requests.length;
  elements.scrPriceMin.value = '20'; elements.scrPriceMax.value = '10';
  await run();
  assert.equal(requests.length, count, '无效区间不能发起选股请求');
  assert(elements.scrResultTable.innerHTML.includes('现价下限不能大于上限'));
  assert.equal(elements.scrAdv.hidden, false, '显示被折叠的错误条件');
  await elements.scrResetBtn.fire(); await run();
  assert.equal(elements.scrPresetMode.value, 'any');
  assert(params().every(p => p.disabled && p.value === ''));
  assert.deepEqual(requests.at(-1), requests[0]);
  response = {ok: true, stocks: [], partial: true, funnel: [{dimension: 'kline', before: 400, after: 0, missing: 0}]};
  await run();
  assert(elements.scrResultTable.innerHTML.includes('其余候选尚未评估'));
  assert(elements.scrResultMeta.textContent.includes('部分候选'));
  response = {ok: true, stocks: [], funnel: [{dimension: 'radar', before: 4, after: 0, missing: 4}]};
  await run();
  assert(elements.scrResultTable.innerHTML.includes('盘口异动数据不足'));
  response = {ok: true, stocks: [], funnel: [{dimension: 'flow', before: 4, after: 0, missing: 0}]};
  await run();
  assert(elements.scrResultTable.innerHTML.includes('主力资金条件将 4 只候选筛为 0 只'));
  await timers[0]();
  assert.equal(elements.scrSpikeMult.placeholder, '默认 1.8', '占位默认值应跟随服务端元数据');
  assert.equal(elements.scrAmountMin.placeholder, '不限');

  // ---- 牛股挖掘(底部埋伏三档):点选即筛选 ----
  assert.deepEqual(bulls.map(c => c.dataset.bull), ['ambush_steady', 'ambush_balanced', 'ambush_aggressive']);
  assert.deepEqual(bulls.map(c => c.dataset.risk), ['low', 'mid', 'high']);
  assert(bull('ambush_balanced').title.includes('低位未启动') && bull('ambush_balanced').title.includes('波动风险：中'));
  assert.equal(bull('ambush_balanced').dataset.count, '26');
  assert.equal(bull('ambush_aggressive').dataset.count, undefined, '没有命中数时不显示角标');
  await elements.scrResetBtn.fire();
  response = {ok: true, applied: ['bull'], base_count: 4, total_matched: 1, stocks: [
    {code: '600000', name: '样本', ambush_score: 93.1, pos60: 12.3, dip60: -1.2, elasticity: 3.4,
      hits: ['底部埋伏·均衡·位置12%·承接分位93%']}]};
  let sent = requests.length;
  await bull('ambush_balanced').fire();
  assert.equal(requests.length, sent + 1, '点选牛股标签直接发起筛选');
  assert.deepEqual(requests.at(-1).bull, {strategies: ['ambush_balanced']});
  assert.equal(requests.at(-1).sort, 'ambush_score', '排序停在默认项时自动切到承接分位');
  assert.equal(elements.scrSort.value, 'ambush_score', '切换对用户可见');
  assert.equal(bull('ambush_balanced').getAttribute('aria-pressed'), 'true');
  assert(elements.scrApplied.textContent.includes('牛股挖掘: 底部埋伏·均衡'));
  for (const text of ['承接分位', '60日位置%', '跌日主力%', '日波动%', '>93<', '>12<', '-1.2', '3.40', '承接分位93%']) {
    assert(elements.scrResultTable.innerHTML.includes(text), '启用牛股挖掘时展示' + text);
  }
  for (const gone of ['连板', '路径效率', '60日最大涨%']) {
    assert(!elements.scrResultTable.innerHTML.includes(gone), '旧策略的列不应再出现: ' + gone);
  }
  sent = requests.length;   // 上一轮未返回时连点:只补跑一次,且带上最新的全部选择
  await Promise.all([bull('ambush_aggressive').fire(), bull('ambush_steady').fire()]);
  assert.equal(requests.length, sent + 2);
  assert.deepEqual(requests.at(-1).bull, {strategies: ['ambush_steady', 'ambush_balanced', 'ambush_aggressive']});
  elements.scrSort.value = 'ret20';   // 用户自己选过排序:取消牛股标签时不该被改回去
  await bull('ambush_steady').fire(); await bull('ambush_aggressive').fire(); await bull('ambush_balanced').fire();
  assert.equal(requests.at(-1).bull, undefined, '全部取消后不再带牛股条件');
  assert.equal(requests.at(-1).sort, 'ret20');
  elements.scrSort.value = 'main_net_wan';
  await bull('ambush_steady').fire(); await bull('ambush_steady').fire();
  assert.equal(elements.scrSort.value, 'main_net_wan', '自动切过去的排序,取消后切回默认');
  response = {ok: true, applied: ['market'], stocks: [{code: '600000', name: '样本', hits: []}]};
  await run();
  assert(!elements.scrResultTable.innerHTML.includes('承接分位'), '未启用牛股挖掘时不加列');
  await bull('ambush_aggressive').fire(); await elements.scrResetBtn.fire(); await run();
  assert.deepEqual(requests.at(-1), requests[0], '重置会清掉牛股选择并恢复默认排序');
  response = {ok: true, stocks: [], funnel: [{dimension: 'bull', before: 4, after: 0, missing: 4}]};
  await bull('ambush_aggressive').fire();
  assert(elements.scrResultTable.innerHTML.includes('牛股挖掘数据不足'));
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
