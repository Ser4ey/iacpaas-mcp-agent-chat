/*
 * Сверка ответа с данными: строка сверки чисел, подсветка ненайденных чисел, ход проверки и блок вердикта
 * с цитатами, ведущими к месту утверждения в ответе.
 * Только DOM-узлы и textContent, как в markdown.js, — innerHTML не используется.
 */
import { formatCost, plural } from './format.js';

const NO_MATCH_HINT = 'Сверено с данными: {confirmed} из {checked}. '
  + 'Сюда попадают и числа, вычисленные моделью (пересчёт единиц, суммы), — это не обязательно ошибка.';

/** Строка сверки для карточки ответа; null, если показывать нечего (старые сообщения, нет чисел). */
export function numberCheckNode(check) {
  if (!check) return null;
  if (check.status === 'no_data') return span('number-check', 'нет данных для сверки');
  if (check.status !== 'checked') return null;
  const unconfirmed = check.unconfirmed ?? [];
  if (!unconfirmed.length) {
    return span('number-check', `Числа сверены с данными: ${check.confirmed} из ${check.checked}`);
  }
  const node = span('number-check is-warning', `Не найдены в данных: ${unconfirmed.join(', ')} — проверьте`);
  node.title = NO_MATCH_HINT.replace('{confirmed}', check.confirmed).replace('{checked}', check.checked);
  return node;
}

/** Оборачивает в <mark> каждое вхождение ненайденных чисел в тексте контейнера. */
export function highlightNumbers(container, numbers) {
  // Длинные строки первыми: «10 300» не должно разбиться на «10» и «300».
  const wanted = [...new Set(numbers)].filter(Boolean).sort((a, b) => b.length - a.length);
  if (!wanted.length) return;
  // Узлы собираем заранее: во время обхода мы сами меняем дерево.
  textNodes(container).forEach((node) => markInTextNode(node, wanted));
}

function textNodes(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  return nodes;
}

function markInTextNode(node, wanted) {
  const text = node.nodeValue;
  const ranges = findRanges(text, wanted);
  if (!ranges.length) return;
  const fragment = document.createDocumentFragment();
  let pos = 0;
  for (const [start, end] of ranges) {
    if (start > pos) fragment.append(text.slice(pos, start));
    const mark = document.createElement('mark');
    mark.className = 'number-unconfirmed';
    mark.textContent = text.slice(start, end);
    fragment.append(mark);
    pos = end;
  }
  if (pos < text.length) fragment.append(text.slice(pos));
  node.replaceWith(fragment);
}

/** Непересекающиеся границы [начало, конец) целых вхождений; слева направо, длинные строки в приоритете. */
export function findRanges(text, wanted) {
  const ranges = [];
  let pos = 0;
  while (pos < text.length) {
    let hit = null;
    for (const number of wanted) {
      const start = nextWhole(text, number, pos);
      if (start >= 0 && (!hit || start < hit[0])) hit = [start, start + number.length];
    }
    if (!hit) break;
    ranges.push(hit);
    pos = hit[1];
  }
  return ranges;
}

/** Первое целое вхождение number не раньше pos; частичные («19500» для «9500») пропускаются. */
function nextWhole(text, number, pos) {
  for (let start = text.indexOf(number, pos); start >= 0; start = text.indexOf(number, start + 1)) {
    if (isWholeNumber(text, start, start + number.length)) return start;
  }
  return -1;
}

const WORD_CHAR = /[\p{L}\p{N}]/u;
const DIGIT = /\p{N}/u;

/** Число стоит целиком: рядом нет буквы или цифры, а справа нет «,5» или «.5». */
function isWholeNumber(text, start, end) {
  if (start > 0 && WORD_CHAR.test(text[start - 1])) return false;
  if (end >= text.length) return true;
  if (WORD_CHAR.test(text[end])) return false;
  return !(/[.,]/.test(text[end]) && end + 1 < text.length && DIGIT.test(text[end + 1]));
}

const CLAIM_STATUS = {
  confirmed: 'подтверждено',
  not_in_data: 'нет в данных',
  contradicted: 'противоречит данным',
};

const VERDICT_TONE = { 'подтверждён': 'ok', 'частично подтверждён': 'partial', 'не подтверждён': 'bad' };

