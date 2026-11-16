#!/usr/bin/env node
// Export the docs as plain Markdown, for agents to read.
//
// Every page under src/content/docs/docs/ becomes one Markdown file: its frontmatter
// title and description kept, imports dropped, the Starlight and site components
// rendered to Markdown, links rewritten. Two targets share one rendering:
//
//   --target guide (default): the guide plugin's bundled docs. Pages go to
//       src/clio_agent/plugins/builtin/guide/skills/clio/docs/<path>.md, the map to
//       .../skills/clio/docs-map.md, and each capture a page shows is copied to
//       .../guide/assets/captures/<name>.webp at the width the site serves. Links to
//       other pages point at bundled paths (docs/<path>.md); images use
//       plugin-asset://guide/captures/<name>.webp, which CLIO resolves offline.
//   --target site --out <dir>: the same pages for the published site at
//       /docs/<path>.md, with absolute links and image URLs, plus llms.txt listing
//       every page. The site build runs it into dist/.
//
//   --check: write nothing; exit 1 when the committed guide export differs from a
//       fresh one (CI runs this).
//
// A component this script does not know fails the export: nothing is dropped silently.

import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { sidebar } from '../src/sidebar.mjs';
import { widgetUses } from '../src/data/widget-library.mjs';

const SITE = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = resolve(SITE, '..');
const DOCS = join(SITE, 'src/content/docs/docs');
const ORIGIN = 'https://clio.iowarp.ai';
const GUIDE = join(REPO, 'src/clio_agent/plugins/builtin/guide');
const SKILL = join(GUIDE, 'skills/clio');
/** The width the site serves a capture at in a page (its middle responsive width). */
const CAPTURE_WIDTH = 800;
const CAPTURE_QUALITY = 74;

// ---------------------------------------------------------------- arguments

const argv = process.argv.slice(2);
const option = (name, fallback) => {
	const at = argv.indexOf(name);
	return at >= 0 ? argv[at + 1] : fallback;
};
const TARGET = option('--target', 'guide');
const CHECK = argv.includes('--check');
if (!['guide', 'site'].includes(TARGET)) throw new Error(`unknown --target ${TARGET}`);

// ---------------------------------------------------------------- pages

/** Every page file under docs/, as { file, path } where path is the export path without .md. */
function pages() {
	const found = [];
	const walk = (dir) => {
		for (const name of readdirSync(dir).sort()) {
			const full = join(dir, name);
			if (statSync(full).isDirectory()) walk(full);
			else if (/\.mdx?$/.test(name)) {
				const rel = relative(DOCS, full).replace(/\\/g, '/').replace(/\.mdx?$/, '');
				found.push({ file: full, path: rel });
			}
		}
	};
	walk(DOCS);
	return found;
}

/** The site slug of an export path ("files/index" -> "docs/files"). */
function slugOf(path) {
	const bare = path === 'index' ? '' : path.replace(/\/index$/, '');
	return bare ? `docs/${bare}` : 'docs';
}

/** Frontmatter: the scalar keys, enough for title and description. */
function frontmatter(text) {
	const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
	if (!match) return { data: {}, body: text };
	const data = {};
	const lines = match[1].split(/\r?\n/);
	for (let i = 0; i < lines.length; i += 1) {
		const kv = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(lines[i]);
		if (!kv) continue;
		let value = kv[2];
		if (value === '>' || value === '|' || value === '>-' || value === '|-') {
			const block = [];
			while (i + 1 < lines.length && /^\s+/.test(lines[i + 1])) block.push(lines[(i += 1)].trim());
			value = block.join(value.startsWith('>') ? ' ' : '\n');
		} else if (/^".*"$/.test(value)) value = JSON.parse(value);
		else if (/^'.*'$/.test(value)) value = value.slice(1, -1).replace(/''/g, "'");
		data[kv[1]] = value;
	}
	return { data, body: text.slice(match[0].length) };
}

// ---------------------------------------------------------------- MDX parsing

/**
 * Replace each fenced code block with a one-line placeholder, so components and links in
 * prose are rendered while code stays verbatim. Returns [text, restore].
 */
