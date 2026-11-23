<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { toast } from 'svelte-sonner';
	import { selectedFolder, models, user, temporaryChat } from '$lib/stores';
	import { teamRequest, saveTeam, quotaDate, shouldIndexProjectReference } from '$lib/apis/team';
	import { uploadFile, getFileBlobById } from '$lib/apis/files';
	import type { Project, ConversationVisibility } from '$lib/types/team';
	import PageShell from '$lib/components/team/PageShell.svelte';
	import AccessControl from '$lib/components/workspace/common/AccessControl.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	const i18n = getContext<any>('i18n');
	let conflict: { updated_at?: number | null } | null = null;
	async function saveContext(force = false) {
		conflict = null;
		await action(async () => {
			try {
				await saveTeam(`/projects/${project!.id}`, {
					...form,
					revision: project!.data.context_revision ?? 0,
					force
				});
			} catch (e: any) {
				if (e?.status === 409 && e?.detail?.code === 'project_conflict') {
					conflict = e.detail;
					return;
				}
				throw e;
			}
			conflict = null;
			await load();
			toast.success($i18n.t('Saved'));
		});
	}
	let project: Project | null = null;
	let chats: any[] = [];
	let error = '';
	let busy = false;
	let tab = 'chats';
	let query = '';
	let grants: any[] = [];
	let form = { name: '', system_prompt: '', model_ids: [] as string[] };
	let loadedId = '';
	async function load(id: string = $page.params.id ?? '') {
		try {
			[project, chats] = await Promise.all([
				teamRequest(`/projects/${id}`),
				teamRequest(`/projects/${id}/chats`)
			]);
			form = {
				name: project!.name,
				system_prompt: project!.data.system_prompt ?? '',
				model_ids: project!.data.model_ids ?? []
			};
			grants = project!.access_grants ?? [];
			error = '';
			loadedId = id;
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		}
	}
	async function action(fn: () => Promise<unknown>) {
		busy = true;
		try {
			await fn();
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = false;
		}
	}
	async function startChat() {
		selectedFolder.set(project as any);
		temporaryChat.set(false);
		await goto(`/?project=${project!.id}`);
	}
	async function search() {
		chats = await teamRequest(
			`/chats/search?project_id=${project!.id}&text=${encodeURIComponent(query)}`
		);
	}
	async function upload(event: Event) {
		const input = event.target as HTMLInputElement;
		const files = Array.from(input.files ?? []);
		await action(async () => {
			for (const file of files) {
				const index = shouldIndexProjectReference(file);
				const uploaded = await uploadFile(localStorage.token, file, null, index, index);
				if (!uploaded || uploaded.error)
					throw new Error(uploaded?.error || $i18n.t('File processing failed'));
				await saveTeam(`/projects/${project!.id}/files`, { file_id: uploaded.id }, 'POST');
			}
			await load();
			input.value = '';
		});
	}
	async function downloadReference(id: string, name: string) {
		const blob = await getFileBlobById(localStorage.token, id);
		const url = URL.createObjectURL(blob);
		const anchor = document.createElement('a');
		anchor.href = url;
		anchor.download = name;
		anchor.click();
		setTimeout(() => URL.revokeObjectURL(url), 1000);
	}
	async function shareChat(chat: any, visibility: ConversationVisibility) {
		await action(async () => {
			await saveTeam(`/projects/${project!.id}/chats/${chat.id}/visibility`, { visibility });
			await load();
		});
	}
	onMount(() => {
		load();
	});
	$: if (loadedId && loadedId !== $page.params.id) {
		loadedId = '';
		void load($page.params.id);
	}
</script>

