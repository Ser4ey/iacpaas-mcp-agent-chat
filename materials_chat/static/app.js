/*
 * Интерфейс чата с ассистентом по базе материалов IACPaaS.
 *
 * Модули:
 *   api.js       — запросы к серверу и чтение потока SSE;
 *   markdown.js  — безопасный рендер ответа модели (только DOM-узлы);
 *   checks.js    — сверка чисел, ход и вердикт проверки ответа, переход по цитатам;
 *   trace.js     — журнал вызовов MCP-инструментов;
 *   data-view.js — раздел «Данные»;
 *   context-view.js — панель «Что видит модель» (история, которая уйдёт в модель);
 *   format.js    — числа, склонения, рубли, даты.
 * Здесь — состояние, разделы, список диалогов, отправка вопроса и индикатор расхода.
 */
import { api, streamChat, streamVerify } from './api.js';
import { renderContextView } from './context-view.js';
import { renderDataView, renderIndexStatus } from './data-view.js';
import { formatCompact, formatCost, formatNumber, formatShortDate, plural } from './format.js';
import { formatElapsed, highlightNumbers, numberCheckNode, progressNode, verdictNode } from './checks.js';
import { renderMarkdown } from './markdown.js';
import { ToolLog, normalizeTrace, toolActivity } from './trace.js';

const $ = (id) => document.getElementById(id);

const DEFAULT_SUGGESTIONS = [
  'Какой химический состав у стали 12Х18Н10Т?',
  'Какие сплавы на основе титана есть в справочнике?',
  'Какие свойства описаны для сплава Д16?',
  'В каких единицах записан предел прочности?',
];

const STATUS_POLL_MS = 12000;
const STATUS_POLL_BUSY_MS = 3000; // пока обновляется индекс, опрашиваем чаще
const NARROW_SCREEN = window.matchMedia('(max-width: 820px)');

