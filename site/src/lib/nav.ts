/** One primary destination in the site header. */
export interface NavItem {
	label: string;
	href: string;
	/** Path prefix that marks this item as the current section. */
	match: string;
}

/** The three primary destinations, in header order. */
export const primaryNav: readonly NavItem[] = [
	{ label: 'Overview', href: '/', match: '/' },
	{ label: 'Docs', href: '/docs/', match: '/docs' },
	{ label: 'Tutorials', href: '/tutorials/', match: '/tutorials' },
];

/**
 * Whether `item` is the section that contains `pathname`.
 *
 * The overview matches only the site root; the other sections match their
 * own path and everything below it.
 */
export function isActiveNav(item: NavItem, pathname: string): boolean {
	if (item.match === '/') return pathname === '/' || pathname === '/index.html';
	return pathname === item.match || pathname.startsWith(`${item.match}/`);
}
