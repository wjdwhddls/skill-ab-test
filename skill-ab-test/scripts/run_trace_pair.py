#!/usr/bin/env python3
"""Run two fresh Codex processes; preserve emitted events, never judge quality."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import queue
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from report_lib import ValidationError, digest, local_path, need, read_json, safe_url, write_json_new, write_new


def now():
    return datetime.now(timezone.utc).isoformat()


def config_override(skills, target, enabled):
    """TOML values are passed as argv, never through a shell."""
    paths = sorted({str(Path(item['path']).resolve()) for item in skills})
    need(str(target.resolve()) in paths, 'pinned skill was not discovered by Codex')
    entries = ['{path=' + json.dumps(path, ensure_ascii=False) + ',enabled=' +
               ('true' if enabled and path == str(target.resolve()) else 'false') + '}' for path in paths]
    return 'skills.config=[' + ','.join(entries) + ']'


def _reader(stream, messages):
    try:
        for line in iter(stream.readline, b''):
            messages.put((now(), line))
    finally:
        messages.put((now(), None))


def skill_inventory(codex, cwd, overrides, destination, timeout=45):
    command = [codex]
    for value in overrides:
        command += ['-c', value]
    command += ['app-server']
    messages = queue.Queue()
    with (destination / 'inventory-stderr.txt').open('xb') as err:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        threading.Thread(target=_reader, args=(process.stdout, messages), daemon=True).start()
        raw_path = destination / 'inventory-rpc.jsonl'
        def send(value):
            process.stdin.write((json.dumps(value) + '\n').encode())
            process.stdin.flush()
        try:
            send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'skill_ab_test', 'version': '2'}}})
            deadline = time.monotonic() + timeout
            with raw_path.open('xb') as raw:
                while time.monotonic() < deadline:
                    try:
                        _, line = messages.get(timeout=max(.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if line is None:
                        raise ValidationError('skills/list process ended before its response')
                    raw.write(line)
                    value = json.loads(line)
                    if value.get('id') == 1:
                        need('error' not in value, 'app-server initialize failed')
                        send({'method': 'initialized', 'params': {}})
                        send({'id': 2, 'method': 'skills/list', 'params': {'cwds': [str(cwd)], 'forceReload': True}})
                    if value.get('id') == 2:
                        need('error' not in value, 'skills/list failed')
                        entries = value.get('result', {}).get('data', [])
                        need(len(entries) == 1 and Path(entries[0]['cwd']).resolve() == cwd.resolve(), 'unexpected skill inventory cwd')
                        entry = entries[0]
                        result = {'observed_at': now(), 'command': command, 'cwd': str(cwd),
                                  'skills': [{'name': s['name'], 'path': s['path'], 'enabled': s['enabled']} for s in entry['skills']],
                                  'errors': entry['errors']}
                        write_json_new(destination / 'inventory.json', result)
                        return result
            raise ValidationError('skills/list timed out')
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdin.close()
            process.stdout.close()


def verify_inventory(inventory, target, enabled):
    need(not inventory['errors'], 'skill discovery reported errors; run stopped')
    need(all(type(s.get('enabled')) is bool for s in inventory['skills']), 'inventory enabled flags must be boolean')
    matches = [s for s in inventory['skills'] if Path(s['path']).resolve() == target.resolve()]
    need(len(matches) <= 1 and (not enabled or len(matches) == 1), 'unexpected pinned target registration')
    need(not matches or matches[0]['enabled'] is enabled, 'target skill override was not applied')
    active = [s for s in inventory['skills'] if s['enabled']]
    need(len(active) == int(enabled), 'unexpected enabled skills; run stopped')
    return {'target_enabled': enabled, 'enabled_skills': len(active), 'target_path': str(target),
            'target_registered': bool(matches), 'target_name': matches[0]['name'] if matches else target.parent.name}


def check_not_installed(plan, inventory, target, source, destination):
    roots = [Path.home() / '.codex/skills', Path.home() / '.agents/skills', Path.home() / '.codex/plugins/cache', Path('/etc/codex/skills')]
    paths = {Path(s['path']).resolve() for s in inventory['skills']}
    for root in roots:
        if root.is_dir():
            paths.update(p.resolve() for p in root.rglob('SKILL.md') if p.is_file())
    source_sha = digest(source)
    inventory_names = {}
    for item in inventory['skills']:
        inventory_names.setdefault(Path(item['path']).resolve(), []).append(item['name'])
    entries, collisions = [], []
    for path in sorted(paths):
        if path == target.resolve():
            continue
        text = path.read_text(encoding='utf-8')
        match = re.search(r'^name:\s*[\"\']?([^\n\"\']+)', text, re.MULTILINE)
        name = match.group(1).strip() if match else path.parent.name
        sha = digest(path)
        names = [name, *inventory_names.get(path, [])]
        entry = {'name': name, 'inventory_names': inventory_names.get(path, []), 'path': str(path), 'sha256': sha}
        entries.append(entry)
        if any(n.split(':')[-1].casefold() == plan['source']['name'].casefold() for n in names) or sha == source_sha:
            collisions.append(entry)
    result = {'status': 'already_installed' if collisions else 'not_installed', 'observed_at': now(),
              'checked_count': len(entries), 'roots': [str(p) for p in roots], 'skills': entries, 'collisions': collisions}
    write_json_new(destination, result)
    need(not collisions, 'target skill already exists locally; only uninstalled skills are eligible')
    return {key: value for key, value in result.items() if key not in ('skills', 'collisions')}


def permission_overrides(root, cwd, skills, target, enabled):
    """Limit reads without changing global configuration or managed policy."""
    need(root.parent not in (Path('/'), Path.home()), 'run directory must be inside a dedicated runs folder')
    rules = {str(root.parent): 'deny', str(cwd): 'read'}
    for path in (Path.home() / '.codex/skills', Path.home() / '.agents/skills', Path.home() / '.codex/plugins/cache'):
        rules[str(path)] = 'deny'
    for skill in skills:
        rules[str(Path(skill['path']).resolve().parent)] = 'deny'
    rules[str(target.parent)] = 'read' if enabled else 'deny'
    table = ','.join(json.dumps(path, ensure_ascii=False) + '=' + json.dumps(value) for path, value in sorted(rules.items()))
    return ['default_permissions="skill_ab_test"',
            'permissions={skill_ab_test={extends=":read-only",filesystem={' + table + '}}}']


def verify_read_guard(codex, root, cwd, skills, target, enabled, overrides, destination):
    probes = {str(Path(skill['path']).resolve()): False for skill in skills}
    probes[str(target)] = enabled
    probes[str(root / 'source/SKILL.md')] = False
    probes[str(cwd / 'task-scope.txt')] = True
    need(all(Path(p).is_file() for p in probes), 'probe paths must exist before checking access')
    # Only try opening one byte. No skill or credential contents are returned.
    program = ('import json,errno\nchecks=' + repr(probes) + '\nresults=[]\n'
               'for path,expected in checks.items():\n'
               ' error=None\n'
               ' try:\n  with open(path,"rb") as f: f.read(1)\n  readable=True\n'
               ' except OSError as e: readable=False; error=e.errno\n'
               ' results.append({"path":path,"expected_readable":expected,"readable":readable,"errno":error})\n'
               'print(json.dumps({"checks":results,"passed":all(x["readable"] if x["expected_readable"] else x["errno"] in (errno.EPERM,errno.EACCES) for x in results)}))\n')
    command = [codex, 'sandbox', '--include-managed-config', '-P', 'skill_ab_test', '-C', str(cwd)]
    for value in overrides:
        command += ['-c', value]
    command += ['--', shutil.which('python3') or '/usr/bin/python3', '-c', program]
    result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=45)
    (destination / 'read-probe-stdout.txt').write_bytes(result.stdout)
    (destination / 'read-probe-stderr.txt').write_bytes(result.stderr)
    need(result.returncode == 0, 'filesystem read probe failed to execute')
    try:
        observation = json.loads(result.stdout)
    except ValueError as exc:
        raise ValidationError('filesystem read probe returned invalid JSON') from exc
    observation.update(command=command, observed_at=now())
    write_json_new(destination / 'read-probe.json', observation)
    checks = observation.get('checks', [])
    need(isinstance(checks, list) and len(checks) == len(probes) and {x.get('path') for x in checks} == set(probes), 'read probe did not check every requested path')
    for check in checks:
        expected = probes[check['path']]
        need(check.get('expected_readable') is expected and type(check.get('readable')) is bool, 'invalid read probe state')
        need((check['readable'] is True and check.get('errno') is None) if expected else
             (check['readable'] is False and check.get('errno') in (1, 13)), 'read probe did not observe the expected permission result')
    need(observation.get('passed') is True, 'filesystem read restrictions were not enforced; no models started')
    return {'verified': True, 'target_readable': enabled, 'checked_paths': len(probes)}


def make_prompt(plan, target_name=None, target_path=None):
    # The criteria stay with the main agent, not in the response process.
    prompt = ('다음 Q1~Q3 사용자 과제에 한국어로 답하세요. 각 결과 앞에 Q1, Q2, Q3 제목을 붙이세요. '
              '비교나 채점은 하지 마세요. 제공된 입력 자료의 사실을 보존하세요. '
              '필요한 읽기 도구를 사용할 수 있습니다. 작업 범위는 아래 텍스트와 현재 제공되는 스킬입니다. '
              '활성 스킬 목록에 없는 스킬은 파일 검색으로 찾아 읽거나 적용하지 마세요. '
              '다른 실행 폴더와 다른 에이전트의 결과는 열람하지 마세요. '
              '과제의 가상 자료만 사용하고 웹이나 연결된 계정에서 추가 정보를 조회하지 마세요. '
              '파일 수정, 외부 서비스 변경, 다른 에이전트 생성은 하지 마세요.\n\n')
    if target_name:
        prompt += (f'이 작업에는 {target_name} 스킬을 사용하세요. '
                   f'먼저 읽기 도구로 {target_path} 본문을 열어 실제 지침을 확인한 뒤 수행하세요.\n\n')
    for case in plan['cases']:
        prompt += f"## {case['id']}\n{case['prompt']}\n\n입력 자료:\n{case['input']}\n\n"
    return prompt


def capture_process(command, cwd, prompt, destination, timeout):
    need(timeout > 0, 'timeout must be positive')
    messages = queue.Queue()
    started = now()
    with (destination / 'stderr.txt').open('xb') as err, (destination / 'events.jsonl').open('xb') as out, \
            (destination / 'receipts.jsonl').open('x', encoding='utf-8') as receipt:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=err, start_new_session=True)
        reader = threading.Thread(target=_reader, args=(process.stdout, messages), daemon=True)
        reader.start()
        def feed():
            try:
                process.stdin.write(prompt.encode('utf-8'))
            except (BrokenPipeError, OSError):
                pass  # Exit status and stderr retain the failure; do not lose stdout.
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        deadline, timed_out, line_number, killed = time.monotonic() + timeout, False, 0, False
        def stop(sig):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if not timed_out:
                        timed_out = True
                        stop(signal.SIGTERM)
                        deadline = time.monotonic() + 3
                    elif not killed:
                        killed = True
                        stop(signal.SIGKILL)
                        deadline = time.monotonic() + 3
                    else:
                        raise ValidationError('stdout failed to close after process-group termination')
                try:
                    received, line = messages.get(timeout=.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                out.write(line)
                out.flush()
                line_number += 1
                receipt.write(json.dumps({'line': line_number, 'received_at': received}) + '\n')
                receipt.flush()
            try:
                code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                stop(signal.SIGKILL)
                code = process.wait()
        finally:
            if process.poll() is None:
                stop(signal.SIGKILL)
                process.wait()
            reader.join(timeout=3)
            writer.join(timeout=3)
            process.stdout.close()
    return {'started_at': started, 'finished_at': now(), 'exit_code': code,
            'status': 'timeout' if timed_out else ('completed' if code == 0 else 'failed')}


def validate_plan(plan, root):
    need(plan.get('schema_version') == 2, 'trace-plan schema_version must be 2')
    for key in ('title', 'model', 'reasoning_effort'):
        need(isinstance(plan.get(key), str) and bool(plan[key].strip()), f'{key} required')
    source = plan['source']
    safe_url(source['url'], github=True)
    need(re.fullmatch(r'[a-zA-Z0-9_-]+', source['name']) is not None, 'skill name must be a simple folder name')
    need(source['entrypoint'] == 'source/SKILL.md', 'source entrypoint must be source/SKILL.md')
    need(local_path(root, source['entrypoint']).is_file(), 'pinned SKILL.md required')
    need(re.fullmatch(r'[0-9a-f]{40}', source['commit']) is not None, 'pinned commit required')
    cases = plan['cases']
    need([c['id'] for c in cases] == ['Q1', 'Q2', 'Q3'], 'Q1~Q3 required in order')
    for case in cases:
        need(isinstance(case['prompt'], str) and case['prompt'].strip(), 'case prompt required')
        need(isinstance(case['input'], str), 'case input required')
        need(bool(case['criteria']), 'predeclared criteria required')


def run_pair(root, codex, timeout):
    root = root.resolve()
    plan = read_json(root / 'trace-plan.json')
    validate_plan(plan, root)
    need(not (root / 'trace-run.json').exists() and not (root / 'arms').exists(), 'existing executions are never overwritten')
    source_files = sorted((root / 'source').rglob('*'))
    need(all(not p.is_symlink() for p in source_files), 'source symlinks are not allowed')
    inputs = {str(p.relative_to(root)): digest(p) for p in [root / 'trace-plan.json', *source_files] if p.is_file()}
    workspace = root / 'workspace'
    need(not workspace.exists(), 'execution workspace must be new')
    cwds = {arm: workspace / arm for arm in ('baseline', 'treatment')}
    target_dir = cwds['treatment'] / '.agents' / 'skills' / plan['source']['name']
    shutil.copytree(root / 'source', target_dir)
    target = target_dir / 'SKILL.md'
    for cwd in cwds.values():
        write_new(cwd / 'task-scope.txt', 'This is the readable text-task workspace.\n')
    # Config is observed for change detection only; no content is stored or copied.
    global_config = Path.home() / '.codex' / 'config.toml'
    config_before = digest(global_config) if global_config.is_file() else None
    version = subprocess.run([codex, '--version'], capture_output=True, check=True, text=True).stdout.strip()
    run = {'schema_version': 2, 'cli_version': version, 'created_at': now(), 'inputs_sha256': inputs,
           'control': {'method': 'per_process_skills_config', 'filesystem_isolation': False,
                       'plugins_disabled': True,
                       'filesystem_read_guard': True,
                       'global_config_unchanged': None,
                       'limitations': ['대상·설치 스킬·실험 기록 경로의 읽기 제한을 검사했다. 시스템 전체의 모든 사본을 격리한 것은 아니다.',
                                       'CLI가 내보낸 이벤트만 관찰한다. 내부 사고 과정이나 기록되지 않은 동작은 판단하지 않는다.',
                                       '각 조건 1회 실행이다. 작은 응답 차이를 스킬의 인과 효과로 단정하지 않는다.']},
           'arms': {}}
    discovery = root / 'discovery'
    discovery.mkdir()
    # Match the same runtime configuration for listing and execution.
    common = ['model=' + json.dumps(plan['model']), 'model_reasoning_effort=' + json.dumps(plan['reasoning_effort']),
              'features.plugins=false', 'features.remote_plugin=false']
    initial = skill_inventory(codex, cwds['treatment'], common, discovery)
    need(not initial['errors'], 'initial skill discovery failed')
    run['control']['eligibility'] = check_not_installed(plan, initial, target, root / 'source/SKILL.md', root / 'eligibility.json')
    run['inputs_sha256']['eligibility.json'] = digest(root / 'eligibility.json')
    for arm in ('baseline', 'treatment'):
        cwd = cwds[arm]
        dest = root / 'arms' / arm
        dest.mkdir(parents=True)
        overrides = common + [config_override(initial['skills'], target, arm == 'treatment')]
        overrides += permission_overrides(root, cwd, initial['skills'], target, arm == 'treatment')
        inventory = skill_inventory(codex, cwd, overrides, dest)
        state = verify_inventory(inventory, target, arm == 'treatment')
        guard = verify_read_guard(codex, root, cwd, initial['skills'], target, arm == 'treatment', overrides, dest)
        prompt = make_prompt(plan, state['target_name'] if arm == 'treatment' else None, target)
        write_new(dest / 'prompt.txt', prompt)
        command = [codex, 'exec', '--json', '--ephemeral', '--skip-git-repo-check', '-C', str(cwd)]
        for value in overrides:
            command += ['-c', value]
        command += ['-o', str(dest / 'answer.txt'), '-']
        run['arms'][arm] = {'command': command, 'cwd': str(cwd), 'model': plan['model'],
                            'reasoning_effort': plan['reasoning_effort'], 'preflight_verified': True,
                            'skill_state': state, 'read_guard': guard, 'overrides': overrides}
    # Both manifests must pass before either model runs.
    write_json_new(root / 'trace-start.json', run)
    def execute_arm(arm):
        info = run['arms'][arm]
        dest = root / 'arms' / arm
        print(f'{arm}: starting ({info["skill_state"]["enabled_skills"]} enabled skills)', flush=True)
        attempt_started = now()
        try:
            info.update(capture_process(info['command'], cwds[arm], (dest / 'prompt.txt').read_text(), dest, timeout))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            info.update(status='failed', exit_code=None, started_at=attempt_started, finished_at=now(), error=str(exc))
        for name in ('events.jsonl', 'receipts.jsonl', 'stderr.txt'):
            if not (dest / name).exists():
                write_new(dest / name, '')
        if not (dest / 'answer.txt').exists():
            write_new(dest / 'answer.txt', '')
        info['files'] = {key: str((dest / name).relative_to(root)) for key, name in {
            'events': 'events.jsonl', 'stderr': 'stderr.txt', 'answer': 'answer.txt',
            'receipts': 'receipts.jsonl', 'inventory': 'inventory.json', 'prompt': 'prompt.txt',
            'inventory_rpc': 'inventory-rpc.jsonl', 'read_probe': 'read-probe.json',
            'read_probe_stdout': 'read-probe-stdout.txt', 'read_probe_stderr': 'read-probe-stderr.txt'}.items()}
        info['sha256'] = {key: digest(root / path) for key, path in info['files'].items()}
        print(f'{arm}: {info["status"]}', flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(execute_arm, ('baseline', 'treatment')))
    run['control']['global_config_unchanged'] = config_before == (digest(global_config) if global_config.is_file() else None)
    run['control']['source_unchanged'] = all(digest(root / path) == sha for path, sha in inputs.items())
    run['control']['installed_copy_unchanged'] = all(digest(target_dir / p.relative_to(root / 'source')) == inputs[str(p.relative_to(root))] for p in source_files if p.is_file())
    write_json_new(root / 'trace-run.json', run)
    print(str(root / 'trace-run.json'), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--codex', default=shutil.which('codex') or 'codex')
    parser.add_argument('--timeout', type=int, default=600, help='Per-response process timeout in seconds')
    args = parser.parse_args()
    try:
        run_pair(args.run_dir, args.codex, args.timeout)
    except (ValidationError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Run failed; existing evidence preserved: {exc}\n')


if __name__ == '__main__':
    main()
