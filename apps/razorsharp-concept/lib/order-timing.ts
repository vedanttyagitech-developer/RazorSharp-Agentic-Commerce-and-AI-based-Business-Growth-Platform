// Reading a confirmed order's timing, which the backend has always measured and no screen
// has ever shown.
//
// `GET /v1/orders/{id}` returns `duration_seconds` and a four-span `timing` object. Until
// this module, `commerce.orders.read` was typed `Record<string, unknown>` and nothing in the
// app called it -- so a platform whose distinguishing claim is that it can time the half of
// a transaction a payment provider cannot see was unable to show a single second of it.
//
// The order is fetched rather than derived. Every span is a subtraction the *database*
// performed between two rows it stamped, and recomputing any of it in the browser would
// substitute this device's clock for the one that made the measurement.

import { useEffect, useState } from 'react';
import { CommerceError, commerce, type Order } from './commerce';

export type OrderState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; order: Order }
  /** The order exists; its timing does not load. Never a reason to doubt the purchase. */
  | { status: 'unavailable'; detail: string };

/**
 * The confirmed order behind an id, once there is one.
 *
 * Failure here is deliberately quiet. This runs on a screen that has just told a buyer their
 * purchase is confirmed -- which is true, and was established by capture evidence, not by
 * this request. An alarm because a *timing panel* could not load would contradict a fact the
 * screen has already proven.
 */
export function useOrder(orderId: string | null): OrderState {
  const [state, setState] = useState<OrderState>({ status: 'idle' });

  const [previousId,setPreviousId]=useState(orderId);if(previousId!==orderId){setPreviousId(orderId);setState({status:orderId?'loading':'idle'})}
  useEffect(() => {
    if (!orderId) return;
    const controller = new AbortController();
    commerce.orders
      .read(orderId, controller.signal)
      .then((order) => setState({ status: 'ready', order }))
      .catch((cause: unknown) => {
        if ((cause as Error)?.name === 'AbortError') return;
        setState({
          status: 'unavailable',
          detail:
            cause instanceof CommerceError
              ? cause.detail
              : 'The order is confirmed; its timing could not be read.',
        });
      });
    return () => controller.abort();
  }, [orderId]);

  return state;
}

/** Whole seconds as something to read: `0s`, `47s`, `2m 14s`. Never rounds a span away. */
export function seconds(value: number | null): string {
  if (value === null || value === undefined) return 'not measured';
  if (value < 60) return `${value}s`;
  const m = Math.floor(value / 60);
  const s = value % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}
