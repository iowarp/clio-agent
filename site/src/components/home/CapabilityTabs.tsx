import { useState } from 'react';
import { ArrowRight, Atom, Boxes, FlaskConical, GitBranch, MousePointerClick, Radio, ScrollText, SlidersHorizontal } from 'lucide-react';
import LoopVideo from './LoopVideo';

interface Capability {
	id: string;
	tab: string;
	icon: typeof Atom;
	title: string;
	body: string;
	link: { href: string; text: string };
	height: number;
}

export const CAPABILITIES: readonly Capability[] = [
	{
		id: 'selection',
		tab: 'Ask your data',
		icon: MousePointerClick,
		title: 'Ask your data',
		body: 'Select observations on a map, chart or table and ask about exactly those records. CLIO reads them, works out the answer and shows which records it used.',
		link: { href: '/docs/dashboards/', text: 'Working with results' },
		height: 528,
	},
	{
		id: 'interface',
		tab: 'Interactive results',
		icon: SlidersHorizontal,
		title: 'The answer is an interface',
		body: 'Results arrive as charts, tables and maps you can change. Move a control and every linked view follows, then export the view you settled on.',
		link: { href: '/docs/dashboards/', text: 'Dashboards and views' },
		height: 536,
	},
	{
		id: 'provenance',
		tab: 'Where it came from',
		icon: ScrollText,
		title: 'Show me where that came from',
		body: 'Follow a number back through the calculation that produced it to the dataset it started from, then return to the result without losing your place.',
		link: { href: '/docs/evidence/', text: 'Evidence and sources' },
		height: 554,
	},
	{
		id: 'hypothesis',
		tab: 'Changing a claim',
		icon: FlaskConical,
		title: 'What if we’re wrong?',
		body: 'When a result disagrees with what you expected, CLIO keeps the evidence and the revised claim together in the science log, so anyone can see why a conclusion changed.',
		link: { href: '/docs/science-log/', text: 'The science log' },
		height: 516,
	},
	{
		id: 'method',
		tab: 'Your method',
		icon: Atom,
		title: 'Bring your method',
		body: 'Write the procedure the way your lab already does. CLIO turns it into steps it can run, has the result reviewed and goes back to the step that needs fixing.',
		link: { href: '/docs/flows/', text: 'Flows' },
		height: 528,
	},
	{
		id: 'team',
		tab: 'Specialists',
		icon: GitBranch,
		title: 'Workflows as complex as your science',
		body: 'Several specialist agents work at once, and a follow-up starts only when the evidence calls for it. The whole run stays in one graph you can open and inspect.',
		link: { href: '/docs/subagents/', text: 'Subagents' },
		height: 528,
	},
	{
		id: 'tools',
		tab: 'Your tools',
		icon: Boxes,
		title: 'Your tools, your sources, your science',
		body: 'Connect the programs and data sources you already use. CLIO can read them together in one step and return a summary that links back to each source.',
		link: { href: '/docs/mcp-servers/', text: 'Connect tools' },
		height: 476,
	},
	{
		id: 'streams',
		tab: 'Live experiments',
		icon: Radio,
		title: 'Tap into your experiment',
		body: 'Follow an instrument, a log or a running simulation. Ask about the latest observations while the data keeps arriving and its dashboard keeps updating.',
		link: { href: '/docs/live-sources/', text: 'Live data' },
		height: 570,
	},
];

/** One capability at a time: its clip on the left, what it does on the right. */
export default function CapabilityTabs() {
	const [active, setActive] = useState(0);
	const item = CAPABILITIES[active];
	return (
		<div className="ct">
			<div className="ct-tabs" role="tablist" aria-label="What CLIO can do">
				{CAPABILITIES.map((capability, index) => {
					const Icon = capability.icon;
					return (
						<button
							key={capability.id}
							type="button"
							role="tab"
							id={`ct-tab-${capability.id}`}
							aria-selected={index === active}
							aria-controls="ct-panel"
							tabIndex={index === active ? 0 : -1}
							onClick={() => setActive(index)}
							onKeyDown={(event) => {
								const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
								if (!step) return;
								event.preventDefault();
								const next = (active + step + CAPABILITIES.length) % CAPABILITIES.length;
								setActive(next);
								document.getElementById(`ct-tab-${CAPABILITIES[next].id}`)?.focus();
							}}
						>
							<Icon size={16} aria-hidden="true" />
							{capability.tab}
						</button>
					);
				})}
			</div>
			<div className="ct-panel" id="ct-panel" role="tabpanel" aria-labelledby={`ct-tab-${item.id}`}>
				<div className="ct-stage">
					{CAPABILITIES.map((capability, index) => {
						const distance = Math.abs(index - active);
						return (
							<LoopVideo
								key={capability.id}
								src={`/media/home/cap-${capability.id}.mp4`}
								poster={`/media/home/cap-${capability.id}.webp`}
								width={1280}
								height={capability.height}
								label={`${capability.title}: a short clip of CLIO`}
								className="ct-video"
								active={index === active}
								warm={distance === 1 || distance === CAPABILITIES.length - 1}
							/>
						);
					})}
				</div>
				<div className="ct-copy">
					<h3>{item.title}</h3>
					<p>{item.body}</p>
					<a href={item.link.href}>
						{item.link.text} <ArrowRight size={16} aria-hidden="true" />
					</a>
				</div>
			</div>
		</div>
	);
}
