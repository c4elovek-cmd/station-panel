/* Встроенный плеер SoundCloud (panel-player) — логика без зависимостей.
 *
 * Контракт с панелью (server.py, только чтение):
 *   GET  /api/soundya/state   → {ok, state:{backend,web_up,state,track_info,
 *        position_ms,queue[],queue_index,accounts{},active_account,volume,…}}
 *   POST /api/soundya/cmd     → {cmd:'play|pause|resume|next|prev|stop|likes|recs|index', arg}
 *   POST /api/soundya/switch  → {account:'me|other', what:'likes|recs'}
 *   POST /api/soundya/settings→ {volume:0..100}
 *
 * Правила, зашитые сюда:
 *  — сетевая/авторизационная ошибка НЕ считается «не играет»: последний
 *    успешный ответ живёт в кэше, транспорт остаётся рабочим;
 *  — запросы строго последовательные (никаких параллельных poll);
 *  — AbortSignal.timeout с фолбэком на ручной AbortController;
 *  — скрытая вкладка (document.visibilitychange) опрос приостанавливает;
 *  — весь текст из ответов вставляется только через textContent.
 */
'use strict';

/* ------------------------------ константы ------------------------------ */

const API_STATE = '/api/soundya/state';
const API_CMD = '/api/soundya/cmd';
const API_SWITCH = '/api/soundya/switch';
const API_SETTINGS = '/api/soundya/settings';

const POLL_ACTIVE_MS = 1500;   // что-то играет/на паузе
const POLL_IDLE_MS = 5000;     // тишина/ошибка — реже
const POLL_BACKOFF_MAX = 20000;
const UPTIME_OK_MS = 800;      // ниже этого считаем веб демона упавшим
const SEEK_DEBOUNCE_MS = 150;
const VOLUME_DEBOUNCE_MS = 250;
const VOLUME_KEEP_MS = 2000;   // локальная правка громкости защищена от затирания так долго

const accLabel = (who) => (who === 'other' ? 'аккаунт брата' : 'мой аккаунт');
const stateLabel = {
  playing: 'играет',
  paused: 'пауза',
  stopped: 'остановлено',
  idle: 'тишина',
  connecting: 'подключение',
};

/* ------------------------------ утилиты ------------------------------ */

const $ = (id) => document.getElementById(id);

/* Timeout-сигнал: стандартный AbortSignal.timeout, при отсутствии —
   ручной контроллер (старые браузеры). Причину не подменяем. */
function timeoutSignal(ms) {
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    return AbortSignal.timeout(ms);
  }
  const ctrl = new AbortController();
  setTimeout(() => ctrl.abort(new DOMException('таймаут запроса', 'TimeoutError')), ms);
  return ctrl.signal;
}

const isNetAbort = (err) =>
  err instanceof DOMException && (err.name === 'AbortError' || err.name === 'TimeoutError');

/* Время «2:07» из миллисекунд; null → прочерк. */
function fmtMs(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return null;
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m + ':' + String(s).padStart(2, '0');
}

/* «Artist — Title» → {artist, title}; иначе всё — title. */
function splitTrack(raw) {
  const text = String(raw || '').trim();
  if (!text) return { artist: '', title: '' };
  const cut = text.indexOf(' — ') >= 0 ? text.indexOf(' — ')
    : text.indexOf(' - ') >= 0 ? text.indexOf(' - ') : -1;
  if (cut > 0) {
    return { artist: text.slice(0, cut).trim(), title: text.slice(cut + 3).trim() };
  }
  return { artist: '', title: text };
}

/* ------------------------------ элементы ------------------------------ */

const el = {
  conn: $('plConn'), acc: $('plAcc'),
  error: $('plError'), msg: $('plMsg'), announce: $('plAnnounce'),
  title: $('plTitle'), artist: $('plArtist'),
  barFill: $('plBarFill'), pos: $('plPos'), dur: $('plDur'),
  seek: $('plSeek'), stateChip: $('plStateChip'), index: $('plIndex'),
  prev: $('btnPrev'), play: $('btnPlay'), pause: $('btnPause'),
  stop: $('btnStop'), next: $('btnNext'),
  volume: $('plVolume'), volumeVal: $('plVolumeVal'),
  query: $('plQuery'), searchPlay: $('btnSearchPlay'),
  likesMe: $('btnLikesMe'), likesOther: $('btnLikesOther'), recs: $('btnRecs'),
  queue: $('plQueue'), queueEmpty: $('plQueueEmpty'),
};

/* ------------------------------ состояние ------------------------------ */

