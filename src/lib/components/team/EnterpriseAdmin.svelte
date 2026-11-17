<script lang="ts">
	import { onMount, getContext } from 'svelte';
	import { toast } from 'svelte-sonner';
	import { teamRequest, saveTeam } from '$lib/apis/team';
	import { copyToClipboard } from '$lib/utils';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	const i18n = getContext<any>('i18n');
	let connections: any[] = [];
	let editing = false;
	let busy = false;
	let error = '';
	let identifier = '';
	let tenants = '';
	let identityRows: any[] = [];
	let link = { connection_id: '', tenant_id: '', subject: '', user_id: '', reason: '' };
	let confirmLink = false;
	// Share this with the enterprise's employees; the sign-in page never lists enterprises.
	const loginUrl = (id: string) => `${location.origin}/connect/${encodeURIComponent(id)}`;
	async function copyLoginUrl(id: string) {
		if (await copyToClipboard(loginUrl(id))) toast.success($i18n.t('Copied to clipboard'));
	}
	async function loadIdentities() {
		try {
			identityRows = await teamRequest('/enterprise/admin/identities');
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}
	async function linkIdentity() {
		busy = true;
		try {
			const { connection_id, ...data } = link;
			await saveTeam(
				`/enterprise/admin/connections/${encodeURIComponent(connection_id)}/identity`,
				data
			);
			confirmLink = false;
			await loadIdentities();
			toast.success($i18n.t('Identity linked; previous sessions revoked'));
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
	const blank = () => ({
		name: '',
		platform: 'feishu',
		client_id: '',
		client_secret: '',
		redirect_uri: '',
		scope: 'contact:user.base:readonly',
		enabled: true
	});
	let form = blank();
	async function load() {
		connections = await teamRequest('/enterprise/admin/connections');
	}
	function choose(row: any) {
		editing = true;
		identifier = row.id;
		tenants = row.tenant_ids.join('\n');
		form = { ...blank(), ...row, client_secret: '' };
	}
	function newConnection() {
		editing = false;
		identifier = '';
		tenants = '';
		form = blank();
	}
	function setCallback() {
		if (!editing && identifier)
			form.redirect_uri = `${location.origin}/api/v1/enterprise/${encodeURIComponent(identifier)}/callback`;
	}
	async function save() {
		busy = true;
		try {
			await saveTeam(`/enterprise/admin/connections/${encodeURIComponent(identifier)}`, {
				name: form.name,
				platform: form.platform,
				client_id: form.client_id,
				client_secret: form.client_secret || null,
				redirect_uri: form.redirect_uri,
				scope: form.scope,
				enabled: form.enabled,
				tenant_ids: tenants
					.split(/[\n,]/)
					.map((s) => s.trim())
					.filter(Boolean)
			});
			await load();
			form.client_secret = '';
			editing = true;
			error = '';
			toast.success($i18n.t('Saved'));
		} catch (e) {
			error = String(e instanceof Error ? e.message : e);
		} finally {
			busy = false;
		}
	}
	onMount(() => {
		load().catch((e) => (error = String(e)));
	});
</script>

<ConfirmDialog
	bind:show={confirmLink}
	title={$i18n.t('Link this enterprise identity to the selected account?')}
	on:confirm={linkIdentity}
/>

<div class="grid gap-9 lg:grid-cols-2">
	<section>
		<h2 class="text-lg font-medium">{$i18n.t('Enterprise login')}</h2>
		<p class="mt-2 text-sm leading-6 text-gray-500">
			{$i18n.t(
				'Each enterprise uses its own Lark or Feishu custom app. Enterprises are not listed on the sign-in page: employees enter the connection ID as their enterprise ID, or open the login URL below, which fills it in. Matching email addresses never merge accounts.'
			)}
		</p>
		<div class="mt-6 space-y-2">
			{#each connections as row}<div class="rounded-xl bg-gray-50 dark:bg-gray-900">
					<button
						class="flex w-full items-center justify-between p-4 pb-2 text-left"
						on:click={() => choose(row)}
						><span
							><strong class="text-sm">{row.name}</strong><span
								class="mt-1 block text-xs text-gray-500"
								>{$i18n.t('Enterprise ID')}: {row.id} · {row.platform}</span
							></span
						><span class="text-xs text-gray-500"
							>{$i18n.t(row.enabled ? 'Enabled' : 'Disabled')}</span
						></button
					>
					<div class="flex items-center gap-2 px-4 pb-3 text-xs">
						<span class="min-w-0 flex-1 truncate font-mono text-gray-500" title={loginUrl(row.id)}
							>{loginUrl(row.id)}</span
						><button type="button" class="shrink-0 underline" on:click={() => copyLoginUrl(row.id)}
							>{$i18n.t('Copy login URL')}</button
						>
					</div>
				</div>{/each}
		</div>
		<button class="mt-4 text-sm underline" on:click={newConnection}
			>{$i18n.t('Add enterprise connection')}</button
		>
	</section>
	<form class="space-y-4" on:submit|preventDefault={save}>
		{#if error}<p
				role="alert"
				class="rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
			>
				{error}
			</p>{/if}
		<label class="block text-sm"
			>{$i18n.t('Connection ID')}<input
				class="field mt-2"
				required
				pattern="[A-Za-z0-9_-]+"
				bind:value={identifier}
				disabled={editing}
				on:change={setCallback}
			/></label
		>
		<label class="block text-sm"
			>{$i18n.t('Enterprise name')}<input
				class="field mt-2"
				required
				bind:value={form.name}
			/></label
		>
		<label class="block text-sm"
			>{$i18n.t('Platform')}<select class="field mt-2" bind:value={form.platform}
				><option value="feishu">飞书 / Feishu</option><option value="lark">Lark</option></select
			></label
		>
		<label class="block text-sm"
			>App ID<input
				class="field mt-2"
				required
				bind:value={form.client_id}
				autocomplete="off"
			/></label
		>
		<label class="block text-sm"
			>App Secret<input
				type="password"
				class="field mt-2"
				bind:value={form.client_secret}
				required={!editing}
				autocomplete="new-password"
				placeholder={editing ? $i18n.t('Leave blank to keep the existing secret') : ''}
			/></label
		>
		<label class="block text-sm"
			>{$i18n.t('Callback URL')}<input
				class="field mt-2"
				type="url"
				required
				bind:value={form.redirect_uri}
			/></label
		>
		<label class="block text-sm"
			>{$i18n.t('Allowed tenant keys (one per line)')}<textarea
				class="field mt-2"
				rows="3"
				required
				bind:value={tenants}
			></textarea></label
		>
		<label class="block text-sm"
			>{$i18n.t('Authorization scope')}<input
				class="field mt-2"
				required
				bind:value={form.scope}
			/></label
		>
		<label class="flex items-center gap-3 text-sm"
			><input type="checkbox" bind:checked={form.enabled} />{$i18n.t('Enabled')}</label
		>
		<p class="text-sm text-gray-500">
			{$i18n.t(
				'Changing or disabling a connection revokes its existing sessions and outstanding login callbacks.'
			)}
		</p>
		<button
			class="rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
			disabled={busy}>{$i18n.t('Save enterprise connection')}</button
		>
	</form>
</div>

<details
	class="mt-9 border-t border-gray-200 pt-5 dark:border-gray-800"
	on:toggle={(event) => {
		if ((event.currentTarget as HTMLDetailsElement).open) loadIdentities();
	}}
>
	<summary class="cursor-pointer text-sm font-medium">{$i18n.t('Link an existing account')}</summary
	>
	<p class="my-4 max-w-3xl text-sm leading-6 text-gray-500">
		{$i18n.t(
			'Verify tenant_key and open_id in the enterprise directory before linking. Linking changes the account used for future sign-ins and revokes sessions of both affected accounts.'
		)}
	</p>
	<form class="grid gap-4 md:grid-cols-2" on:submit|preventDefault={() => (confirmLink = true)}>
		<label class="text-sm"
			>{$i18n.t('Enterprise connection')}<select
				class="field mt-2"
				bind:value={link.connection_id}
				required
				><option value="">{$i18n.t('Select')}</option>{#each connections as row}<option
						value={row.id}>{row.name}</option
					>{/each}</select
			></label
		>
		<label class="text-sm"
			>tenant_key<input class="field mt-2" bind:value={link.tenant_id} required /></label
		>
		<label class="text-sm"
			>open_id<input class="field mt-2" bind:value={link.subject} required /></label
		>
		<label class="text-sm"
			>{$i18n.t('Existing internal user ID')}<input
				class="field mt-2"
				bind:value={link.user_id}
				required
			/></label
		>
		<label class="text-sm"
			>{$i18n.t('Reason')}<input class="field mt-2" bind:value={link.reason} required /></label
		>
		<button
			class="self-end justify-self-start rounded-xl bg-gray-900 px-4 py-2.5 text-sm text-white disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900"
			disabled={busy}>{$i18n.t('Verify and link identity')}</button
		>
	</form>
	<div class="mt-5 space-y-2">
		{#each identityRows as row}<button
				class="block w-full rounded-xl bg-gray-50 p-3 text-left text-sm dark:bg-gray-900"
				on:click={() => {
					link = {
						connection_id: row.connection_id,
						tenant_id: row.tenant_id,
						subject: row.subject,
						user_id: row.user_id,
						reason: ''
					};
				}}
				><span class="font-medium">{row.profile?.name || row.subject}</span><span
					class="mt-1 block break-all text-xs text-gray-500"
					>{row.connection_id} · {row.tenant_id} · {row.subject} → {row.user_id}</span
				></button
			>{/each}
	</div>
</details>

<style>
	@reference "../../../tailwind.css";
	.field {
		@apply block w-full min-w-0 rounded-xl border border-gray-200 bg-transparent px-3 py-2.5 text-sm dark:border-gray-700;
	}
</style>
