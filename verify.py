"""Offline release gate. Exit 0=all passed, 1=failed, 2=incomplete.
No models, servers, package installs or network calls are started here.
"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent


def main():
    results=[]
    def record(name,status,detail=''):
        results.append(status)
        print(f'[{status}] {name}' + (f' -- {detail}' if detail else ''),flush=True)
    def run(name,cmd):
        try:
            result=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,timeout=90,encoding='utf-8',errors='replace')
        except (OSError,subprocess.TimeoutExpired) as exc:
            record(name,'FAIL',str(exc));return
        status='PASS' if result.returncode==0 else 'FAIL'
        record(name,status)
        print(result.stdout.strip())
        if result.stderr.strip(): print(result.stderr.strip())

    expected=['scripts/aml_codec.py','scripts/aml_codec.js','scripts/nam.py',
              'scripts/frame_validation.py','scripts/grammar_probe.py',
              'scripts/llm_client.py','scripts/llm_client_sdk.py',
              'scripts/aml_grammar.lark','docs/aml_v2.gbnf','assets/nam.schema.json',
              'tests/test_regressions.py','tests/test_gbnf_file.mjs','package-lock.json']
    missing=[x for x in expected if not (ROOT/x).is_file()]
    record('layout','FAIL' if missing else 'PASS',', '.join(missing))
    deps=[x for x in ['lark','jsonschema'] if importlib.util.find_spec(x) is None]
    record('Python dependencies','SKIP' if deps else 'PASS',
           'missing '+', '.join(deps)+'; pip install -r requirements-test.txt' if deps else '')
    node=shutil.which('node')
    if not deps and node:
        run('Python, independent schema and cross-language regression tests',
            [sys.executable,'-m','unittest','discover','-s','tests','-p','test_*.py'])
    else:
        record('Python suite','SKIP','requires Python dependencies and Node for cross-language tests')
    if node:
        run('JavaScript tests',[node,'--test','tests/test_aml_codec.test.js'])
        dependency=subprocess.run([node,'-e',"require.resolve('gbnf')"],cwd=ROOT,capture_output=True)
        if dependency.returncode:
            record('GBNF equivalent-language check','SKIP','run npm ci')
        else:
            run('GBNF equivalent-language check',[node,'tests/test_gbnf_file.mjs'])
    else:
        record('JavaScript and GBNF checks','SKIP','Node not installed')
    if not deps:
        result=subprocess.run([sys.executable,'scripts/llm_client.py','--demo'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace')
        expected_lines=['keyframe accepted','proposal accepted','ghost target rejected by receiver','missing t<ms> rejected by codec']
        passed=result.returncode==0 and all(x in result.stdout for x in expected_lines)
        record('offline demo','PASS' if passed else 'FAIL')
        print(result.stdout.strip())
        if not passed: print(result.stderr.strip())
    else: record('offline demo','SKIP','requires lark')
    if 'FAIL' in results:
        print('OFFLINE FAILED');return 1
    if 'SKIP' in results:
        print('OFFLINE INCOMPLETE: skipped checks are not passes');return 2
    print('OFFLINE PASSED. Native engine loading, sampling, latency and decision quality remain UNVERIFIED.')
    return 0


if __name__=='__main__': sys.exit(main())
