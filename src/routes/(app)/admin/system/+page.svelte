<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import {
		getSandboxReport,
		getStorageReport,
		getSystemOverview,
		type SandboxReport,
		type StorageReport,
		type SystemOverview
	} from '$lib/apis/system';
	import { secondary } from '$lib/components/team/admin/ui';
	import HostCards from '$lib/components/admin/System/HostCards.svelte';
	import StorageTable from '$lib/components/admin/System/StorageTable.svelte';
	import SandboxTable from '$lib/components/admin/System/SandboxTable.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	const i18n = getContext<any>('i18n');
	const REFRESH_MS = 30000;

	let overview: SystemOverview | null = null;
	let storage: StorageReport | null = null;
	let sandboxes: SandboxReport | null = null;
	// Each block fails on its own, so one broken service does not blank the page.
	let errors: Record<string, string> = {};
	let loading = false;

	const message = (e: unknown) => String(e instanceof Error ? e.message : e);

	async function load() {
		if (loading) return;
		loading = true;
		const [o, s, b] = await Promise.allSettled([
			getSystemOverview(),
			getStorageReport(),
			getSandboxReport()
		]);
		errors = {};
		if (o.status === 'fulfilled') overview = o.value;
		else errors.overview = message(o.reason);
		if (s.status === 'fulfilled') storage = s.value;
		else errors.storage = message(s.reason);
		if (b.status === 'fulfilled') sandboxes = b.value;
		else errors.sandboxes = message(b.reason);
		loading = false;
	}

	onMount(() => {
		load();
		const interval = setInterval(() => {
			if (!document.hidden) load();
		}, REFRESH_MS);
		return () => clearInterval(interval);
	});
</script>

<svelte:head>
	<title>{$i18n.t('System')}</title>
</svelte:head>

<div class="mx-auto w-full max-w-7xl space-y-5 px-4 pb-10 pt-2 md:px-6">
	<div class="flex flex-wrap items-center justify-between gap-3">
		<div>
			<h2 class="text-lg font-medium">{$i18n.t('System')}</h2>
			<p class="text-sm text-gray-500">
				{$i18n.t('Server resources, storage per member and sandboxes. Refreshes every 30 seconds.')}
			</p>
		</div>
		<button class={secondary} on:click={load} disabled={loading}>
			{#if loading}<Spinner className="size-3.5" />{/if}
			{$i18n.t('Refresh')}
		</button>
	</div>

	{#each Object.entries(errors) as [key, text] (key)}
		<p
			role="alert"
			class="rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
		>
			{text}
		</p>
	{/each}

	{#if overview}
		<HostCards {overview} />
	{:else if !errors.overview}
		<div class="flex justify-center py-10"><Spinner /></div>
	{/if}

	{#if sandboxes}<SandboxTable report={sandboxes} onChange={load} />{/if}
	{#if storage}<StorageTable report={storage} />{/if}
</div>
