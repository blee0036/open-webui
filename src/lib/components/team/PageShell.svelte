<script lang="ts">
	import { getContext } from 'svelte';
	import { showSidebar, mobile } from '$lib/stores';
	import Sidebar from '$lib/components/icons/Sidebar.svelte';
	const i18n = getContext<any>('i18n');
	export let title = '';
	export let wide = false;
</script>

<div
	class="h-screen max-h-[100dvh] min-w-0 flex-1 overflow-y-auto {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''}"
>
	<div class="mx-auto w-full {wide ? 'max-w-screen-2xl' : 'max-w-5xl'} px-5 py-6 md:p-9">
		<header class="mb-7 flex items-center gap-3">
			{#if $mobile}<button
					class="rounded-lg p-2 hover:bg-gray-100 dark:hover:bg-gray-800"
					aria-label={$i18n.t('Open Sidebar')}
					on:click={() => showSidebar.set(true)}><Sidebar className="size-5" /></button
				>{/if}
			<h1 class="flex-1 text-2xl font-semibold tracking-tight">{title}</h1>
			<slot name="actions" />
		</header>
		<slot />
	</div>
</div>
