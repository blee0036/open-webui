<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import Modal from '$lib/components/common/Modal.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import {
		teamRequest,
		saveTeam,
		quotaAmount,
		quotaDate,
		quotaStored,
		type QuotaPlan
	} from '$lib/apis/team';
	import {
		card,
		field,
		primary,
		secondary,
		link,
		th,
		td,
		tableHead,
		row,
		tag,
		errorText,
		toTimestamp
	} from './ui';

	const i18n = getContext<any>('i18n');
	const PAGE_SIZES = [10, 20, 50, 100];

	type Member = {
		id: string;
		name: string;
		email: string;
		role: string;
		org?: string | null;
		enabled?: boolean;
		configured?: boolean;
		plan?: QuotaPlan;
		inherited?: boolean;
		period?: {
			base: number;
			bonus: number;
			used: number;
			reserved: number;
			starts_at: number;
			ends_at: number | null;
		};
		available?: number;
		overage?: number;
		pending_plan?: QuotaPlan | null;
		pending_at?: number | null;
		requests?: number;
		error?: any;
	};

	let plans: (QuotaPlan & { default?: boolean })[] = [];
	let items: Member[] = [];
	let total = 0;
	let page = 1;
	let pageSize = 20;
	let query = '';
	let planFilter = '';
	let loading = false;
	let busy = false;

	$: pages = Math.max(1, Math.ceil(total / pageSize));
	$: activePlans = plans.filter((plan) => !plan.archived);

	async function load(target = page) {
		loading = true;
		try {
			const params = new URLSearchParams({
				query,
				page: String(target),
				page_size: String(pageSize),
				...(planFilter ? { plan_id: planFilter } : {})
			});
			const result = await teamRequest(`/team/admin/members?${params}`);
			items = result.items;
			total = result.total;
			page = target;
		} catch (e) {
			toast.error(errorText(e));
		} finally {
			loading = false;
		}
	}

	async function loadPlans() {
		try {
			plans = await teamRequest('/team/admin/plans');
		} catch {
			plans = [];
		}
	}

	export const refresh = () => Promise.all([loadPlans(), load()]);

	const totalOf = (m: Member) => (m.period ? m.period.base + m.period.bonus : 0);
	const spentOf = (m: Member) => (m.period ? m.period.used + m.period.reserved : 0);
	const percent = (m: Member) =>
		totalOf(m) > 0 ? Math.min(100, (spentOf(m) / totalOf(m)) * 100) : spentOf(m) > 0 ? 100 : 0;
	const barTone = (value: number) =>
		value >= 100 ? 'bg-red-500' : value >= 80 ? 'bg-amber-500' : 'bg-green-500';
	const amount = (value: number | undefined, unit?: string) =>
		unit === 'credits' ? quotaAmount(value, unit) : (value ?? 0).toLocaleString();
	const signed = (value: number, unit?: string) =>
		(value > 0 ? '+' : '-') + amount(Math.abs(value), unit);

	// ---- adjust quota ----------------------------------------------------------
	let adjusting: Member | null = null;
	let showAdjust = false;
	let direction: 'add' | 'deduct' = 'add';
	let adjustValue: number | null = null;
	let adjustNote = '';
	let operation: { key: string; id: string } | null = null;
	let history: any[] = [];

	$: adjustUnit = adjusting?.plan?.unit;
	$: delta =
		adjustValue && adjustValue > 0
			? quotaStored(adjustValue, adjustUnit) * (direction === 'deduct' ? -1 : 1)
			: 0;
	$: afterAvailable = adjusting?.period ? totalOf(adjusting) + delta - spentOf(adjusting) : 0;
	$: quickAmounts = adjustUnit === 'credits' ? [1, 5, 10, 50] : [100, 500, 1000, 5000];

	async function openAdjust(member: Member) {
		adjusting = member;
		direction = 'add';
		adjustValue = null;
		adjustNote = '';
		operation = null;
		history = [];
		showAdjust = true;
		try {
			history = (await teamRequest(`/team/admin/audit?user_id=${member.id}&limit=5`)) ?? [];
		} catch {
			history = [];
		}
	}

	async function submitAdjust() {
		if (!adjusting || !delta || busy) return;
		busy = true;
		const key = JSON.stringify([adjusting.id, delta, adjustNote]);
		if (operation?.key !== key) operation = { key, id: crypto.randomUUID() };
		try {
			await saveTeam(
				`/team/admin/users/${adjusting.id}/bonus`,
				{ amount: delta, reason: adjustNote, operation_id: operation.id },
				'POST'
			);
			operation = null;
			toast.success($i18n.t('Quota adjusted'));
			showAdjust = false;
			await load();
		} catch (e) {
			toast.error(errorText(e));
		} finally {
			busy = false;
		}
	}

	// ---- change package ----------------------------------------------------------
	let assigning: Member | null = null;
	let showAssign = false;
	let assigned = '';
	let assignEffective = '';
	let assignNote = '';

	$: target = plans.find((plan) => plan.id === (assigned || plans.find((p) => p.default)?.id));
	$: changesRule =
		!!assigning?.plan &&
		!!target &&
		(target.unit !== assigning.plan.unit ||
			JSON.stringify(target.reset) !== JSON.stringify(assigning.plan.reset));
	$: needsTime = changesRule && assigning?.period?.ends_at == null;

	function openAssign(member: Member) {
		assigning = member;
		assigned = member.inherited ? '' : (member.plan?.id ?? '');
		assignEffective = '';
		assignNote = '';
		showAssign = true;
	}

	async function submitAssign() {
		if (!assigning || busy) return;
		busy = true;
		try {
			await saveTeam(`/team/admin/users/${assigning.id}/plan`, {
				plan_id: assigned || null,
				effective_at: toTimestamp(assignEffective),
				reason: assignNote
			});
			toast.success($i18n.t('Package updated'));
			showAssign = false;
			await Promise.all([load(), loadPlans()]);
		} catch (e) {
			toast.error(errorText(e));
		} finally {
			busy = false;
		}
	}

	const historyLabel = (entry: any, unit?: string) =>
		entry.action === 'bonus'
			? signed(entry.data?.amount ?? 0, unit)
			: entry.action === 'assign'
				? $i18n.t('Package changed')
				: $i18n.t(entry.action);

	onMount(refresh);
