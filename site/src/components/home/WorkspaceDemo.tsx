import { useCallback, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';
import {
	Activity,
	Archive,
	ArrowUp,
	AtSign,
	Atom,
	Bell,
	BellRing,
	Bot,
	Box,
	Check,
	ChevronDown,
	ChevronLeft,
	ChevronRight,
	ChevronsUpDown,
	CircleHelp,
	Columns2,
	CornerDownLeft,
	File,
	FileCode,
	FileText,
	Folder,
	Globe,
	Inbox,
	ListFilter,
	ListTree,
	LoaderCircle,
	Maximize2,
	MoreHorizontal,
	PanelLeft,
	PanelRight,
	Paperclip,
	Play,
	Plus,
	Rows2,
	Search,
	SlidersHorizontal,
	SquarePen,
	SquareTerminal,
	Upload,
	User,
	Wrench,
	X,
} from 'lucide-react';
import VoxelView, { CYCLES, volumeAt } from './voxel-view';
import './workspace-demo.css';

type Panel = 'files' | 'sources' | 'artifacts' | 'activity' | 'agent' | 'terminal' | 'browser';

interface Conversation {
	id: string;
	title: string;
	time: string;
	inWorkspace: boolean;
}

interface Turn {
	id: number;
	text: string;
	kind: 'export' | 'energy';
	done: boolean;
}

const RUN = 'abaqus optimization task=Opt-Process-1.par job=Opt-Process-1-Job';
const QUESTION = 'Export cycle 20 as an STL for printing?';
const FINAL_VOLUME = Math.round(volumeAt(CYCLES) * 100);

interface WorkFile {
	name: string;
	size: string;
	icon: typeof File;
	/** The artifact kind, for files the agent produced. */
	artifact?: string;
}

const WORK_FILES: WorkFile[] = [
	{ name: 'abaqusMacros.py', size: '8.4 KB', icon: FileCode },
	{ name: 'design-review.md', size: '1.2 KB', icon: FileText, artifact: 'document' },
	{ name: 'Opt-Process-1-Job.odb', size: '182 MB', icon: File },
	{ name: 'Opt-Process-1.par', size: '2.1 KB', icon: FileCode },
	{ name: 'topology_cycle20.vtu', size: '3.6 MB', icon: Box, artifact: 'mesh' },
];
const STL_FILE: WorkFile = { name: 'block_cycle20.stl', size: '1.9 MB', icon: Box, artifact: 'model' };

function workFiles(exported: boolean) {
	return [...WORK_FILES, ...(exported ? [STL_FILE] : [])].sort((a, b) =>
		a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
	);
}

const PAR_TEXT = `DRESP
  ID_NAME = DR_StrainEnergy
  TYPE = STRAIN_ENERGY
  GROUP_OPER = SUM
END_
DRESP
  ID_NAME = DR_Volume
  TYPE = VOLUME
END_
OBJ_FUNC
  ID_NAME = OBJ_MinEnergy
  DRESP = DR_StrainEnergy
  TARGET = MIN
END_
CONSTRAINT
  ID_NAME = CON_Volume
  DRESP = DR_Volume
  MAGNITUDE = REL
  LE_VALUE = ${(FINAL_VOLUME / 100).toFixed(2)}
END_
STOP
  ID_NAME = global_stop
  ITER_MAX = ${CYCLES}
END_`;

const SOURCES: Record<string, string> = {
	'Opt-Process-1.par': PAR_TEXT,
	'abaqusMacros.py': `# -*- coding: mbcs -*-
from abaqus import *
from abaqusConstants import *


def load_block():
    import regionToolset
    m = mdb.models['Block']
    a = m.rootAssembly
    face = a.instances['Block-1'].faces.findAt(((0.0, 25.0, 25.0),))
    m.EncastreBC(name='Fixed', createStepName='Initial',
                 region=regionToolset.Region(faces=face))
    corner = a.instances['Block-1'].vertices.findAt(((200.0, 0.0, 50.0),))
    m.ConcentratedForce(name='Load', createStepName='Load',
                        region=regionToolset.Region(vertices=corner), cf2=-1000.0)`,
};

/** Models the composer chip cycles through, written as the app writes them, with the maker's mark. */
const MODELS = [
	{ logo: 'openai', route: 'codex', model: 'luna 5.6' },
	{ logo: 'anthropic', route: 'claude code', model: 'opus 5.5' },
	{ logo: 'ollama', route: 'ollama', model: 'qwen3:8b' },
	{ logo: 'openai', route: 'codex', model: 'sol 5.6' },
	{ logo: 'anthropic', route: 'claude code', model: 'sonnet 5.5' },
	{ logo: 'ollama', route: 'ollama', model: 'gpt-oss:20b' },
];

function ModelLabel({ model }: { model: (typeof MODELS)[number] }) {
	return (
		<>
			<i className="wsd-logo" style={{ '--logo': `url(/provider-logos/${model.logo}.svg)` } as CSSProperties} />
			<span>
				{model.route} / {model.model}
			</span>
		</>
	);
}

/** The composer's model chip. The page cannot run a model, so it rolls through a few the app can use. */
function ModelChip({ animate }: { animate: boolean }) {
	const [at, setAt] = useState(0);
	useEffect(() => {
		if (!animate) return;
		const timer = window.setInterval(() => setAt((value) => (value + 1) % MODELS.length), 1800);
		return () => window.clearInterval(timer);
	}, [animate]);
	const previous = (at + MODELS.length - 1) % MODELS.length;
	return (
		<span className="wsd-model">
			{animate ? (
				<span key={`out-${at}`} className="wsd-model-item" data-leaving>
					<ModelLabel model={MODELS[previous]} />
				</span>
			) : null}
			<span key={`in-${at}`} className="wsd-model-item" data-entering={animate || undefined}>
				<ModelLabel model={MODELS[at]} />
			</span>
		</span>
	);
}

const START: Conversation[] = [
	{ id: 'topology', title: 'Optimize the loaded block', time: 'Now', inWorkspace: true },
	{ id: 'mesh', title: 'Mesh convergence check', time: '1h', inWorkspace: true },
	{ id: 'review', title: 'Notes for the design review', time: '2d', inWorkspace: false },
];

const STRIP: { id: Panel; label: string; icon: typeof Folder }[] = [
	{ id: 'files', label: 'Files', icon: Folder },
	{ id: 'sources', label: 'Sources', icon: Paperclip },
	{ id: 'artifacts', label: 'Artifacts', icon: Box },
	{ id: 'activity', label: 'Activity', icon: Activity },
	{ id: 'agent', label: 'Agent', icon: Bot },
	{ id: 'terminal', label: 'Terminal', icon: SquareTerminal },
	{ id: 'browser', label: 'Browser', icon: Globe },
];

/** Phases of the opening run: asked, thinking, reading, optimizing, writing, answered, asking. */
const PHASE_AT = [0, 700, 2000, 3100, 8400, 9300, 10300];

function useReducedMotion() {
	const [reduced, setReduced] = useState(false);
	useEffect(() => {
		const query = matchMedia('(prefers-reduced-motion: reduce)');
		setReduced(query.matches);
	}, []);
	return reduced;
}

function Spinner({ size = 14, label }: { size?: number; label?: string }) {
	return (
		<LoaderCircle
			className="wsd-spin"
			size={size}
			aria-label={label}
			aria-hidden={label ? undefined : true}
		/>
	);
}

function Elapsed({ from }: { from: number }) {
	const [now, setNow] = useState(() => Date.now());
	useEffect(() => {
		const timer = window.setInterval(() => setNow(Date.now()), 250);
		return () => window.clearInterval(timer);
	}, []);
	return <span className="wsd-elapsed">{Math.max(0, Math.floor((now - from) / 1000))}s</span>;
}

function Thinking({ from }: { from: number }) {
	return (
		<div className="wsd-activity wsd-live" role="status">
			<ChevronRight size={13} className="wsd-faint" aria-hidden="true" />
			<span className="wsd-shimmer">Thinking...</span>
			<Elapsed from={from} />
			<Spinner label="Thinking" />
		</div>
	);
}

function ToolRow({
	verb,
	target,
	meta,
	running,
	command,
	output,
}: {
	verb: string;
	target: string;
	meta?: string;
	running?: boolean;
	command?: string;
	output?: string;
}) {
	const [open, setOpen] = useState(false);
	const expandable = Boolean(output) && !running;
	return (
		<div className="wsd-tool" data-open={open || undefined}>
			<button
				type="button"
				className="wsd-tool-row"
				aria-expanded={expandable ? open : undefined}
				disabled={!expandable}
				onClick={() => setOpen((value) => !value)}
			>
				{running ? <Spinner label="Running" /> : <Check className="wsd-ok" size={14} aria-label="Done" />}
				<strong>{verb}</strong>
				<code>{target}</code>
				{meta ? <span className="wsd-tool-meta">{meta}</span> : null}
				{expandable ? <ChevronDown className="wsd-chevron" size={14} aria-hidden="true" /> : null}
			</button>
			{open && output ? (
				<div className="wsd-term">
					{command || verb === 'Run' ? (
						<>
							<span className="wsd-term-label">Command</span>
							<pre>
								<span className="wsd-term-prompt">$</span> {command ?? target}
							</pre>
						</>
					) : null}
					<span className="wsd-term-label">{verb === 'Write' ? 'Wrote' : 'Output'}</span>
					<pre>{output}</pre>
					{verb === 'Run' ? <span className="wsd-term-foot">Process exited with code 0.</span> : null}
				</div>
			) : null}
		</div>
	);
}

/** The collapsed run of tool calls, as the app shows it between messages. */
function ActivityGroup({
	summary,
	running,
	live,
	defaultOpen = false,
	children,
}: {
	summary: string;
	running?: boolean;
	live?: ReactNode;
	defaultOpen?: boolean;
	children: ReactNode;
}) {
	const [open, setOpen] = useState(defaultOpen);
	return (
		<div className="wsd-group" data-open={open || undefined}>
			<button
				type="button"
				className="wsd-activity"
				aria-expanded={open}
				onClick={() => setOpen((value) => !value)}
			>
				<ChevronRight size={13} className="wsd-chevron-r" aria-hidden="true" />
				<Wrench size={13} aria-hidden="true" />
				<span className={running ? 'wsd-shimmer' : undefined}>{summary}</span>
				{live}
				{running ? <Spinner label="Running" /> : <Check className="wsd-ok" size={14} aria-label="Done" />}
			</button>
			{open ? <div className="wsd-group-body">{children}</div> : null}
		</div>
	);
}

function FileCard({ badge, name, kind, size, onOpen }: { badge: string; name: string; kind: string; size: string; onOpen: () => void }) {
	return (
		<button type="button" className="wsd-file-card" onClick={onOpen}>
			<span className="wsd-file-icon" data-badge={badge}>
				<File size={30} strokeWidth={1.2} aria-hidden="true" />
				<span>{badge}</span>
			</span>
			<span>
				<strong>{name}</strong>
				<small>
					{kind}&nbsp;&nbsp;{size}&nbsp;&nbsp;v1&nbsp;&nbsp;now
				</small>
			</span>
		</button>
	);
}

function Voice({ children, stream }: { children: string; stream?: boolean }) {
	const words = children.split(' ');
	const [shown, setShown] = useState(stream ? 0 : words.length);
	useEffect(() => {
		if (!stream) return;
		const timer = window.setInterval(
			() =>
				setShown((count) => {
					if (count >= words.length) window.clearInterval(timer);
					return Math.min(words.length, count + 2);
				}),
			45,
		);
		return () => window.clearInterval(timer);
	}, [stream, words.length]);
	return <p className="wsd-voice">{words.slice(0, shown).join(' ')}</p>;
}

function UserMessage({ children }: { children: ReactNode }) {
	return <div className="wsd-user">{children}</div>;
}

function FileRows({
	files,
	note,
	fresh,
	onOpen,
}: {
	files: WorkFile[];
	note: (file: WorkFile) => string;
	fresh?: boolean;
	onOpen: (name: string) => void;
}) {
	return (
		<ul className="wsd-files">
			{files.map((file) => {
				const Icon = file.icon;
				return (
					<li key={file.name} data-fresh={(fresh && file.name.endsWith('.stl')) || undefined}>
						<button type="button" onClick={() => onOpen(file.name)}>
							<Icon size={14} aria-hidden="true" />
							<span>{file.name}</span>
							<small>{note(file)}</small>
						</button>
					</li>
				);
			})}
		</ul>
	);
}

/** An opened file in the canvas: 3D for meshes, rendered notes, or the source text. */
function DocView({ name, colorBy }: { name: string; colorBy: 'shape' | 'energy' }) {
	if (/\.(vtu|stl)$/.test(name)) {
		const stl = name.endsWith('.stl');
		return (
			<div className="wsd-doc wsd-doc-3d">
				<VoxelView
					cycle={CYCLES}
					colorBy={stl ? 'shape' : colorBy}
					label={`${name}, the block at design cycle ${CYCLES}. Drag to rotate.`}
				/>
				<p>
					Design cycle {CYCLES}, {FINAL_VOLUME}% of the starting volume
					{stl ? '. Watertight, 1.9 MB' : '. 3.6 MB'}
				</p>
			</div>
		);
	}
	if (name === 'design-review.md') {
		return (
			<article className="wsd-doc wsd-doc-md">
				<h4>Block design review</h4>
				<p>
					<b>Load case.</b> The left face is fixed and a 1 kN load pulls down on the far top corner.
				</p>
				<p>
					<b>Run.</b> {CYCLES} design cycles minimizing strain energy, with the volume held to{' '}
					{FINAL_VOLUME}% of the block (<code>Opt-Process-1.par</code>).
				</p>
				<p>
					<b>Shape.</b> Two chords run from the fixed face to the loaded corner, joined by diagonal
					braces (<code>topology_cycle20.vtu</code>).
				</p>
				<p>
					<b>Mesh check.</b> Going from 2 mm to 1 mm elements changes the total strain energy by 0.7%, so
					the 2 mm mesh is fine enough (Mesh convergence check).
				</p>
				<p>
					<b>Open question.</b> Whether to print the cycle {CYCLES} shape as is or smooth it first.
				</p>
			</article>
		);
	}
	const text = SOURCES[name];
	if (text) return <pre className="wsd-doc wsd-doc-code">{text}</pre>;
	return (
		<p className="wsd-doc wsd-none">
			{name} is an Abaqus results database, 182 MB. There is no preview for this file type.
		</p>
	);
}

function CanvasBody({
	panel,
	exported,
	fresh,
	onOpen,
}: {
	panel: Panel;
	exported: boolean;
	fresh: boolean;
	onOpen: (name: string) => void;
}) {
	const files = workFiles(exported);
	const title = STRIP.find((item) => item.id === panel)?.label ?? '';
	const head = (
		<div className="wsd-canvas-sub">
			<strong>{title}</strong>
			<Search size={14} aria-hidden="true" />
			<ListFilter size={14} aria-hidden="true" />
		</div>
	);
	switch (panel) {
		case 'files':
			return (
				<>
					{head}
					<FileRows files={files} note={(file) => file.size} fresh={fresh} onOpen={onOpen} />
				</>
			);
		case 'sources':
			return (
				<>
					{head}
					<FileRows
						files={files.filter((file) => !file.artifact && !file.name.endsWith('.py'))}
						note={() => 'read'}
						onOpen={onOpen}
					/>
				</>
			);
		case 'artifacts':
			return (
				<>
					{head}
					<FileRows files={files.filter((file) => file.artifact)} note={() => 'v1'} onOpen={onOpen} />
				</>
			);
		case 'activity':
			return (
				<>
					{head}
					<ol className="wsd-timeline">
						<li>
							<strong>Read the optimization task</strong>
							<small>Finished in 1s</small>
						</li>
						<li>
							<strong>Run 20 design cycles</strong>
							<small>Finished in 6m 2s</small>
						</li>
						{exported ? (
							<li>
								<strong>Export the shape</strong>
								<small>Finished in 2s</small>
							</li>
						) : null}
					</ol>
				</>
			);
		case 'agent':
			return (
				<>
					{head}
					<div className="wsd-agent">
						<strong>Scientist</strong>
						<p>Plugins in this conversation</p>
						<ul className="wsd-files">
							<li>
								<Box size={14} aria-hidden="true" />
								<span>Simulation</span>
							</li>
							<li>
								<FileText size={14} aria-hidden="true" />
								<span>Reports</span>
							</li>
						</ul>
					</div>
				</>
			);
		case 'terminal':
			return (
				<pre className="wsd-terminal">
					{`$ ${RUN}\nDesign cycle 19 completed\nDesign cycle 20 completed\nOptimization finished\n$ `}
				</pre>
			);
		case 'browser':
			return (
				<div className="wsd-browser">
					<div className="wsd-address">
						<Globe size={13} aria-hidden="true" />
						<span>Enter an address</span>
					</div>
				</div>
			);
	}
}

/**
 * The CLIO desktop app, rebuilt in HTML for the home page with the app's own
 * colours and layout. It plays one run when it scrolls into view, then every
 * control here responds the way the app does.
 */
export default function WorkspaceDemo({ mark }: { mark: string }) {
	const root = useRef<HTMLDivElement>(null);
	const transcript = useRef<HTMLDivElement>(null);
	const reduced = useReducedMotion();

	const [size, setSize] = useState<'wide' | 'mid' | 'narrow'>('wide');
	const [workspaceOpen, setWorkspaceOpen] = useState(true);
	const [active, setActive] = useState('topology');
	const [panel, setPanel] = useState<Panel | null>(null);
	const [doc, setDoc] = useState<string | null>(null);
	const [docShown, setDocShown] = useState(false);

	const [startedAt, setStartedAt] = useState(0);
	const [phase, setPhase] = useState(-1);
	const [cycle, setCycle] = useState(1);
	const [colorBy, setColorBy] = useState<'shape' | 'energy'>('shape');
	const [turns, setTurns] = useState<Turn[]>([]);
	const [exported, setExported] = useState(false);
	const [fresh, setFresh] = useState(false);
	const [dragged, setDragged] = useState(false);

	const asking = phase >= 6 && !turns.some((turn) => turn.kind === 'export');
	const pendingTurn = turns.find((turn) => !turn.done);
	const questionOpen = asking && !pendingTurn;
	const running = (phase >= 1 && phase < 5) || Boolean(pendingTurn);
	const questions = questionOpen ? 1 : 0;
	const current = START.find((c) => c.id === active);

	// Size the layout from the demo's own width, like the app's breakpoints.
	useEffect(() => {
		const el = root.current;
		if (!el) return;
		const observer = new ResizeObserver(([entry]) => {
			const width = entry.contentRect.width;
			setSize(width < 620 ? 'narrow' : width < 860 ? 'mid' : 'wide');
		});
		observer.observe(el);
		return () => observer.disconnect();
	}, []);

	// Play the opening run once the demo is on screen.
	useEffect(() => {
		const el = root.current;
		if (!el || phase !== -1) return;
		if (reduced) {
			setPhase(6);
			setCycle(CYCLES);
			return;
		}
		const observer = new IntersectionObserver(
			([entry]) => {
				if (!entry.isIntersecting) return;
				observer.disconnect();
				setStartedAt(Date.now());
				setPhase(0);
			},
			{ threshold: 0.35 },
		);
		observer.observe(el);
		return () => observer.disconnect();
	}, [reduced, phase]);

	useEffect(() => {
		if (phase < 0 || phase >= 6 || reduced) return;
		const timer = window.setTimeout(() => setPhase(phase + 1), PHASE_AT[phase + 1] - PHASE_AT[phase]);
		return () => window.clearTimeout(timer);
	}, [phase, reduced]);

	useEffect(() => {
		if (phase !== 3) return;
		const timer = window.setInterval(() => setCycle((value) => Math.min(CYCLES, value + 1)), 250);
		return () => window.clearInterval(timer);
	}, [phase]);

	// Finish whatever the latest reply is doing.
	useEffect(() => {
		if (!pendingTurn) return;
		const timer = window.setTimeout(
			() => {
				if (pendingTurn.kind === 'energy') setColorBy('energy');
				if (pendingTurn.kind === 'export') {
					setExported(true);
					setFresh(true);
					setPanel('files');
					setDocShown(false);
				}
				setTurns((list) => list.map((turn) => (turn.id === pendingTurn.id ? { ...turn, done: true } : turn)));
			},
			pendingTurn.kind === 'export' ? 1800 : 1400,
		);
		return () => window.clearTimeout(timer);
	}, [pendingTurn]);

	useEffect(() => {
		if (!fresh) return;
		const timer = window.setTimeout(() => setFresh(false), 2400);
		return () => window.clearTimeout(timer);
	}, [fresh]);

	// Follow new content to the bottom unless the reader has scrolled up.
	useEffect(() => {
		const el = transcript.current;
		const column = el?.firstElementChild;
		if (!el || !column) return;
		let pinned = true;
		const onScroll = () => {
			pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
		};
		const follow = () => {
			if (pinned) el.scrollTop = el.scrollHeight;
		};
		const observer = new ResizeObserver(follow);
		observer.observe(column);
		observer.observe(el);
		el.addEventListener('scroll', onScroll, { passive: true });
		follow();
		return () => {
			observer.disconnect();
			el.removeEventListener('scroll', onScroll);
		};
	}, [active]);

	useEffect(() => {
		const el = transcript.current;
		if (el) el.scrollTop = el.scrollHeight;
	}, [turns.length]);

	const answer = useCallback(
		(kind: 'export' | 'energy') => {
			if (!questionOpen) return;
			const text = kind === 'export' ? 'Yes, export it' : 'Not yet. Color it by strain energy first';
			setTurns((list) => [...list, { id: Date.now(), text, kind, done: false }]);
			setActive('topology');
		},
		[questionOpen],
	);

	const open = (id: string) => setActive(id);

	const openDoc = (name: string) => {
		setDoc(name);
		setDocShown(true);
	};

	const closeDoc = () => {
		setDoc(null);
		setDocShown(false);
	};

	const onKey = (event: KeyboardEvent) => {
		if (questionOpen && active === 'topology') {
			if (event.key === '1' || event.key === 'Enter') answer('export');
			if (event.key === '2') answer('energy');
		}
	};

	const showRail = size === 'mid';
	const volume = Math.round(volumeAt(cycle) * 100);
	const everyFile = workFiles(exported);
	const DocIcon = everyFile.find((file) => file.name === doc)?.icon ?? File;
	const conversationRow = (c: Conversation) => {
		const busy = c.id === 'topology' && running;
		const waiting = c.id === 'topology' && questionOpen;
		return (
			<button
				key={c.id}
				type="button"
				className="wsd-conv"
				data-indent={c.inWorkspace || undefined}
				aria-current={c.id === active ? 'true' : undefined}
				onClick={() => open(c.id)}
			>
				<span>{c.title}</span>
				{busy ? (
					<Spinner size={13} label="Working" />
				) : waiting ? (
					<BellRing size={14} className="wsd-question-ink" aria-label="Waiting for your answer" />
				) : (
					<small>{c.time}</small>
				)}
			</button>
		);
	};

	const topology = (
		<>
			{phase >= 0 ? (
				<UserMessage>
					Run the topology optimization in Opt-Process-1.par. Minimize strain energy under the volume
					constraint, stop after 20 design cycles, and show me the final shape in 3D.
				</UserMessage>
			) : null}
			{phase === 1 ? <Thinking from={startedAt} /> : null}
			{phase >= 2 ? (
				<ActivityGroup
					summary={
						phase === 2
							? 'Reading Opt-Process-1.par'
							: phase === 3
								? `Running ${RUN}`
								: phase === 4
									? 'Writing topology_cycle20.vtu'
									: 'Called 3 tools, thought once'
					}
					running={phase < 5}
					live={phase < 5 ? <Elapsed from={startedAt} /> : <span className="wsd-tool-meta">6m 4s</span>}
				>
					<ToolRow
						verb="Read"
						target="Opt-Process-1.par"
						meta="2.1 KB"
						running={phase === 2}
						output={PAR_TEXT}
					/>
					{phase >= 3 ? (
						<ToolRow
							verb="Run"
							target={RUN}
							meta={phase > 3 ? 'exit 0  6m 2s' : undefined}
							running={phase === 3}
							output={`Design cycle 18 completed\nDesign cycle 19 completed\nDesign cycle 20 completed\nOptimization finished: maximum design cycles reached`}
						/>
					) : null}
					{phase >= 4 ? (
						<ToolRow
							verb="Write"
							target="topology_cycle20.vtu"
							meta="3.6 MB"
							running={phase === 4}
							output={`Design cycle ${CYCLES} exported from Opt-Process-1-Job.odb\nPoints: 81,225  Cells: 70,400\nCell data: DESIGN_VARIABLE, STRAIN_ENERGY`}
						/>
					) : null}
				</ActivityGroup>
			) : null}
			{phase >= 3 ? (
				<figure className="wsd-result">
					<figcaption>
						<strong>
							Block, design cycle {cycle} of {CYCLES}
						</strong>
						<span>{volume}% of the starting volume</span>
					</figcaption>
					<div className="wsd-stage" onPointerDown={() => setDragged(true)}>
						<VoxelView
							cycle={cycle}
							colorBy={colorBy}
							label={`Optimized block at design cycle ${cycle}, ${volume}% of the starting volume. Drag to rotate.`}
						/>
						{phase >= 5 && !dragged ? <span className="wsd-hint">Drag to turn it</span> : null}
					</div>
					{phase >= 5 ? (
						<div className="wsd-result-controls">
							<label>
								<span>Cycle</span>
								<input
									type="range"
									min={1}
									max={CYCLES}
									value={cycle}
									onChange={(event) => setCycle(Number(event.target.value))}
									aria-label="Design cycle"
								/>
							</label>
							<div className="wsd-segmented" role="group" aria-label="Color by">
								<button type="button" aria-pressed={colorBy === 'shape'} onClick={() => setColorBy('shape')}>
									Shape
								</button>
								<button
									type="button"
									aria-pressed={colorBy === 'energy'}
									onClick={() => setColorBy('energy')}
								>
									Strain energy
								</button>
							</div>
						</div>
					) : null}
				</figure>
			) : null}
			{phase >= 5 ? (
				<>
					<Voice stream={!reduced}>
						{`All 20 design cycles ran. The material settles into two chords from the fixed face to the loaded corner, joined by diagonal braces, at ${FINAL_VOLUME}% of the starting volume.`}
					</Voice>
					<FileCard
						badge="VTU"
						name="topology_cycle20.vtu"
						kind="VTK mesh"
						size="3.6 MB"
						onOpen={() => openDoc('topology_cycle20.vtu')}
					/>
					<p className="wsd-usage">48.2K in, 1.6K out&nbsp;&nbsp;&nbsp;6m 12s</p>
				</>
			) : null}
			{turns.map((turn) => (
				<div key={turn.id} className="wsd-turn">
					<UserMessage>{turn.text}</UserMessage>
					{!turn.done ? (
						turn.kind === 'export' ? (
							<ActivityGroup summary="Writing block_cycle20.stl" running defaultOpen>
								<ToolRow verb="Write" target="block_cycle20.stl" running />
							</ActivityGroup>
						) : (
							<Thinking from={turn.id} />
						)
					) : turn.kind === 'export' ? (
						<>
							<ActivityGroup summary="Called 1 tool" live={<span className="wsd-tool-meta">2s</span>}>
								<ToolRow
									verb="Write"
									target="block_cycle20.stl"
									meta="1.9 MB"
									output={`Surface of the kept elements, design cycle ${CYCLES}\nTriangles: 38,912\nWatertight: yes`}
								/>
							</ActivityGroup>
							<Voice stream={!reduced}>
								Saved block_cycle20.stl beside the run files. It is watertight and 1.9 MB, ready for your
								slicer.
							</Voice>
							<FileCard
								badge="STL"
								name="block_cycle20.stl"
								kind="3D model"
								size="1.9 MB"
								onOpen={() => openDoc('block_cycle20.stl')}
							/>
						</>
					) : (
						<Voice stream={!reduced}>
							Colored by strain energy. Most of it sits in the upper chord next to the fixed face, where the
							bending moment is largest.
						</Voice>
					)}
				</div>
			))}
		</>
	);

	const other =
		active === 'mesh' ? (
			<>
				<UserMessage>Check that the mesh is fine enough before we trust the optimization.</UserMessage>
				<ActivityGroup summary="Called 3 tools" live={<span className="wsd-tool-meta">4m 10s</span>}>
					<ToolRow
						verb="Run"
						target="abaqus job=Block-mesh-4mm"
						meta="exit 0  48s"
						output={`Analysis of job Block-mesh-4mm completed\nElements: 8,800\nTotal strain energy: 412.6 mJ`}
					/>
					<ToolRow
						verb="Run"
						target="abaqus job=Block-mesh-2mm"
						meta="exit 0  1m 9s"
						output={`Analysis of job Block-mesh-2mm completed\nElements: 70,400\nTotal strain energy: 425.4 mJ`}
					/>
					<ToolRow
						verb="Run"
						target="abaqus job=Block-mesh-1mm"
						meta="exit 0  2m 13s"
						output={`Analysis of job Block-mesh-1mm completed\nElements: 563,200\nTotal strain energy: 428.4 mJ`}
					/>
				</ActivityGroup>
				<Voice>Going from 2 mm to 1 mm elements changes the total strain energy by less than 1%.</Voice>
				<table className="wsd-table">
					<thead>
						<tr>
							<th>Element size</th>
							<th>Elements</th>
							<th>Change</th>
						</tr>
					</thead>
					<tbody>
						<tr>
							<td>4 mm</td>
							<td>8,800</td>
							<td></td>
						</tr>
						<tr>
							<td>2 mm</td>
							<td>70,400</td>
							<td>3.1%</td>
						</tr>
						<tr>
							<td>1 mm</td>
							<td>563,200</td>
							<td>0.7%</td>
						</tr>
					</tbody>
				</table>
				<p className="wsd-usage">22.9K in, 840 out&nbsp;&nbsp;&nbsp;4m 31s</p>
			</>
		) : active === 'review' ? (
			<>
				<UserMessage>Write up what changed in the block for Thursday's design review.</UserMessage>
				<ActivityGroup summary="Called 5 tools" live={<span className="wsd-tool-meta">14s</span>}>
					<ToolRow verb="Read" target="Opt-Process-1.par" meta="2.1 KB" output={PAR_TEXT} />
					<ToolRow
						verb="Read"
						target="topology_cycle20.vtu"
						meta="3.6 MB"
						output={`UnstructuredGrid, design cycle ${CYCLES}\nPoints: 81,225  Cells: 70,400\nCell data: DESIGN_VARIABLE, STRAIN_ENERGY\nElements kept: ${FINAL_VOLUME}% of the starting volume`}
					/>
					<ToolRow
						verb="Read"
						target="Opt-Process-1-Job.odb"
						meta="182 MB"
						output={`Steps: Load\nFrames per design cycle: 1\nDesign cycles: ${CYCLES}\nStrain energy, cycle 1: 425.4 mJ\nStrain energy, cycle ${CYCLES}: 611.8 mJ`}
					/>
					<ToolRow
						verb="Read"
						target="Mesh convergence check"
						meta="conversation"
						output={`4 mm  8,800 elements  412.6 mJ\n2 mm  70,400 elements  425.4 mJ  (3.1%)\n1 mm  563,200 elements  428.4 mJ  (0.7%)`}
					/>
					<ToolRow
						verb="Write"
						target="design-review.md"
						meta="1.2 KB"
						output={`# Block design review\n\nLoad case, run, shape, mesh check and one open question,\neach linked to the file it relies on.`}
					/>
				</ActivityGroup>
				<Voice>
					The notes cover the load case, the 20 cycle run, the final shape and the mesh check, with a link back
					to each file they rely on.
				</Voice>
				<FileCard
					badge="MD"
					name="design-review.md"
					kind="Markdown"
					size="1.2 KB"
					onOpen={() => openDoc('design-review.md')}
				/>
			</>
		) : null;

	return (
		<div
			ref={root}
			className="wsd"
			data-size={size}
			data-rail={showRail || undefined}
			data-panel={panel ?? undefined}
			onKeyDown={onKey}
		>
			{size !== 'narrow' ? (
				<aside className="wsd-side" aria-label="Sidebar">
					{showRail ? (
						<div className="wsd-rail">
							<img src={mark} alt="CLIO" width={26} height={26} />
							<span className="wsd-bell" aria-hidden="true">
								<Bell size={17} />
								{questions ? <span>{questions}</span> : null}
							</span>
							<span className="wsd-primary-ink" aria-hidden="true">
								<SquarePen size={17} />
							</span>
							<span aria-hidden="true">
								<Search size={17} />
							</span>
							<button type="button" className="wsd-primary-ink" aria-label="Topology study" onClick={() => open('topology')}>
								<Atom size={17} aria-hidden="true" />
							</button>
							<span className="wsd-rail-end">
								<Archive size={16} aria-hidden="true" />
								<Rows2 size={16} aria-hidden="true" />
								<SlidersHorizontal size={16} aria-hidden="true" />
							</span>
						</div>
					) : (
						<>
							<div className="wsd-brand">
								<img src={mark} alt="" width={30} height={30} />
								<div>
									<strong>
										CLIO <ChevronsUpDown size={13} aria-hidden="true" />
									</strong>
									<small>
										<i aria-hidden="true" /> Agent @ this computer
									</small>
								</div>
								<span className="wsd-bell" aria-hidden="true">
									<Bell size={17} />
									{questions ? <span>{questions}</span> : null}
								</span>
								<span aria-hidden="true">
									<PanelLeft size={17} />
								</span>
							</div>
							<div className="wsd-nav">
								<span>
									<SquarePen size={16} className="wsd-primary-ink" aria-hidden="true" />
									<span>New conversation</span>
									<Upload size={15} className="wsd-faint" aria-hidden="true" />
								</span>
								<span>
									<Search size={16} aria-hidden="true" />
									<span>Search</span>
									<small>Ctrl K</small>
								</span>
							</div>
							<div className="wsd-section">
								<div className="wsd-label">
									Workspaces <Plus size={14} aria-hidden="true" />
								</div>
								<button
									type="button"
									className="wsd-workspace"
									aria-expanded={workspaceOpen}
									onClick={() => setWorkspaceOpen((value) => !value)}
								>
									{workspaceOpen ? (
										<ChevronDown size={14} aria-hidden="true" />
									) : (
										<ChevronRight size={14} aria-hidden="true" />
									)}
									<Atom size={15} className="wsd-primary-ink" aria-hidden="true" />
									<strong>Topology study</strong>
								</button>
								{workspaceOpen ? START.filter((c) => c.inWorkspace).map(conversationRow) : null}
							</div>
							<div className="wsd-section">
								<div className="wsd-label">
									Conversations
									<span>
										<ListFilter size={14} aria-hidden="true" />
										<SlidersHorizontal size={14} aria-hidden="true" />
									</span>
								</div>
								{START.filter((c) => !c.inWorkspace).length ? (
									START.filter((c) => !c.inWorkspace).map(conversationRow)
								) : (
									<span className="wsd-empty">
										<Inbox size={14} aria-hidden="true" /> No conversations yet
									</span>
								)}
							</div>
							<div className="wsd-profile">
								<span className="wsd-avatar">
									<User size={15} aria-hidden="true" />
								</span>
								<div>
									<span>Your profile</span>
									<small>Local profile</small>
								</div>
								<Archive size={16} aria-hidden="true" />
								<Rows2 size={16} aria-hidden="true" />
								<SlidersHorizontal size={16} aria-hidden="true" />
							</div>
						</>
					)}
				</aside>
			) : null}

			<section className="wsd-main" aria-label="Conversation">
				<header className="wsd-header">
					{showRail ? (
						<span aria-hidden="true">
							<PanelLeft size={16} />
						</span>
					) : null}
					<span className="wsd-crumb">
						{current?.inWorkspace ? (
							<>
								<Atom size={15} className="wsd-primary-ink" aria-hidden="true" />
								<span className="wsd-crumb-ws">Topology study</span>
							</>
						) : (
							<span className="wsd-crumb-ws">Conversations</span>
						)}
						<ChevronRight size={14} aria-hidden="true" />
						<span className="wsd-crumb-title">
							<span>{current?.title}</span>
							<ChevronDown size={14} aria-hidden="true" />
						</span>
					</span>
					<span className="wsd-header-tools">
						{questions ? (
							<button
								type="button"
								className="wsd-qcount"
								aria-label={`${questions} open question`}
								onClick={() => open('topology')}
							>
								<CircleHelp size={15} aria-hidden="true" /> {questions}
							</button>
						) : null}
						<button
							type="button"
							aria-label="Workspace canvas"
							aria-pressed={panel !== null}
							onClick={() => setPanel((value) => (value ? null : 'files'))}
						>
							<PanelRight size={16} aria-hidden="true" />
						</button>
						<span aria-hidden="true">
							<Columns2 size={16} />
						</span>
						<span aria-hidden="true">
							<MoreHorizontal size={16} />
						</span>
					</span>
				</header>

				<div className="wsd-body">
					<span className="wsd-outline" aria-hidden="true">
						<ListTree size={14} />
					</span>
					<div className="wsd-transcript" ref={transcript}>
						<div className="wsd-column">
							{active === 'topology' ? topology : other}
						</div>
					</div>

					<div className="wsd-dock">
						{questionOpen && active === 'topology' ? (
							<div className="wsd-question" role="group" aria-label="CLIO asks a question">
								<div className="wsd-question-title">
									<CircleHelp size={17} aria-hidden="true" />
									<strong>{QUESTION}</strong>
									<span className="wsd-pager">
										<ChevronLeft size={14} aria-hidden="true" /> 1/1 <ChevronRight size={14} aria-hidden="true" />
									</span>
								</div>
								<button type="button" className="wsd-option" onClick={() => answer('export')}>
									<kbd>1</kbd>
									<span>Yes, export it</span>
									<CornerDownLeft size={12} className="wsd-enter" aria-hidden="true" />
								</button>
								<button type="button" className="wsd-option" onClick={() => answer('energy')}>
									<kbd>2</kbd>
									<span>Not yet. Color it by strain energy first</span>
									<CornerDownLeft size={12} className="wsd-enter" aria-hidden="true" />
								</button>
							</div>
						) : null}
						<div className="wsd-composer" aria-hidden="true">
							<div className="wsd-composer-input">Reply</div>
							<div className="wsd-composer-tools">
								<span>
									<Plus size={16} />
								</span>
								<span>
									<AtSign size={16} />
								</span>
								<span>
									<CircleHelp size={16} />
								</span>
								<span>
									<Play size={16} />
								</span>
								<ModelChip animate={!reduced} />
								<span className="wsd-send">
									<ArrowUp size={15} />
								</span>
							</div>
						</div>
					</div>
				</div>
			</section>

			{(panel && size !== 'narrow') || doc ? (
				<section className="wsd-canvas" data-doc={(doc && (docShown || !panel)) || undefined} aria-label="Workspace canvas">
					<header>
						<span className="wsd-tabs">
							{panel ? (
								<span className="wsd-tab" data-active={!(doc && docShown) || undefined}>
									<button type="button" className="wsd-tab-name" onClick={() => setDocShown(false)}>
										{(() => {
											const Icon = STRIP.find((item) => item.id === panel)?.icon ?? Folder;
											return <Icon size={15} aria-hidden="true" />;
										})()}
										{STRIP.find((item) => item.id === panel)?.label}
									</button>
									<button type="button" aria-label="Close the panel" onClick={() => setPanel(null)}>
										<X size={13} aria-hidden="true" />
									</button>
								</span>
							) : null}
							{doc ? (
								<span className="wsd-tab" data-active={docShown || !panel || undefined}>
									<button type="button" className="wsd-tab-name" onClick={() => setDocShown(true)}>
										<DocIcon size={15} aria-hidden="true" />
										<span>{doc}</span>
									</button>
									<button type="button" aria-label={`Close ${doc}`} onClick={closeDoc}>
										<X size={13} aria-hidden="true" />
									</button>
								</span>
							) : null}
						</span>
						<Maximize2 size={14} className="wsd-faint" aria-hidden="true" />
					</header>
					{doc && (docShown || !panel) ? (
						<DocView key={doc} name={doc} colorBy={colorBy} />
					) : panel ? (
						<CanvasBody panel={panel} exported={exported} fresh={fresh} onOpen={openDoc} />
					) : null}
				</section>
			) : null}

			{size !== 'narrow' ? (
				<nav className="wsd-strip" aria-hidden="true">
					{STRIP.map(({ id, label, icon: Icon }, index) => (
						<span
							key={id}
							title={label}
							data-pressed={(panel === id && !(doc && docShown)) || undefined}
							data-split={index === 5 || undefined}
							data-fresh={(fresh && id === 'files') || undefined}
						>
							<Icon size={17} aria-hidden="true" />
						</span>
					))}
				</nav>
			) : null}

		</div>
	);
}
