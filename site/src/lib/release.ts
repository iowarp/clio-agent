/**
 * Build-time release facts, read from the repository's pyproject.toml.
 *
 * The site deploys from `main` when a release lands, so the package version
 * in pyproject.toml is the released version. Reading it at build time keeps
 * every version string on the site in step with the release; the old static
 * site hardcoded these and drifted 23 patch releases behind.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/** Release identity used across the overview, install docs, and footer. */
export interface Release {
	/** Package version without a leading `v`, e.g. `0.9.4.24`. */
	version: string;
	/** Git tag, e.g. `v0.9.4.24`. */
	tag: string;
	/** GitHub release page for this tag. */
	notesUrl: string;
	/** Container tag for the published web image. */
	webImage: string;
}

/** The GitHub repository that publishes CLIO releases. */
export const REPO = 'iowarp/clio-agent';

/**
 * Extract `[project].version` from pyproject.toml text.
 *
 * @throws Error when the `[project]` table or its version key is missing.
 */
export function parseProjectVersion(pyproject: string): string {
	const table = pyproject.split(/^\[/m).find((block) => block.startsWith('project]'));
	if (!table) throw new Error('pyproject.toml has no [project] table');
	const match = table.match(/^version\s*=\s*"([^"]+)"/m);
	if (!match) throw new Error('pyproject.toml [project] table has no version');
	return match[1];
}

/** Build the release record for `version`. */
export function releaseFor(version: string): Release {
	const tag = `v${version.replace(/^(\d+\.\d+\.\d+)b(\d+)$/, '$1-beta.$2')}`;
	return {
		version,
		tag,
		notesUrl: `https://github.com/${REPO}/releases/tag/${tag}`,
		webImage: `ghcr.io/iowarp/clio-web:${version}`,
	};
}

/** Release facts for this build. `pnpm build` runs from `site/`. */
export const release: Release = releaseFor(
	parseProjectVersion(readFileSync(resolve(process.cwd(), '..', 'pyproject.toml'), 'utf8')),
);
