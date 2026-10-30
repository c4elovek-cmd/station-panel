/* Панель Станции — логика фронтенда (без зависимостей) */
'use strict';

const $ = id => document.getElementById(id);

const state = {
  status: null,
  settings: null,
  plugins: [],
  configPluginId: null,
  configSchema: {},
  ledColors: [],
  ledCount: 6,
  series: { temp: [], light: [] },
  chartTips: new Map(), // canvas -> { x, index }
  clockDebounce: null,
  clockLast: { time: null, brightness: null },
  clockOff: false,
  term: null,
  fit: null,
  socket: null
};

/* ------------------------------ утилиты ------------------------------ */
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { 'content-type': 'application/json' },
    ...options
  });
  if (response.status === 401) { location.href = '/login'; throw new Error('сессия истекла'); }
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || text; } catch (_) {}
    throw new Error(detail || response.statusText);
  }
  const type = response.headers.get('content-type') || '';
  const text = await response.text();
  if (type.includes('json') && text.trim()) {
    try { return JSON.parse(text); } catch (_) { return text; }
  }
  return text;
}

const yam = (path, options) => api('/yam/api' + path, options);

function secondsToHuman(seconds) {
  if (!seconds && seconds !== 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} д ${h} ч`;
  if (h) return `${h} ч ${m} мин`;
  return `${m} мин`;
}

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* SVG-иконки из спрайта index.html (#i-*): svgIcon('play'), svgIcon('warn', 'ic-lvl-high') */
const svgIcon = (name, cls = '') => `<svg class="ic ${cls}"><use href="#i-${name}"/></svg>`;

/* Красивые имена моделей (код из /api/device → человекочитаемое название).
   CLOCK — модели с LED-кольцом, на котором панель показывает часы. */
const MODEL_NAMES = {
  yandexmini_2: 'Яндекс Станция Мини 2',
  yandexmini_3: 'Яндекс Станция Мини 3',
  yandexmini_3_pro: 'Яндекс Станция Мини 3 Про',
  yandexmini: 'Яндекс Станция Мини',
  yandexstation: 'Яндекс Станция',
  yandexstation_2: 'Яндекс Станция 2',
  yandexstation_3: 'Яндекс Станция 3',
  yandexmidi: 'Яндекс Станция Миди',
  yandexmax: 'Яндекс Станция Макс',
  yandexlite: 'Яндекс Станция Лайт',
  yandexlite_2: 'Яндекс Станция Лайт 2',
  yandexstreet: 'Яндекс Станция Стрит',
  yandexduo_max: 'Яндекс Станция Дуо Макс',
  yandexduomax: 'Яндекс Станция Дуо Макс',
  yandexstation_duo_max: 'Яндекс Станция Дуо Макс',
  yandexduo: 'Яндекс Станция Дуо',
  yandexstation_duo: 'Яндекс Станция Дуо',
};
const CLOCK_MODELS = new Set(['yandexmini_2', 'yandexmini', 'yandexstation', 'yandexstation_2']);
function prettyModel(model) {
  if (!model) return 'Яндекс Станция';
  const key = String(model).toLowerCase().replace(/[-\s]+/g, '_');
  let name = MODEL_NAMES[key];
  if (!name) {
    if (key.includes('mini') && key.includes('2')) name = 'Яндекс Станция Мини 2';
    else if (key.includes('mini')) name = 'Яндекс Станция Мини';
    else name = 'Яндекс Станция ' + String(model).replace(/[_-]+/g, ' ').trim();
  }
  return CLOCK_MODELS.has(key) ? name + ' с часами' : name;
}

/* Фото колонки в «Обзоре»: официальные изображения из Справки Алисы
   (static/img/speakers/*.png). Если фото для модели нет — остаётся SVG. */
const MODEL_PHOTOS = {
  yandexmini_2: 'mini2.png',
  yandexmini_3: 'mini3.png',
  yandexmini_3_pro: 'mini3-pro.png',
  yandexmini: 'mini1.png',
  yandexstation_2: 'gen2.png',
  yandexstation_3: 'gen3.png',
  yandexmidi: 'midi.png',
  yandexmax: 'max.png',
  yandexlite: 'lite.png',
  yandexlite_2: 'lite2.png',
  yandexstreet: 'street.png',
  yandexduo_max: 'duo-max.png',
  yandexduomax: 'duo-max.png',
  yandexstation_duo_max: 'duo-max.png',
};

function setDevicePhoto(model) {
  const img = $('devicePhoto');
  if (!img) return;
  const key = String(model || '').toLowerCase().replace(/[-\s]+/g, '_');
  const file = MODEL_PHOTOS[key];
  if (!file) return;                       // фото нет — оставляем текущую картинку
  img.src = '/static/img/speakers/' + file;
  img.onerror = () => {                    // файл не доехал — возврат к SVG
    img.src = '/static/img/station-mini2.svg';
    img.onerror = null;
  };
}

/* Компактные характеристики модели — со страниц Справки Алисы
   (alice.yandex.ru/support/.../characteristics-*, собрано для v2). */
const MODEL_SPECS = {
  yandexmini_2: 'Ø97×54 мм · 260 г · 10 Вт · LED-кольцо 32 диода (часы — YNDX-00020) · 4 мика',
  yandexmini_3: 'Ø102×60,5 мм · 326 г · 12 Вт · LED 32 сегмента · 3 мика, шумоподавление',
  yandexmini_3_pro: 'Ø102×71 мм · 438 г · 18 Вт · модули: АКБ и климатический датчик · Zigbee 3.0',
  yandexmini: 'Ø90×45 мм · 170 г · 3 Вт · датчик жестов TOF · 4 мика',
  yandexstation_2: '123×99×199 мм · 1,3 кг · 30 Вт · круговая подсветка · Zigbee 3.0',
  yandexstation_3: '132×132×201 мм · 1,76 кг · 50 Вт · эмбиент-подсветка 12 LED · Zigbee 3.0, Matter',
  yandexmidi: '96×96×110 мм · 0,9 кг · 24 Вт · датчик присутствия · Zigbee 3.0',
  yandexmax: '141×141×231 мм · 2,65 кг · 65 Вт · Dolby Audio · HDMI 2.0 (вывод 4K) · Zigbee',
  yandexlite: 'Ø90×55 мм · 230 г · 5 Вт · 4 мика · 6 расцветок',
  yandexlite_2: 'Ø90×65 мм · 350 г · 6 Вт · LED 36 сегментов (часы — YNDX-00026) · опциональный АКБ',
  yandexstreet: 'Ø83×202 мм · 700 г · 30 Вт · АКБ 3300 мАч (до 12 ч) · защита IP67 · 3 мика',
  yandexduo_max: '256×118×240 мм · 3,3 кг · 60 Вт · экран 10,5" 1920×1200 · камера 13 Мп · Zigbee',
  yandexduomax: '256×118×240 мм · 3,3 кг · 60 Вт · экран 10,5" 1920×1200 · камера 13 Мп · Zigbee',
  yandexstation_duo_max: '256×118×240 мм · 3,3 кг · 60 Вт · экран 10,5" 1920×1200 · камера 13 Мп · Zigbee',
};

function setDeviceSpecs(model) {
  const el = $('deviceSpecs');
  if (!el) return;
  const key = String(model || '').toLowerCase().replace(/[-\s]+/g, '_');
  el.textContent = MODEL_SPECS[key] || '';
}

const hex = color => {
  const value = String(color || '').trim();
  return /^#[0-9a-f]{6}$/i.test(value) ? value.toUpperCase() : '#000000';
};

function notify(element, message, kind = '') {
  if (!element) return;
  element.textContent = message;
  element.style.color = kind === 'error' ? 'var(--danger)' : kind === 'ok' ? 'var(--ok)' : '';
}

/* ------------------------------ вкладки ------------------------------ */
function showTab(name) {
  document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.tab === name));
  document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.id === 'tab-' + name));
  if (name === 'console') setTimeout(() => { state.fit && state.fit.fit(); state.term && state.term.focus(); }, 60);
  if (name === 'sensors') drawCharts();
  if (name === 'wakeword') { wwRefresh(); if (!wwTimer) wwTimer = setInterval(wwRefresh, 700); }
  if (name === 'alice') loadAliceAll();
  if (name === 'system') { loadMinidumps(); runSecurityAudit(); loadSounds(); }
  if (name === 'led') { ledAnimLoad(); loadNightClock(); }
  if (name === 'checklist') loadChecklist();
  if (name === 'plugins') loadCatalog();
  localStorage.setItem('sp_tab', name);
}

document.querySelectorAll('.nav-item').forEach(item => item.onclick = () => showTab(item.dataset.tab));
$('voiceGoWords').onclick = () => showTab('wakeword');
/* ссылки-переходы на вкладки из пояснительных текстов */
document.querySelectorAll('[data-goto-tab]').forEach(link => link.onclick = (e) => { e.preventDefault(); showTab(link.dataset.gotoTab); });

$('logout').onclick = async () => {
  await api('/api/logout', { method: 'POST', body: '{}' }).catch(() => {});
  location.href = '/login';
};

/* ---------------------- автоопределение колонки ---------------------- */
let detecting = false;

/* Список найденных станций: с YAM — рабочие, без YAM — «сырые» (видны по mDNS,
   как в alice bridge: рут и YAM на них ещё не установлены). */
function renderDetectList(devices) {
  const box = $('detectList');
  if (!box) return;
  const list = Array.isArray(devices) ? devices : [];
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = list.map(d => `
    <div class="setup-step ${d.has_yam ? 'ok' : 'warn'}" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
      <div style="flex:1; min-width:170px">
        <b>${esc(d.name || 'станция')}</b>
        <div class="muted small">${esc(d.ip)} · найдена: ${esc(d.source || 'сеть')}${d.model ? ' · ' + esc(d.model) : ''}</div>
        ${d.has_yam ? '' : '<span class="advice">YAM не установлен: панель на этой колонке почти ничего не увидит. Сначала рут, затем установка YAM из «Чек-листа».</span>'}
      </div>
      ${d.has_yam ? '<span class="chip">YAM</span>' : '<span class="chip">без YAM</span>'}
      ${d.ssh ? '<span class="chip">SSH</span>' : ''}
      <button class="btn sm" data-pick="${esc(d.ip)}">Использовать</button>
    </div>`).join('');
  box.querySelectorAll('[data-pick]').forEach(button => button.onclick = () => {
    const ip = button.dataset.pick;
    $('cfgIp').value = ip;
    $('cfgSave').click();
    notify($('cfgStatus'), 'выбран IP станции: ' + ip, 'ok');
  });
}

async function detectStation(silent = false) {
  if (detecting) return null;
  detecting = true;
  const btn = $('btnAutoDetect');
  const svg = btn && btn.querySelector('svg');
  if (svg) svg.style.animation = 'spin 1s linear infinite';
  if (!silent) {
    $('statusText').textContent = 'ищу колонку в сети…';
    $('dot').className = 'dot pulse';
  }
  try {
    const result = await api('/api/detect', { method: 'POST', body: '{}' });
    renderDetectList(result.devices);
    if (result.found) {
      $('ipText').textContent = result.ip;
      $('cfgIp').value = result.ip;
      if (!silent) notify($('cfgStatus'), 'найдена колонка: ' + result.ip, 'ok');
    } else if ((result.devices || []).length) {
      $('statusText').textContent = 'найдены станции без YAM: ' + result.devices.length;
      $('dot').className = 'dot off';
      if (!silent) notify($('cfgStatus'), 'найдены станции без YAM — на них нужен рут и YAM', 'error');
    } else if (!silent) {
      $('statusText').textContent = 'колонка не найдена в подсети';
      $('dot').className = 'dot off';
      if (!silent) notify($('cfgStatus'), 'колонка в сети не найдена', 'error');
    }
    return result;
  } catch (error) {
    if (!silent) {
      $('statusText').textContent = 'поиск не удался: ' + error.message;
      notify($('cfgStatus'), 'поиск не удался: ' + error.message, 'error');
    }
    return { found: false };
  } finally {
    detecting = false;
    if (svg) svg.style.animation = '';
  }
}

$('btnAutoDetect').onclick = () => detectStation(false);
$('cfgDetect').onclick = () => detectStation(false);

/* ------------------------------ статус ------------------------------ */
async function refreshStatus() {
  try {
    const data = await api('/api/status');
    state.status = data;

    const online = data.reachable;
    const found = online && data.yam_online;
    $('dot').className = 'dot ' + (found ? 'on' : online ? 'pulse' : 'off');
    $('statusText').textContent = online ? (found ? 'колонка на связи' : 'связь есть, YAM недоступен') : 'нет связи';
    $('ipText').textContent = data.station_ip || '—';

    $('cUptime').textContent = online ? secondsToHuman(data.uptime_sec) : '—';
    $('cHost').textContent = data.host || '—';
    $('cTemp').textContent = data.cpu_temp ? data.cpu_temp + ' °C' : '—';
    $('cMem').textContent = data.mem_used_mb ? `${data.mem_used_mb} / ${data.mem_total_mb} МБ` : '—';
    $('cMemBar').style.width = (data.mem_percent || 0) + '%';
    $('cDisk').textContent = data.disk_used ? `${data.disk_used} / ${data.disk_size} (${data.disk_percent}%)` : '—';
    $('cDiskBar').style.width = (data.disk_percent || 0) + '%';

    $('cYam').textContent = data.yam_online ? 'работает' : 'недоступен';
    $('cYamSub').textContent = data.device ? `${prettyModel(data.device.model)} · ${data.device.architecture}` : (data.yam_error || '—');
    $('cPlugins').textContent = data.plugins === null ? '—' : data.plugins;
    $('cPluginsSub').textContent = data.plugins_running ? `${data.plugins_running} запущено` : 'нет запущенных';

    $('svcAdb').textContent = data.adb ? 'работает (PID ' + data.adb + ')' : 'остановлен';
    $('svcSsh').textContent = data.ssh ? 'работает (PID ' + data.ssh + ')' : 'остановлен';
    $('svcYam').textContent = data.yamd ? 'работает (PID ' + data.yamd + ')' : 'остановлен';
    $('svcKernel').textContent = data.kernel || '—';

    const features = (data.device && data.device.features) || [];
    $('features').innerHTML = features.length
      ? features.map(f => `<span class="chip">${esc(f)}</span>`).join('')
      : '<span class="muted small">нет данных (демон YAM недоступен)</span>';

    if (data.device) {
      /* В шапке — только название модели (две строки вместо многоточия),
         архитектура уходит в подсказку: в узком сайдбаре она не помещается. */
      $('brandModel').textContent = prettyModel(data.device.model);
      $('brandModel').title = `${prettyModel(data.device.model)} · ${data.device.architecture}`;
      document.title = `Панель Станции — ${prettyModel(data.device.model)}`;
      setDevicePhoto(data.device.model);
      setDeviceSpecs(data.device.model);
    }
    adaptTabsToFeatures(features);

    $('linkYam').href = `http://${data.station_ip}:${state.settingsYamPort || 8099}/`;
    $('overviewUpdated').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
  } catch (error) {
    $('dot').className = 'dot off';
    $('statusText').textContent = 'ошибка: ' + error.message;
  }
}

/* Вкладки и блоки по железу модели: что колонка не умеет — то не показываем.
   Возможности приходят из YAM /api/device → features (led.*, display.clock.*),
   так панель честно живёт и на моделях без LED-кольца (Лайт и др.). */
function adaptTabsToFeatures(features) {
  const list = features || [];
  const hasLed = list.some(f => String(f).startsWith('led.'));
  const hasClock = list.some(f => String(f).startsWith('led.ring.') || String(f).includes('clock'));
  document.querySelectorAll('[data-tab="led"]').forEach(btn => { btn.hidden = !hasLed; });
  const clockBlock = $('clockBlock');
  if (clockBlock) clockBlock.hidden = !hasClock;
}

