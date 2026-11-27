<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import dayjs from 'dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import { controlSandbox, type SandboxReport, type SandboxWorkspace } from '$lib/apis/system';
	import {
		card,
		link,
		row,
		tableHead,
		tag,
		td,
		th,
		type Tone
	} from '$lib/components/team/admin/ui';
	import { formatBytes } from '$lib/utils/storageQuota';
	import UsageBar from './UsageBar.svelte';

	dayjs.extend(relativeTime);

	const i18n = getContext<any>('i18n');

	export let report: SandboxReport;
	export let onChange: () => Promise<void> | void = () => {};

	type Action = 'stop' | 'recreate' | 'clear';
	let filter: 'active' | 'all' = 'active';
	// $i18n.t('Active') $i18n.t('All')
	const FILTERS: ['active' | 'all', string][] = [
		['active', 'Active'],
		['all', 'All']
	];
	let pending: { item: SandboxWorkspace; action: Action } | null = null;
	let confirming = false;
	let busyId = '';

	const ACTIVE = new Set(['running', 'starting', 'queued']);
	const statusTone: Record<string, Tone> = {
		running: 'green',
		starting: 'blue',
		queued: 'amber',
		failed: 'red',
		timeout: 'red',
		stopped: 'gray'
	};
	// $i18n.t('Running') $i18n.t('Starting') $i18n.t('Queued') $i18n.t('Failed') $i18n.t('Timed out') $i18n.t('Stopped')
	const statusLabel: Record<string, string> = {
		running: 'Running',
		starting: 'Starting',
		queued: 'Queued',
		failed: 'Failed',
		timeout: 'Timed out',
		stopped: 'Stopped'
	};

	$: workspaces = report.workspaces ?? [];
	$: shown = workspaces.filter((item) => filter === 'all' || ACTIVE.has(item.status));
	$: overUsers = new Set(
		Object.entries(report.users ?? {})
			.filter(([, value]) => value.over)
			.map(([id]) => id)
	);
	$: workspaceName = (item: SandboxWorkspace) =>
		item.project_id === 'personal'
			? $i18n.t('Personal workspace')
			: (item.project_name ?? item.project_id);

	const prompts: Record<Action, string> = {
		stop: 'Stop this workspace? Running processes end; files are kept and it starts again when used.',
		recreate:
			'Rebuild this workspace container? Running processes end; files in the home folder are kept.',
		clear:
			'Permanently delete every file in this workspace? This cannot be undone. Type CLEAR to confirm.'
	};
	// $i18n.t('Stop this workspace? Running processes end; files are kept and it starts again when used.')
	// $i18n.t('Rebuild this workspace container? Running processes end; files in the home folder are kept.')
	// $i18n.t('Permanently delete every file in this workspace? This cannot be undone. Type CLEAR to confirm.')

	function ask(item: SandboxWorkspace, action: Action) {
		pending = { item, action };
		confirming = true;
	}

	async function run(value: string) {
		if (!pending) return;
		const { item, action } = pending;
		if (action === 'clear' && value?.trim() !== 'CLEAR') {
			toast.error($i18n.t('Type CLEAR to delete the workspace files.'));
			return;
		}
		busyId = item.id;
		try {
			await controlSandbox(item.id, action);
			toast.success(
				action === 'clear'
					? $i18n.t('Workspace cleared. The member was notified.')
					: action === 'stop'
						? $i18n.t('Workspace stopped. The member was notified.')
						: $i18n.t('Workspace rebuilt. The member was notified.')
			);
			await onChange();
		} catch (e) {
			toast.error(String(e instanceof Error ? e.message : e));
		} finally {
			busyId = '';
			pending = null;
		}
	}
</script>

<ConfirmDialog
	bind:show={confirming}
	title={pending
		? `${pending.item.user_name ?? pending.item.user_id} · ${workspaceName(pending.item)}`
		: ''}
	message={pending ? $i18n.t(prompts[pending.action]) : ''}
	input={pending?.action === 'clear'}
	inputPlaceholder="CLEAR"
	confirmLabel={$i18n.t('Confirm')}
	on:confirm={(event) => run(event.detail)}
/>

