/*
 * Журнал вызовов MCP-инструментов под вопросом.
 * Одна строка на вызов: имя · главное из аргументов · итог · время · откуда данные.
 * По клику строка раскрывается и показывает аргументы и JSON результата.
 */
import { formatDuration, plural } from './format.js';

// Откуда пришли данные (meta.served_from): подпись и вид метки.
const SOURCE_LABELS = {
  iacpaas_live: ['IACPaaS', 'is-live'],
  local_index: ['индекс', 'is-index'],
  local_index_fallback: ['индекс — платформа недоступна', 'is-fallback'],
};

// Части JSON для подсветки: строка (ключ, если за ней двоеточие), литерал, число.
const JSON_TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

// Что делает агент, пока инструмент выполняется (строка статуса под журналом).
const TOOL_ACTIVITY = {
  search_graph: 'Ищет в базе',
  get_subgraph: 'Читает вершины графа',
  get_inforesource: 'Читает описание ресурса',
  list_inforesources: 'Смотрит список ресурсов',
  count_graph: 'Считает вершины',
  validate_resource: 'Проверяет структуру ресурса',
  find_materials: 'Подбирает марки по составу',
  write_inforesource: 'Готовит запись в IACPaaS',
  delete_inforesource: 'Готовит удаление в IACPaaS',
};

// Итог карточки подтверждения записи (confirmation.status).
const CONFIRMATION_STATUS = {
  accepted: ['Подтверждено', 'is-accepted'],
  declined: ['Отменено — запись не выполнена', 'is-declined'],
  expired: ['Время вышло — запись не выполнена', 'is-declined'],
  cancelled: ['Ход остановлен — запись не выполнена', 'is-declined'],
  pending: ['Решение не принято — запись не выполнена', 'is-declined'],
};

const NODES = ['вершина', 'вершины', 'вершин'];

export function toolActivity(name) {
  return `${TOOL_ACTIVITY[name] ?? `Вызывает ${name}`}…`;
}

/** Сохранённый trace может содержать и старт, и конец вызова — склеиваем по id. */
export function normalizeTrace(trace) {
  const calls = new Map();
  for (const item of trace ?? []) {
    const data = item?.data ?? item;
    if (!data || typeof data !== 'object') continue;
    const id = data.id ?? data.tool_call_id ?? `tool-${calls.size}`;
    calls.set(id, { ...calls.get(id), ...data, id });
  }
  return [...calls.values()];
}

/** Журнал одного ответа. */
export class ToolLog {
  constructor(container) {
    this.container = container;
    this.rows = new Map(); // id → { node, call }
  }

  /** Добавляет вызов или дополняет уже показанный (tool_start → tool_end). */
  update(data) {
    const id = data.id ?? `tool-${this.rows.size}`;
    let row = this.rows.get(id);
    if (!row) {
      row = { node: createRowNode(), call: {} };
      row.node.addEventListener('toggle', () => fillDetails(row));
      this.rows.set(id, row);
      this.container.append(row.node);
    }
    row.call = { ...row.call, ...data, id };
    renderRow(row);
    renderConfirmation(row);
  }

  /**
   * Запрос подтверждения записи (событие confirm_request): карточка у вызова с кнопками и отсчётом.
   * decide(id, accept) отправляет решение на сервер; пока оно не принято, запись не выполняется.
   */
  confirm(request, decide) {
    const id = request.call_id ?? `confirm-${request.id}`;
    if (!this.rows.has(id)) this.update({ id, name: request.tool ?? 'запись' });
    const row = this.rows.get(id);
    row.decide = decide;
    row.deadline = Date.now() + (request.timeout_seconds ?? 120) * 1000;
    this.update({ id, confirmation: { ...request, status: 'pending' } });
  }

  /** Поток оборвался — не оставляем «выполняющиеся» строки навсегда. */
  interruptPending() {
    for (const row of this.rows.values()) {
      if (isPending(row.call)) {
        const confirmation = row.call.confirmation?.status === 'pending'
          ? { ...row.call.confirmation, status: 'cancelled' } : row.call.confirmation;
        this.update({ ...row.call, confirmation, is_error: true, result: { ok: false, error: { message: 'вызов прерван' } } });
      }
    }
  }

  calls() {
    return [...this.rows.values()].map((row) => row.call);
  }
}

/* ---------- Строка журнала ---------- */

function createRowNode() {
  const details = document.createElement('details');
  details.className = 'tool';
  const summary = document.createElement('summary');
  const body = document.createElement('div');
  body.className = 'tool-details';
  details.append(summary, body);
  return details;
}

