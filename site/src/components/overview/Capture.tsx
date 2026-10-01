import { Maximize2 } from 'lucide-react';
import { Frame, FramePanel } from '@/components/reui/frame';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/ui/dialog';

/** A product capture, with its responsive sources produced at build time. */
export interface CaptureImage {
	src: string;
	srcSet: string;
	width: number;
	height: number;
	/** Full-resolution source opened in the viewer. */
	full: string;
}

interface Props {
	image: CaptureImage;
	alt: string;
	caption: string;
	sizes?: string;
	/** Load eagerly; used for the capture in the first viewport. */
	priority?: boolean;
}

/**
 * A real product capture in a reui Frame. Clicking it opens the full-size
 * image in a dialog, with the caption as the dialog's description.
 */
export default function Capture({ image, alt, caption, sizes = '(min-width: 64rem) 44rem, 100vw', priority }: Props) {
	return (
		<Dialog>
			<Frame spacing="xs" className="[--frame-radius:var(--radius-xl)]">
				<FramePanel className="overflow-hidden p-0">
					<DialogTrigger
						className="group relative block w-full cursor-zoom-in border-0 bg-transparent p-0"
						aria-label={`View larger: ${alt}`}
					>
						<img
							src={image.src}
							srcSet={image.srcSet}
							sizes={sizes}
							width={image.width}
							height={image.height}
							alt={alt}
							loading={priority ? 'eager' : 'lazy'}
							decoding="async"
							className="block h-auto w-full"
						/>
						<span className="absolute top-2 right-2 inline-flex size-8 items-center justify-center rounded-md bg-background/80 text-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100">
							<Maximize2 className="size-4" aria-hidden="true" />
						</span>
					</DialogTrigger>
				</FramePanel>
			</Frame>
			<DialogContent className="w-[min(96vw,1400px)] max-w-none sm:max-w-none">
				<DialogTitle className="sr-only">{alt}</DialogTitle>
				<img src={image.full} alt={alt} width={image.width} height={image.height} className="h-auto w-full rounded-md" />
				<DialogDescription>{caption}</DialogDescription>
			</DialogContent>
		</Dialog>
	);
}
