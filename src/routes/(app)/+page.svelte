<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import Chat from '$lib/components/chat/Chat.svelte';
	import { page } from '$app/stores';
	import { selectedFolder, temporaryChatEnabled } from '$lib/stores';
	import { teamRequest } from '$lib/apis/team';
	let loadedProject = '';
	let ready = !$page.url.searchParams.has('project');
	async function selectProject(id: string) {
		ready = false;
		loadedProject = id;
		try {
			const project = await teamRequest(`/projects/${encodeURIComponent(id)}`);
			if (loadedProject === id) {
				selectedFolder.set(project);
				temporaryChatEnabled.set(false);
			}
		} catch (error) {
			selectedFolder.set(null);
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			if (loadedProject === id) ready = true;
		}
	}
	$: if (
		$page.url.searchParams.get('project') &&
		$page.url.searchParams.get('project') !== loadedProject
	) {
		void selectProject($page.url.searchParams.get('project')!);
	}
	$: if (!$page.url.searchParams.has('project') && loadedProject) {
		loadedProject = '';
		selectedFolder.set(null);
		ready = true;
	}

	onMount(() => {
		if ($page.url.searchParams.get('error')) {
			toast.error($page.url.searchParams.get('error') || 'An unknown error occurred.');
		}
	});
</script>

{#if ready}<Chat />{/if}
