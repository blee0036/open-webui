<script lang="ts">
	import { getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import dayjs from 'dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	import { saveTeam, type QuotaSummary } from '$lib/apis/team';
	import FilesModal from '$lib/components/layout/FilesModal.svelte';
	import { formatBytes, usagePercent, usageTone, type UsageTone } from '$lib/utils/storageQuota';

	dayjs.extend(relativeTime);

	const i18n = getContext<any>('i18n');

	export let quota: QuotaSummary;
	export let onRefresh: () => Promise<void> | void = () => {};

	let showFiles = false;
	let filesWereOpen = false;
	let checking = false;

	// Deleting files frees space at once; show it when the manager closes.
	$: if (showFiles) filesWereOpen = true;
	$: if (!showFiles && filesWereOpen) {
		filesWereOpen = false;
		onRefresh();
	}

	$: files = quota.storage;
	$: sandbox = quota.sandbox;
	$: filePercent = usagePercent(files?.used, files?.limit);
	$: fileTone = usageTone(filePercent);
	$: sandboxPercent = usagePercent(sandbox?.used, sandbox?.limit);
	let sandboxTone: UsageTone;
	$: sandboxTone = sandbox?.over ? 'full' : usageTone(sandboxPercent);

	const bar: Record<UsageTone, string> = {
		ok: 'bg-emerald-600 dark:bg-emerald-400',
		warn: 'bg-amber-500',
		full: 'bg-red-500'
	};
	const value: Record<UsageTone, string> = {
		ok: '',
		warn: 'text-amber-700 dark:text-amber-300',
		full: 'text-red-600 dark:text-red-400'
	};

	async function checkAgain() {
		checking = true;
		try {
			await saveTeam('/sandboxes/personal', { action: 'rescan' }, 'POST');
			await onRefresh();
		} catch (e) {
			toast.error(String(e instanceof Error ? e.message : e));
		} finally {
			checking = false;
		}
	}
</script>

<div class="grid gap-4 md:grid-cols-2">
	{#if files}
		<div class="rounded-2xl border border-base-200 p-5 dark:border-base-800">
			<div class="flex items-baseline justify-between gap-3">
				<h3 class="font-medium">{$i18n.t('Uploaded files')}</h3>
				<span class="text-sm {value[fileTone]}">
					{formatBytes(files.used)}
					{#if files.limit !== null}/ {formatBytes(files.limit)}{:else}· {$i18n.t('Unlimited')}{/if}
				</span>
			</div>
			{#if files.limit !== null}
				<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
					<div
						class="h-full rounded-full {bar[fileTone]}"
						style:width="{Math.min(100, filePercent ?? 0)}%"
					></div>
				</div>
			{/if}
			<p class="mt-3 text-sm text-base-500 dark:text-base-400">
				{$i18n.t('{{count}} files: chat attachments, knowledge and notes.', {
					count: files.file_count
				})}
				{#if files.unknown_size}
					{$i18n.t('{{count}} older files have no recorded size and are not counted.', {
						count: files.unknown_size
					})}
				{/if}
			</p>
			{#if fileTone === 'full'}
				<p
					role="alert"
					class="mt-3 rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
				>
					{$i18n.t(
						'Storage is full. New uploads are refused until you delete files or an administrator raises your limit.'
					)}
				</p>
			{:else if fileTone === 'warn'}
				<p role="status" class="mt-3 text-sm text-amber-700 dark:text-amber-300">
					{$i18n.t('Storage is almost full. Delete files you no longer need to keep uploading.')}
				</p>
			{/if}
			<button
				class="mt-4 rounded-xl bg-base-100 px-3 py-1.5 text-sm hover:bg-base-200 dark:bg-base-800 dark:hover:bg-base-700"
				on:click={() => (showFiles = true)}>{$i18n.t('Manage files')}</button
			>
		</div>
	{/if}

	{#if sandbox}
		<div class="rounded-2xl border border-base-200 p-5 dark:border-base-800">
			<div class="flex items-baseline justify-between gap-3">
				<h3 class="font-medium">{$i18n.t('Sandbox storage')}</h3>
				<span class="text-sm {value[sandboxTone]}">
					{formatBytes(sandbox.used)}
					{#if sandbox.limit !== null}/ {formatBytes(sandbox.limit)}{:else}· {$i18n.t(
							'Unlimited'
						)}{/if}
				</span>
			</div>
			{#if sandbox.limit !== null}
				<div class="mt-3 h-1.5 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
					<div
						class="h-full rounded-full {bar[sandboxTone]}"
						style:width="{Math.min(100, sandboxPercent ?? 0)}%"
					></div>
				</div>
			{/if}
			<p class="mt-3 text-sm text-base-500 dark:text-base-400">
				{$i18n.t('Home folders of your personal and project workspaces combined.')}
				{#if sandbox.scanned_at}
					{$i18n.t('Measured {{time}}; checked every few minutes.', {
						time: dayjs(sandbox.scanned_at * 1000).fromNow()
					})}
				{:else}
					{$i18n.t('Not measured yet.')}
				{/if}
			</p>
			{#if sandbox.over}
				<p
					role="alert"
					class="mt-3 rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
				>
					{$i18n.t(
						'Sandbox storage is full. Agent commands are paused; the terminal and file panel still work so you can delete files. Agent commands resume after the next check.'
					)}
				</p>
			{:else if sandboxTone === 'warn'}
				<p role="status" class="mt-3 text-sm text-amber-700 dark:text-amber-300">
					{$i18n.t('Sandbox storage is almost full. Agent commands pause when it is full.')}
				</p>
			{/if}
			<div class="mt-4 flex flex-wrap gap-2">
				<a
					href="/environment"
					class="rounded-xl bg-base-100 px-3 py-1.5 text-sm hover:bg-base-200 dark:bg-base-800 dark:hover:bg-base-700"
					>{$i18n.t('Open workspace')}</a
				>
				<button
					class="rounded-xl bg-base-100 px-3 py-1.5 text-sm hover:bg-base-200 disabled:opacity-50 dark:bg-base-800 dark:hover:bg-base-700"
					disabled={checking}
					on:click={checkAgain}>{$i18n.t('Check again')}</button
				>
			</div>
		</div>
	{/if}
</div>

<FilesModal bind:show={showFiles} />
