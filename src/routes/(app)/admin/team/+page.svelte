<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { teamRequest, saveTeam, quotaDate } from '$lib/apis/team';
	import PackagesPanel from '$lib/components/team/admin/PackagesPanel.svelte';
	import MembersPanel from '$lib/components/team/admin/MembersPanel.svelte';
	import EnterpriseAdmin from '$lib/components/team/EnterpriseAdmin.svelte';
	import UsageDashboard from '$lib/components/team/usage/UsageDashboard.svelte';
	import PriceSync from '$lib/components/team/PriceSync.svelte';
	const i18n = getContext<any>('i18n');
	let tab = 'packages';
	let error = '';
	let busy = false;
	let prices: any[] = [];
	let priceReason = '';
	let price = {
		model_id: '',
		expression: 'tier("base", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)',
		multiplier: '1'
	};
	let audits: any[] = [];
	let auditMore = false;
	async function loadAudit() {
		audits = await teamRequest('/team/admin/audit');
		auditMore = audits.length === 50;
	}
	async function load() {
		try {
			prices = await teamRequest('/team/admin/prices');
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		}
	}
	async function action(work: () => Promise<unknown>, notifySuccess = true) {
		if (busy) return;
		busy = true;
		try {
			await work();
			if (notifySuccess) toast.success($i18n.t('Saved'));
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = false;
		}
	}
	onMount(() => {
		load();
	});
</script>