/** Блок вердикта проверки: итог с цветной плашкой, доли статусов, расход, список утверждений с цитатами. */
export function verdictNode(verification, { answer = null, open = false } = {}) {
  const root = document.createElement('div');
  root.className = 'answer-verdict';
  const claims = verification.claims ?? [];
  const total = verification.total ?? claims.length;
  const head = document.createElement('div');
  head.className = 'verdict-head';
  if (verification.verdict) {
    head.append(span(`verdict-badge is-${VERDICT_TONE[verification.verdict] ?? 'neutral'}`, verification.verdict));
  }
  if (total > 0) {
    const percent = typeof verification.percent === 'number' ? ` (${verification.percent} %)` : '';
    const count = plural(total, ['утверждения', 'утверждений', 'утверждений']);
    head.append(span('verdict-summary', `Подтверждено ${verification.confirmed} из ${count}${percent}`));
  }
  root.append(head);
  if (claims.length) root.append(statusBar(claims), statusCounts(claims));

  const usage = usageLine(verification.usage);
  if (usage) root.append(span('verdict-usage', usage));
  if (claims.length) root.append(claimsList(claims, { answer, open }));
  return root;
}

/** Ход проверки: что делает проверяющий, сколько прошло времени и утверждения по мере готовности. */
export function progressNode(progress, { answer = null, elapsed = 0 } = {}) {
  const root = document.createElement('div');
  root.className = 'answer-verdict is-running';
  root.setAttribute('role', 'status');
  const claims = progress.claims.filter(Boolean);
  const head = document.createElement('div');
  head.className = 'verdict-head';
  const stage = claims.length
    ? `Проверено утверждений: ${claims.length}…`
    : 'Проверяющий читает ответ и данные из базы…';
  head.append(span('verdict-spinner', ''), span('verdict-summary', stage), span('verdict-timer', formatElapsed(elapsed)));
  const bar = document.createElement('div');
  bar.className = 'verdict-bar is-running';
  root.append(head, bar);
  if (claims.length) root.append(statusCounts(claims), claimsList(claims, { answer, open: true }));
  return root;
}