<PageShell title={project?.name ?? $i18n.t('Project')}>
	<svelte:fragment slot="actions"
		>{#if project}<button
				class="rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white dark:bg-gray-100 dark:text-gray-900"
				on:click={startChat}>{$i18n.t('New Chat')}</button
			>{/if}</svelte:fragment
	>
	<a class="text-sm text-gray-500" href="/projects">← {$i18n.t('All projects')}</a>
	{#if error}<p
			role="alert"
			class="mt-5 rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{error}
		</p>{/if}
	{#if project}
		<div
			class="mt-5 flex flex-wrap items-center justify-between gap-4 border-b border-gray-200 pb-3 dark:border-gray-800"
		>
			<nav class="flex gap-5 text-sm">
				{#each ['chats', 'references', 'context', 'members'] as item}<button
						class="py-2 {tab === item
							? 'font-medium text-gray-900 dark:text-gray-100'
							: 'text-gray-500'}"
						on:click={() => (tab = item)}
						>{$i18n.t(
							{
								chats: 'Conversations',
								references: 'Reference files',
								context: 'Project context',
								members: 'Members'
							}[item]!
						)}</button
					>{/each}
			</nav>
			<a class="text-sm underline underline-offset-4" href="/environment?project={project.id}"
				>{$i18n.t('My execution workspace')} ↗</a
			>
		</div>
		{#if tab === 'chats'}
			<form class="my-6 flex gap-3" on:submit|preventDefault={() => action(search)}>
				<input
					class="min-w-0 flex-1 rounded-xl bg-gray-50 px-4 py-3 text-sm dark:bg-gray-900"
					bind:value={query}
					placeholder={$i18n.t('Search this project')}
					aria-label={$i18n.t('Search this project')}
				/><button class="px-3 text-sm" disabled={busy}>{$i18n.t('Search')}</button>
			</form>
			<p class="mb-4 text-sm text-gray-500">
				{$i18n.t(
					'Only your conversations and conversations explicitly shared with this project appear here.'
				)}
			</p>
			<div class="divide-y divide-gray-100 dark:divide-gray-800">
				{#each chats as chat}<div class="flex flex-wrap items-center gap-3 py-4">
						<a class="min-w-0 flex-1" href="/c/{chat.id}"
							><p class="truncate text-sm font-medium">{chat.title}</p>
							<p class="mt-1 text-xs text-gray-500">
								{quotaDate(chat.updated_at)} · {$i18n.t(
									chat.visibility === 'project' ? 'Shared with project' : 'Private conversation'
								)}
							</p></a
						>{#if chat.user_id === $user?.id}<button
								class="rounded-lg bg-gray-50 px-3 py-2 text-xs dark:bg-gray-900"
								disabled={busy}
								on:click={() =>
									shareChat(chat, chat.visibility === 'project' ? 'private' : 'project')}
								>{$i18n.t(
									chat.visibility === 'project' ? 'Make private' : 'Share with project'
								)}</button
							>{/if}
					</div>{/each}
			</div>
			{#if !chats.length}<p class="py-14 text-center text-sm text-gray-500">
					{$i18n.t('No conversations yet.')}
				</p>{/if}
		{:else if tab === 'references'}
			<div class="my-6 flex flex-wrap items-center justify-between gap-4">
				<p class="max-w-lg text-sm leading-6 text-gray-500">
					{$i18n.t(
						'Published reference files are readable by project members. Uploads in your execution workspace stay private until published.'
					)}
				</p>
				{#if project.write_access}<label
						class="cursor-pointer rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white dark:bg-gray-100 dark:text-gray-900"
						>{$i18n.t(busy ? 'Processing…' : 'Add references')}<input
							type="file"
							class="sr-only"
							multiple
							disabled={busy}
							on:change={upload}
						/></label
					>{/if}
			</div>
			<div class="divide-y divide-gray-100 dark:divide-gray-800">
				{#each project.data.files ?? [] as file}<div class="flex items-center gap-4 py-4">
						<button
							class="min-w-0 flex-1 truncate text-left text-sm underline underline-offset-4"
							on:click={() => action(async () => downloadReference(file.id, file.name))}
							>{file.name}</button
						>{#if project.write_access}<button
								class="text-xs text-gray-500"
								disabled={busy}
								on:click={() =>
									action(async () => {
										await teamRequest(`/projects/${project!.id}/files/${file.id}`, {
											method: 'DELETE'
										});
										await load();
									})}>{$i18n.t('Remove from project')}</button
							>{/if}
					</div>{/each}
			</div>
		{:else if tab === 'context'}
			<form class="my-6 max-w-2xl space-y-5" on:submit|preventDefault={() => saveContext()}>
				<label class="block text-sm"
					>{$i18n.t('Project name')}<input
						class="mt-2 w-full rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 dark:border-gray-700"
						bind:value={form.name}
						required
						disabled={!project.write_access}
					/></label
				><label class="block text-sm"
					>{$i18n.t('Project instructions')}<textarea
						class="mt-2 w-full rounded-xl border border-gray-200 bg-transparent px-3 py-3 leading-6 dark:border-gray-700"
						rows="8"
						bind:value={form.system_prompt}
						disabled={!project.write_access}
					></textarea></label
				><label class="block text-sm"
					>{$i18n.t('Model presets')}<select
						class="mt-2 w-full rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 dark:border-gray-700"
						multiple
						bind:value={form.model_ids}
						disabled={!project.write_access}
						>{#each $models.filter((m) => !m.id.startsWith('agent:')) as model}<option
								value={model.id}>{model.name}</option
							>{/each}</select
					></label
				>
				<p class="text-sm text-gray-500">
					{$i18n.t(
						'Project conversations do not automatically read or write your cross-project personal memory.'
					)}
				</p>
				{#if project.write_access}<button
						class="rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
						disabled={busy}>{$i18n.t('Save project context')}</button
					>{/if}
			</form>
		{:else if tab === 'members'}
			<section class="my-6 max-w-2xl space-y-5">
				<p class="text-sm leading-6 text-gray-500">
					{$i18n.t(
						'Read access includes project references and shared conversations. Write access also allows editing project material. Neither grants access to another member’s private conversations or execution files.'
					)}
				</p>
				{#if project.user_id === $user?.id}<AccessControl
						bind:accessGrants={grants}
						sharePublic={false}
						accessRoles={['read', 'write']}
					/><button
						class="rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
						disabled={busy}
						on:click={() =>
							action(async () => {
								await saveTeam(`/projects/${project!.id}/access`, { access_grants: grants });
								toast.success($i18n.t('Saved'));
							})}>{$i18n.t('Save members')}</button
					>{:else}<p class="text-sm text-gray-500">
						{$i18n.t('The project owner manages membership.')}
					</p>{/if}
			</section>
		{/if}
	{/if}
	<ConfirmDialog
		show={conflict !== null}
		title={$i18n.t('Project context changed')}
		message={$i18n.t(
			'Someone else saved this project context ({{time}}) after you opened it. Overwrite their changes with yours? Choose Cancel to reload the latest version (your unsaved edits will be lost).',
			{ time: quotaDate(conflict?.updated_at) }
		)}
		confirmLabel={$i18n.t('Overwrite')}
		on:confirm={() => saveContext(true)}
		on:cancel={() => {
			conflict = null;
			void load();
		}}
	/>
</PageShell>