let last = null;            // последний успешный state
let netDown = false;        // нет связи сейчас
let announced = '';         // чтобы live-область не трещала каждую секунду
let pollChain = Promise.resolve();   // строгая последовательность
let pollDelay = POLL_IDLE_MS;
let backoff = 0;            // серия неудач подряд
let ticker = null;          // локальный ход позиции
let localPos = null;        // мс на момент последнего рендера
let localAt = 0;
let seekTimer = 0;
let volTimer = 0;
let volumeSending = false;
let volumeLocal = null;     // локальная правка громкости, пока не подтверждена сервером
let volumeLocalAt = 0;      // время последней правки (мс)
let wakeReason = null;      // флаг немедленного опроса
let pageHidden = false;

/* ------------------------------ сеть ------------------------------ */

/* Один запрос с таймаутом; бросает Error с человеческим текстом. */
async function request(url, body, timeoutMs) {
  let response;
  try {
    response = await fetch(url, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: timeoutSignal(timeoutMs),
      cache: 'no-store',
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('запрос отменён');
    }
    throw new Error('нет ответа');
  }
  if (response.status === 401) {
    throw new AuthError('сессия панели истекла');
  }
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).detail || ''; } catch (_) { /* не JSON */ }
    throw new Error(detail || ('HTTP ' + response.status));
  }
  const text = await response.text();
  try { return JSON.parse(text); } catch (_) { return text; }
}

class AuthError extends Error {}
const isAuth = (err) => err instanceof AuthError;

/* ------------------------------ опрос ------------------------------ */

function schedulePoll() {
  const playing = last && (last.state === 'playing' || last.state === 'paused');
  pollDelay = playing ? POLL_ACTIVE_MS : POLL_IDLE_MS;
  setTimeout(() => { pollChain = pollChain.then(pollOnce); }, pollDelay);
}

/* Один шаг опроса; ошибки не рвут цепочку. */
async function pollOnce() {
  if (pageHidden) { wakeReason = wakeReason || 'hidden-return'; return; }
  if (wakeReason) { wakeReason = null; }
  try {
    const data = await request(API_STATE, null, 12000);
    backoff = 0;
    applyNet(true);
    const s = data && data.state;
    if (s && typeof s === 'object') {
      applyState(s);
    } else {
      applyState(null);
    }
  } catch (err) {
    backoff += 1;
    applyNet(false, err);
    pollDelay = Math.min(POLL_IDLE_MS * Math.pow(2, backoff), POLL_BACKOFF_MAX);
    setTimeout(() => { pollChain = pollChain.then(pollOnce); }, pollDelay);
    return;
  }
  schedulePoll();
}

/* Сеть/авторизация отдельно от playing: сбой связи не сбрасывает трек. */
function applyNet(up, err) {
  if (up) {
    if (netDown) {
      netDown = false;
      announce('связь с колонкой восстановилась');
    }
    el.error.hidden = true;
    el.error.replaceChildren();
    el.conn.replaceChildren(document.createTextNode('на связи'));
    el.conn.classList.remove('off');
  } else {
    const why = isAuth(err)
      ? 'Сессия панели истекла: войди заново в основной вкладке — воспроизведение на колонке не прервано.'
      : 'Нет связи с панелью. Это не значит, что музыка остановилась: колонка играет сама.';
    el.error.replaceChildren(document.createTextNode(why));
    el.error.hidden = false;
    el.conn.replaceChildren(document.createTextNode('нет связи'));
    el.conn.classList.add('off');
    if (!netDown) {
      netDown = true;
      announce(why);
    }
  }
}

/* ------------------------------ рендер ------------------------------ */

