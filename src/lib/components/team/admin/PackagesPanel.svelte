<script lang="ts">
	import { createEventDispatcher, getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import Modal from '$lib/components/common/Modal.svelte';
	import Switch from '$lib/components/common/Switch.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';
	import {
		teamRequest,
		saveTeam,
		quotaAmount,
		quotaInput,
		quotaStored,
		CREDITS_PER_USD,
		type QuotaPlan
	} from '$lib/apis/team';
	import { formatBytes, GB } from '$lib/utils/storageQuota';
	import { getBackendConfig } from '$lib/apis';
	import { config } from '$lib/stores';
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
	const dispatch = createEventDispatcher();

	type Plan = QuotaPlan & {
		default?: boolean;
		members?: number;
		personal_members?: number;
		created_at?: number;
		updated_at?: number;
	};

	let plans: Plan[] = [];
	let settings: any = { enabled: false, quota_enabled: false, default_plan_id: null };
	let effective = '';
	let busy = false;
	let error = '';

	let showEditor = false;
	let editorId = '';
	let editor: any = null;
	let note = '';
	const TIMEZONES = [
		'Asia/Shanghai',
		'Asia/Singapore',
		'Asia/Tokyo',
		'Asia/Hong_Kong',
		'Europe/London',
		'America/New_York',
		'America/Los_Angeles',
		'UTC'
	];
	const blankPlan = () => ({
		name: '',
		unit: 'credits',
		allowance: 10 * CREDITS_PER_USD,
		reset: {
			kind: 'month',
			timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
			days: 30
		},
		archived: false,
		storage_bytes: null as number | null,
		sandbox_bytes: null as number | null
	});
	// Storage caps are entered in GB; an empty field is unlimited.
	const gbInput = (bytes: number | null | undefined) =>
		bytes === null || bytes === undefined ? '' : Number((bytes / GB).toFixed(3));
	const gbStored = (value: string) =>
		value.trim() === '' ? null : Math.max(0, Math.round(Number(value) * GB));
	const storageLabel = (bytes: number | null | undefined) =>
		bytes === null || bytes === undefined ? $i18n.t('Unlimited') : formatBytes(bytes);

	$: active = plans.filter((plan) => !plan.archived);
	// A package that never resets only switches at an explicit time.
	$: needsEffective = plans.some((plan) => plan.reset.kind === 'never');

	const resetLabel = (reset: QuotaPlan['reset']) =>
		reset.kind === 'days'
			? $i18n.t('Every {{days}} days', { days: reset.days })
			: reset.kind === 'never'
				? $i18n.t('Never resets')
				: { day: $i18n.t('Daily'), week: $i18n.t('Weekly'), month: $i18n.t('Monthly') }[reset.kind];

	const allowanceLabel = (plan: QuotaPlan) =>
		plan.unit === 'credits'
			? quotaAmount(plan.allowance, plan.unit)
			: $i18n.t('{{amount}} requests', { amount: plan.allowance.toLocaleString() });

	async function load() {
		try {
			[settings, plans] = await Promise.all([
				teamRequest('/team/admin/settings'),
				teamRequest('/team/admin/plans')
			]);
			error = '';
		} catch (e) {
			error = errorText(e);
		}
	}

	async function run(work: () => Promise<unknown>, done = $i18n.t('Saved')) {
		if (busy) return false;
		busy = true;
		try {
			await work();
			if (done) toast.success(done);
			error = '';
			return true;
		} catch (e) {
			toast.error(errorText(e));
			return false;
		} finally {
			busy = false;
		}
	}

	const saveSettings = (patch: Record<string, unknown> = {}) =>
		run(async () => {
			await saveTeam('/team/admin/settings', {
				...settings,
				...patch,
				default_plan_id: (patch.default_plan_id ?? settings.default_plan_id) || null,
				default_effective_at: toTimestamp(effective)
			});
			await load();
			config.set(await getBackendConfig(localStorage.token));
			dispatch('change');
		});

	function openEditor(plan: Plan | null) {
		editorId = plan?.id ?? '';
		editor = plan ? structuredClone(plan) : blankPlan();
		editor.reset.days ??= 30;
		note = '';
		showEditor = true;
	}

	const savePlan = async () => {
		const { id, created_at, updated_at, default: _d, members, personal_members, ...form } = editor;
		if (form.reset.kind !== 'days') delete form.reset.days;
		const saved = await run(async () => {
			await saveTeam(
				`/team/admin/plans${editorId ? '/' + editorId : ''}`,
				{ ...form, reason: note },
				editorId ? 'PUT' : 'POST'
			);
			await load();
			dispatch('change');
		});
		if (saved) showEditor = false;
	};

	const setArchived = (plan: Plan, archived: boolean) =>
		run(async () => {
			const { id, created_at, updated_at, default: _d, members, personal_members, ...form } = plan;
			await saveTeam(`/team/admin/plans/${plan.id}`, { ...form, archived });
			await load();
			dispatch('change');
		});

	onMount(load);
</script>

{#if error}
	<p
		role="alert"
		class="mb-4 rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
	>
		{error}
	</p>
{/if}

<section class="{card} mb-5 p-5">
	<div class="mb-4 flex flex-wrap items-center justify-between gap-3">
		<div>
			<h2 class="text-base font-medium">{$i18n.t('Global settings')}</h2>
			<p class="mt-0.5 text-xs text-gray-500">
				{$i18n.t('Members without a personal package use the default package.')}
			</p>
		</div>
		<button class={primary} disabled={busy} on:click={() => saveSettings()}
			>{$i18n.t('Save configuration')}</button
		>
	</div>
	<div class="grid gap-4 md:grid-cols-3">
		<div
			class="flex items-center justify-between gap-3 rounded-xl bg-gray-50 px-4 py-3 dark:bg-gray-850/50"
		>
			<div>
				<div class="text-sm font-medium">{$i18n.t('Team workspaces')}</div>
				<div class="text-xs text-gray-500">{$i18n.t('Projects, sandboxes and sharing')}</div>
			</div>
			<Switch bind:state={settings.enabled} />
		</div>
		<div
			class="flex items-center justify-between gap-3 rounded-xl bg-gray-50 px-4 py-3 dark:bg-gray-850/50"
		>
			<div>
				<div class="text-sm font-medium">{$i18n.t('Model quota')}</div>
				<div class="text-xs text-gray-500">{$i18n.t('Charge model calls against packages')}</div>
			</div>
			<Switch bind:state={settings.quota_enabled} />
		</div>
		<label class="rounded-xl bg-gray-50 px-4 py-3 text-sm dark:bg-gray-850/50">
			<div class="mb-1.5 font-medium">{$i18n.t('Default package')}</div>
			<select class={field} bind:value={settings.default_plan_id}>
				<option value={null}>{$i18n.t('Select a package')}</option>
				{#each active as plan}
					<option value={plan.id}>{plan.name} · {allowanceLabel(plan)}</option>
				{/each}
			</select>
		</label>
	</div>
	{#if needsEffective}
		<label class="mt-4 block max-w-sm text-sm">
			<span class="text-xs text-gray-500"
				>{$i18n.t('Effective time for non-resetting accounts')}</span
			>
			<input class="{field} mt-1" type="datetime-local" bind:value={effective} />
		</label>
	{/if}
</section>

<section class={card}>
	<div class="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
		<div>
			<h2 class="text-base font-medium">{$i18n.t('Packages')}</h2>
			<p class="mt-0.5 text-xs text-gray-500">
				{$i18n.t(
					'Unit and reset rule are fixed once created; temporary quota expires at each reset.'
				)}
			</p>
		</div>
		<button class={primary} on:click={() => openEditor(null)}>+ {$i18n.t('New package')}</button>
	</div>
	<div class="overflow-x-auto">
		<table class="w-full text-sm">
			<thead class={tableHead}>
				<tr>
					<th class={th}>{$i18n.t('Name')}</th>
					<th class={th}>{$i18n.t('Metering')}</th>
					<th class="{th} text-right">{$i18n.t('Allowance')}</th>
					<th class={th}>{$i18n.t('Reset')}</th>
					<th class={th}>{$i18n.t('Storage')}</th>
					<th class="{th} text-right">{$i18n.t('Members')}</th>
					<th class={th}>{$i18n.t('Status')}</th>
					<th class="{th} text-right">{$i18n.t('Actions')}</th>
				</tr>
			</thead>
			<tbody>
				{#each plans as plan (plan.id)}
					<tr class="{row} {plan.archived ? 'opacity-60' : ''}">
						<td class={td}>
							<div class="flex items-center gap-2">
								<span class="font-medium">{plan.name}</span>
								{#if plan.default}<span class={tag('blue')}>{$i18n.t('Default')}</span>{/if}
							</div>
						</td>
						<td class={td}>
							<span class={tag(plan.unit === 'credits' ? 'purple' : 'amber')}
								>{plan.unit === 'credits'
									? $i18n.t('Token credits')
									: $i18n.t('API requests')}</span
							>
						</td>
						<td class="{td} text-right tabular-nums">{allowanceLabel(plan)}</td>
						<td class={td}>
							<div>{resetLabel(plan.reset)}</div>
							<div class="text-xs text-gray-500">{plan.reset.timezone}</div>
						</td>
						<td class={td}>
							<div class="text-xs">
								{$i18n.t('Files')}: {storageLabel(plan.storage_bytes)}
							</div>
							<div class="text-xs text-gray-500">
								{$i18n.t('Sandbox')}: {storageLabel(plan.sandbox_bytes)}
							</div>
						</td>
						<td class="{td} text-right tabular-nums">
							<div>{(plan.members ?? 0).toLocaleString()}</div>
							{#if plan.default && plan.personal_members}
								<div class="text-xs text-gray-500">
									{$i18n.t('{{number}} assigned personally', { number: plan.personal_members })}
								</div>
							{/if}
						</td>
						<td class={td}>
							<span class={tag(plan.archived ? 'gray' : 'green')}
								>{plan.archived ? $i18n.t('Archived') : $i18n.t('Active')}</span
							>
						</td>
						<td class="{td} text-right">
							<div class="flex justify-end gap-3">
								<button class={link} on:click={() => openEditor(plan)}>{$i18n.t('Edit')}</button>
								{#if !plan.default && !plan.archived}
									<button
										class={link}
										disabled={busy}
										on:click={() => saveSettings({ default_plan_id: plan.id })}
										>{$i18n.t('Set as default')}</button
									>
								{/if}
								{#if !plan.default}
									<button
										class="{link} {plan.archived ? '' : '!text-red-600 dark:!text-red-400'}"
										disabled={busy}
										on:click={() => setArchived(plan, !plan.archived)}
										>{plan.archived ? $i18n.t('Restore') : $i18n.t('Archive')}</button
									>
								{/if}
							</div>
						</td>
					</tr>
				{:else}
					<tr>
						<td colspan="7" class="px-4 py-10 text-center text-sm text-gray-500">
							{$i18n.t('No packages yet. Create one, then set it as the default.')}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</section>

{#if editor}
	<Modal
		bind:show={showEditor}
		size="md"
		ariaLabel={editorId ? $i18n.t('Edit package') : $i18n.t('New package')}
	>
		<form class="p-5 dark:text-gray-200" on:submit|preventDefault={savePlan}>
			<div class="mb-4 flex items-center justify-between">
				<h3 class="text-base font-medium">
					{editorId ? $i18n.t('Edit package') : $i18n.t('New package')}
				</h3>
				<button
					type="button"
					aria-label={$i18n.t('Close')}
					class="text-gray-500 hover:text-gray-700 dark:hover:text-gray-200"
					on:click={() => (showEditor = false)}><XMark className="size-4" /></button
				>
			</div>
			<div class="space-y-4">
				<label class="block text-sm"
					><span class="text-xs text-gray-500">{$i18n.t('Name')}</span>
					<input class="{field} mt-1" bind:value={editor.name} required maxlength="100" /></label
				>
				<div>
					<div class="mb-1 text-xs text-gray-500">{$i18n.t('Metering')}</div>
					<div class="grid grid-cols-2 gap-2">
						{#each [['credits', $i18n.t('Token credits'), $i18n.t('Charged in USD at each model’s price')], ['requests', $i18n.t('API requests'), $i18n.t('Each model call counts as 1')]] as [unit, title, hint]}
							<button
								type="button"
								disabled={!!editorId}
								class="rounded-xl border px-3 py-2.5 text-left text-sm transition disabled:cursor-not-allowed {editor.unit ===
								unit
									? 'border-gray-900 dark:border-gray-100'
									: 'border-gray-200 opacity-70 dark:border-gray-700'}"
								on:click={() => {
									editor.unit = unit;
									editor.allowance = unit === 'credits' ? 10 * CREDITS_PER_USD : 1000;
								}}
							>
								<div class="font-medium">{title}</div>
								<div class="text-xs text-gray-500">{hint}</div>
							</button>
						{/each}
					</div>
				</div>
				<label class="block text-sm"
					><span class="text-xs text-gray-500"
						>{editor.unit === 'credits'
							? $i18n.t('Allowance per period (USD)')
							: $i18n.t('Requests per period')}</span
					>
					<div class="relative mt-1">
						{#if editor.unit === 'credits'}<span
								class="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-400"
								>$</span
							>{/if}
						<input
							class="{field} {editor.unit === 'credits' ? 'pl-7' : ''}"
							type="number"
							min="0"
							step={editor.unit === 'credits' ? '0.01' : '1'}
							value={quotaInput(editor.allowance, editor.unit)}
							on:input={(e) =>
								(editor.allowance = quotaStored(e.currentTarget.value || 0, editor.unit))}
							required
						/>
					</div></label
				>
				<div class="grid grid-cols-2 gap-3">
					<label class="text-sm"
						><span class="text-xs text-gray-500">{$i18n.t('Reset')}</span>
						<select class="{field} mt-1" bind:value={editor.reset.kind} disabled={!!editorId}>
							<option value="day">{$i18n.t('Daily')}</option>
							<option value="week">{$i18n.t('Weekly')}</option>
							<option value="month">{$i18n.t('Monthly')}</option>
							<option value="days">{$i18n.t('Every N days')}</option>
							<option value="never">{$i18n.t('Never resets')}</option>
						</select></label
					>
					{#if editor.reset.kind === 'days'}
						<label class="text-sm"
							><span class="text-xs text-gray-500">{$i18n.t('Interval in days')}</span>
							<input
								class="{field} mt-1"
								type="number"
								min="1"
								max="3660"
								bind:value={editor.reset.days}
								disabled={!!editorId}
								required
							/></label
						>
					{:else if editor.reset.kind !== 'never'}
						<label class="text-sm"
							><span class="text-xs text-gray-500">{$i18n.t('Timezone')}</span>
							<input
								class="{field} mt-1"
								list="team-timezones"
								bind:value={editor.reset.timezone}
								disabled={!!editorId}
								required
							/>
							<datalist id="team-timezones"
								>{#each TIMEZONES as zone}<option value={zone}></option>{/each}</datalist
							></label
						>
					{/if}
				</div>
				<div class="grid grid-cols-2 gap-3">
					<label class="text-sm"
						><span class="text-xs text-gray-500">{$i18n.t('File storage (GB)')}</span>
						<input
							class="{field} mt-1"
							type="number"
							min="0"
							step="0.1"
							placeholder={$i18n.t('Unlimited')}
							value={gbInput(editor.storage_bytes)}
							on:input={(e) => (editor.storage_bytes = gbStored(e.currentTarget.value))}
						/></label
					>
					<label class="text-sm"
						><span class="text-xs text-gray-500">{$i18n.t('Sandbox storage (GB)')}</span>
						<input
							class="{field} mt-1"
							type="number"
							min="0"
							step="0.1"
							placeholder={$i18n.t('Unlimited')}
							value={gbInput(editor.sandbox_bytes)}
							on:input={(e) => (editor.sandbox_bytes = gbStored(e.currentTarget.value))}
						/></label
					>
				</div>
				<p class="text-xs leading-5 text-gray-500">
					{$i18n.t(
						'Uploads past the file limit are refused. Sandbox storage is measured every few minutes across all workspaces of each member; past the limit, Agent commands pause until they free space. Leave empty for no limit. Administrators are exempt.'
					)}
				</p>
				{#if editorId}
					<p class="rounded-lg bg-gray-50 px-3 py-2 text-xs text-gray-500 dark:bg-gray-850/50">
						{$i18n.t(
							'Metering and reset rule cannot change after creation. Create a new package and move members to it instead.'
						)}
					</p>
				{/if}
				<label class="block text-sm"
					><span class="text-xs text-gray-500">{$i18n.t('Note (optional)')}</span>
					<input class="{field} mt-1" maxlength="500" bind:value={note} /></label
				>
			</div>
			<div class="mt-5 flex justify-end gap-2">
				<button type="button" class={secondary} on:click={() => (showEditor = false)}
					>{$i18n.t('Cancel')}</button
				>
				<button class={primary} disabled={busy}>{$i18n.t('Save package')}</button>
			</div>
		</form>
	</Modal>
{/if}
