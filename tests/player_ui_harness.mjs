/* Харнесс DOM/unit-тестов для static/player.js — node без браузера.
 * Запуск: node tests/player_ui_harness.mjs
 * Вывод: строки «PASS <имя>» либо стек упавшего assert.
 */
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const source = readFileSync(join(root, 'static', 'player.js'), 'utf8');

/* ------------------------- DOM-стаб ------------------------- */

const calls = { fetches: [] };
// стартовый ответ: player.js начинает опрос сразу при загрузке,
// до того как тесты переопределят respond
let respond = async () => jsonResponse(fakeState());
let now = 0;
let hidden = false;
const docListeners = {};

function elStub(id) {
  const listeners = {};
  const attrs = {};
  const classes = new Set();
  const children = [];
  const el = {
    id,
    hidden: false,
    disabled: false,
    textContent: '',
    value: '0',
    min: '0',
    max: '1000',
    style: { setProperty() {}, width: '' },
    innerHTML: null,      // тест следит, чтобы сюда никто не писал
    children,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => { if (on === undefined) { on = !classes.has(c); } if (on) { classes.add(c); } else { classes.delete(c); } },
      contains: (c) => classes.has(c),
    },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    click() { (listeners.click || []).forEach((fn) => fn({})); },
    fire(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev || {})); },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return (k in attrs) ? attrs[k] : null; },
    replaceChildren(...nodes) { children.length = 0; this.innerHTML = null; for (const n of nodes) children.push(n); },
    append(...nodes) { for (const n of nodes) children.push(n); },
    appendChild(n) { children.push(n); return n; },
    focus() {},
  };
  return el;
}
const elementIds = [
  'plConn', 'plAcc', 'plError', 'plMsg', 'plAnnounce', 'plTitle', 'plArtist', 'plBarFill',
  'plPos', 'plDur', 'plSeek', 'plStateChip', 'plIndex', 'btnPrev', 'btnPlay', 'btnPause',
  'btnStop', 'btnNext', 'plVolume', 'plVolumeVal', 'plQuery', 'btnSearchPlay',
  'btnLikesMe', 'btnLikesOther', 'btnRecs', 'plQueue', 'plQueueEmpty',
];
const elements = Object.fromEntries(elementIds.map((id) => [id, elStub(id)]));

const doc = {
  hidden: false,
  title: '',
  activeElement: null,
  getElementById: (id) => elements[id] || null,
  createElement: () => elStub('created'),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
};

const timers = { seq: 0, list: new Map() };
function schedule(fn, ms, once) {
  const id = ++timers.seq;
  timers.list.set(id, { fn, due: now + Math.max(0, ms || 0), once });
  return id;
}
const setTimeoutFn = (fn, ms) => schedule(fn, ms, true);
const setIntervalFn = (fn, ms) => schedule(fn, ms, false);
function clearTimeoutFn(id) { timers.list.delete(id); }

process.on('unhandledRejection', (e) => { console.error('UNHANDLED REJECTION:', e && e.stack || e); });

/* Микрозадачи: промисы fetch/await успевают выполниться. */
async function settle() {
  for (let i = 0; i < 200; i += 1) await new Promise((r) => setImmediate(r));
}

/* Прокрутка времени: выполняются только таймеры, чей срок наступил. */
async function advance(ms) {
  const target = now + Math.max(0, ms || 0);
  await settle();
  for (let guard = 0; guard < 400; guard += 1) {
    const due = [...timers.list.entries()]
      .filter(([, t]) => t.due <= target)
      .sort((a, b) => a[1].due - b[1].due);
    if (!due.length) break;
    const [id, t] = due[0];
    now = Math.max(now, t.due);
    if (t.once) timers.list.delete(id);
    else t.due = now + Math.max(1, t.ms || 1);
    await t.fn();
    await settle();
  }
  now = target;
  await settle();
}

class FakeDOMException extends Error {
  constructor(msg, name) { super(msg); this.name = name; }
}

