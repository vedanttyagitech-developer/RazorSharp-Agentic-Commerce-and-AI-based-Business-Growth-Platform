/** Retry a recovery read until it succeeds, without overlapping requests or late callbacks. */
export function retryRecoveryRead<T>(
  read: (signal: AbortSignal) => Promise<T>,
  onSuccess: (value: T) => void,
  onError: (error: unknown) => void,
): () => void {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let complete = false;
  const run = async () => {
    let value: T;
    try {
      value = await read(controller.signal);
    } catch (error) {
      if (controller.signal.aborted) return;
      onError(error);
      timer = setTimeout(() => void run(), 5000);
      return;
    }
    if (controller.signal.aborted || complete) return;
    complete = true;
    onSuccess(value);
  };
  void run();
  return () => {
    controller.abort();
    if (timer !== undefined) clearTimeout(timer);
  };
}