/* ---------------------------- быстрые команды ---------------------------- */
document.querySelectorAll('[data-quick]').forEach(button => {
  button.onclick = async () => {
    const output = $('quickOut');
    output.textContent = '$ ' + button.dataset.quick + '\n…';
    try {
      const result = await api('/api/exec', { method: 'POST', body: JSON.stringify({ command: button.dataset.quick }) });
      output.textContent = '$ ' + button.dataset.quick + '\n' + (result.stdout || '') + (result.stderr ? '\n[stderr] ' + result.stderr : '');
    } catch (error) {
      output.textContent = 'ошибка: ' + error.message;
    }
  };
});

/* ------------------------------ терминал ------------------------------ */
function connectTerminal() {
  if (state.term) { state.term.dispose(); state.term = null; }
  if (state.socket) { try { state.socket.close(); } catch (_) {} }

  const term = new Terminal({
    fontFamily: '"JetBrains Mono", ui-monospace, Consolas, monospace',
    fontSize: 13,
    cursorBlink: true,
    scrollback: 4000,
    theme: {
      background: '#05070c',
      foreground: '#c9d4ec',
      cursor: '#8b7cff',
      cursorAccent: '#05070c',
      selectionBackground: 'rgba(139,124,255,.3)',
      black: '#0b0e14', red: '#fb7185', green: '#34d399', yellow: '#fbbf24',
      blue: '#60a5fa', magenta: '#c084fc', cyan: '#22d3ee', white: '#edf0fa'
    }
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($('term'));
  fit.fit();
  state.term = term;
  state.fit = fit;

  const protocol = location.protocol === 'https:' ? 'wss://' : 'ws://';
  const socket = new WebSocket(protocol + location.host + '/ws/console');
  state.socket = socket;

  socket.onopen = () => term.writeln('\x1b[38;2;139;124;255m● \x1b[0mсоединение с колонкой…\r\n');
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === 'data') term.write(message.data);
    else if (message.type === 'error') term.writeln('\r\n\x1b[31m' + message.message + '\x1b[0m');
  };
  socket.onclose = () => term.writeln('\r\n\x1b[33m[соединение закрыто — «Переподключить»]\x1b[0m\r\n');

  term.onData(data => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data }));
  });

  const resize = () => {
    fit.fit();
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    }
  };
  new ResizeObserver(() => resize()).observe($('term'));
  term.onResize(resize);
}

$('termReconnect').onclick = () => connectTerminal();
$('termClear').onclick = () => state.term && state.term.clear();
document.querySelectorAll('[data-cmd]').forEach(button => {
  button.onclick = () => {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify({ type: 'input', data: button.dataset.cmd + '\r' }));
      state.term && state.term.focus();
    }
  };
});

/* ------------------------------- пакеты ------------------------------- */
async function loadPlugins() {
  const list = $('pkgList');
  try {
    const plugins = await yam('/plugins');
    if (!plugins.length) {
      list.innerHTML = '<div class="panel muted small">Пакетов нет. Установи из реестра или загрузи .yap.</div>';
      return;
    }
    list.innerHTML = plugins.map(plugin => {
      const m = plugin.manifest || {};
      const id = esc(m.id);
      const running = plugin.running;
      const enabled = plugin.enabled;
      const service = plugin.service_enabled === true;
      const compatible = plugin.compatible !== false;
      const badges = [
        enabled ? '<span class="badge on">включён</span>' : '<span class="badge">выключен</span>',
        running ? '<span class="badge run">запущен</span>' : '',
        service ? '<span class="badge">service</span>' : '',
        compatible ? '' : `<span class="badge bad" title="${esc(plugin.incompatibility || '')}">несовместим</span>`
      ].join('');
      const launch = service
        ? `<button class="btn sm" data-act="service/start" data-id="${id}" ${compatible ? '' : 'disabled'}>${running ? 'Перезапустить' : 'Запустить'}</button>`
        : `<button class="btn sm" data-act="invoke" data-id="${id}" ${compatible ? '' : 'disabled'}>Запустить</button>`;
      return `<div class="pkg">
        <div class="pkg-info">
          <b>${esc(m.name || m.id)}</b>${badges}
          <div class="meta">${id} · v${esc(m.version || '?')}</div>
          <div class="desc">${esc(m.description || '')}</div>
        </div>
        <div class="pkg-actions">
          <button class="btn sm" data-config="${id}">Настройки</button>
          <button class="btn sm" data-act="toggle" data-id="${id}">${enabled ? 'Выключить' : 'Включить'}</button>
          ${launch}
          ${running ? `<button class="btn sm" data-act="stop" data-id="${id}">Стоп</button>` : ''}
          <button class="btn sm danger" data-act="delete" data-id="${id}">Удалить</button>
        </div>
      </div>`;
    }).join('');

    list.querySelectorAll('button[data-config]').forEach(button => {
      button.onclick = () => openPluginConfig(button.dataset.config);
    });

    list.querySelectorAll('button[data-act]').forEach(button => {
      button.onclick = async () => {
        const { id, act } = button.dataset;
        button.disabled = true;
        try {
          const suffix = act === 'delete' ? '' : '/' + act;
          const method = act === 'delete' ? 'DELETE' : 'POST';
          await yam(`/plugins/${encodeURIComponent(id)}${suffix}`, { method, body: method === 'POST' ? '{}' : undefined });
          notify($('pkgStatus'), `пакет ${id}: ${act} — готово`, 'ok');
          await loadPlugins();
        } catch (error) {
          notify($('pkgStatus'), `ошибка (${act}): ${error.message}`, 'error');
          button.disabled = false;
        }
      };
    });
  } catch (error) {
    list.innerHTML = `<div class="panel muted small">Не удалось получить список: ${esc(error.message)}</div>`;
  }
}

function settingLabel(key, definition) {
  return definition.label || definition.title || key;
}

function settingInput(key, definition, value) {
  const type = definition.type || 'string';
  const label = document.createElement('label');
  label.className = 'config-field';
  const caption = document.createElement('span');
  caption.textContent = settingLabel(key, definition);
  if (definition.description) {
    const help = document.createElement('small');
    help.textContent = definition.description;
    caption.appendChild(help);
  }
  label.appendChild(caption);

  let input;
  if (type === 'boolean') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = Boolean(value);
  } else if (Array.isArray(definition.enum)) {
    input = document.createElement('select');
    input.className = 'input';
    definition.enum.forEach(option => {
      const item = document.createElement('option');
      item.value = String(option);
      item.textContent = String(option);
      item.selected = option === value;
      input.appendChild(item);
    });
  } else {
    input = document.createElement('input');
    input.className = 'input';
    input.type = type === 'integer' || type === 'number' ? 'number' : 'text';
    input.value = type === 'object' || type === 'array' ? JSON.stringify(value ?? (type === 'array' ? [] : {})) : String(value ?? '');
    if (definition.minimum !== undefined) input.min = definition.minimum;
    if (definition.maximum !== undefined) input.max = definition.maximum;
    if (definition.step !== undefined) input.step = definition.step;
  }
  input.dataset.key = key;
  input.dataset.type = type;
  label.appendChild(input);
  return label;
}

function valuesFromFields(schema) {
  const values = {};
  document.querySelectorAll('#pluginConfigFields [data-key]').forEach(input => {
    const key = input.dataset.key;
    const type = input.dataset.type;
    if (type === 'boolean') values[key] = input.checked;
    else if (type === 'integer') values[key] = Number.parseInt(input.value, 10);
    else if (type === 'number') values[key] = Number(input.value);
    else if (type === 'array' || type === 'object') values[key] = JSON.parse(input.value || (type === 'array' ? '[]' : '{}'));
    else values[key] = input.value;
  });
  return values;
}

