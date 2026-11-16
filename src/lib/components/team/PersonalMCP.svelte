<script lang="ts">
	import { getContext, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { teamRequest, saveTeam } from '$lib/apis/team';
	import { initiateOAuthRedirect } from '$lib/apis/configs';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	const i18n = getContext<any>('i18n');
	let connections: any[] = [];
	let editing = false;
	let identifier = '';
	let busy = false;
	let error = '';
	let deleting: any = null;
	let confirmDelete = false;
	const blank = () => ({
		name: '',
		url: '',
		auth_type: 'none',
		key: '',
		oauth_client_id: '',
		oauth_client_secret: '',
		oauth_scope: '',
		enabled: true,
		clear_credentials: false
	});
	let form = blank();
	let headers = '';
	async function load() {
		try {
			connections = await teamRequest('/personal-connections');
			error = '';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}
	function choose(row: any = null) {
		identifier = row?.id ?? crypto.randomUUID().replaceAll('-', '');
		form = {
			...blank(),
			...(row
				? { name: row.name, url: row.url, auth_type: row.auth_type, enabled: row.enabled }
				: {})
		};
		headers = '';
		editing = true;
	}
	async function save() {
		busy = true;
		try {
			const extraHeaders = headers.trim() ? JSON.parse(headers) : null;
			await saveTeam(`/personal-connections/${identifier}`, {
				...form,
				key: form.key || null,
				headers: extraHeaders,
				oauth_client_id: form.oauth_client_id || null,
				oauth_client_secret: form.oauth_client_secret || null,
				oauth_scope: form.oauth_scope || null
			});
			form = blank();
			headers = '';
			editing = false;
			await load();
			toast.success($i18n.t('Saved'));
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
	async function remove() {
		busy = true;
		try {
			await teamRequest(`/personal-connections/${deleting.id}`, { method: 'DELETE' });
			await load();
			confirmDelete = false;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
	onMount(load);
</script>

<ConfirmDialog
	bind:show={confirmDelete}
	title={$i18n.t('Delete personal MCP connection?')}
	on:confirm={remove}
/>
<section class="mb-7" aria-labelledby="personal-mcp-heading">
	<div class="flex items-center justify-between gap-3">
		<h3 id="personal-mcp-heading" class="font-medium">{$i18n.t('Personal MCP connections')}</h3>
		<button
			type="button"
			class="rounded-lg px-3 py-2 text-sm hover:bg-gray-100 dark:hover:bg-gray-800"
			on:click={() => choose()}>{$i18n.t('Add connection')}</button
		>
	</div>
	<p class="my-3 text-sm leading-6 text-gray-500">
		{$i18n.t(
			'Only you can use these connections. Credentials are stored encrypted and are never included in shared tools or conversations.'
		)}
	</p>
	{#if error}<p
			role="alert"
			class="mb-3 rounded-xl bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-200"
		>
			{error}
		</p>{/if}
	<div class="divide-y divide-gray-100 dark:divide-gray-800">
		{#each connections as row}<div class="flex flex-wrap items-center gap-3 py-3">
				<button type="button" class="min-w-0 flex-1 text-left" on:click={() => choose(row)}
					><p class="truncate text-sm font-medium">
						{row.name}
						{row.enabled ? '' : '· ' + $i18n.t('Disabled')}
					</p>
					<p class="truncate text-xs text-gray-500">{row.url}</p></button
				>{#if row.enabled && row.auth_type.startsWith('oauth_')}<button
						type="button"
						class="text-sm underline"
						on:click={() =>
							initiateOAuthRedirect({
								id: `server:mcp:${row.server_id}`,
								serverId: row.server_id,
								authType: 'mcp'
							})}>{$i18n.t('Authorize')}</button
					>{/if}<button
					type="button"
					class="px-2 py-1 text-sm text-gray-500"
					disabled={busy}
					on:click={() => {
						deleting = row;
						confirmDelete = true;
					}}>{$i18n.t('Delete')}</button
				>
			</div>{/each}
	</div>
	{#if !connections.length && !editing}<p class="py-2 text-sm text-gray-500">
			{$i18n.t('No personal MCP connections yet.')}
		</p>{/if}
	{#if editing}
		<form
			class="mt-4 space-y-4 rounded-xl border border-gray-200 p-4 dark:border-gray-700"
			on:submit|preventDefault={save}
		>
			<label class="block text-sm"
				>{$i18n.t('Name')}<input class="field mt-2" required bind:value={form.name} /></label
			>
			<label class="block text-sm"
				>{$i18n.t('MCP endpoint URL')}<input
					class="field mt-2"
					type="url"
					required
					bind:value={form.url}
					placeholder="https://mcp.example.com/mcp"
				/></label
			>
			<label class="block text-sm"
				>{$i18n.t('Authentication')}<select class="field mt-2" bind:value={form.auth_type}
					><option value="none">{$i18n.t('None')}</option><option value="bearer">Bearer</option
					><option value="oauth_2.1">OAuth 2.1</option><option value="oauth_2.1_static"
						>OAuth 2.1 · {$i18n.t('Static client')}</option
					></select
				></label
			>
			{#if form.auth_type === 'bearer'}<label class="block text-sm"
					>{$i18n.t('Connection access token')}<input
						class="field mt-2"
						type="password"
						autocomplete="new-password"
						bind:value={form.key}
						placeholder={$i18n.t('Leave blank to keep the existing secret')}
					/></label
				>{/if}
			{#if form.auth_type === 'oauth_2.1_static'}<label class="block text-sm"
					>Client ID<input
						class="field mt-2"
						bind:value={form.oauth_client_id}
						placeholder={$i18n.t('Leave blank to keep existing credentials')}
					/></label
				><label class="block text-sm"
					>Client Secret<input
						class="field mt-2"
						type="password"
						autocomplete="new-password"
						bind:value={form.oauth_client_secret}
						placeholder={$i18n.t('Leave blank to keep the existing secret')}
					/></label
				>{/if}
			{#if form.auth_type.startsWith('oauth_')}<label class="block text-sm"
					>{$i18n.t('Authorization scope')}<input
						class="field mt-2"
						bind:value={form.oauth_scope}
					/></label
				>
				<p class="text-sm text-gray-500">
					{$i18n.t(
						'Save first, then authorize this connection. Editing a connection revokes its previous authorization.'
					)}
				</p>{/if}
			<details>
				<summary class="cursor-pointer text-sm">{$i18n.t('Custom headers and credentials')}</summary
				><label class="mt-3 block text-sm"
					>{$i18n.t('Custom headers (JSON)')}<textarea
						class="field mt-2 font-mono"
						rows="3"
						bind:value={headers}
						placeholder={'{"X-Access-Token":"…"}'}
					></textarea></label
				>
				<p class="mt-2 text-sm text-gray-500">
					{$i18n.t(
						'Blank keeps existing headers. Use {} to remove headers, or clear all credentials below.'
					)}
				</p>
				<label class="mt-3 flex items-center gap-2 text-sm"
					><input type="checkbox" bind:checked={form.clear_credentials} />{$i18n.t(
						'Clear stored credentials before saving'
					)}</label
				>
			</details>
			<label class="flex items-center gap-2 text-sm"
				><input type="checkbox" bind:checked={form.enabled} />{$i18n.t('Enabled')}</label
			>
			<div class="flex gap-3">
				<button
					class="rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
					disabled={busy}>{$i18n.t('Save connection')}</button
				><button
					type="button"
					class="px-3 py-2 text-sm"
					disabled={busy}
					on:click={() => {
						editing = false;
						form = blank();
						headers = '';
					}}>{$i18n.t('Cancel')}</button
				>
			</div>
		</form>
	{/if}
</section>

<style>
	@reference "../../../tailwind.css";
	.field {
		@apply block w-full min-w-0 rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 text-sm dark:border-gray-700;
	}
</style>