<section class={card} aria-labelledby="system-sandboxes">
	<div class="flex flex-wrap items-center justify-between gap-3 p-5 pb-3">
		<div>
			<h3 id="system-sandboxes" class="font-medium">{$i18n.t('Sandboxes')}</h3>
			{#if report.available}
				<p class="mt-1 text-xs text-gray-500">
					{$i18n.t(
						'{{running}} of {{limit}} running · {{queued}} waiting · {{memory}} memory each',
						{
							running: report.running ?? 0,
							limit: report.limit ?? 0,
							queued: report.queued ?? 0,
							memory: formatBytes(report.memory_per_workspace)
						}
					)}
				</p>
			{/if}
		</div>
		{#if report.available}
			<div class="flex gap-1 text-sm" role="tablist">
				{#each FILTERS as [value, text]}
					<button
						role="tab"
						aria-selected={filter === value}
						class="rounded-lg px-3 py-1.5 {filter === value
							? 'bg-gray-100 dark:bg-gray-800'
							: 'text-gray-500'}"
						on:click={() => (filter = value)}>{$i18n.t(text)}</button
					>
				{/each}
			</div>
		{/if}
	</div>
	{#if !report.configured}
		<p class="px-5 pb-5 text-sm text-gray-500">
			{$i18n.t('The sandbox service is not configured.')}
		</p>
	{:else if !report.available}
		<p
			role="alert"
			class="mx-5 mb-5 rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{$i18n.t('The sandbox service is unavailable.')}
			{#if report.error}<span class="block text-xs opacity-80">{report.error}</span>{/if}
		</p>
	{:else}
		<div class="overflow-x-auto">
			<table class="w-full text-sm">
				<thead class={tableHead}>
					<tr>
						<th class={th}>{$i18n.t('Member')}</th>
						<th class={th}>{$i18n.t('Workspace')}</th>
						<th class={th}>{$i18n.t('Status')}</th>
						<th class="{th} min-w-36">{$i18n.t('RAM')}</th>
						<th class="{th} text-right">{$i18n.t('Disk')}</th>
						<th class={th}>{$i18n.t('Last used')}</th>
						<th class="{th} text-right">{$i18n.t('Actions')}</th>
					</tr>
				</thead>
				<tbody>
					{#each shown as item (item.id)}
						<tr class={row}>
							<td class={td}>
								<div class="font-medium">{item.user_name ?? item.user_id}</div>
								{#if overUsers.has(item.user_id)}
									<div class="text-xs text-red-600 dark:text-red-400">
										{$i18n.t('Over sandbox storage limit')}
									</div>
								{/if}
							</td>
							<td class={td}>{workspaceName(item)}</td>
							<td class={td}>
								<div class="flex flex-wrap items-center gap-1">
									<span class={tag(statusTone[item.status] ?? 'gray')}
										>{$i18n.t(statusLabel[item.status] ?? item.status)}</span
									>
									{#if item.agent_status === 'running'}<span class={tag('purple')}
											>{$i18n.t('Agent running')}</span
										>{:else if item.manual}<span class={tag('amber')}
											>{$i18n.t('Manual control')}</span
										>{/if}
									{#if item.public}<span class={tag('blue')}>{$i18n.t('Public')}</span>{/if}
								</div>
								{#if item.error}<div
										class="mt-1 max-w-64 truncate text-xs text-red-600"
										title={item.error}
									>
										{item.error}
									</div>{/if}
							</td>
							<td class={td}>
								{#if item.memory}
									<div class="text-xs tabular-nums">
										{formatBytes(item.memory.used)}{#if item.memory.limit}
											/ {formatBytes(item.memory.limit)}{/if}
									</div>
									<div class="mt-1">
										<UsageBar used={item.memory.used} total={item.memory.limit} warn={85} />
									</div>
								{:else}<span class="text-xs text-gray-500">—</span>{/if}
							</td>
							<td class="{td} text-right tabular-nums">
								{item.disk === null ? '—' : formatBytes(item.disk)}
							</td>
							<td class="{td} text-xs text-gray-500">
								{item.idle_seconds === null
									? '—'
									: dayjs().subtract(item.idle_seconds, 'second').fromNow()}
							</td>
							<td class="{td} text-right">
								<div class="flex justify-end gap-3">
									{#if ACTIVE.has(item.status)}
										<button
											class={link}
											disabled={busyId === item.id}
											on:click={() => ask(item, 'stop')}>{$i18n.t('Stop')}</button
										>
									{/if}
									<button
										class={link}
										disabled={busyId === item.id}
										on:click={() => ask(item, 'recreate')}>{$i18n.t('Rebuild')}</button
									>
									<button
										class="{link} !text-red-600 dark:!text-red-400"
										disabled={busyId === item.id}
										on:click={() => ask(item, 'clear')}>{$i18n.t('Clear files')}</button
									>
								</div>
							</td>
						</tr>
					{:else}
						<tr
							><td class="{td} text-gray-500" colspan="7"
								>{filter === 'active'
									? $i18n.t('No sandbox is running.')
									: $i18n.t('No workspaces yet.')}</td
							></tr
						>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</section>