function protectFences(text) {
	const blocks = [];
	const lines = text.split('\n');
	const out = [];
	for (let i = 0; i < lines.length; i += 1) {
		const fence = /^([\t ]*)(```+|~~~+)/.exec(lines[i]);
		if (!fence) {
			out.push(lines[i]);
			continue;
		}
		const indent = fence[1];
		const code = [lines[i].slice(indent.length)];
		while (i + 1 < lines.length) {
			i += 1;
			code.push(lines[i].startsWith(indent) ? lines[i].slice(indent.length) : lines[i].trimStart());
			if (lines[i].trim().startsWith(fence[2])) break;
		}
		out.push(`${indent}\u0000FENCE${blocks.length}\u0000`);
		blocks.push(code.join('\n'));
	}
	const restore = (rendered) =>
		rendered.replace(/^(.*?)\u0000FENCE(\d+)\u0000/gm, (_, prefix, n) =>
			blocks[Number(n)].split('\n').map((l, k) => (k === 0 ? prefix + l : (prefix.replace(/[^\t >]/g, ' ') + l).trimEnd() || prefix.trimEnd())).join('\n'),
		);
	return [out.join('\n'), restore];
}

/** Read one JSX attribute value starting at s[i] ("...", '...' or {...}); returns [value, end]. */
function attrValue(s, i) {
	const q = s[i];
	if (q === '"' || q === "'") {
		const end = s.indexOf(q, i + 1);
		return [s.slice(i + 1, end), end + 1];
	}
	if (q === '{') {
		let depth = 0;
		let quote = '';
		for (let j = i; j < s.length; j += 1) {
			const c = s[j];
			if (quote) {
				if (c === '\\') j += 1;
				else if (c === quote) quote = '';
				continue;
			}
			if (c === '"' || c === "'" || c === '`') quote = c;
			else if (c === '{') depth += 1;
			else if (c === '}') {
				depth -= 1;
				if (depth === 0) return [{ expr: s.slice(i + 1, j).trim() }, j + 1];
			}
		}
		throw new Error(`unclosed {…} attribute near: ${s.slice(i, i + 60)}`);
	}
	throw new Error(`bad attribute value near: ${s.slice(i, i + 60)}`);
}

/** Parse an opening tag at s[i] ("<Name ..." ); returns { name, attrs, selfClosing, end }. */
function openTag(s, i) {
	const name = /^<([A-Z][\w.]*)/.exec(s.slice(i))[1];
	let j = i + 1 + name.length;
	const attrs = {};
	for (;;) {
		while (/\s/.test(s[j])) j += 1;
		if (s.startsWith('/>', j)) return { name, attrs, selfClosing: true, end: j + 2 };
		if (s[j] === '>') return { name, attrs, selfClosing: false, end: j + 1 };
		const key = /^[\w:.-]+/.exec(s.slice(j));
		if (!key) throw new Error(`cannot read <${name}> attributes near: ${s.slice(j, j + 60)}`);
		j += key[0].length;
		if (s[j] === '=') {
			const [value, end] = attrValue(s, j + 1);
			attrs[key[0]] = value;
			j = end;
		} else attrs[key[0]] = true;
	}
}

/** The end of <Name>'s children: index of the matching </Name> (nesting aware). */
function closeTag(s, from, name) {
	let depth = 1;
	const re = new RegExp(`<(/?)${name.replace('.', '\\.')}(?=[\\s/>])`, 'g');
	re.lastIndex = from;
	for (let m = re.exec(s); m; m = re.exec(s)) {
		if (m[1]) {
			depth -= 1;
			if (depth === 0) return m.index;
		} else {
			const tag = openTag(s, m.index);
			if (!tag.selfClosing) depth += 1;
			re.lastIndex = tag.end;
		}
	}
	throw new Error(`<${name}> is never closed`);
}

function dedent(text) {
	const lines = text.split('\n');
	const indents = lines.filter((l) => l.trim()).map((l) => /^[\t ]*/.exec(l)[0].length);
	const cut = indents.length ? Math.min(...indents) : 0;
	return lines.map((l) => l.slice(cut)).join('\n').trim();
}

const quote = (text) => text.split('\n').map((l) => (l.trim() ? `> ${l}` : '>')).join('\n');

// ---------------------------------------------------------------- rendering

class Page {
	constructor(entry, all) {
		this.entry = entry;
		this.all = all;
		this.captures = [];
		this.imports = {};
	}

