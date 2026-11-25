import { writable, get } from 'svelte/store';
import { getModelPrices, type ModelPriceList, type ModelPriceSummary } from '$lib/apis/team';

/** Member-visible model rates, loaded when the model list opens (team quota mode only). */
export const modelPrices = writable<ModelPriceList | null>(null);

let loading: Promise<void> | null = null;

export const loadModelPrices = () => {
	loading ??= getModelPrices()
		.then((list) => modelPrices.set(list))
		.catch(() => {})
		.finally(() => {
			loading = null;
		});
	return loading;
};

/** A workspace model is priced by its own id first, then by its base model (as the backend does). */
export const priceOf = (list: ModelPriceList | null, model: any): ModelPriceSummary | undefined => {
	if (!list?.enabled || !model) return undefined;
	return list.prices[model.id] ?? list.prices[model.info?.base_model_id ?? ''];
};

export const isFreeModel = (model: any) => !!priceOf(get(modelPrices), model)?.free;
