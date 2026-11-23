<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { saveTeam, teamRequest, type WorkspaceStatus } from '$lib/apis/team';

	/** 'personal' or a project id, as the Work environment page uses. */
	export let scope: string;
	/** Only the managed workspace has an Agent queue to pause. */
	export let active = false;

	const i18n = getContext<any>('i18n');
	const POLL_MS = 15000;
	let workspace: WorkspaceStatus | null = null;
	let busy = false;
	let requestId = 0;

	async function refresh() {
		if (!active || typeof document === 'undefined' || document.hidden) return;
		const id = ++requestId;
		try {
			const value = await teamRequest<WorkspaceStatus>(`/sandboxes/${encodeURIComponent(scope)}`);
			if (id === requestId) workspace = value;
		} catch {
			// Status is advisory here; the Work environment page shows errors.
		}
	}

	async function resume() {
		busy = true;
		try {
			workspace = await saveTeam(
				`/sandboxes/${encodeURIComponent(scope)}`,
				{ action: 'resume' },
				'POST'
			);
			toast.success($i18n.t('Agent commands resumed'));
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			busy = false;
		}
	}

	$: if (active && scope) {
		workspace = null;
		void refresh();
	}
	onMount(() => {
		const interval = setInterval(refresh, POLL_MS);
		const onVisible = () => void refresh();
		document.addEventListener('visibilitychange', onVisible);
		return () => {
			clearInterval(interval);
			document.removeEventListener('visibilitychange', onVisible);
		};
	});
</script>

{#if active && workspace?.manual}
	<div
		role="status"
		class="mx-auto mb-2 flex w-full max-w-3xl items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-100"
	>
		<span class="min-w-0">
			{workspace.agent_queue
				? $i18n.t('Workspace is under manual control; {{count}} Agent commands are waiting.', {
						count: workspace.agent_queue
					})
				: $i18n.t('Workspace is under manual control; Agent commands are paused.')}
		</span>
		<button
			class="shrink-0 rounded-lg bg-amber-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50 dark:bg-amber-100 dark:text-amber-950"
			disabled={busy}
			on:click={resume}>{$i18n.t('Resume Agent')}</button
		>
	</div>
{/if}