function renderRow(row) {
  const { node, call } = row;
  const failed = isFailed(call);
  const pending = isPending(call);
  node.classList.toggle('is-error', failed);
  node.classList.toggle('is-pending', pending);

  let parts;
  if (pending) {
    parts = [span('', 'выполняется…')];
  } else {
    parts = failed
      ? [span('tool-flag', 'ошибка'), span('tool-error-text', errorText(call.result))]
      : [span('', describeResult(call.name, call.result))];
    parts.push(span('', formatDuration(call.duration_ms)), sourceLabel(call.result));
  }
  // Пробел между частями — место, где длинная строка может перенестись.
  const facts = span('tool-facts');
  parts.filter((part) => part?.textContent).forEach((part, position) => {
    if (position) facts.append(' ');
    facts.append(part);
  });

  const args = span('tool-args', describeArguments(call.name, call.arguments));
  node.firstChild.replaceChildren(span('tool-name', call.name ?? 'инструмент'), args, facts);
  node.firstChild.title = 'Показать аргументы и результат';
  if (node.open) fillDetails(row);
}

/* ---------- Карточка подтверждения записи ---------- */

function renderConfirmation(row) {
  const confirmation = row.call.confirmation;
  if (!confirmation) {
    row.card?.remove();
    return;
  }
  if (!row.card) {
    row.card = document.createElement('div');
    row.card.className = 'confirm-card';
    row.card.setAttribute('role', 'group');
    row.card.setAttribute('aria-label', 'Подтверждение записи в IACPaaS');
    row.node.after(row.card);
  }
  const live = confirmation.status === 'pending' && row.decide && !row.deciding;
  row.card.classList.toggle('is-danger', Boolean(confirmation.danger));
  row.card.classList.toggle('is-open', live);
  const title = span('confirm-title', confirmation.danger ? 'Подтвердите удаление' : 'Подтвердите запись в IACPaaS');
  const message = span('confirm-message', confirmation.message ?? '');
  const parts = [title, message];
  clearInterval(row.timer);
  if (live) {
    const accept = button(confirmation.danger ? 'Удалить' : 'Подтвердить', 'confirm-accept');
    const decline = button('Отменить', 'confirm-decline');
    const countdown = span('confirm-countdown');
    const tick = () => {
      const left = Math.max(0, Math.ceil((row.deadline - Date.now()) / 1000));
      countdown.textContent = left ? `осталось ${left} с` : 'время вышло';
      if (!left) {
        clearInterval(row.timer);
        accept.disabled = decline.disabled = true;
      }
    };
    tick();
    row.timer = setInterval(tick, 1000);
    accept.addEventListener('click', () => sendDecision(row, true));
    decline.addEventListener('click', () => sendDecision(row, false));
    const actions = span('confirm-actions');
    actions.append(accept, decline, countdown);
    parts.push(actions);
  } else if (row.deciding) {
    parts.push(span('confirm-status', 'Отправляю решение…'));
  } else {
    const [text, kind] = CONFIRMATION_STATUS[confirmation.status] ?? CONFIRMATION_STATUS.pending;
    parts.push(span(`confirm-status ${kind}`, text));
  }
  row.card.replaceChildren(...parts);
}

async function sendDecision(row, accept) {
  row.deciding = true;
  renderConfirmation(row);
  let status;
  try {
    status = (await row.decide(row.call.confirmation.id, accept)).status;
  } catch {
    status = 'expired'; // запрос уже закрыт: время вышло или ход остановлен
  }
  row.deciding = false;
  row.call.confirmation = { ...row.call.confirmation, status };
  renderConfirmation(row);
}

function button(text, className) {
  const node = document.createElement('button');
  node.type = 'button';
  node.className = className;
  node.textContent = text;
  return node;
}

/** Аргументы и результат строим только при раскрытии: JSON бывает большим. */
function fillDetails(row) {
  const body = row.node.lastChild;
  if (!row.node.open) return;
  const parts = [label('Аргументы'), jsonBlock(row.call.arguments ?? {})];
  if ('result' in row.call) parts.push(label('Результат'), jsonBlock(row.call.result));
  body.replaceChildren(...parts);
}

function label(text) {
  const node = document.createElement('p');
  node.className = 'tool-label';
  node.textContent = text;
  return node;
}

function jsonBlock(value) {
  const pre = document.createElement('pre');
  if (typeof value === 'string') pre.textContent = value;
  else pre.append(...highlightJson(JSON.stringify(value, null, 2)));
  pre.tabIndex = 0; // прокручивается с клавиатуры
  return pre;
}

