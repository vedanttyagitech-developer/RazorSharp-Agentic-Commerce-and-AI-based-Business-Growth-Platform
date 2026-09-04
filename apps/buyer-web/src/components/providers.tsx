"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { API_BASE_LABEL, API_MODE, getClient, type CommerceClient } from "@/lib/api";
import { NetworkError } from "@/lib/api/problem";

// ------------------------------------------------------------------ client context

const ClientContext = createContext<CommerceClient | null>(null);

export function useClient(): CommerceClient {
  const client = useContext(ClientContext);
  if (!client) throw new Error("useClient must be used inside <AppProviders>");
  return client;
}

// -------------------------------------------------------------- degradation notices

export interface DegradationNotice {
  id: string;
  component: string;
  message: string;
}

interface DegradationApi {
  notices: DegradationNotice[];
  report: (id: string, component: string, message: string) => void;
  clear: (id: string) => void;
}

const DegradationContext = createContext<DegradationApi | null>(null);

export function useDegradation(): DegradationApi {
  const api = useContext(DegradationContext);
  if (!api) throw new Error("useDegradation must be used inside <AppProviders>");
  return api;
}

// -------------------------------------------------------------------- basket ref

const BASKET_KEY = "buyer-web:basket-id";

interface BasketRefApi {
  basketId: string | null;
  setBasketId: (id: string | null) => void;
  lineCount: number;
  setLineCount: (count: number) => void;
}

const BasketRefContext = createContext<BasketRefApi | null>(null);

export function useBasketRef(): BasketRefApi {
  const api = useContext(BasketRefContext);
  if (!api) throw new Error("useBasketRef must be used inside <AppProviders>");
  return api;
}

function readStoredBasketId(): string | null {
  try {
    return window.sessionStorage.getItem(BASKET_KEY);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------- provider

export function AppProviders({ children }: { children: ReactNode }) {
  const client = useMemo(() => getClient(), []);
  const [notices, setNotices] = useState<DegradationNotice[]>([]);
  const [basketId, setBasketIdState] = useState<string | null>(null);
  const [lineCount, setLineCount] = useState(0);

  const report = useCallback((id: string, component: string, message: string) => {
    setNotices((current) => {
      const rest = current.filter((notice) => notice.id !== id);
      return [...rest, { id, component, message }];
    });
  }, []);
  const clear = useCallback((id: string) => {
    setNotices((current) => current.filter((notice) => notice.id !== id));
  }, []);

  const setBasketId = useCallback((id: string | null) => {
    setBasketIdState(id);
    try {
      if (id) window.sessionStorage.setItem(BASKET_KEY, id);
      else window.sessionStorage.removeItem(BASKET_KEY);
    } catch {
      // Session storage is a convenience; the id is also held in memory.
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve(readStoredBasketId()).then((stored) => {
      if (!cancelled && stored) setBasketIdState(stored);
    });
    client
      .getConfig()
      .then((config) => {
        if (cancelled) return;
        for (const item of config.degraded) report(`config:${item.component}`, item.component, item.notice);
        if (config.safe_mode) report("safe-mode", "Safe Mode", "Delegated and Reserve Pay debits are paused. A fresh human-present Razorpay Standard Checkout remains available.");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (API_MODE === "live") {
          const reason = error instanceof NetworkError ? `Commerce API is unreachable at ${API_BASE_LABEL}.` : "Commerce API did not answer /v1/config.";
          report("api", "Commerce API", `${reason} Nothing transactional can proceed until it is back; no state has changed.`);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, report]);

  const degradation = useMemo<DegradationApi>(() => ({ notices, report, clear }), [notices, report, clear]);
  const basketRef = useMemo<BasketRefApi>(() => ({ basketId, setBasketId, lineCount, setLineCount }), [basketId, setBasketId, lineCount]);

  return (
    <ClientContext.Provider value={client}>
      <DegradationContext.Provider value={degradation}>
        <BasketRefContext.Provider value={basketRef}>{children}</BasketRefContext.Provider>
      </DegradationContext.Provider>
    </ClientContext.Provider>
  );
}
