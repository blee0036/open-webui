import { WEBUI_API_BASE_URL } from '$lib/constants';

export type QuotaPlan = {
	id: string;
	name: string;
	unit: 'requests' | 'credits';
	allowance: number;
	reset: { kind: 'day' | 'week' | 'month' | 'days' | 'never'; timezone: string; days?: number };
	archived: boolean;
};
export type QuotaSummary = {
	enabled: boolean;
	configured: boolean;
	plan?: QuotaPlan;
	inherited?: boolean;
	available?: number;
	overage?: number;
	period?: {
		id: string;
		base: number;
		bonus: number;
		used: number;
		reserved: number;
		starts_at: number;
		ends_at: number | null;
	};
	pending_plan?: QuotaPlan;
	pending_at?: number;
};
export type WorkspaceStatus = {
	id: string;
	status: string;
	public: boolean;
	manual: boolean;
	error?: string;
	queue_position?: number;
	agent_status?: 'running' | 'paused' | 'queued' | 'idle';
	agent_queue?: number;
};

export async function teamRequest<T = any>(path: string, options: RequestInit = {}): Promise<T> {
	const response = await fetch(`${WEBUI_API_BASE_URL}${path}`, {
		...options,
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${localStorage.token}`,
			...options.headers
		}
	});
	const data = await response.json().catch(() => null);
	if (!response.ok) {
		const detail = data?.detail ?? data?.error;
		const error = new Error(
			typeof detail === 'string'
				? detail
				: (detail?.message ?? JSON.stringify(detail ?? response.status))
		) as Error & { status?: number; detail?: unknown };
		error.status = response.status;
		error.detail = detail;
		throw error;
	}
	return data;
}

export const saveTeam = (path: string, data: unknown, method = 'PUT') =>
	teamRequest(path, { method, body: JSON.stringify(data) });
export const getMyQuota = () => teamRequest<QuotaSummary>('/team/quota/me');
export const quotaNumber = (value: number | undefined) => (value ?? 0).toLocaleString();

// Credit plans are stored in micro-USD (1 USD = 1,000,000 credits, as quota_pricing.py
// charges); every screen shows and accepts them as dollars instead.
export const CREDITS_PER_USD = 1_000_000;
export const quotaAmount = (value: number | undefined, unit?: string, precise = false) =>
	unit === 'credits'
		? '$' +
			((value ?? 0) / CREDITS_PER_USD).toLocaleString(undefined, {
				minimumFractionDigits: 2,
				maximumFractionDigits: precise ? 6 : 2
			})
		: quotaNumber(value);
export const quotaInput = (value: number | undefined, unit?: string) =>
	unit === 'credits' ? (value ?? 0) / CREDITS_PER_USD : (value ?? 0);
export const quotaStored = (input: number | string, unit?: string) =>
	Math.round(Number(input) * (unit === 'credits' ? CREDITS_PER_USD : 1));
// Ledger entries carry a frozen price snapshot only when they were charged in credits.
export const entryUnit = (entry: { pricing?: unknown }) =>
	entry?.pricing ? 'credits' : 'requests';
export const quotaDate = (value: number | null | undefined) =>
	value ? new Date(value * 1000).toLocaleString() : '—';

// Publishing an image or archive must not depend on a text/OCR/transcription
// service. Documents still use the established extraction and RAG pipeline.
export const shouldIndexProjectReference = (file: Pick<File, 'name' | 'type'>) =>
	file.type.startsWith('text/') ||
	/\.(pdf|docx?|pptx?|xlsx?|ods|odt|rtf|epub|json|ya?ml|xml|csv|tsv|md|txt|rst|log|html?|ipynb)$/i.test(
		file.name
	);
