import { useId, type CSSProperties } from "react";

const frontBolt = "M16 4 7 15h5.5l-1 7L21 11h-5.5z";

function OverlappingBolts() {
  const cutoutId = useId();
  return (
    <>
      <defs>
        <mask id={cutoutId} maskUnits="userSpaceOnUse" x="0" y="0" width="24" height="24">
          <rect width="24" height="24" fill="white" stroke="none" />
          <path d={frontBolt} fill="black" stroke="black" strokeWidth="2.5" />
        </mask>
      </defs>
      <g fill="currentColor" stroke="none">
        <path d="M12 2 3 13h5.5l-1 7L17 9h-5.5z" mask={`url(#${cutoutId})`} />
        <path d={frontBolt} />
      </g>
    </>
  );
}

const paths = {
  lens: (
    <>
      <path d="M4 5h6l2 2 2-2h6v15h-6l-2 2-2-2H4z" />
      <path d="M12 7v15M7 9h2M7 13h2M15 9h2M15 13h2" />
    </>
  ),
  graph: (
    <>
      <rect x="8" y="2" width="8" height="5" rx="1.5" />
      <rect x="2" y="17" width="7" height="5" rx="1.5" />
      <rect x="15" y="17" width="7" height="5" rx="1.5" />
      <path d="M12 7v5H5.5v5M12 12h6.5v5" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m16 16 5 5" />
    </>
  ),
  arrow: (
    <>
      <path d="M5 12h14m-5-5 5 5-5 5" />
    </>
  ),
  chevron: <path d="m9 5 7 7-7 7" />,
  down: <path d="m6 9 6 6 6-6" />,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  download: (
    <>
      <path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5l1.5 1.5M5 19l1.5-1.5M17.5 6.5l1.5-1.5" />
    </>
  ),
  moon: <path d="M20 15.2A9 9 0 0 1 8.8 4 9 9 0 1 0 20 15.2z" />,
  file: (
    <>
      <path d="M14 2H5v20h14V7zM14 2v5h5M8 12h8M8 16h6" />
    </>
  ),
  quote: (
    <>
      <path d="M10 6H4v7h5c0 3-1 4-3 5M20 6h-6v7h5c0 3-1 4-3 5" />
    </>
  ),
  book: (
    <>
      <path d="M12 5c-3-2-6-2-10-1v15c4-1 7-1 10 1 3-2 6-2 10-1V4c-4-1-7-1-10 1zM12 5v15" />
    </>
  ),
  check: <path d="m5 12 4 4L20 5" />,
  star: (
    <path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.3-4.1 5.9-.9z" />
  ),
  bookmark: <path d="M6 3.5h12v17l-6-4.2-6 4.2z" />,
  checklist: (
    <path d="M11 6h9M11 12h9M11 18h9M3.5 6l1.5 1.5L7.5 5M3.5 12l1.5 1.5 2.5-2.5M3.5 18l1.5 1.5 2.5-2.5" />
  ),
  shield: (
    <>
      <path d="m12 2 8 3v7c0 5-8 10-8 10S4 17 4 12V5z" />
      <path d="m8 11 3 3 5-6" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  minus: <path d="M5 12h14" />,
  fit: (
    <>
      <path d="M3 9V3h6M15 3h6v6M21 15v6h-6M9 21H3v-6" />
      <rect x="8" y="8" width="8" height="8" rx="1" />
    </>
  ),
  expand: (
    <>
      <path d="M3 8V3h5M16 3h5v5M21 16v5h-5M8 21H3v-5M8 8 3 3M16 8l5-5M16 16l5 5M8 16l-5 5" />
    </>
  ),
  collapse: (
    <>
      <path d="M3 8h5V3M16 3v5h5M21 16h-5v5M8 21v-5H3" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v6M12 7h.01" />
    </>
  ),
  upload: (
    <>
      <path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" />
    </>
  ),
  dot: <circle cx="12" cy="12" r="4" />,
  layers: (
    <>
      <path d="m12 2 10 6-10 6L2 8zM2 12l10 6 10-6M2 16l10 6 10-6" />
    </>
  ),
  bolt: <path d="m14 2-10 12h7l-1 8 10-12h-7z" />,
  bolts: <OverlappingBolts />,
  warning: (
    <>
      <path d="m12 3 10 18H2zM12 9v5M12 17h.01" />
    </>
  ),
  link: (
    <>
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </>
  ),
  library: (
    <>
      <path
        d="M4 4h4v16H4zM10 4h4v16h-4zM16.5 4.8l3.8-1 3.7 15.4-3.8 1z"
        transform="translate(-1 0)"
      />
    </>
  ),
  gear: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </>
  ),
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
  trash: <path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />,
  pause: <path d="M9 5v14M15 5v14" />,
  play: <path d="m7 4 13 8-13 8z" />,
  refresh: <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />,
  external: <path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6" />,
  keyboard: (
    <>
      <rect x="2" y="6" width="20" height="12" rx="2" />
      <path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M8 14h8" />
    </>
  ),
  panel: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M9 4v16" />
    </>
  ),
  table: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="1.5" />
      <path d="M3 10h18M3 15h18M10 4v16" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  sparkle: (
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z" />
  ),
  chat: (
    <>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4h0A1.5 1.5 0 0 1 4 14.5z" />
      <path d="M8.5 8.5h7M8.5 12h4" />
    </>
  ),
  highlighter: (
    <>
      <path d="m14.5 4.5 5 5-8 8H7v-4.5z" />
      <path d="M12 7l5 5M4 21h8" />
    </>
  ),
  send: <path d="M4 12 20 4l-6 16-3-7zM11 13l9-9" />,
  stop: <rect x="7" y="7" width="10" height="10" rx="2" />,
  copy: (
    <>
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </>
  ),
  pin: <path d="M9 4h6l-1 5 3 3v2H7v-2l3-3zM12 14v7" />,
  at: (
    <>
      <circle cx="12" cy="12" r="3.5" />
      <path d="M15.5 12v1.5a2.5 2.5 0 0 0 5 0V12a8.5 8.5 0 1 0-3.4 6.8" />
    </>
  ),
  pencil: <path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4" />,
};
export type IconName = keyof typeof paths;
export function Icon({
  name,
  size = 18,
  style,
}: {
  name: IconName;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      {paths[name]}
    </svg>
  );
}
