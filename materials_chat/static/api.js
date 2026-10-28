/* Обращения к серверу. Ключи и вызовы модели живут только на сервере. */

/** Ошибка HTTP с кодом ответа: интерфейсу иногда важно отличить 503 от 409. */
export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Достаём понятный текст ошибки из ответа сервера ({"detail": "..."}). */
async function errorFromResponse(response) {
  let message = `Сервер ответил ${response.status}`;
  try {
    const body = await response.json();
    const detail = body.detail ?? body.message;
    if (detail) message = typeof detail === 'string' ? detail : JSON.stringify(detail);
  } catch {
    // Тело может быть не JSON — тогда остаётся код ответа.
  }
  return new ApiError(message, response.status);
}

/** JSON-запрос к API. */
export async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) throw await errorFromResponse(response);
  return response.status === 204 ? null : response.json();
}

/**
 * Отправляет вопрос и читает поток Server-Sent Events.
 * EventSource умеет только GET, поэтому читаем тело POST-ответа вручную.
 * onEvent(name, data) вызывается для каждого события по порядку.
 */
export async function streamChat(sessionId, message, signal, onEvent) {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
    signal,
  });
  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ.');
  await readEvents(response.body, onEvent);
}

/** Проверка ответа: поток claim (утверждение, как только готово), затем done с вердиктом или error. */
export async function streamVerify(sessionId, messageId, onEvent) {
  const path = `/api/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/verify`;
  const response = await fetch(path, { method: 'POST' });
  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) throw new Error('Браузер не поддерживает потоковый ответ.');
  await readEvents(response.body, onEvent);
}

/** Разбор формата SSE: блоки «event: …\ndata: …», разделённые пустой строкой. */
async function readEvents(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const flushCompleteBlocks = () => {
    buffer = buffer.replace(/\r\n/g, '\n');
    let boundary;
    while ((boundary = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      dispatchBlock(block, onEvent);
    }
  };

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      flushCompleteBlocks();
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      buffer += '\n\n';
      flushCompleteBlocks();
    }
  } finally {
    reader.releaseLock();
  }
}

function dispatchBlock(block, onEvent) {
  let event = 'message';
  const dataLines = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length) onEvent(event, JSON.parse(dataLines.join('\n')));
}
