"""Exercise isolation preflight and byte-preserving subprocess capture locally."""

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skill-ab-test" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from report_lib import ValidationError
import run_trace_pair as runner


class TracePairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="trace-pair-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "target" / "SKILL.md"
        self.other = self.root / "other" / "SKILL.md"

    def inventory(self, enabled=False):
        return {
            "errors": [],
            "skills": [
                {"name": "target", "path": str(self.target), "enabled": enabled},
                {"name": "other", "path": str(self.other), "enabled": False},
            ],
        }

    def process(self, body, prompt="공통 입력\r\n", timeout=3):
        destination = self.root / "capture"
        destination.mkdir()
        script = self.root / "fake response.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        result = runner.capture_process(
            [sys.executable, "-u", str(script)], self.root, prompt,
            destination, timeout,
        )
        return result, destination

    def trace_fixture(self, fail_treatment=False, contaminated_inventory=False,
                      guard_failure_arm=None, guard_response=None, missing_probe=False,
                      installed_collision=None):
        """A local executable implements only the CLI/RPC boundary used here."""
        run = self.root / "trace-run"
        (run / "source").mkdir(parents=True)
        (run / "source/SKILL.md").write_text(
            "---\nname: target\ndescription: Test fixture only.\n---\nPreserve supplied facts.\n",
            encoding="utf-8",
        )
        plan = {
            "schema_version": 2, "title": "Local integration test", "model": "test-model",
            "reasoning_effort": "high",
            "source": {"name": "target", "entrypoint": "source/SKILL.md",
                       "url": "https://github.com/example/skills/blob/main/SKILL.md",
                       "commit": "a" * 40},
            "cases": [{"id": f"Q{i}", "prompt": f"요청 {i}", "input": f"자료 {i}",
                       "criteria": [{"id": "C1", "rule": "PRIVATE_MAIN_RULE"}]}
                      for i in (1, 2, 3)],
        }
        (run / "trace-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        executable = self.root / "integration fake codex"
        observations = self.root / "fake-observations"
        observations.mkdir()
        other_skill = self.root / "fake host skills" / "other" / "SKILL.md"
        other_skill.parent.mkdir(parents=True)
        other_skill.write_bytes((run / "source/SKILL.md").read_bytes() if installed_collision == "hash"
                               else b"---\nname: other\ndescription: Other test skill.\n---\nOther instructions.\n")
        other_name = "plugin:target" if installed_collision == "name" else "other"
        executable.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""
            import builtins, errno, json, sys, tomllib
            from pathlib import Path
            args = sys.argv[1:]
            if args == ['--version']:
                print('codex-cli test-fixture')
                raise SystemExit(0)
            config = {{}}
            cli_args = args[:args.index('--')] if '--' in args else args
            for index, argument in enumerate(cli_args):
                if argument == '-c':
                    config.update(tomllib.loads(cli_args[index + 1]))
            assert config['model'] == 'test-model'
            assert config['model_reasoning_effort'] == 'high'
            assert 'sandbox_mode' not in config
            cwd = Path.cwd()
            target = Path({str(run / 'workspace/treatment/.agents/skills/target/SKILL.md')!r})
            other = Path({str(other_skill)!r})
            entries = config.get('skills', {{}}).get('config')
            enabled = {{entry['path']: entry['enabled'] for entry in entries}} if entries else {{}}
            target_enabled = enabled.get(str(target), True)
            other_enabled = enabled.get(str(other), True)
            observations = Path({str(observations)!r})
            if entries is not None:
                assert config['default_permissions'] == 'skill_ab_test'
                assert config['permissions']['skill_ab_test']['extends'] == ':read-only'
            if 'app-server' in args:
                for line in sys.stdin.buffer:
                    message = json.loads(line)
                    if message.get('method') == 'initialize':
                        response = {{'id': 1, 'result': {{}}}}
                    elif message.get('method') == 'skills/list':
                        assert message['params']['cwds'] == [str(cwd)]
                        if {contaminated_inventory!r} and entries is not None and target_enabled:
                            other_enabled = True
                        discovered = [{{'name': {other_name!r}, 'path': str(other), 'enabled': other_enabled}}]
                        if target.is_file() and target.is_relative_to(cwd):
                            discovered.insert(0, {{'name': 'target', 'path': str(target), 'enabled': target_enabled}})
                        response = {{'id': 2, 'result': {{'data': [{{'cwd': str(cwd),
                            'skills': discovered,
                            'errors': []}}]}}}}
                    else:
                        continue
                    sys.stdout.write(json.dumps(response) + '\\n')
                    sys.stdout.flush()
            elif args[0] == 'sandbox':
                assert '--include-managed-config' in args
                assert args[args.index('-P') + 1] == 'skill_ab_test'
                assert args[args.index('-C') + 1] == str(cwd)
                arm = 'treatment' if target_enabled else 'baseline'
                (observations / ('sandbox-' + arm)).write_text(json.dumps(args))
                if {guard_response!r} == 'empty':
                    print(json.dumps({{'checks': [], 'passed': True}}))
                    raise SystemExit(0)
                rules = config['permissions']['skill_ab_test']['filesystem']
                original_open = builtins.open
                def guarded_open(path, *open_args, **open_kwargs):
                    resolved = Path(path).resolve()
                    if {missing_probe!r} and resolved == Path({str(run / 'source/SKILL.md')!r}):
                        raise FileNotFoundError(errno.ENOENT, 'simulated missing probe', str(path))
                    matching = [(Path(rule), mode) for rule, mode in rules.items()
                                if resolved == Path(rule) or resolved.is_relative_to(Path(rule))]
                    permission = max(matching, key=lambda item: len(item[0].parts))[1] if matching else 'read'
                    if {guard_failure_arm!r} == arm and resolved == Path({str(run / 'source/SKILL.md')!r}):
                        permission = 'read'
                    if permission == 'deny':
                        raise PermissionError(errno.EACCES, 'simulated sandbox deny', str(path))
                    return original_open(path, *open_args, **open_kwargs)
                probe_builtins = dict(vars(builtins))
                probe_builtins['open'] = guarded_open
                exec(compile(args[-1], '<read-probe>', 'exec'), {{'__builtins__': probe_builtins}})
            elif args[0] == 'exec':
                assert '--ephemeral' in args and '--json' in args
                assert '-s' not in args and '--sandbox' not in args
                assert args[args.index('-C') + 1] == str(cwd)
                arm = 'treatment' if target_enabled else 'baseline'
                with (observations / ('exec-' + arm)).open('a') as invocation:
                    invocation.write(json.dumps(args) + '\\n')
                (observations / ('input-' + arm)).write_bytes(sys.stdin.buffer.read())
                sys.stdout.buffer.write(b'{{"type":"thread.started"}}\\r\\n')
                sys.stdout.buffer.flush()
                if {fail_treatment!r} and target_enabled:
                    sys.stdout.buffer.write(b'{{"type":"turn.failed"}}\\n')
                    sys.stdout.buffer.flush()
                    sys.stderr.write('fake treatment failure\\n')
                    raise SystemExit(7)
                Path(args[args.index('-o') + 1]).write_text('Q1\\n답변 1\\nQ2\\n답변 2\\nQ3\\n답변 3\\n')
                sys.stdout.buffer.write(b'{{"type":"turn.completed","usage":{{"input_tokens":5,"output_tokens":3}}}}\\n')
                sys.stdout.buffer.flush()
            else:
                raise SystemExit(99)
        """), encoding="utf-8")
        executable.chmod(0o700)
        return run, executable, observations

    def test_config_roundtrips_quoted_unicode_paths_without_shell_interpretation(self):
        # TOML parsing must preserve the argv value, including astral Unicode.
        target = self.root / '스킬 😀 "quoted" $(touch SENTINEL)' / "SKILL.md"
        other = self.root / "other'`name`" / "SKILL.md"
        skills = [{"path": str(other)}, {"path": str(target)}, {"path": str(other)}]
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                value = runner.config_override(skills, target, enabled)
                config = tomllib.loads(value)["skills"]["config"]
                self.assertEqual(len(config), 2)
                actual = {item["path"]: item["enabled"] for item in config}
                self.assertEqual(actual, {str(target.resolve()): enabled, str(other.resolve()): False})
        self.assertFalse((self.root / "SENTINEL").exists())

    def test_config_rejects_target_absent_from_discovered_inventory(self):
        with self.assertRaises(ValidationError):
            runner.config_override([{"path": str(self.other)}], self.target, True)

    def test_permission_table_roundtrips_paths_and_denies_all_but_selected_target(self):
        run = self.root / 'runs' / '시험 😀 "quoted"'
        cwd = run / "workspace/baseline"
        target = run / "workspace/treatment/.agents/skills/target/SKILL.md"
        external = self.root / "external ' skill" / "SKILL.md"
        skills = [{"path": str(target)}, {"path": str(external)}]
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                config = {}
                for override in runner.permission_overrides(run, cwd, skills, target, enabled):
                    config.update(tomllib.loads(override))
                self.assertEqual(config["default_permissions"], "skill_ab_test")
                profile = config["permissions"]["skill_ab_test"]
                self.assertEqual(profile["extends"], ":read-only")
                rules = profile["filesystem"]
                self.assertEqual(rules[str(run.parent)], "deny")
                self.assertEqual(rules[str(cwd)], "read")
                self.assertEqual(rules[str(external.parent)], "deny")
                self.assertEqual(rules[str(target.parent)], "read" if enabled else "deny")
                for suffix in (".codex/skills", ".agents/skills", ".codex/plugins/cache"):
                    self.assertEqual(rules[str(Path.home() / suffix)], "deny")

    def test_off_inventory_allows_target_absent_but_no_active_skills(self):
        inventory = self.inventory(False)
        inventory["skills"] = inventory["skills"][1:]
        state = runner.verify_inventory(inventory, self.target, False)
        self.assertIs(state["target_enabled"], False)
        self.assertIs(state["target_registered"], False)
        self.assertEqual(state["enabled_skills"], 0)

    def test_permission_rules_reject_root_or_home_as_denied_parent(self):
        for run in (Path("/trace-run"), Path.home() / "trace-run"):
            with self.subTest(run=run), self.assertRaises(ValidationError):
                runner.permission_overrides(run, run / "workspace/baseline", [],
                                            run / "workspace/treatment/SKILL.md", False)

    def test_read_guard_rejects_nonexistent_probe_before_launching_sandbox(self):
        run = self.root / "guard-run"
        cwd = run / "workspace"
        cwd.mkdir(parents=True)
        (cwd / "task-scope.txt").write_text("readable marker")
        (run / "source").mkdir()
        (run / "source/SKILL.md").write_text("pinned source")
        target = cwd / "SKILL.md"
        target.write_text("pinned temporary target")
        destination = run / "probe"
        destination.mkdir()
        with self.assertRaises(ValidationError):
            runner.verify_read_guard(str(self.root / "must-not-run"), run, cwd,
                                     [{"path": str(run / "missing/SKILL.md")}],
                                     target, False, [], destination)
        self.assertFalse((destination / "read-probe-stdout.txt").exists())

    def test_inventory_accepts_all_off_and_exactly_target_on(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                state = runner.verify_inventory(self.inventory(enabled), self.target, enabled)
                self.assertIs(state["target_enabled"], enabled)
                self.assertEqual(state["enabled_skills"], int(enabled))

    def test_inventory_rejects_other_active_missing_duplicate_or_failed_discovery(self):
        active = self.inventory(True)
        active["skills"][1]["enabled"] = True
        missing = self.inventory(True)
        missing["skills"] = missing["skills"][1:]
        duplicate = self.inventory(True)
        duplicate["skills"].append(copy.deepcopy(duplicate["skills"][0]))
        failed = self.inventory(True)
        failed["errors"] = [{"path": str(self.other), "message": "unreadable"}]
        for label, inventory in [("extra active", active), ("missing", missing),
                                 ("duplicate", duplicate), ("discovery error", failed)]:
            with self.subTest(label=label), self.assertRaises(ValidationError):
                runner.verify_inventory(inventory, self.target, True)

    def test_inventory_requires_explicit_boolean_states_for_every_discovered_skill(self):
        for unknown in (None, 0, "", []):
            with self.subTest(value=unknown):
                inventory = self.inventory(False)
                inventory["skills"][1]["enabled"] = unknown
                with self.assertRaises(ValidationError):
                    runner.verify_inventory(inventory, self.target, False)

    def test_prompts_share_tasks_and_constraints_but_hide_main_only_criteria(self):
        plan = {"cases": [
            {"id": f"Q{i}", "prompt": f"사용자 요청 {i}", "input": f"공통 자료 {i}",
             "criteria": [{"id": "SECRET_CRITERION", "rule": "SECRET_RULE"}], "why": "SECRET_WHY"}
            for i in (1, 2, 3)
        ]}
        baseline = runner.make_prompt(plan)
        treatment = runner.make_prompt(plan, "TARGET_MARKER", self.target)
        a_prefix, a_tasks = baseline.split("## Q1", 1)
        b_prefix, b_tasks = treatment.split("## Q1", 1)
        self.assertEqual(a_tasks, b_tasks)
        self.assertTrue(b_prefix.startswith(a_prefix))
        self.assertNotIn("TARGET_MARKER", baseline)
        self.assertNotIn(str(self.target), baseline)
        self.assertIn("TARGET_MARKER", treatment)
        self.assertIn(str(self.target), treatment)
        for prompt in (baseline, treatment):
            for secret in ("SECRET_CRITERION", "SECRET_RULE", "SECRET_WHY"):
                self.assertNotIn(secret, prompt)
            self.assertLess(prompt.index("## Q1"), prompt.index("## Q2"))
            self.assertLess(prompt.index("## Q2"), prompt.index("## Q3"))

    def test_capture_preserves_stdout_stderr_and_input_bytes_with_receipt_locations(self):
        expected_out = b'{"type":"first"}\r\n' + '한글 이벤트\n'.encode() + b'partial-final-\xff'
        expected_err = b'warning\r\n\xff'
        prompt = "입력 원문\r\n따옴표 ' \""
        result, destination = self.process(f"""
            from pathlib import Path
            import sys
            Path('received-input.bin').write_bytes(sys.stdin.buffer.read())
            sys.stdout.buffer.write({expected_out!r})
            sys.stdout.buffer.flush()
            sys.stderr.buffer.write({expected_err!r})
            sys.stderr.buffer.flush()
        """, prompt=prompt)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((destination / "events.jsonl").read_bytes(), expected_out)
        self.assertEqual((destination / "stderr.txt").read_bytes(), expected_err)
        self.assertEqual((self.root / "received-input.bin").read_bytes(), prompt.encode())
        receipts = [json.loads(line) for line in (destination / "receipts.jsonl").read_text().splitlines()]
        self.assertEqual([item["line"] for item in receipts], [1, 2, 3])
        for value in [result["started_at"], result["finished_at"], *[item["received_at"] for item in receipts]]:
            self.assertIsNotNone(datetime.fromisoformat(value).tzinfo)

    def test_nonzero_exit_keeps_partial_evidence_and_marks_process_failed(self):
        result, destination = self.process("""
            import sys
            sys.stdin.buffer.read()
            sys.stdout.buffer.write(b'{"type":"started"}\\npartial')
            sys.stdout.buffer.flush()
            sys.stderr.buffer.write(b'upstream failure\\n')
            sys.stderr.buffer.flush()
            raise SystemExit(7)
        """)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual((destination / "events.jsonl").read_bytes(), b'{"type":"started"}\npartial')
        self.assertEqual((destination / "stderr.txt").read_bytes(), b'upstream failure\n')

    def test_timeout_drains_termination_events_and_unterminated_final_bytes(self):
        result, destination = self.process("""
            import signal, sys, time
            def terminate(signum, frame):
                sys.stdout.buffer.write(b'{"type":"terminated"}\\nlast-bytes')
                sys.stdout.buffer.flush()
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, terminate)
            sys.stdin.buffer.read()
            sys.stdout.buffer.write(b'{"type":"started"}\\n')
            sys.stdout.buffer.flush()
            while True:
                time.sleep(.05)
        """, timeout=.3)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual((destination / "events.jsonl").read_bytes(),
                         b'{"type":"started"}\n{"type":"terminated"}\nlast-bytes')

    def test_timeout_applies_while_child_does_not_read_large_stdin(self):
        start = time.monotonic()
        result, _ = self.process("""
            import time
            time.sleep(1.2)
        """, prompt="x" * 2_000_000, timeout=.15)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, .9)

    def test_early_exit_during_stdin_delivery_is_recorded_as_failure(self):
        result, destination = self.process("""
            import os
            os.write(2, b'exited before reading input\\n')
            os._exit(9)
        """, prompt="x" * 2_000_000)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 9)
        self.assertEqual((destination / "stderr.txt").read_bytes(), b'exited before reading input\n')

    def test_stdout_eof_does_not_cancel_process_deadline(self):
        start = time.monotonic()
        result, _ = self.process("""
            import os, sys, time
            sys.stdin.buffer.read()
            os.close(1)
            time.sleep(.9)
        """, timeout=.15)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, .8)

    def test_existing_evidence_is_not_overwritten(self):
        destination = self.root / "capture"
        destination.mkdir()
        path = destination / "events.jsonl"
        path.write_bytes(b'original evidence\n')
        with self.assertRaises((ValidationError, FileExistsError)):
            runner.capture_process([sys.executable, "-c", "raise SystemExit(0)"],
                                   self.root, "", destination, 1)
        self.assertEqual(path.read_bytes(), b'original evidence\n')

    def test_inventory_rpc_uses_literal_argv_and_preserves_received_bytes(self):
        cwd = self.root / "workspace"
        cwd.mkdir()
        destination = self.root / "inventory"
        destination.mkdir()
        executable = self.root / "fake codex"
        argv_path = self.root / "inventory-argv.json"
        init_response = b'{"id":1,"result":{}}\r\n'
        inventory_response = (json.dumps({"id": 2, "result": {"data": [{
            "cwd": str(cwd), "skills": self.inventory()["skills"], "errors": []
        }]}}, ensure_ascii=False) + "\n").encode()
        executable.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""
            import json, sys
            from pathlib import Path
            Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]))
            for line in sys.stdin.buffer:
                request = json.loads(line)
                if request.get('method') == 'initialize':
                    sys.stdout.buffer.write({init_response!r})
                elif request.get('method') == 'skills/list':
                    assert request['params']['cwds'] == [{str(cwd)!r}]
                    assert request['params']['forceReload'] is True
                    sys.stdout.buffer.write({inventory_response!r})
                sys.stdout.buffer.flush()
        """), encoding="utf-8")
        executable.chmod(0o700)
        override = 'model="literal $(touch SENTINEL) `text`"'
        inventory = runner.skill_inventory(str(executable), cwd, [override], destination, timeout=2)
        self.assertEqual(inventory["skills"], self.inventory()["skills"])
        self.assertEqual(json.loads(argv_path.read_text()), ["-c", override, "app-server"])
        self.assertEqual((destination / "inventory-rpc.jsonl").read_bytes(), init_response + inventory_response)
        self.assertFalse((cwd / "SENTINEL").exists())

    def test_pair_runs_once_per_condition_and_records_real_artifact_hashes(self):
        run, executable, observations = self.trace_fixture()
        original_plan = (run / "trace-plan.json").read_bytes()
        original_skill = (run / "source/SKILL.md").read_bytes()
        runner.run_pair(run, str(executable), timeout=3)
        manifest = json.loads((run / "trace-run.json").read_text())
        self.assertEqual({p.name for p in observations.glob("exec-*")}, {"exec-baseline", "exec-treatment"})
        self.assertFalse((run / "workspace/baseline/.agents/skills/target/SKILL.md").exists())
        self.assertEqual((run / "workspace/treatment/.agents/skills/target/SKILL.md").read_bytes(), original_skill)
        self.assertNotEqual(manifest["arms"]["baseline"]["cwd"], manifest["arms"]["treatment"]["cwd"])
        for arm, expected_enabled in (("baseline", False), ("treatment", True)):
            record = manifest["arms"][arm]
            self.assertEqual(record["status"], "completed")
            self.assertIs(record["skill_state"]["target_enabled"], expected_enabled)
            self.assertEqual(record["skill_state"]["enabled_skills"], int(expected_enabled))
            self.assertEqual(record["model"], "test-model")
            self.assertEqual(record["reasoning_effort"], "high")
            self.assertEqual(len((observations / ("exec-" + arm)).read_text().splitlines()), 1)
            self.assertIs(record["read_guard"]["verified"], True)
            probe_args = json.loads((observations / ("sandbox-" + arm)).read_text())
            self.assertIn("--include-managed-config", probe_args)
            self.assertEqual(probe_args[probe_args.index("-P") + 1], "skill_ab_test")
            self.assertEqual(probe_args[probe_args.index("-C") + 1], record["cwd"])
            probe_cli = probe_args[:probe_args.index("--")]
            probe_overrides = [probe_cli[i + 1] for i, value in enumerate(probe_cli) if value == "-c"]
            exec_args = json.loads((observations / ("exec-" + arm)).read_text().splitlines()[0])
            exec_overrides = [exec_args[i + 1] for i, value in enumerate(exec_args) if value == "-c"]
            self.assertEqual(probe_overrides, exec_overrides)
            self.assertEqual(exec_overrides, record["overrides"])
            self.assertNotIn("-s", exec_args)
            self.assertNotIn("--sandbox", exec_args)
            self.assertFalse(any(value.startswith("sandbox_mode=") for value in exec_overrides))
            for key, relative in record["files"].items():
                self.assertEqual(record["sha256"][key], hashlib.sha256((run / relative).read_bytes()).hexdigest())
            self.assertNotIn("PRIVATE_MAIN_RULE", (observations / ("input-" + arm)).read_text())
            self.assertEqual((run / record["files"]["answer"]).read_text(),
                             "Q1\n답변 1\nQ2\n답변 2\nQ3\n답변 3\n")
        self.assertEqual((run / "trace-plan.json").read_bytes(), original_plan)
        self.assertEqual((run / "source/SKILL.md").read_bytes(), original_skill)
        self.assertTrue(manifest["control"]["source_unchanged"])
        self.assertTrue(manifest["control"]["installed_copy_unchanged"])
        before = (run / "trace-run.json").read_bytes()
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual((run / "trace-run.json").read_bytes(), before)

    def test_pair_preserves_successful_arm_when_other_process_fails(self):
        run, executable, _ = self.trace_fixture(fail_treatment=True)
        runner.run_pair(run, str(executable), timeout=3)
        manifest = json.loads((run / "trace-run.json").read_text())
        self.assertEqual(manifest["arms"]["baseline"]["status"], "completed")
        self.assertEqual(manifest["arms"]["treatment"]["status"], "failed")
        self.assertEqual(manifest["arms"]["treatment"]["exit_code"], 7)
        self.assertTrue((run / "arms/baseline/answer.txt").read_text().startswith("Q1"))
        self.assertEqual((run / "arms/treatment/answer.txt").read_bytes(), b"")
        self.assertEqual((run / "arms/treatment/events.jsonl").read_bytes(),
                         b'{"type":"thread.started"}\r\n{"type":"turn.failed"}\n')
        self.assertEqual((run / "arms/treatment/stderr.txt").read_bytes(), b'fake treatment failure\n')

    def test_pair_never_launches_either_response_if_second_inventory_is_contaminated(self):
        run, executable, observations = self.trace_fixture(contaminated_inventory=True)
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual(list(observations.glob("exec-*")), [])
        self.assertTrue((run / "arms/baseline/inventory-rpc.jsonl").is_file())
        self.assertTrue((run / "arms/treatment/inventory-rpc.jsonl").is_file())

    def test_pair_records_both_outcomes_when_one_response_cannot_be_spawned(self):
        run, executable, observations = self.trace_fixture()
        real_popen = runner.subprocess.Popen

        def deny_treatment_spawn(command, *args, **kwargs):
            if len(command) > 1 and command[1] == "exec":
                overrides = [command[i + 1] for i, value in enumerate(command) if value == "-c"]
                skill_config = next(tomllib.loads(value)["skills"]["config"]
                                    for value in overrides if value.startswith("skills.config="))
                if any(item["enabled"] for item in skill_config):
                    raise OSError("simulated process creation failure")
            return real_popen(command, *args, **kwargs)

        with patch.object(runner.subprocess, "Popen", side_effect=deny_treatment_spawn):
            try:
                runner.run_pair(run, str(executable), timeout=3)
            except OSError:
                # A caller-visible failure is allowed; losing the experiment
                # record after the other process ran is not.
                pass
        self.assertTrue((observations / "exec-baseline").exists())
        manifest_path = run / "trace-run.json"
        self.assertTrue(manifest_path.exists(), "a completed arm must retain the failed paired outcome in its manifest")
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["arms"]["baseline"]["status"], "completed")
        self.assertEqual(manifest["arms"]["treatment"]["status"], "failed")
        self.assertTrue(manifest["inputs_sha256"])

    def test_pair_starts_no_models_if_treatment_read_guard_fails(self):
        run, executable, observations = self.trace_fixture(guard_failure_arm="treatment")
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual(list(observations.glob("exec-*")), [])
        failed_probe = run / "arms/treatment/read-probe-stdout.txt"
        self.assertTrue(failed_probe.is_file())
        self.assertIs(json.loads(failed_probe.read_text())["passed"], False)

    def test_read_guard_rejects_empty_checks_despite_passed_claim(self):
        run, executable, observations = self.trace_fixture(guard_response="empty")
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual(list(observations.glob("exec-*")), [])

    def test_missing_probe_file_is_not_mistaken_for_permission_denial(self):
        run, executable, observations = self.trace_fixture(missing_probe=True)
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual(list(observations.glob("exec-*")), [])

    def test_installed_namespaced_target_collision_prevents_model_execution(self):
        run, executable, observations = self.trace_fixture(installed_collision="name")
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual(list(observations.glob("exec-*")), [])

    def test_installed_identical_source_hash_prevents_model_execution(self):
        run, executable, observations = self.trace_fixture(installed_collision="hash")
        with self.assertRaises(ValidationError):
            runner.run_pair(run, str(executable), timeout=3)
        self.assertEqual(list(observations.glob("exec-*")), [])

    def test_cached_skill_header_collision_is_detected_without_inventory_entry(self):
        fake_home = self.root / "fake-home"
        installed = fake_home / ".codex/plugins/cache/vendor/arbitrary-folder/SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_text('---\nname: "target"\ndescription: Prior installed version.\n---\nOld instructions.\n')
        source = self.root / "new-source.md"
        source.write_text("---\nname: target\ndescription: New version.\n---\nDifferent instructions.\n")
        destination = self.root / "eligibility.json"
        with patch.object(runner.Path, "home", return_value=fake_home):
            with self.assertRaises(ValidationError):
                runner.check_not_installed({"source": {"name": "target"}}, {"skills": []},
                                           self.root / "temporary/SKILL.md", source, destination)
        observation = json.loads(destination.read_text())
        self.assertEqual(observation["status"], "already_installed")
        self.assertIn(str(installed), [entry["path"] for entry in observation["collisions"]])


if __name__ == "__main__":
    unittest.main()
