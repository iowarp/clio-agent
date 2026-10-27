import { describe, expect, it } from 'vitest';
import { archOf, detectArch, detectOS, isBundled, pickAsset, type ReleaseAsset } from './downloads';

/** Asset names exactly as published on the v0.9.4.24 release. */
const NAMES = [
	'clio-agent-aarch64-pc-windows-msvc.exe',
	'clio-desktop-aarch64-pc-windows-msvc.exe',
	'clio-tui-windows-amd64.exe',
	'clio-tui-windows-arm64.exe',
	'clio-tui-linux-amd64',
	'CLIO.Desktop-0.9.4.24-1.aarch64-bundled.rpm',
	'CLIO.Desktop-0.9.4.24-1.aarch64.rpm',
	'CLIO.Desktop-0.9.4.24-1.x86_64-bundled.rpm',
	'CLIO.Desktop-0.9.4.24-1.x86_64.rpm',
	'CLIO.Desktop-aarch64-apple-darwin-bundled.app.tar.gz',
	'CLIO.Desktop_0.9.4.24_aarch64-bundled.dmg',
	'CLIO.Desktop_0.9.4.24_aarch64.AppImage',
	'CLIO.Desktop_0.9.4.24_aarch64.AppImage.sig',
	'CLIO.Desktop_0.9.4.24_aarch64.dmg',
	'CLIO.Desktop_0.9.4.24_amd64-bundled.deb',
	'CLIO.Desktop_0.9.4.24_amd64.AppImage',
	'CLIO.Desktop_0.9.4.24_amd64.deb',
	'CLIO.Desktop_0.9.4.24_arm64-bundled.deb',
	'CLIO.Desktop_0.9.4.24_arm64.deb',
	'CLIO.Desktop_0.9.4.24_x64-setup-bundled.exe',
	'CLIO.Desktop_0.9.4.24_x64-setup-bundled.exe.sig',
	'CLIO.Desktop_0.9.4.24_x64-setup.exe',
	'CLIO.Desktop_0.9.4.24_x64.dmg',
	'CLIO.Desktop_0.9.4.24_x64_en-US-bundled.msi',
	'CLIO.Desktop_0.9.4.24_x64_en-US.msi',
];
const ASSETS: ReleaseAsset[] = NAMES.map((name) => ({ name, browser_download_url: `https://dl/${name}` }));

describe('pickAsset', () => {
	it.each([
		['exe', 'x64', 'CLIO.Desktop_0.9.4.24_x64-setup-bundled.exe'],
		['msi', 'x64', 'CLIO.Desktop_0.9.4.24_x64_en-US-bundled.msi'],
		['dmg', 'arm64', 'CLIO.Desktop_0.9.4.24_aarch64-bundled.dmg'],
		['deb', 'x64', 'CLIO.Desktop_0.9.4.24_amd64-bundled.deb'],
		['deb', 'arm64', 'CLIO.Desktop_0.9.4.24_arm64-bundled.deb'],
		['rpm', 'x64', 'CLIO.Desktop-0.9.4.24-1.x86_64-bundled.rpm'],
		['rpm', 'arm64', 'CLIO.Desktop-0.9.4.24-1.aarch64-bundled.rpm'],
	] as const)('offers the bundled %s for %s', (format, arch, name) => {
		expect(pickAsset(ASSETS, format, arch)).toMatchObject({ name });
	});

	it.each([
		['dmg', 'x64'],
		['exe', 'arm64'],
	] as const)('never offers an attach-only %s for %s', (format, arch) => {
		expect(pickAsset(ASSETS, format, arch)).toBeNull();
	});

	it('never returns a signature file', () => {
		for (const format of ['exe', 'msi'] as const) {
			for (const arch of ['x64', 'arm64'] as const) {
				expect(pickAsset(ASSETS, format, arch)?.name.endsWith('.sig')).not.toBe(true);
			}
		}
	});

	it('returns null when the release has no matching installer', () => {
		expect(pickAsset([], 'dmg', 'arm64')).toBeNull();
		expect(pickAsset(ASSETS.filter((a) => !a.name.endsWith('.dmg')), 'dmg', 'x64')).toBeNull();
	});
});

describe('asset name parsing', () => {
	it('reads the architecture from every token the pipeline uses', () => {
		expect(archOf('CLIO.Desktop_0.9.4.24_amd64.deb')).toBe('x64');
		expect(archOf('CLIO.Desktop-0.9.4.24-1.x86_64.rpm')).toBe('x64');
		expect(archOf('CLIO.Desktop_0.9.4.24_x64.dmg')).toBe('x64');
		expect(archOf('CLIO.Desktop_0.9.4.24_aarch64.dmg')).toBe('arm64');
		expect(archOf('CLIO.Desktop_0.9.4.24_arm64.deb')).toBe('arm64');
		expect(archOf('CLIO.Desktop.dmg')).toBeNull();
	});

	it('detects bundled builds by the -bundled token', () => {
		expect(isBundled('CLIO.Desktop_0.9.4.24_amd64-bundled.deb')).toBe(true);
		expect(isBundled('CLIO.Desktop_0.9.4.24_amd64.deb')).toBe(false);
	});
});

describe('platform detection', () => {
	const mac = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15';
	const win = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36';
	const linuxArm = 'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36';

	it('detects desktop operating systems and ignores phones', () => {
		expect(detectOS({ userAgent: mac })).toBe('macos');
		expect(detectOS({ userAgent: win })).toBe('windows');
		expect(detectOS({ userAgent: linuxArm })).toBe('linux');
		expect(detectOS({ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)' })).toBeNull();
		expect(detectOS({ userAgent: 'Mozilla/5.0 (Linux; Android 14)' })).toBeNull();
	});

	it('trusts client hints and otherwise assumes Apple Silicon on a Mac', () => {
		expect(detectArch('macos', { userAgent: mac, architecture: 'x86' })).toBe('x64');
		expect(detectArch('macos', { userAgent: mac, architecture: 'arm' })).toBe('arm64');
		expect(detectArch('macos', { userAgent: mac })).toBe('arm64');
		expect(detectArch('windows', { userAgent: win })).toBe('x64');
		expect(detectArch('linux', { userAgent: linuxArm })).toBe('arm64');
	});
});
