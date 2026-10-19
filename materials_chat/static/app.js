/* Browser state stays deliberately small. Credentials and model calls live on the server. */
const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  sessions: [],
  session: null,
  streaming: false,
  controller: null,
  refreshing: false,
};
const number = new Intl.NumberFormat('ru-RU');

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) {
    let message = `Сервер ответил ${response.status}`;
    try {
      const data = await response.json();
      message = data.detail || data.message || message;
      if (typeof message !== 'string') message = JSON.stringify(message);
    } catch { /* An upstream error can have a non-JSON response. */ }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function showError(message) {
  $('error-text').textContent = message;
  $('error-banner').hidden = false;
}

function updateComposer() {
  $('send-button').hidden = state.streaming;
  $('stop-button').hidden = !state.streaming;
  $('send-button').disabled = !state.status?.configured || !$('message-input').value.trim();
  $('new-chat').disabled = state.streaming;
  $('message-input').disabled = state.streaming;
}

function updateUsage(usage = {}, context = 0, estimated = false) {
  const tokens = usage.total_tokens || 0;
  $('session-usage').textContent = usage.known === false ? `Сессия: ${tokens ? `≥ ${number.format(tokens)} токенов` : 'расход не получен'}` : `Сессия: ${number.format(tokens)} токенов`;
  $('session-usage').title = `Вход: ${number.format(usage.input_tokens || 0)} · Выход: ${number.format(usage.output_tokens || 0)}. Включает промежуточные обращения модели.`;
  const limits = state.status?.limits || {};
  const limit = limits.context_tokens || limits.max_context_tokens || limits.model_context_tokens || 0;
  $('context-usage').textContent = `Контекст${estimated ? ' ≈' : ':'} ${number.format(context || 0)}${limit ? ` / ${number.format(limit)}` : ''}`;
  $('context-usage').title = 'Размер текущего контекста отличается от накопленного расхода токенов за весь диалог.';
  const ratio = limit ? Math.min(100, (context / limit) * 100) : 0;
  $('context-fill').style.width = `${ratio}%`;
  $('context-fill').style.background = ratio > 85 ? '#bf8953' : '#8bab78';
}

function renderStatus(data) {
  state.status = data;
  $('model-name').textContent = data.model || 'Модель не выбрана';
  $('connection-dot').classList.toggle('offline', !data.configured);
  $('setup-banner').hidden = data.configured;
  const missing = data.missing_settings || [];
  $('setup-text').textContent = `Укажите ${missing.length ? missing.join(', ') : 'ключ API и имя модели'} в локальном файле .env и перезапустите приложение. Ключи остаются на сервере.`;
  const resources = data.resources || [];
  $('resource-count').textContent = resources.length;
  $('resources-list').replaceChildren();
  resources.forEach((resource) => {
    const row = element('div', 'resource-item');
    const kind = String(resource.kind || 'Ресурс');
    row.append(
      element('div', 'resource-kind', /ontology|meta|мета/i.test(kind) ? 'Онтология' : 'Данные'),
      element('div', 'resource-title', resource.title || resource.id),
      element('div', 'resource-path', resource.path || ''),
    );
    $('resources-list').append(row);
  });
  const index = data.index || {};
  const busy = index.busy || index.refresh_running || state.refreshing;
  const errors = Object.entries(index.errors || {});
  if (data.configured && !index.ready) {
    $('setup-banner').hidden = false;
    $('setup-banner').querySelector('strong').textContent = busy ? 'Загружаем данные из IACPaaS' : 'Подготовьте источники для поиска';
    $('setup-text').textContent = busy ? `Обновление индекса продолжается${index.current_resource ? `: ${index.current_resource}` : ''}. Состояние доступно в панели «Источники».` : 'Откройте «Источники» и нажмите «Обновить из IACPaaS». Пока загружены не все ресурсы, поиск будет неполным.';
  } else {
    $('setup-banner').querySelector('strong').textContent = 'Настройте модель, чтобы начать диалог';
  }
  $('index-badge').textContent = busy ? 'Обновляется…' : errors.length ? 'Есть ошибки' : index.ready ? 'Готов' : 'Нужна загрузка';
  $('index-badge').classList.toggle('failed', errors.length > 0);
  $('index-description').textContent = `${index.resources_indexed || 0} из ${index.total_resources ?? resources.length} ресурсов · ${number.format(index.nodes || 0)} вершин в индексе${busy && index.current_resource ? `\nСейчас: ${index.current_resource}` : ''}`;
  $('index-time').textContent = index.refresh_error || (index.last_synced ? `Обновлён: ${formatDate(index.last_synced)}` : 'Первый поиск может потребовать загрузки ресурсов.');
  $('index-errors').replaceChildren();
  $('index-errors').hidden = errors.length === 0;
  if (errors.length) {
    $('index-errors').append(element('strong', '', 'Обновление неполное. Для этих ресурсов новые данные не получены:'));
    errors.forEach(([id, message]) => {
      const name = resources.find((resource) => resource.id === id)?.title || id;
      $('index-errors').append(element('p', '', `${name}: ${message}`));
    });
    $('index-errors').append(element('span', '', 'Ранее загруженные данные могут остаться доступны. Проверьте время их загрузки в журнале инструментов.'));
  }
  $('refresh-index').disabled = !!busy;
  $('refresh-index').textContent = busy ? 'Обновление источников…' : '↻ Обновить из IACPaaS';
  updateComposer();
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

async function loadStatus() {
  try { renderStatus(await api('/api/status')); }
  catch (error) { $('model-name').textContent = 'Нет связи с сервером'; $('connection-dot').classList.add('offline'); throw error; }
}

async function loadSessions() {
  const data = await api('/api/sessions');
  state.sessions = data.sessions || [];
  renderSessions();
}

function renderSessions() {
  const list = $('session-list');
  list.replaceChildren();
  if (!state.sessions.length) list.append(element('p', 'session-empty', 'Здесь появятся ваши диалоги. История хранится локально.'));
  for (const session of state.sessions) {
    const row = element('div', `session-row${state.session?.id === session.id ? ' active' : ''}`);
    const select = element('button', 'session-select', session.title || 'Новый диалог');
    select.title = session.title || 'Новый диалог';
    select.disabled = state.streaming;
    select.addEventListener('click', () => selectSession(session.id).catch((error) => showError(error.message)));
    const remove = element('button', 'session-delete', '×');
    remove.setAttribute('aria-label', `Удалить диалог «${session.title || 'Новый диалог'}»`);
    remove.disabled = state.streaming;
    remove.addEventListener('click', () => deleteSession(session).catch((error) => showError(error.message)));
    row.append(select, remove);
    list.append(row);
  }
}

async function selectSession(id) {
  if (state.streaming) return;
  state.session = await api(`/api/sessions/${encodeURIComponent(id)}`);
  $('chat-title').textContent = state.session.title || 'Новый диалог';
  $('messages').replaceChildren();
  const messages = state.session.messages || [];
  $('welcome').hidden = messages.length > 0;
  messages.filter((message) => message.role === 'user' || message.role === 'assistant').forEach(renderMessage);
  updateUsage(state.session.usage, state.session.context_tokens, state.session.context_estimated ?? true);
  renderSessions();
  $('sidebar').classList.remove('open');
  $('toggle-sidebar').setAttribute('aria-expanded', 'false');
  // New chats start at the heading. Only an existing conversation should jump
  // to its latest message; scrolling an oversized welcome screen hid its top.
  if (messages.length) scrollToEnd(true);
  else $('conversation').scrollTop = 0;
}

async function newSession() {
  if (state.streaming) return;
  // Reuse a pristine session, so repeatedly clicking "new" does not create empty rows.
  if (state.session && !(state.session.messages || []).length) {
    $('message-input').focus();
    $('sidebar').classList.remove('open');
    $('toggle-sidebar').setAttribute('aria-expanded', 'false');
    return;
  }
  const session = await api('/api/sessions', { method: 'POST', body: '{}' });
  await loadSessions();
  await selectSession(session.id);
  $('message-input').focus();
}

async function deleteSession(session) {
  if (!window.confirm(`Удалить диалог «${session.title || 'Новый диалог'}» и его историю?`)) return;
  await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' });
  await loadSessions();
  if (state.session?.id === session.id) {
    state.session = null;
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await newSession();
  }
}

/* Minimal inline Markdown uses DOM nodes only. No model output is inserted as HTML. */
function renderAnswer(target, text) {
  target.replaceChildren();
  const pattern = /(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|\*\*[^*]+\*\*|`[^`\n]+`)/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    target.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith('[')) {
      const link = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      const anchor = element('a', '', link[1]);
      anchor.href = link[2];
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      target.append(anchor);
    } else if (token.startsWith('**')) target.append(element('strong', '', token.slice(2, -2)));
    else target.append(element('code', '', token.slice(1, -1)));
    offset = match.index + token.length;
  }
  target.append(document.createTextNode(text.slice(offset)));
}

function normalizeTrace(trace) {
  const byId = new Map();
  for (const item of trace || []) {
    const data = item.data || item;
    const id = data.id || data.tool_call_id || `tool-${byId.size}`;
    byId.set(id, { ...byId.get(id), ...data, id });
  }
  return [...byId.values()];
}

function renderMessage(message) {
  const row = element('article', `message ${message.role}`);
  const body = element('div', 'message-body');
  const trace = element('details', 'tool-trace');
  const traceSummary = element('summary', '', 'Вызовы инструментов');
  const traceBody = element('div');
  const tools = new Map();
  trace.append(traceSummary, traceBody);
  trace.hidden = true;
  if (message.role === 'assistant') {
    const label = element('div', 'message-label');
    label.append(element('span', 'assistant-mark', 'М'), document.createTextNode('Ассистент'));
    row.append(label, trace);
    renderAnswer(body, message.content || '');
  } else body.textContent = message.content || '';
  row.append(body);
  const status = element('div', 'working-status');
  status.hidden = true;
  const statusText = element('span');
  status.append(element('span', 'pulse'), statusText);
  row.append(status);
  const meta = element('div', 'message-meta');
  row.append(meta);
  const view = { row, body, trace, traceSummary, traceBody, tools, status, statusText, meta, content: message.content || '' };
  normalizeTrace(message.trace).forEach((tool) => renderTool(view, tool));
  if (message.role === 'assistant' && message.content) renderMessageMeta(view, message.usage);
  $('messages').append(row);
  return view;
}

function pretty(value) {
  if (value === undefined) return 'Нет данных';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

function renderTool(view, update) {
  const id = update.id || `tool-${view.tools.size}`;
  const tool = { ...(view.tools.get(id)?.data || {}), ...update, id };
  let entry = view.tools.get(id);
  if (!entry) {
    entry = { node: element('div', 'tool-item'), data: tool };
    view.tools.set(id, entry);
    view.traceBody.append(entry.node);
  }
  entry.data = tool;
  entry.node.replaceChildren();
  const title = element('div', 'tool-title');
  const hasResult = Object.prototype.hasOwnProperty.call(tool, 'result');
  const complete = hasResult || tool.duration_ms !== undefined;
  title.append(element('span', '', tool.is_error ? '!' : complete ? '✓' : '◌'), element('span', 'tool-name', tool.name || 'Инструмент'));
  const milliseconds = Number.isFinite(tool.duration_ms) ? `${number.format(Math.round(tool.duration_ms))} мс` : '';
  title.append(element('span', `tool-state${tool.is_error ? ' failed' : ''}`, complete ? `${tool.is_error ? 'Ошибка' : 'Готово'}${milliseconds ? ` · ${milliseconds}` : ''}` : 'Выполняется…'));
  entry.node.append(title);
  if (hasResult) renderToolSources(entry.node, tool.result);
  if (tool.arguments !== undefined) {
    const args = element('details');
    const code = element('pre', '', pretty(tool.arguments));
    code.tabIndex = 0;
    args.append(element('summary', '', 'Аргументы'), code);
    entry.node.append(args);
  }
  if (hasResult) {
    const result = element('details');
    const code = element('pre', '', pretty(tool.result));
    code.tabIndex = 0;
    result.append(element('summary', '', tool.is_error ? 'Подробности ошибки' : 'Полный результат · JSON'), code);
    entry.node.append(result);
  }
  view.trace.hidden = false;
  const pending = [...view.tools.values()].some((item) => !Object.prototype.hasOwnProperty.call(item.data, 'result') && item.data.duration_ms === undefined);
  view.traceSummary.textContent = `${pending ? 'Поиск в источниках…' : 'Вызовы инструментов'} · ${view.tools.size}`;
}

function renderToolSources(target, result) {
  // The raw response remains available below; these visible labels explain
  // whether a result came from a local snapshot and when it was indexed.
  if (!result || typeof result !== 'object') return;
  const meta = result.meta || {};
  let sources = meta.sources || (meta.resource_id ? [meta] : []);
  if (!sources.length && result.data?.resources) {
    sources = result.data.resources.map((resource) => ({
      resource_title: resource.title,
      indexed_at: resource.indexed_at,
      served_from: resource.indexed ? 'local_index' : 'not_indexed',
    }));
  }
  if (sources.length) {
    const block = element('div', 'tool-sources');
    sources.forEach((source) => {
      const row = element('div', 'tool-source');
      const name = source.resource_title || source.resource_id || source.source || 'IACPaaS';
      row.append(element('strong', '', name));
      const date = element('span', '', source.indexed_at ? `IACPaaS · локальный индекс · ${formatDate(source.indexed_at)}` : 'IACPaaS · ресурс ещё не загружен в индекс');
      date.title = source.indexed_at || 'Время загрузки отсутствует';
      row.append(date);
      if (source.last_sync_error) row.append(element('span', 'source-warning', `Последнее обновление не удалось: ${source.last_sync_error}`));
      block.append(row);
    });
    target.append(block);
  }
  const data = result.data || {};
  if (data.coverage_complete === false) target.append(element('div', 'tool-warning', 'Поиск выполнен не по всем ресурсам: часть источников не загружена. Полный список — в результате.'));
  if (meta.result_size_limited || data.truncated || data.has_more) target.append(element('div', 'tool-note', 'Показана ограниченная выборка. В результате есть данные для продолжения поиска.'));
}

function renderMessageMeta(view, usage = {}) {
  view.meta.replaceChildren();
  const total = usage?.total_tokens || 0;
  if (total) {
    const label = element('span', '', `Вход ${number.format(usage.input_tokens || 0)} · Выход ${number.format(usage.output_tokens || 0)} · Всего ${number.format(total)} токенов`);
    label.title = 'Суммарно по всем обращениям модели в этом ответе, включая вызовы инструментов.';
    view.meta.append(label);
  }
  if (usage?.known === false) view.meta.append(element('span', '', 'Часть расхода токенов не получена от провайдера'));
  if (view.content) {
    const copy = element('button', 'copy-button', 'Копировать');
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(view.content); copy.textContent = 'Скопировано'; }
      catch { copy.textContent = 'Не удалось скопировать'; }
      setTimeout(() => { copy.textContent = 'Копировать'; }, 2000);
    });
    view.meta.append(copy);
  }
}

function scrollToEnd(force = false) {
  const pane = $('conversation');
  if (force || pane.scrollHeight - pane.scrollTop - pane.clientHeight < 180) pane.scrollTop = pane.scrollHeight;
}

function setWorking(view, text) {
  view.status.hidden = !text;
  view.statusText.textContent = text;
}

/* Fetch supports POST + streamed SSE; EventSource supports only GET. */
async function readEvents(response, onEvent) {
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  function consume() {
    buffer = buffer.replace(/\r\n/g, '\n');
    let boundary;
    while ((boundary = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = 'message';
      const data = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
      }
      if (data.length) onEvent(event, JSON.parse(data.join('\n')));
    }
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      consume();
    }
    buffer += decoder.decode();
    if (buffer.trim()) { buffer += '\n\n'; consume(); }
  } finally { reader.releaseLock(); }
}

async function submitMessage(event) {
  event.preventDefault();
  const text = $('message-input').value.trim();
  if (!text || state.streaming || !state.status?.configured) return;
  if (!state.session) await newSession();
  const sessionId = state.session.id;
  state.streaming = true;
  state.controller = new AbortController();
  $('error-banner').hidden = true;
  $('welcome').hidden = true;
  const userMessage = { role: 'user', content: text };
  state.session.messages.push(userMessage);
  renderMessage(userMessage);
  const view = renderMessage({ role: 'assistant', content: '' });
  view.trace.open = true;
  setWorking(view, 'Ассистент изучает запрос…');
  $('message-input').value = '';
  resizeInput();
  updateComposer();
  renderSessions();
  scrollToEnd(true);
  let turnUsage = {};
  let finished = false;
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
      signal: state.controller.signal,
    });
    if (!response.ok) {
      let detail = `Ошибка запроса: ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch { /* Keep status code. */ }
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    await readEvents(response, (name, data) => {
      switch (name) {
        case 'status': setWorking(view, data.message || 'Обработка запроса…'); break;
        case 'tool_start':
          renderTool(view, data);
          setWorking(view, `Вызов ${data.name}…`);
          break;
        case 'tool_end':
          renderTool(view, data);
          setWorking(view, 'Ассистент обрабатывает найденные данные…');
          break;
        case 'answer_delta':
          setWorking(view, 'Формируется ответ…');
          view.content += data.text || '';
          renderAnswer(view.body, view.content);
          break;
        case 'usage':
          turnUsage = data.turn || {};
          state.session.usage = data.session || {};
          state.session.context_tokens = data.context_tokens || 0;
          updateUsage(data.session, data.context_tokens, data.context_estimated);
          renderMessageMeta(view, turnUsage);
          break;
        case 'session':
          state.session.title = data.title || state.session.title;
          $('chat-title').textContent = state.session.title;
          break;
        case 'error':
          view.row.append(element('div', 'message-error', data.message || 'Не удалось завершить запрос.'));
          break;
        case 'done': finished = true; break;
        default: break;
      }
      scrollToEnd();
    });
    if (!finished) view.row.append(element('div', 'message-error', 'Поток завершился до подтверждения ответа. Проверьте соединение и повторите запрос.'));
  } catch (error) {
    view.row.append(element('div', 'message-error', error.name === 'AbortError' ? 'Генерация остановлена. Уже выполненные обращения могут учитываться провайдером.' : error.message));
  } finally {
    setWorking(view, '');
    // A disconnected stream may end while a tool is still pending. Never leave
    // an apparently running tool spinning after the turn has already stopped.
    for (const entry of [...view.tools.values()]) {
      if (!Object.prototype.hasOwnProperty.call(entry.data, 'result') && entry.data.duration_ms === undefined) {
        renderTool(view, { ...entry.data, is_error: true, result: { error: 'Вызов прерван; результат не получен.' } });
      }
    }
    state.streaming = false;
    state.controller = null;
    renderMessageMeta(view, turnUsage);
    state.session.messages.push({ role: 'assistant', content: view.content, trace: [...view.tools.values()].map((item) => item.data), usage: turnUsage });
    updateComposer();
    loadSessions().then(async () => {
      // Stop disconnects SSE before the final usage event. Read the saved
      // counters once more, so the session total does not stay stale.
      if (state.session?.id === sessionId && !state.streaming) {
        const saved = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
        if (state.session?.id === sessionId && !state.streaming) {
          updateUsage(saved.usage, saved.context_tokens, saved.context_estimated);
        }
      }
    }).catch((error) => showError(error.message));
    loadStatus().catch(() => {});
    $('message-input').focus();
    scrollToEnd();
  }
}

function resizeInput() {
  const input = $('message-input');
  input.style.height = 'auto';
  input.style.height = `${Math.min(180, input.scrollHeight)}px`;
}

async function refreshIndex() {
  if (state.refreshing) return;
  state.refreshing = true;
  if (state.status) renderStatus(state.status);
  try {
    await api('/api/index/refresh', { method: 'POST', body: '{}' });
  } catch (error) { showError(error.message); }
  finally {
    state.refreshing = false;
    await loadStatus().catch((error) => showError(error.message));
  }
}

$('chat-form').addEventListener('submit', (event) => submitMessage(event).catch((error) => showError(error.message)));
$('message-input').addEventListener('input', () => { resizeInput(); updateComposer(); });
$('message-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('chat-form').requestSubmit();
  }
});
$('stop-button').addEventListener('click', () => state.controller?.abort());
$('new-chat').addEventListener('click', () => newSession().catch((error) => showError(error.message)));
$('open-resources').addEventListener('click', () => { $('resources-panel').hidden = false; $('open-resources').setAttribute('aria-expanded', 'true'); $('close-resources').focus(); });
$('close-resources').addEventListener('click', () => { $('resources-panel').hidden = true; $('open-resources').setAttribute('aria-expanded', 'false'); $('open-resources').focus(); });
$('toggle-sidebar').addEventListener('click', () => { const open = $('sidebar').classList.toggle('open'); $('toggle-sidebar').setAttribute('aria-expanded', String(open)); });
$('dismiss-error').addEventListener('click', () => { $('error-banner').hidden = true; });
$('refresh-index').addEventListener('click', refreshIndex);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (!$('resources-panel').hidden) { $('resources-panel').hidden = true; $('open-resources').setAttribute('aria-expanded', 'false'); $('open-resources').focus(); }
    if ($('sidebar').classList.contains('open')) { $('sidebar').classList.remove('open'); $('toggle-sidebar').setAttribute('aria-expanded', 'false'); $('toggle-sidebar').focus(); }
  }
});
document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => {
  $('message-input').value = button.dataset.prompt;
  resizeInput();
  updateComposer();
  $('message-input').focus();
}));

(async function initialize() {
  try {
    await Promise.all([loadStatus(), loadSessions()]);
    if (state.sessions.length) await selectSession(state.sessions[0].id);
    else await newSession();
  } catch (error) { showError(`Не удалось загрузить приложение: ${error.message}`); }
  // Only lightweight status is polled; the corpus is never reloaded by polling.
  setInterval(() => loadStatus().catch(() => {}), 12000);
})();
