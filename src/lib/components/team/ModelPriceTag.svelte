<script lang="ts">
	import { getContext } from 'svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import { contextSize, priceRatio } from '$lib/apis/team';
	import { modelPrices, priceOf } from './modelPrices';

	const i18n = getContext<any>('i18n');

	export let model: any;

	$: list = $modelPrices;
	$: price = priceOf(list, model);
	$: requests = list?.unit === 'requests';
	$: rates = (input?: number, output?: number) =>
		$i18n.t('In ×{{input}} Out ×{{output}}', {
			input: priceRatio(input),
			output: priceRatio(output)
		});
	$: tip = [
		price?.long
			? $i18n.t('Over {{size}} context: {{rates}}', {
					size: contextSize(price.long.above),
					rates: rates(price.long.input, price.long.output)
				})
			: '',
		price && !price.free && price.per_call === undefined
			? $i18n.t('1× = $2 per million input tokens')
			: '',
		requests && price && !price.free
			? $i18n.t('Request-count plan: every request counts as 1; the ratio is for reference')
			: ''
	]
		.filter(Boolean)
		.join('<br>');
</script>

{#if list?.enabled}
	{#if price?.free}
		<span
			class="shrink-0 rounded-md bg-green-500/15 px-1 text-[0.6875rem] font-medium text-green-700 dark:text-green-300"
		>
			{$i18n.t('Free')}
		</span>
	{:else if price}
		<Tooltip content={tip} allowHTML={true} placement="top-start">
			<span
				class="line-clamp-1 shrink-0 text-[0.6875rem] font-normal text-gray-500 dark:text-gray-400"
			>
				{#if price.per_call !== undefined}
					{$i18n.t('${{amount}} / call', { amount: Number(price.per_call.toPrecision(3)) })}
				{:else}
					{rates(price.input, price.output)}{price.long ? '+' : ''}
				{/if}
			</span>
		</Tooltip>
	{:else if !requests}
		<span class="shrink-0 text-[0.6875rem] font-normal text-gray-400 dark:text-gray-500">
			{$i18n.t('No price')}
		</span>
	{/if}
{/if}
