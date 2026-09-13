import type { CSSProperties, ImgHTMLAttributes } from 'react';
type Props = Omit<ImgHTMLAttributes<HTMLImageElement>, 'src'> & {
  src: string | { src: string };
  fill?: boolean;
  priority?: boolean;
  quality?: number;
  unoptimized?: boolean;
};
export default function Image({
  src,
  alt = '',
  fill,
  priority,
  quality: _quality,
  unoptimized: _unoptimized,
  style,
  ...props
}: Props) {
  const positioned: CSSProperties = fill
    ? { position: 'absolute', height: '100%', width: '100%', inset: 0 }
    : {};
  return (
    // Native image is intentional: this static mount has no Next image optimizer.
    // oxlint-disable-next-line next/no-img-element
    <img
      {...props}
      alt={alt}
      src={typeof src === 'string' ? src : src.src}
      loading={priority ? 'eager' : props.loading || 'lazy'}
      style={{ ...positioned, ...style }}
    />
  );
}
