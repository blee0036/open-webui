import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/constants', () => ({
	OPENAI_API_BASE_URL: '/openai',
	WEBUI_API_BASE_URL: '/api/v1',
	WEBUI_BASE_URL: ''
}));
vi.mock('i18next', () => ({
	default: {
		t: (key: string, options?: { time?: string }) => key.replace('{{time}}', options?.time ?? '')
	}
}));

import { getErrorMessage } from './index';

describe('model quota errors', () => {
	it('includes the reset time for an exhausted package', () => {
		const reset = 1796054400;
		expect(getErrorMessage({ detail: { code: 'quota_exhausted', reset_at: reset } })).toBe(
			`Quota exhausted. Resets at ${new Date(reset * 1000).toLocaleString()}.`
		);
	});

	it('explains packages that do not reset', () => {
		expect(getErrorMessage({ code: 'quota_exhausted', reset_at: null })).toContain(
			'does not reset'
		);
	});

	it('points missing model prices to administration', () => {
		expect(getErrorMessage({ detail: { code: 'model_price_missing' } })).toContain(
			'Ask an administrator'
		);
	});

	it('retains ordinary provider and connection errors', () => {
		expect(getErrorMessage({ error: { message: 'Provider unavailable' } })).toBe(
			'Provider unavailable'
		);
		expect(getErrorMessage(new Error('Connection interrupted'))).toBe('Connection interrupted');
	});
});
