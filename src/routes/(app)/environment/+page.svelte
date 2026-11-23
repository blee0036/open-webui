<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { toast } from 'svelte-sonner';
	import { terminalServers } from '$lib/stores';
	import {
		teamRequest,
		saveTeam,
		shouldIndexProjectReference,
		type WorkspaceStatus
	} from '$lib/apis/team';
	import { resolveTerminalTarget, downloadFileBlob } from '$lib/apis/terminal';
	import { uploadFile } from '$lib/apis/files';
	import type { Project } from '$lib/types/team';
	import PageShell from '$lib/components/team/PageShell.svelte';
	import FileNav from '$lib/components/chat/FileNav.svelte';
	import WorkspaceSession from '$lib/components/team/WorkspaceSession.svelte';
	import Desktop from '$lib/components/chat/Tabs/Desktop.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	const i18n = getContext<any>('i18n');
	let scope = $page.url.searchParams.get('project') || 'personal';
	let selector = 'managed';
	let projects: Project[] = [];
	let workspace: WorkspaceStatus | null = null;
	let error = '';
	let busy = false;
	let mounted = false;
	let tab = 'files';
	let confirmAction = '';
	let confirming = false;
	let fileNav: FileNav;
	let reload = 0;
	let refreshing = false;
	$: context = scope === 'personal' ? 'local:personal-workspace' : `project:${scope}`;
	$: connection = resolveTerminalTarget(
		selector,
		$terminalServers ?? [],
		typeof localStorage !== 'undefined' ? localStorage.token : '',
		{ chatId: context }
	);
	$: managed = selector === 'managed';
	$: available = !managed || workspace?.status === 'running';
	$: active = ['running', 'starting', 'queued'].includes(workspace?.status ?? '');
	const states: Record<string, string> = {
		stopped: 'Stopped',
		queued: 'Queued',
		starting: 'Starting',
		running: 'Running',
		failed: 'Failed',
		timeout: 'Timed out'
	};
	async function refresh() {
		if (!managed || refreshing) return;
		refreshing = true;
		const expected = scope;
		try {
			const value = await teamRequest<WorkspaceStatus>(`/sandboxes/${scope}`);
			if (scope === expected) {
				workspace = value;
				error = '';
			}
		} catch (e) {
			if (scope === expected) error = String(e instanceof Error ? e.message : e);
		} finally {
			refreshing = false;
		}
	}
	async function control(action: string) {
		busy = true;
		try {
			workspace = await saveTeam(
				`/sandboxes/${scope}`,
				{ action, confirm: action === 'clear' ? 'CLEAR MY WORKSPACE' : null },
				'POST'
			);
			error = '';
			if (action !== 'pause' && action !== 'resume') reload++;
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = false;
			confirming = false;
		}
	}
	function requestControl(action: string) {
		confirmAction = action;
		confirming = true;
	}
	async function chooseScope() {
		workspace = null;
		error = '';
		reload++;
		await goto(scope === 'personal' ? '/environment' : `/environment?project=${scope}`, {
			replaceState: true
		});
		await refresh();
	}
	async function publish() {
		const path = fileNav?.captureView()?.selectedFile;
		if (!path || !connection) {
			toast.error($i18n.t('Open a file in the Files panel first.'));
			return;
		}
		busy = true;
		try {
			const downloaded = await downloadFileBlob(connection, path);
			if (!downloaded) throw new Error($i18n.t('File is unavailable'));
			const file = new File([downloaded.blob], downloaded.filename, { type: downloaded.blob.type });
			const index = shouldIndexProjectReference(file);
			const uploaded = await uploadFile(localStorage.token, file, null, index, index);
			if (!uploaded || uploaded.error)
				throw new Error(uploaded?.error || $i18n.t('File processing failed'));
			await saveTeam(`/projects/${scope}/files`, { file_id: uploaded.id }, 'POST');
			toast.success($i18n.t('Published to project references'));
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = false;
		}
	}
	onMount(() => {
		mounted = true;
		teamRequest<Project[]>('/projects')
			.then((value) => (projects = value))
			.catch((e) => (error = String(e)));
		refresh();
		const interval = setInterval(() => {
			if (!document.hidden) refresh();
		}, 3000);
		return () => clearInterval(interval);
	});