function applyState(s) {
  const prev = last;
  last = s;

  if (!s) {
    el.stateChip.replaceChildren(document.createTextNode('нет данных'));
    renderTransport();
    return;
  }

  // связь/бэкенд
  if (s.web_up === false) {
    el.conn.replaceChildren(document.createTextNode('веб демона недоступен'));
    el.conn.classList.add('off');
    if (netDown) { netDown = false; el.error.hidden = true; }
    announceError('Веб плеера на колонке не поднимается. Музыка могла остаться играть.');
  } else {
    el.conn.replaceChildren(document.createTextNode('на связи'));
    el.conn.classList.remove('off');
  }

  // трек
  const info = splitTrack(s.track_info);
  el.title.replaceChildren(document.createTextNode(info.title || '—'));
  el.artist.replaceChildren(document.createTextNode(info.artist || ''));
  document.title = info.title
    ? info.title + ' — Плеер SoundCloud'
    : 'Плеер SoundCloud — Панель Станции';

  // аккаунт
  const who = s.active_account === 'other' ? 'other' : 'me';
  el.acc.hidden = false;
  el.acc.replaceChildren(document.createTextNode('Аккаунт: ' + accLabel(who)));

  // позиция/длительность
  const dur = typeof s.duration_ms === 'number' ? s.duration_ms : null;
  const pos = typeof s.position_ms === 'number' ? s.position_ms : 0;
  localPos = pos;
  localAt = Date.now();
  renderProgress(pos, dur);

  // статус
  const st = stateLabel[s.state] ? s.state : 'unknown';
  el.stateChip.replaceChildren(document.createTextNode(stateLabel[s.state] || s.state || '—'));
  if (s.state === 'playing') { el.stateChip.classList.add('on'); }
  else { el.stateChip.classList.remove('on'); }

  // очередь
  renderQueue(Array.isArray(s.queue) ? s.queue : [], s.queue_index);

  // громкость: не затираем локальную правку, пока она не ушла на сервер
  // и сервер не подтвердил новое значение (иначе получаем дрожание UI)
  const volFresh = volumeLocal !== null
    && (Date.now() - volumeLocalAt) < VOLUME_KEEP_MS;
  if (!volFresh && typeof s.volume === 'number') {
    el.volume.value = Math.max(0, Math.min(100, Math.round(s.volume)));
    syncVolume();
  }

  renderTransport();

  // объявления — только смены состояния, не каждый опрос
  if (prev) {
    if (prev.state !== s.state) {
      announce(statusAnnounce(s));
    }
    if ((prev.track_info || '') !== (s.track_info || '') && s.state === 'playing') {
      announce('Играет: ' + (info.title || s.track_info || '—'));
    }
    if (prev.active_account !== who) {
      announce('Активный аккаунт: ' + accLabel(who));
    }
  } else if (s.state === 'playing') {
    announce('Плеер на связи, играет: ' + (info.title || 'трек'));
  }
}

function statusAnnounce(s) {
  switch (s.state) {
    case 'playing': return 'Воспроизведение идёт';
    case 'paused': return 'Пауза';
    case 'stopped': return 'Остановлено, очередь очищена';
    case 'idle': return 'Ничего не играет';
    default: return 'Статус: ' + (s.state || 'неизвестно');
  }
}

/* Ход трека: позиция из кэша + локальные тики между опросами. */
function renderProgress(posMs, durMs) {
  if (durMs && Number.isFinite(durMs)) {
    el.seek.max = String(Math.max(1000, Math.round(durMs)));
  } else {
    el.seek.max = '1000';
  }
  const pos = Math.max(0, Math.round(posMs || 0));
  if (document.activeElement !== el.seek) { el.seek.value = String(pos); }
  syncSeekA11y(pos, durMs);
  el.pos.replaceChildren(document.createTextNode(fmtMs(pos) || '0:00'));
  el.dur.replaceChildren(document.createTextNode(fmtMs(durMs) || '–:––'));
  const pct = durMs ? Math.min(100, (pos / durMs) * 100) : 0;
  el.barFill.style.width = pct.toFixed(1) + '%';
}

function syncSeekA11y(pos, dur) {
  const p = fmtMs(pos);
  const d = fmtMs(dur);
  el.seek.setAttribute('aria-valuetext', p && d ? p + ' из ' + d : (p || 'начало'));
}

function syncVolume() {
  el.volumeVal.replaceChildren(document.createTextNode(el.volume.value + '%'));
  el.volume.setAttribute('aria-valuetext', el.volume.value + ' процентов');
  const pct = (el.volume.value - el.volume.min) / (el.volume.max - el.volume.min);
  el.volume.style.setProperty('--control-value', String(pct * 100));
}