const fakeFetch = async (url, opts) => {
  calls.fetches.push({ url, opts });
  return respond(url, opts);
};

const sandbox = {
  window: { document: doc, fetch: fakeFetch },
  document: doc,
  console,
  fetch: fakeFetch,
  setTimeout: setTimeoutFn,
  clearTimeout: clearTimeoutFn,
  setInterval: setIntervalFn,
  clearInterval: clearTimeoutFn,
  DOMException: FakeDOMException,
  AbortSignal: undefined,          // проверяем ручной фолбэк таймаута
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  Date: class extends Date { static now() { return now; } },
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'player.js' });

/* ------------------------- ассерты ------------------------- */

const failures = [];
function check(name, fn) {
  return Promise.resolve().then(fn).then(
    () => console.log('PASS ' + name),
    (err) => { failures.push(name); console.error('FAIL ' + name + '\n' + (err && err.stack || err)); },
  );
}
const eq = (a, b, name) => {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) { throw new Error(name + ': ' + sa + ' != ' + sb); }
};
const ok = (cond, name) => { if (!cond) throw new Error(name); };

function textOf(node) {
  // из children-стаба достаём текст: textNode | {textContent} | элементы
  if (!node) return '';
  if (node.nodeType === 3) return node.textContent;
  if (Array.isArray(node.children)) return node.children.map(textOf).join('');
  return String(node.textContent ?? '');
}

function lastFetch() { return calls.fetches[calls.fetches.length - 1]; }
function findFetch(url) { return calls.fetches.filter((f) => f.url === url); }
function bodyOf(f) { return JSON.parse(f.opts && f.opts.body); }

function fakeState(over = {}) {
  return {
    ok: true,
    state: {
      backend: 'panel-player',
      web_up: true,
      state: 'playing',
      track_info: 'Artist — Title',
      position_ms: 30000,
      duration_ms: 180000,
      queue: ['A — One', 'B — Two', 'C — Three'],
      queue_index: 1,
      accounts: {},
      active_account: 'me',
      volume: 70,
      ...over,
    },
  };
}

function jsonResponse(obj) {
  return {
    ok: true,
    status: 200,
    async text() { return JSON.stringify(obj); },
    async json() { return obj; },
    headers: { get: (k) => (k === 'content-type' ? 'application/json' : null) },
  };
}

/* ------------------------- сценарии ------------------------- */

const tests = [];

tests.push(['state-отрисовка и тексты', async () => {
  respond = async () => jsonResponse(fakeState());
  await advance(10);
  await settle();
  ok(textOf(elements.plTitle).includes('Title'), 'заголовок трека');
  ok(textOf(elements.plArtist).includes('Artist'), 'артист');
  ok(textOf(elements.plStateChip).includes('играет'), 'чип статуса');
  ok(!elements.plError.hidden === false ? true : elements.plError.hidden, 'ошибки нет');
  eq(elements.plSeek.max, '180000', 'максимум seek = длительность');
  ok(textOf(elements.plDur).includes('3:00'), 'длительность');
  eq(elements.plQueue.children.length, 3, 'очередь из 3 пунктов');
  ok(!elements.plQueueEmpty.hidden === false ? true : elements.plQueueEmpty.hidden, 'пустышка скрыта');
}]);

tests.push(['выбор трека в очереди шлёт index', async () => {
  const picked = elements.plQueue.children[2];
  const btn = picked.children.find((c) => c.id === undefined || true) || picked.children[0];
  // структура: li > button; кнопка — первый ребёнок
  const li = elements.plQueue.children[2];
  const button = li.children[0];
  ok(button && typeof button.fire === 'function', 'в li лежит кнопка');
  respond = async () => jsonResponse({ ok: true, output: 'ok' });
  button.fire('click');
  await advance(5);
  const cmd = findFetch('/api/soundya/cmd').map(bodyOf).filter((b) => b.cmd === 'index');
  eq(cmd.length, 1, 'одна index-команда');
  eq(cmd[0].arg, '2', 'arg — номер трека строкой');
}]);

