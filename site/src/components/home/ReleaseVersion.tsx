import { useRelease } from '@/components/overview/use-release';

const REPO = 'https://github.com/iowarp/clio-agent';

/**
 * The version a visitor will download: the latest published release, from the
 * same lookup the download links use. Until it loads, or if GitHub cannot be
 * reached, it shows the build's version.
 */
export default function ReleaseVersion({
	fallback,
	before = '',
	after = '',
	link = true,
}: {
	fallback: string;
	before?: string;
	after?: string;
	link?: boolean;
}) {
	const { tag } = useRelease();
	const version = tag ? tag.replace(/^v/, '') : fallback;
	const text = `${before}${version}${after}`;
	if (!link) return <>{text}</>;
	return <a href={`${REPO}/releases/tag/v${version}`}>{text}</a>;
}