async function openPluginConfig(id) {
  try {
    const data = await api(`/api/plugins/${encodeURIComponent(id)}/settings`);
    state.configPluginId = id;
    state.configSchema = data.schema || {};
    $('pluginConfigPanel').hidden = false;
    $('pluginConfigTitle').textContent = `Настройки: ${data.manifest.name || id}`;
    $('pluginConfigFields').innerHTML = '';
    const keys = Object.keys(state.configSchema);
    if (!keys.length) {
      $('pluginConfigFields').innerHTML = '<span class="muted small">Плагин не объявил схему настроек. Используй JSON ниже — значения всё равно доступны через alice.settings.</span>';
    } else {
      keys.forEach(key => $('pluginConfigFields').appendChild(settingInput(key, state.configSchema[key], (data.values || {})[key])));
    }
    $('pluginConfigJson').value = JSON.stringify(data.values || {}, null, 2);
    const restart = data.running ? ' Плагин сейчас запущен — после сохранения перезапусти его.' : '';
    $('pluginConfigHelp').textContent = `API: alice.settings.get(key), alice.settings.all().${restart}`;
    $('pluginConfigPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    notify($('pkgStatus'), 'ошибка настроек: ' + error.message, 'error');
  }
}

$('pluginConfigClose').onclick = () => { $('pluginConfigPanel').hidden = true; state.configPluginId = null; };
$('pluginConfigSave').onclick = async () => {
  if (!state.configPluginId) return;
  try {
    let values;
    const fields = document.querySelectorAll('#pluginConfigFields [data-key]');
    values = fields.length ? valuesFromFields(state.configSchema) : JSON.parse($('pluginConfigJson').value || '{}');
    $('pluginConfigJson').value = JSON.stringify(values, null, 2);
    const result = await api(`/api/plugins/${encodeURIComponent(state.configPluginId)}/settings`, { method: 'PUT', body: JSON.stringify({ values }) });
    notify($('pluginConfigStatus'), result.restart_required ? 'сохранено. Перезапусти плагин.' : 'сохранено', 'ok');
  } catch (error) {
    notify($('pluginConfigStatus'), 'ошибка: ' + error.message, 'error');
  }
};

/* ------------------------------ подсветка ------------------------------ */
const PRESETS = [
  { name: 'Выключено', colors: ['#000000'] },
  { name: 'Красный', colors: ['#FF0000'] },
  { name: 'Радуга', colors: ['#FF0000', '#FF8000', '#FFFF00', '#00FF00', '#00BFFF', '#8000FF'] },
  { name: 'Чёрно-красный', colors: ['#000000', '#FF0000'] },
  { name: 'Неон', colors: ['#00FFFF', '#FF00FF'] },
  { name: 'Синий-белый', colors: ['#0040FF', '#FFFFFF'] },
  { name: 'Алиса', colors: ['#7A55FF', '#FF3B5C', '#FFD166', '#2ECCC0', '#7A55FF', '#FF3B5C'] }
];

function ringColors(list) {
  const count = state.ledCount || list.length || 6;
  return Array.from({ length: count }, (_, index) => hex(list[index % list.length]));
}

function buildLedGrid(count) {
  const grid = $('ledGrid');
  const previous = state.ledColors.slice();
  grid.innerHTML = '';
  state.ledColors = Array.from({ length: count }, (_, index) => previous[index] || '#7A55FF');
  state.ledColors.forEach((color, index) => {
    const wrap = document.createElement('div');
    wrap.className = 'led';
    wrap.innerHTML = `<input type="color" value="${color}"><span>${index + 1}</span>`;
    const input = wrap.querySelector('input');
    input.oninput = () => { state.ledColors[index] = input.value.toUpperCase(); };
    grid.appendChild(wrap);
  });
}

async function applyLed(colors) {
  try {
    await yam('/led', { method: 'POST', body: JSON.stringify({ colors }) });
  } catch (error) {
    alert('не удалось: ' + error.message);
  }
}

$('ledApply').onclick = () => applyLed(ringColors(state.ledColors));
$('ledOff').onclick = async () => {
  try { await yam('/led/off', { method: 'POST', body: '{}' }); } catch (error) { alert('не удалось: ' + error.message); }
};
$('ledAllSame').onclick = () => {
  const color = state.ledColors[0] || '#7A55FF';
  state.ledColors = state.ledColors.map(() => color);
  buildLedGrid(state.ledColors.length);
  applyLed(ringColors(state.ledColors));
};
$('ledRandom').onclick = () => {
  const random = () => '#' + Math.floor(Math.random() * 0xFFFFFF).toString(16).padStart(6, '0').toUpperCase();
  state.ledColors = state.ledColors.map(random);
  buildLedGrid(state.ledColors.length);
  applyLed(ringColors(state.ledColors));
};
$('ledFreeApply').onclick = () => {
  const list = $('ledFree').value.split(',').map(value => value.trim()).filter(Boolean);
  if (!list.length) return;
  applyLed(ringColors(list));
};

$('presets').innerHTML = PRESETS.map((preset, index) => {
  const gradient = preset.colors.length > 1
    ? `conic-gradient(${preset.colors.map((c, i) => `${c} ${i * (100 / preset.colors.length)}% ${(i + 1) * (100 / preset.colors.length)}%`).join(',')})`
    : preset.colors[0];
  return `<button class="preset" data-preset="${index}"><span class="swatch" style="background:${gradient}"></span>${preset.name}</button>`;
}).join('');

document.querySelectorAll('[data-preset]').forEach(button => {
  button.onclick = () => {
    const preset = PRESETS[Number(button.dataset.preset)];
    const colors = ringColors(preset.colors);
    state.ledColors = colors;
    buildLedGrid(colors.length);
    applyLed(colors);
  };
});

/* ------------------------------ LED-часы ------------------------------ */
const clockBrightness = $('clockBrightness');
const clockPreview = $('clockPreview');

function updateRangeFill(slider) {
  const percent = (slider.value - slider.min) / (slider.max - slider.min) * 100 + '%';
  slider.style.setProperty('--fill', percent);
}

$('clockBrightnessVal').textContent = clockBrightness.value;
updateRangeFill(clockBrightness);

function updateClockPreview() {
  clockPreview.textContent = state.clockOff ? '—:—' : ($('clockTime').value || '88:88');
  const b = Number(clockBrightness.value) / 255;
  clockPreview.style.setProperty('--clock-glow', Math.max(0.12, b).toFixed(2));
  clockPreview.style.opacity = (0.3 + 0.7 * b).toFixed(2);
  clockPreview.classList.toggle('dim', state.clockOff);
}

function normalizeClockTime(raw) {
  const out = String(raw).replace(/[^\d:]/g, '').slice(0, 5);
  const colon = out.indexOf(':');
  if (colon === -1) return out.slice(0, 4); // двоеточие допишется само при «Показать»
  return out.slice(0, colon + 1) + out.slice(colon + 1).replace(/:/g, '');
}

function scheduleClockApply() {
  clearTimeout(state.clockDebounce);
  state.clockDebounce = setTimeout(applyClockLive, 300);
}

async function applyClockLive() {
  const time = $('clockTime').value;
  const brightness = Number(clockBrightness.value);
  const valid = /^\d\d:\d\d$/.test(time);
  // неполный/пустой ввод — ничего не отправляем на колонку
  if (!valid) return;
  const last = state.clockLast;
  if (last.time === time && last.brightness === brightness) return;
  try {
    await yam('/clock', { method: 'PUT', body: JSON.stringify({ time, brightness }) });
    state.clockLast = { time, brightness };
  } catch (error) {
    notify($('clockStatus'), 'часы: ' + error.message, 'error');
  }
}

async function clockSet(time, brightness) {
  try {
    await yam('/clock', { method: 'PUT', body: JSON.stringify({ time, brightness: Number(brightness) || 170 }) });
    state.clockLast = { time, brightness: Number(brightness) };
    notify($('clockStatus'), `часы: ${time} · яркость ${brightness}`, 'ok');
  } catch (error) {
    notify($('clockStatus'), 'ошибка: ' + error.message, 'error');
  }
}

async function clockOffRequest() {
  try {
    await yam('/clock', { method: 'PUT', body: JSON.stringify({ off: true }) });
    state.clockOff = true;
    updateClockPreview();
    notify($('clockStatus'), 'часы выключены', 'ok');
  } catch (error) {
    notify($('clockStatus'), 'ошибка: ' + error.message, 'error');
  }
}

clockBrightness.oninput = () => {
  state.clockOff = false;
  $('clockBrightnessVal').textContent = clockBrightness.value;
  updateRangeFill(clockBrightness);
  updateClockPreview();
  scheduleClockApply();
};

$('clockTime').addEventListener('paste', event => {
  event.preventDefault();
  const text = (event.clipboardData || window.clipboardData).getData('text');
  clockBuffer = String(text).replace(/\D/g, '').slice(0, 4);
  renderClockBuffer();
});

// ручной ввод синхронизирует буфер (мобильные клавиатуры, автозаполнение)
$('clockTime').addEventListener('input', () => {
  // программное renderClockBuffer не кидает input, так что любой input — внешний
  clockBuffer = $('clockTime').value.replace(/\D/g, '').slice(0, 4);
  renderClockBuffer();
});

// посимвольный ввод: незаполненные разряды показываются нулями («1» → «10:00»)
let clockBuffer = '';

function setClockValue(time) {
  clockBuffer = String(time).replace(/\D/g, '').slice(0, 4);
  renderClockBuffer();
}

function renderClockBuffer() {
  const input = $('clockTime');
  const d = (clockBuffer + '0000').slice(0, 4);
  input.value = clockBuffer ? d.slice(0, 2) + ':' + d.slice(2) : '';
  const pos = clockBuffer.length + (clockBuffer.length > 2 ? 1 : 0);
  input.setSelectionRange(pos, pos);
  state.clockOff = false;
  updateClockPreview();
  scheduleClockApply();
}

$('clockTime').addEventListener('keydown', event => {
  if (event.key >= '0' && event.key <= '9' && clockBuffer.length < 4) {
    clockBuffer += event.key;
  } else if (event.key === 'Backspace') {
    clockBuffer = clockBuffer.slice(0, -1);
  } else if (event.key.length === 1) {
    event.preventDefault(); // буквы, знаки, вставка — блокируем
    return;
  } else {
    return; // Tab, стрелки, модификаторы — как обычно
  }
  event.preventDefault();
  renderClockBuffer();
});

$('clockShow').onclick = () => {
  const time = $('clockTime').value;
  if (!/^\d\d:\d\d$/.test(time)) {
    notify($('clockStatus'), 'формат: 2 цифры, двоеточие, 2 цифры — например 99:99', 'error');
    return;
  }
  state.clockOff = false;
  updateClockPreview();
  clockSet(time, clockBrightness.value);
};

$('clockNow').onclick = () => {
  const now = new Date();
  const time = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
  setClockValue(time);
  clockSet(time, clockBrightness.value);
  // «Сейчас» = реальное время: закрепление снимаем всегда (чекбокс мог
  // устареть, а фоновый цикл панели иначе вернёт старое время)
  $('clockPin').checked = false;
  clockPinSync();
};

$('clockOff').onclick = clockOffRequest;

document.querySelectorAll('[data-clock]').forEach(button => {
  button.onclick = () => {
    const time = button.dataset.clock;
    setClockValue(time);
    clockSet(time, clockBrightness.value);
  };
});

updateClockPreview();

/* закрепление времени на LED-часах: колонка сама возвращает реальное время,
   панель переподтверждает заданное значение каждые 5 с (см. server.py) */
async function clockPinSync() {
  const active = $('clockPin').checked;
  const time = $('clockTime').value;
  if (active && !/^\d\d:\d\d$/.test(time)) {
    $('clockPin').checked = false;
    notify($('clockStatus'), 'сначала введите время в формате ЧЧ:ММ', 'error');
    return;
  }
  try {
    const res = await api('/api/clock/pin', {
      method: 'POST',
      body: JSON.stringify({ active, time, brightness: Number(clockBrightness.value) })
    });
    notify($('clockStatus'), res.active
      ? `закреплено: ${res.time} — панель удерживает значение`
      : 'закрепление снято — колонка снова показывает реальное время', 'ok');
  } catch (error) {
    notify($('clockStatus'), 'ошибка закрепления: ' + error.message, 'error');
    $('clockPin').checked = false;
  }
}
$('clockPin').onchange = clockPinSync;
api('/api/clock/pin').then(pin => { $('clockPin').checked = !!pin.active; }).catch(() => {});

/* ------------------------------- датчики ------------------------------- */
async function refreshSensors() {
  try {
    const sensors = await yam('/sensors');
    const light = sensors.find(sensor => sensor.kind === 'ambient_light');
    const temperature = sensors.find(sensor => sensor.kind === 'temperature');
    $('sLight').textContent = light ? light.value.toFixed(2) + ' lx' : '—';
    $('sTemp').textContent = temperature ? temperature.value.toFixed(1) + ' °C' : '—';
    $('sensorsRaw').textContent = JSON.stringify(sensors, null, 2);

    if (temperature) { state.series.temp.push(temperature.value); if (state.series.temp.length > 120) state.series.temp.shift(); }
    if (light) { state.series.light.push(light.value); if (state.series.light.length > 120) state.series.light.shift(); }

    if ($('tab-sensors').classList.contains('active')) drawCharts();
  } catch (error) {
    $('sLight').textContent = '—';
    $('sTemp').textContent = '—';
    $('sensorsRaw').textContent = 'ошибка: ' + error.message;
  }
}

function drawChart(canvas, seriesKey, color, unit, cursorX) {
  const values = state.series[seriesKey];
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = 150;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  // сетка
  ctx.strokeStyle = 'rgba(255,255,255,.05)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = Math.round((height / 4) * i);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }

  if (values.length < 2) {
    ctx.fillStyle = '#6b7488';
    ctx.font = '12px "Segoe UI", system-ui';
    ctx.fillText('накапливаю данные…', 10, height / 2);
    return;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1) || 1;
  const yOf = i => height - 12 - ((values[i] - min) / span) * (height - 30);

  // линия+заливка: монотонная кубическая интерполяция — плавная кривая без выбросов за min/max
  ctx.beginPath();
  {
    const n = values.length;
    const slopes = [];
    for (let i = 0; i < n - 1; i++) slopes.push((values[i + 1] - values[i]) / step);
    const tangents = [slopes[0]];
    for (let i = 1; i < n - 1; i++) {
      if (slopes[i - 1] * slopes[i] <= 0) tangents.push(0);
      else {
        const w1 = 2 * step + step, w2 = step + 2 * step;
        tangents.push((w1 + w2) / (w1 / slopes[i - 1] + w2 / slopes[i]));
      }
    }
    tangents.push(slopes[n - 2]);
    ctx.moveTo(0, yOf(0));
    for (let i = 0; i < n - 1; i++) {
      const x0 = i * step, x1 = (i + 1) * step, y0 = yOf(i), y1 = yOf(i + 1);
      ctx.bezierCurveTo(x0 + step / 3, y0 + tangents[i] * step / 3, x1 - step / 3, y1 - tangents[i + 1] * step / 3, x1, y1);
    }
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.closePath();
  const gradient = ctx.createLinearGradient(0, 0, 0, height);
  gradient.addColorStop(0, color + '40');
  gradient.addColorStop(1, color + '00');
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.fillStyle = '#b9c5de';
  ctx.font = '12px ui-monospace, Consolas, monospace';
  ctx.fillText(`${values[values.length - 1].toFixed(1)} ${unit} · min ${min.toFixed(1)} · max ${max.toFixed(1)}`, 10, 16);

  // тултип по позиции курсора: линейная интерполяция между точками — курсор движется плавно
  if (cursorX == null) return;
  const f = Math.max(0, Math.min(values.length - 1, cursorX / step));
  const i0 = Math.min(values.length - 2, Math.floor(f));
  const frac = f - i0;
  const value = values[i0] + (values[i0 + 1] - values[i0]) * frac;
  const x = f * step;
  const y = height - 12 - ((value - min) / span) * (height - 30);

  ctx.strokeStyle = color + '55';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke();

  ctx.beginPath();
  ctx.fillStyle = color;
  ctx.arc(x, y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = '#090b10';
  ctx.lineWidth = 2;
  ctx.stroke();

  const ago = Math.round((values.length - 1 - f) * 3);
  const when = ago <= 1 ? 'сейчас' : ago + ' с назад';
  const line = `${value.toFixed(1)} ${unit} · ${when}`;
  ctx.font = '12px "Segoe UI", system-ui';
  const w = ctx.measureText(line).width + 18;
  const bx = Math.min(Math.max(x - w / 2, 4), width - w - 4);
  const by = y > 44 ? y - 38 : y + 14;
  ctx.fillStyle = 'rgba(15,19,28,.94)';
  ctx.strokeStyle = color + '88';
  ctx.lineWidth = 1;
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(bx, by, w, 25, 8); else ctx.rect(bx, by, w, 25);
  ctx.fill(); ctx.stroke();
  ctx.fillStyle = '#edf0fa';
  ctx.fillText(line, bx + 9, by + 17);
}

function drawCharts() {
  drawChart($('chartTemp'), 'temp', '#8b7cff', '°C', state.chartTips.get('temp'));
  drawChart($('chartLight'), 'light', '#22d3ee', 'lx', state.chartTips.get('light'));
}

['chartTemp', 'chartLight'].forEach(canvasId => {
  const canvas = $(canvasId);
  const key = canvasId === 'chartTemp' ? 'temp' : 'light';
  canvas.addEventListener('mousemove', event => {
    if (state.series[key].length < 2) return;
    state.chartTips.set(key, event.clientX - canvas.getBoundingClientRect().left);
    drawChart(canvas, key, key === 'temp' ? '#8b7cff' : '#22d3ee', key === 'temp' ? '°C' : 'lx', state.chartTips.get(key));
  });
  canvas.addEventListener('mouseleave', () => {
    state.chartTips.delete(key);
    drawChart(canvas, key, key === 'temp' ? '#8b7cff' : '#22d3ee', key === 'temp' ? '°C' : 'lx', null);
  });
});

/* ------------------------- автонастройка ------------------------- */
function setupRenderStep(step) {
  const cls = step.ok ? 'ok' : 'err';
  const mark = step.ok ? svgIcon('check', 'ic-lvl-ok') : svgIcon('x', 'ic-lvl-high');
  return `<div class="setup-step ${cls}"><b>${mark} ${esc(step.step)}:</b> ${esc(step.detail)}</div>`;
}

$('setupFull').onclick = async () => {
  const btn = $('setupFull');
  btn.disabled = true;
  $('setupSteps').innerHTML = '';
  notify($('setupStatus'), 'идёт автонастройка, это до минуты…');
  try {
    const res = await api('/api/setup/full', { method: 'POST', body: '{}' });
    $('setupSteps').innerHTML = (res.steps || []).map(setupRenderStep).join('');
    if (res.ok) {
      notify($('setupStatus'), 'автонастройка завершена — колонка настроена', 'ok');
      state.socket && state.socket.close();
      setTimeout(() => { connectTerminal(); refreshStatus(); }, 400);
      loadSettings();
    } else {
      notify($('setupStatus'), 'не готово: ' + (res.error || 'смотрите шаги выше'), 'error');
    }
  } catch (error) {
    notify($('setupStatus'), 'ошибка: ' + error.message, 'error');
  } finally {
    btn.disabled = false;
  }
};

$('setupKeyShow').onclick = async () => {
  try {
    const res = await api('/api/setup/ssh-key');
    if (res.exists) {
      $('setupSteps').innerHTML = setupRenderStep({ step: 'Публичный ключ панели', ok: true, detail: '<code style="word-break:break-all">' + res.public_key + '</code>' });
    } else {
      notify($('setupStatus'), 'ключ ещё не создан — запустите автонастройку', 'error');
    }
  } catch (error) {
    notify($('setupStatus'), 'ошибка: ' + error.message, 'error');
  }
};

/* --------------------------- мастер настройки ---------------------------
   Один прогон по всем ручкам панели: связь → станция → часы → громкость →
   голосовой мост → слово активации → телеметрия. Шаги складываются в
   .setup-steps тем же рендером, что и у автонастройки; ошибка одного шага
   не прерывает остальные. */
$('masterStart').onclick = async () => {
  const btn = $('masterStart');
  const steps = [];
  const render = () => { $('masterSteps').innerHTML = steps.map(setupRenderStep).join(''); };
  const push = (step, ok, detail) => { steps.push({ step, ok, detail: String(detail ?? '') }); render(); };
  const failedNow = message => { notify($('masterStatus'), message, 'error'); };

  btn.disabled = true;
  steps.length = 0;
  render();
  notify($('masterStatus'), 'идёт мастер настройки — не закрывайте вкладку…');

  try {
    // 1. связь: детект IP, портов, SSH-ключ (существующая автонастройка)
    let infra;
    try {
      infra = await api('/api/setup/full', { method: 'POST', body: '{}' });
    } catch (error) {
      failedNow('остановлено на связи: ' + error.message);
      return;
    }
    (infra.steps || []).forEach(step => steps.push({ step: 'Связь: ' + step.step, ok: step.ok, detail: String(step.detail ?? '') }));
    render();
    if (!infra.ok) {
      failedNow('остановлено: ' + (infra.error || 'колонка недоступна — проверьте, что они в одной сети'));
      return;
    }
    // порты могли смениться — переподключаем терминал и статус
    state.socket && state.socket.close();
    setTimeout(() => { connectTerminal(); refreshStatus(); }, 300);

    // 2. станция на связи
    try {
      const st = await api('/api/status');
      const model = st.device ? prettyModel(st.device.model) : 'модель неизвестна';
      push('Станция на связи', !!st.yam_online,
        st.yam_online ? `${model} · YAM отвечает` : (st.yam_error || 'YAM не отвечает — раздел «Система»'));
    } catch (error) {
      push('Станция на связи', false, error.message);
    }

    // 3. LED-часы: показать текущее время (без закрепления — колонка сама
    //    ходит по реальному времени; закрепление нужно только для чужого значения)
    try {
      const pin = await api('/api/clock/pin');
      if (pin.active) {
        push('LED-часы', true, `закреплено: ${pin.time || ''} — снять закрепление можно кнопкой «Сейчас»`);
      } else {
        const now = new Date();
        const time = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
        await yam('/clock', { method: 'PUT', body: JSON.stringify({ time, brightness: 170 }) });
        push('LED-часы', true, `${time} отправлено на кольцо, яркость 170`);
      }
    } catch (error) {
      push('LED-часы', false, error.message + ' — раздел «Подсветка»');
    }

    // 4. громкость
    try {
      const volume = await api('/api/volume');
      if (!volume.ok) throw new Error('нет данных');
      push('Громкость', true,
        `${volume.volume}%${volume.volume < 10 ? ' — очень тихо, поднимите ползунком в «Обзоре»' : ''}`);
    } catch (error) {
      push('Громкость', false, error.message);
    }

    // 5. голосовой мост (запускается сам, если ещё не готов)
    try {
      let bridge = await api('/api/bridge/status');
      if (!(bridge.running && bridge.has_token)) {
        await api('/api/bridge/setup', { method: 'POST', body: '{}' });
        bridge = await api('/api/bridge/status');
      }
      const llm = bridge.llm || {};
      const llmReady = !!(llm.deepseek_token || llm.deepseek_cookie || llm.openrouter_key);
      const ok = !!(bridge.running && bridge.has_token && llmReady);
      push('Голосовой мост', ok, ok
        ? `запущен: ${bridge.url || ('127.0.0.1:' + bridge.port)}`
        : 'не готов — «Система → Голосовые команды»: укажите токен моста и LLM');
    } catch (error) {
      push('Голосовой мост', false, error.message);
    }

    // 6. слово активации
    try {
      const wwStateNow = await ww('api/state');
      const words = (wwStateNow && wwStateNow.words) || [];
      const names = words
        .map(word => word.phrase || word.name || word.word || '')
        .filter(Boolean);
      const enabledCount = words.filter(word => word.enabled !== false).length;
      push('Слово активации', words.length > 0,
        words.length
          ? `${words.length} шт. (${enabledCount} вкл.): ${names.slice(0, 3).join(', ')}${names.length > 3 ? '…' : ''}`
          : 'слов нет — раздел «Слова активации»: запишите своё слово');
      if (words.length && !enabledCount) {
        steps[steps.length - 1].ok = false;
        steps[steps.length - 1].detail += ' — все слова выключены';
        render();
      }
    } catch (error) {
      push('Слово активации', false, 'служба слов не отвечает: ' + error.message);
    }

    // 7. телеметрия — информационный шаг, ничего не включает и не выключает
    try {
      const tel = await api('/api/telemetry');
      if (tel.ok) {
        const off = !(tel.telemetry_in_list || tel.fluent_in_list);
        push('Телеметрия', true, off
          ? 'отключена'
          : 'включена — выключается в «Системе → Телеметрия Яндекса» (после перезагрузки колонки)');
      } else {
        push('Телеметрия', true, 'состояние прочитать не удалось: ' + (tel.detail || ''));
      }
    } catch (error) {
      push('Телеметрия', true, 'инфо недоступно: ' + error.message);
    }

    // 8. итог
    const failed = steps.filter(step => !step.ok).length;
    push('Итог', failed === 0, failed === 0
      ? 'всё настроено. Дальше: «Подсветка» — часы и LED, «Слова активации» — своё слово, «Голосовые команды» — триггеры.'
      : `шагов с ошибкой: ${failed} — раскройте инструкцию под кнопкой и повторите`);
    notify($('masterStatus'), failed === 0
      ? 'мастер завершён — колонка настроена'
      : `мастер завершён, проблем: ${failed}`, failed === 0 ? 'ok' : 'error');
  } catch (error) {
    failedNow('непредвиденная ошибка мастера: ' + error.message);
  } finally {
    btn.disabled = false;
  }
};

/* ------------------------------- система ------------------------------- */
/* рекомендованный YAM-профиль подсветки по платформе колонки (YAM /api/device → model) */
function ledProfileForModel(model) {
  const m = String(model || '').toLowerCase();
  if (!m) return null;
  if (m.includes('mini2') || m === 'yandexmini_2') return 'mini2';
  if (m.includes('micro')) return 'micro';
  if (m.includes('plum')) return 'plum';
  if (m.includes('midi')) return 'midi';
  return null; // макс/станция/дуо и неизвестные — профиль не подбираем
}

async function loadSettings() {
  try {
    const [config, settings] = await Promise.all([api('/api/config'), yam('/settings')]);
    state.settings = settings;
    state.settingsYamPort = config.yam_port;
    $('cfgIp').value = config.station_ip;
    $('cfgSsh').value = config.ssh_port;
    $('cfgYam').value = config.yam_port;
    $('cfgUser').value = config.ssh_user;
    $('termPort').textContent = config.ssh_port;

    const led = settings.led || {};
    const recommended = ledProfileForModel(state.status?.device?.model);
    const autoKey = 'sp_ledAuto:' + (state.status?.device?.model || '?');
    if (recommended && led.profile !== recommended && !localStorage.getItem(autoKey)) {
      // автоподбор: один раз на модель; ручной выбор после этого не перебивается
      try {
        settings.led.profile = recommended;
        await yam('/settings', { method: 'PUT', body: JSON.stringify(settings) });
        localStorage.setItem(autoKey, '1');
        led.profile = recommended;
        notify($('yamStatus'), `профиль подсветки подобран по модели: ${recommended}`, 'ok');
      } catch (_) { /* подберём при следующей загрузке */ }
    }
    $('yamProfile').value = led.profile || 'mini2';
    $('yamCount').value = led.count || 6;
    $('yamBinaries').checked = !!settings.allow_plugin_binaries;
    $('yamUpdates').checked = !(settings.updates && settings.updates.enabled === false);
    state.ledCount = Number(led.count) || 6;
    buildLedGrid(state.ledCount);
  } catch (error) {
    notify($('yamStatus'), 'настройки демона недоступны: ' + error.message, 'error');
  }
}

/* ---------------------- каталог пакетов (реестр yapkg) ---------------------- */
let catalogItems = [];

async function loadCatalog() {
  const box = $('catList');
  box.textContent = 'загружаю реестр…';
  try {
    const data = await api('/api/packages/catalog');
    catalogItems = data.items || [];
    renderCatalog(catalogItems);
  } catch (error) {
    box.textContent = 'ошибка: ' + error.message;
  }
}

function renderCatalog(items) {
  $('catCount').textContent = items.length + ' шт';
  $('catList').innerHTML = items.length ? items.map(item => `
    <div class="setup-step" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
      <div style="flex:1; min-width:220px">
        <b>${esc(item.name)}</b> <span class="muted small">v${esc(item.version || '?')} · id ${esc(item.id)}</span>
        <div class="muted small" style="margin-top:2px">${esc(item.description || '')}</div>
      </div>
      ${item.running
        ? '<span class="chip">работает</span>'
        : item.installed
          ? '<span class="chip">установлен</span>'
          : `<button class="btn sm primary" data-catinstall="${esc(item.id)}">Установить</button>`}
    </div>`).join('') : '<p class="muted small">пусто</p>';
  $('catList').querySelectorAll('[data-catinstall]').forEach(btn => btn.onclick = () => installCatalogItem(btn.dataset.catinstall, btn));
}

async function installCatalogItem(id, btn) {
  const busy = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'ставлю…';
  try {
    const result = await api('/api/packages/catalog/install', { method: 'POST', body: JSON.stringify({ id }) });
    notify($('pkgStatus'), `пакет ${id} установлен (${result.version || ''}), автозапуск включён`, 'ok');
    await loadCatalog();
    await loadPlugins();
  } catch (error) {
    notify($('pkgStatus'), 'ошибка: ' + error.message, 'error');
    btn.disabled = false;
    btn.textContent = busy;
  }
}

$('catRefresh').onclick = loadCatalog;
$('catSearch').oninput = () => {
  const q = $('catSearch').value.trim().toLowerCase();
  renderCatalog(q ? catalogItems.filter(i => (i.name + ' ' + i.id + ' ' + i.description).toLowerCase().includes(q)) : catalogItems);
};

/* ---------------------- минидампы падений демонов ---------------------- */
async function loadMinidumps() {
  const box = $('mdList');
  try {
    const data = await api('/api/system/minidumps');
    $('mdCount').textContent = `${data.items.length} шт${data.total ? ' · ' + data.total : ''}`;
    box.textContent = data.items.length
      ? data.items.map(i => `${i.date}  ${String(i.size).padStart(8)}  ${i.name}`).join('\n')
      : 'падений не зафиксировано — каталог пуст';
  } catch (error) {
    box.textContent = 'ошибка: ' + error.message;
  }
}
$('mdRefresh').onclick = loadMinidumps;

/* ------------------------- аудит безопасности -------------------------
   Только чтение: сканируем порты колонки с ПК, слушающих демонов и флаги
   dropbear; показываем находки с подсказками. Ничего не выключаем. */
const SEC_MARK = { high: 'warn', warn: 'warn', info: 'info', ok: 'check' };

async function runSecurityAudit() {
  const box = $('secFindings');
  $('secChip').textContent = 'проверяю…';
  box.textContent = 'сканирую порты и демонов колонки…';
  try {
    const data = await api('/api/security/audit');
    box.innerHTML = (data.findings || []).map(f => {
      const lvl = f.level === 'ok' ? 'ok' : f.level === 'high' ? 'err' : f.level;
      const iconCls = f.level === 'high' ? 'ic-lvl-high' : f.level === 'warn' ? 'ic-lvl-warn' : f.level === 'ok' ? 'ic-lvl-ok' : 'ic-lvl-info';
      return `<div class="setup-step ${lvl}"><b>${svgIcon(SEC_MARK[f.level] || 'info', iconCls)} ${esc(f.title)}</b>` +
        (f.detail ? `${esc(f.detail)}` : '') +
        (f.advice ? `<span class="advice">Что делать: ${esc(f.advice)}</span>` : '') + `</div>`;
    }).join('');
    const bad = (data.findings || []).filter(f => f.level === 'high').length;
    const warn = (data.findings || []).filter(f => f.level === 'warn').length;
    $('secChip').textContent = bad ? `${bad} критично` : warn ? `${warn} с осторожностью` : 'порядок';
  } catch (error) {
    box.textContent = 'ошибка: ' + error.message;
    $('secChip').textContent = 'ошибка';
  }
}
$('secAudit').onclick = runSecurityAudit;

/* -------------------- редактор LED-анимаций --------------------
   Кадры = палитры, раскладывающиеся на кольцо как пресеты; панель
   гонит их циклом через YAM /led. Играет, пока открыта панель. */
function ledAnimLoad() {
  try { state.animFrames = JSON.parse(localStorage.getItem('sp_ledAnim') || 'null') || null; } catch (_) { state.animFrames = null; }
  if (!Array.isArray(state.animFrames) || !state.animFrames.length) {
    state.animFrames = [
      { colors: ['#7A55FF', '#FF5A8A'], ms: 400 },
      { colors: ['#FF5A8A', '#7A55FF'], ms: 400 },
    ];
  }
  ledAnimRender();
}

function ledAnimSave() {
  localStorage.setItem('sp_ledAnim', JSON.stringify(state.animFrames));
}

function ledAnimRender() {
  const box = $('ledAnimFrames');
  box.innerHTML = state.animFrames.map((frame, i) => `
    <div class="setup-step" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
      <b style="min-width:34px">#${i + 1}</b>
      <div class="led-grid" style="gap:6px">${frame.colors.map((c, j) =>
        `<div class="led"><input type="color" value="${c}" data-animc="${i}.${j}"><span>${j + 1}</span></div>`).join('')}</div>
      <label class="row" style="gap:5px; font-size:12px; color:var(--muted)"><input type="number" min="80" max="10000" step="20" value="${frame.ms}" data-animms="${i}" style="width:84px"> мс</label>
      <button class="btn sm ghost" data-animup="${i}" title="выше">${svgIcon('up')}</button>
      <button class="btn sm ghost" data-animdown="${i}" title="ниже">${svgIcon('down')}</button>
      <button class="btn sm ghost" data-animdel="${i}" title="удалить">${svgIcon('x')}</button>
    </div>`).join('') || '<p class="muted small">Кадров нет — добавь первый.</p>';
  $('ledAnimChip').textContent = state.animFrames.length + ' кадр(ов)';
  box.querySelectorAll('[data-animc]').forEach(input => input.oninput = () => {
    const [fi, ci] = input.dataset.animc.split('.').map(Number);
    state.animFrames[fi].colors[ci] = input.value.toUpperCase();
    ledAnimSave();
  });
  box.querySelectorAll('[data-animms]').forEach(input => input.onchange = () => {
    state.animFrames[+input.dataset.animms].ms = Math.max(80, Number(input.value) || 400);
    ledAnimSave();
  });
  box.querySelectorAll('[data-animup]').forEach(btn => btn.onclick = () => {
    const i = +btn.dataset.animup;
    if (i === 0) return;
    [state.animFrames[i - 1], state.animFrames[i]] = [state.animFrames[i], state.animFrames[i - 1]];
    ledAnimSave(); ledAnimRender();
  });
  box.querySelectorAll('[data-animdown]').forEach(btn => btn.onclick = () => {
    const i = +btn.dataset.animdown;
    if (i >= state.animFrames.length - 1) return;
    [state.animFrames[i + 1], state.animFrames[i]] = [state.animFrames[i], state.animFrames[i + 1]];
    ledAnimSave(); ledAnimRender();
  });
  box.querySelectorAll('[data-animdel]').forEach(btn => btn.onclick = () => {
    state.animFrames.splice(+btn.dataset.animdel, 1);
    ledAnimSave(); ledAnimRender();
  });
}

let ledAnimToken = 0;
$('ledAnimAdd').onclick = () => {
  const last = state.animFrames[state.animFrames.length - 1];
  state.animFrames.push({ colors: (last?.colors || ['#7A55FF', '#FF5A8A']).slice(), ms: last?.ms || 400 });
  ledAnimSave(); ledAnimRender();
};
$('ledAnimPlay').onclick = async () => {
  if (!state.animFrames.length) { $('ledAnimStatus').textContent = 'Сначала добавь кадры.'; return; }
  const token = ++ledAnimToken;
  $('ledAnimChip').textContent = 'играет…';
  let i = 0;
  while (token === ledAnimToken) {
    const frame = state.animFrames[i % state.animFrames.length];
    i += 1;
    try {
      await yam('/led', { method: 'POST', body: JSON.stringify({ colors: ringColors(frame.colors) }) });
    } catch (error) {
      $('ledAnimStatus').textContent = 'колонка недоступна: ' + error.message;
      $('ledAnimChip').textContent = 'ошибка';
      return;
    }
    await new Promise(resolve => setTimeout(resolve, Math.max(80, frame.ms)));
  }
};
$('ledAnimStop').onclick = () => {
  ledAnimToken += 1;
  $('ledAnimChip').textContent = 'остановлено';
};

/* -------------------- ночной профиль часов -------------------- */
async function loadNightClock() {
  try {
    const data = await api('/api/clock/night');
    $('nightEnabled').checked = !!data.enabled;
    $('nightStart').value = data.start || '23:00';
    $('nightEnd').value = data.end || '07:00';
    $('nightBrightness').value = data.night_brightness ?? 12;
    $('nightDay').value = data.day_brightness ?? 170;
    $('nightStatus').textContent = data.enabled
      ? (data.was_night ? 'сейчас ночное окно — держу низкую яркость' : 'включено, ждёт окна ' + data.start + '–' + data.end)
      : 'выключено';
  } catch (error) {
    $('nightStatus').textContent = 'ошибка: ' + error.message;
  }
}

$('nightSave').onclick = async () => {
  const status = $('nightStatus');
  try {
    const data = await api('/api/clock/night', {
      method: 'POST',
      body: JSON.stringify({
        enabled: $('nightEnabled').checked,
        start: $('nightStart').value.trim(),
        end: $('nightEnd').value.trim(),
        night_brightness: Number($('nightBrightness').value),
        day_brightness: Number($('nightDay').value),
      })
    });
    status.textContent = data.enabled ? 'сохранено — включено, окно ' + data.start + '–' + data.end : 'сохранено — выключено';
  } catch (error) {
    status.textContent = 'ошибка: ' + error.message;
  }
};

/* -------------------- системные звуки колонки -------------------- */
async function loadSounds() {
  const box = $('sndList');
  try {
    const data = await api('/api/sounds');
    const sounds = data.sounds || [];
    $('sndChip').textContent = sounds.length + ' шт';
    box.innerHTML = sounds.map(s => `
      <div class="setup-step" style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
        <b style="min-width:250px">${esc(s.name)}</b>
        <span class="muted small">${s.dir} · ${s.size} Б${s.backup ? ' · есть бэкап' : ''}</span>
        <button class="btn sm ghost" data-sndplay="${s.name}" title="прослушать">${svgIcon('play')}</button>
        <label class="btn sm file" style="padding:3px 10px">заменить<input type="file" accept=".opus,.ogg,.wav,.mp3,.m4a,.flac" data-sndfile="${s.name}" hidden></label>
        ${s.backup ? `<button class="btn sm ghost" data-sndrestore="${s.name}" title="вернуть оригинал">${svgIcon('undo')}</button>` : ''}
      </div>`).join('');
    box.querySelectorAll('[data-sndplay]').forEach(btn => btn.onclick = () => {
      const audio = $('sndAudio');
      audio.style.display = '';
      audio.src = '/api/sounds/file?name=' + encodeURIComponent(btn.dataset.sndplay);
      audio.play().catch(() => {});
    });
    box.querySelectorAll('[data-sndfile]').forEach(input => input.onchange = async () => {
      const name = input.dataset.sndfile;
      const file = input.files[0];
      if (!file) return;
      $('sndStatus').textContent = 'конвертирую и заливаю ' + name + '…';
      const form = new FormData();
      form.append('name', name);
      form.append('file', file);
      try {
        await api('/api/sounds/upload', { method: 'POST', body: form });
        $('sndStatus').textContent = 'заменено: ' + name + '. Оригинал в бэкапе.';
        await loadSounds();
      } catch (error) {
        $('sndStatus').textContent = 'ошибка: ' + error.message;
      }
    });
    box.querySelectorAll('[data-sndrestore]').forEach(btn => btn.onclick = async () => {
      $('sndStatus').textContent = 'восстанавливаю ' + btn.dataset.sndrestore + '…';
      try {
        await api('/api/sounds/restore', { method: 'POST', body: JSON.stringify({ name: btn.dataset.sndrestore }) });
        $('sndStatus').textContent = 'оригинал возвращён: ' + btn.dataset.sndrestore;
        await loadSounds();
      } catch (error) {
        $('sndStatus').textContent = 'ошибка: ' + error.message;
      }
    });
  } catch (error) {
    box.textContent = 'ошибка: ' + error.message;
    $('sndChip').textContent = 'ошибка';
  }
}
$('sndRefresh').onclick = loadSounds;

/* ------------------------- чек-лист готовности -------------------------
   Панель проверяет пакеты ПК, инструменты (ffmpeg/ffplay/yt-dlp), SSH-ключ,
   плагины на колонке и демон слов; недостающее ставится кнопкой отсюда. */
async function loadChecklist() {
  const box = $('clList');
  box.textContent = 'проверяю…';
  $('clUpdated').textContent = '';
  try {
    renderChecklist(await api('/api/checklist'));
    $('clUpdated').textContent = 'проверено ' + new Date().toLocaleTimeString();
  } catch (error) {
    box.textContent = 'ошибка: ' + error.message;
  }
}

function renderChecklist(data) {
  const items = data.items || [];
  const done = items.filter(i => i.status === 'ok').length;
  $('clSummary').textContent = `${done} из ${items.length} готово`;
  $('clList').innerHTML = items.map(item => {
    const ok = item.status === 'ok';
    const icon = ok ? svgIcon('check', 'ic-lvl-ok') : svgIcon('warn', 'ic-lvl-warn');
    return `<div class="setup-step ${ok ? 'ok' : 'warn'}" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
      <b style="min-width:24px">${icon}</b>
      <div style="flex:1; min-width:220px">
        <b>${esc(item.title)}</b>
        <div class="muted small" style="margin-top:2px">${esc(item.detail || '')}${item.hint ? ' — ' + esc(item.hint) : ''}</div>
      </div>
      ${item.action ? `<button class="btn sm primary" data-clinstall="${item.id}">${esc(item.action)}</button>` : ''}
    </div>`;
  }).join('');
  $('clList').querySelectorAll('[data-clinstall]').forEach(btn => btn.onclick = () => installChecklistItem(btn.dataset.clinstall, btn));
}

async function installChecklistItem(id, btn) {
  const status = $('clStatus');
  const busy = btn.textContent;
  btn.disabled = true;
  status.textContent = 'Устанавливаю: ' + id + (id === 'ffmpeg' ? ' — качается большой архив, это может занять пару минут…' : '…');
  try {
    const result = await api('/api/checklist/install', { method: 'POST', body: JSON.stringify({ id }) });
    status.textContent = result.output || 'готово';
    if (result.items) renderChecklist(result); else await loadChecklist();
  } catch (error) {
    status.textContent = 'Ошибка: ' + error.message;
  } finally {
    btn.disabled = false;
    btn.textContent = busy;
  }
}

$('clRefresh').onclick = loadChecklist;

$('cfgSave').onclick = async () => {
  try {
    const ip = $('cfgIp').value.trim();
    if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) {
      notify($('cfgStatus'), 'IP выглядит некорректно, пример: 192.168.0.172', 'error');
      return;
    }
    await api('/api/config', {
      method: 'PUT',
      body: JSON.stringify({
        station_ip: ip,
        ssh_port: Number($('cfgSsh').value),
        yam_port: Number($('cfgYam').value),
        ssh_user: $('cfgUser').value.trim()
      })
    });
    notify($('cfgStatus'), 'сохранено — переподключаюсь', 'ok');
    state.socket && state.socket.close();
    setTimeout(() => { connectTerminal(); refreshStatus(); refreshSensors(); }, 500);
  } catch (error) {
    notify($('cfgStatus'), 'ошибка: ' + error.message, 'error');
  }
};

$('yamSave').onclick = async () => {
  try {
    const settings = state.settings || await yam('/settings');
    settings.led = settings.led || {};
    settings.led.profile = $('yamProfile').value;
    settings.led.count = Number($('yamCount').value);
    settings.allow_plugin_binaries = $('yamBinaries').checked;
    settings.updates = { ...(settings.updates || {}), enabled: $('yamUpdates').checked };
    await yam('/settings', { method: 'PUT', body: JSON.stringify(settings) });
    state.ledCount = Number(settings.led.count) || 6;
    buildLedGrid(state.ledCount);
    notify($('yamStatus'), 'настройки демона сохранены', 'ok');
  } catch (error) {
    notify($('yamStatus'), 'ошибка: ' + error.message, 'error');
  }
};

$('pwSave').onclick = async () => {
  try {
    await api('/api/password', {
      method: 'POST',
      body: JSON.stringify({ current: $('pwCurrent').value, new: $('pwNew').value })
    });
    $('pwCurrent').value = $('pwNew').value = '';
    notify($('pwStatus'), 'пароль изменён', 'ok');
  } catch (error) {
    notify($('pwStatus'), 'ошибка: ' + error.message, 'error');
  }
};

$('rebootBtn').onclick = async () => {
  if (!confirm('Перезагрузить колонку сейчас?')) return;
  notify($('rebootStatus'), 'отправляю команду reboot…');
  try {
    await api('/api/reboot', { method: 'POST', body: '{}' });
    notify($('rebootStatus'), 'команда отправлена — колонка перезагружается', 'ok');
  } catch (error) {
    notify($('rebootStatus'), 'ошибка: ' + error.message, 'error');
  }
};

/* ------------------------- телеметрия Яндекса -------------------------
   servicesList в /system/vendor/quasar/quasar.cfg (см. server.py): убираем
   telemetry и fluent-bitd, бэкап рядом. Применение — после перезагрузки. */
async function refreshTelemetry() {
  try {
    const st = await api('/api/telemetry');
    if (!st.ok) {
      notify($('telemetryStatus'), 'не удалось прочитать: ' + (st.detail || 'нет данных'), 'error');
      return;
    }
    const off = !(st.telemetry_in_list || st.fluent_in_list);
    const running = (st.running_telemetry || 0) + (st.running_fluent || 0);
    const bak = st.backup ? 'бэкап: есть' : 'бэкапа нет';
    const box = $('telemetryOff');
    if (box) box.checked = off;
    notify($('telemetryStatus'), off
      ? (running > 0
        ? `отключена (${bak}) — процессы ещё работают: перезагрузите колонку`
        : `отключена (${bak})`)
      : (running > 0
        ? `включена: ${st.services_list.join(', ')}`
        : `службы возвращены (${st.services_list.join(', ')}) — перезагрузите колонку`),
      'ok');
  } catch (error) {
    notify($('telemetryStatus'), 'ошибка: ' + error.message, 'error');
  }
}

const telemetryOffBox = $('telemetryOff');
if (telemetryOffBox) telemetryOffBox.addEventListener('change', async () => {
  const wantOff = telemetryOffBox.checked;
  const question = wantOff
    ? 'Отключить телеметрию? Конфиг колонки изменится (исходник сохраняется бэкапом), применится после перезагрузки.'
    : 'Вернуть телеметрию Яндекса в исходное состояние?';
  if (!confirm(question)) { refreshTelemetry(); return; }
  telemetryOffBox.disabled = true;
  notify($('telemetryStatus'), 'правлю quasar.cfg на колонке…');
  try {
    await api('/api/telemetry', { method: 'POST', body: JSON.stringify({ enabled: !wantOff }) });
    await refreshTelemetry();
  } catch (error) {
    notify($('telemetryStatus'), 'ошибка: ' + error.message, 'error');
    await refreshTelemetry();
  } finally {
    telemetryOffBox.disabled = false;
  }
});

/* ------------------------- обновления панели ------------------------- */
$('panelUpdAuto').checked = localStorage.getItem('sp_panel_auto') === '1';
$('panelUpdAuto').onchange = event => {
  localStorage.setItem('sp_panel_auto', event.target.checked ? '1' : '0');
};

async function panelUpdateCheck(silent) {
  try {
    const info = await api('/api/panel/update');
    const text = info.repo + ' · ' + info.commit + ' → ' + info.remote_commit + ': '
      + (info.up_to_date ? 'панель актуальна' : 'доступно обновление');
    $('panelUpdInfo').textContent = text;
    if (!silent) notify($('panelUpdStatus'), text, info.up_to_date ? 'ok' : '');
    return info;
  } catch (error) {
    if (!silent) notify($('panelUpdStatus'), 'ошибка: ' + error.message, 'error');
    return null;
  }
}

$('panelUpdCheck').onclick = () => panelUpdateCheck(false);

$('panelUpdApply').onclick = async () => {
  if (!confirm('Обновить панель из GitHub? Страница перезагрузится.')) return;
  notify($('panelUpdStatus'), 'ставлю обновление…');
  try {
    await api('/api/panel/update', { method: 'POST', body: '{}' });
    notify($('panelUpdStatus'), 'готово — жду перезапуск панели…', 'ok');
    setTimeout(() => location.reload(), 8000);
  } catch (error) {
    notify($('panelUpdStatus'), 'ошибка: ' + error.message, 'error');
  }
};

// автопроверка: один раз при старте и затем раз в сутки
if ($('panelUpdAuto').checked) {
  setTimeout(() => panelUpdateCheck(false), 2000);
  setInterval(() => panelUpdateCheck(true), 24 * 3600 * 1000);
}

/* ------------------------- голосовые команды ------------------------- */
async function loadVoiceConfig() {
  try {
    const voice = await api('/api/voice/config');
    $('voicePhrases').value = (voice.phrases || []).join('\n');
    $('voiceBridge').value = voice.bridge_url || '';
    $('voiceBridgeToken').dataset.hasToken = voice.has_token ? '1' : '0';
    const status = await api('/api/voice/status');
    if (status.installed) {
      const state = [status.enabled ? 'включён' : 'выключен', status.running ? 'работает' : 'не запущен'].join(', ');
      notify($('voiceStatus'), `плагин на колонке: ${state}. Фразы: ${(status.phrases || []).join(', ')}`, 'ok');
    } else {
      notify($('voiceStatus'), 'плагин на колонке не установлен');
    }
  } catch (error) {
    notify($('voiceStatus'), 'конфиг голоса: ' + error.message, 'error');
  }
}

$('voiceDeploy').onclick = async () => {
  const phrases = $('voicePhrases').value.split('\n').map(x => x.trim()).filter(Boolean);
  const payload = { phrases, bridge_url: $('voiceBridge').value.trim() };
  const token = $('voiceBridgeToken').value.trim();
  if (token) payload.bridge_token = token;
  if (!phrases.length) {
    notify($('voiceStatus'), 'нужна хотя бы одна фраза', 'error');
    return;
  }
  notify($('voiceStatus'), 'устанавливаю плагин на колонку…');
  try {
    await api('/api/voice/config', { method: 'PUT', body: JSON.stringify(payload) });
    const result = await api('/api/voice/deploy', { method: 'POST', body: '{}' });
    notify($('voiceStatus'), 'плагин установлен и запущен. Скажи: «Алиса, ' + phrases[0] + '»', 'ok');
  } catch (error) {
    notify($('voiceStatus'), 'ошибка: ' + error.message, 'error');
  }
};

/* ------------------------- сказать на колонке -------------------------
   Два движка: Эдж (бесплатный) и ElevenLabs (выше качество; API из РФ —
   через прокси). Голос/интонации Эджа и фильтры интонаций работают в обоих. */
async function sayLoadEngine() {
  try {
    const data = await api('/api/say/config');
    sayApplyVisibility(data.engine, data);
    $('sayEngine').value = data.engine;
    if (data.has_key) $('sayKey').placeholder = 'ключ сохранён в панели — можно ввести новый';
    $('sayModel').innerHTML = (data.models || []).map(m => `<option value="${m.id}" ${m.id === data.model ? 'selected' : ''}>${esc(m.name)}</option>`).join('');
    $('sayProxy').value = data.proxy || '';
    if (data.engine === 'elevenlabs' && data.has_key) await sayLoadElevenVoices(data.voice_id);
  } catch (error) {
    $('sayEngineHint').textContent = 'ошибка: ' + error.message;
  }
}

function sayApplyVisibility(engine, data) {
  const eleven = engine === 'elevenlabs';
  const alice = engine === 'alice';
  $('sayElevenBox').style.display = eleven ? '' : 'none';
  $('sayVoice').style.display = eleven || alice ? 'none' : '';
  // интонации работают только в синтезе (Эдж/ElevenLabs) — у Алисы фраза играется её движком
  $('sayMood').style.display = alice ? 'none' : '';
  $('sayEngineHint').textContent = alice
    ? 'родной голос Алисы из облака; нужен вход в Яндекс (вкладка «Алиса»)'
    : eleven
    ? (data && data.has_key ? 'ключ задан' : 'ключ не задан')
    : 'бесплатный синтез Microsoft';
}

async function sayLoadElevenVoices(selectedId) {
  const v = await api('/api/say/voices?engine=elevenlabs').catch(() => null);
  if (v && v.ok) {
    $('sayVoiceEl').innerHTML = v.voices.map(x => `<option value="${x.id}" ${x.id === selectedId ? 'selected' : ''}>${esc(x.name)}</option>`).join('')
      || '<option value="">голосов нет</option>';
  } else if (v) {
    $('sayEngineHint').textContent = v.detail || 'список голосов недоступен';
  }
  const q = await api('/api/say/quota').catch(() => null);
  if (q && q.ok && typeof q.used === 'number') {
    $('sayEngineHint').textContent = `символов: ${q.used} из ${q.limit} в этом месяце`;
  }
}

$('sayEngine').onchange = () => {
  const engine = $('sayEngine').value;
  sayApplyVisibility(engine, { has_key: true });
  if (engine === 'elevenlabs' && !$('sayVoiceEl').options.length) sayLoadElevenVoices('');
};

$('sayKeySave').onclick = async () => {
  notify($('sayStatus'), 'сохраняю настройки голоса…');
  try {
    await api('/api/say/config', {
      method: 'PUT',
      body: JSON.stringify({
        engine: $('sayEngine').value,
        eleven_api_key: $('sayKey').value.trim(),
        eleven_model: $('sayModel').value,
        eleven_voice_id: $('sayVoiceEl').value,
        eleven_proxy: $('sayProxy').value.trim()
      })
    });
    $('sayKey').value = '';
    notify($('sayStatus'), 'сохранено', 'ok');
    await sayLoadEngine();
  } catch (error) {
    notify($('sayStatus'), 'ошибка: ' + error.message, 'error');
  }
};

$('sayBtn').onclick = async () => {
  const text = $('sayText').value.trim();
  if (!text) {
    notify($('sayStatus'), 'введи текст', 'error');
    return;
  }
  notify($('sayStatus'), 'синтезирую речь…');
  $('sayBtn').disabled = true;
  try {
    await api('/api/speaker/say', {
      method: 'POST',
      body: JSON.stringify({
        text,
        engine: $('sayEngine').value,
        voice: $('sayEngine').value === 'elevenlabs' ? $('sayVoiceEl').value : $('sayVoice').value,
        mood: $('sayMood').value
      })
    });
    notify($('sayStatus'), $('sayEngine').value === 'alice' ? 'Алиса произнесла текст' : 'колонка произнесла текст', 'ok');
  } catch (error) {
    notify($('sayStatus'), 'ошибка: ' + error.message, 'error');
  } finally {
    $('sayBtn').disabled = false;
  }
};
sayLoadEngine();

/* ------------------------- файловый менеджер ------------------------- */
let filesCurrent = '/data';

function formatSize(bytes) {
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' МБ';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' КБ';
  return bytes + ' Б';
}

async function loadFiles(path) {
  path = path.replace(/\/+$/, '') || '/';
  filesCurrent = path;
  $('filesPath').value = path;
  $('filesEditor').style.display = 'none';
  notify($('filesStatus'), 'читаю ' + path + '…');
  try {
    const data = await api('/api/files/list?path=' + encodeURIComponent(path));
    const rows = [['📁', '..', '', '', '']];
    for (const entry of data.entries) {
      rows.push([
        entry.is_dir ? '📁' : '📄',
        entry.name,
        entry.is_dir ? '' : formatSize(entry.size),
        entry.mtime,
        entry.perms
      ]);
    }
    const table = document.createElement('table');
    table.style.width = '100%';
    table.style.borderCollapse = 'collapse';
    for (const [i, row] of rows.entries()) {
      const tr = document.createElement('tr');
      const name = row[1];
      if (i === 0) {
        tr.onclick = () => loadFiles(path.replace(/\/[^/]*$/, '') || '/');
        tr.style.cursor = path === '/' ? 'default' : 'pointer';
      } else {
        const entry = data.entries[i - 1];
        tr.style.cursor = 'pointer';
        tr.onclick = () => entry.is_dir ? loadFiles(path + '/' + entry.name) : openFileEditor(path + '/' + entry.name);
      }
      for (const [col, cell] of row.entries()) {
        const td = document.createElement('td');
        if (col === 1 && i > 0) {
          td.textContent = cell;
          td.style.flex = '1';
          // кнопки действий в той же строке
          const entry = data.entries[i - 1];
          const actions = document.createElement('span');
          actions.style.cssText = 'float:right; opacity:.75';
          const dl = document.createElement('a');
          dl.textContent = '⬇ ';
          dl.href = '/api/files/download?path=' + encodeURIComponent(path + '/' + entry.name);
          actions.appendChild(dl);
          if (!entry.is_dir) {
            const edit = document.createElement('span');
            edit.innerHTML = svgIcon('pencil');
            edit.style.cursor = 'pointer';
            edit.onclick = event => { event.stopPropagation(); openFileEditor(path + '/' + entry.name); };
            actions.appendChild(edit);
          }
          const del = document.createElement('span');
          del.innerHTML = svgIcon('x');
          del.style.cursor = 'pointer';
          del.onclick = async event => {
            event.stopPropagation();
            if (!confirm('Удалить ' + (entry.is_dir ? 'папку со всем содержимым' : 'файл') + ' ' + name + '?')) return;
            try {
              await api('/api/files/delete', { method: 'POST', body: JSON.stringify({ path: path + '/' + entry.name, recursive: entry.is_dir }) });
              loadFiles(path);
            } catch (error) { notify($('filesStatus'), 'ошибка: ' + error.message, 'error'); }
          };
          actions.appendChild(del);
          td.appendChild(actions);
        } else {
          td.textContent = cell;
        }
        td.style.padding = '2px 8px 2px 0';
        td.style.whiteSpace = 'nowrap';
        tr.appendChild(td);
      }
      table.appendChild(tr);
    }
    const container = $('filesTable');
    container.textContent = '';
    container.appendChild(table);
    notify($('filesStatus'), data.entries.length + ' объектов в ' + path, 'ok');
  } catch (error) {
    notify($('filesStatus'), 'ошибка: ' + error.message, 'error');
  }
}

async function openFileEditor(filePath) {
  try {
    const data = await api('/api/files/read?path=' + encodeURIComponent(filePath));
    if (data.binary) {
      notify($('filesStatus'), 'бинарный файл — смотрим через скачивание', 'error');
      return;
    }
    $('filesEditor').style.display = 'block';
    $('filesEditPath').textContent = filePath + ' · ' + formatSize(data.size);
    $('filesEditArea').value = data.content;
    $('filesEditArea').dataset.path = filePath;
    $('filesEditArea').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (error) {
    notify($('filesStatus'), 'ошибка: ' + error.message, 'error');
  }
}

$('filesGo').onclick = () => loadFiles($('filesPath').value.trim() || '/');
$('filesPath').onkeydown = event => { if (event.key === 'Enter') loadFiles($('filesPath').value.trim() || '/'); };
$('filesUp').onclick = () => loadFiles(filesCurrent.replace(/\/[^/]*$/, '') || '/');
$('filesRefresh').onclick = () => loadFiles(filesCurrent);
$('filesMkdir').onclick = async () => {
  const name = prompt('Имя новой папки в ' + filesCurrent);
  if (!name) return;
  try {
    await api('/api/files/mkdir', { method: 'POST', body: JSON.stringify({ path: filesCurrent + '/' + name }) });
    loadFiles(filesCurrent);
  } catch (error) { notify($('filesStatus'), 'ошибка: ' + error.message, 'error'); }
};
$('filesUpload').onchange = async event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    await api('/api/files/upload?path=' + encodeURIComponent(filesCurrent), { method: 'POST', body: form });
    loadFiles(filesCurrent);
  } catch (error) { notify($('filesStatus'), 'ошибка: ' + error.message, 'error'); }
};
$('filesSave').onclick = async () => {
  try {
    await api('/api/files/write', { method: 'PUT', body: JSON.stringify({ path: $('filesEditArea').dataset.path, content: $('filesEditArea').value }) });
    notify($('filesStatus'), 'сохранено', 'ok');
  } catch (error) { notify($('filesStatus'), 'ошибка: ' + error.message, 'error'); }
};
$('filesEditClose').onclick = () => { $('filesEditor').style.display = 'none'; };

// файлы грузим при первом открытии вкладки
let filesLoaded = false;
const filesObserver = new MutationObserver(() => {
  if ($('tab-files').classList.contains('active') && !filesLoaded) {
    filesLoaded = true;
    loadFiles('/data');
    loadBackups();
  }
});
filesObserver.observe($('tab-files'), { attributes: true, attributeFilter: ['class'] });

/* ------------------------- бэкапы /data ------------------------- */
async function loadBackups() {
  try {
    const data = await api('/api/files/backups');
    const container = $('backupsList');
    container.textContent = '';
    if (!data.backups.length) {
      container.textContent = 'бэкапов пока нет';
      return;
    }
    const table = document.createElement('table');
    table.style.width = '100%';
    for (const backup of data.backups) {
      const tr = document.createElement('tr');
      const name = document.createElement('td');
      name.textContent = backup.name + ' · ' + formatSize(backup.size) + ' · ' + new Date(backup.mtime * 1000).toLocaleString('ru-RU');
      name.style.padding = '2px 8px 2px 0';
      const actions = document.createElement('td');
      actions.style.textAlign = 'right';
      actions.style.whiteSpace = 'nowrap';
      const dl = document.createElement('a');
      dl.textContent = 'скачать';
      dl.href = '/api/files/backups/download?name=' + encodeURIComponent(backup.name);
      actions.appendChild(dl);
      const restore = document.createElement('span');
      restore.textContent = ' · восстановить';
      restore.style.cursor = 'pointer';
      restore.onclick = async () => {
        if (!confirm('Восстановить ' + backup.name + ' на колонку?\nФайлы из архива перезапишут текущие в /data.')) return;
        notify($('filesStatus'), 'восстанавливаю…');
        try {
          const result = await api('/api/files/restore', { method: 'POST', body: JSON.stringify({ name: backup.name }) });
          notify($('filesStatus'), 'восстановлено объектов: ' + result.restored, 'ok');
          loadFiles(filesCurrent);
        } catch (error) { notify($('filesStatus'), 'ошибка: ' + error.message, 'error'); }
      };
      actions.appendChild(restore);
      tr.appendChild(name);
      tr.appendChild(actions);
      table.appendChild(tr);
    }
    container.appendChild(table);
  } catch (error) {
    $('backupsList').textContent = 'ошибка: ' + error.message;
  }
}

$('backupBtn').onclick = async () => {
  notify($('filesStatus'), 'архивирую /data на колонке и качаю на ПК…');
  $('backupBtn').disabled = true;
  try {
    const result = await api('/api/files/backup', { method: 'POST', body: '{}' });
    notify($('filesStatus'), 'бэкап готов: ' + result.name + ' (' + formatSize(result.size) + ')', 'ok');
    loadBackups();
  } catch (error) {
    notify($('filesStatus'), 'ошибка: ' + error.message, 'error');
  } finally {
    $('backupBtn').disabled = false;
  }
};
$('backupsRefresh').onclick = loadBackups;

/* ------------------------- музыка: soundya + spotiya ------------------------- */
let musicObserverBound = false;
let spotiyaPaused = false;
let spotiyaVolume = 60;

async function musicStatus() {
  try {
    const data = await api('/api/music/status');
    const s = data.soundya || {};
    $('soundyaPanel').style.display = s.installed === false ? 'none' : '';
    $('soundyaChip').textContent = s.running ? 'работает' : (s.installed === false ? 'не установлен' : 'остановлен');
    $('soundyaStatus').textContent = s.raw || '';
    const p = data.spotiya || {};
    $('spotiyaPanel').style.display = p.installed === false ? 'none' : '';
    spotiyaPaused = !!p.paused;
    if (typeof p.volume === 'number') spotiyaVolume = p.volume;
    $('spotiyaChip').textContent = p.installed ? (p.active ? (p.paused ? 'на паузе' : 'играет') : 'готов') : 'не установлен';
    $('spotiyaStatus').textContent = p.installed
      ? `${p.artist ? p.artist + ' — ' : ''}${p.title || 'ничего не играет'} · громкость ${p.volume ?? '?'} · голос ${p.voice ? 'ВКЛ(!)' : 'выкл'} · AirPlay ${p.airplay ? 'работает' : 'выкл'}${p.error ? ' · ' + p.error : ''}`
      : (p.raw || '');
    const missing = [];
    if (s.installed === false) missing.push('SoundCloud (soundya)');
    if (p.installed === false) missing.push('Spotify (spotiya)');
    $('musicMissing').style.display = missing.length ? '' : 'none';
    $('musicMissing').textContent = missing.length ? `Не установлено: ${missing.join(', ')}. Поставить можно во вкладке «Пакеты».` : '';
  } catch (error) {
    $('soundyaChip').textContent = 'ошибка';
  }
}

async function musicCmd(kind, cmd, arg) {
  try {
    const result = await api('/api/music/' + kind, { method: 'POST', body: JSON.stringify({ cmd, arg }) });
    if (!result.ok) notify($('soundyaStatus'), result.output || 'ошибка', 'error');
  } catch (error) {
    notify($('soundyaStatus'), 'ошибка: ' + error.message, 'error');
  }
  setTimeout(musicStatus, 1500);
}

document.querySelectorAll('[data-soundya]').forEach(button => {
  button.onclick = () => musicCmd('soundya', button.dataset.soundya);
});
document.querySelectorAll('[data-spotiya]').forEach(button => {
  button.onclick = () => {
    const cmd = button.dataset.spotiya;
    if (cmd === 'toggle') musicCmd('spotiya', spotiyaPaused ? 'resume' : 'pause');
    else musicCmd('spotiya', cmd);
  };
});
$('soundyaPlay').onclick = () => {
  const q = $('soundyaQuery').value.trim();
  if (q) musicCmd('soundya', 'play', q);
};
$('soundyaQuery').onkeydown = event => {
  if (event.key === 'Enter') $('soundyaPlay').click();
};
$('spotiyaVolUp').onclick = async () => {
  try { await api('/api/music/spotiya/volume', { method: 'POST', body: JSON.stringify({ volume: Math.min(100, spotiyaVolume + 10) }) }); } catch (error) { notify($('spotiyaStatus'), 'ошибка: ' + error.message, 'error'); }
  setTimeout(musicStatus, 1200);
};
$('spotiyaVolDown').onclick = async () => {
  try { await api('/api/music/spotiya/volume', { method: 'POST', body: JSON.stringify({ volume: Math.max(0, spotiyaVolume - 10) }) }); } catch (error) { notify($('spotiyaStatus'), 'ошибка: ' + error.message, 'error'); }
  setTimeout(musicStatus, 1200);
};

/* ------------------------- плеер на ПК ------------------------- */
let localTimer = null;

async function localStatus() {
  try {
    const data = await api('/api/player/local/status');
    $('localChip').textContent = data.playing ? 'играет' : 'тишина';
    $('localStatus').textContent = data.playing
      ? `играет: ${data.title} · ${data.elapsed} с`
      : 'ничего не играет';
  } catch (error) {
    $('localChip').textContent = 'ошибка';
  }
}

$('localPlay').onclick = async () => {
  const query = $('localQuery').value.trim();
  if (!query) { notify($('localStatus'), 'введи запрос', 'error'); return; }
  notify($('localStatus'), 'ищу и запускаю на ПК…');
  try {
    const result = await api('/api/player/local', { method: 'POST', body: JSON.stringify({ query }) });
    notify($('localStatus'), 'играет: ' + result.title, 'ok');
  } catch (error) {
    notify($('localStatus'), 'ошибка: ' + error.message, 'error');
  }
};
$('localQuery').onkeydown = event => { if (event.key === 'Enter') $('localPlay').click(); };
$('localStop').onclick = async () => {
  try { await api('/api/player/local/stop', { method: 'POST', body: '{}' }); } catch (error) { notify($('localStatus'), 'ошибка: ' + error.message, 'error'); }
  localStatus();
};

$('soundyaTokenSave').onclick = async () => {
  const token = $('soundyaToken').value.trim();
  if (!token) { notify($('soundyaStatus'), 'вставь oauth_token из браузера', 'error'); return; }
  notify($('soundyaStatus'), 'сохраняю токен…');
  try {
    const result = await api('/api/music/soundya/token', { method: 'POST', body: JSON.stringify({ token }) });
    $('soundyaToken').value = '';
    notify($('soundyaStatus'), result.output || 'токен сохранён', result.output && result.output.includes('error') ? 'error' : 'ok');
  } catch (error) {
    notify($('soundyaStatus'), 'ошибка: ' + error.message, 'error');
  }
};

// статус музыки — при открытии вкладки и далее по таймеру
const musicObserver = new MutationObserver(() => {
  if ($('tab-music').classList.contains('active') && !musicObserverBound) {
    musicObserverBound = true;
    musicStatus();
    localStatus();
    setInterval(musicStatus, 10000);
    setInterval(localStatus, 5000);
  }
});
musicObserver.observe($('tab-music'), { attributes: true, attributeFilter: ['class'] });

/* ------------------------- слова активации ------------------------- */
const ww = (path, options) => api('/wakeword/' + path, options);
let wwState = null;
let wwTimer = null;
let wwBusy = false;
let wwDragging = false;

const wwSleep = ms => new Promise(r => setTimeout(r, ms));

function wwPct(x) { return Math.round(Math.max(0, Math.min(1, x)) * 100) + '%'; }

function wwEsc(value) { return esc(value); }

/* аудио и прогресс */
function wwRenderAudio() {
  const audio = wwState?.audio || {};
  const chip = $('wwAudioChip');
  chip.textContent = audio.ok ? 'слышно' : 'нет звука';
  chip.style.color = audio.ok ? 'var(--ok)' : 'var(--danger)';
  const level = typeof audio.level === 'number' ? audio.level : -90;
  const norm = Math.max(0, Math.min(1, (level + 80) / 70));
  $('wwLevelBar').style.width = wwPct(norm);
  $('wwLevelText').textContent = `${level.toFixed(1)} дБ${audio.channel ? ' · ' + audio.channel : ''}`;
}

/* обучение */
function wwRenderTrain() {
  const t = wwState?.train || {};
  const panel = $('wwTrainPanel');
  panel.hidden = !t.active && !t.error;
  if (panel.hidden) return;
  $('wwTrainWord').textContent = t.name || t.word || '';
  const stages = { load: [0, 0.02], embed: [0.02, 0.45], train: [0.45, 0.95], calibrate: [0.95, 1] };
  const names = { load: 'загрузка', embed: 'обработка записей', train: 'обучение', calibrate: 'проверка' };
  const [a, b] = stages[t.stage] || [0, 1];
  const frac = Math.max(0, Math.min(1, a + (b - a) * (t.frac || 0)));
  $('wwTrainBar').style.width = wwPct(frac);
  $('wwTrainPct').textContent = wwPct(frac);
  $('wwTrainStage').textContent = t.error ? 'Ошибка: ' + t.error : (names[t.stage] || t.stage || '') + '…';
}

/* список слов */
function wwRenderWords() {
  const words = wwState?.words || [];
  $('wwEmpty').hidden = words.length > 0;
  const box = $('wwWords');
  if (wwDragging) {  // не перерисовываем под активным ползунком
    words.forEach(w => {
      const meter = box.querySelector(`[data-meter="${w.id}"]`);
      if (meter) meter.style.width = wwPct(w.score);
    });
    return;
  }
  box.innerHTML = words.map(w => {
    const thr = Math.min(0.99, Math.max(0.05, w.threshold - w.sensitivity));
    const notes = (w.notes || []).map(n => `<div class="muted small">${wwEsc(n)}</div>`).join('');
    return `<div class="ww-word">
      <div class="ww-word-head">
        <h3>«${wwEsc(w.name)}»</h3>
        <label class="check" style="margin:0"><input type="checkbox" data-ww-enable="${wwEsc(w.id)}" ${w.enabled ? 'checked' : ''}> включено</label>
        <span class="grow" style="flex:1"></span>
        <span class="muted small">${w.takes} записей · узнаёт ${wwPct(w.recall)} · ложных ≈ ${(w.false_per_h ?? 0).toFixed(1)}/час</span>
      </div>
      <div class="ww-meter" title="Уверенность сейчас; отметка — порог срабатывания">
        <i data-meter="${wwEsc(w.id)}" style="width:${wwPct(w.score)}"></i><b style="left:${wwPct(thr)}"></b>
      </div>
      <div class="row wrap">
        <span class="muted small">реже</span>
        <input type="range" min="-0.3" max="0.3" step="0.05" value="${w.sensitivity}" data-ww-sens="${wwEsc(w.id)}" style="flex:1; min-width:120px">
        <span class="muted small">чаще</span>
      </div>
      ${notes}
      <div class="row wrap" style="margin-top:10px">
        <button class="btn sm" data-ww-more="${wwEsc(w.name)}">Дозаписать</button>
        <button class="btn sm" data-ww-retrain="${wwEsc(w.id)}">Переобучить</button>
        <span style="flex:1"></span>
        <button class="btn sm danger" data-ww-del="${wwEsc(w.id)}" data-ww-name="${wwEsc(w.name)}">Удалить</button>
      </div>
    </div>`;
  }).join('');
}

/* мастер записи слова */
function wwRenderEnroll() {
  const e = wwState?.enroll;
  $('wwWizard').hidden = !e;
  if (!e) return;
  $('wwName').textContent = e.name;
  $('wwMinTakes').textContent = e.min_takes;
  const enough = e.takes.length >= e.min_takes;
  $('wwStepAmbient').className = 'ww-step ' + (e.has_ambient ? 'done' : 'cur');
  $('wwStepTakes').className = 'ww-step ' + (enough ? 'done' : (e.has_ambient ? 'cur' : ''));
  $('wwStepSpeech').className = 'ww-step ' + (e.speech_secs > 0 ? 'done' : '');
  $('wwStepTrain').className = 'ww-step ' + (enough && e.has_ambient ? 'cur' : '');
  $('wwTrainBtn').disabled = wwBusy || !enough || !e.has_ambient;
  $('wwAmbientMsg').textContent = e.has_ambient ? 'записано' : '';
  $('wwSpeechMsg').textContent = e.speech_secs > 0 ? 'записано' : '';
  $('wwTakes').innerHTML = (e.takes || []).map((d, i) => `<span class="ww-take">#${i + 1} ${Number(d).toFixed(2)} с
      <button class="btn sm ghost" data-ww-play-take="${i}" title="Послушать">${svgIcon('play')}</button>
      <button class="btn sm ghost" data-ww-del-take="${i}" title="Удалить">${svgIcon('x')}</button></span>`).join('');
}

/* срабатывания */
function wwRenderHits() {
  const hits = wwState?.hits || [];
  $('wwNoHits').hidden = hits.length > 0;
  $('wwHits').innerHTML = hits.map(h => `<div class="ww-hit">
      <time>${new Date(h.at).toLocaleTimeString()}</time>
      <span>«${wwEsc(h.name)}» · ${Number(h.score).toFixed(2)} ${h.error ? '· ' + wwEsc(h.error) : (h.fired ? '· слушала' : '')}</span>
      <span></span>
      <span class="row">
        <button class="btn sm" data-ww-play-hit="${wwEsc(h.id)}">▶</button>
        <button class="btn sm" data-ww-false="${wwEsc(h.id)}">Ложное</button>
      </span>
    </div>`).join('');
}

async function wwRefresh() {
  try {
    wwState = await ww('api/state');
    wwRenderAudio();
    wwRenderTrain();
    wwRenderWords();
    wwRenderEnroll();
    wwRenderHits();
  } catch (error) {
    $('wwAudioChip').textContent = 'нет связи';
    $('wwAudioChip').style.color = 'var(--danger)';
  }
}

async function wwRun(btn, fn) {
  if (wwBusy) return;
  wwBusy = true;
  if (btn) btn.disabled = true;
  try { await fn(); } catch (error) { alert(error.message); }
  finally { wwBusy = false; if (btn) btn.disabled = false; await wwRefresh(); }
}

$('wwNewBtn').onclick = () => {
  const name = prompt('Какое слово будет звать станцию? Например: Джарвис, Компьютер, Станция');
  if (!name) return;
  wwRun(null, async () => {
    $('wwTakeMsg').textContent = '';
    $('wwSpeechMsg').textContent = '';
    $('wwSay').textContent = '';
    await ww('api/enroll/start', { method: 'POST', body: JSON.stringify({ name: name.trim() }) });
  });
};

$('wwCancel').onclick = () => {
  if (confirm('Бросить запись этого слова?')) wwRun(null, () => ww('api/enroll/cancel', { method: 'POST', body: '{}' }));
};

$('wwAmbientBtn').onclick = () => wwRun($('wwAmbientBtn'), async () => {
  notify($('wwAmbientMsg'), 'тихо… 8 секунд');
  const r = await ww('api/enroll/ambient', { method: 'POST', body: '{}' });
  notify($('wwAmbientMsg'), `записано (фон ${Number(r.level).toFixed(0)} дБ)`, 'ok');
});

$('wwTakeBtn').onclick = () => wwRun($('wwTakeBtn'), async () => {
  notify($('wwTakeMsg'), '');
  $('wwPlayLast').hidden = true;
  for (const n of ['3', '2', '1']) { $('wwSay').textContent = n + '…'; await wwSleep(500); }
  $('wwSay').textContent = 'Говорите!';
  try {
    const r = await ww('api/enroll/take', { method: 'POST', body: '{}' });
    notify($('wwTakeMsg'), r.ok ? `отлично: ${Number(r.duration).toFixed(2)} с · всего ${r.takes}` : (r.message || 'не получилось'), r.ok ? 'ok' : 'error');
  } finally {
    $('wwSay').textContent = '';
    $('wwPlayLast').hidden = false;
  }
});

$('wwSpeechBtn').onclick = () => wwRun($('wwSpeechBtn'), async () => {
  notify($('wwSpeechMsg'), 'читайте вслух… 20 секунд');
  await ww('api/enroll/speech?secs=20', { method: 'POST', body: '{}' });
  notify($('wwSpeechMsg'), 'записано', 'ok');
});

$('wwTrainBtn').onclick = () => wwRun($('wwTrainBtn'), async () => {
  await ww('api/enroll/train', { method: 'POST', body: '{}' });
  notify($('wwTrainMsg'), 'обучение началось', 'ok');
});

$('wwListenBtn').onclick = () => wwRun($('wwListenBtn'), async () => {
  await ww('api/listen', { method: 'POST', body: '{}' });
});

$('wwPlayLast').onclick = () => new Audio('/wakeword/api/enroll/take/last?t=' + Date.now()).play();

/* прослушивание: записи слова и срабатываний */
document.addEventListener('click', event => {
  const btn = event.target.closest('button');
  if (!btn) return;
  const playTake = btn.dataset.wwPlayTake;
  if (playTake !== undefined) { new Audio('/wakeword/api/enroll/take/' + playTake + '?t=' + Date.now()).play(); return; }
  const delTake = btn.dataset.wwDelTake;
  if (delTake !== undefined) { wwRun(null, () => ww('api/enroll/take/' + delTake, { method: 'DELETE' })); return; }
  const playHit = btn.dataset.wwPlayHit;
  if (playHit !== undefined) { new Audio('/wakeword/api/hits/' + playHit + '?t=' + Date.now()).play(); return; }
  const falseHit = btn.dataset.wwFalse;
  if (falseHit !== undefined) { wwRun(null, () => ww('api/hits/' + falseHit + '/false', { method: 'POST', body: '{}' })); return; }
  const more = btn.dataset.wwMore;
  if (more !== undefined) { wwRun(null, () => ww('api/enroll/start', { method: 'POST', body: JSON.stringify({ name: more }) })); return; }
  const retrain = btn.dataset.wwRetrain;
  if (retrain !== undefined) { wwRun(null, () => ww('api/words/' + retrain + '/retrain', { method: 'POST', body: '{}' })); return; }
  const del = btn.dataset.wwDel;
  if (del !== undefined && confirm(`Удалить слово «${btn.dataset.wwName}» со всеми записями?`)) {
    wwRun(null, () => ww('api/words/' + del, { method: 'DELETE' }));
  }
});

/* включение/чувствительность */
document.addEventListener('change', event => {
  const target = event.target;
  if (target.dataset.wwEnable !== undefined) {
    wwRun(null, () => ww('api/words/' + target.dataset.wwEnable, { method: 'POST', body: JSON.stringify({ enabled: target.checked }) }));
  }
  if (target.dataset.wwSens !== undefined) {
    wwDragging = true;
    const value = parseFloat(target.value);
    wwRun(null, async () => {
      await ww('api/words/' + target.dataset.wwSens, { method: 'POST', body: JSON.stringify({ sensitivity: value }) });
      target.blur();
    }).finally(() => { wwDragging = false; });
  }
});

/* -------------------------- внешний вид панели --------------------------
   Тема, курсор, шрифт и жидкое стекло — вкладка «Внешний вид».
   Настройки хранятся в localStorage браузера (не на сервере): панель одна,
   а вкус у каждого свой. Само применение — inline-скрипт в <head> index.html,
   здесь только обработка кликов и отметка активных кнопок. */
const APPEARANCE_KEY = 'sp.appearance.v1';
const APPEARANCE_DEFAULTS = { theme: 'wine', cursor: 'default', font: 'system', glass: true };
const APPEARANCE_GROUPS = [['themeGroup', 'theme'], ['cursorGroup', 'cursor'], ['fontGroup', 'font']];
let appearance = { ...APPEARANCE_DEFAULTS };
try {
  appearance = { ...APPEARANCE_DEFAULTS, ...JSON.parse(localStorage.getItem(APPEARANCE_KEY) || '{}') };
} catch (e) { /* повреждённое хранилище — работаем с дефолтами */ }

function appearanceSave() {
  try { localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance)); } catch (e) { /* приватный режим */ }
}

