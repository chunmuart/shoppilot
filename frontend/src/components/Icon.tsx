import type { CSSProperties, ReactNode } from "react";
const paths: Record<string, ReactNode> = {
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <ellipse cx="12" cy="12" rx="4" ry="9" />
      <path d="M3 12h18M5 6.5c4.5 2 9.5 2 14 0M5 17.5c4.5-2 9.5-2 14 0" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
  chat: (
    <>
      <path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-10a9 9 0 0 1 18 0Z" />
      <path d="M7 10h10M7 14h6" />
    </>
  ),
  heart: (
    <path d="M20.5 4.7c-2-2-5.5-1.2-8.5 1.7C9 3.5 5.5 2.7 3.5 4.7c-3.8 4 1.7 10.3 8.5 15 6.8-4.7 12.3-11 8.5-15Z" />
  ),
  arrow: <path d="M5 12h14m-5-5 5 5-5 5" />,
  up: <path d="M12 19V5m-6 6 6-6 6 6" />,
  check: <path d="m5 12 4 4L19 6" />,
  pin: (
    <>
      <path d="M18.5 10c0 5-6.5 11-6.5 11S5.5 15 5.5 10a6.5 6.5 0 1 1 13 0Z" />
      <circle cx="12" cy="10" r="2" />
    </>
  ),
  spark: <path d="m12 3 2.3 6.7L21 12l-6.7 2.3L12 21l-2.3-6.7L3 12l6.7-2.3Z" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  bag: (
    <>
      <path d="M5 7h14l1 14H4L5 7Z" />
      <path d="M8 8V6a4 4 0 0 1 8 0v2" />
    </>
  ),
  order: (
    <>
      <path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5Z" />
      <path d="M14 3v5h5M8 12h8M8 16h6" />
    </>
  ),
  leaf: (
    <>
      <path d="M20 3c0 10-3 17-10 17a7 7 0 0 1-7-7C3 6 10 3 20 3Z" />
      <path d="M5 19 15 9" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6m0-10v.1" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
  compare: <path d="M5 4v16m14-16v16M8 8h8m-3-3 3 3-3 3M16 16H8m3-3-3 3 3 3" />,
};
export default function Icon({
  name,
  className = "",
  style,
}: {
  name: string;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      style={style}
      aria-hidden="true"
    >
      {paths[name] ?? paths.spark}
    </svg>
  );
}
