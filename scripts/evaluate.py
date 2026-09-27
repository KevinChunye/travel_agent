#!/usr/bin/env python3
"""Five real CLI scenarios on baseline and revised code; no model/network calls."""
import argparse, io, json, os, subprocess, sys, tarfile, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
BASELINE = (ROOT / 'docs/evidence/baseline-commit.txt').read_text().strip()

def evaluate(root, temp):
    env = dict(os.environ, TRAVEL_PROVIDERS='mock', DATABASE_PATH=str(temp / 'state.sqlite3'), TRAVEL_AGENT_HOME=str(temp))
    env.pop('SERPAPI_API_KEY', None)
    trace = []
    def call(*args, providers='mock'):
        p = subprocess.run([sys.executable, '-m', 'src.cli', *args], cwd=root,
                           env=dict(env, TRAVEL_PROVIDERS=providers), capture_output=True, text=True)
        try: data = json.loads(p.stdout)
        except json.JSONDecodeError: data = {'error':'command unavailable or invalid', 'exit_code':p.returncode}
        trace.append({'command':list(args), 'exit_code':p.returncode, 'result':data})
        return data
    call('prefs-set', '--user', 'evaluation-demo', '--json', '{"home_airport":"BOS","dietary_preferences":["vegetarian"]}')
    first = call('new-trip', '--user', 'evaluation-demo', '--request-json', '{"destination":"JFK","outbound_date":{"start":"2027-10-09"}}')
    prefs = call('prefs-get', '--user', 'evaluation-demo')
    memory = first.get('missing_fields') == [] and prefs.get('preferences', {}).get('dietary_preferences') == ['vegetarian']
    trip = call('new-trip', '--user', 'evaluation-demo', '--request-json', '{"origin":"BOS","destination":"JFK","outbound_date":{"start":"2027-10-09"}}')['trip_id']
    call('checkpoint', '--trip', trip, '--json', '{"phase":"researching","next_action":"Await synthetic child"}')
    resumed = call('checkpoint', '--trip', trip)
    persistence = (resumed.get('checkpoint') or {}).get('phase') == 'researching'
    rejected = call('checkpoint', '--trip', trip, '--json', '{"phase":"ready","next_action":"Deliver","reviewed":false}')
    call('checkpoint', '--trip', trip, '--json', '{"phase":"blocked","next_action":"Request source review","unresolved":["Child omitted citations"]}')
    recovered = call('checkpoint', '--trip', trip)
    validation = rejected.get('ok') is False and (recovered.get('checkpoint') or {}).get('phase') == 'blocked'
    failed = call('search', '--trip', trip, providers='google_flights')
    failure = failed.get('state') == 'SEARCH_FAILED' and failed.get('total_offers') == 0
    searched = call('search', '--trip', trip)
    refined = call('refine', '--trip', trip, '--command', 'cheaper', providers='google_flights')
    refinement = searched.get('total_offers', 0) > 0 and refined.get('state') == 'OPTIONS_READY'
    return {'checks':dict(memory_changes_behavior=memory, checkpoint_restart=persistence,
                         reject_and_recover_child_output=validation, provider_failure_detected=failure,
                         refinement_without_search=refinement), 'trace':trace}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/evidence/comparison.json')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='travel-evaluation-') as tmp:
        temp = Path(tmp); baseline = temp / 'baseline'; baseline.mkdir()
        archive = subprocess.check_output(['git', 'archive', BASELINE], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(baseline, filter='data')
        (temp / 'before').mkdir(); (temp / 'after').mkdir()
        report = {'kind':'Deterministic CLI evaluation; NOT an LLM/delegation trace', 'baseline_commit':BASELINE,
                  'baseline':evaluate(baseline, temp/'before'), 'improved':evaluate(ROOT, temp/'after')}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({k:report[k]['checks'] for k in ('baseline','improved')}, indent=2))
    if not all(report['improved']['checks'].values()): raise SystemExit(1)
if __name__ == '__main__': main()
