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
  }

  /** Поток оборвался — не оставляем «выполняющиеся» строки навсегда. */
  interruptPending() {
    for (const row of this.rows.values()) {
      if (isPending(row.call)) {
        this.update({ ...row.call, is_error: true, result: { ok: false, error: { message: 'вызов прерван' } } });
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
  if (meta.live_error) node.title = `Платформа: ${meta.live_error}`;
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
      return [[resource, args.root].filter(Boolean).join(' / '), args.depth != null && `глубина ${args.depth}`]
        .filter(Boolean).join(', ');
    case 'count_graph':
      return [resource, args.root].filter(Boolean).join(' / ');
    case 'get_inforesource':
    case 'validate_resource':
      return resource;
    case 'list_inforesources':
      return args.filter ? `«${args.filter}»` : 'все ресурсы';
    default: {
      const text = JSON.stringify(args);
      return text === '{}' ? '' : text;
    }
  }
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
    case 'get_inforesource':
      return typeof data.nodes === 'number' ? plural(data.nodes, NODES) : '';
    case 'list_inforesources':
      return Array.isArray(data.resources) ? plural(data.resources.length, ['ресурс', 'ресурса', 'ресурсов']) : '';
    case 'validate_resource':
      if (typeof data.valid !== 'boolean') return '';
      return data.valid ? 'структура в порядке' : plural(data.issues?.length ?? 0, ['замечание', 'замечания', 'замечаний']);
    default:
      return '';
  }
}
