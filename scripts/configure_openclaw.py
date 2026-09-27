#!/usr/bin/env python3
"""Build a private, validated proposal for a dedicated travel OpenClaw gateway.

Never activates the proposal, changes the input, or prints credentials.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def proposal(existing: dict, template: dict, workspace: str, route_whatsapp=False, make_default=False):
    result = deepcopy(existing)
    agents = result.setdefault('agents', {})
    if 'entries' in agents:
        raise ValueError('This installer targets the tested agents.list schema; migrate/validate agents.entries separately')
    entries = agents.setdefault('list', [])
    if not isinstance(entries,list):
        raise ValueError('agents.list must be an array')
    defaults = template['agents']['defaults']
    for wanted in deepcopy(template['agents']['list']):
        ident=wanted['id']
        prior=next((a for a in entries if a.get('id')==ident), None)
        wanted['workspace']=workspace if ident=='travel' else str(Path(workspace)/'config/openclaw/researcher')
        wanted['model']=deepcopy(defaults['model']) if ident=='travel' else defaults['subagents']['model']
        if ident=='travel':
            wanted['subagents']['model']=defaults['subagents']['model']
            wanted['default']=make_default or bool(prior and prior.get('default'))
        if prior is None:
            entries.append(wanted)
        else:
            # Retain independent runtime/sandbox settings, but install the reviewed
            # travel tool policy on these two explicitly managed agents.
            prior.update(wanted)
    if make_default:
        for entry in entries:
            entry['default']=entry.get('id')=='travel'
    # These shared limits intentionally affect every subagent on this dedicated gateway.
    shared=agents.setdefault('defaults', {})
    shared['thinkingDefault']=defaults['thinkingDefault']
    shared.setdefault('subagents', {}).update(maxConcurrent=1,archiveAfterMinutes=60)
    tools=result.setdefault('tools', {})
    cross=tools.setdefault('agentToAgent', {})
    cross['enabled']=True
    cross['allow']=list(dict.fromkeys([*cross.get('allow', []),'travel','travel-researcher']))
    tools['subagents']=deepcopy(template['tools']['subagents'])
    result.setdefault('session', {})['dmScope']='per-channel-peer'
    if route_whatsapp:
        bindings=result.setdefault('bindings', [])
        # Preserve account- and peer-specific routes; replace only the generic fallback.
        bindings[:]=[b for b in bindings if b.get('match')!={'channel':'whatsapp'}]
        bindings.append({'agentId':'travel','match':{'channel':'whatsapp'}})
        channels=result.setdefault('channels', {})
        channels.setdefault('whatsapp',deepcopy(template['channels']['whatsapp']))
    return result


def write_proposal(value, output: Path, validate=True):
    if output.exists():
        raise ValueError('Output already exists; choose a new proposal path')
    output.parent.mkdir(parents=True,exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix='.travel-config-',suffix='.json',dir=output.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,indent=2);f.write('\n')
        if validate:
            env=dict(os.environ,OPENCLAW_CONFIG_PATH=staged)
            run=subprocess.run(['openclaw','config','get','agents.list'],env=env,
                               stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True,timeout=30)
            if run.returncode:
                # Validation output can contain config values. Do not echo it.
                raise ValueError('OpenClaw rejected the proposal; inspect configuration privately on this host')
        # link fails rather than overwriting if another process creates the destination.
        os.link(staged,output)
    finally:
        Path(staged).unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True,help='Existing strict-JSON config')
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--route-whatsapp',action='store_true')
    parser.add_argument('--make-default',action='store_true')
    args=parser.parse_args()
    try:
        if args.config.resolve()==args.output.resolve():
            raise ValueError('Input and output must differ; this command only prepares a proposal')
        base=json.loads(args.config.read_text())
        template=json.loads((ROOT/'config/openclaw/openclaw.example.json').read_text())
        merged=proposal(base,template,str(args.workspace.resolve()),args.route_whatsapp,args.make_default)
        write_proposal(merged,args.output)
    except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
        # Never echo parser excerpts that may contain secrets.
        print(json.dumps({'ok':False,'error':type(exc).__name__,
                          'message':'Proposal not activated. Check paths, strict JSON, and the installed OpenClaw schema.'}))
        return 1
    print(json.dumps({'ok':True,'proposal':str(args.output),'activated':False,
                      'shared_changes':['subagent web-only tools','subagent concurrency 1','low reasoning','per-channel-peer DM sessions']}))
    return 0

if __name__=='__main__': raise SystemExit(main())
