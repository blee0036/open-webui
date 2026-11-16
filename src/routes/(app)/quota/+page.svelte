<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import {
		getMyQuota,
		teamRequest,
		quotaNumber,
		quotaDate,
		type QuotaSummary
	} from '$lib/apis/team';
	import PageShell from '$lib/components/team/PageShell.svelte';
	const i18n = getContext<any>('i18n');
	let quota: QuotaSummary | null = null;
	let entries: any[] = [];
	let error = '';
	let loading = false;
	let more = true;
	async function refresh() {
		loading = true;
		try {
			[quota, entries] = await Promise.all([getMyQuota(), teamRequest('/team/quota/me/entries')]);
			error = '';
			more = entries.length === 50;
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			loading = false;
		}
	}
	async function nextPage() {
		try {
			const page = await teamRequest<any[]>(
				`/team/quota/me/entries?before=${entries.at(-1)?.created_at}&before_id=${encodeURIComponent(entries.at(-1)?.id ?? '')}`
			);
			entries = [...entries, ...page];
			more = page.length === 50;
		} catch (e) {
			error = String(e);
		}
	}
	onMount(() => {
		refresh();
	});
	const usageInput = (u: any) =>
		u?.prompt_tokens ?? u?.input_tokens ?? u?.prompt_eval_count ?? u?.promptTokenCount;
	const usageOutput = (u: any) =>
		u?.completion_tokens ?? u?.output_tokens ?? u?.eval_count ?? u?.candidatesTokenCount;
</script>

<PageShell title={$i18n.t('My quota')}>
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
							{quotaNumber(quota.available)}
							<span class="text-base font-normal text-gray-500"
								>{$i18n.t(quota.plan?.unit === 'requests' ? 'requests left' : 'credits left')}</span
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
							<dd class="mt-1 text-lg font-medium">{quotaNumber(Number(item[1]))}</dd>
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
							'1 USD equivalent = 1,000,000 credits. Each attempt uses its frozen model price.'
						)}
					</p>{/if}
			</section>
			<section>
				<h2 class="mb-4 text-lg font-medium">{$i18n.t('Request history')}</h2>
				<div class="overflow-x-auto">
					<table class="w-full text-left text-sm">
						<thead class="text-gray-500"
							><tr
								>{#each ['Time', 'Model', 'Status', 'Input / Output tokens', 'Charge / Reserved'] as label}<th
										class="whitespace-nowrap pb-3 pr-5 font-normal">{$i18n.t(label)}</th
									>{/each}</tr
							></thead
						><tbody>
							{#each entries as entry}<tr class="border-t border-gray-100 dark:border-gray-800"
									><td class="whitespace-nowrap py-3 pr-5">{quotaDate(entry.created_at)}</td><td
										class="max-w-64 truncate pr-5"
										title={entry.model_id}>{entry.model_id}</td
									><td class="pr-5"
										><span class:text-amber-700={entry.state === 'pending'}
											>{$i18n.t(entry.state)}</span
										>{#if entry.detail}<details class="mt-1 max-w-60 text-xs text-gray-500">
												<summary>{$i18n.t('Details')}</summary>{entry.detail}
											</details>{/if}</td
									><td class="pr-5"
										>{usageInput(entry.usage) ?? '—'} / {usageOutput(entry.usage) ??
											'—'}{#if entry.usage}<details class="mt-1 text-xs text-gray-500">
												<summary>{$i18n.t('Token details')}</summary>
												<pre
													class="mt-2 max-w-80 overflow-auto whitespace-pre-wrap">{JSON.stringify(
														entry.usage,
														null,
														2
													)}</pre>
											</details>{/if}</td
									><td
										>{entry.charged ?? '—'} / {['reserved', 'pending'].includes(entry.state)
											? entry.reserved
											: 0}</td
									></tr
								>{/each}
						</tbody>
					</table>
				</div>
				{#if !entries.length}<p class="py-8 text-sm text-gray-500">
						{$i18n.t('No model requests in your ledger yet.')}
					</p>{/if}
				{#if more}<button
						class="mt-4 rounded-xl bg-gray-100 px-4 py-2 text-sm dark:bg-gray-800"
						on:click={nextPage}>{$i18n.t('Load more')}</button
					>{/if}
			</section>
		{:else if quota}<p class="text-gray-500">
				{$i18n.t('No quota package has been configured.')}
			</p>{/if}
	</div>
</PageShell>
