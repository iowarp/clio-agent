import { useEffect, useRef, useState } from 'react';

interface Props {
	src: string;
	poster: string;
	width: number;
	height: number;
	label: string;
	className?: string;
	/** Shown and playing; inactive clips stay paused, out of sight and out of the accessibility tree. */
	active?: boolean;
	/** Load the clip ahead of being shown. */
	warm?: boolean;
}

/**
 * A silent looping clip. It plays only while on screen, and with reduced motion
 * it shows its poster and leaves playback to the visitor.
 */
export default function LoopVideo({ src, poster, width, height, label, className, active = true, warm = false }: Props) {
	const video = useRef<HTMLVideoElement>(null);
	const [reduced, setReduced] = useState(false);

	useEffect(() => {
		const query = window.matchMedia('(prefers-reduced-motion: reduce)');
		setReduced(query.matches);
		const change = () => setReduced(query.matches);
		query.addEventListener('change', change);
		return () => query.removeEventListener('change', change);
	}, []);

	useEffect(() => {
		const el = video.current;
		if (!el) return;
		if (!active) {
			el.pause();
			return;
		}
		if (reduced) return;
		el.currentTime = 0;
		const observer = new IntersectionObserver(
			([entry]) => {
				if (entry.isIntersecting) void el.play().catch(() => {});
				else el.pause();
			},
			{ threshold: 0.2 },
		);
		observer.observe(el);
		return () => observer.disconnect();
	}, [reduced, active]);

	return (
		<video
			ref={video}
			className={className}
			data-active={active || undefined}
			width={width}
			height={height}
			poster={active || warm ? poster : undefined}
			muted
			loop
			playsInline
			preload={warm && !active ? 'auto' : 'none'}
			controls={reduced && active}
			aria-label={label}
			aria-hidden={active ? undefined : true}
			tabIndex={active ? undefined : -1}
		>
			<source src={src} type="video/mp4" />
		</video>
	);
}