tests.push(['пауза/стоп/следующий — команды по контракту', async () => {
  calls.fetches.length = 0;
  respond = async () => jsonResponse({ ok: true });
  elements.btnPause.fire('click');
  await advance(5);
  eq(bodyOf(lastFetch()), { cmd: 'pause' }, 'pause');
  elements.btnNext.fire('click');
  await advance(5);
  eq(bodyOf(lastFetch()), { cmd: 'next' }, 'next');
  elements.btnStop.fire('click');
  await advance(5);
  eq(bodyOf(lastFetch()), { cmd: 'stop' }, 'stop');
}]);

tests.push(['play из паузы шлёт resume', async () => {
  calls.fetches.length = 0;
  respond = async (url) => jsonResponse(url.includes('/state') ? fakeState({ state: 'paused' }) : { ok: true });
  await advance(2000);   // дождаться планового опроса с paused
  calls.fetches.length = 0;
  elements.btnPlay.fire('click');
  await settle();
  const resumes = findFetch('/api/soundya/cmd').map(bodyOf).filter((b) => b.cmd === 'resume');
  eq(resumes.length, 1, 'один resume');
  eq(resumes[0], { cmd: 'resume' }, 'resume');
}]);

tests.push(['сбой сети не сбрасывает playing', async () => {
  respond = async () => jsonResponse(fakeState());
  await advance(10);
  ok(textOf(elements.plTitle).includes('Title'), 'до сбоя трек есть');
  calls.fetches.length = 0;
  respond = async () => { throw new FakeDOMException('network', 'TypeError'); };
  await advance(20000 + 100);
  ok(textOf(elements.plTitle).includes('Title'), 'после сбоя трек остался');
  ok(elements.plError.hidden === false, 'ошибка видна');
  ok(textOf(elements.plError).includes('не значит'), 'ошибка различает связь и игру');
  ok(textOf(elements.plStateChip).includes('играет'), 'статус по-прежнему играет');
  ok(elements.btnPause.disabled === false, 'пауза всё ещё доступна');
}]);

tests.push(['авторизация 401 не считается остановкой', async () => {
  respond = async () => jsonResponse(fakeState());
  await advance(2000);
  calls.fetches.length = 0;
  respond = async (url, opts) => {
    if (String(url).includes('/state')) {
      return { ok: false, status: 401, async text() { return '{"detail":"нет сессии"}'; } };
    }
    return jsonResponse({ ok: true });
  };
  await advance(30000 + 100);
  ok(textOf(elements.plTitle).includes('Title'), 'трек не потерян');
  ok(textOf(elements.plError).includes('войд'), 'подсказка про вход');
}]);

tests.push(['лайки: свой — cmd, чужой — switch с {account,what}', async () => {
  calls.fetches.length = 0;
  respond = async (url) => jsonResponse(url.includes('/state') ? fakeState({ active_account: 'me' }) : { ok: true });
  await advance(10);
  calls.fetches.length = 0;
  elements.btnLikesMe.fire('click');
  await advance(10);
  eq(bodyOf(findFetch('/api/soundya/cmd')[0]), { cmd: 'likes' }, 'мои лайки — команда');
  calls.fetches.length = 0;
  elements.btnLikesOther.fire('click');
  await advance(10);
  const sw = findFetch('/api/soundya/switch');
  eq(sw.length, 1, 'один switch');
  eq(bodyOf(sw[0]), { account: 'other', what: 'likes' }, 'switch-контракт');
}]);

tests.push(['волна при чужом аккаунте — switch на recs', async () => {
  calls.fetches.length = 0;
  respond = async (url) => jsonResponse(url.includes('/state') ? fakeState({ active_account: 'other' }) : { ok: true });
  await advance(2000);   // дождаться опроса с active_account: other
  calls.fetches.length = 0;
  elements.btnRecs.fire('click');
  await settle();
  const sw = findFetch('/api/soundya/switch').map(bodyOf);
  eq(sw.length, 1, 'один switch');
  eq(sw[0], { account: 'other', what: 'recs' }, 'волна брата через switch');
}]);