function appearanceApply() {
  const html = document.documentElement;
  html.dataset.theme = appearance.theme;
  html.dataset.cursor = appearance.cursor;
  html.dataset.font = appearance.font;
  html.dataset.glass = appearance.glass ? 'on' : 'off';
  APPEARANCE_GROUPS.forEach(([groupId, key]) => {
    const group = $(groupId);
    if (!group) return;
    group.querySelectorAll('.preset').forEach(button => {
      button.classList.toggle('active', button.dataset[key] === appearance[key]);
    });
  });
  const glassBox = $('glassCursor');
  if (glassBox) glassBox.checked = !!appearance.glass;
  const glassQuick = $('glassQuick');
  if (glassQuick) {
    glassQuick.classList.toggle('active', !!appearance.glass);
    const label = $('glassQuickLabel');
    if (label) label.textContent = appearance.glass ? 'Пузырь: вкл' : 'Пузырь: выкл';
  }
}

APPEARANCE_GROUPS.forEach(([groupId, key]) => {
  const group = $(groupId);
  if (!group) return;
  group.addEventListener('click', event => {
    const button = event.target.closest('.preset');
    if (!button || !group.contains(button) || !button.dataset[key]) return;
    appearance[key] = button.dataset[key];
    appearanceSave();
    appearanceApply();
  });
});

