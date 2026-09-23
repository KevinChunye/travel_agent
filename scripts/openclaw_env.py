#!/usr/bin/env python3
"""Run OpenClaw with local .env credentials or hosted environment variables."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    'OPENAI_API_KEY', 'SERPAPI_API_KEY', 'BRAVE_API_KEY',
    'TRAVEL_PROVIDERS', 'DATABASE_PATH', 'TRAVEL_AGENT_HOME',
    'SERPAPI_MONTHLY_LIMIT', 'SERPAPI_RESERVE', 'SEARCH_CACHE_TTL_MINUTES',
    'DASHBOARD_PORT',
}


def environment(path: Path | None, inherited: dict[str, str]) -> dict[str, str]:
    env = inherited.copy()
    if path is not None:
        if not path.is_file():
            raise ValueError(f'Environment file does not exist: {path}')
        # Parse data only: never execute a shell or interpolate secret values.
        # An explicitly chosen local file wins over stale inherited credentials.
        for name, value in dotenv_values(path, interpolate=False).items():
            if name in ALLOWED and value:
                env[name] = value
    if not env.get('OPENAI_API_KEY'):
        raise ValueError('OPENAI_API_KEY is missing; set it in .env or the deployment environment')
    return env


def check_api(env: dict[str, str], model: str) -> tuple[dict, int]:
    try:
        response = httpx.post(
            'https://api.openai.com/v1/responses',
            headers={'Authorization': 'Bearer ' + env['OPENAI_API_KEY']},
            json={'model': model.removeprefix('openai/'), 'input': 'Reply exactly: travel-agent API connection verified.',
                  'max_output_tokens': 40, 'store': False},
            timeout=30,
        )
    except httpx.RequestError:
        return {'ok': False, 'error': 'connection_error'}, 1
    try:
        data = response.json()
    except ValueError:
        return {'ok': False, 'http_status': response.status_code, 'error': 'invalid_response'}, 1
    result = {'ok': response.is_success, 'http_status': response.status_code}
    if response.is_success:
        result.update(model=data.get('model'), usage=data.get('usage'))
    else:
        # Raw error messages can contain credential fragments. Never echo them.
        result['error'] = data.get('error', {}).get('code') or 'api_error'
    return result, 0 if response.is_success else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, help='Local dotenv file; defaults to repo .env when present')
    parser.add_argument('--check', action='store_true', help='Make one small billable OpenAI API request')
    parser.add_argument('--model', default='gpt-4.1-mini', help='Model for --check only')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='OpenClaw arguments after --')
    args = parser.parse_args(argv)
    path = args.env_file or (ROOT / '.env' if (ROOT / '.env').is_file() else None)
    try:
        env = environment(path, dict(os.environ))
    except ValueError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1
    if args.check:
        result, code = check_api(env, args.model)
        print(json.dumps(result, indent=2))
        return code
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('provide --check or OpenClaw arguments after --')
    try:
        return subprocess.call(['openclaw', *command], env=env, cwd=ROOT)
    except FileNotFoundError:
        print(json.dumps({'ok': False, 'error': 'openclaw executable not found'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
