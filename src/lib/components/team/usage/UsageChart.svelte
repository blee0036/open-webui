<script lang="ts">
	import { onDestroy, onMount } from 'svelte';

	/** Chart.js type: 'bar' | 'line' | 'doughnut'. */
	export let type: 'bar' | 'line' | 'doughnut' = 'bar';
	export let labels: string[] = [];
	export let datasets: any[] = [];
	export let stacked = false;
	export let horizontal = false;
	export let format: (value: number) => string = (value) => String(value);
	export let height = 280;
	export let label = '';

	let canvas: HTMLCanvasElement;
	let chart: any = null;
	let Chart: any = null;
	let dark = false;
	let observer: MutationObserver | null = null;

	const textColor = () => (dark ? '#d1d5db' : '#4b5563');
	const gridColor = () => (dark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)');

	function options() {
		const round = type === 'doughnut';
		const axis = {
			stacked,
			ticks: { color: textColor() },
			grid: { color: gridColor() },
			border: { display: false }
		};
		const valueAxis = { ...axis, beginAtZero: true, ticks: { ...axis.ticks, callback: format } };
		return {
			responsive: true,
			maintainAspectRatio: false,
			animation: { duration: 250 },
			indexAxis: horizontal ? 'y' : 'x',
			interaction: { mode: round ? 'nearest' : 'index', intersect: round },
			plugins: {
				legend: {
					display: round || datasets.length > 1,
					position: round ? 'right' : 'bottom',
					labels: { color: textColor(), boxWidth: 10, boxHeight: 10, usePointStyle: true }
				},
				tooltip: {
					callbacks: {
						label: (context: any) => {
							const value = round
								? context.parsed
								: horizontal
									? context.parsed.x
									: context.parsed.y;
							return `${context.dataset.label || context.label}: ${format(value)}`;
						}
					}
				}
			},
			scales: round
				? {}
				: horizontal
					? { x: valueAxis, y: { ...axis, grid: { display: false } } }
					: { x: { ...axis, grid: { display: false } }, y: valueAxis }
		};
	}

	async function draw() {
		if (!canvas) return;
		if (!Chart) Chart = (await import('chart.js/auto')).default;
		chart?.destroy();
		chart = new Chart(canvas, { type, data: { labels, datasets }, options: options() });
	}

	onMount(() => {
		dark = document.documentElement.classList.contains('dark');
		observer = new MutationObserver(() => {
			const next = document.documentElement.classList.contains('dark');
			if (next !== dark) {
				dark = next;
				void draw();
			}
		});
		observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
	});
	onDestroy(() => {
		observer?.disconnect();
		chart?.destroy();
	});

	// Redraw whenever the data or layout changes.
	$: (labels, datasets, type, stacked, horizontal, canvas && void draw());
</script>

<div style="height: {height}px" class="relative w-full">
	<canvas bind:this={canvas} aria-label={label}></canvas>
</div>
