'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { CopilotExecution } from './copilot-execution';
import { rawCommerceCall } from './commerce';
import {
  addMessage,
  emptyHistory,
  mergeHistory,
  historyKey,
  readHistory,
  saveReply,
  type ChatHistory,
} from './chat-history';

export function useChatHistory(surface: 'buyer' | 'merchant' = 'buyer') {
  const [history, setHistory] = useState<ChatHistory>(emptyHistory);
  const [notice, setNotice] = useState('');
  const current = useRef(history),
    key = useRef<string | null>(null),
    revision = useRef(0);
  const commit = useCallback((next: ChatHistory) => {
    if (next === current.current) return;
    current.current = next;
    setHistory(next);
    revision.current++;
    if (key.current)
      try {
        localStorage.setItem(key.current, JSON.stringify(next));
        setNotice('');
      } catch {
        setNotice(
          'Chat history cannot be saved in this browser. Your cart is still saved separately.',
        );
      }
  }, []);
  useEffect(() => {
    let cancelled = false;
    const identity =
      surface === 'merchant'
        ? fetch('/api/merchant/session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
          }).then(async (response) => {
            if (!response.ok) throw Error('Merchant session unavailable');
            return ((await response.json()) as { principal_id: string })
              .principal_id;
          })
        : rawCommerceCall<{ specialists: { principal_id: string }[] }>(
            'agent/capabilities',
          ).then((body) => body.specialists[0]?.principal_id ?? '');
    void identity
      .then((principal) => {
        if (cancelled) return;
        key.current = historyKey(principal, surface);
        const saved = readHistory(localStorage.getItem(key.current));
        // A user can type during bootstrap. Never replace that new turn with old storage.
        const next = revision.current
          ? mergeHistory(saved, current.current)
          : saved;
        commit(next);
      })
      .catch(() => {
        if (!cancelled)
          setNotice(
            'Chat history is unavailable. New messages remain in this tab until you reload.',
          );
      });
    return () => {
      cancelled = true;
    };
  }, [commit, surface]);
  const append = useCallback(
    (text: string) => {
      const id = crypto.randomUUID();
      const next = addMessage(current.current, text, id, crypto.randomUUID());
      commit(next);
      return { chatId: next.activeId!, messageId: id };
    },
    [commit],
  );
  const reply = useCallback(
    (
      target: { chatId: string; messageId: string },
      text: string,
      execution?: CopilotExecution,
    ) =>
      commit(
        saveReply(
          current.current,
          target.chatId,
          target.messageId,
          text,
          execution,
        ),
      ),
    [commit],
  );
  const open = useCallback(
    (id: string | null) => commit({ ...current.current, activeId: id }),
    [commit],
  );
  return {
    history,
    active: history.chats.find((c) => c.id === history.activeId) ?? null,
    notice,
    append,
    reply,
    open,
  };
}
