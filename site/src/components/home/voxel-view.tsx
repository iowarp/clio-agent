import { useEffect, useRef } from 'react';

/** Block dimensions in voxels: length, height, depth. */
const NX = 22;
const NY = 10;
const NZ = 8;
export const CYCLES = 20;

type Segment = readonly [number, number, number, number];

/**
 * Side profile of a cantilever after topology optimization: two chords from the
 * fixed face to the loaded corner and the braces between them.
 */
const CHORDS: readonly Segment[] = [
	[0, 8.6, 21, 2.2],
	[0, 0.7, 21, 1.0],
];
const BRACES: readonly Segment[] = [
	[0.5, 8.4, 7.5, 1.0],
	[7.5, 1.0, 12, 5.0],
	[12, 5.0, 16.5, 1.1],
	[16.5, 1.1, 19.5, 2.6],
];

function distance([ax, ay, bx, by]: Segment, x: number, y: number): number {
	const dx = bx - ax;
	const dy = by - ay;
	const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)));
	return Math.hypot(x - (ax + t * dx), y - (ay + t * dy));
}

/** Which voxels hold material at design cycle `cycle` (1 is the full block). */
export function solidAt(cycle: number): Uint8Array {
	const grow = Math.max(0, 1 - (cycle - 1) / (CYCLES - 4)) * 9;
	const cells = new Uint8Array(NX * NY * NZ);
	for (let x = 0; x < NX; x++) {
		for (let y = 0; y < NY; y++) {
			const cx = x + 0.5;
			const cy = y + 0.5;
			const chord = Math.min(...CHORDS.map((s) => distance(s, cx, cy)));
			const brace = Math.min(...BRACES.map((s) => distance(s, cx, cy)));
			for (let z = 0; z < NZ; z++) {
				const inner = z >= 2 && z <= NZ - 3;
				const keep = chord < 1.15 + grow || (inner && brace < 0.95 + grow) || (x === 0 && grow > 2);
				if (keep) cells[(x * NY + y) * NZ + z] = 1;
			}
		}
	}
	return cells;
}

/** Share of the block's volume kept at `cycle`. */
export function volumeAt(cycle: number): number {
	const cells = solidAt(cycle);
	let kept = 0;
	for (const cell of cells) kept += cell;
	return kept / cells.length;
}

interface Face {
	depth: number;
	points: number[];
	shade: number;
	heat: number;
}

const FACES = [
	{ n: [1, 0, 0], quad: [[1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 0, 1]] },
	{ n: [-1, 0, 0], quad: [[0, 0, 0], [0, 0, 1], [0, 1, 1], [0, 1, 0]] },
	{ n: [0, 1, 0], quad: [[0, 1, 0], [0, 1, 1], [1, 1, 1], [1, 1, 0]] },
	{ n: [0, -1, 0], quad: [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]] },
	{ n: [0, 0, 1], quad: [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]] },
	{ n: [0, 0, -1], quad: [[0, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 0]] },
] as const;

type RGB = [number, number, number];

function readColor(el: Element, name: string, fallback: string): RGB {
	const value = getComputedStyle(el).getPropertyValue(name).trim();
	const hex = (value.startsWith('#') ? value : fallback).slice(1);
	const full = hex.length === 3 ? [...hex].map((c) => c + c).join('') : hex;
	return [0, 2, 4].map((i) => Number.parseInt(full.slice(i, i + 2), 16)) as RGB;
}

interface Props {
	cycle: number;
	colorBy: 'shape' | 'energy';
	label: string;
}

/**
 * A small orthographic voxel renderer standing in for CLIO's 3D mesh view.
 * Drag (mouse or touch) or use the arrow keys to orbit.
 */
