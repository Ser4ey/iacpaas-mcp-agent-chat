/* Форматирование для русского интерфейса: числа, склонения, деньги, время. */

const numberFormat = new Intl.NumberFormat('ru-RU');
const compactFormat = new Intl.NumberFormat('ru-RU', { notation: 'compact', maximumFractionDigits: 1 });
const moneyFormat = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pluralRules = new Intl.PluralRules('ru-RU');

const CURRENCY_SIGNS = { RUB: '₽', USD: '$', EUR: '€' };

/** 94569 → «94 569» */
export function formatNumber(value) {
  return numberFormat.format(value);
}

/** 1044480 → «1 млн», 18498 → «18,5 тыс.» */
export function formatCompact(value) {
  return compactFormat.format(value);
}

/** plural(24, ['совпадение', 'совпадения', 'совпадений']) → «24 совпадения» */
export function plural(count, [one, few, many]) {
  const forms = { one, few, many };
  // Неразрывный пробел: число не отрывается от слова при переносе строки.
  return `${formatNumber(count)}\xa0${forms[pluralRules.select(count)] ?? many}`;
}

/**
 * Стоимость: «0,12 ₽»; меньше копейки — «< 0,01 ₽».
 * Возвращает пустую строку, если стоимость неизвестна — тогда её просто не показываем.
 */
export function formatCost(cost, currency) {
  if (typeof cost !== 'number' || !Number.isFinite(cost)) return '';
  const sign = CURRENCY_SIGNS[currency] ?? currency ?? '';
  const amount = cost > 0 && cost < 0.01 ? `< ${moneyFormat.format(0.01)}` : moneyFormat.format(cost);
  return `${amount}\xa0${sign}`.trim();
}

/** 34 → «34 мс», 1840 → «1,8 с» */
export function formatDuration(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms)) return '';
  if (ms < 1000) return `${Math.round(ms)}\xa0мс`;
  return `${(ms / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}\xa0с`;
}

/** Дата и время: «18 окт., 12:59». Непонятное значение возвращаем как есть. */
export function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value ?? '');
  return date.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Короткая дата для списка диалогов: сегодня — время, иначе — день и месяц. */
export function formatShortDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}
