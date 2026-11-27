import { saveTeam, teamRequest } from '$lib/apis/team';

type Disk = { path?: string; total: number; used: number; free: number };

export type SandboxHost = {
	available: boolean;
	configured: boolean;
	error?: string;
	docker_version?: string;
	cpus?: number;
	memory_total?: number;
	docker_root?: string;
	images_size?: number;
	sandbox_volumes?: number;
	sandbox_volumes_size?: number;
	disk?: Disk;
	running?: number;
	queued?: number;
	limit?: number;
};

export type SystemOverview = {
	host: {
		hostname?: string;
		cpus: number;
		cpu_percent: number;
		load: number[] | null;
		memory: { total: number; available: number; used: number };
		swap: { total: number; used: number };
		disks: Disk[];
		process: { rss: number; cpu_percent: number; started_at: number; threads: number };
		boot_at: number;
	};
	database: { dialect: string; size: number | null };
	storage_provider: string;
	sandbox: SandboxHost;
	generated_at: number;
};

export type StorageRow = {
	id: string;
	name: string;
	email: string;
	role: string;
	plan_name: string | null;
	files: { used: number; file_count: number; unknown_size: number; limit: number | null };
	sandbox: { used: number | null; over: boolean; limit: number | null };
};

export type StorageReport = {
	users: StorageRow[];
	enforced: boolean;
	total_files_bytes: number;
	sandbox_available: boolean;
	sandbox_scanned_at: number | null;
};

export type SandboxWorkspace = {
	id: string;
	user_id: string;
	user_name: string | null;
	project_id: string;
	project_name: string | null;
	status: string;
	public: boolean;
	error: string | null;
	manual: boolean;
	agent_status: 'running' | 'paused' | 'queued' | 'idle';
	agent_queue: number;
	queue_position: number | null;
	disk: number | null;
	memory: { used: number; limit: number | null } | null;
	idle_seconds: number | null;
};

export type SandboxReport = {
	available: boolean;
	configured: boolean;
	error?: string;
	workspaces?: SandboxWorkspace[];
	users?: Record<string, { used: number; limit: number | null; over: boolean }>;
	scanned_at?: number | null;
	scan_interval?: number;
	running?: number;
	queued?: number;
	limit?: number;
	memory_per_workspace?: number;
};

export const getSystemOverview = () => teamRequest<SystemOverview>('/system/overview');
export const getStorageReport = () => teamRequest<StorageReport>('/system/storage/users');
export const getSandboxReport = () => teamRequest<SandboxReport>('/system/sandboxes');
export const controlSandbox = (id: string, action: 'stop' | 'recreate' | 'clear') =>
	saveTeam(
		`/system/sandboxes/${encodeURIComponent(id)}`,
		{ action, confirm: action === 'clear' ? 'CLEAR MY WORKSPACE' : null },
		'POST'
	);
