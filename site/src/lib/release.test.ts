import { describe, expect, it } from 'vitest';
import { parseProjectVersion, releaseFor } from './release';

describe('parseProjectVersion', () => {
	it('reads the [project] version, not a version from another table', () => {
		const toml = '[tool.other]\nversion = "9.9.9"\n\n[project]\nname = "clio-agent"\nversion = "0.9.4.24"\n';
		expect(parseProjectVersion(toml)).toBe('0.9.4.24');
	});

	it('fails loudly instead of publishing a made-up version', () => {
		expect(() => parseProjectVersion('[tool.x]\nversion = "1"\n')).toThrow(/no \[project\] table/);
		expect(() => parseProjectVersion('[project]\nname = "x"\n')).toThrow(/no version/);
	});
});

describe('releaseFor', () => {
	it('uses the GitHub beta spelling while preserving the package and container versions', () => {
		expect(releaseFor('0.9.5b1')).toEqual({
			version: '0.9.5b1', tag: 'v0.9.5-beta.1',
			notesUrl: 'https://github.com/iowarp/clio-agent/releases/tag/v0.9.5-beta.1',
			webImage: 'ghcr.io/iowarp/clio-web:0.9.5b1',
		});
	});
	it('derives the tag, notes page, and web image from one version', () => {
		expect(releaseFor('0.9.4.24')).toEqual({
			version: '0.9.4.24',
			tag: 'v0.9.4.24',
			notesUrl: 'https://github.com/iowarp/clio-agent/releases/tag/v0.9.4.24',
			webImage: 'ghcr.io/iowarp/clio-web:0.9.4.24',
		});
	});
});
