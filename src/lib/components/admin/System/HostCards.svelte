<script lang="ts">
	import { getContext } from 'svelte';
	import dayjs from 'dayjs';
	import relativeTime from 'dayjs/plugin/relativeTime';
	import type { SystemOverview } from '$lib/apis/system';
	import { card } from '$lib/components/team/admin/ui';
	import { formatBytes } from '$lib/utils/storageQuota';
	import UsageBar from './UsageBar.svelte';

	dayjs.extend(relativeTime);

	const i18n = getContext<any>('i18n');

	export let overview: SystemOverview;

	$: host = overview.host;
	$: sandbox = overview.sandbox;
	$: memoryUsed = host.memory.total - host.memory.available;

	const label = 'text-xs text-gray-500';
	const metric = 'mt-1 flex items-baseline justify-between gap-2 text-sm';
</script>

<div class="grid gap-4 lg:grid-cols-3">
	<section class="{card} p-5" aria-labelledby="system-webui">
		<div class="flex items-baseline justify-between gap-2">
			<h3 id="system-webui" class="font-medium">{$i18n.t('WebUI server')}</h3>
			<span class="truncate text-xs text-gray-500" title={host.hostname}>{host.hostname ?? ''}</span
			>
		</div>
		<p class="mt-1 text-xs text-gray-500">
			{$i18n.t('The replica that answered this page. Other replicas may differ.')}
		</p>
		<div class="mt-4 space-y-4">
			<div>
				<div class={metric}>
					<span>{$i18n.t('Memory')}</span>
					<span class="tabular-nums"
						>{formatBytes(memoryUsed)} / {formatBytes(host.memory.total)}</span
					>
				</div>
				<div class="mt-1.5"><UsageBar used={memoryUsed} total={host.memory.total} warn={85} /></div>
				<p class="{label} mt-1">
					{$i18n.t('{{free}} available', { free: formatBytes(host.memory.available) })}
					{#if host.swap.total}· {$i18n.t('Swap')}
						{formatBytes(host.swap.used)} / {formatBytes(host.swap.total)}{/if}
				</p>
			</div>
			{#each host.disks as disk}
				<div>
					<div class={metric}>
						<span>{$i18n.t('Disk')}</span>
						<span class="tabular-nums">{formatBytes(disk.used)} / {formatBytes(disk.total)}</span>
					</div>
					<div class="mt-1.5"><UsageBar used={disk.used} total={disk.total} warn={85} /></div>
					<p class="{label} mt-1 truncate" title={disk.path}>
						{$i18n.t('{{free}} free', { free: formatBytes(disk.free) })} · {disk.path}
					</p>
				</div>
			{/each}
			<dl class="grid grid-cols-2 gap-3 text-sm">
				<div>
					<dt class={label}>{$i18n.t('CPU')}</dt>
					<dd class="tabular-nums">
						{host.cpu_percent.toFixed(0)}% · {$i18n.t('{{count}} cores', { count: host.cpus })}
					</dd>
				</div>
				{#if host.load}
					<div>
						<dt class={label}>{$i18n.t('Load average')}</dt>
						<dd class="tabular-nums">{host.load.map((value) => value.toFixed(2)).join(' / ')}</dd>
					</div>
				{/if}
				<div>
					<dt class={label}>{$i18n.t('WebUI process')}</dt>
					<dd class="tabular-nums">{formatBytes(host.process.rss)}</dd>
				</div>
				<div>
					<dt class={label}>{$i18n.t('Started')}</dt>
					<dd>{dayjs(host.process.started_at * 1000).fromNow()}</dd>
				</div>
			</dl>
		</div>
	</section>

	<section class="{card} p-5" aria-labelledby="system-sandbox">
		<h3 id="system-sandbox" class="font-medium">{$i18n.t('Sandbox host')}</h3>
		{#if !sandbox.configured}
			<p class="mt-3 text-sm text-gray-500">{$i18n.t('The sandbox service is not configured.')}</p>
		{:else if !sandbox.available}
			<p
				role="alert"
				class="mt-3 rounded-xl bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-200"
			>
				{$i18n.t('The sandbox service is unavailable.')}
				{#if sandbox.error}<span class="block text-xs opacity-80">{sandbox.error}</span>{/if}
			</p>
		{:else}
			<p class="mt-1 text-xs text-gray-500">Docker {sandbox.docker_version ?? ''}</p>
			<div class="mt-4 space-y-4">
				<div>
					<div class={metric}>
						<span>{$i18n.t('Running sandboxes')}</span>
						<span class="tabular-nums">{sandbox.running ?? 0} / {sandbox.limit ?? 0}</span>
					</div>
					<div class="mt-1.5">
						<UsageBar used={sandbox.running} total={sandbox.limit} warn={80} full={100} />
					</div>
					{#if sandbox.queued}
						<p class="{label} mt-1 text-amber-700 dark:text-amber-300">
							{$i18n.t('{{count}} waiting for a free slot', { count: sandbox.queued })}
						</p>
					{/if}
				</div>
				{#if sandbox.disk}
					<div>
						<div class={metric}>
							<span>{$i18n.t('Docker data disk')}</span>
							<span class="tabular-nums"
								>{formatBytes(sandbox.disk.used)} / {formatBytes(sandbox.disk.total)}</span
							>
						</div>
						<div class="mt-1.5">
							<UsageBar used={sandbox.disk.used} total={sandbox.disk.total} warn={85} />
						</div>
						<p class="{label} mt-1">
							{$i18n.t('{{free}} free', { free: formatBytes(sandbox.disk.free) })}
						</p>
					</div>
				{/if}
				<dl class="grid grid-cols-2 gap-3 text-sm">
					<div>
						<dt class={label}>{$i18n.t('Host memory')}</dt>
						<dd class="tabular-nums">{formatBytes(sandbox.memory_total)}</dd>
					</div>
					<div>
						<dt class={label}>{$i18n.t('CPU')}</dt>
						<dd>{$i18n.t('{{count}} cores', { count: sandbox.cpus ?? 0 })}</dd>
					</div>
					<div>
						<dt class={label}>{$i18n.t('Workspace volumes')}</dt>
						<dd class="tabular-nums">
							{sandbox.sandbox_volumes ?? 0} · {formatBytes(sandbox.sandbox_volumes_size)}
						</dd>
					</div>
					<div>
						<dt class={label}>{$i18n.t('Images')}</dt>
						<dd class="tabular-nums">{formatBytes(sandbox.images_size)}</dd>
					</div>
				</dl>
			</div>
		{/if}
	</section>

	<section class="{card} p-5" aria-labelledby="system-data">
		<h3 id="system-data" class="font-medium">{$i18n.t('Data')}</h3>
		<dl class="mt-4 grid grid-cols-2 gap-3 text-sm">
			<div>
				<dt class={label}>{$i18n.t('Database')}</dt>
				<dd>{overview.database.dialect}</dd>
			</div>
			<div>
				<dt class={label}>{$i18n.t('Database size')}</dt>
				<dd class="tabular-nums">
					{overview.database.size === null ? '—' : formatBytes(overview.database.size)}
				</dd>
			</div>
			<div>
				<dt class={label}>{$i18n.t('File storage')}</dt>
				<dd>{overview.storage_provider}</dd>
			</div>
			<div>
				<dt class={label}>{$i18n.t('Host up since')}</dt>
				<dd>{dayjs(host.boot_at * 1000).fromNow()}</dd>
			</div>
		</dl>
		<slot />
	</section>
</div>