</script>

<ConfirmDialog
	bind:show={confirming}
	title={$i18n.t(
		confirmAction === 'clear'
			? 'Permanently clear your workspace files?'
			: confirmAction === 'public'
				? 'Allow public HTTP(S) access?'
				: 'Rebuild the container and stop running processes?'
	)}
	confirmLabel={$i18n.t('Confirm')}
	on:confirm={() => control(confirmAction)}
/>

<PageShell title={$i18n.t('Work environment')} wide>
	<a
		slot="actions"
		class="text-sm text-gray-500 underline"
		href={scope === 'personal' ? '/' : `/projects/${scope}`}>{$i18n.t('Back to chat')}</a
	>
	<div class="mb-5 flex flex-wrap items-end justify-between gap-4">
		<div class="flex flex-wrap gap-4">
			<label class="text-sm"
				>{$i18n.t('Scope')}<select
					class="mt-2 block max-w-72 rounded-xl bg-gray-100 px-3 py-2.5 dark:bg-gray-800"
					bind:value={scope}
					on:change={chooseScope}
					><option value="personal">{$i18n.t('Personal workspace')}</option
					>{#each projects as project}<option value={project.id}>{project.name}</option
						>{/each}</select
				></label
			><label class="text-sm"
				>{$i18n.t('Connection')}<select
					class="mt-2 block max-w-72 rounded-xl bg-gray-100 px-3 py-2.5 dark:bg-gray-800"
					bind:value={selector}
					on:change={() => {
						reload++;
						refresh();
					}}
					><option value="managed">{$i18n.t('My managed workspace')}</option
					>{#each ($terminalServers ?? []).filter((s) => s.id !== 'managed') as server}<option
							value={server.id || server.url}
							>{server.name || server.url} · {$i18n.t('User-managed')}</option
						>{/each}</select
				></label
			>
		</div>
		{#if managed}<div class="text-sm" role="status">
				<span
					class="inline-block size-2 rounded-full mr-2 {workspace?.status === 'running'
						? 'bg-emerald-500'
						: 'bg-gray-400'}"
				></span>{$i18n.t(
					states[workspace?.status ?? 'stopped'] ?? workspace?.status ?? 'Stopped'
				)}{workspace?.queue_position
					? ' · ' + $i18n.t('Queue position {{position}}', { position: workspace.queue_position })
					: ''}{workspace?.manual ? ' · ' + $i18n.t('Manual control') : ''}<span
					class="ml-4 text-gray-500"
					>{$i18n.t(workspace?.public ? 'Public HTTP(S)' : 'Offline')}</span
				>
			</div>{/if}
	</div>
	{#if managed && (workspace?.agent_status === 'queued' || workspace?.agent_status === 'running')}
		<p class="mb-4 text-sm text-gray-500" role="status">
			{$i18n.t(
				workspace.agent_status === 'running' ? 'Agent is running' : 'Agent execution queued'
			)} · {$i18n.t('{{count}} waiting', { count: workspace.agent_queue ?? 0 })}
		</p>
	{/if}
	{#if error}<div
			role="alert"
			class="mb-5 rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{error} <button class="underline" on:click={refresh}>{$i18n.t('Retry')}</button>
		</div>{/if}
	{#if managed}
		<div class="mb-5 flex flex-wrap items-center gap-2 text-sm">
			<button
				class="rounded-xl bg-gray-900 px-4 py-2.5 text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
				disabled={busy}
				on:click={() => control(active ? 'stop' : 'start')}
				>{$i18n.t(active ? 'Stop workspace' : 'Start workspace')}</button
			>
			<button
				class="rounded-xl bg-gray-100 px-3 py-2.5 disabled:opacity-40 dark:bg-gray-800"
				disabled={busy}
				on:click={() => control(workspace?.manual ? 'resume' : 'pause')}
				>{$i18n.t(workspace?.manual ? 'Resume Agent queue' : 'Take manual control')}</button
			>
			<button
				class="rounded-xl px-3 py-2.5 disabled:opacity-40"
				disabled={busy}
				on:click={() => (workspace?.public ? control('offline') : requestControl('public'))}
				>{$i18n.t(workspace?.public ? 'Go offline' : 'Enable public access')}</button
			>
			<details class="relative">
				<summary class="cursor-pointer rounded-xl px-3 py-2.5"
					>{$i18n.t('Workspace settings')}</summary
				>
				<div
					class="absolute top-11 z-20 w-64 space-y-2 rounded-xl border border-gray-200 bg-white p-3 shadow-lg dark:border-gray-700 dark:bg-gray-900"
				>
					<p class="text-xs leading-5 text-gray-500">
						1 CPU · 2 GiB · 128 PIDs<br />{$i18n.t('Files persist across stops and rebuilds.')}
					</p>
					<button
						class="block py-2 text-sm"
						disabled={busy}
						on:click={() => requestControl('recreate')}>{$i18n.t('Rebuild container')}</button
					><button
						class="block py-2 text-sm text-red-600"
						disabled={busy}
						on:click={() => requestControl('clear')}>{$i18n.t('Clear workspace files')}</button
					>
				</div>
			</details>
		</div>
		<p class="mb-5 text-sm leading-6 text-gray-500">
			{$i18n.t(
				'This environment belongs only to you in this scope. Agent runs are serialized; manual control pauses new Agent runs. Idle shutdown keeps files, not running processes.'
			)}
		</p>
	{:else}<p class="mb-5 text-sm leading-6 text-amber-700 dark:text-amber-300">
			{$i18n.t(
				'User-managed external environment. Its credentials, files and isolation are managed by you, separately from the platform workspace.'
			)}
		</p>{/if}
	<div class="mb-3 flex flex-wrap items-center justify-between gap-3">
		<nav class="flex gap-1">
			{#each ['files', 'terminal', 'browser', 'desktop'] as item}<button
					class="rounded-xl px-4 py-2 text-sm {tab === item
						? 'bg-gray-100 font-medium dark:bg-gray-800'
						: 'text-gray-500'}"
					on:click={() => (tab = item)}
					>{$i18n.t(
						{ files: 'Files', terminal: 'Terminal', browser: 'Browser', desktop: 'Desktop' }[item]!
					)}</button
				>{/each}
		</nav>
		{#if tab === 'files' && managed && scope !== 'personal'}<button
				class="text-sm underline disabled:opacity-40"
				disabled={busy || !available}
				on:click={publish}>{$i18n.t('Publish selected file to project')}</button
			>{/if}
	</div>
	{#if mounted && available && connection}
		{#key `${scope}:${selector}:${reload}`}
			<div
				class="h-[max(420px,calc(100dvh-24rem))] overflow-hidden rounded-2xl border border-gray-200 dark:border-gray-800"
			>
				{#if tab === 'files'}<FileNav
						bind:this={fileNav}
						terminalId={selector}
						chatId={context}
						initialDirectory="/home/user"
						manageFileRequests={false}
					/>
				{:else if tab === 'terminal' || tab === 'browser'}{#key tab}<WorkspaceSession
							target={connection}
							kind={tab}
						/>{/key}
				{:else}<Desktop target={connection} />{/if}
			</div>
		{/key}
	{:else}<div
			class="flex min-h-80 items-center justify-center rounded-2xl bg-gray-50 p-8 text-center text-sm leading-6 text-gray-500 dark:bg-gray-900"
		>
			{$i18n.t(
				'Start your workspace to upload files, run Python, and preview generated charts or spreadsheets.'
			)}
		</div>{/if}
</PageShell>
