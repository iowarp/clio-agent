import { ArrowRight, ChartNoAxesCombined, FileText, Layers } from 'lucide-react';
import { useState } from 'react';
import Capture, { type CaptureImage } from './Capture';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

export interface ShowcaseItem {
	id: 'figures' | 'documents' | 'evidence';
	label: string;
	title: string;
	description: string;
	href: string;
	link: string;
	image: CaptureImage;
	alt: string;
	caption: string;
}

const icons = { figures: ChartNoAxesCombined, documents: FileText, evidence: Layers };

/** Real product captures with keyboard-accessible tabs and full-size viewing. */
export default function Showcase({ items }: { items: ShowcaseItem[] }) {
	const [active, setActive] = useState('documents');
	return (
		<Tabs value={active} onValueChange={(value) => setActive(String(value))} className="product-showcase">
			<TabsList aria-label="Explore the CLIO workspace" activateOnFocus variant="line" className="showcase-tabs">
				{items.map(({ id, label }) => {
					const Icon = icons[id];
					return <TabsTrigger key={id} value={id}><Icon aria-hidden="true" />{label}</TabsTrigger>;
				})}
			</TabsList>
			{items.map((item) => (
				<TabsContent key={item.id} value={item.id} aria-hidden={active !== item.id} inert={active !== item.id}>
					<div className="showcase-copy">
						<div>
							<h2>{item.title}</h2>
							<p>{item.description}</p>
						</div>
						<a href={item.href}>{item.link}<ArrowRight aria-hidden="true" size={16} /></a>
					</div>
					<Capture image={item.image} alt={item.alt} caption={item.caption} priority={item.id === 'documents'} sizes="(min-width: 80rem) 76rem, 100vw" />
				</TabsContent>
			))}
		</Tabs>
	);
}
