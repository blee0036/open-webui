<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { getMyQuota, quotaAmount, quotaDate, type QuotaSummary } from '$lib/apis/team';
	import PageShell from '$lib/components/team/PageShell.svelte';
	import UsageDashboard from '$lib/components/team/usage/UsageDashboard.svelte';
	const i18n = getContext<any>('i18n');
	let quota: QuotaSummary | null = null;
	let error = '';
	let loading = false;
	async function refresh() {
		loading = true;
		try {
			quota = await getMyQuota();
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			loading = false;
		}
	}
	onMount(() => {
		refresh();
	});
</script>

<PageShell title={$i18n.t('My quota')} wide>
	<div class="space-y-7">
		<header class="flex items-start justify-between gap-3">
			<a href="/" class="text-sm text-gray-500">← {$i18n.t('Back to chat')}</a>
			<button
				class="rounded-xl bg-gray-100 px-4 py-2 text-sm dark:bg-gray-800"
				on:click={refresh}
				disabled={loading}>{$i18n.t('Refresh')}</button
			>
		</header>
		{#if error}<p
				role="alert"
				class="rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
			>
				{error}
			</p>{/if}
		{#if quota?.configured && quota.period}
			<section>
				{#if !quota.enabled}<p class="mb-4 text-sm text-gray-500" role="status">
						{$i18n.t('Quota enforcement is currently off.')}
					</p>{/if}
				<div class="flex flex-wrap items-end justify-between gap-5">
					<div>
						<p class="text-sm text-gray-500">
							{quota.plan?.name} · {$i18n.t(
								quota.inherited ? 'Default package' : 'Personal package'
							)}
						</p>
						<p class="mt-2 text-4xl font-semibold tracking-tight">
							{quotaAmount(quota.available, quota.plan?.unit)}
							<span class="text-base font-normal text-gray-500"
								>{$i18n.t(quota.plan?.unit === 'requests' ? 'requests left' : 'available')}</span
							>
						</p>
					</div>
					<p class="text-sm text-gray-500">
						{quota.period.ends_at
							? $i18n.t('Resets') + ' ' + quotaDate(quota.period.ends_at)
							: $i18n.t('Does not reset')}
					</p>
				</div>
				{#if quota.enabled && !quota.available}<p
						role="status"
						class="mt-5 rounded-xl bg-amber-50 p-4 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200"
					>
						{$i18n.t(
							'Quota exhausted. New model requests are blocked; history, files and manual workspace operations remain available.'
						)}
					</p>{:else if quota.enabled && (quota.available ?? 0) <= (quota.period.base + quota.period.bonus) * 0.1}
					<p role="status" class="mt-5 text-sm text-amber-700 dark:text-amber-300">
						{$i18n.t(
							'Your quota is running low. Contact an administrator if you need more this period.'
						)}
					</p>
				{/if}
				<dl
					class="mt-6 grid grid-cols-2 gap-5 border-y border-gray-200 py-5 dark:border-gray-800 md:grid-cols-5"
				>
					{#each [['Base allowance', quota.period.base], ['Temporary bonus', quota.period.bonus], ['Settled usage', quota.period.used], ['Reserved usage', quota.period.reserved], ['Overage', quota.overage]] as item}
						<div>
							<dt class="text-sm text-gray-500">{$i18n.t(String(item[0]))}</dt>
							<dd class="mt-1 text-lg font-medium">
								{quotaAmount(Number(item[1]), quota.plan?.unit)}
							</dd>
						</div>
					{/each}
				</dl>
				{#if quota.pending_plan}<p class="mt-4 text-sm">
						{$i18n.t('Scheduled package')}: <strong>{quota.pending_plan.name}</strong> · {quotaDate(
							quota.pending_at
						)}
					</p>{/if}
				{#if quota.plan?.unit === 'credits'}<p class="mt-3 text-sm text-gray-500">
						{$i18n.t(
							'Amounts are in USD. Each request is charged at the model price in effect when it was sent.'
						)}
					</p>{/if}
			</section>
		{:else if quota}<p class="text-gray-500">
				{$i18n.t('No quota package has been configured.')}
			</p>{/if}
		<section>
			<h2 class="mb-4 text-lg font-medium">{$i18n.t('Usage')}</h2>
			<UsageDashboard />
		</section>
	</div>
</PageShell>
