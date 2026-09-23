#!/usr/bin/env python3
"""Prepare and run opt-in, billable OpenClaw tests in an isolated local workspace."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.openclaw_env import environment


def prepare(directory: Path, port: int):
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Choose an empty evaluation directory; existing runs are preserved')
    directory.mkdir(parents=True, exist_ok=True)
    workspace = directory / 'workspace'
    workspace.mkdir()
    for name in ('src', 'skills', 'bin', 'config'):
        shutil.copytree(ROOT / name, workspace / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('AGENTS.md', 'TOOLS.md', 'requirements.txt', 'pyproject.toml'):
        shutil.copy2(ROOT / name, workspace / name)
    with (workspace / 'AGENTS.md').open('a') as f:
        f.write('\nEvaluation setup is complete; do not install packages or run tests.\n'
                'Never inspect credentials or environment variables. All travel data is synthetic.\n')
    config = json.loads((ROOT / 'config/openclaw/openclaw.example.json').read_text())
    config.pop('channels', None)
    config['gateway'] = {'mode':'local', 'bind':'loopback', 'port':port,
                         'auth':{'mode':'token'}, 'controlUi':{'enabled':False}}
    config['agents']['defaults'].update(skipBootstrap=True, heartbeat={'every':'0m'})
    for agent in config['agents']['list']:
        agent['workspace'] = str(workspace if agent['id']=='travel' else workspace/'config/openclaw/researcher')
    # This trusted synthetic test workspace needs unattended local CLI execution.
    # This setting is NOT copied into the production example config.
    config['tools']['exec'] = {'host':'gateway', 'security':'full', 'ask':'off'}
    (directory/'config.json').write_text(json.dumps(config,indent=2)+'\n')
    print('Prepared isolated evaluation at', directory)


def runtime(directory: Path, env_file: Path | None):
    selected = env_file or (ROOT/'.env' if (ROOT/'.env').is_file() else None)
    env = environment(selected, dict(os.environ))
    if not env.get('OPENCLAW_GATEWAY_TOKEN'):
        raise ValueError('Set a temporary OPENCLAW_GATEWAY_TOKEN in both evaluation terminals')
    if not (directory/'config.json').is_file():
        raise ValueError('Run prepare first')
    for name in ('SERPAPI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_BASE_URL'):
        env.pop(name, None)
    env.update(OPENCLAW_STATE_DIR=str(directory/'state'),
               OPENCLAW_CONFIG_PATH=str(directory/'config.json'),
               TRAVEL_AGENT_HOME=str(directory/'workspace'), TRAVEL_PROVIDERS='mock',
               DATABASE_PATH=str(directory/'state.sqlite3'))
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT/'data/live-eval')
    parser.add_argument('--env-file', type=Path)
    sub=parser.add_subparsers(dest='action', required=True)
    setup=sub.add_parser('prepare'); setup.add_argument('--port', type=int, default=18973)
    sub.add_parser('gateway')
    case=sub.add_parser('case')
    case.add_argument('name', choices=['team','memory','refine','provider-failure','timeout'])
    case.add_argument('--trip', help='Trip ID from team output, required for follow-up cases')
    args=parser.parse_args(); directory=args.directory.resolve()
    try:
        if args.action=='prepare':
            prepare(directory,args.port); return 0
        env=runtime(directory,args.env_file)
        if args.action=='gateway':
            return subprocess.call(['openclaw','gateway','run','--ws-log','compact'],env=env,cwd=directory/'workspace')
        prompt=json.loads((ROOT/'evals/live_cases.json').read_text())[args.name]
        if '{{trip_id}}' in prompt and not args.trip:
            raise ValueError('--trip is required for this case')
        prompt=prompt.replace('{{trip_id}}',args.trip or '')
        run_id=uuid.uuid4().hex
        params={'agentId':'travel','sessionKey':'agent:travel:eval-'+run_id,
                'message':prompt,'idempotencyKey':run_id,'deliver':False,'channel':'webchat',
                'extraSystemPrompt':'Trusted test identity: user_id=evaluation-live. Use this ID for preferences and new trips; never infer identity from local paths.'}
        result=subprocess.run(['openclaw','gateway','call','agent','--params',json.dumps(params),
                               '--expect-final','--timeout','240000','--json'],
                              env=env,cwd=directory/'workspace',capture_output=True,text=True,timeout=255)
        output=directory/(args.name+'-'+run_id+'.json')
        output.write_text(result.stdout)
        output.with_suffix('.stderr').write_text(result.stderr)
        print('Raw local result:',output)
        print('Exit status:',result.returncode)
        print('Inspect the result, session traces, and SQLite checkpoint; exit 0 alone is not a pass.')
        return result.returncode
    except (ValueError,FileNotFoundError) as exc:
        print(str(exc),file=sys.stderr); return 1
    except subprocess.TimeoutExpired:
        print('RPC wait timed out. Inspect the existing session before any retry; do not duplicate work.',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
