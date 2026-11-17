<script lang="ts">
	// Short enterprise sign-in link (/connect/<connection ID>), like a cloud console's
	// account-specific sign-in URL: remember the enterprise on this browser, then open
	// the sign-in page with it filled in. Nothing is listed or validated here.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { ENTERPRISE_LOGIN_KEY } from '$lib/apis/team';

	onMount(() => {
		const id = ($page.params.id ?? '').trim().slice(0, 80);
		try {
			if (id) localStorage.setItem(ENTERPRISE_LOGIN_KEY, id);
		} catch {}
		goto(id ? `/auth?connect=${encodeURIComponent(id)}` : '/auth', { replaceState: true });
	});
</script>
