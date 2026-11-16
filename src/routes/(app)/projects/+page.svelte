<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { teamRequest, saveTeam } from '$lib/apis/team';
	import type { Project } from '$lib/types/team';
	import PageShell from '$lib/components/team/PageShell.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	const i18n = getContext<any>('i18n');
	let projects: Project[] = [];
	let name = '';
	let creating = false;
	let loading = true;
	let busy = false;
	let error = '';
	async function load() {
		try {
			projects = await teamRequest('/projects');
			error = '';
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			loading = false;
		}
	}
	async function create() {
		busy = true;
		try {
			const project = await saveTeam('/projects', { name: name.trim() }, 'POST');
			await goto(`/projects/${project.id}`);
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = false;
		}
	}
	onMount(load);
</script>

<PageShell title={$i18n.t('Projects')}>
	<button
		slot="actions"
		class="rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white dark:bg-gray-100 dark:text-gray-900"
		on:click={() => (creating = !creating)}>{$i18n.t('New project')}</button
	>
	<p class="mb-7 max-w-2xl text-sm leading-6 text-gray-500">
		{$i18n.t(
			'Keep project instructions and references together. Share material with your team; your conversations and execution files stay private until you publish them.'
		)}
	</p>
	{#if error}<div
			role="alert"
			class="mb-5 rounded-xl bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{error} <button class="underline" on:click={load}>{$i18n.t('Retry')}</button>
		</div>{/if}
	{#if creating}<form
			class="mb-7 flex flex-wrap gap-3 rounded-2xl bg-gray-50 p-5 dark:bg-gray-900"
			on:submit|preventDefault={create}
		>
			<label class="min-w-0 flex-1 text-sm"
				>{$i18n.t('Project name')}<input
					class="mt-2 block w-full rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 dark:border-gray-700"
					bind:value={name}
					required
					maxlength="120"
				/></label
			><button
				class="self-end rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
				disabled={busy || !name.trim()}>{$i18n.t('Create project')}</button
			>
		</form>{/if}
	{#if loading}<div class="py-16 flex justify-center"><Spinner /></div>
	{:else if !projects.length}<div class="py-20 text-center">
			<h2 class="text-lg font-medium">{$i18n.t('A place for your next project')}</h2>
			<p class="mt-2 text-sm text-gray-500">
				{$i18n.t('Create a project, add references, then start a conversation.')}
			</p>
		</div>
	{:else}<div class="grid gap-4 sm:grid-cols-2">
			{#each projects as project}<a
					href="/projects/{project.id}"
					class="group min-w-0 rounded-2xl border border-gray-200 p-6 transition hover:border-gray-400 dark:border-gray-800 dark:hover:border-gray-600"
					><div class="flex items-start justify-between gap-4">
						<h2 class="truncate text-lg font-medium">{project.name}</h2>
						<span class="text-gray-400 group-hover:text-gray-800 dark:group-hover:text-gray-200"
							>↗</span
						>
					</div>
					<p class="mt-3 line-clamp-2 min-h-12 text-sm leading-6 text-gray-500">
						{project.data?.system_prompt || $i18n.t('No project instructions yet.')}
					</p>
					<p class="mt-5 text-xs text-gray-500">
						{$i18n.t('{{count}} reference files', { count: project.data?.files?.length ?? 0 })}
					</p></a
				>{/each}
		</div>{/if}
</PageShell>