</script>

<section class={card}>
	<form
		class="flex flex-wrap items-center gap-2 px-5 py-4"
		on:submit|preventDefault={() => load(1)}
	>
		<input
			class="{field} w-full sm:w-72"
			bind:value={query}
			placeholder={$i18n.t('Search name, email or enterprise ID')}
		/>
		<select class="{field} w-full sm:w-48" bind:value={planFilter} on:change={() => load(1)}>
			<option value="">{$i18n.t('All packages')}</option>
			<option value="personal">{$i18n.t('Assigned personally')}</option>
			<option value="inherited">{$i18n.t('Using the default')}</option>
			{#each plans as plan}
				<option value={plan.id}>{plan.name}</option>
			{/each}
		</select>
		<button class={primary} disabled={loading}>{$i18n.t('Search')}</button>
		<button
			type="button"
			class={secondary}
			disabled={loading}
			on:click={() => {
				query = '';
				planFilter = '';
				load(1);
			}}>{$i18n.t('Reset')}</button
		>
		<span class="ml-auto text-xs text-gray-500"
			>{$i18n.t('{{number}} members', { number: total.toLocaleString() })}</span
		>
	</form>

	<div class="overflow-x-auto">
		<table class="w-full text-sm">
			<thead class={tableHead}>
				<tr>
					<th class={th}>{$i18n.t('Member')}</th>
					<th class={th}>{$i18n.t('Package')}</th>
					<th class="{th} min-w-52">{$i18n.t('This period')}</th>
					<th class="{th} text-right">{$i18n.t('Remaining')}</th>
					<th class="{th} text-right">{$i18n.t('Requests')}</th>
					<th class={th}>{$i18n.t('Resets')}</th>
					<th class="{th} text-right">{$i18n.t('Actions')}</th>
				</tr>
			</thead>
			<tbody class={loading ? 'opacity-60' : ''}>
				{#each items as member (member.id)}
					{@const unit = member.plan?.unit}
					<tr class={row}>
						<td class={td}>
							<div class="flex items-center gap-1.5">
								<span class="font-medium">{member.name}</span>
								{#if member.role === 'admin'}<span class={tag('red')}>{$i18n.t('Admin')}</span>{/if}
							</div>
							<div class="max-w-56 truncate text-xs text-gray-500" title={member.email}>
								{member.org ?? $i18n.t('Local account')}
							</div>
						</td>
						<td class={td}>
							{#if member.plan}
								<div class="flex items-center gap-1.5">
									<span>{member.plan.name}</span>
									<span class={tag(member.inherited ? 'gray' : 'blue')}
										>{member.inherited ? $i18n.t('Default') : $i18n.t('Personal')}</span
									>
								</div>
								{#if member.pending_plan}
									<div class="mt-0.5 text-xs text-amber-600 dark:text-amber-400">
										{$i18n.t('→ {{name}} from {{time}}', {
											name: member.pending_plan.name,
											time: quotaDate(member.pending_at)
										})}
									</div>
								{/if}
							{:else}
								<span class="text-xs text-gray-500"
									>{member.error?.message ?? member.error ?? $i18n.t('No package')}</span
								>
							{/if}
						</td>
						<td class={td}>
							{#if member.period}
								{@const value = percent(member)}
								<div class="flex items-center justify-between text-xs tabular-nums">
									<span>{amount(spentOf(member), unit)} / {amount(totalOf(member), unit)}</span>
									<span class="text-gray-500">{value.toFixed(0)}%</span>
								</div>
								<div
									class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800"
								>
									<div class="h-full rounded-full {barTone(value)}" style="width: {value}%"></div>
								</div>
								{#if member.period.bonus}
									<div class="mt-1 text-xs text-gray-500">
										{$i18n.t('Temporary {{amount}}', { amount: signed(member.period.bonus, unit) })}
									</div>
								{/if}
							{:else}
								<span class="text-xs text-gray-500">—</span>
							{/if}
						</td>
						<td class="{td} text-right tabular-nums">
							{#if member.overage}
								<span class="font-medium text-red-600 dark:text-red-400"
									>-{amount(member.overage, unit)}</span
								>
							{:else if member.period}
								<span
									class="font-medium {member.available
										? 'text-green-700 dark:text-green-400'
										: 'text-gray-500'}">{amount(member.available, unit)}</span
								>
							{:else}—{/if}
						</td>
						<td class="{td} text-right tabular-nums">{(member.requests ?? 0).toLocaleString()}</td>
						<td class="{td} whitespace-nowrap text-xs text-gray-500">
							{member.period
								? member.period.ends_at
									? quotaDate(member.period.ends_at)
									: $i18n.t('Never resets')
								: '—'}
						</td>
						<td class="{td} text-right">
							<div class="flex justify-end gap-3">
								<button class={link} disabled={!member.period} on:click={() => openAdjust(member)}
									>{$i18n.t('Adjust quota')}</button
								>
								<button class={link} on:click={() => openAssign(member)}
									>{$i18n.t('Change package')}</button
								>
							</div>
						</td>
					</tr>
				{:else}
					<tr>
						<td colspan="7" class="px-4 py-10 text-center text-sm text-gray-500">
							{loading ? $i18n.t('Loading...') : $i18n.t('No members found')}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	<div
		class="flex flex-wrap items-center justify-end gap-3 border-t border-gray-100 px-5 py-3 text-sm dark:border-gray-850"
	>
		<select
			class="{field} w-auto py-1.5"
			bind:value={pageSize}
			on:change={() => load(1)}
			aria-label={$i18n.t('Page size')}
		>
			{#each PAGE_SIZES as size}
				<option value={size}>{$i18n.t('{{size}} / page', { size: size })}</option>
			{/each}
		</select>
		<button
			class="{secondary} py-1.5"
			disabled={page <= 1 || loading}
			on:click={() => load(page - 1)}>{$i18n.t('Previous')}</button
		>
		<span class="tabular-nums text-gray-500">{page} / {pages}</span>
		<button
			class="{secondary} py-1.5"
			disabled={page >= pages || loading}
			on:click={() => load(page + 1)}>{$i18n.t('Next')}</button
		>
	</div>
</section>

{#if adjusting}
	<Modal bind:show={showAdjust} size="sm" ariaLabel={$i18n.t('Adjust quota')}>
		<form class="p-5 dark:text-gray-200" on:submit|preventDefault={submitAdjust}>
			<div class="mb-4 flex items-center justify-between">
				<div>
					<h3 class="text-base font-medium">{$i18n.t('Adjust quota')}</h3>
					<p class="text-xs text-gray-500">{adjusting.name} · {adjusting.plan?.name}</p>
				</div>
				<button
					type="button"
					aria-label={$i18n.t('Close')}
					class="text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
					on:click={() => (showAdjust = false)}><XMark className="size-4" /></button
				>
			</div>
			<div class="mb-4 grid grid-cols-3 gap-2 text-center">
				{#each [[$i18n.t('Total'), totalOf(adjusting)], [$i18n.t('Used'), spentOf(adjusting)], [$i18n.t('Remaining'), totalOf(adjusting) - spentOf(adjusting)]] as [label, value]}
					<div class="rounded-xl bg-gray-50 px-2 py-2.5 dark:bg-gray-850/50">
						<div class="text-xs text-gray-500">{label}</div>
						<div class="mt-0.5 font-medium tabular-nums {Number(value) < 0 ? 'text-red-600' : ''}">
							{Number(value) < 0 ? '-' : ''}{amount(Math.abs(Number(value)), adjustUnit)}
						</div>
					</div>
				{/each}
			</div>
			<div class="mb-3 grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 text-sm dark:bg-gray-850">
				{#each [['add', $i18n.t('Add')], ['deduct', $i18n.t('Deduct')]] as [value, label]}
					<button
						type="button"
						class="rounded-md py-1.5 transition {direction === value
							? 'bg-white font-medium shadow-sm dark:bg-gray-700'
							: 'text-gray-500'}"
						on:click={() => (direction = value === 'add' ? 'add' : 'deduct')}>{label}</button
					>
				{/each}
			</div>
			<div class="relative">
				{#if adjustUnit === 'credits'}<span
						class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
						>$</span
					>{/if}
				<input
					class="{field} {adjustUnit === 'credits' ? 'pl-7' : ''}"
					type="number"
					min={adjustUnit === 'credits' ? '0.01' : '1'}
					step={adjustUnit === 'credits' ? '0.01' : '1'}
					placeholder={adjustUnit === 'credits' ? $i18n.t('Amount in USD') : $i18n.t('Requests')}
					required
					bind:value={adjustValue}
				/>
			</div>
			<div class="mt-2 flex flex-wrap gap-1.5">
				{#each quickAmounts as value}
					<button
						type="button"
						class="rounded-md border border-gray-200 px-2 py-1 text-xs transition hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-850"
						on:click={() => (adjustValue = value)}
						>{adjustUnit === 'credits' ? `$${value}` : value.toLocaleString()}</button
					>
				{/each}
			</div>
			<input
				class="{field} mt-3"
				maxlength="500"
				placeholder={$i18n.t('Note (optional)')}
				bind:value={adjustNote}
			/>
			{#if delta}
				<p class="mt-3 text-sm">
					{$i18n.t('Remaining after adjustment')}:
					<span class="font-medium tabular-nums {afterAvailable < 0 ? 'text-red-600' : ''}"
						>{afterAvailable < 0 ? '-' : ''}{amount(Math.abs(afterAvailable), adjustUnit)}</span
					>
				</p>
			{/if}
			<p class="mt-2 text-xs text-gray-500">
				{$i18n.t('Adjustments apply to the current period only and expire at the next reset.')}
			</p>
			{#if history.length}
				<div class="mt-4 border-t border-gray-100 pt-3 dark:border-gray-850">
					<div class="mb-1.5 text-xs text-gray-500">{$i18n.t('Recent changes')}</div>
					{#each history as entry}
						<div class="flex justify-between gap-3 py-1 text-xs">
							<span class="truncate"
								>{historyLabel(entry, adjustUnit)}{entry.data?.reason
									? ` · ${entry.data.reason}`
									: ''}</span
							>
							<span class="shrink-0 text-gray-500">{quotaDate(entry.created_at)}</span>
						</div>
					{/each}
				</div>
			{/if}
			<div class="mt-5 flex justify-end gap-2">
				<button type="button" class={secondary} on:click={() => (showAdjust = false)}
					>{$i18n.t('Cancel')}</button
				>
				<button class={primary} disabled={busy || !delta}>{$i18n.t('Confirm')}</button>
			</div>
		</form>
	</Modal>
{/if}

{#if assigning}
	<Modal bind:show={showAssign} size="sm" ariaLabel={$i18n.t('Change package')}>
		<form class="p-5 dark:text-gray-200" on:submit|preventDefault={submitAssign}>
			<div class="mb-4 flex items-center justify-between">
				<div>
					<h3 class="text-base font-medium">{$i18n.t('Change package')}</h3>
					<p class="text-xs text-gray-500">
						{assigning.name} · {$i18n.t('Current')}: {assigning.plan?.name ?? '—'}
					</p>
				</div>
				<button
					type="button"
					aria-label={$i18n.t('Close')}
					class="text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
					on:click={() => (showAssign = false)}><XMark className="size-4" /></button
				>
			</div>
			<div class="space-y-1.5">
				{#each [{ id: '', name: $i18n.t('Use the default package'), plan: plans.find((p) => p.default) }, ...activePlans.map( (plan) => ({ id: plan.id, name: plan.name, plan }) )] as option (option.id)}
					<label
						class="flex cursor-pointer items-center gap-3 rounded-xl border px-3 py-2.5 text-sm transition {assigned ===
						option.id
							? 'border-gray-900 dark:border-gray-100'
							: 'border-gray-200 dark:border-gray-700'}"
					>
						<input type="radio" class="sr-only" bind:group={assigned} value={option.id} />
						<div class="flex-1">
							<div class="font-medium">{option.name}</div>
							{#if option.plan}
								<div class="text-xs text-gray-500">
									{option.id === '' ? option.plan.name + ' · ' : ''}{option.plan.unit === 'credits'
										? quotaAmount(option.plan.allowance, 'credits')
										: $i18n.t('{{amount}} requests', {
												amount: option.plan.allowance.toLocaleString()
											})}
								</div>
							{/if}
						</div>
						{#if assigned === option.id}<span class={tag('blue')}>{$i18n.t('Selected')}</span>{/if}
					</label>
				{/each}
			</div>
			{#if changesRule}
				<p
					class="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200"
				>
					{needsTime
						? $i18n.t('The current package never resets, so choose when the new package starts.')
						: $i18n.t(
								'Different metering or reset rule: the new package starts at the end of the current period.'
							)}
				</p>
				{#if needsTime}
					<input class="{field} mt-2" type="datetime-local" required bind:value={assignEffective} />
				{/if}
			{/if}
			<input
				class="{field} mt-3"
				maxlength="500"
				placeholder={$i18n.t('Note (optional)')}
				bind:value={assignNote}
			/>
			<div class="mt-5 flex justify-end gap-2">
				<button type="button" class={secondary} on:click={() => (showAssign = false)}
					>{$i18n.t('Cancel')}</button
				>
				<button class={primary} disabled={busy}>{$i18n.t('Save')}</button>
			</div>
		</form>
	</Modal>
{/if}
