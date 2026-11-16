export type ConversationVisibility = 'private' | 'project';
export type ToolRuntime = 'native' | 'sandbox';
export type QuotaState = 'reserved' | 'pending' | 'settled' | 'released';
export type Project = {
	id: string;
	name: string;
	kind: 'project';
	user_id: string;
	created_at: number;
	updated_at: number;
	data: {
		system_prompt?: string;
		model_ids?: string[];
		context_revision?: number;
		context_updated_by?: string;
		context_updated_at?: number;
		terminal_id?: string;
		working_directory?: string;
		files?: { id: string; type: string; name: string; published_by?: string }[];
	};
	write_access?: boolean;
	access_grants?: {
		principal_type: 'user' | 'group';
		principal_id: string;
		permission: 'read' | 'write';
	}[];
};
