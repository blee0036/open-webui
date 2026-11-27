<script lang="ts">
	import { getContext } from 'svelte';
	import dayjs from 'dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	import type { StorageReport, StorageRow } from '$lib/apis/system';
	import { card, field, row, tableHead, tag, td, th } from '$lib/components/team/admin/ui';
	import { formatBytes } from '$lib/utils/storageQuota';
	import UsageBar from './UsageBar.svelte';

	dayjs.extend(relativeTime);

	const i18n = getContext<any>('i18n');

	export let report: StorageReport;

	let query = '';
	let sort: 'total' | 'files' | 'sandbox' | 'name' = 'total';
	let showAll = false;
	const PAGE = 50;

	const total = (item: StorageRow) => item.files.used + (item.sandbox.used ?? 0);
	const fileOver = (item: StorageRow) =>
		item.files.limit !== null && item.files.used >= item.files.limit;
	const sandboxOver = (item: StorageRow) =>
		item.sandbox.over ||
		(item.sandbox.limit !== null && (item.sandbox.used ?? 0) > item.sandbox.limit);

	$: needle = query.trim().toLowerCase();
	$: rows = report.users
		.filter(
			(item) =>
				!needle ||
				item.name?.toLowerCase().includes(needle) ||
				item.email?.toLowerCase().includes(needle)
		)
		.sort((a, b) =>
			sort === 'name'
				? (a.name ?? '').localeCompare(b.name ?? '')
				: sort === 'files'
					? b.files.used - a.files.used
					: sort === 'sandbox'
						? (b.sandbox.used ?? 0) - (a.sandbox.used ?? 0)
						: total(b) - total(a)
		);
	$: visible = showAll ? rows : rows.slice(0, PAGE);
	$: overCount = report.users.filter((item) => fileOver(item) || sandboxOver(item)).length;
</script>

<section class={card} aria-labelledby="system-storage">
	<div class="flex flex-wrap items-center justify-between gap-3 p-5 pb-3">
		<div>
			<h3 id="system-storage" class="font-medium">{$i18n.t('Storage by member')}</h3>
			<p class="mt-1 text-xs text-gray-500">
				{$i18n.t('Uploaded files total {{size}}.', { size: formatBytes(report.total_files_bytes) })}
				{#if report.sandbox_available && report.sandbox_scanned_at}
					{$i18n.t('Sandbox sizes measured {{time}}.', {
						time: dayjs(report.sandbox_scanned_at * 1000).fromNow()
					})}
				{/if}
				{#if !report.enforced}
					{$i18n.t('Limits are not enforced while quota enforcement is off.')}
				{/if}
			</p>
		</div>
		<div class="flex flex-wrap items-center gap-2">
			{#if overCount}<span class={tag('red')}
					>{$i18n.t('{{count}} over limit', { count: overCount })}</span
				>{/if}
			<input
				class="{field} w-48"
				type="search"
				placeholder={$i18n.t('Search members')}
				aria-label={$i18n.t('Search members')}
				bind:value={query}
			/>
			<select class="{field} w-auto" bind:value={sort} aria-label={$i18n.t('Sort by')}>
				<option value="total">{$i18n.t('Most storage')}</option>
				<option value="files">{$i18n.t('Most file storage')}</option>
				<option value="sandbox">{$i18n.t('Most sandbox storage')}</option>
				<option value="name">{$i18n.t('Name')}</option>
			</select>
		</div>
	</div>
	<div class="overflow-x-auto">
		<table class="w-full text-sm">
			<thead class={tableHead}>
				<tr>
					<th class={th}>{$i18n.t('Member')}</th>
					<th class={th}>{$i18n.t('Package')}</th>
					<th class="{th} min-w-48">{$i18n.t('Uploaded files')}</th>
					<th class="{th} min-w-48">{$i18n.t('Sandbox storage')}</th>
				</tr>
			</thead>
			<tbody>
				{#each visible as item (item.id)}
					<tr
						class="{row} {fileOver(item) || sandboxOver(item)
							? 'bg-red-50/60 dark:bg-red-950/20'
							: ''}"
					>
						<td class={td}>
							<div class="flex items-center gap-2">
								<span class="font-medium">{item.name}</span>
								{#if item.role === 'admin'}<span class={tag('blue')}>{$i18n.t('Admin')}</span>{/if}
							</div>
							<div class="text-xs text-gray-500">{item.email}</div>
						</td>
						<td class="{td} text-gray-500">{item.plan_name ?? '—'}</td>
						<td class={td}>
							<div class="flex items-baseline justify-between gap-2 tabular-nums">
								<span class={fileOver(item) ? 'text-red-600 dark:text-red-400' : ''}>
									{formatBytes(item.files.used)}
								</span>
								<span class="text-xs text-gray-500"
									>{item.files.limit === null
										? $i18n.t('Unlimited')
										: formatBytes(item.files.limit)}</span
								>
							</div>
							<div class="mt-1"><UsageBar used={item.files.used} total={item.files.limit} /></div>
							<div class="mt-1 text-xs text-gray-500">
								{$i18n.t('{{count}} files', { count: item.files.file_count })}
								{#if item.files.unknown_size}
									· {$i18n.t('{{count}} without size', { count: item.files.unknown_size })}
								{/if}
							</div>
						</td>
						<td class={td}>
							{#if item.sandbox.used === null}
								<span class="text-xs text-gray-500">—</span>
							{:else}
								<div class="flex items-baseline justify-between gap-2 tabular-nums">
									<span class={sandboxOver(item) ? 'text-red-600 dark:text-red-400' : ''}>
										{formatBytes(item.sandbox.used)}
									</span>
									<span class="text-xs text-gray-500"
										>{item.sandbox.limit === null
											? $i18n.t('Unlimited')
											: formatBytes(item.sandbox.limit)}</span
									>
								</div>
								<div class="mt-1">
									<UsageBar
										used={item.sandbox.used}
										total={item.sandbox.limit}
										over={item.sandbox.over}
									/>
								</div>
								{#if item.sandbox.over}
									<div class="mt-1 text-xs text-red-600 dark:text-red-400">
										{$i18n.t('Agent paused')}
									</div>
								{/if}
							{/if}
						</td>
					</tr>
				{:else}
					<tr><td class="{td} text-gray-500" colspan="4">{$i18n.t('No members found')}</td></tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if rows.length > PAGE}
		<div class="p-3 text-center">
			<button
				class="text-sm text-blue-600 dark:text-blue-400"
				on:click={() => (showAll = !showAll)}
			>
				{showAll ? $i18n.t('Show less') : $i18n.t('Show all {{count}}', { count: rows.length })}
			</button>
		</div>
	{/if}
</section>
