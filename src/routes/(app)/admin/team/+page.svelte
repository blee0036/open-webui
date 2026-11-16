<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { teamRequest, saveTeam, quotaDate, quotaNumber, type QuotaPlan } from '$lib/apis/team';
	import EnterpriseAdmin from '$lib/components/team/EnterpriseAdmin.svelte';
	import PriceSync from '$lib/components/team/PriceSync.svelte';
	import { getBackendConfig } from '$lib/apis';
	import { config } from '$lib/stores';
	const i18n = getContext<any>('i18n');
	let tab = 'packages';
	let plans: QuotaPlan[] = [];
	let settings: any = { enabled: false, quota_enabled: false, default_plan_id: null };
	let error = '';
	let busy = false;
	let reason = '';
	let effective = '';
	let editorId = '';
	let planReason = '';
	const blankPlan = () => ({
		name: '',
		unit: 'requests',
		allowance: 1000,
		reset: { kind: 'month', timezone: 'Asia/Singapore', days: 30 },
		archived: false
	});
	let editor: any = blankPlan();
	let query = '';
	let users: any[] = [];
	let selectedUser: any = null;
	let userQuota: any = null;
	let assigned = '';
	let bonus = 0;
	let bonusOperation: { key: string; id: string } | null = null;
	let prices: any[] = [];
	let priceReason = '';
	let price = {
		model_id: '',
		expression: 'tier("base", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)',
		multiplier: '1'
	};
	let pending: any[] = [];
	let charges: Record<string, number> = {};
	let audits: any[] = [];
	let auditMore = false;
	async function loadAudit() {
		audits = await teamRequest('/team/admin/audit');
		auditMore = audits.length === 50;
	}
	async function load() {
		try {
			[settings, plans, prices, pending] = await Promise.all([
				teamRequest('/team/admin/settings'),
				teamRequest('/team/admin/plans'),
				teamRequest('/team/admin/prices'),
				teamRequest('/team/admin/entries?state=pending')
			]);
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
	async function searchUsers() {
		users = await teamRequest(`/team/admin/users?query=${encodeURIComponent(query)}`);
	}
	async function chooseUser(member: any) {
		selectedUser = member;
		userQuota = await teamRequest(`/team/admin/users/${member.id}/quota`);
		assigned = userQuota.inherited ? '' : (userQuota.plan?.id ?? '');
	}
	async function addBonus() {
		const key = JSON.stringify([selectedUser.id, bonus, reason]);
		if (bonusOperation?.key !== key) bonusOperation = { key, id: crypto.randomUUID() };
		await saveTeam(
			`/team/admin/users/${selectedUser.id}/bonus`,
			{ amount: bonus, reason, operation_id: bonusOperation.id },
			'POST'
		);
		bonusOperation = null;
		bonus = 0;
		await chooseUser(selectedUser);
	}
	onMount(() => {
		load();
	});
</script>

<div class="mx-auto max-w-6xl px-5 py-6 md:px-10">
	<h1 class="text-2xl font-semibold">{$i18n.t('Team & quota')}</h1>
	<nav class="my-6 flex gap-1 overflow-x-auto border-b border-gray-200 dark:border-gray-800">
		{#each [['packages', 'Packages'], ['members', 'Member quota'], ['pricing', 'Model pricing'], ['pending', 'Reconciliation'], ['sso', 'Enterprise login'], ['audit', 'Adjustment history']] as item}
			<button
				class="whitespace-nowrap border-b-2 px-4 py-3 text-sm {tab === item[0]
					? 'border-current font-medium'
					: 'border-transparent text-gray-500'}"
				on:click={() => {
					tab = item[0];
					if (tab === 'members') action(searchUsers, false);
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
	{#if tab === 'packages'}
		<div class="grid gap-9 lg:grid-cols-2">
			<section>
				<h2 class="mb-4 text-lg font-medium">{$i18n.t('Default package')}</h2>
				<form
					class="space-y-4"
					on:submit|preventDefault={() =>
						action(async () => {
							await saveTeam('/team/admin/settings', {
								...settings,
								default_plan_id: settings.default_plan_id || null,
								default_effective_at: effective
									? Math.floor(new Date(effective).getTime() / 1000)
									: null,
								reason
							});
							await load();
							config.set(await getBackendConfig(localStorage.token));
						})}
				>
					<label class="flex items-center gap-3"
						><input type="checkbox" bind:checked={settings.enabled} />
						{$i18n.t('Enable team workspaces')}</label
					>
					<label class="flex items-center gap-3"
						><input type="checkbox" bind:checked={settings.quota_enabled} />
						{$i18n.t('Enforce model quota')}</label
					>
					<label class="block text-sm"
						>{$i18n.t('Inherited by all users without a personal package')}<select
							class="field mt-2"
							bind:value={settings.default_plan_id}
							><option value="">{$i18n.t('Select a package')}</option
							>{#each plans.filter((p) => !p.archived) as plan}<option value={plan.id}
									>{plan.name} · {quotaNumber(plan.allowance)} {$i18n.t(plan.unit)}</option
								>{/each}</select
						></label
					>
					<label class="block text-sm"
						>{$i18n.t('Effective time for non-resetting accounts')}<input
							class="field mt-2"
							type="datetime-local"
							bind:value={effective}
						/></label
					>
					<label class="block text-sm"
						>{$i18n.t('Reason')}<input class="field mt-2" bind:value={reason} required /></label
					>
					<button class="primary" disabled={busy}>{$i18n.t('Save configuration')}</button>
				</form>
				<p class="mt-4 text-sm leading-6 text-gray-500">
					{$i18n.t(
						'Personal assignments are preserved. Different units or reset rules take effect at the current period boundary. Temporary bonuses expire at reset.'
					)}
				</p>
				<div class="mt-7 space-y-2">
					{#each plans as plan}<button
							class="flex w-full items-center justify-between gap-3 rounded-xl bg-gray-50 p-3 text-left text-sm dark:bg-gray-900"
							on:click={() => {
								editorId = plan.id;
								editor = structuredClone(plan);
								planReason = '';
							}}
							><span class="font-medium"
								>{plan.name}{plan.archived ? ' · ' + $i18n.t('Archived') : ''}</span
							><span
								>{quotaNumber(plan.allowance)}
								{$i18n.t(plan.unit)} · {$i18n.t(plan.reset.kind)}</span
							></button
						>{/each}
				</div>
			</section>
			<section>
				<div class="mb-4 flex items-center justify-between">
					<h2 class="text-lg font-medium">{$i18n.t(editorId ? 'Edit package' : 'New package')}</h2>
					{#if editorId}<button
							class="text-sm underline"
							on:click={() => {
								editorId = '';
								editor = blankPlan();
								planReason = '';
							}}>{$i18n.t('New package')}</button
						>{/if}
				</div>
				<form
					class="space-y-4"
					on:submit|preventDefault={() =>
						action(async () => {
							const { id, created_at, updated_at, ...form } = editor;
							await saveTeam(
								`/team/admin/plans${editorId ? '/' + editorId : ''}`,
								{ ...form, reason: planReason },
								editorId ? 'PUT' : 'POST'
							);
							await load();
							planReason = '';
						})}
				>
					<label class="block text-sm"
						>{$i18n.t('Name')}<input
							class="field mt-2"
							bind:value={editor.name}
							required
							maxlength="100"
						/></label
					>
					<div class="grid grid-cols-2 gap-3">
						<label class="text-sm"
							>{$i18n.t('Metering')}<select
								class="field mt-2"
								bind:value={editor.unit}
								disabled={!!editorId}
								><option value="requests">{$i18n.t('API requests')}</option><option value="credits"
									>{$i18n.t('Token credits')}</option
								></select
							></label
						><label class="text-sm"
							>{$i18n.t('Base allowance')}<input
								class="field mt-2"
								type="number"
								min="0"
								max="9007199254740991"
								step="1"
								bind:value={editor.allowance}
								required
							/></label
						>
					</div>
					<div class="grid grid-cols-2 gap-3">
						<label class="text-sm"
							>{$i18n.t('Reset rule')}<select
								class="field mt-2"
								bind:value={editor.reset.kind}
								disabled={!!editorId}
								>{#each ['day', 'week', 'month', 'days', 'never'] as kind}<option value={kind}
										>{$i18n.t(kind)}</option
									>{/each}</select
							></label
						><label class="text-sm"
							>{$i18n.t('Timezone')}<input
								class="field mt-2"
								bind:value={editor.reset.timezone}
								disabled={!!editorId}
								required
							/></label
						>
					</div>
					{#if editor.reset.kind === 'days'}<label class="block text-sm"
							>{$i18n.t('Interval in days')}<input
								class="field mt-2"
								type="number"
								min="1"
								max="3660"
								bind:value={editor.reset.days}
								disabled={!!editorId}
								required
							/></label
						>{/if}
					{#if editorId}<label class="flex items-center gap-3 text-sm"
							><input type="checkbox" bind:checked={editor.archived} />{$i18n.t('Archived')}</label
						>{/if}
					<label class="block text-sm"
						>{$i18n.t('Reason')}<input
							class="field mt-2"
							required
							maxlength="500"
							bind:value={planReason}
						/></label
					>
					<button class="primary" disabled={busy}>{$i18n.t('Save package')}</button>
				</form>
			</section>
		</div>
	{:else if tab === 'members'}
		<div class="grid gap-8 md:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)]">
			<section>
				<form class="mb-4 flex gap-2" on:submit|preventDefault={() => action(searchUsers, false)}>
					<input class="field" bind:value={query} placeholder={$i18n.t('Search users')} /><button
						class="primary"
						disabled={busy}>{$i18n.t('Search')}</button
					>
				</form>
				<div class="space-y-1">
					{#each users as member}<button
							class="w-full rounded-xl p-3 text-left text-sm {selectedUser?.id === member.id
								? 'bg-gray-100 dark:bg-gray-800'
								: 'hover:bg-gray-50 dark:hover:bg-gray-900'}"
							on:click={() => action(() => chooseUser(member), false)}
							><p class="font-medium">{member.name}</p>
							<p class="truncate text-xs text-gray-500">{member.email}</p></button
						>{/each}
				</div>
			</section>
			{#if selectedUser && userQuota}<section class="space-y-5">
					<h2 class="text-lg font-medium">{selectedUser.name}</h2>
					<p class="text-sm">
						{userQuota.plan?.name ?? '—'} · {quotaNumber(userQuota.available)}
						{$i18n.t('available')} · {quotaDate(userQuota.period?.ends_at)}
					</p>
					{#if userQuota.pending_plan}<p class="text-sm text-amber-700 dark:text-amber-300">
							{$i18n.t('Scheduled package')}: {userQuota.pending_plan.name} · {quotaDate(
								userQuota.pending_at
							)}
						</p>{/if}
					<form
						class="space-y-4"
						on:submit|preventDefault={() =>
							action(async () => {
								await saveTeam(`/team/admin/users/${selectedUser.id}/plan`, {
									plan_id: assigned || null,
									effective_at: effective ? Math.floor(new Date(effective).getTime() / 1000) : null,
									reason
								});
								await chooseUser(selectedUser);
							})}
					>
						<label class="block text-sm"
							>{$i18n.t('Package')}<select class="field mt-2" bind:value={assigned}
								><option value="">{$i18n.t('Inherit global default')}</option
								>{#each plans.filter((p) => !p.archived) as plan}<option value={plan.id}
										>{plan.name}</option
									>{/each}</select
							></label
						>
						<label class="block text-sm"
							>{$i18n.t('Effective time for non-resetting accounts')}<input
								type="datetime-local"
								class="field mt-2"
								bind:value={effective}
							/></label
						>
						<label class="block text-sm"
							>{$i18n.t('Reason')}<input class="field mt-2" required bind:value={reason} /></label
						><button class="primary" disabled={busy}>{$i18n.t('Assign package')}</button>
					</form>
					<form
						class="space-y-3 border-t border-gray-200 pt-5 dark:border-gray-800"
						on:submit|preventDefault={() => action(addBonus)}
					>
						<label class="block text-sm"
							>{$i18n.t('Add quota for this period only')}<input
								class="field mt-2"
								type="number"
								min="1"
								step="1"
								required
								bind:value={bonus}
							/></label
						><label class="block text-sm"
							>{$i18n.t('Reason')}<input class="field mt-2" required bind:value={reason} /></label
						><button class="primary" disabled={busy}>{$i18n.t('Add temporary quota')}</button>
					</form>
				</section>{/if}
		</div>
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
					>{$i18n.t('Reason')}<input
						class="field mt-2"
						required
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
	{:else if tab === 'pending'}
		<h2 class="text-lg font-medium">{$i18n.t('Usage awaiting reconciliation')}</h2>
		<p class="mt-2 mb-5 text-sm text-gray-500">
			{$i18n.t(
				'Reservations remain held when upstream usage is missing. Reconcile only against verified provider usage.'
			)}
		</p>
		{#each pending as entry}<form
				class="mb-4 space-y-3 rounded-xl border border-gray-200 p-4 dark:border-gray-800"
				on:submit|preventDefault={() =>
					action(async () => {
						await saveTeam(
							`/team/admin/entries/${entry.id}/reconcile`,
							{ charge: charges[entry.id], reason },
							'POST'
						);
						await load();
					})}
			>
				<div class="flex flex-wrap justify-between gap-2 text-sm">
					<strong>{entry.model_id}</strong><span
						>{quotaDate(entry.created_at)} · {$i18n.t('Reserved usage')}: {entry.reserved}</span
					>
				</div>
				<p class="break-all text-xs text-gray-500">{entry.user_id} · {entry.detail}</p>
				<div class="flex flex-wrap gap-3">
					<input
						class="field max-w-40"
						type="number"
						min="0"
						required
						bind:value={charges[entry.id]}
						aria-label={$i18n.t('Actual charge')}
						placeholder={$i18n.t('Actual charge')}
					/><input
						class="field flex-1"
						required
						bind:value={reason}
						aria-label={$i18n.t('Reason')}
						placeholder={$i18n.t('Reason')}
					/><button class="primary" disabled={busy}>{$i18n.t('Reconcile')}</button>
				</div>
			</form>{/each}
		{#if !pending.length}<p class="py-8 text-sm text-gray-500">
				{$i18n.t('No pending settlements.')}
			</p>{/if}
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
