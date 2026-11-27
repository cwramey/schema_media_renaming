export interface NavItem {
  to: string;
  label: string;
  /** Match the path exactly (Home). */
  end?: boolean;
}

/** The whole main navigation: everything else is reached from these pages. */
export const NAV: readonly NavItem[] = [
  { to: '/', label: 'Home', end: true },
  { to: '/history', label: 'History' },
  { to: '/settings', label: 'Settings' },
];