const state = {
  view: 'chat',            // 'chat' или 'data'
  status: null,            // ответ GET /api/status
  overview: null,          // ответ GET /api/data/overview
  overviewError: '',
  sessions: [],            // краткий список диалогов
  session: null,           // открытый диалог с сообщениями
  streaming: false,
  controller: null,        // AbortController текущего ответа (кнопка «Стоп»)
  refreshRequested: false, // нажата «Обновить из IACPaaS», ждём ответа сервера
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/* ---------- Ошибки ---------- */

function showError(error) {
  $('error-text').textContent = typeof error === 'string' ? error : error?.message ?? 'Неизвестная ошибка.';
  $('error-notice').hidden = false;
}

function hideError() {
  $('error-notice').hidden = true;
}

/* ---------- Разделы «Диалоги» и «Данные» ---------- */

function viewFromHash() {
  return location.hash === '#data' ? 'data' : 'chat';
}

function showView(view) {
  state.view = view;
  $('view-chat').hidden = view !== 'chat';
  $('view-data').hidden = view !== 'data';
  $('meter').hidden = view !== 'chat';
  for (const [name, link] of [['chat', $('nav-chat')], ['data', $('nav-data')]]) {
    if (name === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  renderNotices();
  closeSidebar();
}

function goToView(view) {
  const hash = view === 'data' ? '#data' : '#chat';
  if (location.hash !== hash) history.pushState(null, '', hash);
  showView(view);
}

/** Вопрос из подсказки: вставляем в поле ввода и переходим в диалог. Отправляет пользователь. */
function askQuestion(text) {
  goToView('chat');
  const input = $('message-input');
  input.value = text;
  resizeInput();
  updateComposer();
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

/* ---------- Выезжающая панель диалогов (узкий экран) ---------- */

function openSidebar() {
  $('sidebar').classList.add('open');
  $('scrim').hidden = false;
  $('open-sidebar').setAttribute('aria-expanded', 'true');
  $('main').inert = true; // пока панель открыта, фокус не уходит под неё
  $('new-chat').focus();
}

function closeSidebar({ restoreFocus = false } = {}) {
  if (!$('sidebar').classList.contains('open')) return;
  $('sidebar').classList.remove('open');
  $('scrim').hidden = true;
  $('open-sidebar').setAttribute('aria-expanded', 'false');
  $('main').inert = false;
  if (restoreFocus) $('open-sidebar').focus();
}

/* ---------- Состояние сервера ---------- */

function isIndexBusy(status) {
  const index = status?.index ?? {};
  return Boolean(index.busy || index.refresh_running);
}

async function loadStatus() {
  const wasBusy = isIndexBusy(state.status);
  try {
    state.status = await api('/api/status');
  } catch (error) {
    $('meter-model').textContent = 'нет связи с сервером';
    throw error;
  }
  renderStatus();
  // Индекс обновился — описание данных могло измениться.
  if (wasBusy && !isIndexBusy(state.status)) loadOverview();
}

function scheduleStatusPoll() {
  const busy = state.refreshRequested || isIndexBusy(state.status);
  setTimeout(async () => {
    await loadStatus().catch(() => {});
    scheduleStatusPoll();
  }, busy ? STATUS_POLL_BUSY_MS : STATUS_POLL_MS);
}

function renderStatus() {
  renderMeter();
  renderNotices();
  updateComposer();
  renderIndexStatus($('index-status'), state.status, { requested: state.refreshRequested, onRefresh: refreshIndex });
}

/** Узкие уведомления: модель не настроена; данные не загружены. */
function renderNotices() {
  const status = state.status;
  const setup = $('setup-notice');
  const indexNotice = $('index-notice');
  if (!status) return;

  setup.hidden = Boolean(status.configured);
  if (!status.configured) {
    const missing = status.missing_settings?.length ? status.missing_settings.join(', ') : 'ключ API и модель';
    setup.replaceChildren(
      el('strong', '', 'Модель не настроена.'),
      ` Укажите ${missing} в файле .env и перезапустите приложение. Ключ остаётся на сервере.`,
    );
  }

  const index = status.index ?? {};
  // В разделе «Данные» состояние индекса и так показано подробно.
  indexNotice.hidden = index.ready !== false || state.view === 'data';
  if (!indexNotice.hidden) {
    const text = isIndexBusy(status)
      ? `Загружаем данные из IACPaaS${index.current_resource ? `: ${index.current_resource}` : ''}…`
      : 'Данные из IACPaaS загружены не полностью — поиск будет неполным.';
    const link = el('a', '', 'Открыть «Данные»');
    link.href = '#data';
    indexNotice.replaceChildren(el('span', '', text), link);
  }
}

/** Индикатор в шапке: модель · контекст (полоса) · стоимость диалога. */
function renderMeter() {
  const status = state.status ?? {};
  const info = status.model_info ?? {};
  const model = $('meter-model');
  model.textContent = shortModelName(info.name) || status.model || '';
  model.title = describeModel(status);

  const session = state.session;
  const tokens = session?.context_tokens;
  const budget = session?.context_budget || status.limits?.context_budget || info.context_window;
  const context = $('meter-context');
  context.hidden = !(typeof tokens === 'number' && budget > 0);
  if (!context.hidden) {
    const share = Math.min(1, tokens / budget);
    // Полоса видна даже при доле меньше процента.
    $('meter-fill').style.width = `${tokens > 0 ? Math.max(share * 100, 2) : 0}%`;
    context.classList.toggle('is-high', share > 0.85);
    const approx = session.context_estimated ? '≈ ' : '';
    $('meter-context-text').replaceChildren(
      el('span', 'meter-label', 'контекст '),
      `${approx}${formatCompact(tokens)}`,
      el('span', 'meter-budget', ` из ${formatCompact(budget)}`),
    );
    context.title = `Контекст следующего вопроса: ${approx}${formatNumber(tokens)} из ${formatNumber(budget)} токенов `
      + `(${(share * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} %). Нажмите, чтобы посмотреть, что уйдёт в модель.`;
  }

  const usage = session?.usage ?? {};
  const cost = formatCost(usage.cost, usage.currency ?? info.currency);
  const costNode = $('meter-cost');
  costNode.hidden = !cost;
  if (cost) {
    costNode.replaceChildren(el('span', 'meter-label', 'диалог '), costPrefix(usage, cost) + cost);
    costNode.title = 'Стоимость всех ответов в этом диалоге';
  }
}

/** «DeepSeek: DeepSeek V4.1 Flash» → «DeepSeek V4.1 Flash». */
function shortModelName(name) {
  if (!name) return '';
  const position = name.indexOf(': ');
  return position > 0 ? name.slice(position + 2) : name;
}

function describeModel(status) {
  const info = status.model_info ?? {};
  const lines = [[status.provider, status.model].filter(Boolean).join(' · ')];
  const prices = [info.price_input_per_million, info.price_output_per_million];
  if (prices.every((price) => typeof price === 'number')) {
    const [input, output] = prices.map((price) => formatCost(price, info.currency));
    lines.push(`1 млн токенов: вход ${input}, выход ${output}`);
  } else {
    lines.push('Цены модели неизвестны');
  }
  if (info.context_window) lines.push(`Окно контекста: ${formatNumber(info.context_window)} токенов`);
  return lines.join('\n');
}

/** «≥», если провайдер прислал расход не по всем обращениям. */
function costPrefix(usage, cost) {
  return usage.cost_known === false && !cost.startsWith('<') ? '≥ ' : '';
}

/* ---------- Раздел «Данные» ---------- */

async function loadOverview() {
  try {
    state.overview = await api('/api/data/overview');
    state.overviewError = '';
  } catch (error) {
    state.overview = null;
    state.overviewError = error.message;
  }
  renderSuggestions();
  renderData();
}

function renderData() {
  renderDataView($('data-content'), {
    overview: state.overview,
    error: state.overviewError,
    resources: state.status?.resources,
    onAsk: askQuestion,
  });
  renderIndexStatus($('index-status'), state.status, { requested: state.refreshRequested, onRefresh: refreshIndex });
}

async function refreshIndex() {
  if (state.refreshRequested) return;
  state.refreshRequested = true;
  renderStatus();
  try {
    await api('/api/index/refresh', { method: 'POST', body: '{}' });
  } catch (error) {
    showError(error);
  } finally {
    state.refreshRequested = false;
    // Сразу после запуска индекс уже «busy»: следующий опрос пойдёт в ускоренном режиме.
    await loadStatus().catch(showError);
  }
}

/* ---------- Список диалогов ---------- */

async function loadSessions() {
  const data = await api('/api/sessions');
  state.sessions = data.sessions ?? [];
  renderSessionList();
}

function renderSessionList() {
  const list = $('session-list');
  if (!state.sessions.length) {
    list.replaceChildren(el('p', 'session-empty', 'Диалогов пока нет. История хранится локально.'));
    return;
  }
  list.replaceChildren(...state.sessions.map(sessionRow));
}

function sessionRow(session) {
  const title = session.title || 'Новый диалог';
  const active = session.id === state.session?.id;
  const row = el('div', active ? 'session is-active' : 'session');

  const open = el('button', 'session-open');
  open.type = 'button';
  open.title = title;
  open.disabled = state.streaming;
  if (active) open.setAttribute('aria-current', 'true');
  const cost = formatCost(session.usage?.cost, session.usage?.currency);
  const meta = [formatShortDate(session.updated_at), cost].filter(Boolean).join(' · ');
  open.append(el('span', 'session-title', title), el('span', 'session-meta', meta));
  open.addEventListener('click', () => openSession(session.id).catch(showError));

  const remove = el('button', 'icon-button session-delete');
  remove.type = 'button';
  remove.title = 'Удалить диалог';
  remove.setAttribute('aria-label', `Удалить диалог «${title}»`);
  remove.disabled = state.streaming;
  remove.append($('icon-delete').content.cloneNode(true));
  remove.addEventListener('click', () => deleteSession(session).catch(showError));

  row.append(open, remove);
  return row;
}

async function openSession(id, { stayInView = false } = {}) {
  if (state.streaming) return;
  state.session = await api(`/api/sessions/${encodeURIComponent(id)}`);
  state.session.messages ??= [];
  renderConversation();
  renderSessionList();
  renderMeter();
  if (!stayInView) goToView('chat');
  closeSidebar();
  // Новый диалог начинается сверху, открытый — с последнего сообщения.
  if (state.session.messages.length) scrollToEnd(true);
  else $('conversation').scrollTop = 0;
}

async function newSession({ stayInView = false } = {}) {
  if (state.streaming) return;
  if (!stayInView) goToView('chat');
  // Пустой диалог переиспользуем, чтобы повторные нажатия не плодили пустые строки.
  if (state.session && !state.session.messages?.length) {
    closeSidebar();
    $('message-input').focus();
    return;
  }
  const created = await api('/api/sessions', { method: 'POST', body: '{}' });
  await loadSessions();
  await openSession(created.id, { stayInView });
  if (!stayInView) $('message-input').focus();
}

async function deleteSession(session) {
  const title = session.title || 'Новый диалог';
  if (!window.confirm(`Удалить диалог «${title}» вместе с историей?`)) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(session.id)}`, { method: 'DELETE' });
  } finally {
    await loadSessions(); // даже при ошибке: список мог устареть (диалог уже удалён в другой вкладке)
  }
  if (state.session?.id === session.id) {
    state.session = null;
    if (state.sessions.length) await openSession(state.sessions[0].id, { stayInView: true });
    else await newSession();
  }
}

/* ---------- Диалог как документ ---------- */

function renderConversation() {
  const session = state.session;
  const messages = (session?.messages ?? []).filter((m) => m.role === 'user' || m.role === 'assistant');
  $('chat-heading').textContent = session?.title ? `Диалог: ${session.title}` : 'Новый диалог';
  $('messages').replaceChildren();
  let turn = null;
  for (const message of messages) {
    if (message.role === 'user' || !turn) turn = appendTurn(message.role === 'user' ? message.content : null);
    if (message.role === 'assistant') showSavedAnswer(createAnswerView(turn), message);
  }
  $('intro').hidden = messages.length > 0;
}

function renderSuggestions() {
  const questions = state.overview?.suggested_questions?.length
    ? state.overview.suggested_questions.slice(0, 4)
    : DEFAULT_SUGGESTIONS;
  $('suggestions').replaceChildren(...questions.map((question) => {
    const item = el('li');
    const button = el('button', 'suggestion', question);
    button.type = 'button';
    button.addEventListener('click', () => askQuestion(question));
    item.append(button);
    return item;
  }));
}

/** Ход диалога: вопрос (выделенный абзац), под ним — журнал и ответ. */
function appendTurn(question) {
  const turn = el('article', 'turn');
  if (question !== null) turn.append(el('p', 'question', question));
  $('messages').append(turn);
  return turn;
}

function createAnswerView(turn) {
  const root = el('div', 'answer');
  const log = el('div', 'tool-log');
  log.setAttribute('role', 'group');
  log.setAttribute('aria-label', 'Обращения к данным');
  const status = el('p', 'turn-status');
  status.hidden = true;
  const body = el('div', 'answer-body prose');
  const errors = el('div', 'answer-errors');
  const meta = el('div', 'answer-meta');
  const verdict = el('div', 'answer-verdict-slot');
  // Текст, ошибки, строка расхода и вердикт проверки — одна карточка под журналом инструментов.
  const card = el('div', 'answer-card');
  card.append(body, errors, meta, verdict);
  root.append(log, status, card);
  turn.append(root);
  return {
    root, status, body, errors, meta, verdict,
    tools: new ToolLog(log),
    content: '',
    usage: null,
    grounded: null,
    numberCheck: null,
    messageId: null,      // id сохранённого сообщения: без него проверить ответ нельзя
    verification: null,
    verifying: false,
    verifyError: '',
    progress: null,       // ход проверки: {claims, startedAt}
    verdictOpen: false,   // список утверждений раскрыт сразу после проверки, у сохранённых — свёрнут
    truncated: false,
    done: false,
    renderQueued: false,
  };
}

function showSavedAnswer(view, message) {
  view.content = message.content ?? '';
  view.usage = message.usage ?? null;
  view.grounded = message.grounded ?? null;
  view.numberCheck = message.number_check ?? null;
  view.messageId = message.id ?? null;
  view.verification = message.verification ?? null;
  normalizeTrace(message.trace).forEach((call) => view.tools.update(call));
  renderMarkdown(view.body, view.content);
  highlightNumbers(view.body, view.numberCheck?.unconfirmed ?? []);
  // Остановка или сбой хода хранятся отдельно от текста ответа (поле error).
  if (message.error?.message) addAnswerError(view, message.error.message, message.error.code === 'cancelled');
  renderAnswerMeta(view);
  renderVerdict(view);
}

/** Тихая строка под ответом: токены · стоимость · «без обращения к базе» · сверка чисел · Копировать. */
function renderAnswerMeta(view) {
  const usage = view.usage ?? {};
  const parts = [];
  if (usage.total_tokens) {
    const tokens = el('span', '', `${usage.known === false ? '≥ ' : ''}${plural(usage.total_tokens, ['токен', 'токена', 'токенов'])}`);
    tokens.title = `Вход ${formatNumber(usage.input_tokens ?? 0)}, выход ${formatNumber(usage.output_tokens ?? 0)}`
      + (usage.requests ? `; обращений к модели: ${usage.requests}` : '');
    parts.push(tokens);
  }
  // Нулевую стоимость без токенов (ответ остановлен до обращения к модели) не показываем.
  const cost = usage.total_tokens || usage.cost > 0 ? formatCost(usage.cost, usage.currency) : '';
  if (cost) parts.push(el('span', '', costPrefix(usage, cost) + cost));
  if (view.grounded === false) parts.push(el('span', '', 'без обращения к базе'));
  if (view.truncated) parts.push(el('span', '', 'ответ обрезан по лимиту длины'));
  const check = numberCheckNode(view.numberCheck);
  if (check) parts.push(check);
  if (view.content) parts.push(copyButton(view));
  if (view.content && view.messageId) parts.push(verifyButton(view));
  view.meta.replaceChildren(...parts);
}

function copyButton(view) {
  const button = el('button', 'link-button', 'Копировать');
  button.type = 'button';
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(view.content);
      button.textContent = 'Скопировано';
    } catch {
      button.textContent = 'Не удалось скопировать';
    }
    setTimeout(() => { button.textContent = 'Копировать'; }, 2000);
  });
  return button;
}

/** Заметная кнопка проверки ответа: важное действие, поэтому в цвет акцента, а не тихой ссылкой. */
function verifyButton(view) {
  const label = view.verifying ? 'Проверяю…' : view.verification ? 'Проверить снова' : 'Проверить';
  const button = el('button', 'verify-button', label);
  button.type = 'button';
  button.disabled = view.verifying;
  button.prepend(view.verifying ? el('span', 'verify-spinner') : shieldIcon());
  button.title = 'Сверить утверждения ответа с данными из базы; проверка оплачивается отдельно';
  button.addEventListener('click', () => verifyAnswer(view));
  return button;
}

function shieldIcon() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M8 1.8 3 3.6v4c0 3 2.1 5.3 5 6.6 2.9-1.3 5-3.6 5-6.6v-4L8 1.8ZM5.8 8l1.6 1.6 3-3.2');
  svg.append(path);
  return svg;
}

/** Блок под строкой расхода: ход проверки, затем вердикт; повторная проверка заменяет прежний блок. */
function renderVerdict(view) {
  const answer = view.body;
  if (view.verifyError) view.verdict.replaceChildren(el('p', 'answer-error', `Не удалось проверить: ${view.verifyError}`));
  else if (view.progress) {
    const elapsed = performance.now() - view.progress.startedAt;
    view.verdict.replaceChildren(progressNode(view.progress, { answer, elapsed }));
  } else if (view.verification) {
    view.verdict.replaceChildren(verdictNode(view.verification, { answer, open: view.verdictOpen }));
  } else view.verdict.replaceChildren();
}

async function verifyAnswer(view) {
  const session = state.session;
  if (view.verifying || !session || !view.messageId) return;
  const sessionId = session.id;
  view.verifying = true;
  view.verifyError = '';
  view.progress = { claims: [], startedAt: performance.now() };
  renderAnswerMeta(view);
  renderVerdict(view);
  // Блок хода проверки появляется под ответом — часто ниже края экрана.
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  view.verdict.scrollIntoView({ block: 'nearest', behavior: still ? 'auto' : 'smooth' });
  // Секундомер обновляет только своё поле: список утверждений при этом не перерисовывается.
  const timer = setInterval(() => {
    const node = view.verdict.querySelector('.verdict-timer');
    if (node && view.progress) node.textContent = formatElapsed(performance.now() - view.progress.startedAt);
  }, 1000);
  let finished = false;
  try {
    await streamVerify(sessionId, view.messageId, (name, data) => {
      if (name === 'claim') {
        view.progress.claims[data.index] = data.claim;
        renderVerdict(view);
      } else if (name === 'done') {
        finished = true;
        view.verification = data.verification;
        view.verdictOpen = true;
        // Диалог за время запроса могли сменить: тогда обновляем только вид этого ответа.
        if (state.session?.id === sessionId) {
          state.session.usage = data.session_usage ?? state.session.usage;
          const saved = state.session.messages.find((m) => m.id === view.messageId);
          if (saved) saved.verification = data.verification;
          renderMeter();
        }
        loadSessions().catch(() => {}); // стоимость диалога в списке
      } else if (name === 'error') {
        finished = true;
        view.verifyError = data.message;
      }
    });
    if (!finished) view.verifyError = 'Соединение прервалось до конца проверки. Повторите её.';
  } catch (error) {
    view.verifyError = error.message;
  } finally {
    clearInterval(timer);
    view.verifying = false;
    view.progress = null;
    renderAnswerMeta(view);
    renderVerdict(view);
  }
}

/** Панель «Что видит модель»: история диалога в том виде, в каком она уйдёт в модель со следующим вопросом. */
async function openContextView() {
  const session = state.session;
  if (!session) return;
  const dialog = $('context-dialog');
  const body = $('context-body');
  body.replaceChildren(el('p', 'context-note', 'Загружаю…'));
  if (!dialog.open) dialog.showModal();
  try {
    renderContextView(body, await api(`/api/sessions/${encodeURIComponent(session.id)}/context`));
  } catch (error) {
    body.replaceChildren(el('p', 'answer-error', `Не удалось загрузить: ${error.message}`));
  }
}

function setTurnStatus(view, text) {
  view.status.hidden = !text;
  view.status.textContent = text ?? '';
}

function addAnswerError(view, text, quiet = false) {
  view.errors.append(el('p', quiet ? 'answer-error is-quiet' : 'answer-error', text));
}

/** Во время потока перерисовываем Markdown не чаще раза за кадр. */
function queueAnswerRender(view) {
  if (view.renderQueued) return;
  view.renderQueued = true;
  requestAnimationFrame(() => {
    view.renderQueued = false;
    const follow = isNearBottom();
    renderMarkdown(view.body, view.content);
    // Кадр может наступить уже после finishAnswer: без этого перерисовка стёрла бы подсветку чисел.
    highlightNumbers(view.body, view.numberCheck?.unconfirmed ?? []);
    if (follow) scrollToEnd(true);
  });
}

function isNearBottom() {
  const pane = $('conversation');
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight < 160;
}

function scrollToEnd(force = false) {
  const pane = $('conversation');
  if (force || isNearBottom()) pane.scrollTop = pane.scrollHeight;
}

/* ---------- Отправка вопроса и поток ответа ---------- */

async function sendMessage(text) {
  if (!text || state.streaming || !state.status?.configured) return;
  if (!state.session) await newSession();
  const session = state.session;

  state.streaming = true;
  state.controller = new AbortController();
  hideError();
  $('intro').hidden = true;
  session.messages.push({ role: 'user', content: text });
  const view = createAnswerView(appendTurn(text));
  view.root.setAttribute('aria-busy', 'true');
  setTurnStatus(view, 'Изучает вопрос…');
  $('message-input').value = '';
  resizeInput();
  updateComposer();
  renderSessionList();
  scrollToEnd(true);

  try {
    await streamChat(session.id, text, state.controller.signal, (event, data) => {
      const follow = isNearBottom();
      handleStreamEvent(view, event, data);
      if (follow) scrollToEnd(true);
    });
    if (!view.done) addAnswerError(view, 'Поток оборвался до конца ответа. Проверьте соединение и повторите вопрос.');
  } catch (error) {
    if (error.name === 'AbortError') {
      addAnswerError(view, 'Ответ остановлен. Уже выполненные обращения к модели могут быть оплачены.', true);
    } else {
      addAnswerError(view, error.message);
      if (error.status === 503) loadStatus().catch(() => {});
    }
  } finally {
    finishAnswer(view);
    state.streaming = false;
    state.controller = null;
    updateComposer();
    renderSessionList();
    syncAfterTurn(session.id, view);
    $('message-input').focus();
  }
}

function handleStreamEvent(view, event, data) {
  switch (event) {
    case 'session':
      updateSessionTitle(data.title);
      break;
    case 'status':
      setTurnStatus(view, data.message || 'Работает…');
      break;
    case 'tool_start':
      view.tools.update(data);
      setTurnStatus(view, toolActivity(data.name));
      break;
    case 'tool_end':
      view.tools.update(data);
      setTurnStatus(view, 'Разбирает найденное…');
      break;
    case 'answer_delta':
      setTurnStatus(view, '');
      view.content += data.text ?? '';
      queueAnswerRender(view);
      break;
    case 'answer_meta':
      view.grounded = data.grounded ?? null;
      view.truncated = Boolean(data.truncated);
      break;
    case 'number_check':
      view.numberCheck = data;
      break;
    case 'usage':
      view.usage = data.turn ?? null;
      Object.assign(state.session, {
        usage: data.session ?? state.session.usage,
        context_tokens: data.context_tokens ?? state.session.context_tokens,
        context_budget: data.context_budget ?? state.session.context_budget,
        context_estimated: data.context_estimated ?? state.session.context_estimated,
      });
      renderMeter();
      break;
    case 'error':
      addAnswerError(view, data.message || 'Не удалось завершить ответ.');
      break;
    case 'done':
      view.done = true;
      view.messageId = data?.message_id ?? null;
      break;
    default:
      break; // незнакомые события пропускаем: так старый интерфейс переживёт новые поля сервера
  }
}

function finishAnswer(view) {
  view.tools.interruptPending();
  setTurnStatus(view, '');
  renderMarkdown(view.body, view.content);
  highlightNumbers(view.body, view.numberCheck?.unconfirmed ?? []);
  view.root.removeAttribute('aria-busy');
  renderAnswerMeta(view);
  state.session?.messages.push({
    role: 'assistant', content: view.content, trace: view.tools.calls(), usage: view.usage ?? {}, grounded: view.grounded,
    number_check: view.numberCheck, id: view.messageId, verification: null,
  });
  scrollToEnd();
}

function updateSessionTitle(title) {
  if (!title || !state.session) return;
  state.session.title = title;
  $('chat-heading').textContent = `Диалог: ${title}`;
  const summary = state.sessions.find((item) => item.id === state.session.id);
  if (summary) summary.title = title;
  renderSessionList();
}

/** После ответа берём итоги с сервера: после «Стоп» событие usage не приходит. */
async function syncAfterTurn(sessionId, view) {
  try {
    await loadSessions();
    if (state.session?.id !== sessionId || state.streaming) return;
    const saved = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (state.session?.id !== sessionId || state.streaming) return;
    state.session = { ...saved, messages: saved.messages ?? [] };
    renderMeter();
    if (!view.usage) {
      const last = state.session.messages.at(-1);
      if (last?.role === 'assistant' && last.usage) {
        view.usage = last.usage;
        renderAnswerMeta(view);
      }
    }
  } catch (error) {
    showError(error);
  }
}

/* ---------- Поле ввода ---------- */

function updateComposer() {
  const hasText = Boolean($('message-input').value.trim());
  $('send-button').hidden = state.streaming;
  $('stop-button').hidden = !state.streaming;
  $('send-button').disabled = !hasText || !state.status?.configured;
  $('new-chat').disabled = state.streaming;
}

function resizeInput() {
  const input = $('message-input');
  input.style.height = 'auto';
  input.style.height = `${Math.min(200, input.scrollHeight)}px`;
  // Полоса прокрутки нужна, только когда текст выше максимальной высоты поля.
  input.style.overflowY = input.scrollHeight > 200 ? 'auto' : 'hidden';
}

/* ---------- События ---------- */

function bindEvents() {
  $('chat-form').addEventListener('submit', (event) => {
    event.preventDefault();
    sendMessage($('message-input').value.trim()).catch(showError);
  });
  $('message-input').addEventListener('input', () => {
    resizeInput();
    updateComposer();
  });
  $('message-input').addEventListener('keydown', (event) => {
    // Enter — отправить, Shift+Enter — новая строка; во время набора через IME не отправляем.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('chat-form').requestSubmit();
    }
  });
  $('stop-button').addEventListener('click', () => state.controller?.abort());
  $('new-chat').addEventListener('click', () => newSession().catch(showError));
  $('dismiss-error').addEventListener('click', hideError);
  $('meter-context').addEventListener('click', () => openContextView());
  $('close-context').addEventListener('click', () => $('context-dialog').close());
  // Щелчок по затемнению вокруг панели закрывает её; Esc закрывает <dialog> сам.
  $('context-dialog').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });

  $('open-sidebar').addEventListener('click', openSidebar);
  $('close-sidebar').addEventListener('click', () => closeSidebar({ restoreFocus: true }));
  $('scrim').addEventListener('click', () => closeSidebar({ restoreFocus: true }));
  NARROW_SCREEN.addEventListener('change', () => closeSidebar());

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeSidebar({ restoreFocus: true });
  });
  window.addEventListener('hashchange', () => showView(viewFromHash()));
  window.addEventListener('popstate', () => showView(viewFromHash()));
}

async function initialize() {
  bindEvents();
  showView(viewFromHash());
  renderSuggestions();
  renderData();
  loadOverview(); // не ждём: подсказки и раздел «Данные» дорисуются, когда придёт ответ
  try {
    await Promise.all([loadStatus(), loadSessions()]);
    if (state.sessions.length) await openSession(state.sessions[0].id, { stayInView: true });
    else await newSession({ stayInView: true });
    if (state.view === 'data') renderData(); // список ресурсов для запасного варианта появился вместе со статусом
  } catch (error) {
    showError(`Не удалось загрузить приложение: ${error.message}`);
  }
  scheduleStatusPoll();
}

initialize();