/** Подсветка JSON: только текстовые узлы и span, данные не разбираются как HTML. */
function highlightJson(text) {
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(JSON_TOKEN)) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const [token, string, colon, literal] = match;
    if (string && colon) parts.push(span('json-key', string), colon);
    else parts.push(span(string ? 'json-string' : literal ? 'json-literal' : 'json-number', token));
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function span(className, text) {
  const node = document.createElement('span');
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/* ---------- Что показать в строке ---------- */

function isPending(call) {
  return !('result' in call) && call.duration_ms === undefined;
}

function isFailed(call) {
  return Boolean(call.is_error || call.result?.ok === false);
}

function errorText(result) {
  const error = result?.error;
  const text = typeof error === 'string' ? error : error?.message ?? error?.code ?? '';
  return text.replace(/\.$/, ''); // точка в конце мешает разделителям «·»
}

/** Источник данных: meta.served_from, а у старых записей — общий served_from всех sources. */
function sourceLabel(result) {
  const meta = result?.meta ?? {};
  let servedFrom = meta.served_from;
  if (!servedFrom && Array.isArray(meta.sources) && meta.sources.length) {
    const values = new Set(meta.sources.map((source) => source.served_from));
    if (values.size === 1) [servedFrom] = values;
  }
  const [text, kind] = SOURCE_LABELS[servedFrom] ?? [];
  if (!text) return null;
  const node = span(`tool-source ${kind}`, text);
  // live_error — объект {code, message}; в старых сохранённых диалогах это строка.
  const liveError = meta.live_error;
  const liveText = typeof liveError === 'string' ? liveError : liveError?.message || liveError?.code;
  if (liveText) node.title = `Платформа: ${liveText}`;
  return node;
}

/** Самое важное из аргументов — коротко, без JSON. */
function describeArguments(name, args) {
  if (!args || typeof args !== 'object') return '';
  const resource = args.resource_id ?? '';
  switch (name) {
    case 'search_graph':
      return [`«${args.query ?? ''}»`, resource && `в ${resource}`].filter(Boolean).join(' ');
    case 'get_subgraph':
      return [[resource || (args.path && `«${args.path}»`), args.root].filter(Boolean).join(' / '),
        args.depth != null && `глубина ${args.depth}`]
        .filter(Boolean).join(', ');
    case 'count_graph':
      return [[resource, args.root].filter(Boolean).join(' / '), args.concept && `понятие «${args.concept}»`]
        .filter(Boolean).join(', ');
    case 'get_inforesource':
      return resource || (args.path ? `«${args.path}»` : '');
    case 'validate_resource':
      return resource;
    case 'list_inforesources':
      return args.filter ? `«${args.filter}»` : 'все ресурсы';
    case 'find_materials':
      return describeComposition(args);
    case 'write_inforesource': {
      const request = args.request ?? {};
      const target = request.path ?? [request.folder_path ?? request.parent_path, request.name].filter(Boolean).join(' / ');
      return [request.operation, target && `«${target}»`].filter(Boolean).join(' ');
    }
    case 'delete_inforesource':
      return [args.path && `«${args.path}»`, args.mode === 'delete' ? 'безвозвратно' : 'в корзину'].filter(Boolean).join(' ');
    default: {
      const text = JSON.stringify(args);
      return text === '{}' ? '' : text;
    }
  }
}

const OP_SIGNS = { '>=': '≥', '<=': '≤' };

/** Условия по составу, класс и сортировка: «Cr > 20 · нет Ni · Сплавы на основе железа». */
function describeComposition(args) {
  // Показываем ровно то, что прислала модель, даже если бэкенд отклонил: мусорные записи пропускаем.
  const list = Array.isArray(args.conditions) ? args.conditions : [];
  const conditions = list.filter((item) => item && typeof item === 'object').map(describeCondition);
  const sort = args.sort_by && (args.order === 'asc' ? `по возрастанию ${args.sort_by}` : `топ по ${args.sort_by}`);
  return [...conditions, args.material_class, sort].filter(Boolean).join(' · ');
}

function describeCondition({ element, op, value, include_unlisted: unlisted }) {
  const name = element || '?';
  if (op === 'present') return `есть ${name}`;
  if (op === 'absent') return `нет ${name}`;
  if (!(op in OP_SIGNS) && !['>', '<'].includes(op)) return name; // неизвестная операция — только элемент
  const text = `${name} ${OP_SIGNS[op] ?? op} ${value ?? '?'}`;
  return unlisted ? `${text} (и без элемента)` : text;
}

/** Итог вызова одной фразой: «24 совпадения», «40 вершин». */
function describeResult(name, result) {
  const data = result?.data;
  if (!data || typeof data !== 'object') return '';
  switch (name) {
    case 'search_graph':
      if (typeof data.total !== 'number') return '';
      return data.total ? plural(data.total, ['совпадение', 'совпадения', 'совпадений']) : 'нет совпадений';
    case 'get_subgraph':
      if (!Array.isArray(data.nodes)) return '';
      return plural(data.nodes.length, NODES) + (data.has_more ? ', есть ещё' : '');
    case 'count_graph':
      if (typeof data.count === 'number') return `«${data.concept}»: ${data.count}`;
      return typeof data.nodes === 'number' ? plural(data.nodes, NODES) : '';
    case 'get_inforesource':
      return typeof data.nodes === 'number' ? plural(data.nodes, NODES) : '';
    case 'list_inforesources':
      return Array.isArray(data.resources) ? plural(data.resources.length, ['ресурс', 'ресурса', 'ресурсов']) : '';
    case 'validate_resource':
      if (typeof data.valid !== 'boolean') return '';
      return data.valid ? 'структура в порядке' : plural(data.issues?.length ?? 0, ['замечание', 'замечания', 'замечаний']);
    case 'write_inforesource':
    case 'delete_inforesource':
      return data.path ? `записано: «${data.path}»` : '';
    case 'find_materials':
      if (typeof data.total !== 'number') return '';
      return (data.total ? plural(data.total, ['марка', 'марки', 'марок']) : 'нет марок')
        + (data.total && data.has_more ? ', есть ещё' : '');
    default:
      return '';
  }
}
