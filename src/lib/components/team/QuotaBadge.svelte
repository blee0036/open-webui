<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { getMyQuota, quotaAmount, quotaDate, type QuotaSummary } from '$lib/apis/team';
	const i18n = getContext<any>('i18n');
	let quota: QuotaSummary | null = null;
	let error = '';
	const refresh = async () => {
		try {
			quota = await getMyQuota();
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
	</a>
{/if}
