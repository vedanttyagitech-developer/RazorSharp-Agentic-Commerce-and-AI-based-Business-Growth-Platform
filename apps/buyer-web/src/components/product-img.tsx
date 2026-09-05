"use client";

import { useState, type ReactNode } from "react";
import { Package } from "lucide-react";

interface SafeImageProps {
  src?: string | null;
  alt: string;
  fallbackIcon?: ReactNode;
  fallbackEmoji?: string;
  className?: string;
  loading?: "lazy" | "eager";
}

/**
 * Reusable image component that handles loading failures and falls back
 * to a clean vector icon placeholder without breaking UI layouts.
 */
export function SafeImage({
  src,
  alt,
  fallbackIcon,
  fallbackEmoji,
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
        className="flex h-full w-full items-center justify-center select-none text-muted/40"
        aria-hidden="true"
      >
        {fallbackIcon || (fallbackEmoji && typeof fallbackEmoji !== "string" ? fallbackEmoji : <Package className="h-8 w-8 stroke-1" />)}
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
