import { OPENAI_API_BASE_URL, WEBUI_API_BASE_URL, WEBUI_BASE_URL } from '$lib/constants';
import i18next from 'i18next';

export const getErrorMessage = (err: any, fallback = 'Server connection failed') => {
	if (Array.isArray(err)) return fallback;
	// Background chat tasks report HTTP errors as {content: <detail>} (chat:message:error).
	const detail =
		err?.detail ??
		err?.error ??
		(err?.content && typeof err.content === 'object' ? err.content : undefined) ??
		err;
	if (detail?.code === 'quota_exhausted') {
		return detail.reset_at
			? i18next.t('Quota exhausted. Resets at {{time}}.', {
					time: new Date(detail.reset_at * 1000).toLocaleString()
				})
			: i18next.t('Quota exhausted. This package does not reset; contact an administrator.');
	}
	if (detail?.code === 'model_price_missing')
		return i18next.t('This model has no configured price. Ask an administrator to set its price.');
	return (
		[
			err?.detail?.error?.message,
			err?.detail?.message,
			err?.detail?.content,
			err?.detail?.error,
			err?.detail,
			err?.error?.message,
			err?.error?.content,
			err?.error,
			err?.message,
			err?.content,
			err
		].find((value) => typeof value === 'string' && value.trim()) ?? fallback
	);
};

export const getOpenAIConfig = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/config`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

type OpenAIConfig = {
	ENABLE_OPENAI_API: boolean;
	OPENAI_API_BASE_URLS: string[];
	OPENAI_API_KEYS: string[];
	OPENAI_API_CONFIGS: object;
};

export const updateOpenAIConfig = async (token: string = '', config: OpenAIConfig) => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/config/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		},
		body: JSON.stringify({
			...config
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			console.error(err);
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getOpenAIModels = async (token: string, urlIdx?: number) => {
	let error = null;

	const res = await fetch(
		`${OPENAI_API_BASE_URL}/models${typeof urlIdx === 'number' ? `/${urlIdx}` : ''}`,
		{
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				...(token && { authorization: `Bearer ${token}` })
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = `OpenAI: ${err?.error?.message ?? 'Network Problem'}`;
			return [];
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getProviderModelCatalog = async (token: string, urlIdx: number) => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/models/${urlIdx}/catalog`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(token && { authorization: `Bearer ${token}` })
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const downloadProviderModel = async (
	token: string,
	urlIdx: number,
	model: string,
	signal?: AbortSignal
) => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/models/${urlIdx}/download`, {
		signal,
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ model })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getProviderModelDownloadStatus = async (
	token: string,
	urlIdx: number,
	jobId: string,
	signal?: AbortSignal
) => {
	let error = null;

	const res = await fetch(
		`${OPENAI_API_BASE_URL}/models/${urlIdx}/download/status/${encodeURIComponent(jobId)}`,
		{
			signal,
			method: 'GET',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				Authorization: `Bearer ${token}`
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const loadProviderModel = async (token: string, urlIdx: number, model: string) => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/models/${urlIdx}/load`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ model })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const unloadProviderModel = async (
	token: string,
	urlIdx: number,
	model: string,
	instanceId?: string
) => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/models/${urlIdx}/unload`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ model, ...(instanceId ? { instance_id: instanceId } : {}) })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteProviderModel = async (token: string, urlIdx: number, model: string) => {
	let error = null;

	const res = await fetch(
		`${OPENAI_API_BASE_URL}/models/${urlIdx}?${new URLSearchParams({ model })}`,
		{
			method: 'DELETE',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				Authorization: `Bearer ${token}`
			}
		}
	)
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const verifyOpenAIConnection = async (
	token: string = '',
	connection: Record<string, any> = {}
) => {
	const { url, key, config } = connection;
	if (!url) {
		throw 'OpenAI: URL is required';
	}

	let error = null;
	let res = null;

	res = await fetch(`${OPENAI_API_BASE_URL}/verify`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify({
			url,
			key,
			config
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = `OpenAI: ${err?.error?.message ?? 'Network Problem'}`;
			return [];
		});

	if (error) {
		throw error;
	}

	return res;
};

export const chatCompletion = async (
	token: string = '',
	body: object,
	url: string = `${WEBUI_BASE_URL}/api`
): Promise<[Response | null, AbortController]> => {
	const controller = new AbortController();
	let error = null;

	const res = await fetch(`${url}/chat/completions`, {
		signal: controller.signal,
		method: 'POST',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify(body)
	}).catch((err) => {
		console.error(err);
		error = err;
		return null;
	});

	if (error) {
		throw error;
	}

	return [res, controller];
};

export const generateOpenAIChatCompletion = async (
	token: string = '',
	body: object,
	url: string = `${WEBUI_BASE_URL}/api`
) => {
	let error = null;

	const res = await fetch(`${url}/chat/completions`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		credentials: 'include',
		body: JSON.stringify(body)
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = getErrorMessage(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const synthesizeOpenAISpeech = async (
	token: string = '',
	speaker: string = 'alloy',
	text: string = '',
	model: string = 'tts-1'
) => {
	let error = null;

	const res = await fetch(`${OPENAI_API_BASE_URL}/audio/speech`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${token}`,
			'Content-Type': 'application/json'
		},
		body: JSON.stringify({
			model: model,
			input: text,
			voice: speaker
		})
	}).catch((err) => {
		console.error(err);
		error = err;
		return null;
	});

	if (error) {
		throw error;
	}

	return res;
};
