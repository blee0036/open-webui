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
// Enterprise ID remembered by /connect/<id> and the sign-in page (localStorage).
export const ENTERPRISE_LOGIN_KEY = 'enterpriseLoginId';

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
// Newer entries record their plan unit; older ones carried a price snapshot only in credits.
export const entryUnit = (entry: { pricing?: { unit?: string } | null }) =>
	entry?.pricing?.unit ?? (entry?.pricing ? 'credits' : 'requests');
export const usd = (micro: number | undefined, digits = 2) =>
	'$' +
	((micro ?? 0) / CREDITS_PER_USD).toLocaleString(undefined, {
		minimumFractionDigits: digits,
		maximumFractionDigits: Math.max(digits, 2)
	});
export const compactNumber = (value: number | undefined) =>
	new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(
		value ?? 0
	);

export type UsageTotals = {
	requests: number;
	prompt_tokens: number;
	completion_tokens: number;
	cache_read_tokens: number;
	cache_write_tokens: number;
	total_tokens: number;
	cost: number;
};
export type UsageStatistics = {
	summary: UsageTotals;
	models: (UsageTotals & { model_id: string })[];
	series: { day: string; models: Record<string, UsageTotals> }[];
	/** org: enterprise (SSO connect) IDs; null for local accounts. */
	users: (UsageTotals & { user_id: string; name?: string | null; org?: string | null })[];
};
export type UsageLog = Omit<UsageTotals, 'requests'> & {
	id: string;
	user_id: string;
	name?: string | null;
	org?: string | null;
	model_id: string;
	state: string;
	detail?: string | null;
	created_at: number;
	settled_at?: number | null;
	charged?: number | null;
	reserved: number;
	unit: string;
	tier?: string | null;
};
export type UsageLogPage = { items: UsageLog[]; total: number; page: number; page_size: number };
export const quotaDate = (value: number | null | undefined) =>
	value ? new Date(value * 1000).toLocaleString() : '—';

// Publishing an image or archive must not depend on a text/OCR/transcription
// service. Documents still use the established extraction and RAG pipeline.
export const shouldIndexProjectReference = (file: Pick<File, 'name' | 'type'>) =>
	file.type.startsWith('text/') ||
	/\.(pdf|docx?|pptx?|xlsx?|ods|odt|rtf|epub|json|ya?ml|xml|csv|tsv|md|txt|rst|log|html?|ipynb)$/i.test(
		file.name
	);
