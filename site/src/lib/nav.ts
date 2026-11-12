/** One primary destination in the site header. */
export interface NavItem {
	label: string;
	href: string;
	/** Path prefixes that mark this item as the current section. */
	match: readonly string[];
	/** Path prefixes inside `match` that belong to another item. */
	exclude?: readonly string[];
}

/** The primary destinations, in header order, shared by the home page and the docs. */
export const primaryNav: readonly NavItem[] = [
	{ label: 'Overview', href: '/', match: ['/'] },
	{ label: 'Docs', href: '/docs/', match: ['/docs'], exclude: ['/docs/examples', '/docs/versions'] },
	{ label: 'Examples', href: '/docs/examples/', match: ['/docs/examples', '/tutorials'] },
	{ label: "What's new", href: '/docs/versions/', match: ['/docs/versions', '/versions'] },
];

const under = (pathname: string, prefix: string) =>
	pathname === prefix || pathname.startsWith(`${prefix}/`);

/**
 * Whether `item` is the section that contains `pathname`.
 *
 * The overview matches only the site root; the other sections match their
 * own paths and everything below them, minus paths owned by another item.
 */
export function isActiveNav(item: NavItem, pathname: string): boolean {
	if (item.href === '/') return pathname === '/' || pathname === '/index.html';
	if (item.exclude?.some((prefix) => under(pathname, prefix))) return false;
	return item.match.some((prefix) => under(pathname, prefix));
}
