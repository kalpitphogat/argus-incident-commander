// A small stroke icon set (24x24 grid, currentColor).
const P = {
  bell: '<path d="M6 9a6 6 0 0 1 12 0c0 6.5 2.5 8 2.5 8h-17S6 15.5 6 9z"/><path d="M10 20.5a2.2 2.2 0 0 0 4 0"/>',
  triage: '<rect x="5" y="4" width="14" height="17" rx="2.5"/><path d="M9 4.5h6v2.5H9z"/><path d="M9 12h6M9 16h4"/>',
  flag: '<path d="M5 21V4"/><path d="M5 4.5h12l-2.5 4 2.5 4H5"/>',
  metrics: '<path d="M3.5 20.5h17"/><path d="M5 16l4.5-5 3.5 3.5 6-7.5"/><path d="M15 7h4v4"/>',
  logs: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><path d="M7 9.5l2.5 2.5L7 14.5M12 14.5h5"/>',
  changes: '<circle cx="12" cy="12" r="3.2"/><path d="M3 12h5.8M15.2 12H21"/>',
  deps: '<circle cx="6" cy="6.5" r="2.5"/><circle cx="18" cy="6.5" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="M7.4 8.6l3.4 7.2M16.6 8.6l-3.4 7.2M8.5 6.5h7"/>',
  sparkle: '<path d="M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1L5 10.5l5.1-1.9z"/><path d="M18.5 16.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  shield: '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.2-7.5 9.5-4.3-1.3-7.5-4.9-7.5-9.5V6z"/><path d="M9 12l2 2 4-4"/>',
  route: '<circle cx="6" cy="18.5" r="2.3"/><circle cx="18" cy="5.5" r="2.3"/><path d="M8.3 18.5H15a3.5 3.5 0 0 0 0-7H9a3.5 3.5 0 0 1 0-7h6.7"/>',
  usercheck: '<circle cx="9" cy="8" r="3.8"/><path d="M2.5 20.5a6.5 6.5 0 0 1 13 0"/><path d="M16 11.5l2 2 4-4"/>',
  zap: '<path d="M13.5 2.5L4.5 14h7l-1 7.5 9-11.5h-7z"/>',
  activity: '<path d="M2.5 12h4l2.5-7 5 14 2.5-7h5"/>',
  pen: '<path d="M4 20l4.2-1 11-11a2.6 2.6 0 0 0-3.7-3.7l-11 11z"/><path d="M14 6.5l3.5 3.5"/>',
  database: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13"/><path d="M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9.5" y="9.5" width="5" height="5" rx="1"/><path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.5v.01"/>',
  loader: '<path d="M12 3a9 9 0 1 0 9 9"/>',
  play: '<path d="M7 4.5v15l12-7.5z"/>',
  pause: '<path d="M8 5v14M16 5v14"/>',
  dice: '<rect x="3.5" y="3.5" width="17" height="17" rx="4"/><circle cx="8.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="15.5" cy="15.5" r="1.1" fill="currentColor"/><circle cx="12" cy="12" r="1.1" fill="currentColor"/><circle cx="15.5" cy="8.5" r="1.1" fill="currentColor"/><circle cx="8.5" cy="15.5" r="1.1" fill="currentColor"/>',
  rocket: '<path d="M14.5 4.5c3-1.5 5.5-1 5.5-1s.5 2.5-1 5.5L13 15l-4-4z"/><path d="M9 11l-4.5-.5L7 8h4M13 15l.5 4.5L16 17v-4"/><path d="M6.5 17.5c-1 1-1.5 3-1.5 3s2-.5 3-1.5"/>',
  flame: '<path d="M12 21c-3.9 0-7-2.8-7-6.6 0-3 1.8-5 3.4-6.6.3 1.6 1.1 2.8 2.3 3.3C10.7 7.2 12.5 4.5 15 3c-.3 3.1 4 5.4 4 10.4 0 4.2-3.1 7.6-7 7.6z"/>',
  memory: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 10.5v3M10.5 10.5v3M14 10.5v3M17.5 10.5v3M6 17v2.5M18 17v2.5M12 17v2.5"/>',
  card: '<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 10h19M6.5 15h4"/>',
  lock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
  cart: '<circle cx="9" cy="20" r="1.4"/><circle cx="17" cy="20" r="1.4"/><path d="M2.5 3.5h3l2.4 11.5h10.6l2-8H6.5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
  download: '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M4.5 19.5h15"/>',
  copy: '<rect x="8.5" y="8.5" width="12" height="12" rx="2.5"/><path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.5-4.5L3.5 9M4 13a8 8 0 0 0 14.5 4.5l2-2.5"/><path d="M3.5 4v5h5M20.5 20v-5h-5"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  map: '<path d="M9 4.5l-5.5 2v13l5.5-2 6 2 5.5-2v-13l-5.5 2z"/><path d="M9 4.5v13M15 6.5v13"/>',
  layers: '<path d="M12 3.5l9 4.5-9 4.5L3 8z"/><path d="M3 12.5l9 4.5 9-4.5M3 16.5l9 4.5 9-4.5"/>',
  message: '<path d="M4.5 5.5h15a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-4.5 3.5v-3.5h-1A1.5 1.5 0 0 1 3 16V7a1.5 1.5 0 0 1 1.5-1.5z"/>',
  graph: '<circle cx="6" cy="5.5" r="2.2"/><circle cx="6" cy="18.5" r="2.2"/><circle cx="18" cy="12" r="2.2"/><path d="M6 7.7v8.6M8 6.4l8 4.6M8 17.6l8-4.6"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
};

export function icon(name, size = 16, cls = "") {
  const body = P[name] || P.sparkle;
  return `<svg class="${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

export const AGENT = {
  Pager: { icon: "bell", c: "var(--crit)" },
  Triage: { icon: "triage", c: "var(--accent)" },
  Commander: { icon: "flag", c: "var(--accent)" },
  Memory: { icon: "database", c: "var(--accent)" },
  "RCA lead": { icon: "sparkle", c: "var(--accent)" },
  Critic: { icon: "shield", c: "var(--accent)" },
  Planner: { icon: "route", c: "var(--accent)" },
  Approval: { icon: "usercheck", c: "var(--warn-ink)" },
  Executor: { icon: "zap", c: "var(--accent)" },
  Verifier: { icon: "activity", c: "var(--accent)" },
  Scribe: { icon: "pen", c: "var(--accent)" },
  Engine: { icon: "cpu", c: "var(--warn-ink)" },
  You: { icon: "usercheck", c: "var(--accent)" },
};

export const ANALYST = {
  metrics: { label: "Metrics analyst", icon: "metrics", c: "var(--s1)" },
  logs: { label: "Logs analyst", icon: "logs", c: "var(--s2)" },
  changes: { label: "Change analyst", icon: "changes", c: "var(--s3)" },
  dependencies: { label: "Dependency analyst", icon: "deps", c: "var(--s4)" },
};

export const LOGO = `<svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
  <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6250d6"/><stop offset=".55" stop-color="#2a78d6"/><stop offset="1" stop-color="#1baf7a"/></linearGradient></defs>
  <rect x="1" y="1" width="30" height="30" rx="9" fill="url(#lg)"/>
  <path d="M5.5 16S9.5 9 16 9s10.5 7 10.5 7-4 7-10.5 7S5.5 16 5.5 16z" fill="none" stroke="#fff" stroke-width="1.9" stroke-linejoin="round"/>
  <circle cx="16" cy="16" r="3.6" fill="#fff"/><circle cx="16" cy="16" r="1.5" fill="#2a78d6"/>
</svg>`;
