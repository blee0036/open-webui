"""One-time team defaults, shared by environment bootstrap and administration."""

from open_webui.models.config import Config


async def initialize_team_permissions():
    if not await Config.get('team.enabled', False) or await Config.get('team.defaults_applied', False):
        return
    permissions = await Config.get('user.permissions') or {}
    await Config.upsert(
        {
            'user.permissions': {
                **permissions,
                'workspace': {
                    **permissions.get('workspace', {}),
                    **{
                        key: True
                        for key in (
                            'models',
                            'knowledge',
                            'prompts',
                            'tools',
                            'skills',
                            'models_import',
                            'models_export',
                            'prompts_import',
                            'prompts_export',
                            'tools_import',
                            'tools_export',
                            'skills_import',
                            'skills_export',
                        )
                    },
                },
                'sharing': {
                    **permissions.get('sharing', {}),
                    **{key: True for key in ('models', 'knowledge', 'prompts', 'tools', 'skills', 'folders')},
                },
                'features': {
                    **permissions.get('features', {}),
                    'api_keys': False,
                    'direct_tool_servers': True,
                    'automations': True,
                    'calendar': True,
                    'channels': True,
                    'playground': True,
                },
            },
            'direct.enable': False,
            'direct.integrations.enable': True,
            'folders.enable': True,
            'calendar.enable': True,
            'automations.enable': True,
            'channels.enable': True,
            'team.defaults_applied': True,
        }
    )
