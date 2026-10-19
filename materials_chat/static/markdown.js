/*
 * Небольшой безопасный рендер Markdown.
 *
 * Текст модели НИКОГДА не попадает в innerHTML: мы создаём элементы через
 * document.createElement, а текст добавляем текстовыми узлами. Поэтому любая
 * разметка вида <script> внутри ответа покажется как обычный текст.
 *
 * Поддерживается: абзацы, заголовки # – ###, маркированные и нумерованные
 * списки (с вложенностью), блоки ``` ```, цитаты >, простые таблицы | a | b |,
 * горизонтальная черта; в строке — **жирный**, *курсив*, `код` и ссылки
 * [текст](https://…) только с протоколами http и https.
 */

const FENCE = /^\s{0,3}```/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/;
const LIST_ITEM = /^([ \t]*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_DIVIDER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/;

// Строчная разметка. Порядок групп важен: код первым, чтобы звёздочки внутри `…` не стали курсивом.
const INLINE = new RegExp([
  '`([^`\\n]+)`',                                        // 1: `код`
  '\\[([^\\]\\n]+)\\]\\(([^)\\s]+)\\)',                  // 2, 3: [текст](адрес)
  '\\*\\*(?=\\S)([^\\n]*?\\S)\\*\\*',                    // 4: **жирный**
  '(?<![\\p{L}\\p{N}*])\\*(?=\\S)([^*\\n]*?\\S)\\*(?![\\p{L}\\p{N}*])', // 5: *курсив*
].join('|'), 'gu');

/** Перерисовывает target по исходному тексту Markdown. */
export function renderMarkdown(target, source) {
  target.replaceChildren(...parseBlocks(String(source ?? '')));
}

function el(tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

/* ---------- Блоки ---------- */

function parseBlocks(source) {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const nodes = [];
  let index = 0;
  while (index < lines.length) {
    if (!lines[index].trim()) {
      index += 1;
      continue;
    }
    const read = blockReaderFor(lines, index);
    const [node, next] = read(lines, index);
    nodes.push(node);
    index = next;
  }
  return nodes;
}

/** Определяет, какой блок начинается со строки index. */
function blockReaderFor(lines, index) {
  const line = lines[index];
  if (FENCE.test(line)) return readCode;
  if (HEADING.test(line)) return readHeading;
  if (RULE.test(line)) return readRule;
  if (LIST_ITEM.test(line)) return readList;
  if (QUOTE.test(line)) return readQuote;
  if (isTableStart(lines, index)) return readTable;
  return readParagraph;
}

function readParagraph(lines, start) {
  const paragraph = el('p');
  let index = start;
  while (index < lines.length && lines[index].trim()) {
    if (index > start && blockReaderFor(lines, index) !== readParagraph) break;
    if (index > start) paragraph.append(el('br'));
    renderInline(paragraph, lines[index].trim());
    index += 1;
  }
  return [paragraph, index];
}

function readHeading(lines, start) {
  const [, hashes, text] = lines[start].match(HEADING);
  // Внутри ответа заголовки младше заголовков страницы: # → h3, ## → h4, ### и глубже → h5.
  const heading = el(`h${Math.min(hashes.length + 2, 5)}`);
  renderInline(heading, text);
  return [heading, start + 1];
}

function readRule(lines, start) {
  return [el('hr'), start + 1];
}

function readCode(lines, start) {
  const body = [];
  let index = start + 1;
  // Пока ответ идёт потоком, закрывающей ``` может ещё не быть — тогда блок длится до конца.
  while (index < lines.length && !FENCE.test(lines[index])) {
    body.push(lines[index]);
    index += 1;
  }
  const pre = el('pre');
  const code = el('code');
  code.textContent = body.join('\n');
  pre.append(code);
  return [pre, index + 1];
}

function readQuote(lines, start) {
  const inner = [];
  let index = start;
  while (index < lines.length && QUOTE.test(lines[index])) {
    inner.push(lines[index].match(QUOTE)[1]);
    index += 1;
  }
  const quote = el('blockquote');
  quote.append(...parseBlocks(inner.join('\n')));
  return [quote, index];
}

/* ---------- Таблицы ---------- */

function isTableStart(lines, index) {
  return lines[index].includes('|') && index + 1 < lines.length && TABLE_DIVIDER.test(lines[index + 1]);
}

function splitRow(line) {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|')) row = row.slice(0, -1);
  return row.split('|').map((cell) => cell.trim());
}

function readTable(lines, start) {
  const header = splitRow(lines[start]);
  const aligns = splitRow(lines[start + 1]).map((cell) => {
    if (cell.startsWith(':') && cell.endsWith(':')) return 'center';
    return cell.endsWith(':') ? 'right' : '';
  });
  const table = el('table');
  const head = el('thead');
  head.append(tableRow('th', header, aligns));
  const body = el('tbody');
  let index = start + 2;
  while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
    body.append(tableRow('td', splitRow(lines[index]), aligns));
    index += 1;
  }
  table.append(head, body);
  // Обёртка даёт горизонтальную прокрутку на узком экране.
  const wrap = el('div', 'table-wrap');
  wrap.append(table);
  return [wrap, index];
}

