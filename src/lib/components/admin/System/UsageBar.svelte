<script lang="ts">
	/** A thin usage bar: amber from `warn`%, red from `full`% (or when `over`). */
	export let used: number | null | undefined = 0;
	export let total: number | null | undefined = null;
	export let over = false;
	export let warn = 80;
	export let full = 95;

	$: percent = total ? Math.max(0, ((used ?? 0) / total) * 100) : 0;
	$: tone =
		over || percent >= full
			? 'bg-red-500'
			: percent >= warn
				? 'bg-amber-500'
				: 'bg-emerald-600 dark:bg-emerald-400';
</script>

{#if total}
	<div
		class="h-1.5 overflow-hidden rounded-full bg-black/10 dark:bg-white/10"
		role="meter"
		aria-valuemin={0}
		aria-valuemax={100}
		aria-valuenow={Math.round(percent)}
	>
		<div class="h-full rounded-full {tone}" style:width="{Math.min(100, percent)}%"></div>
	</div>
{/if}