	/** Where a link to the site points from this export. */
	link(href) {
		if (!href.startsWith('/') || href.startsWith('//')) return href;
		const [pathPart, hash = ''] = href.split('#');
		const anchor = hash ? `#${hash}` : '';
		const trimmed = pathPart.replace(/\/+$/, '').replace(/^\//, '');
		if (trimmed === 'docs' || trimmed.startsWith('docs/')) {
			const rel = trimmed === 'docs' ? '' : trimmed.slice(5);
			const target = this.all.find((p) => slugOf(p.path) === (rel ? `docs/${rel}` : 'docs'));
			if (target) {
				return TARGET === 'guide' ? `docs/${target.path}.md${anchor}` : `${ORIGIN}/docs/${target.path}.md${anchor}`;
			}
		}
		return `${ORIGIN}${href}`;
	}

	/** A capture image: register it for copying and return its reference. */
	image(source, alt, caption) {
		const name = source.split('/').pop().replace(/\.(png|jpe?g|webp)$/i, '');
		if (!this.captures.some((c) => c.name === name)) this.captures.push({ name, source });
		const ref = TARGET === 'guide' ? `plugin-asset://guide/captures/${name}.webp` : `${ORIGIN}/docs-captures/${name}.webp`;
		const cap = caption ? `\n*${caption.trim()}*` : '';
		return `![${(alt || '').replace(/\s+/g, ' ').trim()}](${ref})${cap}`;
	}

	component(name, attrs, children) {
		const str = (key) => (typeof attrs[key] === 'string' ? attrs[key] : '');
		switch (name) {
			case 'Aside': {
				const kind = { note: 'Note', tip: 'Tip', caution: 'Caution', danger: 'Danger' }[str('type') || 'note'] || 'Note';
				const title = str('title');
				return quote(`**${kind}${title ? `: ${title}` : ''}**\n\n${this.render(dedent(children))}`);
			}
			case 'Steps':
			case 'CardGrid':
			case 'Tabs':
				return this.render(dedent(children));
			case 'TabItem':
				return `**${str('label')}**\n\n${this.render(dedent(children))}`;
			case 'FileTree':
				return '```text\n' + dedent(children).replace(/^(\s*)- /gm, '$1') + '\n```';
			case 'LinkCard': {
				const description = str('description');
				return `- [${str('title')}](${this.link(str('href'))})${description ? `: ${description}` : ''}`;
			}
			case 'Capture': {
				const ident = attrs.image && attrs.image.expr && /captureImage\((\w+)\)/.exec(attrs.image.expr);
				const source = ident && this.imports[ident[1]];
				if (!source) throw new Error(`${this.entry.path}: <Capture> image ${JSON.stringify(attrs.image)} is not an imported capture`);
				return this.image(source, str('alt'), str('caption'));
			}
			case 'GuideScreenshot':
				return this.image(`public/media/${str('name')}.${str('format') || 'jpg'}`, str('alt'), str('caption'));
			case 'PreviewGuide': {
				const since = str('since');
				return quote(`**Preview:** ${str('features')} ${since ? `(since ${since})` : '(in development builds)'}. Availability depends on your build.`);
			}
			case 'TasksPreview':
				return quote('**Development preview:** these guides describe the task updates under review. Availability depends on your build.');
			case 'WidgetDetail': {
				const uses = widgetUses[str('name')] || '';
				return `Component \`${str('name')}\`. ${uses}\n\nThe site shows a live example, its fields and the agent's skill for it.`;
			}
			case 'WidgetRecording':
				return `*Recording on the site: ${str('title')}.* ${str('caption')}`.trim();
			case 'WidgetLive':
			case 'QuestionGallery':
			case 'ConnectionDiagram':
				return '*An interactive example is shown on the site version of this page.*';
			// Navigation and branding that the docs map and headings already carry.
			case 'FilesGuideNav':
			case 'TasksGuideNav':
			case 'WidgetSectionTabs':
			case 'NdpBrand':
				return '';
			default:
				throw new Error(`${this.entry.path}: unknown component <${name}>; teach export-docs-markdown.mjs to render it`);
		}
	}

	/** Render one prose region (no fences inside except those children carry). */
	render(text) {
		let out = '';
		let i = 0;
		for (;;) {
			const at = text.slice(i).search(/<[A-Z]/);
			if (at < 0) {
				out += text.slice(i);
				break;
			}
			out += text.slice(i, i + at);
			const tag = openTag(text, i + at);
			let children = '';
			let end = tag.end;
			if (!tag.selfClosing) {
				const close = closeTag(text, tag.end, tag.name);
				children = text.slice(tag.end, close);
				end = text.indexOf('>', close) + 1;
			}
			out += this.component(tag.name, tag.attrs, children);
			i = end;
		}
		return out;
	}

	prose(text) {
		let t = text.replace(/\{\/\*[\s\S]*?\*\/\}/g, '');
		t = this.render(t);
		// Markdown links and raw HTML hrefs to site pages.
		t = t.replace(/\]\((\/[^)\s]*)\)/g, (_, href) => `](${this.link(href)})`);
		t = t.replace(/href="(\/[^"]*)"/g, (_, href) => `href="${this.link(href)}"`);
		return t;
	}

	export() {
		const raw = readFileSync(this.entry.file, 'utf8');
		const { data, body } = frontmatter(raw);
		this.title = data.title || this.entry.path;
		this.description = data.description || '';
		// Imports: remember image imports, then drop every import statement.
		const importRe = /^import\s+([\s\S]*?)\s+from\s+["']([^"']+)["'];?[ \t]*$/gm;
		for (const m of body.matchAll(importRe)) {
			if (/\.(png|jpe?g|webp)$/i.test(m[2]) && /^\w+$/.test(m[1].trim())) {
				this.imports[m[1].trim()] = resolve(dirname(this.entry.file), m[2]);
			}
		}
		const stripped = body.replace(importRe, '');
		const [prose, restore] = protectFences(stripped);
		const rendered = restore(this.prose(prose)).replace(/\n{3,}/g, '\n\n').trim();
		const site = `${ORIGIN}/${slugOf(this.entry.path)}/`;
		const header = [
			'---',
			`title: ${JSON.stringify(this.title)}`,
			...(this.description ? [`description: ${JSON.stringify(this.description)}`] : []),
			`source: ${site}`,
			'---',
			'',
			`# ${this.title}`,
			'',
			...(this.description ? [this.description, ''] : []),
		];
		return `${header.join("\n")}\n${rendered}\n`;
	}
}

