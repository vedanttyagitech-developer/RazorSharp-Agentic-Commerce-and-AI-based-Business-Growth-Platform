"use client";

import { useState } from "react";

interface SafeImageProps {
  src?: string | null;
  alt: string;
  fallbackEmoji?: string;
  className?: string;
  loading?: "lazy" | "eager";
}

/**
 * Reusable image component that handles loading failures and falls back
 * to a styled placeholder or emoji without breaking UI layouts.
 */
export function SafeImage({
  src,
  alt,
  fallbackEmoji = "📦",
  className = "w-full h-full object-contain",
  loading = "lazy",
}: SafeImageProps) {
  const [prevSrc, setPrevSrc] = useState(src);
  const [hasError, setHasError] = useState(false);

  if (src !== prevSrc) {
    setPrevSrc(src);
    setHasError(false);
  }

  if (!src || hasError) {
    return (
      <span
        className="flex h-full w-full items-center justify-center text-3xl select-none"
        aria-hidden="true"
      >
        {fallbackEmoji}
      </span>
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      loading={loading}
      onError={() => setHasError(true)}
      className={className}
    />
  );
}
