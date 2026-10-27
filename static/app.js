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
  localStorage.setItem('sp_tab', name);
}

document.querySelectorAll('.nav-item').forEach(item => item.onclick = () => showTab(item.dataset.tab));

$('logout').onclick = async () => {
  await api('/api/logout', { method: 'POST', body: '{}' }).catch(() => {});
  location.href = '/login';
};

/* ---------------------- автоопределение колонки ---------------------- */
let detecting = false;

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
    if (result.found) {
      $('ipText').textContent = result.ip;
      $('cfgIp').value = result.ip;
      if (!silent) notify($('cfgStatus'), 'найдена колонка: ' + result.ip, 'ok');
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
    $('cYamSub').textContent = data.device ? `${data.device.model} · ${data.device.architecture}` : (data.yam_error || '—');
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
      $('brandModel').textContent = `${data.device.model} · ${data.device.architecture}`;
      document.title = `Панель Станции — ${data.device.model}`;
    }

    $('linkYam').href = `http://${data.station_ip}:${state.settingsYamPort || 8099}/`;
    $('overviewUpdated').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');
  } catch (error) {
    $('dot').className = 'dot off';
    $('statusText').textContent = 'ошибка: ' + error.message;
  }
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

  // линия+заливка
  ctx.beginPath();
  values.forEach((_, i) => { i ? ctx.lineTo(i * step, yOf(i)) : ctx.moveTo(0, yOf(0)); });
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

  // тултип по позиции курсора
  if (cursorX == null) return;
  const index = Math.max(0, Math.min(values.length - 1, Math.round(cursorX / step)));
  const x = index * step;
  const y = yOf(index);

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

  const ago = Math.round((values.length - 1 - index) * 3);
  const when = ago <= 1 ? 'сейчас' : ago + ' с назад';
  const line = `${values[index].toFixed(1)} ${unit} · ${when}`;
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

/* ------------------------------- система ------------------------------- */
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

/* -------------------------------- запуск -------------------------------- */
(async function start() {
  showTab(localStorage.getItem('sp_tab') || 'overview');
  connectTerminal();
  await refreshStatus();
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

  setInterval(refreshStatus, 5000);
  setInterval(refreshSensors, 3000);
  setInterval(() => { if ($('tab-plugins').classList.contains('active')) loadPlugins(); }, 15000);
})();
