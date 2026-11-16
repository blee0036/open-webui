import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/constants', () => ({ WEBUI_API_BASE_URL: '/api/v1' }));

import { shouldIndexProjectReference } from './index';

describe('project reference publication', () => {
	it.each([
		['report.pdf', 'application/pdf'],
		['sales.xlsx', 'application/octet-stream'],
		['sales.csv', 'text/csv'],
		['notes', 'text/plain']
	])('indexes documents: %s', (name, type) => {
		expect(shouldIndexProjectReference({ name, type })).toBe(true);
	});

	it.each([
		['chart.png', 'image/png'],
		['recording.mp3', 'audio/mpeg'],
		['video.mp4', 'video/mp4'],
		['archive.zip', 'application/zip']
	])('publishes assets without OCR or transcription: %s', (name, type) => {
		expect(shouldIndexProjectReference({ name, type })).toBe(false);
	});
});