// ---------------------------------------------------------------- the map

function groups(all) {
	const bySlug = new Map(all.map((p) => [slugOf(p.path), p]));
	const used = new Set();
	const out = [];
	const visit = (items, label, rows) => {
		for (const item of items) {
			if (item.items) visit(item.items, label, rows);
			else if (item.slug && bySlug.has(item.slug)) {
				used.add(item.slug);
				rows.push({ page: bySlug.get(item.slug), label: item.label });
			}
		}
	};
	for (const group of sidebar) {
		const rows = [];
		visit(group.items || [], group.label, rows);
		if (rows.length) out.push({ label: group.label, rows });
	}
	const rest = all.filter((p) => !used.has(slugOf(p.path))).map((page) => ({ page, label: '' }));
	if (rest.length) out.push({ label: 'Other pages', rows: rest });
	return out;
}

function docsMap(all, meta) {
	const lines = [
		'# CLIO documentation map',
		'',
		'One line per page of the CLIO docs that ship with this version: its path, title and',
		'description, grouped as the docs sidebar groups them. Load a page with',
		'`load_skill("clio", files=["docs/<path>.md"])`. The site address of a page is',
		`${ORIGIN}/docs/<path>/ (an \`index\` page is its folder).`,
		'',
	];
	for (const group of groups(all)) {
		lines.push(`## ${group.label}`, '');
		for (const { page } of group.rows) {
			const m = meta.get(page.path);
			lines.push(`- \`docs/${page.path}.md\`: **${m.title}**${m.description ? `. ${m.description}` : ''}`);
		}
		lines.push('');
	}
	return `${lines.join('\n').trimEnd()}\n`;
}

function llms(all, meta) {
	const head = readFileSync(join(SITE, 'public/llms.txt'), 'utf8').trimEnd();
	const lines = [head, '', '## Every docs page as Markdown', ''];
	for (const group of groups(all)) {
		lines.push(`### ${group.label}`, '');
		for (const { page } of group.rows) {
			const m = meta.get(page.path);
			lines.push(`- [${m.title}](${ORIGIN}/docs/${page.path}.md)${m.description ? `: ${m.description}` : ''}`);
		}
		lines.push('');
	}
	return `${lines.join('\n').trimEnd()}\n`;
}

// ---------------------------------------------------------------- output

async function webp(source) {
	const { default: sharp } = await import('sharp');
	const image = sharp(source);
	const { width } = await image.metadata();
	return image
		.resize({ width: Math.min(width, CAPTURE_WIDTH), withoutEnlargement: true })
		.webp({ quality: CAPTURE_QUALITY, effort: 6 })
		.toBuffer();
}