const glassBox = $('glassCursor');
if (glassBox) glassBox.addEventListener('change', () => {
  appearance.glass = glassBox.checked;
  appearanceSave();
  appearanceApply();
});

/* быстрый тумблер пузыря в сайдбаре — переключает тот же glass-флаг */
const glassQuick = $('glassQuick');
if (glassQuick) glassQuick.addEventListener('click', () => {
  appearance.glass = !appearance.glass;
  appearanceSave();
  appearanceApply();
});

const appearanceReset = $('appearanceReset');
if (appearanceReset) appearanceReset.addEventListener('click', () => {
  appearance = { ...APPEARANCE_DEFAULTS };
  appearanceSave();
  appearanceApply();
  const label = appearanceReset.textContent;
  appearanceReset.textContent = 'Сброшено ✓';
  setTimeout(() => { appearanceReset.textContent = label; }, 1400);
});

appearanceApply();

/* ------------------------------ громкость ------------------------------
   Общий регулятор ALSA (Master) на колонке — GET/PUT /api/volume, шкала
   0..100 %. Читаем при старте и раз в 30 с, пока открыт «Обзор»; во время
   перетаскивания ползунка не опрашиваем, чтобы не дёргать значение. */
const volumeRange = $('volumeRange');
const volumeValue = $('volumeValue');
const volumeStatus = $('volumeStatus');
let volumeDragging = false;

