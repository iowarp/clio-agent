/**
 * Build-time image processing for product captures.
 *
 * Every capture on the overview goes through here, so each one ships as
 * responsive WebP at a few widths plus a full-size WebP for the viewer.
 */
import { getImage } from 'astro:assets';
import type { CaptureImage } from '@/components/overview/Capture';

const WIDTHS = [480, 800, 1246];

/** Produce responsive sources for one capture. */
export async function captureImage(source: ImageMetadata): Promise<CaptureImage> {
	const widths = WIDTHS.filter((w) => w <= source.width);
	const responsive = await getImage({ src: source, format: 'webp', widths, quality: 82 });
	const full = await getImage({ src: source, format: 'webp', quality: 90 });
	return {
		src: responsive.src,
		srcSet: responsive.srcSet.attribute,
		width: source.width,
		height: source.height,
		full: full.src,
	};
}