/** Capture conversion is the slow part: reuse a WebP whose source hash is recorded. */
function captureIndexPath(dir) {
	return join(dir, 'sources.json');
}

async function main() {
	const all = pages();
	const files = new Map(); // relative output path -> string | Buffer
	const meta = new Map();
	const captures = new Map();
	for (const entry of all) {
		const page = new Page(entry, all);
		const text = page.export();
		meta.set(entry.path, { title: page.title, description: page.description });
		files.set(`docs/${entry.path}.md`, text);
		for (const capture of page.captures) {
			const source = capture.source.startsWith('public/') ? join(SITE, capture.source) : capture.source;
			if (!existsSync(source)) throw new Error(`${entry.path}: capture ${source} does not exist`);
			captures.set(capture.name, source);
		}
	}

	if (TARGET === 'site') {
		const out = resolve(option('--out', join(SITE, 'dist')));
		for (const [rel, text] of files) write(join(out, rel), text);
		write(join(out, 'llms.txt'), llms(all, meta));
		const capDir = join(out, 'docs-captures');
		for (const [name, source] of captures) write(join(capDir, `${name}.webp`), await webp(source));
		console.log(`site: ${files.size} pages, ${captures.size} captures -> ${out}`);
		return;
	}

	files.set('docs-map.md', docsMap(all, meta));
	const capDir = join(GUIDE, 'assets/captures');
	const previous = existsSync(captureIndexPath(capDir)) ? JSON.parse(readFileSync(captureIndexPath(capDir), 'utf8')) : {};
	const index = {};
	const images = new Map();
	for (const [name, source] of [...captures].sort()) {
		const hash = createHash('sha256').update(readFileSync(source)).update(`${CAPTURE_WIDTH}:${CAPTURE_QUALITY}`).digest('hex').slice(0, 16);
		index[name] = { source: relative(REPO, source).replace(/\\/g, '/'), hash };
		const existing = join(capDir, `${name}.webp`);
		if (previous[name]?.hash === hash && existsSync(existing)) continue;
		if (CHECK) continue; // reported as drift below
		images.set(name, await webp(source));
	}

	if (CHECK) {
		const drift = [];
		for (const [rel, text] of files) {
			const path = join(SKILL, rel);
			if (!existsSync(path) || readFileSync(path, 'utf8') !== text) drift.push(rel);
		}
		const committed = existsSync(join(SKILL, 'docs')) ? listFiles(join(SKILL, 'docs')).map((p) => `docs/${p}`) : [];
		for (const rel of committed) if (!files.has(rel)) drift.push(`${rel} (stale)`);
		if (JSON.stringify(previous) !== JSON.stringify(index)) drift.push('assets/captures/sources.json');
		for (const name of Object.keys(index)) if (!existsSync(join(capDir, `${name}.webp`))) drift.push(`assets/captures/${name}.webp`);
		if (drift.length) {
			console.error(`The guide's bundled docs differ from a fresh export (${drift.length}):`);
			for (const rel of drift.slice(0, 40)) console.error(`  ${rel}`);
			console.error('Run: node site/scripts/export-docs-markdown.mjs');
			process.exit(1);
		}
		console.log(`guide docs up to date: ${all.length} pages, ${captures.size} captures`);
		return;
	}

	rmSync(join(SKILL, 'docs'), { recursive: true, force: true });
	for (const [rel, text] of files) write(join(SKILL, rel), text);
	for (const name of Object.keys(previous)) if (!index[name]) rmSync(join(capDir, `${name}.webp`), { force: true });
	for (const [name, buffer] of images) write(join(capDir, `${name}.webp`), buffer);
	write(captureIndexPath(capDir), `${JSON.stringify(index, null, 2)}\n`);
	console.log(`guide: ${all.length} pages, ${captures.size} captures (${images.size} converted) -> ${relative(REPO, GUIDE)}`);
}

function listFiles(dir) {
	const out = [];
	const walk = (d) => {
		for (const name of readdirSync(d).sort()) {
			const full = join(d, name);
			if (statSync(full).isDirectory()) walk(full);
			else out.push(relative(dir, full).replace(/\\/g, '/'));
		}
	};
	walk(dir);
	return out;
}

function write(path, content) {
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, content);
}

await main();
