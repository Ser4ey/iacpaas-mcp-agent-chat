/*
 * Сверка чисел ответа с данными: строка под ответом и подсветка ненайденных чисел.
 * Только DOM-узлы и textContent, как в markdown.js, — innerHTML не используется.
 */

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
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node) => markInTextNode(node, wanted));
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

function span(className, text) {
  const node = document.createElement('span');
  node.className = className;
  node.textContent = text;
  return node;
}
