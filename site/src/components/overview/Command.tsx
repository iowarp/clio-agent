import { CodeBlock, CodeBlockCopyButton } from '@/components/reui/code-block/code-block';

interface Props {
	command: string;
	language?: string;
	label?: string;
}

/** A shell command in a reui CodeBlock with a copy button. */
export default function Command({ command, language = 'bash', label }: Props) {
	return (
		<CodeBlock code={command} language={language} label={label} highlight={false} wrap className="text-sm">
			<CodeBlockCopyButton alwaysVisible labels={{ copy: 'Copy command', copied: 'Copied' }} />
		</CodeBlock>
	);
}
