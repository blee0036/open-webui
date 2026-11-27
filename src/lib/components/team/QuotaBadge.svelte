<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { getMyQuota, quotaAmount, quotaDate, type QuotaSummary } from '$lib/apis/team';
	import {
		formatBytes,
		rememberQuota,
		usagePercent,
		usageTone,
		type UsageTone
	} from '$lib/utils/storageQuota';
	const i18n = getContext<any>('i18n');
	let quota: QuotaSummary | null = null;
	let error = '';
	const refresh = async () => {
		try {
			quota = await getMyQuota();
			rememberQuota(quota);
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		}
	};
	onMount(() => {
		refresh();
		const interval = setInterval(() => {
			if (!document.hidden) refresh();
		}, 15000);
		window.addEventListener('focus', refresh);
		return () => {
			clearInterval(interval);
			window.removeEventListener('focus', refresh);
		};
	});
	$: total = (quota?.period?.base ?? 0) + (quota?.period?.bonus ?? 0);
	$: low = quota?.enabled && (quota?.available ?? 0) <= total * 0.1;
	type StorageRow = { label: string; used: number; limit: number; over: boolean };
	// Storage rows only appear when the package caps them.
	$: storageRows = (
		[
			quota?.storage && quota.storage.limit !== null
				? {
						label: $i18n.t('Files'),
						used: quota.storage.used,
						limit: quota.storage.limit,
						over: false
					}
				: null,
			quota?.sandbox && quota.sandbox.limit !== null
				? {
						label: $i18n.t('Sandbox'),
						used: quota.sandbox.used,
						limit: quota.sandbox.limit,
						over: quota.sandbox.over
					}
				: null
		] as (StorageRow | null)[]
	)
		.filter((row): row is StorageRow => row !== null)
		.map((row) => {
			const percent = usagePercent(row.used, row.limit);
			const tone: UsageTone = row.over ? 'full' : usageTone(percent);
			return { ...row, percent, tone };
		});
	const toneText: Record<UsageTone, string> = {
		ok: 'text-base-500 dark:text-base-400',
		warn: 'text-amber-700 dark:text-amber-300',
		full: 'text-red-600 dark:text-red-400'
	};
	const toneBar: Record<UsageTone, string> = {
		ok: 'bg-emerald-600 dark:bg-emerald-400',
		warn: 'bg-amber-500',
		full: 'bg-red-500'
	};
</script>

{#if error}
	<button
		class="mx-3 my-2 text-left text-xs text-amber-700 dark:text-amber-300"
		on:click={refresh}
		title={error}
	>
		{$i18n.t('Quota unavailable · Retry')}
	</button>
{:else if quota?.configured}
	<a
		href="/quota"
		class="mx-2 my-2 block rounded-xl bg-black/[.035] p-3 dark:bg-white/[.045] hover:bg-black/[.06] dark:hover:bg-white/[.07]"
		aria-label={$i18n.t('My quota')}
	>
		<div class="flex items-center justify-between gap-2 text-xs">
			<span class="truncate font-medium">{quota.plan?.name}</span>
			<span class:text-amber-700={low} class:dark:text-amber-300={low}>
				{quotaAmount(quota.available, quota.plan?.unit)}
				{$i18n.t(quota.plan?.unit === 'requests' ? 'requests left' : 'available')}
			</span>
		</div>
		<div class="my-2 h-1 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
			<div
				class="h-full rounded-full {low ? 'bg-amber-500' : 'bg-emerald-600 dark:bg-emerald-400'}"
				style:width="{Math.min(100, Math.max(0, ((quota.available ?? 0) / (total || 1)) * 100))}%"
			></div>
		</div>
		<p class="text-xs leading-5 text-gray-500 dark:text-gray-400">
			{!quota.enabled
				? $i18n.t('Quota enforcement is currently off.')
				: quota.period?.ends_at
					? $i18n.t('Resets') + ' ' + quotaDate(quota.period.ends_at)
					: $i18n.t('Does not reset')}
		</p>
		{#each storageRows as row}
			<div class="mt-2 text-xs">
				<div class="flex items-center justify-between gap-2">
					<span class="truncate">{row.label}</span>
					<span class={toneText[row.tone]}>
						{formatBytes(row.used)} / {formatBytes(row.limit)}
					</span>
				</div>
				<div class="mt-1 h-1 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
					<div
						class="h-full rounded-full {toneBar[row.tone]}"
						style:width="{Math.min(100, Math.max(0, row.percent ?? 0))}%"
					></div>
				</div>
				{#if row.over}
					<p class="mt-1 leading-5 {toneText.full}">
						{$i18n.t('Sandbox storage is full. Agent commands are paused until you free space.')}
					</p>
				{:else if row.tone === 'full'}
					<p class="mt-1 leading-5 {toneText.full}">
						{$i18n.t('Storage is full. New uploads are refused until you delete files.')}
					</p>
				{/if}
			</div>
		{/each}
	</a>
{/if}
