"""Public behavioral checks. Adapter is a JSON file containing a command array.
Example: python3 check_platform.py --adapter adapter.json --case case.json --out evidence.json
The adapter must accept --case, --db, --request and --response. HTTP implementations may use a thin adapter.
"""
import argparse
from collections import Counter
import copy
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from check_result import evaluate


def check(adapter, case_path):
    config = json.loads(adapter.read_text(encoding='utf-8'))
    command = config.get('command') if isinstance(config, dict) else None
    if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
        raise ValueError('adapter.command must be a nonempty string array')
    if command[0] == 'python3':
        command[0] = sys.executable
    case = json.loads(case_path.read_text(encoding='utf-8'))
    unique = list({j['job_id']: j for j in case['jobs']}.values())
    group = next([j for j in unique if j['station_id'] == s] for s in case['stations']
                 if sum(j['station_id'] == s for j in unique) >= 3)
    first, second, third = group[:3]
    checks, transcript = [], []
    with tempfile.TemporaryDirectory(prefix='platform-contract-') as folder:
        tmp = Path(folder)
        db = tmp/'shared.db'
        counter = 0

        def launch(request):
            nonlocal counter
            counter += 1
            req, out = tmp/f'{counter}.request.json', tmp/f'{counter}.response.json'
            req.write_text(json.dumps(request), encoding='utf-8')
            argv = command + ['--case', str(case_path), '--db', str(db), '--request', str(req), '--response', str(out)]
            proc = subprocess.Popen(argv, cwd=adapter.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return proc, out, request, time.monotonic()

        def finish(item):
            proc, out, request, started = item
            try:
                _, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate(); raise AssertionError('adapter timed out')
            assert proc.returncode == 0, f'adapter exit {proc.returncode}: {stderr[-1000:]}'
            result = json.loads(out.read_text(encoding='utf-8'))
            assert isinstance(result, dict), 'response must be a JSON object'
            transcript.append({'request': request, 'response': result, 'duration_s': round(time.monotonic()-started, 4)})
            return result

        def call(request):
            return finish(launch(request))

        def pair(a, b):
            # Both child processes are launched before either result is collected.
            pa = launch(a); pb = launch(b)
            return finish(pa), finish(pb)

        def same_run(a, b):
            return all(a[k] == b[k] for k in ('job_id','station_id','ready_s','start_s','end_s'))

        assert call({'op':'get','job_id':first['job_id']})['status'] == 'not_found'
        checks.append('unsubmitted task query is not_found')
        a, b = pair({'op':'submit','job':first}, {'op':'submit','job':first})
        assert sorted([a['status'],b['status']]) == ['accepted','duplicate']
        assert same_run(a['run'],b['run'])
        duplicate = call({'op':'submit','job':first})
        assert duplicate['status'] == 'duplicate' and same_run(a['run'],duplicate['run'])
        checks.append('two simultaneous submissions produce one plan; later retry is stable')
        changed = dict(first,ready_s=first['ready_s']+1)
        conflict = call({'op':'submit','job':changed})
        assert conflict['status'] == 'conflict'
        a, b = pair({'op':'submit','job':second}, {'op':'submit','job':third})
        assert a['status'] == b['status'] == 'accepted'
        checks.append('simultaneous distinct tasks preserve the station constraint')
        for bad in ([], {'op':'bad'}, {'op':'submit','job':dict(first,ready_s=-1)}):
            error = call(bad)
            assert error['status'] == 'error' and error.get('code') and error.get('message')
        checks.append('malformed requests return structured errors')
        report = call({'op':'report','at_s':0})
        assert report['status'] == 'ok' and report['record_kind'] == 'virtual_plan_and_replay'
        subset = copy.deepcopy(case); subset['jobs'] = [first,second,third]
        validation = evaluate(subset, report)
        assert validation['valid'], validation['errors']
        expected_jobs = {j['job_id']:j for j in subset['jobs']}
        runs = {r['job_id']:r for r in report['runs']}
        for jid, row in runs.items():
            assert all(row[k] == expected_jobs[jid][k] for k in expected_jobs[jid]), 'export changed job payload'
        events = report['events']
        assert isinstance(events,list), 'events must be an array'
        for event in events:
            assert isinstance(event,dict) and type(event.get('seq')) is int and event['seq'] > 0, 'invalid event sequence'
            assert event.get('job_id') in expected_jobs, 'event belongs to an unknown task'
            assert isinstance(event.get('event'),str) and isinstance(event.get('detail'),dict), 'invalid event payload'
        assert len({e['seq'] for e in events}) == len(events)
        assert [e['seq'] for e in events] == sorted(e['seq'] for e in events)
        per_job = Counter((e['job_id'],e['event']) for e in events)
        for jid in runs:
            for kind, wanted in {'accepted':1,'planned':1,'duplicate':2 if jid==first['job_id'] else 0,
                                 'conflict':1 if jid==first['job_id'] else 0}.items():
                assert per_job[jid,kind] == wanted, f'{jid}: wrong {kind} event attribution'

        def records(value):
            # Only persistent contract fields; request metadata and export order may vary.
            rows = sorted((tuple(r[k] for k in ('job_id','station_id','ready_s','start_s','end_s')) for r in value['runs']))
            ev = [{k:e[k] for k in ('seq','job_id','event','detail')} for e in value['events']]
            return rows, ev

        def metrics(value, at):
            assert value['status']=='ok' and value['record_kind']=='virtual_plan_and_replay'
            m=value['metrics']
            assert type(m['at_s']) is int and m['at_s']==at, 'metrics.at_s must match the requested instant'
            assert type(m['planned_count']) is int and m['planned_count']==len(runs), 'wrong plan count'
            assert type(m['simulated_completed_count']) is int and m['simulated_completed_count']==sum(r['end_s']<=at for r in runs.values()), 'wrong completed count at query instant'
            assert m['event_counts']==dict(Counter(e['event'] for e in events)), 'event counts disagree with stored events'

        metrics(report,0)
        instants={0}
        for jid,row in runs.items():
            points={0,row['ready_s'],math.floor(row['start_s']),math.ceil(row['start_s']),
                    max(0,math.ceil(row['end_s'])-1),math.ceil(row['end_s'])}
            instants.update(points)
            for at in sorted(points):
                response=call({'op':'get','job_id':jid,'at_s':at})
                state=('not_ready' if at<row['ready_s'] else 'waiting' if at<row['start_s']
                       else 'running' if at<row['end_s'] else 'simulated_completed')
                assert response['status']=='ok' and response['state']==state, 'wrong task state at query instant'
                assert same_run(response['run'],row), 'query returned a different task or plan'
                assert response['wait']['wait_s']==row['start_s']-row['ready_s'], 'incorrect wait duration'
                expected=[e for e in events if e['job_id']==jid]
                assert records({'runs':[],'events':response['events']})[1]==records({'runs':[],'events':expected})[1], 'query events do not match this task'
        for at in sorted(instants):
            current=call({'op':'report','at_s':at})
            metrics(current,at)
            assert records(current)==records(report), 'queries must not mutate events or plans'
        # The plans promised by submit must also match later exports.
        for item in transcript:
            request=item['request'];response=item['response']
            if isinstance(request,dict) and request.get('op')=='submit' and response['status'] in ('accepted','duplicate'):
                assert same_run(response['run'],runs[request['job']['job_id']]), 'submit and export disagree'
        checks.append('task payloads, per-task events, progress boundaries and stable persisted facts agree')
    return {'passed':True,'checks':checks,'limits':'Behavioral samples; not proof of all races, authentication, or physical execution.', 'transcript':transcript}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adapter', required=True); p.add_argument('--case', required=True); p.add_argument('--out', required=True)
    a = p.parse_args()
    try:
        result = check(Path(a.adapter).resolve(),Path(a.case).resolve())
    except Exception as exc:
        result = {'passed':False,'error':f'{type(exc).__name__}: {exc}'}
    Path(a.out).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='transcript'},ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__': main()
