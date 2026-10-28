/*
 * Панель «Что видит модель»: системный промпт и история диалога в том виде, в каком они уйдут в модель
 * со следующим вопросом (GET /api/sessions/{id}/context). Последние ходы — полностью, у более старых
 * результаты инструментов обрезаны: остаётся начало и пометка о сокращении.
 * Всё строится через DOM-узлы; тексты с сервера вставляются только как текст.
 */
import { formatNumber, plural } from './format.js';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

const tokens = (count) => `≈ ${plural(count, ['токен', 'токена', 'токенов'])}`;
const chars = (count) => plural(count, ['символ', 'символа', 'символов']);

function keptTurns(count) {
  if (count === 0) return 'Ни один ход не уходит';
  if (count === 1) return 'Последний ход уходит';
  return `Последние ${plural(count, ['ход', 'хода', 'ходов'])} уходят`;
}

/** Полная перерисовка панели по ответу сервера. */
export function renderContextView(container, view) {
  const parts = [
    el('p', 'context-total', `${tokens(view.context_tokens)} из ${formatNumber(view.context_budget)} `
      + 'уйдёт в модель со следующим вопросом'),
    el('p', 'context-rule', `${keptTurns(view.full_turns)} полностью, у более старых результаты инструментов `
      + `обрезаны до ${chars(view.tool_chars)}. `
      + 'Пороги задают HISTORY_FULL_TURNS и HISTORY_TOOL_CHARS в .env.'),
    collapsible('Системный промпт', tokens(view.system.tokens), view.system.content, 'context-msg is-system'),
  ];
  if (!view.turns.length) {
    parts.push(el('p', 'context-empty', 'История пуста: с первым вопросом модель получит только системный промпт, '
      + 'описания инструментов и сам вопрос.'));
  }
  for (const turn of view.turns) parts.push(turnNode(turn));
  parts.push(el('p', 'context-note', 'Кроме этого модель получает описания инструментов MCP-сервера (их JSON Schema) '
    + 'и новый вопрос. Размер — оценка (длина JSON / 3); фактический расход сообщает провайдер.'));
  container.replaceChildren(...parts);
}

function turnNode(turn) {
  const root = el('section', `context-turn${turn.kept_in_full ? '' : ' is-compacted'}`);
  const head = el('div', 'context-turn-head');
  const size = !turn.kept_in_full && turn.tokens < turn.full_tokens
    ? `${tokens(turn.tokens)} вместо ${formatNumber(turn.full_tokens)}`
    : tokens(turn.tokens);
  head.append(
    el('span', 'context-turn-title', `Ход ${turn.number}`),
    el('span', `context-badge ${turn.kept_in_full ? 'is-full' : 'is-compacted'}`, turn.kept_in_full ? 'полностью' : 'сокращён'),
    el('span', 'context-size', size),
  );
  root.append(head);
  // Результат инструмента ссылается на вызов по tool_call_id; имя берём из вызова.
  const names = new Map();
  for (const message of turn.messages) {
    for (const call of message.tool_calls ?? []) names.set(call.id, call.function?.name ?? '');
  }
  for (const message of turn.messages) root.append(...messageNodes(message, names));
  return root;
}

function messageNodes(message, names) {
  const content = typeof message.content === 'string' ? message.content : '';
  if (message.role === 'user') {
    const node = el('div', 'context-msg is-user');
    node.append(el('span', 'context-msg-label', 'Вопрос'), el('p', '', content));
    return [node];
  }
  if (message.role === 'tool') {
    const name = names.get(message.tool_call_id) || 'инструмента';
    const node = collapsible(`Результат ${name}`, chars(content.length), content, 'context-msg is-tool');
    if (message.compacted) {
      node.querySelector('summary').append(el('span', 'context-cut', `обрезан, было ${chars(message.full_chars)}`));
    }
    return [node];
  }
  const nodes = [];
  for (const call of message.tool_calls ?? []) {
    const node = el('div', 'context-msg is-call');
    node.append(el('span', 'context-msg-label', 'Вызов инструмента'),
      el('code', 'context-call', `${call.function?.name ?? ''}(${call.function?.arguments ?? ''})`));
    nodes.push(node);
  }
  if (content) {
    const title = message.tool_calls?.length ? 'Текст модели перед вызовом' : 'Ответ модели';
    nodes.push(collapsible(title, chars(content.length), content, 'context-msg is-answer'));
  }
  return nodes;
}

/** Свёрнутый блок: в заголовке название и размер, внутри — текст как есть. */
function collapsible(title, size, text, className) {
  const node = el('div', className);
  const details = el('details');
  const summary = el('summary', '', `${title} · ${size}`);
  details.append(summary, el('pre', '', text));
  node.append(details);
  return node;
}