function renderVolume(level) {
  if (!volumeRange) return;
  volumeRange.value = level;
  volumeRange.style.setProperty('--fill', level + '%');
  if (volumeValue) volumeValue.textContent = level + '%';
}

async function refreshVolume() {
  if (!volumeRange || volumeDragging) return;
  try {
    const data = await api('/api/volume');
    if (data && data.ok) {
      renderVolume(data.volume);
      const parts = ['Сейчас: ' + data.volume + '%'];
      if (typeof data.pc === 'number') parts.push('ПК: ' + data.pc + '%');
      if (volumeSync && volumeSync.checked !== !!data.sync) volumeSync.checked = !!data.sync;
      if (volumeStatus) volumeStatus.textContent = parts.join(' · ');
    }
  } catch (e) {
    if (volumeStatus) volumeStatus.textContent = 'Громкость недоступна: ' + e.message;
  }
}

const volumeSync = $('volumeSync');
if (volumeSync) {
  volumeSync.onchange = async () => {
    try {
      const data = await api('/api/volume/sync', { method: 'POST', body: JSON.stringify({ enabled: volumeSync.checked }) });
      if (volumeStatus) volumeStatus.textContent = data.sync
        ? 'Синхронизация включена: громкость ПК повторяется на колонке (колонка → ползунок обновляется сам)'
        : 'Синхронизация выключена';
    } catch (error) {
      if (volumeStatus) volumeStatus.textContent = 'ошибка: ' + error.message;
      volumeSync.checked = !volumeSync.checked;
    }
  };
}

