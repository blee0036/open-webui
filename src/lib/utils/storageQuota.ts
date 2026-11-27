import { get, writable } from 'svelte/store';
import { toast } from 'svelte-sonner';
import i18n from '$lib/i18n';
import { getMyQuota, type QuotaSummary } from '$lib/apis/team';

export const formatBytes = (value: number | null | undefined) => {
	let size = Math.max(0, value ?? 0);
	for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
		if (size < 1024 || unit === 'TB') {
			return unit === 'B' ? `${Math.round(size)} B` : `${size.toFixed(size < 10 ? 1 : 0)} ${unit}`;
		}
		size /= 1024;
	}
	return '';
};

export const GB = 1024 ** 3;

/** Share of a cap in percent, or null when unlimited. */
export const usagePercent = (used: number | null | undefined, limit: number | null | undefined) =>
	limit === null || limit === undefined ? null : limit <= 0 ? 100 : ((used ?? 0) / limit) * 100;

export type UsageTone = 'ok' | 'warn' | 'full';
export const usageTone = (percent: number | null): UsageTone =>
	percent === null ? 'ok' : percent >= 100 ? 'full' : percent >= 80 ? 'warn' : 'ok';

// The latest /team/quota/me answer, refreshed by the sidebar badge and the quota page.
// Uploads use it to refuse early and to warn when crossing 80% / 95%.
export const quotaSnapshot = writable<{ value: QuotaSummary; at: number } | null>(null);
export const rememberQuota = (value: QuotaSummary) => quotaSnapshot.set({ value, at: Date.now() });

const t = (key: string, options: Record<string, unknown> = {}) => get(i18n).t(key, options);

type QuotaDetail = { code?: string; used?: number; limit?: number; incoming?: number };

export const isStorageQuotaError = (detail: unknown): detail is QuotaDetail =>
	!!detail &&
	typeof detail === 'object' &&
	(detail as QuotaDetail).code === 'storage_quota_exceeded';

export const storageQuotaMessage = (detail: QuotaDetail) =>
	t(
		'Not enough storage: {{used}} of {{limit}} used, and this file needs {{size}}. Delete files you no longer need, or ask an administrator for more space.',
		{
			used: formatBytes(detail.used),
			limit: formatBytes(detail.limit),
			size: formatBytes(detail.incoming)
		}
	);

const wouldExceed = (summary: QuotaSummary | undefined, size: number) => {
	const storage = summary?.storage;
	if (!storage || storage.limit === null || storage.limit === undefined) return null;
	return storage.used + size > storage.limit
		? { used: storage.used, limit: storage.limit, incoming: size }
		: null;
};

/** A readable refusal when the file cannot fit, or null. Re-reads the quota before refusing. */
export const precheckUpload = async (size: number): Promise<string | null> => {
	const cached = get(quotaSnapshot)?.value;
	if (!wouldExceed(cached, size)) return null;
	// The cache may predate deletions; only refuse on a fresh answer.
	try {
		const fresh = await getMyQuota();
		rememberQuota(fresh);
		const over = wouldExceed(fresh, size);
		return over ? storageQuotaMessage(over) : null;
	} catch {
		return null; // The server still enforces the cap.
	}
};

const warned = new Set<number>();

/** Count an upload against the cached usage and warn once per session at 80% and 95%. */
export const noteUploaded = (size: number) => {
	const snapshot = get(quotaSnapshot);
	const storage = snapshot?.value?.storage;
	if (!snapshot || !storage || storage.limit === null || storage.limit === undefined) return;
	const before = usagePercent(storage.used, storage.limit) ?? 0;
	const used = storage.used + size;
	const after = usagePercent(used, storage.limit) ?? 0;
	quotaSnapshot.set({ ...snapshot, value: { ...snapshot.value, storage: { ...storage, used } } });
	for (const threshold of [95, 80]) {
		if (before < threshold && after >= threshold && !warned.has(threshold)) {
			warned.add(threshold);
			toast.warning(
				t('You have used {{percent}}% of your storage ({{used}} of {{limit}}).', {
					percent: Math.floor(after),
					used: formatBytes(used),
					limit: formatBytes(storage.limit)
				})
			);
			break;
		}
	}
};
