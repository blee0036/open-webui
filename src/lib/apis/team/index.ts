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
export const quotaDate = (value: number | null | undefined) =>
	value ? new Date(value * 1000).toLocaleString() : '—';

// Publishing an image or archive must not depend on a text/OCR/transcription
// service. Documents still use the established extraction and RAG pipeline.
export const shouldIndexProjectReference = (file: Pick<File, 'name' | 'type'>) =>
	file.type.startsWith('text/') ||
	/\.(pdf|docx?|pptx?|xlsx?|ods|odt|rtf|epub|json|ya?ml|xml|csv|tsv|md|txt|rst|log|html?|ipynb)$/i.test(
		file.name
	);