if (volumeRange) {
  volumeRange.addEventListener('input', () => {
    volumeDragging = true;
    volumeRange.style.setProperty('--fill', volumeRange.value + '%');
    if (volumeValue) volumeValue.textContent = volumeRange.value + '%';
    if (volumeStatus) volumeStatus.textContent = 'отпусти ползунок — применится';
  });
  volumeRange.addEventListener('change', async () => {
    const level = Number(volumeRange.value);
    try {
      const data = await api('/api/volume', { method: 'PUT', body: JSON.stringify({ volume: level }) });
      if (volumeStatus) volumeStatus.textContent = 'Громкость: ' + (data && data.ok ? data.volume : level) + '% — применено';
    } catch (e) {
      if (volumeStatus) volumeStatus.textContent = 'Не удалось выставить: ' + e.message;
      volumeDragging = false;
      await refreshVolume();
      return;
    }
    volumeDragging = false;
  });
}

/* ------------------------- настройки Алисы -------------------------
   Локальные настройки колонки из user_config.dat (protobuf с JSON внутри):
   тумблеры звука/быстрых команд/DND/звонков/Wi-Fi, имя устройства, Matter. */
const ALICE_TOGGLES = [
  { id: 'a-adaptive', path: 'account_config.aliceAdaptiveVolume.enabled' },
  { id: 'a-crossfade', path: 'account_config.audio_player.crossfadeEnabled' },
  { id: 'a-jingle', path: 'account_config.jingle' },
  { id: 'a-alwayson', path: 'account_config.alwaysOnMicForShortcuts' },
  { id: 'a-m-playpause', path: 'account_config.enabledCommandSpotters.music.playAndPause' },
  { id: 'a-m-volume', path: 'account_config.enabledCommandSpotters.music.volume' },
  { id: 'a-m-tracks', path: 'account_config.enabledCommandSpotters.music.navigation' },
  { id: 'a-m-feedback', path: 'account_config.enabledCommandSpotters.music.feedback' },
  { id: 'a-m-bt', path: 'account_config.enabledCommandSpotters.music.bluetooth' },
  { id: 'a-tv-nav', path: 'account_config.enabledCommandSpotters.tv.navigation' },
  { id: 'a-tv-home', path: 'account_config.enabledCommandSpotters.tv.backToHome' },
  { id: 'a-sh-light', path: 'account_config.enabledCommandSpotters.smartHome.light' },
  { id: 'a-sh-tv', path: 'account_config.enabledCommandSpotters.smartHome.tv' },
  { id: 'a-call-answer', path: 'account_config.enabledCommandSpotters.call.answer' },
  { id: 'a-dnd', path: 'device_config.dndMode.enabled' },
  { id: 'a-dnd-calls', path: 'device_config.dndMode.features.allowIncomingCalls' },
  { id: 'a-calls', path: 'system_config.phone_calls.enabled' },
  { id: 'a-wifi-auto', path: 'system_config.wifi_capability.reassociateWhenNoInternet' },
];

