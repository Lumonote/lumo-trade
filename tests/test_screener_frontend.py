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
const elements = {}, chips = [], boards = [], labels = [], requests = [], timers = [];
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
for (const match of markup.matchAll(/<input\b([^>]*class="scr-board"[^>]*)>/g)) boards.push(new Element('', attrs(match[1])));
for (const match of markup.matchAll(/<label\b([^>]*data-scr-presets="[^"]+"[^>]*)>([\s\S]*?)<\/label>/g)) {
  const label = new Element('', attrs(match[1])); label.input = elements[attrs(match[2]).id]; labels.push(label);
}
const params = () => Object.values(elements).filter(el => el.dataset.scrParam);
const document = {body: {dataset: {page: 'screener'}}, getElementById: id => elements[id] || null,
  querySelectorAll(selector) {
    if (selector === '.scr-chip') return chips;
    if (selector === '.scr-chip.active') return chips.filter(c => c.classList.contains('active'));
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
  if (url.endsWith('/meta')) return {dims: {}, presets: {defaults: {spike_mult: 1.8, amount_min: 0}}};
  assert.equal(url, '/api/screener/run'); requests.push(JSON.parse(options.body)); return response;
}};
elements.scrRunBtn.addEventListener('click', () => {throw new Error('旧事件不应重复执行');});
const context = vm.createContext({document, window, localStorage: {getItem: () => null, setItem() {}},
  setTimeout: fn => timers.push(fn)});
vm.runInContext(fs.readFileSync('webui/static/lumo_screener.js', 'utf8'), context);
const chip = key => chips.find(c => c.dataset.preset === key);
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
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run([shutil.which("node"), "-e", script],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
