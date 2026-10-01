import { Download } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { PLATFORMS, pickAsset, variantNote } from '@/lib/downloads';
import { useRelease } from './use-release';

/**
 * The hero's primary action.
 *
 * Server-rendered as "Download CLIO", which jumps to the platform cards.
 * Once the visitor's OS and the latest release are known, it becomes a direct
 * download of that platform's recommended installer.
 */
export default function HeroDownload() {
	const { status, assets, os, arch } = useRelease();
	const platform = PLATFORMS.find((p) => p.os === os);
	const picked =
		status === 'ready' && platform
			? pickAsset(assets, platform.primary.format, platform.primary.arch ?? arch)
			: null;

	return (
		<div className="flex flex-col items-start gap-2">
			<Button
				size="lg"
				className="h-11 gap-2 px-5 text-base"
				render={<a href={picked?.url ?? '#download'} />}
				nativeButton={false}
			>
				<Download aria-hidden="true" />
				{picked && platform ? `Download for ${platform.name}` : 'Download CLIO'}
			</Button>
			<p className="m-0 min-h-5 text-sm text-muted-foreground">{picked ? variantNote(picked.variant) : ''}</p>
		</div>
	);
}