const aliceDeep = (config, path) => path.split('.').reduce((obj, key) => (obj == null ? obj : obj[key]), config);
const ACCESS_NAMES = { without: 'без ограничений', moderate: 'умеренный', children: 'детский', kids: 'для малышей', toddlers: 'для малышей' };

async function aliceSet(path, value, source) {
  try {
    await api('/api/alice/settings', { method: 'PUT', body: JSON.stringify({ sets: [{ path, value }] }) });
    const label = source && source.label ? ` «${source.label}»` : '';
    $('aliceUpdated').textContent = `сохранено${label} — применится после перезагрузки колонки`;
  } catch (err) {
    if (source && source.type === 'checkbox') source.el.checked = !source.el.checked;
    $('aliceUpdated').textContent = 'ошибка: ' + err.message;
  }
}

async function loadAlice() {
  const data = await api('/api/alice/settings');
  const config = data.config || {};
  ALICE_TOGGLES.forEach(item => {
    const el = $(item.id);
    if (!el) return;
    const value = aliceDeep(config, item.path);
    if (typeof value === 'boolean') el.checked = value;
    el.disabled = false;
  });
  const labelOf = { 'a-adaptive': 'адаптивная громкость', 'a-crossfade': 'кроссфейд', 'a-jingle': 'звук активации', 'a-alwayson': 'слушать без «Алисы»' };
  ALICE_TOGGLES.forEach(item => {
    const el = $(item.id);
    if (!el || el.dataset.aliceBound) return;
    el.dataset.aliceBound = '1';
    el.onchange = () => aliceSet(item.path, el.checked, { type: 'checkbox', el, label: labelOf[item.id] || item.path.split('.').pop() });
  });
  $('a-name').value = typeof config.device_config?.name === 'string' ? config.device_config.name : '';
  $('a-locale').textContent = config.device_config?.locale || '—';
  const spotter = config.account_config?.spotter;
  $('a-spotter').textContent = spotter === 'alisa' ? '«Алиса»' : (spotter || '—');
  $('a-access').textContent = ACCESS_NAMES[config.account_config?.contentAccess] || config.account_config?.contentAccess || '—';
  $('a-child-access').textContent = ACCESS_NAMES[config.account_config?.childContentAccess] || config.account_config?.childContentAccess || '—';
}

async function loadAliceWifi() {
  const data = await api('/api/alice/wifi');
  const nets = data.networks || [];
  const box = $('aWifiNets');
  if (!nets.length) {
    box.innerHTML = '<p class="muted small" style="margin:0">Сохранённых сетей не найдено.</p>';
  } else {
    box.innerHTML = nets.map((net, i) => {
      const psk = net.psk ? '•'.repeat(Math.min(net.psk.length, 12)) : '—';
      return `<p class="muted small" style="margin:0 0 4px">Сеть ${i + 1}: <b>${esc(net.ssid || '(без имени)')}</b> · пароль: <span class="mono">${esc(psk)}</span></p>`;
    }).join('');
  }
  const wd = data.watchdog;
  if (wd === 'ok') $('aWifiStatus').textContent = 'Прошлая смена пароля прошла успешно, связь стабильна.';
  else if (wd === 'rolled_back') $('aWifiStatus').textContent = 'Прошлая попытка не удалась: конфиг был откатен к прежнему паролю.';
}

async function loadAliceMatter() {
  const data = await api('/api/alice/matter');
  const box = $('aMatterList');
  box.textContent = (data.records && data.records.length)
    ? data.records.join('\n')
    : 'Устройств Matter не найдено. Подключите первое через приложение: «Подключение умных устройств» → «Matter».';
}

function loadAliceAll() {
  $('aliceUpdated').textContent = '';
  loadAlice().catch(err => { $('aliceUpdated').textContent = 'ошибка загрузки: ' + err.message; });
  loadAliceCloud().catch(err => { $('cloudStatus').textContent = 'облако: ' + err.message; });
  loadAliceWifi().catch(() => {});
  loadAliceMatter().catch(() => { $('aMatterList').textContent = 'не удалось прочитать базу Matter'; });
}

$('aNameSave').onclick = async () => {
  const name = $('a-name').value.trim();
  if (!name) { $('aliceUpdated').textContent = 'имя не может быть пустым'; return; }
  await aliceSet('device_config.name', name, { type: 'name' });
};

$('aWifiEye').onclick = () => {
  const input = $('aWifiPsk');
  input.type = input.type === 'password' ? 'text' : 'password';
};

$('aWifiSave').onclick = async () => {
  const psk = $('aWifiPsk').value;
  const status = $('aWifiStatus');
  if (psk.length < 8 || psk.length > 63) { status.textContent = 'Пароль WPA: от 8 до 63 символов.'; return; }
  status.textContent = 'Применяю: записываю конфиг и перечитываю wpa_supplicant…';
  try {
    await api('/api/alice/wifi/password', { method: 'PUT', body: JSON.stringify({ psk }) });
  } catch (err) {
    status.textContent = 'Ошибка: ' + err.message;
    return;
  }
  $('aWifiPsk').value = '';
  const deadline = Date.now() + 110000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 5000));
    let watchdog = '';
    try {
      const data = await api('/api/alice/wifi');
      watchdog = data.watchdog || '';
    } catch (_) { /* колонка могла мигнуть сетью — продолжаем опрос */ }
    if (watchdog === 'ok') { status.textContent = 'Готово: колонка на связи с новым паролем.'; return; }
    if (watchdog === 'rolled_back') { status.textContent = 'Связь не восстановилась — пароль откачен к прежнему.'; return; }
    status.textContent = 'Жду подключения колонки… (' + Math.round((deadline - Date.now()) / 1000) + ' с)';
  }
  status.textContent = 'Статус неизвестен: проверьте связь с колонкой.';
};

/* --------------- облачные функции (кука аккаунта Яндекса) ---------------
   Панель обменивает куку на x_token паспорта и ходит в те же API, что
   приложение «Дом с Алисой» (iot.quasar.yandex.ru). Значения применяются
   в облаке мгновенно и спускаются на колонку без перезагрузки. */
const CLOUD_TOGGLES = [
  { id: 'c-dj', label: 'режим DJ' },
  { id: 'c-whisper', label: 'ответ шепотом' },
  { id: 'c-sh-silent', label: 'умный дом без лишних слов' },
  { id: 'c-hide-orders', label: 'скрывать названия товаров' },
];

function cloudBindOnce() {
  if ($('cloudSave').dataset.bound) return;
  $('cloudSave').dataset.bound = '1';
  $('cloudSave').onclick = async () => {
    const status = $('cloudStatus');
    const cookie = $('cloudCookie').value.trim();
    if (!cookie) { status.textContent = 'Вставьте строку Cookie (key=value; …).'; return; }
    status.textContent = 'Обмениваю куку на токен паспорта…';
    try {
      const r = await api('/api/alice/cloud', { method: 'PUT', body: JSON.stringify({ cookie }) });
      $('cloudCookie').value = '';
      status.textContent = 'Кука принята: аккаунт ' + (r.login || '?') + '.';
      await loadAliceCloud();
    } catch (err) {
      status.textContent = 'Ошибка: ' + err.message;
    }
  };
  $('cloudForget').onclick = async () => {
    await api('/api/alice/cloud', { method: 'DELETE' }).catch(() => {});
    await loadAliceCloud();
  };
  $('cloudRefresh').onclick = () => {
    $('cloudAfter').style.display = 'none';
    $('cloudForm').style.display = '';
    $('cloudStatus').textContent = 'Вставьте свежую строку Cookie — старый токен заменится.';
    $('cloudCookie').focus();
  };
  CLOUD_TOGGLES.forEach(item => {
    const el = $(item.id);
    el.onchange = async () => {
      try {
        await api('/api/alice/cloud/settings', { method: 'POST', body: JSON.stringify({ id: item.id, value: el.checked }) });
        $('cloudStatus').textContent = `Применено в облаке: ${item.label}.`;
      } catch (err) {
        el.checked = !el.checked;
        $('cloudStatus').textContent = 'Ошибка облака: ' + err.message;
      }
    };
  });
  const devicesDetails = $('cloudDevices').closest('details');
  devicesDetails.ontoggle = async () => {
    if (!devicesDetails.open || devicesDetails.dataset.loaded) return;
    devicesDetails.dataset.loaded = '1';
    try {
      const r = await api('/api/alice/cloud/devices');
      $('cloudDevices').textContent = (r.speakers || []).map(s =>
        `${s.online ? '●' : '○'} ${s.name} (${s.platform || 'платформа ?'}) — ${s.house || 'без дома'} — id ${s.id}`
      ).join('\n') || 'колонок в облаке не найдено';
      if (r.login && $('cloudLogin').textContent === '—') $('cloudLogin').textContent = r.login;
    } catch (err) {
      devicesDetails.dataset.loaded = '';
      $('cloudDevices').textContent = 'ошибка: ' + err.message;
    }
  };
  const configDetails = $('cloudConfig').closest('details');
  configDetails.ontoggle = async () => {
    if (!configDetails.open || configDetails.dataset.loaded) return;
    configDetails.dataset.loaded = '1';
    try {
      const r = await api('/api/alice/cloud/config');
      $('cloudConfig').textContent = JSON.stringify(r.config, null, 2);
    } catch (err) {
      configDetails.dataset.loaded = '';
      $('cloudConfig').textContent = 'ошибка: ' + err.message;
    }
  };
}

async function loadAliceCloud() {
  cloudBindOnce();
  const data = await api('/api/alice/cloud');
  const status = $('cloudStatus');
  if (!data.configured) {
    status.textContent = 'Кука не задана — вставьте её по инструкции ниже, чтобы открыть облачные тумблеры.';
    $('cloudForm').style.display = '';
    $('cloudAfter').style.display = 'none';
    return;
  }
  $('cloudForm').style.display = 'none';
  $('cloudAfter').style.display = '';
  status.textContent = data.session_open ? 'Облачная сессия открыта.' : 'Токен сохранён; сессия откроется при первом переключении.';
  $('cloudLogin').textContent = data.login || '—';
  try {
    const s = await api('/api/alice/cloud/settings');
    if (s.ok) {
      const st = (s.settings && s.settings.settings) || s.settings || {};
      const set = (id, v) => { if (typeof v === 'boolean') $(id).checked = v; };
      set('c-dj', st.music ? st.music.announce_tracks : undefined);
      set('c-whisper', st.tts_whisper);
      set('c-sh-silent', st.iot && st.iot.response_reaction_type ? st.iot.response_reaction_type === 'sound' : undefined);
      set('c-hide-orders', st.order ? st.order.hide_item_names : undefined);
    }
  } catch (_) { /* чтение состояния не поддержано облаком — тумблеры остаются как есть */ }
}

/* ----------------------------- 3D tilt карточек -----------------------------
   .card и .panel наклоняются за курсором (inline-перспектива + rotateX/Y) и
   подсвечиваются бликом (--glare-x/--glare-y, CSS в style.css). Включаем только
   при точном указателе (мышь) и без prefers-reduced-motion — на тач-устройствах
   класс .tilt не ставится вовсе; элементы с .no-tilt пропускаются. */
(function initTilt() {
  if (!matchMedia('(hover: hover) and (pointer: fine)').matches) return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  document.querySelectorAll('.card, .panel').forEach(el => {
    if (el.classList.contains('no-tilt') || el.querySelector('.term-wrap')) return;
    const maxAngle = el.classList.contains('card') ? 7 : 4;
    el.classList.add('tilt');
    el.addEventListener('pointermove', event => {
      if (event.pointerType !== 'mouse') return;
      const rect = el.getBoundingClientRect();
      if (!rect.width || !rect.height) return;   // скрытая секция — не считаем
      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;
      el.classList.add('live');
      el.style.transform = `perspective(900px) rotateX(${(-y * maxAngle).toFixed(2)}deg) rotateY(${(x * maxAngle).toFixed(2)}deg)`;
      el.style.setProperty('--glare-x', ((x + 0.5) * 100).toFixed(1) + '%');
      el.style.setProperty('--glare-y', ((y + 0.5) * 100).toFixed(1) + '%');
    });
    el.addEventListener('pointerleave', () => {
      el.classList.remove('live');
      el.style.transform = '';
      el.style.removeProperty('--glare-x');
      el.style.removeProperty('--glare-y');
    });
  });
})();

/* -------------------------------- запуск -------------------------------- */
(async function start() {
  showTab(localStorage.getItem('sp_tab') || 'overview');
  connectTerminal();
  await refreshStatus();
  loadVoiceConfig();
  // если колонка недоступна по сохранённому IP — пробуем найти автоматически
  if (state.status && !state.status.reachable) {
    const detect = await detectStation(true);
    if (detect && detect.found) {
      await api('/api/config', { method: 'PUT', body: JSON.stringify({ station_ip: detect.ip }) });
      await refreshStatus();
    }
  }
  await loadSettings();
  await loadPlugins();
  await refreshSensors();
  refreshVolume();
  refreshTelemetry();

  setInterval(refreshStatus, 5000);
  setInterval(refreshSensors, 3000);
  setInterval(() => { if (!document.hidden) refreshVolume(); }, 3000);
  setInterval(() => { if ($('tab-plugins').classList.contains('active')) loadPlugins(); }, 15000);
})();
