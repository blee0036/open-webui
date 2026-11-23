<script lang="ts">
	import { toast } from 'svelte-sonner';

	import { page } from '$app/stores';
	import { selectedFolder, temporaryChat } from '$lib/stores';
	import { teamRequest } from '$lib/apis/team';
	// The chat itself is rendered by the (app) layout; this page only applies ?project=<id>.
	let loadedProject = '';
	async function selectProject(id: string) {
		loadedProject = id;
		try {
			const project = await teamRequest(`/projects/${encodeURIComponent(id)}`);
			if (loadedProject === id) {
				selectedFolder.set(project);
				temporaryChat.set(false);
			}
		} catch (error) {
			selectedFolder.set(null);
			toast.error(error instanceof Error ? error.message : String(error));
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
	}

	$: error = $page.url.searchParams.get('error');
	$: if (error) toast.error(error);
</script>