function tableRow(cellTag, cells, aligns) {
  const row = el('tr');
  cells.forEach((text, column) => {
    const cell = el(cellTag);
    if (aligns[column]) cell.style.textAlign = aligns[column];
    renderInline(cell, text);
    row.append(cell);
  });
  return row;
}

/* ---------- Списки ---------- */

function indentWidth(text) {
  return text.match(/^[ \t]*/)[0].replace(/\t/g, '    ').length;
}

function isListItem(line) {
  return LIST_ITEM.test(line) && !RULE.test(line);
}

/** Список читается целиком, вложенность определяется отступом пункта. */
function readList(lines, start) {
  const fragment = document.createDocumentFragment();
  const levels = []; // стек уровней: { indent, ordered, list, lastItem }
  let index = start;
  while (index < lines.length) {
    const line = lines[index];
    const match = line.match(LIST_ITEM);
    if (match && !RULE.test(line)) {
      addListItem(fragment, levels, indentWidth(match[1]), match[2], match[3]);
    } else if (line.trim() && indentWidth(line) > 0 && !FENCE.test(line)) {
      // Строка с отступом продолжает текущий пункт.
      const item = levels[levels.length - 1].lastItem;
      item.append(el('br'));
      renderInline(item, line.trim());
    } else if (!line.trim() && listContinuesAfterBlank(lines, index)) {
      // Пустая строка между пунктами не обрывает список.
    } else {
      break;
    }
    index += 1;
  }
  return [fragment, index];
}

function listContinuesAfterBlank(lines, index) {
  let next = index;
  while (next < lines.length && !lines[next].trim()) next += 1;
  return next < lines.length && (isListItem(lines[next]) || indentWidth(lines[next]) >= 2);
}

function addListItem(root, levels, indent, marker, text) {
  const ordered = /\d/.test(marker);
  // Пункт левее текущего уровня — закрываем вложенные списки.
  while (levels.length && indent < levels[levels.length - 1].indent) levels.pop();

  let level = levels[levels.length - 1];
  const deeper = !level || indent > level.indent;
  if (deeper || level.ordered !== ordered) {
    if (!deeper) levels.pop(); // тот же отступ, но другой вид списка — новый список рядом
    const list = el(ordered ? 'ol' : 'ul');
    const first = parseInt(marker, 10);
    if (ordered && first !== 1) list.start = first;
    const parent = levels.length ? levels[levels.length - 1].lastItem : root;
    parent.append(list);
    level = { indent, ordered, list, lastItem: null };
    levels.push(level);
  }
  const item = el('li');
  renderInline(item, text);
  level.list.append(item);
  level.lastItem = item;
}

/* ---------- Строчная разметка ---------- */

function renderInline(parent, text) {
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    // append(строка) создаёт текстовый узел — HTML не разбирается.
    parent.append(text.slice(last, match.index));
    const [whole, code, linkText, href, bold, italic] = match;
    if (code !== undefined) {
      const node = el('code');
      node.textContent = code;
      parent.append(node);
    } else if (linkText !== undefined) {
      parent.append(safeLink(linkText, href));
    } else {
      const node = el(bold !== undefined ? 'strong' : 'em');
      renderInline(node, bold ?? italic);
      parent.append(node);
    }
    last = match.index + whole.length;
  }
  parent.append(text.slice(last));
}

/** Ссылка только на http/https; иначе показываем просто текст. */
function safeLink(text, href) {
  let url = null;
  try {
    url = new URL(href);
  } catch {
    // Относительные и битые адреса не превращаем в ссылки.
  }
  if (!url || (url.protocol !== 'http:' && url.protocol !== 'https:')) return document.createTextNode(text);
  const link = el('a');
  link.href = url.href;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = text;
  return link;
}
