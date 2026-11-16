"""Runs only INSIDE the unprivileged runtime. Never imported by WebUI/controller."""

import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, get_type_hints

from pydantic import create_model


async def run(data):
    source = data['content']
    requirements = re.search(r'^requirements:\s*(.+)$', source, re.MULTILINE)
    if requirements:
        packages = shlex.split(requirements.group(1).replace(',', ' '))
        if any(p.startswith('-') for p in packages):
            raise ValueError('Requirements must be package specifiers, not pip options')
        subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--user', '--disable-pip-version-check', *packages],
            check=True,
            timeout=90,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    directory = pathlib.Path.home() / '.team-tools'
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', dir=directory, delete=False) as file:
        file.write(source)
        path = file.name
    try:
        spec = importlib.util.spec_from_file_location('personal_tool', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tool = module.Tools()
        if data['action'] == 'discover':
            functions = []
            for name in dir(tool):
                fn = getattr(tool, name)
                if name.startswith('_') or not callable(fn) or inspect.isclass(fn):
                    continue
                hints = get_type_hints(fn)
                fields = {
                    key: (hints.get(key, Any), ... if param.default is inspect.Parameter.empty else param.default)
                    for key, param in inspect.signature(fn).parameters.items()
                    if not key.startswith('__')
                }
                schema = create_model(name, **fields).model_json_schema()
                functions.append({'name': name, 'description': inspect.getdoc(fn) or name, 'parameters': schema})
            return {
                'specs': functions,
                'valves_schema': tool.Valves.model_json_schema() if hasattr(tool, 'Valves') else None,
                'user_valves_schema': tool.UserValves.model_json_schema() if hasattr(tool, 'UserValves') else None,
            }
        if hasattr(tool, 'Valves'):
            tool.valves = tool.Valves(**data.get('valves', {}))
        user = {'id': data['user_id']}
        if hasattr(tool, 'UserValves'):
            user['valves'] = tool.UserValves(**data.get('user_valves', {}))
        name = data['function']
        if name.startswith('_') or not callable(getattr(tool, name, None)):
            raise ValueError('Unknown tool function')
        fn = getattr(tool, name)
        params = {k: v for k, v in data.get('params', {}).items() if not k.startswith('__')}
        for key, value in {'__user__': user, '__id__': data.get('tool_id')}.items():
            if key in inspect.signature(fn).parameters:
                params[key] = value
        result = fn(**params)
        return await result if inspect.isawaitable(result) else result
    finally:
        os.unlink(path)


if __name__ == '__main__':
    # User prints cannot corrupt the result protocol. Source is untrusted; the container is the boundary.
    data = json.load(sys.stdin)
    logs = io.StringIO()
    try:
        with contextlib.redirect_stdout(logs):
            result = asyncio.run(run(data))
        print(json.dumps({'result': result}, default=str))
    except BaseException as error:
        print(json.dumps({'error': str(error)[:4000]}))
        sys.exit(1)
