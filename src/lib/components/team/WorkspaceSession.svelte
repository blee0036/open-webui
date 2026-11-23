<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { user } from '$lib/stores';
	import { terminalRequest, TerminalRequestError, type TerminalTarget } from '$lib/apis/terminal';
	import { createBrowserSession, getBrowserSession } from '$lib/apis/terminal/browser';
	import XTerminal from '$lib/components/chat/XTerminal.svelte';
	import Browser from '$lib/components/chat/Tabs/Browser.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	export let target: TerminalTarget;
	export let kind: 'terminal' | 'browser';
	const i18n = getContext<any>('i18n');
	let sessionId = '';
	let error = '';
	let busy = true;
	let disposed = false;
	$: cacheKey = `workspace-session:${$user?.id}:${target.baseUrl}:${target.context.chatId}:${kind}`;
	async function connect(fresh = false) {
		busy = true;
		error = '';
		try {
			let id = fresh ? '' : sessionStorage.getItem(cacheKey) || '';
			if (id) {
				try {
					if (kind === 'browser') await getBrowserSession(target, id);
					else await terminalRequest(target, `/api/terminals/${encodeURIComponent(id)}`);
				} catch (cause) {
					if (!(cause instanceof TerminalRequestError) || cause.status !== 404) throw cause;
					id = '';
				}
			}
			if (!id) {
				const session =
					kind === 'browser'
						? await createBrowserSession(target)
						: await terminalRequest<{ id: string }>(target, '/api/terminals', {
								method: 'POST'
							});
				id = session.id;
			}
			if (disposed) return;
			sessionStorage.setItem(cacheKey, id);
			sessionId = id;
		} catch (cause) {
			if (!disposed) error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			if (!disposed) busy = false;
		}
	}
	function unavailable(message: string, status?: number) {
		error = message;
		if (status === 404) sessionStorage.removeItem(cacheKey);
	}
	onMount(() => {
		connect();
		return () => {
			disposed = true;
		};
	});
</script>

{#if busy}
	<div class="flex h-full items-center justify-center" role="status"><Spinner /></div>
{:else if error}
	<div
		class="flex h-full flex-col items-center justify-center gap-4 p-6 text-center text-sm"
		role="alert"
	>
		<p>{error}</p>
		<button class="underline" on:click={() => connect()}>{$i18n.t('Retry')}</button>
	</div>
{:else if sessionId}
	{#if kind === 'terminal'}
		<XTerminal {target} {sessionId} onUnavailable={unavailable} />
	{:else}
		<Browser {target} baseUrl={target.baseUrl} {sessionId} onUnavailable={unavailable} />
	{/if}
{/if}
