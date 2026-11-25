// Shared look for the team admin tables and forms (new-api style: cards, gray table header, tags).
export const card =
	'rounded-2xl border border-gray-100 bg-white dark:border-gray-850 dark:bg-gray-900';
export const field =
	'block w-full min-w-0 rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm outline-hidden focus:border-gray-400 dark:border-gray-700 dark:focus:border-gray-500';
export const primary =
	'inline-flex shrink-0 items-center justify-center gap-1 rounded-lg bg-gray-900 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-black disabled:opacity-40 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white';
export const secondary =
	'inline-flex shrink-0 items-center justify-center gap-1 rounded-lg border border-gray-200 px-3 py-2 text-sm transition hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:hover:bg-gray-850';
export const link =
	'whitespace-nowrap text-sm text-blue-600 transition hover:text-blue-700 disabled:opacity-40 dark:text-blue-400 dark:hover:text-blue-300';
export const th = 'whitespace-nowrap px-4 py-3 text-left text-xs font-medium text-gray-500';
export const td = 'px-4 py-3 align-middle';
export const tableHead = 'bg-gray-50 dark:bg-gray-850/60';
export const row =
	'border-t border-gray-100 transition hover:bg-gray-50/60 dark:border-gray-850 dark:hover:bg-gray-850/30';

const tagTone = {
	blue: 'bg-blue-500/10 text-blue-700 dark:text-blue-300',
	green: 'bg-green-500/10 text-green-700 dark:text-green-300',
	amber: 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
	red: 'bg-red-500/10 text-red-700 dark:text-red-300',
	purple: 'bg-purple-500/10 text-purple-700 dark:text-purple-300',
	gray: 'bg-gray-500/10 text-gray-600 dark:text-gray-300'
};
export type Tone = keyof typeof tagTone;
export const tag = (tone: Tone = 'gray') =>
	`inline-flex shrink-0 items-center whitespace-nowrap rounded-md px-1.5 py-0.5 text-[0.6875rem] font-medium ${tagTone[tone]}`;

export const errorText = (e: unknown) => String(e instanceof Error ? e.message : e);
export const toTimestamp = (value: string) =>
	value ? Math.floor(new Date(value).getTime() / 1000) : null;
