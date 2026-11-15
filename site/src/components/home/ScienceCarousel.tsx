import { useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { ArrowRight, ChevronLeft, ChevronRight } from 'lucide-react';
import type { ScienceSlide } from '@/lib/science-slides';
import './science-carousel.css';

interface Props {
	slides: readonly ScienceSlide[];
}

/** How far a swipe must travel, in CSS pixels, to change slide. */
const SWIPE = 40;
const SIZES = '(max-width: 1180px) calc(100vw - 40px), 62vw';

/**
 * One capture per field, moved by the arrows, the indicators, a swipe or the
 * left and right keys. It never advances on its own.
 */
export default function ScienceCarousel({ slides }: Props) {
	const [active, setActive] = useState(0);
	const start = useRef<{ x: number; y: number } | null>(null);
	const count = slides.length;
	const go = (index: number) => setActive((index + count) % count);

	const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
		const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
		if (!step) return;
		event.preventDefault();
		go(active + step);
	};
	const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
		if (event.pointerType === 'mouse') return;
		start.current = { x: event.clientX, y: event.clientY };
	};
	const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
		const from = start.current;
		start.current = null;
		if (!from) return;
		const dx = event.clientX - from.x;
		if (Math.abs(dx) < SWIPE || Math.abs(dx) < Math.abs(event.clientY - from.y)) return;
		go(active + (dx < 0 ? 1 : -1));
	};

	return (
		<div
			className="sc"
			role="region"
			aria-roledescription="carousel"
			aria-label="CLIO doing science"
			tabIndex={0}
			onKeyDown={onKeyDown}
		>
			<div
				className="sc-stage"
				onPointerDown={onPointerDown}
				onPointerUp={onPointerUp}
				onPointerCancel={() => (start.current = null)}
			>
				<div className="sc-track" style={{ transform: `translateX(-${active * 100}%)` }}>
					{slides.map((slide, index) => {
						const near = Math.min(Math.abs(index - active), count - Math.abs(index - active)) <= 1;
						return (
							<figure
								key={slide.id}
								className="sc-slide"
								role="group"
								aria-roledescription="slide"
								aria-label={`${index + 1} of ${count}: ${slide.field}`}
								aria-hidden={index === active ? undefined : true}
								data-themed={slide.dark ? '' : undefined}
							>
								<img
									className="sc-light"
									src={slide.light.src}
									srcSet={slide.light.srcSet}
									sizes={SIZES}
									alt={slide.alt}
									width={1600}
									height={1000}
									loading={near ? 'eager' : 'lazy'}
									decoding="async"
									draggable={false}
								/>
								{slide.dark && (
									<img
										className="sc-dark"
										src={slide.dark.src}
										srcSet={slide.dark.srcSet}
										sizes={SIZES}
										alt={slide.alt}
										width={1600}
										height={1000}
										loading={near ? 'eager' : 'lazy'}
										decoding="async"
										draggable={false}
									/>
								)}
							</figure>
						);
					})}
				</div>
			</div>
			<div className="sc-side">
				<div className="sc-copies">
					{slides.map((slide, index) => (
						<div
							key={slide.id}
							className="sc-copy"
							data-active={index === active || undefined}
							aria-hidden={index === active ? undefined : true}
						>
							<p className="h-eyebrow">{slide.field}</p>
							<h3>{slide.title}</h3>
							<p>{slide.body}</p>
							<a href={slide.link.href} tabIndex={index === active ? undefined : -1}>
								{slide.link.text} <ArrowRight size={16} aria-hidden="true" />
							</a>
						</div>
					))}
				</div>
				<div className="sc-controls">
					<button type="button" className="sc-arrow" aria-label="Previous slide" onClick={() => go(active - 1)}>
						<ChevronLeft size={20} aria-hidden="true" />
					</button>
					<ol className="sc-dots">
						{slides.map((slide, index) => (
							<li key={slide.id}>
								<button
									type="button"
									aria-label={`Slide ${index + 1} of ${count}: ${slide.field}, ${slide.title}`}
									aria-current={index === active ? 'true' : undefined}
									onClick={() => go(index)}
								/>
							</li>
						))}
					</ol>
					<button type="button" className="sc-arrow" aria-label="Next slide" onClick={() => go(active + 1)}>
						<ChevronRight size={20} aria-hidden="true" />
					</button>
					<span className="sc-count" aria-hidden="true">
						{active + 1} of {count}
					</span>
					<span className="h-hidden" aria-live="polite">
						{`Slide ${active + 1} of ${count}: ${slides[active].field}, ${slides[active].title}`}
					</span>
				</div>
			</div>
		</div>
	);
}