function renderQueue(queue, queueIndex) {
  const items = [];
  const idx = typeof queueIndex === 'number' ? queueIndex : -1;
  queue.slice(0, 500).forEach((raw, i) => {
    const info = splitTrack(raw);
    const li = document.createElement('li');
    if (i === idx) li.className = 'pl-qitem on';
    else li.className = 'pl-qitem';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pl-qpick';
    const num = document.createElement('span');
    num.className = 'pl-qnum';
    num.replaceChildren(document.createTextNode(String(i + 1)));
    const name = document.createElement('span');
    name.className = 'pl-qname';
    name.replaceChildren(document.createTextNode(info.title || raw || '—'));
    const sub = document.createElement('span');
    sub.className = 'pl-qsub';
    sub.replaceChildren(document.createTextNode(info.artist || ''));
    btn.append(num, name, sub);
    if (i === idx) {
      btn.setAttribute('aria-current', 'true');
    }
    btn.setAttribute('aria-label',
      'Играть номер ' + (i + 1) + (info.title ? ': ' + info.title : ''));
    if (i === idx) {
      btn.disabled = true;
    } else {
      btn.addEventListener('click', () => { void sendIndex(i); });
    }
    li.append(btn);
    items.push(li);
  });
  el.queue.replaceChildren(...items);
  el.queueEmpty.hidden = queue.length > 0;
  if (idx >= 0 && queue.length) {
    el.index.hidden = false;
    el.index.replaceChildren(document.createTextNode(
      'трек ' + (idx + 1) + ' из ' + queue.length));
  } else {
    el.index.hidden = true;
  }
}

function renderTransport() {
  const st = last ? last.state : null;
  const qlen = last && Array.isArray(last.queue) ? last.queue.length : 0;
  const known = last && stateLabel[st] != null;

  el.pause.disabled = !(st === 'playing' || st === 'paused');
  el.play.disabled = !(st === 'paused' || (known && st !== 'playing' && qlen > 0));
  el.stop.disabled = !(st === 'playing' || st === 'paused');
  el.next.disabled = !known || qlen < 2;
  el.prev.disabled = !known || qlen < 2;
}

/* ------------------------------ команды ------------------------------ */

async function sendCmd(cmd, arg, opts = {}) {
  const body = arg === undefined ? { cmd } : { cmd, arg };
  await runCommand(() => request(API_CMD, body, opts.timeout || 30000), opts.label || ('команда ' + cmd));
}

async function runCommand(fn, label) {
  try {
    const result = await fn();
    const output = (result && typeof result === 'object' && result.output) || '';
    const ok = !result || typeof result !== 'object' || result.ok !== false;
    if (ok) {
      setMsg(label + ': принято', false);
    } else {
      setMsg(label + ': колонка ответила ошибкой' + (output ? ' — ' + output : ''), true);
      announce(label + ' не получилось');
    }
  } catch (err) {
    if (isAuth(err)) {
      applyNet(false, err);
    } else {
      setMsg(label + ': ' + commandErrorText(err), true);
      announce(label + ' не получилось');
      applyNet(false, err);
    }
    return;
  }
  wakeSoon();
}

/* Ошибку сети при команде показываем отдельно от «не играет». */
function commandErrorText(err) {
  const raw = (err && err.message) || '';
  if (raw === 'нет ответа' || /таймаут/.test(raw)) {
    return 'колонка не ответила; возможно, команда всё же дошла — статус уточнится при связи';
  }
  return raw || 'неизвестная ошибка';
}

function setMsg(text, isError) {
  el.msg.classList.toggle('err', Boolean(isError));
  el.msg.replaceChildren(document.createTextNode(text));
}

function announce(text) {
  if (text === announced) return;
  announced = text;
  el.announce.replaceChildren(document.createTextNode(text));
}

let announceErrShown = '';
function announceError(text) {
  if (text === announceErrShown) return;
  announceErrShown = text;
  setMsg(text, true);
}

function wakeSoon() {
  wakeReason = 'cmd';
  pollDelay = 200;
  setTimeout(() => { pollChain = pollChain.then(pollOnce); }, 300);
}

async function sendIndex(i) {
  await sendCmd('index', String(i), { label: 'выбор трека №' + (i + 1) });
}

/* Пуск: продолжить паузу; из тишины при живой очереди — с первого номера. */
async function play() {
  const st = last ? last.state : null;
  const qlen = last && Array.isArray(last.queue) ? last.queue.length : 0;
  if (st === 'paused') {
    await sendCmd('resume', undefined, { label: 'продолжение' });
  } else if (st !== 'playing' && qlen > 0) {
    const idx = typeof last.queue_index === 'number' ? last.queue_index : -1;
    await sendIndex(idx >= 0 ? idx : 0);
  }
}

/* Лайки: свой аккаунт — команда; чужой — переключение аккаунта. */
async function likes(who) {
  const active = last ? (last.active_account === 'other' ? 'other' : 'me') : null;
  const label = who === 'other' ? 'лайки брата' : 'мои лайки';
  if (who === 'me' && (active === 'me' || active === null)) {
    await sendCmd('likes', undefined, { label, timeout: 90000 });
  } else {
    await runCommand(
      () => request(API_SWITCH, { account: who, what: 'likes' }, 90000),
      'переключение: ' + label);
  }
}

