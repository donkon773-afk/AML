"""Synthetic equivalent-state replay; bytes are not tokens. No model calls by default."""
import argparse
import copy
import json
import shlex
import subprocess
from nam import Receiver, encode


def run(tokenizer_command=None):
    now = 1000000
    peers = {'observer': ['state', 'delta']}
    full_rx = Receiver('bench', 'gateway', peers)
    delta_rx = Receiver('bench', 'gateway', peers)
    entities = [{'id': f'G{i}', 'kind': 'group', 'pos': [1000+i*20,2000,0],
                 'observed_ms': now, 'role': 'INFANTRY', 'count': 20, 'ammo': 0.9} for i in range(10)]
    full_wire, delta_wire = [], []
    for rev in range(1,11):
        if rev > 1:
            entities[0]['pos'][0] += 5
            entities[0]['observed_ms'] = now + rev * 1000
        message = {'v':'NAM/1','id':f'm{rev}','session':'bench','sender':'observer','recipient':'gateway',
                   'seq':rev,'sent_ms':now+rev*1000,'ttl_ms':30000,'kind':'state',
                   'body':{'world':'Altis.bench','rev':rev,'entities':copy.deepcopy(entities)}}
        delta = copy.deepcopy(message)
        # Two keyframes in ten frames; no hidden envelope overhead removed.
        if rev not in (1,6):
            delta['kind'] = 'delta'
            delta['body'] = {'world':'Altis.bench','base':rev-1,'rev':rev,'upsert':[copy.deepcopy(entities[0])],'remove':[]}
        a, b = encode(message), encode(delta)
        full_wire.append(a); delta_wire.append(b)
        full_rx.accept(a,'observer',now+rev*1000)
        delta_rx.accept(b,'observer',now+rev*1000)
        assert full_rx.states == delta_rx.states, 'lossy replay'
    totals = [sum(len(s.encode('utf-8')) for s in stream) for stream in (full_wire, delta_wire)]
    result = {'fixture':'10 groups, 10 frames, 2 keyframes, 1 moving group', 'semantic_replay_equal':True,
              'full_json_bytes':totals[0], 'delta_json_bytes':totals[1],
              'byte_reduction_percent':round(100*(1-totals[1]/totals[0]),2),
              'tokens':None,'prefill_ms':None,'note':'Synthetic byte comparison; not a tokenizer or GPU benchmark.'}
    if tokenizer_command:
        argv = json.loads(tokenizer_command) if tokenizer_command.lstrip().startswith('[') else shlex.split(tokenizer_command)
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) for x in argv):
            raise ValueError('tokenizer command must be argv strings')
        measured, identities = [], set()
        for stream in (full_wire,delta_wire):
            count = 0
            for text in stream:
                response = subprocess.run(argv,input=text,encoding='utf-8',capture_output=True,timeout=30,check=True,shell=False)
                item = json.loads(response.stdout)
                if type(item.get('tokens')) is not int or item['tokens'] < 0 or not item.get('tokenizer'):
                    raise ValueError('invalid tokenizer result')
                count += item['tokens']; identities.add(item['tokenizer'])
            measured.append(count)
        if len(identities) != 1:
            raise ValueError('tokenizer changed during benchmark')
        result['tokens'] = {'tokenizer':next(iter(identities)),'full_json':measured[0],'delta_json':measured[1]}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tokenizer-command')
    args = parser.parse_args()
    print(json.dumps(run(args.tokenizer_command),ensure_ascii=False,indent=2))
