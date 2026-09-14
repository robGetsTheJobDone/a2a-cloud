import type { SVGProps } from "react";

type IconName =
  | "paperclip"
  | "send"
  | "stop"
  | "close"
  | "search"
  | "plus"
  | "settings"
  | "sliders"
  | "chevron-down"
  | "chevron-right"
  | "chevron-left"
  | "arrow-right"
  | "sparkle"
  | "user"
  | "agent"
  | "panel-right"
  | "panel-left"
  | "more"
  | "trash"
  | "download"
  | "upload"
  | "copy"
  | "check"
  | "x"
  | "warning"
  | "info"
  | "key"
  | "mail"
  | "shield"
  | "file"
  | "folder"
  | "folder-open"
  | "menu";

const PATHS: Record<IconName, JSX.Element> = {
  paperclip: (
    <path d="M21 11.5L12.5 20a5 5 0 1 1-7-7L14 4.5a3.5 3.5 0 1 1 5 5L10.5 18a2 2 0 1 1-3-3L15 7.5" />
  ),
  send: <path d="M5 12l14-7-3 7 3 7-14-7zM5 12h9" />,
  stop: <rect x="6" y="6" width="12" height="12" rx="1.5" />,
  close: <path d="M5 5l14 14M19 5L5 19" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9A1.7 1.7 0 0 0 10 3.1V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </>
  ),
  sliders: (
    <>
      <line x1="4" y1="6" x2="12" y2="6" />
      <line x1="16" y1="6" x2="20" y2="6" />
      <line x1="4" y1="12" x2="8" y2="12" />
      <line x1="12" y1="12" x2="20" y2="12" />
      <line x1="4" y1="18" x2="16" y2="18" />
      <line x1="20" y1="18" x2="20" y2="18" />
      <circle cx="14" cy="6" r="2" />
      <circle cx="10" cy="12" r="2" />
      <circle cx="18" cy="18" r="2" />
    </>
  ),
  "chevron-down": <path d="M6 9l6 6 6-6" />,
  "chevron-right": <path d="M9 6l6 6-6 6" />,
  "chevron-left": <path d="M15 6l-6 6 6 6" />,
  "arrow-right": <path d="M5 12h14M13 6l6 6-6 6" />,
  sparkle: (
    <path d="M12 3l1.8 4.6L18.5 9 13.8 11 12 15.6 10.2 11 5.5 9l4.7-1.4zM19 14l.8 2L22 17l-2.2.9-.8 2-.8-2L16 17l2.2-1z" />
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </>
  ),
  agent: (
    <>
      <rect x="4" y="7" width="16" height="12" rx="3" />
      <path d="M12 3v4M8 12v2M16 12v2" />
    </>
  ),
  "panel-right": (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M14 4v16" />
    </>
  ),
  "panel-left": (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M10 4v16" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1.4" />
      <circle cx="12" cy="12" r="1.4" />
      <circle cx="19" cy="12" r="1.4" />
    </>
  ),
  trash: (
    <>
      <path d="M4 7h16" />
      <path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2" />
      <path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
      <path d="M10 11v6M14 11v6" />
    </>
  ),
  download: <path d="M12 4v12m0 0l-5-5m5 5l5-5M4 20h16" />,
  upload: <path d="M12 20V8m0 0l-5 5m5-5l5 5M4 4h16" />,
  copy: (
    <>
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </>
  ),
  check: <path d="M4 12l5 5L20 6" />,
  x: <path d="M6 6l12 12M18 6L6 18" />,
  warning: (
    <>
      <path d="M12 3l10 18H2L12 3z" />
      <path d="M12 10v5M12 18v.5" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8v.5" />
    </>
  ),
  key: (
    <>
      <circle cx="8" cy="14" r="4" />
      <path d="M11 14h10M17 14v3M21 14v3" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="5" width="18" height="14" rx="2" />
      <path d="M3 7.5l9 6 9-6" />
    </>
  ),
  shield: <path d="M12 3l8 3v6c0 4-3.5 7.5-8 9-4.5-1.5-8-5-8-9V6l8-3z" />,
  file: (
    <>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5" />
    </>
  ),
  folder: (
    <>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </>
  ),
  "folder-open": (
    <>
      <path d="M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1" />
      <path d="M4 19h14a2 2 0 0 0 1.9-1.4l2-6A1.2 1.2 0 0 0 20.8 10H7.2a2 2 0 0 0-1.9 1.4z" />
    </>
  ),
  menu: <path d="M4 6h16M4 12h16M4 18h16" />,
};

export type IconProps = SVGProps<SVGSVGElement> & {
  name: IconName;
  size?: number;
};

export function Icon({
  name,
  size = 16,
  strokeWidth = 1.6,
  className,
  ...rest
}: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable={false}
      className={className}
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}