tests.push(['поиск: cmd play с запросом', async () => {
  calls.fetches.length = 0;
  respond = async (url) => jsonResponse(url.includes('/state') ? fakeState() : { ok: true });
  elements.plQuery.value = 'кино группа';
  elements.btnSearchPlay.fire('click');
  await advance(10);
  eq(bodyOf(findFetch('/api/soundya/cmd')[0]), { cmd: 'play', arg: 'кино группа' }, 'play с запросом');
}]);

tests.push(['громкость уходит в settings и дедублируется', async () => {
  calls.fetches.length = 0;
  respond = async (url) => jsonResponse(url.includes('/state') ? fakeState({ volume: 40 }) : { ok: true });
  await advance(2000);   // дождаться опроса с volume: 40
  await settle();
  calls.fetches.length = 0;
  elements.plVolume.value = '55';
  elements.plVolume.fire('input');
  await settle();
  elements.plVolume.fire('input');  // вторая правка до отправки — дублей быть не должно
  await advance(400);
  await settle();
  const settings = findFetch('/api/soundya/settings').map(bodyOf);
  const sent = settings.filter((b) => b && b.volume === 55);
  eq(sent.length, 1, 'одна отправка громкости 55');
  eq(sent[0], { volume: 55 }, 'контракт громкости');
}]);

tests.push(['seek отправляется в секундах', async () => {
  calls.fetches.length = 0;
  respond = async (url) => jsonResponse(url.includes('/state') ? fakeState() : { ok: true });
  await advance(10);
  calls.fetches.length = 0;
  elements.plSeek.value = '61000';
  elements.plSeek.fire('change');
  await advance(300);
  const seeks = findFetch('/api/soundya/cmd').map(bodyOf).filter((b) => b.cmd === 'seek');
  eq(seeks.length, 1, 'один seek');
  eq(seeks[0].arg, '61', 'секунды строкой');
}]);

tests.push(['враждебный текст не попадает в innerHTML', async () => {
  const evil = '<img src=x onerror=alert(1)> — Title';
  respond = async () => jsonResponse(fakeState({ track_info: evil, queue: [evil, 'X — Y'] }));
  await advance(2000);   // дождаться опроса с враждебным payload
  for (const id of elementIds) {
    ok(elements[id].innerHTML === null, 'innerHTML не тронут: ' + id);
  }
  ok(textOf(elements.plArtist).includes('<img'), 'артист пришёл как текст');
  ok(textOf(elements.plTitle).includes('Title'), 'заголовок без потерь');
  ok(!String(textOf(elements.plQueue)).includes('<script'), 'очередь без HTML-инъекций');
  ok(String(textOf(elements.plQueue)).includes('<img'), 'очередь хранит враждебный фрагмент как текст');
}]);

tests.push(['visibility: скрытая вкладка молчит, возврат опрашивает', async () => {
  calls.fetches.length = 0;
  respond = async () => jsonResponse(fakeState());
  await advance(10);
  const before = findFetch('/api/soundya/state').length;
  doc.hidden = true; hidden = true;
  (docListeners.visibilitychange || []).forEach((fn) => fn({}));
  await advance(30000 + 100);
  eq(findFetch('/api/soundya/state').length, before, 'в скрытой вкладке опроса нет');
  doc.hidden = false; hidden = false;
  (docListeners.visibilitychange || []).forEach((fn) => fn({}));
  await advance(10);
  ok(findFetch('/api/soundya/state').length > before, 'после возврата опрос пошёл');
}]);

/* ------------------------- прогон ------------------------- */

for (const [name, fn] of tests) { await check(name, fn); await settle(); }

if (failures.length) {
  console.error('ИТОГ: провалов ' + failures.length);
  process.exit(1);
} else {
  console.log('ИТОГ: все DOM-тесты плеера прошли');
}