<div class="mx-auto max-w-7xl px-5 py-6 md:px-10">
	<h1 class="text-2xl font-semibold">{$i18n.t('Team & quota')}</h1>
	<nav class="my-6 flex gap-1 overflow-x-auto border-b border-gray-200 dark:border-gray-800">
		{#each [['packages', 'Packages'], ['members', 'Member quota'], ['usage', 'Usage'], ['pricing', 'Model pricing'], ['sso', 'Enterprise login'], ['audit', 'Adjustment history']] as item}
			<button
				class="whitespace-nowrap border-b-2 px-4 py-3 text-sm {tab === item[0]
					? 'border-current font-medium'
					: 'border-transparent text-gray-500'}"
				on:click={() => {
					tab = item[0];
					if (tab === 'audit') action(loadAudit, false);
				}}>{$i18n.t(item[1])}</button
			>
		{/each}
	</nav>
	{#if error}<p
			role="alert"
			class="mb-5 rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{error}
		</p>{/if}
	{#if tab === 'packages'}<PackagesPanel />
	{:else if tab === 'members'}<MembersPanel />
	{:else if tab === 'pricing'}
		<div class="grid gap-8 lg:grid-cols-2">
			<section>
				<h2 class="mb-4 text-lg font-medium">{$i18n.t('Model pricing')}</h2>
				<p class="mb-5 text-sm leading-6 text-gray-500">
					{$i18n.t(
						'New-API billing expressions. Token prices are USD per million tokens; fixed(x) is x USD per request. p: input not priced separately, c: output, cr: cache read, cc: cache write (short TTL), cc1h: Claude 1h write, cc12h: OpenAI 12h write, img / img_cr / img_o: image input / image cache read / image output, ai / ao: audio in / out, image_count: images requested. Categories you do not price stay in p / c. Use len (full input, including cache) for tier conditions.'
					)}
				</p>
				<div class="space-y-2">
					{#each prices as row}<button
							class="w-full rounded-xl bg-gray-50 p-3 text-left text-sm dark:bg-gray-900"
							on:click={() => {
								price = {
									model_id: row.model_id,
									expression: row.expression,
									multiplier: row.multiplier
								};
								priceReason = '';
							}}
							><strong>{row.model_id}</strong>
							<p class="mt-1 break-all font-mono text-xs text-gray-500">{row.expression}</p></button
						>{/each}
				</div>
			</section>
			<form
				class="space-y-4"
				on:submit|preventDefault={() =>
					action(async () => {
						await saveTeam(`/team/admin/prices/${encodeURIComponent(price.model_id)}`, {
							expression: price.expression,
							multiplier: price.multiplier,
							reason: priceReason
						});
						await load();
						priceReason = '';
					})}
			>
				<label class="block text-sm"
					>{$i18n.t('Model ID')}<input
						class="field mt-2"
						required
						bind:value={price.model_id}
					/></label
				><label class="block text-sm"
					>{$i18n.t('Pricing expression')}<textarea
						class="field mt-2 font-mono text-sm"
						rows="6"
						required
						bind:value={price.expression}
					></textarea></label
				><label class="block text-sm"
					>{$i18n.t('Multiplier')}<input
						class="field mt-2"
						required
						bind:value={price.multiplier}
					/></label
				><label class="block text-sm"
					>{$i18n.t('Note (optional)')}<input
						class="field mt-2"
						maxlength="500"
						bind:value={priceReason}
					/></label
				><button class="primary" disabled={busy}>{$i18n.t('Save price version')}</button>
				<details class="text-sm text-gray-500">
					<summary>{$i18n.t('Tier pricing examples')}</summary>
					<pre
						class="mt-3 overflow-auto whitespace-pre-wrap text-xs">{`len <= 200000 ? tier("standard", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6) : tier("long_context", p * 6 + c * 22.5 + cr * 0.6 + cc * 7.5 + cc1h * 12)\ntier("image", fixed(0.04)) * image_count\nlen <= 32000 ? tier("short", fixed(0.01)) : tier("long", p * 2 + c * 8)\n(tier("base", p * 5 + c * 25)) * (param("service_tier") == "fast" ? 2 : 1)`}</pre>
				</details>
			</form>
		</div>
		<div class="mt-10 border-t border-gray-200 pt-8 dark:border-gray-800">
			<PriceSync on:applied={() => action(load, false)} />
		</div>
	{:else if tab === 'usage'}<UsageDashboard admin />
	{:else if tab === 'sso'}<EnterpriseAdmin />
	{:else if tab === 'audit'}
		<div class="mb-5 flex items-center justify-between gap-3">
			<h2 class="text-lg font-medium">{$i18n.t('Adjustment history')}</h2>
			<button class="text-sm underline" disabled={busy} on:click={() => action(loadAudit, false)}
				>{$i18n.t('Refresh')}</button
			>
		</div>
		<div class="divide-y divide-gray-100 dark:divide-gray-800">
			{#each audits as row}<div class="py-4 text-sm">
					<div class="flex flex-wrap justify-between gap-2">
						<strong>{$i18n.t(row.action)}</strong><span class="text-gray-500"
							>{quotaDate(row.created_at)}</span
						>
					</div>
					<p class="mt-1 break-all text-xs text-gray-500">
						{$i18n.t('Operator')}: {row.actor_id}{row.user_id
							? ' · ' + $i18n.t('User') + ': ' + row.user_id
							: ''}
					</p>
					<p class="mt-2">{row.data?.reason || ''}</p>
					<details class="mt-2 text-gray-500">
						<summary>{$i18n.t('Details')}</summary>
						<pre class="mt-2 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(
								row.data,
								null,
								2
							)}</pre>
					</details>
				</div>{/each}
		</div>
		{#if auditMore}<button
				class="mt-4 text-sm underline"
				disabled={busy}
				on:click={() =>
					action(async () => {
						const last = audits.at(-1);
						const next = await teamRequest<any[]>(
							`/team/admin/audit?before=${last.created_at}&before_id=${last.id}`
						);
						audits = [...audits, ...next];
						auditMore = next.length === 50;
					}, false)}>{$i18n.t('Load more')}</button
			>{/if}
	{/if}
</div>

<style>
	@reference "../../../../tailwind.css";
	.field {
		@apply block w-full min-w-0 rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 text-sm dark:border-gray-700;
	}
	.primary {
		@apply shrink-0 rounded-xl bg-gray-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900;
	}
</style>
