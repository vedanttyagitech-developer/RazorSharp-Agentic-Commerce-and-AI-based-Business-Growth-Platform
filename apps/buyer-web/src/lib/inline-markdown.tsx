/**
 * The little Markdown a model writes, drawn instead of shown.
 *
 * A reply arrives as text with `**bold**`, `- bullets` and `1. numbering`. The transcript
 * is `whitespace-pre-wrap`, so line breaks already hold; what remained was the markup
 * itself, printed as asterisks. This renders bold as bold and drops list markers and
 * heading hashes, and touches nothing else: no links, no HTML, no model.
 */

import type { ReactNode } from "react";

const MARKER = /^[ \t]*(?:[-*•]|\d{1,3}[.)])[ \t]+/;
const HEADING = /^[ \t]{0,3}#{1,6}[ \t]+/;

export function renderInline(text: string): ReactNode[] {
  const lines = text.split("\n").map((line) => line.replace(HEADING, "").replace(MARKER, ""));
  const nodes: ReactNode[] = [];
  lines.forEach((line, lineIndex) => {
    if (lineIndex > 0) nodes.push("\n");
    const parts = line.split(/\*\*(.+?)\*\*/g);
    parts.forEach((part, index) => {
      if (part.length === 0) return;
      nodes.push(
        index % 2 === 1 ? <strong key={`${lineIndex}-${index}`}>{part}</strong> : part,
      );
    });
  });
  return nodes;
}