async function recs() {
  const active = last ? (last.active_account === 'other' ? 'other' : 'me') : null;
  if (active === 'other') {
    await runCommand(
      () => request(API_SWITCH, { account: 'other', what: 'recs' }, 90000),
      'переключение: волна для брата');
  } else {
    await sendCmd('recs', undefined, { label: 'волна', timeout: 90000 });
  }
}

async function searchPlay() {
  const q = el.query.value.trim();
  if (!q) {
    setMsg('Введи название трека, артиста или ссылку SoundCloud.', true);
    el.query.focus();
    return;
  }
  setMsg('Ищу на колонке: ' + q + '…', false);
  await sendCmd('play', q, { label: 'поиск «' + q + '»', timeout: 90000 });
}

/* ------------------------------ обработчики ------------------------------ */

el.prev.addEventListener('click', () => { void sendCmd('prev', undefined, { label: 'предыдущий трек' }); });
el.next.addEventListener('click', () => { void sendCmd('next', undefined, { label: 'следующий трек' }); });
el.pause.addEventListener('click', () => { void sendCmd('pause', undefined, { label: 'пауза' }); });
el.stop.addEventListener('click', () => { void sendCmd('stop', undefined, { label: 'стоп' }); });
el.play.addEventListener('click', () => { void play(); });

el.searchPlay.addEventListener('click', () => { void searchPlay(); });
el.query.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); void searchPlay(); }
});

el.likesMe.addEventListener('click', () => { void likes('me'); });
el.likesOther.addEventListener('click', () => { void likes('other'); });
el.recs.addEventListener('click', () => { void recs(); });

/* Позиция: тянем — обновляем цифры локально, отпускаем — команда seek. */
el.seek.addEventListener('input', () => {
  clearTimeout(seekTimer);
  const pos = Number(el.seek.value);
  const dur = Number(el.seek.max);
  renderProgress(pos, dur);
});
el.seek.addEventListener('change', () => {
  clearTimeout(seekTimer);
  const posMs = Number(el.seek.value);
  seekTimer = setTimeout(() => {
    const seconds = Math.floor(posMs / 1000);
    if (localPos !== null && Math.abs(posMs - localPos) < 900) return; // дрожь округления
    void sendCmd('seek', String(seconds), { label: 'перематываю' });
  }, SEEK_DEBOUNCE_MS);
});

/* Громкость: локальный отклик сразу, команда — debounced, без дублей. */
el.volume.addEventListener('input', () => {
  volumeLocal = Math.round(Number(el.volume.value));
  volumeLocalAt = Date.now();
  syncVolume();
  clearTimeout(volTimer);
  volTimer = setTimeout(pushVolume, VOLUME_DEBOUNCE_MS);
});
el.volume.addEventListener('change', () => {
  volumeLocal = Math.round(Number(el.volume.value));
  volumeLocalAt = Date.now();
  clearTimeout(volTimer);
  void pushVolume();
});

async function pushVolume() {
  if (volumeSending) { volTimer = setTimeout(pushVolume, VOLUME_DEBOUNCE_MS); return; }
  const v = Math.round(Number(el.volume.value));
  if (!Number.isFinite(v) || v < 0 || v > 100) return;
  volumeSending = true;
  try {
    await request(API_SETTINGS, { volume: v }, 15000);
    volumeLocal = null;   // сервер принял — вернуться к отображению его значения
  } catch (err) {
    if (isAuth(err)) applyNet(false, err);
    else setMsg('Громкость ' + v + '% не применилась: ' + commandErrorText(err), true);
  } finally {
    volumeSending = false;
  }
}

/* ------------------------------ фоновые тики ------------------------------ */

/* Локальный ход позиции между опросами — только когда играет. */
function startTicker() {
  if (ticker) return;
  ticker = setInterval(() => {
    if (!last || last.state !== 'playing' || localPos === null) return;
    const dur = typeof last.duration_ms === 'number' ? last.duration_ms : null;
    if (dur && localPos >= dur) return;
    const pos = localPos + (Date.now() - localAt);
    renderProgress(pos, dur);
  }, 250);
}

/* Скрытая вкладка: опрос стоит, возвращаемся — немедленный опрос. */
document.addEventListener('visibilitychange', () => {
  pageHidden = document.hidden;
  if (!pageHidden && (wakeReason || !last)) {
    wakeReason = 'visible';
    pollChain = pollChain.then(pollOnce);
  }
});

/* ------------------------------ старт ------------------------------ */

syncVolume();
startTicker();
pollChain = pollChain.then(pollOnce);
