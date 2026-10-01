import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import { faq } from '@/data/overview';

/** Frequently asked questions, as a shadcn accordion. */
export default function Faq() {
	return (
		<Accordion className="border-t border-border">
			{faq.map((item) => (
				<AccordionItem key={item.q} value={item.q} className="border-b border-border">
					<AccordionTrigger className="py-5 text-left text-base font-medium text-foreground hover:no-underline">
						{item.q}
					</AccordionTrigger>
					<AccordionContent className="pb-5 text-base leading-relaxed text-ink-soft">{item.a}</AccordionContent>
				</AccordionItem>
			))}
		</Accordion>
	);
}
