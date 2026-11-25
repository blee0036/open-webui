<script lang="ts">
	import { getContext } from 'svelte';
	import Modal from '../common/Modal.svelte';
	import XMark from '../icons/XMark.svelte';
	import { WEBUI_API_BASE_URL } from '$lib/constants';

	const i18n = getContext<any>('i18n');

	export let show = false;
	/** Reset notice from getErrorMessage, e.g. "Quota exhausted. Resets at …". */
	export let message = '';
	export let freeModels: { id: string; name?: string }[] = [];
	export let onSwitch: (modelId: string) => void = () => {};
</script>

<Modal ariaLabel={$i18n.t('Quota exhausted')} bind:show size="sm">
	<div class="flex justify-between px-4 pt-3 pb-1 dark:text-gray-300">
		<div class="self-center text-sm font-medium">{$i18n.t('Quota exhausted')}</div>
		<button
			type="button"
			aria-label={$i18n.t('Close')}
			class="self-center rounded-lg px-0 py-1 text-gray-500 transition hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
			on:click={() => (show = false)}
		>
			<XMark className="size-4" />
		</button>
	</div>
	<div class="flex flex-col gap-3 px-4 pb-4 text-sm dark:text-gray-200">
		<p class="text-gray-600 dark:text-gray-300">{message}</p>
		<p>{$i18n.t('Free models still work without any balance. Switch and send again:')}</p>
		<div class="flex flex-col gap-1">
			{#each freeModels as model (model.id)}
				<button
					type="button"
					class="flex items-center gap-2 rounded-xl px-2.5 py-2 text-left transition hover:bg-gray-50 dark:hover:bg-gray-850"
					on:click={() => {
						show = false;
						onSwitch(model.id);
					}}
				>
					<img
						src={`${WEBUI_API_BASE_URL}/models/model/profile/image?id=${model.id}&lang=${$i18n.language}`}
						alt=""
						class="size-5 rounded-full"
						loading="lazy"
						on:error={(e) => {
							(e.currentTarget as HTMLImageElement).src = '/favicon.png';
						}}
					/>
					<span class="line-clamp-1 flex-1">{model.name ?? model.id}</span>
					<span
						class="shrink-0 rounded-md bg-green-500/15 px-1 text-[0.6875rem] font-medium text-green-700 dark:text-green-300"
						>{$i18n.t('Free')}</span
					>
				</button>
			{/each}
		</div>
		<div class="flex justify-end pt-1">
			<button
				type="button"
				class="flex h-7 items-center justify-center rounded-lg bg-gray-100 px-2.5 text-xs font-normal text-gray-800 transition hover:bg-gray-100/70 dark:bg-gray-850 dark:text-white dark:hover:bg-gray-850/60"
				on:click={() => (show = false)}>{$i18n.t('Cancel')}</button
			>
		</div>
	</div>
</Modal>
