<script lang="ts">
	import { createEventDispatcher, getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { teamRequest, saveTeam } from '$lib/apis/team';
	const i18n = getContext<any>('i18n');
	const dispatch = createEventDispatcher();

	type Upstream = {
		name: string;
		base_url: string;
		endpoint: string;
		api_key?: string;
		preset?: boolean;
		enabled: boolean;
	};
	type Candidate = {
		values: Record<string, unknown>;
		expression: string | null;
		unsupported_reason: string | null;
		trusted: boolean;
		same: boolean;
	};
	type Row = {
		model_id: string;
		current: string | null;
		available: boolean;
		upstreams: Record<string, Candidate>;
	};

	const PAGE = 100;
	let upstreams: Upstream[] = [];
	let endpoints: string[] = [];
	let busy = false;
	let error = '';
	let sources: { name: string; status: string; error: string | null }[] = [];
	let rows: Row[] = [];
	let choice: Record<string, string> = {};
	let selected: Record<string, boolean> = {};
	let query = '';
	let onlyAvailable = true;
	let onlyPriced = false;
	let shown = PAGE;
	let reason = '';
	let compared = false;

	onMount(async () => {
		try {
			const result = await teamRequest('/team/admin/prices/sync/sources');
			endpoints = result.endpoints;
			upstreams = result.presets.map((preset: any) => ({
				name: preset.name,
				base_url: preset.base_url,
				endpoint: preset.endpoint,
				preset: true,
				enabled: preset.id === 'official'
			}));
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	});

	function addUpstream() {
		upstreams = [
			...upstreams,
			{ name: '', base_url: '', endpoint: '/api/pricing', api_key: '', enabled: true }
		];
	}

	function usable(row: Row) {
		return Object.entries(row.upstreams).filter(([, item]) => item.expression && !item.same);
	}

	async function compare() {
		const active = upstreams.filter((item) => item.enabled);
		if (!active.length) {
			error = $i18n.t('Choose at least one upstream');
			return;
		}
		busy = true;
		try {
			const result = await saveTeam(
				'/team/admin/prices/sync/compare',
				{
					upstreams: active.map(({ name, base_url, endpoint, api_key }) => ({
						name: name.trim(),
						base_url: base_url.trim(),
						endpoint: endpoint.trim(),
						...(endpoint === 'openrouter' && api_key ? { api_key } : {})
					})),
					timeout: 20
				},
				'POST'
			);
			sources = result.sources;
			rows = result.models;
			choice = {};
			selected = {};
			for (const row of rows) {
				const options = usable(row);
				// Prefer a trusted candidate; New-API flags its unknown-model default as untrusted.
				const preferred = options.find(([, item]) => item.trusted) ?? options[0];
				if (preferred) choice[row.model_id] = preferred[0];
			}
			shown = PAGE;
			compared = true;
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}

	$: filtered = rows.filter(
		(row) =>
			usable(row).length &&
			(!onlyAvailable || row.available) &&
			(!onlyPriced || row.current) &&
			(!query || row.model_id.toLowerCase().includes(query.toLowerCase()))
	);
	$: selectedCount = Object.values(selected).filter(Boolean).length;

	function selectVisible(value: boolean) {
		const next = { ...selected };
		for (const row of filtered.slice(0, shown)) next[row.model_id] = value;
		selected = next;
	}

	async function apply() {
		const items = rows
			.filter((row) => selected[row.model_id] && choice[row.model_id])
			.map((row) => ({
				model_id: row.model_id,
				expression: row.upstreams[choice[row.model_id]].expression,
				source: choice[row.model_id]
			}));
		if (!items.length) return;
		busy = true;
		try {
			await saveTeam('/team/admin/prices/sync/apply', { items, reason }, 'POST');
			toast.success($i18n.t('Applied {{count}} prices', { count: items.length }));
			reason = '';
			dispatch('applied');
			await compare();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<section class="space-y-5">
	<div>
		<h2 class="text-lg font-medium">{$i18n.t('Sync prices from upstream')}</h2>
		<p class="mt-2 text-sm leading-6 text-gray-500">
			{$i18n.t(
				'Uses the same sources and conversion rules as New-API: a New-API instance (/api/pricing or /api/ratio_config), the official ratio preset, models.dev and OpenRouter. Ratios and per-request prices are converted to expressions, including cache read and cache write prices. Nothing changes until you apply the selected rows.'
			)}
		</p>
	</div>
	{#if error}<p
			role="alert"
			class="rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{error}
		</p>{/if}
	<div class="space-y-2">
		{#each upstreams as upstream, index}
			<div class="flex flex-wrap items-center gap-2 rounded-xl bg-gray-50 p-3 text-sm dark:bg-gray-900">
				<input type="checkbox" bind:checked={upstream.enabled} aria-label={$i18n.t('Use')} />
				{#if upstream.preset}
					<span class="font-medium">{$i18n.t(upstream.name)}</span>
					<span class="truncate text-xs text-gray-500">{upstream.endpoint}</span>
				{:else}
					<input
						class="field max-w-40"
						bind:value={upstream.name}
						placeholder={$i18n.t('Name')}
						required
					/>
					<input
						class="field min-w-48 flex-1"
						bind:value={upstream.base_url}
						placeholder="https://new-api.example.com"
					/>
					<select class="field max-w-44" bind:value={upstream.endpoint}>
						{#each endpoints as endpoint}<option value={endpoint}>{endpoint}</option>{/each}
					</select>
					{#if upstream.endpoint === 'openrouter'}<input
							class="field max-w-56"
							type="password"
							autocomplete="off"
							bind:value={upstream.api_key}
							placeholder={$i18n.t('OpenRouter API key (not stored)')}
						/>{/if}
					<button
						class="text-xs text-gray-500 underline"
						on:click={() => (upstreams = upstreams.filter((_, i) => i !== index))}
						>{$i18n.t('Remove')}</button
					>
				{/if}
			</div>
		{/each}
		<div class="flex flex-wrap gap-3">
			<button class="text-sm underline" on:click={addUpstream}>{$i18n.t('Add upstream')}</button>
			<button class="primary" disabled={busy} on:click={compare}
				>{$i18n.t(busy ? 'Fetching…' : 'Compare prices')}</button
			>
		</div>
	</div>

	{#if compared}
		<div class="flex flex-wrap gap-2 text-xs">
			{#each sources as source}<span
					class="rounded-full px-2.5 py-1 {source.status === 'success'
						? 'bg-green-50 text-green-800 dark:bg-green-950 dark:text-green-200'
						: 'bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200'}"
					title={source.error ?? ''}
					>{source.name}: {source.status === 'success' ? $i18n.t('OK') : source.error}</span
				>{/each}
		</div>
		<div class="flex flex-wrap items-center gap-4 text-sm">
			<input
				class="field max-w-64"
				bind:value={query}
				placeholder={$i18n.t('Filter models')}
				on:input={() => (shown = PAGE)}
			/>
			<label class="flex items-center gap-2"
				><input type="checkbox" bind:checked={onlyAvailable} />{$i18n.t(
					'Only models available here'
				)}</label
			>
			<label class="flex items-center gap-2"
				><input type="checkbox" bind:checked={onlyPriced} />{$i18n.t('Only models already priced')}</label
			>
			<span class="text-gray-500">{filtered.length} {$i18n.t('differences')}</span>
			<button class="underline" on:click={() => selectVisible(true)}>{$i18n.t('Select shown')}</button>
			<button class="underline" on:click={() => selectVisible(false)}>{$i18n.t('Clear shown')}</button>
		</div>
		<div class="divide-y divide-gray-100 dark:divide-gray-800">
			{#each filtered.slice(0, shown) as row (row.model_id)}
				<div class="grid gap-2 py-3 text-sm md:grid-cols-[auto_minmax(0,1fr)]">
					<input
						type="checkbox"
						class="mt-1"
						bind:checked={selected[row.model_id]}
						aria-label={row.model_id}
					/>
					<div class="min-w-0 space-y-1.5">
						<div class="flex flex-wrap items-center gap-2">
							<strong class="break-all">{row.model_id}</strong>
							{#if !row.available}<span class="text-xs text-gray-500"
									>{$i18n.t('not configured here')}</span
								>{/if}
						</div>
						<p class="break-all font-mono text-xs text-gray-500">
							{$i18n.t('Current')}: {row.current ?? $i18n.t('none')}
						</p>
						{#each Object.entries(row.upstreams) as [name, item]}
							<label class="flex items-start gap-2 text-xs">
								<input
									type="radio"
									name={'choice-' + row.model_id}
									value={name}
									bind:group={choice[row.model_id]}
									disabled={!item.expression || item.same}
								/>
								<span class="min-w-0">
									<span class="font-medium">{name}</span>
									{#if !item.trusted}<span class="ml-1 text-amber-700 dark:text-amber-300"
											>{$i18n.t('Untrusted (New-API default ratio)')}</span
										>{/if}
									{#if item.same}<span class="ml-1 text-gray-500">{$i18n.t('Same as current')}</span
										>{/if}
									<span class="block break-all font-mono"
										>{item.expression ?? item.unsupported_reason}</span
									>
								</span>
							</label>
						{/each}
					</div>
				</div>
			{/each}
		</div>
		{#if filtered.length > shown}<button class="text-sm underline" on:click={() => (shown += PAGE)}
				>{$i18n.t('Show more')} ({filtered.length - shown})</button
			>{/if}
		<form class="flex flex-wrap gap-3 border-t border-gray-200 pt-4 dark:border-gray-800" on:submit|preventDefault={apply}>
			<input
				class="field min-w-48 flex-1"
				required
				maxlength="500"
				bind:value={reason}
				placeholder={$i18n.t('Reason')}
				aria-label={$i18n.t('Reason')}
			/>
			<button class="primary" disabled={busy || !selectedCount}
				>{$i18n.t('Apply {{count}} selected prices', { count: selectedCount })}</button
			>
		</form>
	{/if}
</section>

<style>
	@reference "../../../tailwind.css";
	.field {
		@apply block w-full min-w-0 rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 text-sm dark:border-gray-700;
	}
	.primary {
		@apply shrink-0 rounded-xl bg-gray-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900;
	}
</style>
