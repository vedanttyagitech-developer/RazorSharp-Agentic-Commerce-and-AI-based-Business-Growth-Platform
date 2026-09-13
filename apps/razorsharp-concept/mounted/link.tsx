import type { AnchorHTMLAttributes } from 'react';
export default function Link({
  prefetch: _prefetch,
  replace: _replace,
  scroll: _scroll,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & {
  prefetch?: boolean;
  replace?: boolean;
  scroll?: boolean;
}) {
  return <a {...props} />;
}
