/*
 * Раздел «Данные»: подготовленное описание ресурсов из GET /api/data/overview
 * и состояние локального индекса из GET /api/status.
 * Всё строится через DOM-узлы; тексты с сервера вставляются только как текст.
 */
import { formatDateTime, formatNumber, plural } from './format.js';

const GROUPS = [
  { kind: 'data', title: 'Базы и справочники' },
  { kind: 'ontology', title: 'Онтологии' },
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

/**
 * Полная перерисовка раздела.
 * overview — ответ сервера или null; error — текст ошибки загрузки;
 * resources — список из /api/status (запасной вариант, если описания нет);
 * onAsk(text) — вставить вопрос в поле ввода и перейти в диалог.
 */
export function renderDataView(container, { overview, error, resources, onAsk }) {
  const parts = [el('h1', 'data-title', overview?.title || 'Данные')];
  if (overview?.intro) parts.push(el('p', 'lead', overview.intro));
  if (overview?.stats?.length) parts.push(statList(overview.stats));

  // Состояние индекса обновляется отдельно, раз в несколько секунд (renderIndexStatus).
  const index = el('div', 'index-status');
  index.id = 'index-status';
  parts.push(index);

  if (error) {
    parts.push(el('p', 'data-note', `Описание данных пока недоступно: ${error}`));
    if (resources?.length) parts.push(resourceFallback(resources));
  } else if (!overview) {
    parts.push(el('p', 'data-note', 'Загружаем описание данных…'));
  } else if (overview.ready === false) {
    parts.push(el('p', 'data-note', 'Индекс пуст. Описание появится после загрузки данных из IACPaaS.'));
  }

  if (overview?.suggested_questions?.length) parts.push(...questionBlock(overview.suggested_questions, onAsk));

  const sections = overview?.sections ?? [];
  const titles = new Map(sections.map((section) => [section.id, section.title]));
  for (const group of GROUPS) {
    const members = sections.filter((section) => (section.kind === 'ontology') === (group.kind === 'ontology'));
    if (!members.length) continue;
    parts.push(el('h2', 'data-group', group.title));
    parts.push(...members.map((section) => resourceSection(section, titles, onAsk)));
  }

  container.replaceChildren(...parts);
}

/* ---------- Состояние индекса ---------- */

/** Обновляет блок индекса, не пересоздавая кнопку (чтобы не терять фокус). */
export function renderIndexStatus(container, status, { requested, onRefresh }) {
  if (!container || !status) return;
  if (!container.firstChild) {
    const text = el('div', 'index-text');
    const button = el('button', 'button', 'Обновить из IACPaaS');
    button.type = 'button';
    button.addEventListener('click', onRefresh);
    container.append(text, button);
  }
  const [text, button] = container.children;
  const index = status.index ?? {};
  const busy = Boolean(requested || index.busy || index.refresh_running);

  const summary = el('p', 'index-summary');
  const state = busy ? 'Индекс обновляется' : index.ready ? 'Индекс готов' : 'Индекс не загружен';
  summary.append(el('strong', '', state));
  const facts = [
    index.total_resources != null && `${index.resources_indexed ?? 0} из ${plural(index.total_resources, ['ресурса', 'ресурсов', 'ресурсов'])}`,
    typeof index.nodes === 'number' && plural(index.nodes, ['вершина', 'вершины', 'вершин']),
    index.last_synced && `обновлён ${formatDateTime(index.last_synced)}`,
  ].filter(Boolean);
  for (const fact of facts) summary.append(el('span', '', fact));

  const lines = [summary];
  if (busy && index.current_resource) lines.push(el('p', 'index-line', `Сейчас загружается: ${index.current_resource}`));
  for (const [id, message] of Object.entries(index.errors ?? {})) {
    const title = status.resources?.find((resource) => resource.id === id)?.title ?? id;
    lines.push(el('p', 'index-line is-error', `${title}: ${message}`));
  }
  if (index.refresh_error) lines.push(el('p', 'index-line is-error', index.refresh_error));
  text.replaceChildren(...lines);

  button.disabled = busy;
  button.textContent = busy ? 'Обновляется…' : 'Обновить из IACPaaS';
}

/* ---------- Части раздела ---------- */

function statList(stats) {
  const list = el('dl', 'stats');
  for (const stat of stats) {
    const item = el('div');
    item.append(el('dt', '', stat.label), el('dd', '', stat.value));
    list.append(item);
  }
  return list;
}

function questionBlock(questions, onAsk) {
  const list = el('ul', 'suggestions');
  for (const question of questions) {
    const item = el('li');
    item.append(askButton('suggestion', question, question, onAsk));
    list.append(item);
  }
  return [el('h2', 'data-group', 'С чего начать'), list];
}

function resourceSection(section, titles, onAsk) {
  const node = el('section', 'resource');
  node.id = `resource-${section.id}`;
  const heading = el('h3', '', section.title);
  heading.id = `resource-title-${section.id}`;
  heading.tabIndex = -1; // сюда переносим фокус при переходе по ссылке «Структура описана в»
  node.setAttribute('aria-labelledby', heading.id);
  node.append(heading);

  if (section.description) node.append(el('p', 'resource-description', section.description));
  if (section.stats?.length) node.append(factList(section.stats));
  if (section.items?.length) node.append(itemTree(section.items));

  if (section.examples?.length) {
    const line = el('p', 'resource-line', 'Например: ');
    section.examples.forEach((example, position) => {
      if (position) line.append(', ');
      line.append(askButton('link-button accent', example, `Что известно о «${example}»?`, onAsk));
    });
    node.append(line);
  }

  const related = (section.related ?? []).filter((id) => titles.has(id));
  if (related.length) {
    const line = el('p', 'resource-line', section.kind === 'ontology' ? 'Связано: ' : 'Структура описана в: ');
    related.forEach((id, position) => {
      if (position) line.append(', ');
      line.append(jumpButton(id, titles.get(id)));
    });
    node.append(line);
  }

  if (section.source) node.append(sourceLine(section.source));
  return node;
}

function factList(stats) {
  const list = el('dl', 'facts');
  for (const stat of stats) {
    const item = el('div');
    item.append(el('dt', '', stat.label), el('dd', '', stat.value));
    list.append(item);
  }
  return list;
}

/** Дерево не глубже двух уровней; число и подпись к нему необязательны. */
function itemTree(items) {
  const list = el('ul', 'tree');
  for (const item of items) {
    const entry = el('li');
    const row = el('div', 'tree-row');
    row.append(el('span', 'tree-title', item.title));
    if (typeof item.count === 'number') {
      const count = `${formatNumber(item.count)}${item.count_label ? `\xa0${item.count_label}` : ''}`;
      row.append(el('span', 'tree-leader'), el('span', 'tree-count', count));
    }
    entry.append(row);
    if (item.children?.length) entry.append(itemTree(item.children));
    list.append(entry);
  }
  return list;
}

function sourceLine(source) {
  const parts = [
    source.path,
    source.platform_modified && `изменён на платформе ${source.platform_modified}`,
    source.indexed_at && `в индексе с ${formatDateTime(source.indexed_at)}`,
  ].filter(Boolean);
  return el('p', 'resource-source', parts.join(' · '));
}

/** Если описания нет, показываем хотя бы список ресурсов из /api/status. */
function resourceFallback(resources) {
  const list = el('ul', 'tree');
  for (const resource of resources) {
    const entry = el('li');
    const row = el('div', 'tree-row');
    row.append(el('span', 'tree-title', resource.title ?? resource.id));
    entry.append(row);
    list.append(entry);
  }
  return list;
}

function askButton(className, text, question, onAsk) {
  const button = el('button', className, text);
  button.type = 'button';
  button.addEventListener('click', () => onAsk(question));
  return button;
}

function jumpButton(id, title) {
  const button = el('button', 'link-button accent', title);
  button.type = 'button';
  button.addEventListener('click', () => {
    const target = document.getElementById(`resource-${id}`);
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    target?.querySelector('h3')?.focus({ preventScroll: true });
  });
  return button;
}
