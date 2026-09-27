#!/usr/bin/env python3
"""Extract minimal behavioral evidence from synthetic OpenClaw eval traces.

Omits prompts, raw page bodies, reasoning, environment, command prose and final
response text. Never use this as a general redactor for production transcripts.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def payload(text):
    try:
        return json.loads(text)
    except ValueError:
        try:
            return json.loads(text[text.index('{'):])
        except (ValueError, TypeError):
            return {}


def safe_identity(value):
    return value if isinstance(value,str) and value.startswith("evaluation") else "[non-evaluation identity]"


def messages(path):
    if not path.is_file():
        return []
    return [json.loads(line).get('message', {}) for line in path.read_text().splitlines()]


def summarize(directory, files):
    registry=json.loads((directory/'state/subagents/runs.json').read_text()).get('runs',{})
    cases=[]
    for file in files:
        raw=payload(file.read_text()); result=raw.get('result',raw)
        meta=result.get('meta',{}).get('agentMeta',{})
        if not meta.get('sessionId'):
            continue
        ms=messages(directory/'state/agents/travel/sessions'/f"{meta['sessionId']}.jsonl")
        record={'case':file.stem, 'model':meta.get('model'), 'usage':meta.get('usage'),
                'session_id':meta['sessionId'], 'calls':[], 'checkpoints':[], 'children':[],
                'tool_errors':0}
        displays=[]; finals=[]; trip_ids=[]; results=[]
        for m in ms:
            for c in m.get('content',[]):
                if c.get('type')=='toolCall':
                    args=c.get('arguments',{}); call={'tool':c['name']}
                    if c['name']=='exec':
                        cmd=args.get('command',''); match=re.search(r'src\.cli\s+([a-z-]+)',cmd)
                        call['operation']=match.group(1) if match else 'other'
                        if call['operation']=='new-trip':
                            call['explicit_origin_in_command']='"origin"' in cmd or '\\"origin\\"' in cmd
                    elif c['name']=='sessions_spawn':
                        call.update(agent_id=args.get('agentId'),timeout_seconds=args.get('runTimeoutSeconds'))
                    elif c['name']=='web_fetch':
                        call['url']=args.get('url')
                    record['calls'].append(call)
                if m.get('role')=='toolResult' and c.get('type')=='text':
                    d=payload(c.get('text','')); results.append(d)
                    if m.get('isError') or d.get('status') in ('error','forbidden') or d.get('ok') is False:
                        record['tool_errors']+=1
                    if d.get('trip_id'):
                        trip_ids.append(d['trip_id'])
                    if d.get('display_text'):
                        displays.append(d['display_text'])
                    if d.get('checkpoint'):
                        cp=d['checkpoint']
                        record['checkpoints'].append({'phase':cp['phase'],'reviewed':cp['reviewed'],
                            'research_count':len(cp['research']), 'unresolved_count':len(cp['unresolved']),
                            'source_urls':sorted({item['source_url'] for item in cp['research']})})
                    if m.get('toolName')=='sessions_spawn' and d.get('runId'):
                        run=registry.get(d['runId'],{})
                        child={'run_id':d['runId'],'session_key':d.get('childSessionKey'),
                               'outcome':run.get('outcome'),'duration_ms':run.get('endedAt',0)-run.get('startedAt',0)}
                        child_agent=d['childSessionKey'].split(':')[1]
                        child_store=directory/'state/agents'/child_agent/'sessions/sessions.json'
                        if child_store.exists():
                            mapping=json.loads(child_store.read_text()); entry=mapping.get(d['childSessionKey'],{})
                            child_ms=messages(child_store.parent/(entry.get('sessionId','missing')+'.jsonl'))
                            child['tools']=[x['name'] for cm in child_ms for x in cm.get('content',[]) if x.get('type')=='toolCall']
                        record['children'].append(child)
                if m.get('role')=='assistant' and c.get('type')=='text':
                    finals.append(c.get('text',''))
        final='\n'.join(finals)
        record['flight_display_verbatim']=bool(displays) and any(d in final for d in displays)
        record['final_text_sha256']=hashlib.sha256(final.encode()).hexdigest()
        record['trip_ids']=sorted(set(trip_ids))
        record['search_states']=[d['state'] for d in results if 'provider_errors' in d and 'state' in d]
        record['preferences_read']=[{key:(safe_identity(d['preferences'].get(key)) if key=='user_id' else d['preferences'].get(key)) for key in ('user_id','home_airport','dietary_preferences','hotel_max_nightly')}
                                    for d in results if 'preferences' in d]
        cases.append(record)
    conn=sqlite3.connect(directory/'state.sqlite3')
    checkpoints=[{'trip_id':tid,'phase':(cp:=json.loads(data))['phase'],
                  'research_count':len(cp['research']),'unresolved_count':len(cp['unresolved'])}
                 for tid,data in conn.execute('SELECT trip_id,data FROM planning_checkpoints')]
    trips=[{'trip_id':tid,'user_id':safe_identity(user),'origin':(trip:=json.loads(data))['request']['origin'],
            'destination':trip['request']['destination'],'state':trip['state']}
           for tid,user,data in conn.execute('SELECT id,user_id,data FROM trips')]
    conn.close()
    return {'kind':'Observed live OpenClaw tool/session evidence; synthetic user and mock flights',
            'openclaw_version':'2026.2.14', 'cases':cases,'final_checkpoints':checkpoints,'trips':trips}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--files',nargs='+',help='Result JSON basenames; defaults to all *.json')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    files=[args.directory/f for f in args.files] if args.files else sorted(args.directory.glob('*.json'))
    report=summarize(args.directory,files)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print('Summarized',len(report['cases']),'cases without raw prompts/page bodies/reasoning')

if __name__=='__main__': main()
