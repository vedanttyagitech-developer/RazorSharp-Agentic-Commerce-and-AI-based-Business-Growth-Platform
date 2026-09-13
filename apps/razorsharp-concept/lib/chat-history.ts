import { savedExecution, type CopilotExecution } from './copilot-execution';
export type SavedMessage = {
  id: string;
  user: string;
  reply: string | null;
  execution?: CopilotExecution;
};
export type SavedChat = { id: string; title: string; messages: SavedMessage[] };
export type ChatHistory = { activeId: string | null; chats: SavedChat[] };
export const emptyHistory = (): ChatHistory => ({ activeId: null, chats: [] });
export function historyKey(
  principal: string,
  surface: 'buyer' | 'merchant' = 'buyer',
): string {
  if (!principal.startsWith('session:'))
    throw Error('Cannot identify this shopping session');
  return (
    (surface === 'merchant'
      ? 'rs-merchant-chat-history-v1:'
      : 'rs-chat-history-v1:') + principal.split('/')[0]
  );
}
export function readHistory(raw: string | null): ChatHistory {
  if (!raw) return emptyHistory();
  try {
    const value: unknown = JSON.parse(raw);
    if (
      !value ||
      typeof value !== 'object' ||
      !('chats' in value) ||
      !Array.isArray(value.chats)
    )
      return emptyHistory();
    const chats: SavedChat[] = value.chats
      .filter(
        (c): c is SavedChat =>
          !!c &&
          typeof c.id === 'string' &&
          typeof c.title === 'string' &&
          Array.isArray(c.messages) &&
          c.messages.every(
            (m: SavedMessage) =>
              !!m &&
              typeof m.id === 'string' &&
              typeof m.user === 'string' &&
              (m.reply === null || typeof m.reply === 'string'),
          ),
      )
      .slice(-20)
      .map((c) => ({
        ...c,
        messages: c.messages
          .slice(-60)
          .map((m) => ({ ...m, execution: savedExecution(m.execution) })),
      }));
    const activeId =
      'activeId' in value &&
      typeof value.activeId === 'string' &&
      chats.some((c) => c.id === value.activeId)
        ? value.activeId
        : null;
    return {
      activeId,
      chats: [...new Map(chats.map((c) => [c.id, c])).values()],
    };
  } catch {
    return emptyHistory();
  }
}
export function addMessage(
  history: ChatHistory,
  text: string,
  id: string,
  chatId: string,
): ChatHistory {
  const active = history.chats.find((c) => c.id === history.activeId);
  const next = active
    ? {
        ...active,
        messages: [...active.messages, { id, user: text, reply: null }].slice(
          -60,
        ),
      }
    : {
        id: chatId,
        title: text.slice(0, 65),
        messages: [{ id, user: text, reply: null }],
      };
  return {
    activeId: next.id,
    chats: [...history.chats.filter((c) => c.id !== next.id), next].slice(-20),
  };
}
export function saveReply(
  history: ChatHistory,
  chatId: string,
  messageId: string,
  reply: string,
  execution?: CopilotExecution,
): ChatHistory {
  const message = history.chats
    .find((c) => c.id === chatId)
    ?.messages.find((m) => m.id === messageId);
  if (
    !message ||
    (message.reply === reply &&
      (!execution ||
        JSON.stringify(message.execution) === JSON.stringify(execution)))
  )
    return history;
  return {
    ...history,
    chats: history.chats.map((c) =>
      c.id === chatId
        ? {
            ...c,
            messages: c.messages.map((m) =>
              m.id === messageId
                ? { ...m, reply, ...(execution ? { execution } : {}) }
                : m,
            ),
          }
        : c,
    ),
  };
}

export function mergeHistory(
  saved: ChatHistory,
  current: ChatHistory,
): ChatHistory {
  return {
    ...current,
    chats: [
      ...new Map(
        [...saved.chats, ...current.chats].map((c) => [c.id, c]),
      ).values(),
    ].slice(-20),
  };
}