export default function VoxelView({ cycle, colorBy, label }: Props) {
	const canvas = useRef<HTMLCanvasElement>(null);
	const view = useRef({ yaw: -0.62, pitch: 0.42 });
	const cells = useRef<Uint8Array>(solidAt(cycle));
	const settings = useRef({ colorBy });
	const drawRef = useRef<() => void>(() => {});

	useEffect(() => {
		cells.current = solidAt(cycle);
		settings.current.colorBy = colorBy;
		drawRef.current();
	}, [cycle, colorBy]);

	useEffect(() => {
		const el = canvas.current;
		if (!el) return;
		const ctx = el.getContext('2d');
		if (!ctx) return;

		const draw = () => {
			const rect = el.getBoundingClientRect();
			if (rect.width === 0) return;
			const dpr = Math.min(window.devicePixelRatio || 1, 2);
			const w = Math.round(rect.width * dpr);
			const h = Math.round(rect.height * dpr);
			if (el.width !== w || el.height !== h) {
				el.width = w;
				el.height = h;
			}
			ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
			ctx.clearRect(0, 0, rect.width, rect.height);
			const blue = readColor(el, '--brand-blue', '#2f7599');
			const orange = readColor(el, '--brand-orange', '#e87c2f');
			const ivory = readColor(el, '--brand-ivory', '#f2ead9');
			const ink = readColor(el, '--ink-soft', '#d8cfbd');

			const { yaw, pitch } = view.current;
			const cy = Math.cos(yaw);
			const sy = Math.sin(yaw);
			const cp = Math.cos(pitch);
			const sp = Math.sin(pitch);
			const scale = Math.min(rect.width / 29, rect.height / 17);
			const ox = rect.width / 2;
			const oy = rect.height / 2 + scale * 0.4;
			const project = (x: number, y: number, z: number): [number, number, number] => {
				const px = x - NX / 2;
				const py = y - NY / 2;
				const pz = z - NZ / 2;
				const rx = px * cy - pz * sy;
				const rz = px * sy + pz * cy;
				return [ox + rx * scale, oy - (py * cp - rz * sp) * scale, py * sp + rz * cp];
			};
			const facing = (n: readonly number[]) => n[1] * sp + (n[0] * sy + n[2] * cy) * cp;

			const solid = cells.current;
			const at = (x: number, y: number, z: number) =>
				x >= 0 && y >= 0 && z >= 0 && x < NX && y < NY && z < NZ && solid[(x * NY + y) * NZ + z] === 1;
			const faces: Face[] = [];
			for (let x = 0; x < NX; x++) {
				for (let y = 0; y < NY; y++) {
					for (let z = 0; z < NZ; z++) {
						if (!at(x, y, z)) continue;
						for (const face of FACES) {
							if (at(x + face.n[0], y + face.n[1], z + face.n[2]) || facing(face.n) > 0) continue;
							const points: number[] = [];
							let depth = 0;
							for (const [dx, dy, dz] of face.quad) {
								const [px, py, d] = project(x + dx, y + dy, z + dz);
								points.push(px, py);
								depth += d;
							}
							const lambert = face.n[0] * 0.45 + face.n[1] * 0.8 + face.n[2] * 0.4;
							const heat = Math.min(1, Math.abs(y - 4.5) / 5 + (1 - x / NX) * 0.45);
							faces.push({ depth: depth / 4, points, shade: 0.6 + 0.4 * Math.max(0, lambert), heat });
						}
					}
				}
			}
			faces.sort((a, b) => b.depth - a.depth);
			const energy = settings.current.colorBy === 'energy';
			for (const face of faces) {
				const color = energy
					? blue.map((c, i) => c + (orange[i] - c) * face.heat)
					: blue.map((c, i) => c * 0.82 + ivory[i] * 0.18);
				const [r, g, b] = color.map((c) => Math.round(Math.min(255, c * face.shade + 18)));
				ctx.beginPath();
				ctx.moveTo(face.points[0], face.points[1]);
				for (let i = 2; i < 8; i += 2) ctx.lineTo(face.points[i], face.points[i + 1]);
				ctx.closePath();
				ctx.fillStyle = `rgb(${r} ${g} ${b})`;
				ctx.fill();
				ctx.strokeStyle = `rgb(${Math.round(r * 0.7)} ${Math.round(g * 0.7)} ${Math.round(b * 0.7)})`;
				ctx.lineWidth = 0.6;
				ctx.stroke();
			}
			// The fixed face and the load at the free corner.
			const [fx, fy] = project(0, NY, NZ / 2);
			const [gx, gy] = project(0, 0, NZ / 2);
			ctx.strokeStyle = `rgb(${ink.join(' ')} / 0.75)`;
			ctx.lineWidth = 1.5;
			ctx.setLineDash([3, 3]);
			ctx.beginPath();
			ctx.moveTo(fx - 8, fy);
			ctx.lineTo(gx - 8, gy);
			ctx.stroke();
			ctx.setLineDash([]);
			const [lx, ly] = project(NX - 0.5, 0, NZ / 2);
			ctx.strokeStyle = `rgb(${orange.join(' ')})`;
			ctx.fillStyle = `rgb(${orange.join(' ')})`;
			ctx.lineWidth = 2;
			ctx.beginPath();
			ctx.moveTo(lx, ly + 4);
			ctx.lineTo(lx, ly + 26);
			ctx.stroke();
			ctx.beginPath();
			ctx.moveTo(lx, ly + 31);
			ctx.lineTo(lx - 5, ly + 23);
			ctx.lineTo(lx + 5, ly + 23);
			ctx.closePath();
			ctx.fill();
		};
		drawRef.current = draw;
		draw();

		let drag: { x: number; y: number } | null = null;
		const orbit = (dx: number, dy: number) => {
			view.current.yaw += dx;
			view.current.pitch = Math.max(-0.2, Math.min(1.3, view.current.pitch + dy));
			draw();
		};
		const down = (event: PointerEvent) => {
			drag = { x: event.clientX, y: event.clientY };
			el.setPointerCapture(event.pointerId);
		};
		const move = (event: PointerEvent) => {
			if (!drag) return;
			orbit((event.clientX - drag.x) * 0.01, (event.clientY - drag.y) * 0.008);
			drag = { x: event.clientX, y: event.clientY };
		};
		const up = () => {
			drag = null;
		};
		const key = (event: KeyboardEvent) => {
			const steps: Record<string, [number, number]> = {
				ArrowLeft: [-0.15, 0],
				ArrowRight: [0.15, 0],
				ArrowUp: [0, -0.1],
				ArrowDown: [0, 0.1],
			};
			const step = steps[event.key];
			if (!step) return;
			event.preventDefault();
			orbit(step[0], step[1]);
		};
		const resize = new ResizeObserver(draw);
		resize.observe(el);
		const theme = new MutationObserver(draw);
		theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
		el.addEventListener('pointerdown', down);
		el.addEventListener('pointermove', move);
		el.addEventListener('pointerup', up);
		el.addEventListener('pointercancel', up);
		el.addEventListener('keydown', key);
		return () => {
			resize.disconnect();
			theme.disconnect();
			el.removeEventListener('pointerdown', down);
			el.removeEventListener('pointermove', move);
			el.removeEventListener('pointerup', up);
			el.removeEventListener('pointercancel', up);
			el.removeEventListener('keydown', key);
		};
	}, []);

	return <canvas ref={canvas} className="wsd-voxels" role="img" aria-label={label} tabIndex={0} />;
}
