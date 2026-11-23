<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { WEBUI_API_BASE_URL } from '$lib/constants';
	import {
		compactNumber,
		quotaDate,
		quotaNumber,
		teamRequest,
		usd,
		type UsageLogPage,
		type UsageStatistics,
		type UsageTotals
	} from '$lib/apis/team';
	import UsageChart from './UsageChart.svelte';

	/** Admins see everyone, filter by member, rank members and download monthly reports. */
	export let admin = false;

	const i18n = getContext<any>('i18n');
	const base = admin ? '/team/admin/usage' : '/team/usage/me';
	const PAGE_SIZE = 20;
	const TOP_MODELS = 8;
	const palette = [
		'#2563eb',
		'#16a34a',
		'#f59e0b',
		'#db2777',
		'#7c3aed',
		'#0891b2',
		'#dc2626',
		'#65a30d',
		'#9ca3af'
	];
	// Literal $i18n.t calls so the translation extractor finds every label.
	$: presets = [
		['today', $i18n.t('Today')],
		['7d', $i18n.t('Last 7 days')],
		['30d', $i18n.t('Last 30 days')],
		['month', $i18n.t('This month')],
		['last', $i18n.t('Last month')]
	];
	$: metrics = [
		['cost', $i18n.t('Cost')],
		['total_tokens', $i18n.t('Tokens')],
		['requests', $i18n.t('Requests')]
	] as [typeof metric, string][];

	let preset = '7d';
	let startDate = '';
	let endDate = '';
	let modelId = '';
	let state = '';
	let userId = '';
	let userLabel = '';
	let userQuery = '';
	let userOptions: { id: string; name: string; org?: string | null }[] = [];
	let metric: 'cost' | 'total_tokens' | 'requests' = 'cost';
	let stats: UsageStatistics | null = null;
	let logs: UsageLogPage | null = null;
	let page = 1;
	let loading = false;
	let error = '';
	let reportMonth = '';
	let downloading = false;
	let searchTimer: ReturnType<typeof setTimeout>;

	const pad = (n: number) => String(n).padStart(2, '0');
	const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
	const epoch = (value: string, endOfDay = false) => {
		const [y, m, d] = value.split('-').map(Number);
		return Math.floor(
			new Date(y, m - 1, d, endOfDay ? 23 : 0, endOfDay ? 59 : 0, endOfDay ? 59 : 0).getTime() /
				1000
		);
	};
	const formatMetric = (value: number) => (metric === 'cost' ? usd(value) : compactNumber(value));

	function choose(next: string) {
		preset = next;
		const today = new Date();
		let start = new Date(today);
		let end = new Date(today);
		if (next === '7d') start.setDate(start.getDate() - 6);
		else if (next === '30d') start.setDate(start.getDate() - 29);
		else if (next === 'month') start = new Date(today.getFullYear(), today.getMonth(), 1);
		else if (next === 'last') {
			start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
			end = new Date(today.getFullYear(), today.getMonth(), 0);
		}
		startDate = ymd(start);
		endDate = ymd(end);
		reload();
	}

	function query(extra: Record<string, string> = {}) {
		const params = new URLSearchParams({
			start: String(epoch(startDate)),
			end: String(epoch(endDate, true)),
			...extra
		});
		if (modelId) params.set('model_id', modelId);
		if (admin && userId) params.set('user_id', userId);
		return params.toString();
	}

	const fetchLogs = () =>
		teamRequest<UsageLogPage>(
			`${base}/logs?${query({ page: String(page), page_size: String(PAGE_SIZE), ...(state ? { state } : {}) })}`
		);

	async function reload() {
		if (!startDate || !endDate) return;
		if (startDate > endDate) [startDate, endDate] = [endDate, startDate];
		page = 1;
		loading = true;
		try {
			[stats, logs] = await Promise.all([
				teamRequest<UsageStatistics>(
					`${base}?${query({ tz_offset: String(-new Date().getTimezoneOffset()) })}`
				),
				fetchLogs()
			]);
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
	}

	async function goTo(next: number) {
		page = next;
		try {
			logs = await fetchLogs();
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	function searchUsers() {
		clearTimeout(searchTimer);
		const match = userOptions.find((u) => optionLabel(u) === userQuery);
		if (match) return pickUser(match.id, optionLabel(match));
		searchTimer = setTimeout(async () => {
			try {
				userOptions = await teamRequest(
					`/team/admin/users?limit=20&query=${encodeURIComponent(userQuery)}`
				);
			} catch {
				userOptions = [];
			}
		}, 250);
	}
	const optionLabel = (u: { name?: string | null; org?: string | null }) =>
		u.org ? `${u.name ?? ''} (${u.org})` : (u.name ?? '');

	function pickUser(id: string, label: string) {
		userId = id;
		userLabel = label;
		userQuery = label;
		reload();
	}

	function clearUser() {
		userId = userLabel = userQuery = '';
		reload();
	}

	async function downloadReport() {
		downloading = true;
		try {
			const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
			const response = await fetch(
				`${WEBUI_API_BASE_URL}/team/admin/usage/report?month=${reportMonth}&tz=${encodeURIComponent(tz)}`,
				{ headers: { Authorization: `Bearer ${localStorage.token}` } }
			);
			if (!response.ok) {
				const body = await response.json().catch(() => null);
				throw new Error(body?.detail ?? `HTTP ${response.status}`);
			}
			const url = URL.createObjectURL(await response.blob());
			const link = document.createElement('a');
			link.href = url;
			link.download = `usage_report_${reportMonth}.md`;
			link.click();
			URL.revokeObjectURL(url);
		} catch (e) {
			toast.error(e instanceof Error ? e.message : String(e));
		} finally {
			downloading = false;
		}
	}

	// Every day of the range, so quiet days show as gaps instead of disappearing.
	$: days = (() => {
		if (!startDate || !endDate) return [] as string[];
		const out: string[] = [];
		const cursor = new Date(epoch(startDate) * 1000);
		const last = new Date(epoch(endDate) * 1000);
		while (cursor <= last && out.length < 400) {
			out.push(ymd(cursor));
			cursor.setDate(cursor.getDate() + 1);
		}
		return out;
	})();
	$: byDay = Object.fromEntries((stats?.series ?? []).map((item) => [item.day, item.models]));
	$: top = (stats?.models ?? []).slice(0, TOP_MODELS).map((m) => m.model_id);
	$: rest = (stats?.models ?? []).slice(TOP_MODELS).map((m) => m.model_id);
	const pick = (models: Record<string, UsageTotals> | undefined, ids: string[], key: string) =>
		ids.reduce((sum, id) => sum + ((models?.[id] as any)?.[key] ?? 0), 0);
	$: dailyDatasets = [
		...top.map((id, i) => ({
			label: id,
			data: days.map((day) => pick(byDay[day], [id], metric)),
			backgroundColor: palette[i % palette.length],
			borderRadius: 3,
			maxBarThickness: 36
		})),
		...(rest.length
			? [
					{
						label: $i18n.t('Other models'),
						data: days.map((day) => pick(byDay[day], rest, metric)),
						backgroundColor: palette[palette.length - 1],
						borderRadius: 3,
						maxBarThickness: 36
					}
				]
			: [])
	];
	$: shareModels = (stats?.models ?? []).filter((m) => (m as any)[metric] > 0);
	$: shareDataset = [
		{
			data: [
				...shareModels.slice(0, TOP_MODELS).map((m) => (m as any)[metric]),
				...(shareModels.length > TOP_MODELS
					? [shareModels.slice(TOP_MODELS).reduce((s, m) => s + (m as any)[metric], 0)]
					: [])
			],
			backgroundColor: palette,
			borderWidth: 0
		}
	];
	$: shareLabels = [
		...shareModels.slice(0, TOP_MODELS).map((m) => m.model_id),
		...(shareModels.length > TOP_MODELS ? [$i18n.t('Other models')] : [])
	];
	$: topUsers = (stats?.users ?? []).slice(0, 10);
	$: summary = stats?.summary;
	$: pages = logs ? Math.max(1, Math.ceil(logs.total / PAGE_SIZE)) : 1;

	const seconds = (log: { created_at: number; settled_at?: number | null }) =>
		log.settled_at ? `${Math.max(0, log.settled_at - log.created_at)}s` : '—';
	const stateClass: Record<string, string> = {
		settled: 'text-emerald-700 dark:text-emerald-400',
		reserved: 'text-blue-700 dark:text-blue-400',
		released: 'text-gray-500',
		pending: 'text-amber-700 dark:text-amber-400'
	};
	$: stateLabel = {
		settled: $i18n.t('Settled'),
		reserved: $i18n.t('In progress'),
		released: $i18n.t('Not sent'),
		pending: $i18n.t('Awaiting settlement')
	} as Record<string, string>;

	onMount(() => {
		const now = new Date();
		reportMonth = ymd(new Date(now.getFullYear(), now.getMonth() - 1, 1)).slice(0, 7);
		choose('7d');
	});
</script>

<div class="space-y-6">
	<div class="flex flex-wrap items-end gap-3">
		<div class="flex flex-wrap gap-1 rounded-xl bg-gray-100 p-1 text-sm dark:bg-gray-850">
			{#each presets as [id, label]}
				<button
					class="rounded-lg px-3 py-1.5 {preset === id
						? 'bg-white font-medium shadow-sm dark:bg-gray-700'
						: 'text-gray-500'}"
					on:click={() => choose(id)}>{label}</button
				>
			{/each}
		</div>
		<label class="text-xs text-gray-500"
			>{$i18n.t('From')}<input
				type="date"
				class="field mt-1 block"
				bind:value={startDate}
				on:change={() => ((preset = ''), reload())}
			/></label
		>
		<label class="text-xs text-gray-500"
			>{$i18n.t('To')}<input
				type="date"
				class="field mt-1 block"
				bind:value={endDate}
				on:change={() => ((preset = ''), reload())}
			/></label
		>
		<label class="text-xs text-gray-500"
			>{$i18n.t('Model')}<select
				class="field mt-1 block max-w-56"
				bind:value={modelId}
				on:change={reload}
			>
				<option value="">{$i18n.t('All models')}</option>
				{#each stats?.models ?? [] as model}<option value={model.model_id}>{model.model_id}</option
					>{/each}
				{#if modelId && !(stats?.models ?? []).some((m) => m.model_id === modelId)}<option
						value={modelId}>{modelId}</option
					>{/if}
			</select></label
		>
		{#if admin}
			<label class="text-xs text-gray-500"
				>{$i18n.t('Member')}<span class="mt-1 flex items-center gap-1"
					><input
						class="field w-56"
						list="usage-members"
						placeholder={$i18n.t('All members')}
						bind:value={userQuery}
						on:input={searchUsers}
					/>{#if userId}<button
							class="px-2 text-gray-500"
							aria-label={$i18n.t('Clear')}
							on:click={clearUser}>✕</button
						>{/if}</span
				>
				<datalist id="usage-members">
					{#each userOptions as option}<option value={optionLabel(option)}></option>{/each}
				</datalist></label
			>
		{/if}
		<button
			class="rounded-xl bg-gray-100 px-4 py-2 text-sm disabled:opacity-50 dark:bg-gray-800"
			disabled={loading}
			on:click={reload}>{$i18n.t('Refresh')}</button
		>
	</div>

	{#if error}<p
			role="alert"
			class="rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{error}
		</p>{/if}

	<dl class="grid grid-cols-2 gap-3 md:grid-cols-4">
		{#each [[$i18n.t('Total cost'), usd(summary?.cost)], [$i18n.t('Requests'), quotaNumber(summary?.requests)], [$i18n.t('Total tokens'), compactNumber(summary?.total_tokens)], [$i18n.t('Average per request'), usd(summary?.requests ? summary.cost / summary.requests : 0, 4)]] as [label, value]}
			<div class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
				<dt class="text-xs text-gray-500">{label}</dt>
				<dd class="mt-1 text-2xl font-semibold tracking-tight">{value}</dd>
			</div>
		{/each}
	</dl>
	{#if summary}
		<p class="-mt-3 text-xs text-gray-500">
			{$i18n.t('Input')}
			{compactNumber(summary.prompt_tokens)} · {$i18n.t('Output')}
			{compactNumber(summary.completion_tokens)} · {$i18n.t('Cache read')}
			{compactNumber(summary.cache_read_tokens)} · {$i18n.t('Cache write')}
			{compactNumber(summary.cache_write_tokens)}
		</p>
	{/if}

	<section class="grid gap-4 lg:grid-cols-3">
		<div class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800 lg:col-span-2">
			<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
				<h3 class="text-sm font-medium">{$i18n.t('Daily usage by model')}</h3>
				<div class="flex gap-1 text-xs">
					{#each metrics as [id, label]}<button
							class="rounded-lg px-2.5 py-1 {metric === id
								? 'bg-gray-100 font-medium dark:bg-gray-800'
								: 'text-gray-500'}"
							on:click={() => (metric = id)}>{label}</button
						>{/each}
				</div>
			</div>
			<UsageChart
				label={$i18n.t('Daily usage by model')}
				labels={days.map((day) => day.slice(5))}
				datasets={dailyDatasets}
				stacked
				format={formatMetric}
			/>
		</div>
		<div class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
			<h3 class="mb-3 text-sm font-medium">{$i18n.t('Share by model')}</h3>
			{#if shareModels.length}
				<UsageChart
					type="doughnut"
					label={$i18n.t('Share by model')}
					labels={shareLabels}
					datasets={shareDataset}
					format={formatMetric}
				/>
			{:else}<p class="py-24 text-center text-sm text-gray-500">{$i18n.t('No usage yet')}</p>{/if}
		</div>
	</section>

	{#if admin}
		<section class="grid gap-4 lg:grid-cols-2">
			<div class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
				<h3 class="mb-3 text-sm font-medium">{$i18n.t('Top members by cost')}</h3>
				{#if topUsers.length}
					<UsageChart
						horizontal
						label={$i18n.t('Top members by cost')}
						labels={topUsers.map((u) => u.name || u.user_id)}
						datasets={[
							{
								label: $i18n.t('Cost'),
								data: topUsers.map((u) => u.cost),
								backgroundColor: palette[0],
								borderRadius: 3,
								maxBarThickness: 18
							}
						]}
						format={(value) => usd(value)}
					/>
				{:else}<p class="py-24 text-center text-sm text-gray-500">
						{$i18n.t('No usage yet')}
					</p>{/if}
			</div>
			<div class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
				<h3 class="mb-3 text-sm font-medium">{$i18n.t('Members')}</h3>
				<div class="max-h-72 overflow-auto">
					<table class="w-full text-left text-sm">
						<thead class="sticky top-0 bg-white text-xs text-gray-500 dark:bg-gray-900"
							><tr
								><th class="py-2 pr-3 font-normal">{$i18n.t('Member')}</th><th
									class="py-2 pr-3 text-right font-normal">{$i18n.t('Cost')}</th
								><th class="py-2 pr-3 text-right font-normal">{$i18n.t('Tokens')}</th><th
									class="py-2 text-right font-normal">{$i18n.t('Requests')}</th
								></tr
							></thead
						>
						<tbody>
							{#each stats?.users ?? [] as member}
								<tr class="border-t border-gray-100 dark:border-gray-800">
									<td class="max-w-48 truncate py-2 pr-3"
										><button
											class="truncate text-left hover:underline"
											title={member.org ?? member.user_id}
											on:click={() =>
												pickUser(
													member.user_id,
													optionLabel({ ...member, name: member.name ?? member.user_id })
												)}>{member.name || member.user_id}</button
										><span class="block truncate text-xs text-gray-500"
											>{member.org ?? $i18n.t('Local account')}</span
										></td
									>
									<td class="py-2 pr-3 text-right tabular-nums">{usd(member.cost)}</td>
									<td class="py-2 pr-3 text-right tabular-nums"
										>{compactNumber(member.total_tokens)}</td
									>
									<td class="py-2 text-right tabular-nums">{quotaNumber(member.requests)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>
		</section>
	{/if}

	<section>
		<div class="mb-3 flex flex-wrap items-center justify-between gap-2">
			<h3 class="text-base font-medium">
				{$i18n.t('Request log')}
				{#if logs}<span class="ml-1 text-sm font-normal text-gray-500"
						>{quotaNumber(logs.total)}</span
					>{/if}
			</h3>
			<select class="field max-w-40 text-sm" bind:value={state} on:change={() => goTo(1)}>
				<option value="">{$i18n.t('All statuses')}</option>
				{#each Object.entries(stateLabel) as [id, label]}<option value={id}>{label}</option>{/each}
			</select>
		</div>
		<div class="overflow-x-auto">
			<table class="w-full text-left text-sm">
				<thead class="text-xs text-gray-500"
					><tr>
						<th class="whitespace-nowrap pb-2 pr-4 font-normal">{$i18n.t('Time')}</th>
						{#if admin}<th class="pb-2 pr-4 font-normal">{$i18n.t('Member')}</th>{/if}
						<th class="pb-2 pr-4 font-normal">{$i18n.t('Model')}</th>
						<th class="whitespace-nowrap pb-2 pr-4 text-right font-normal">{$i18n.t('Input')}</th>
						<th class="whitespace-nowrap pb-2 pr-4 text-right font-normal">{$i18n.t('Output')}</th>
						<th class="whitespace-nowrap pb-2 pr-4 text-right font-normal"
							>{$i18n.t('Cache read / write')}</th
						>
						<th class="whitespace-nowrap pb-2 pr-4 text-right font-normal">{$i18n.t('Cost')}</th>
						<th class="whitespace-nowrap pb-2 pr-4 text-right font-normal">{$i18n.t('Duration')}</th
						>
						<th class="pb-2 font-normal">{$i18n.t('Status')}</th>
					</tr></thead
				>
				<tbody>
					{#each logs?.items ?? [] as log (log.id)}
						<tr class="border-t border-gray-100 align-top dark:border-gray-800">
							<td class="whitespace-nowrap py-2.5 pr-4 tabular-nums">{quotaDate(log.created_at)}</td
							>
							{#if admin}<td class="max-w-36 truncate py-2.5 pr-4" title={log.org ?? log.user_id}
									>{log.name || log.user_id}<span class="block truncate text-xs text-gray-500"
										>{log.org ?? $i18n.t('Local account')}</span
									></td
								>{/if}
							<td class="max-w-56 truncate py-2.5 pr-4" title={log.model_id}
								>{log.model_id}{#if log.tier}<span class="ml-1 text-xs text-gray-500"
										>{log.tier}</span
									>{/if}</td
							>
							<td class="py-2.5 pr-4 text-right tabular-nums"
								>{log.prompt_tokens != null ? quotaNumber(log.prompt_tokens) : '—'}</td
							>
							<td class="py-2.5 pr-4 text-right tabular-nums"
								>{log.completion_tokens != null ? quotaNumber(log.completion_tokens) : '—'}</td
							>
							<td class="whitespace-nowrap py-2.5 pr-4 text-right tabular-nums text-gray-500"
								>{log.cache_read_tokens || log.cache_write_tokens
									? `${quotaNumber(log.cache_read_tokens)} / ${quotaNumber(log.cache_write_tokens)}`
									: '—'}</td
							>
							<td class="whitespace-nowrap py-2.5 pr-4 text-right tabular-nums"
								>{log.cost != null
									? usd(log.cost, 4)
									: '—'}{#if log.unit === 'requests' && log.state === 'settled'}<span
										class="block text-xs text-gray-500">{$i18n.t('1 request')}</span
									>{/if}</td
							>
							<td class="py-2.5 pr-4 text-right tabular-nums text-gray-500">{seconds(log)}</td>
							<td class="py-2.5"
								><span class={stateClass[log.state] ?? ''}
									>{stateLabel[log.state] ?? log.state}</span
								>{#if log.detail}<details class="mt-1 max-w-60 text-xs text-gray-500">
										<summary>{$i18n.t('Details')}</summary>{log.detail}
									</details>{/if}</td
							>
						</tr>
					{:else}
						<tr
							><td colspan={admin ? 9 : 8} class="py-10 text-center text-sm text-gray-500"
								>{$i18n.t('No requests in this range.')}</td
							></tr
						>
					{/each}
				</tbody>
			</table>
		</div>
		{#if logs && pages > 1}
			<div class="mt-3 flex items-center justify-end gap-2 text-sm">
				<button
					class="rounded-lg px-3 py-1.5 disabled:opacity-40"
					disabled={page <= 1}
					on:click={() => goTo(page - 1)}>{$i18n.t('Previous')}</button
				>
				<span class="tabular-nums text-gray-500">{page} / {pages}</span>
				<button
					class="rounded-lg px-3 py-1.5 disabled:opacity-40"
					disabled={page >= pages}
					on:click={() => goTo(page + 1)}>{$i18n.t('Next')}</button
				>
			</div>
		{/if}
		<p class="mt-2 text-xs text-gray-500">
			{$i18n.t(
				'Request details are kept for 90 days; charts and monthly reports keep full history. Costs use the model price in effect when each request was sent.'
			)}
		</p>
	</section>

	{#if admin}
		<section class="rounded-2xl border border-gray-200 p-4 dark:border-gray-800">
			<h3 class="text-sm font-medium">{$i18n.t('Monthly report')}</h3>
			<p class="mt-1 text-xs text-gray-500">
				{$i18n.t(
					'A Markdown report with the leader board and each member’s usage by model and busiest days. Emails are masked.'
				)}
			</p>
			<div class="mt-3 flex flex-wrap items-center gap-2">
				<input type="month" class="field w-44" bind:value={reportMonth} />
				<button class="primary" disabled={downloading || !reportMonth} on:click={downloadReport}
					>{$i18n.t('Download report')}</button
				>
			</div>
		</section>
	{/if}
</div>

<style>
	@reference "../../../../tailwind.css";
	.field {
		@apply min-w-0 rounded-xl border border-gray-200 bg-transparent px-3 py-2 text-sm dark:border-gray-700;
	}
	.primary {
		@apply shrink-0 rounded-xl bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900;
	}
</style>
