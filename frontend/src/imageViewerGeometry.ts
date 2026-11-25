export interface ImageSize {
  width: number;
  height: number;
}
export interface ImagePan {
  x: number;
  y: number;
}

export function imageBounds(size: ImageSize, rotation: number): ImageSize {
  return Math.abs(rotation % 180) === 90
    ? { width: size.height, height: size.width }
    : size;
}

export function imageFit(size: ImageSize, viewport: ImageSize, rotation: number) {
  const bounds = imageBounds(size, rotation);
  if (!bounds.width || !bounds.height) return 1;
  return Math.min(
    1,
    Math.max(1, viewport.width - 32) / bounds.width,
    Math.max(1, viewport.height - 32) / bounds.height,
  );
}

export function imagePan(
  pan: ImagePan,
  size: ImageSize,
  viewport: ImageSize,
  rotation: number,
  scale: number,
): ImagePan {
  const bounds = imageBounds(size, rotation);
  const x = Math.max(0, (bounds.width * scale - viewport.width) / 2);
  const y = Math.max(0, (bounds.height * scale - viewport.height) / 2);
  return {
    x: Math.max(-x, Math.min(x, pan.x)) || 0,
    y: Math.max(-y, Math.min(y, pan.y)) || 0,
  };
}

export const imageZoom = (value: number) => Math.max(0.25, Math.min(8, value));