/** «7 с», «1 мин 05 с» — время с начала проверки. */
export function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds} с`;
  return `${Math.floor(seconds / 60)} мин ${String(seconds % 60).padStart(2, '0')} с`;
}

function countStatuses(claims) {
  const counts = Object.fromEntries(Object.keys(CLAIM_STATUS).map((status) => [status, 0]));
  for (const claim of claims) if (claim.status in counts) counts[claim.status] += 1;
  return counts;
}

/** Полоса долей: зелёная — подтверждено, жёлтая — нет в данных, красная — противоречит. */
function statusBar(claims) {
  const bar = document.createElement('div');
  bar.className = 'verdict-bar';
  for (const [status, count] of Object.entries(countStatuses(claims))) {
    if (!count) continue;
    const part = span(`is-${status}`, '');
    part.style.flexGrow = String(count);
    bar.append(part);
  }
  return bar;
}

function statusCounts(claims) {
  const parts = Object.entries(countStatuses(claims))
    .filter(([, count]) => count)
    .map(([status, count]) => `${count} — ${CLAIM_STATUS[status]}`);
  return span('verdict-counts', parts.join(' · '));
}

/** Расход самой проверки («Проверка: 1 234 токена · 0,12 ₽»); пусто, если расход неизвестен. */
function usageLine(usage) {
  if (!usage) return '';
  const parts = [];
  if (usage.total_tokens) parts.push(plural(usage.total_tokens, ['токен', 'токена', 'токенов']));
  const cost = formatCost(usage.cost, usage.currency);
  if (cost) parts.push(`${usage.cost_known === false && !cost.startsWith('<') ? '≥ ' : ''}${cost}`);
  return parts.length ? `Проверка: ${parts.join(' · ')}` : '';
}

function claimsList(claims, { answer, open }) {
  const details = document.createElement('details');
  details.className = 'verdict-claims';
  details.open = Boolean(open);
  const title = document.createElement('summary');
  title.textContent = `Утверждения (${claims.length})`;
  const items = claims.map((claim) => claimItem(claim, answer));
  details.append(title);
  if (items.some((item) => item.querySelector('button.claim-quote'))) {
    details.append(span('verdict-hint', 'Нажмите на цитату, чтобы перейти к этому месту в ответе.'));
  }
  const list = document.createElement('ul');
  list.append(...items);
  details.append(list);
  return details;
}

function claimItem(claim, answer) {
  const item = document.createElement('li');
  item.className = `claim is-${claim.status}`;
  item.append(span('claim-status', CLAIM_STATUS[claim.status] ?? claim.status));
  const quote = (claim.quote ?? '').trim();
  if (quote && answer && findQuote(answer, quote)) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'claim-quote';
    button.textContent = `«${quote}»`;
    button.title = 'Показать это место в ответе';
    button.addEventListener('click', () => revealQuote(answer, quote, claim.status));
    item.append(button);
  } else if (quote) {
    const node = span('claim-quote is-missing', `«${quote}»`);
    node.title = 'Этот фрагмент не найден в тексте ответа дословно';
    item.append(node);
  }
  if (claim.text && claim.text !== quote) item.append(span('claim-text', claim.text));
  const support = [claim.node_id ? `вершина ${claim.node_id}` : '', claim.evidence].filter(Boolean).join(': ');
  if (support) item.append(span('claim-support', support));
  return item;
}

// Цитаты проверяющего: поиск места утверждения в тексте ответа и переход к нему.

const QUOTE_FOLD = {
  '«': '"', '»': '"', '„': '"', '“': '"', '”': '"', '‘': "'", '’': "'",
  '–': '-', '—': '-', '−': '-', '‑': '-', 'ё': 'е', 'Ё': 'е',
};
const QUOTE_DROPPED = new Set(['*', '`', '|', '#']); // разметка Markdown, которой нет в отрисованном тексте
const QUOTE_EDGES = /^[\s"'«»“”„….]+|[\s"'«»“”„….]+$/g;
const QUOTE_BLOCKS = 'p, li, td, th, h1, h2, h3, h4, h5, h6, pre, blockquote';
const QUOTE_HIGHLIGHT_MS = 4000;

/** Символ для сравнения: без регистра, любой пробел — пробел, тире и кавычки — одного вида; '' — пропустить. */
function foldChar(char) {
  if (/\s/.test(char)) return ' ';
  if (QUOTE_DROPPED.has(char)) return '';
  const folded = QUOTE_FOLD[char] ?? char.toLowerCase();
  return folded.length === 1 ? folded : char;
}

function foldQuote(quote) {
  let text = '';
  for (const char of String(quote ?? '').replace(QUOTE_EDGES, '')) {
    const folded = foldChar(char);
    if (folded && !(folded === ' ' && text.endsWith(' '))) text += folded;
  }
  return text.trim();
}

/**
 * Где в тексте ответа стоит цитата. parts — текстовые узлы по порядку: {text, block}; между узлами
 * разных блоков (абзацы, ячейки таблицы) считается пробел. Возвращает {start, end} как [номер узла, смещение]
 * (end — после последнего символа) или null, если цитаты нет или она короче трёх символов.
 */
export function locateQuote(parts, quote) {
  const wanted = foldQuote(quote);
  if (wanted.length < 3) return null;
  let text = '';
  const points = []; // для каждого символа text — откуда он: [номер узла, смещение]; null — пробел между блоками
  parts.forEach((part, index) => {
    if (index > 0 && part.block !== parts[index - 1].block && text && !text.endsWith(' ')) {
      text += ' ';
      points.push(null);
    }
    for (let offset = 0; offset < part.text.length; offset += 1) {
      const folded = foldChar(part.text[offset]);
      if (!folded || (folded === ' ' && (!text || text.endsWith(' ')))) continue;
      text += folded;
      points.push([index, offset]);
    }
  });
  const at = text.indexOf(wanted);
  if (at < 0) return null;
  const [endPart, endOffset] = points[at + wanted.length - 1];
  return { start: points[at], end: [endPart, endOffset + 1] };
}

/** Range цитаты в отрисованном ответе или null. */
export function findQuote(container, quote) {
  const nodes = textNodes(container);
  const parts = nodes.map((node) => ({ text: node.nodeValue, block: node.parentElement?.closest(QUOTE_BLOCKS) ?? container }));
  const found = locateQuote(parts, quote);
  if (!found) return null;
  const range = document.createRange();
  range.setStart(nodes[found.start[0]], found.start[1]);
  range.setEnd(nodes[found.end[0]], found.end[1]);
  return range;
}

let quoteTimer = 0;
let flashed = null;

/** Прокрутить к цитате и на несколько секунд подсветить её цветом статуса; false — цитаты в тексте нет. */
export function revealQuote(container, quote, status) {
  const range = findQuote(container, quote);
  if (!range) return false;
  const kind = status in CLAIM_STATUS ? status : 'not_in_data';
  const target = range.startContainer.parentElement?.closest(QUOTE_BLOCKS) ?? container;
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  target.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'center' });
  clearTimeout(quoteTimer);
  flashed?.classList.remove('quote-flash');
  if (globalThis.CSS?.highlights && typeof Highlight === 'function') {
    // Custom Highlight API красит ровно цитату, не трогая DOM ответа (подсветку чисел и разметку).
    for (const name of Object.keys(CLAIM_STATUS)) CSS.highlights.delete(`quote-${name}`);
    CSS.highlights.set(`quote-${kind}`, new Highlight(range));
    quoteTimer = setTimeout(() => CSS.highlights.delete(`quote-${kind}`), QUOTE_HIGHLIGHT_MS);
  } else {
    flashed = target;
    target.classList.add('quote-flash');
    quoteTimer = setTimeout(() => target.classList.remove('quote-flash'), QUOTE_HIGHLIGHT_MS);
  }
  return true;
}

function span(className, text) {
  const node = document.createElement('span');
  node.className = className;
  node.textContent = text;
  return node;
}
