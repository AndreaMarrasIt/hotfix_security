import ast
import base64
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class ApiException(Exception):
    def __init__(self, status):
        self.status = status


class DeleteRequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'requests.db'
        self.batch = Mock()
        source = ast.parse(Path(__file__).with_name('main.py').read_text())
        endpoint = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'delete_request')
        endpoint.decorator_list = []
        namespace = {
            'db': self.db,
            'batch': self.batch,
            'NAMESPACE': 'test',
            'HTTPException': HTTPException,
            'client': SimpleNamespace(exceptions=SimpleNamespace(ApiException=ApiException)),
        }
        exec(compile(ast.Module(body=[endpoint], type_ignores=[]), 'main.py', 'exec'), namespace)
        self.delete = namespace['delete_request']
        with self.db() as connection:
            connection.execute('CREATE TABLE requests (id TEXT PRIMARY KEY, job_name TEXT)')
            connection.executemany('INSERT INTO requests VALUES (?, ?)', [('old', 'job-old'), ('keep', 'job-keep')])

    def db(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def assert_preserved(self):
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 2)

    def test_finished_and_expired_jobs_can_be_removed(self):
        for succeeded, failed, missing in [(1, 0, False), (0, 1, False), (0, 0, True)]:
            with self.subTest(succeeded=succeeded, failed=failed, missing=missing):
                self.batch.read_namespaced_job.side_effect = ApiException(404) if missing else None
                self.batch.read_namespaced_job.return_value = SimpleNamespace(status=SimpleNamespace(succeeded=succeeded, failed=failed))
                self.assertEqual(self.delete('old'), {'id': 'old', 'deleted': True})
                with self.db() as connection:
                    self.assertEqual([row['id'] for row in connection.execute('SELECT id FROM requests')], ['keep'])
                    connection.execute('INSERT INTO requests VALUES (?, ?)', ('old', 'job-old'))

    def test_active_job_is_preserved(self):
        self.batch.read_namespaced_job.return_value = SimpleNamespace(status=SimpleNamespace(succeeded=None, failed=None))
        with self.assertRaises(HTTPException) as result:
            self.delete('old')
        self.assertEqual(result.exception.status_code, 409)
        self.assert_preserved()

    def test_cluster_errors_preserve_history(self):
        for error in [ApiException(403), RuntimeError('offline')]:
            with self.subTest(error=error):
                self.batch.read_namespaced_job.side_effect = error
                with self.assertRaises(HTTPException) as result:
                    self.delete('old')
                self.assertEqual(result.exception.status_code, 503)
                self.assert_preserved()

    def test_unknown_or_injection_identifier_does_not_delete(self):
        with self.assertRaises(HTTPException) as result:
            self.delete("old' OR 1=1 --")
        self.assertEqual(result.exception.status_code, 404)
        self.batch.read_namespaced_job.assert_not_called()
        self.assert_preserved()


class RunnerDNSTests(unittest.TestCase):
    def test_jobs_resolve_external_domains_before_search_suffixes(self):
        source = ast.parse(Path(__file__).with_name('main.py').read_text())
        factory = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'job_spec')
        kubernetes_client = Mock()
        namespace = {'client': kubernetes_client, 'base64': base64, 'RUNNER_IMAGE': 'test-runner', 'build_prompt': lambda task, *args: task}
        exec(compile(ast.Module(body=[factory], type_ignores=[]), 'main.py', 'exec'), namespace)
        for mode in ('analyze', 'propose', 'apply'):
            with self.subTest(mode=mode):
                kubernetes_client.reset_mock()
                namespace['job_spec']('test-job', 'https://github.com/owner/repo', 'Test task', mode, 'test-id')
                kubernetes_client.V1PodDNSConfigOption.assert_called_once_with(name='ndots', value='1')
                kubernetes_client.V1PodDNSConfig.assert_called_once_with(options=[kubernetes_client.V1PodDNSConfigOption.return_value])
                self.assertIs(kubernetes_client.V1PodSpec.call_args.kwargs['dns_config'], kubernetes_client.V1PodDNSConfig.return_value)
                env_names = [call.kwargs['name'] for call in kubernetes_client.V1EnvVar.call_args_list]
                self.assertEqual('WRITE_TOKEN' in env_names, mode == 'apply')


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'history.db'
        self.batch = Mock()
        self.factory = Mock(return_value='job-spec')
        self.namespace = {
            'db': self.db, 'batch': self.batch, 'job_spec': self.factory,
            'HTTPException': HTTPException, 'NAMESPACE': 'test',
            'json': json, 're': re, 'uuid': uuid, 'datetime': datetime, 'timezone': timezone,
            'FollowupRequest': SimpleNamespace, 'ApproveRequest': SimpleNamespace, 'CreateRequest': SimpleNamespace,
            'Path': Path, '__file__': str(Path(__file__).with_name('main.py')),
            'ALLOWED_HOSTS': {'github.com'},
        }
        source = ast.parse(Path(__file__).with_name('main.py').read_text())
        names = {'init_db', 'launch_request', 'get_conversation', 'followup', 'approve', 'validate_branch', 'build_prompt', 'load_profiles', 'get_config', 'create_request', 'validate_repo'}
        nodes = []
        for node in source.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in ('AGENTS', 'SKILLS', 'PROFILE_ROOT') for target in node.targets):
                nodes.append(node)
            if isinstance(node, ast.FunctionDef) and node.name in names:
                node.decorator_list = []
                nodes.append(node)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), self.namespace)
        self.namespace['init_db']()
        self.namespace['get_request'] = self.get_request

    def db(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        return connection

    def get_request(self, request_id):
        with self.db() as connection:
            row = connection.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Request not found')
        return dict(row)

    def completed(self, mode='analyze'):
        result = self.namespace['launch_request']('https://github.com/owner/repo', 'Review the inputs', mode)
        with self.db() as connection:
            connection.execute("UPDATE requests SET status='complete',report='Previous finding',patch_b64='cGF0Y2g=',base_sha=? WHERE id=?", ('a' * 40, result['id']))
        return result['id']

    def test_migration_keeps_legacy_history(self):
        with self.db() as connection:
            connection.execute('DROP TABLE requests')
            connection.execute('CREATE TABLE requests (id TEXT PRIMARY KEY, repo_url TEXT, task TEXT, mode TEXT, status TEXT, job_name TEXT, created_at TEXT, report TEXT, patch_b64 TEXT, base_sha TEXT, pr_url TEXT, error TEXT)')
            connection.execute("INSERT INTO requests (id,task) VALUES ('legacy','Existing question')")
        self.namespace['init_db']()
        self.namespace['init_db']()
        item = self.get_request('legacy')
        self.assertEqual(item['conversation_id'], 'legacy')
        self.assertEqual(item['task'], 'Existing question')
        self.assertEqual(item['agent'], 'general')

    def test_followup_preserves_context_repository_and_profiles(self):
        parent = self.completed()
        result = self.namespace['followup'](parent, SimpleNamespace(task='Please propose a fix', agent='security', skills=['tests', 'input-validation']))
        item = self.get_request(result['id'])
        self.assertEqual(item['parent_id'], parent)
        self.assertEqual(item['conversation_id'], parent)
        self.assertEqual(item['repo_url'], 'https://github.com/owner/repo')
        self.assertEqual(item['mode'], 'propose')
        self.assertEqual(json.loads(item['skills']), ['tests', 'input-validation'])
        self.assertIn('Previous finding', self.factory.call_args.kwargs['context'])
        self.assertEqual(self.factory.call_args.kwargs['agent'], 'security')
        self.assertEqual(len(self.namespace['get_conversation'](parent)), 2)

    def test_parallel_followups_are_rejected(self):
        parent = self.completed()
        request = SimpleNamespace(task='Another analysis', mode='analyze', agent='general', skills=[])
        self.namespace['followup'](parent, request)
        with self.assertRaises(HTTPException) as error:
            self.namespace['followup'](parent, request)
        self.assertEqual(error.exception.status_code, 409)

    def test_branch_validation(self):
        for branch in ('', '-option', 'main', 'master', 'HEAD', 'fix/../main', 'fix//name', 'fix/name.lock', 'fix/name.', 'fix/a b', 'fix/a:main'):
            with self.subTest(branch=branch), self.assertRaises(HTTPException):
                self.namespace['validate_branch'](branch)
        self.assertEqual(self.namespace['validate_branch']('copilot/fix-inputs-123'), 'copilot/fix-inputs-123')

    def test_approval_uses_explicit_branch_and_exact_saved_patch(self):
        parent = self.completed('propose')
        result = self.namespace['approve'](parent, SimpleNamespace(approved_by='Reviewer', branch_name='fix/input-validation'))
        item = self.get_request(result['id'])
        self.assertEqual(item['branch_name'], 'fix/input-validation')
        self.assertEqual(item['parent_id'], parent)
        self.assertEqual(item['patch_b64'], 'cGF0Y2g=')
        self.assertEqual(self.factory.call_args.kwargs['base_sha'], 'a' * 40)
        with self.assertRaises(HTTPException):
            self.namespace['approve'](parent, SimpleNamespace(approved_by='Reviewer', branch_name='fix/duplicate'))

    def test_unknown_profiles_are_rejected(self):
        for agent, skills in [('untrusted', []), ('general', ['arbitrary-file'])]:
            with self.subTest(agent=agent, skills=skills), self.assertRaises(HTTPException):
                self.namespace['build_prompt']('Review inputs', agent, skills)
        prompt = self.namespace['build_prompt']('Review inputs', 'security', ['tests'], 'Earlier answer')
        self.assertIn('Earlier answer', prompt)
        self.assertIn('focused tests', prompt)

    def test_create_request_does_not_require_mode(self):
        self.namespace['validate_repo'] = lambda value: value
        result = self.namespace['create_request'](SimpleNamespace(repo_url='https://github.com/owner/repo', task='Review the inputs', agent='general', skills=[]))
        self.assertEqual(self.get_request(result['id'])['mode'], 'propose')

    def test_markdown_profiles_are_discovered_and_reloaded(self):
        root = Path(self.directory.name) / 'profiles'
        for category in ('agents', 'skills'):
            (root / category).mkdir(parents=True)
        agent = root / 'agents' / 'custom.md'
        agent.write_text('Custom agent instructions', encoding='utf-8')
        (root / 'skills' / 'custom-skill.md').write_text('Custom skill instructions', encoding='utf-8')
        (root / 'agents' / 'ignored.txt').write_text('Not a profile', encoding='utf-8')
        self.namespace['PROFILE_ROOT'] = root
        configuration = self.namespace['get_config']()
        self.assertEqual(configuration['agents'], ['custom'])
        self.assertEqual(configuration['skills'], ['custom-skill'])
        prompt = self.namespace['build_prompt']('Review inputs', 'custom', ['custom-skill', 'custom-skill'])
        self.assertIn('Custom agent instructions', prompt)
        self.assertEqual(prompt.count('Custom skill instructions'), 1)
        agent.write_text('Updated agent instructions', encoding='utf-8')
        self.assertIn('Updated agent instructions', self.namespace['build_prompt']('Review inputs', 'custom'))
        with self.assertRaises(HTTPException):
            self.namespace['build_prompt']('Review inputs', '../custom')

    def test_failed_apply_can_be_retried_with_a_new_branch(self):
        parent = self.completed('propose')
        first = self.namespace['approve'](parent, SimpleNamespace(approved_by='Reviewer', branch_name='fix/first'))
        with self.db() as connection:
            connection.execute("UPDATE requests SET status='failed' WHERE id=?", (first['id'],))
        second = self.namespace['approve'](parent, SimpleNamespace(approved_by='Reviewer', branch_name='fix/second'))
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(self.get_request(second['id'])['branch_name'], 'fix/second')

    def test_runner_failure_logs_are_persisted_and_then_cleared(self):
        request_id = self.completed()
        source = ast.parse(Path(__file__).with_name('main.py').read_text())
        nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in ('get_request', 'extract')]
        for node in nodes:
            node.decorator_list = []
        core = Mock()
        core.list_namespaced_pod.return_value = SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='runner'))])
        core.read_namespaced_pod_log.return_value = 'ERROR: branch already exists; choose a new name.'
        namespace = {**self.namespace, 'core': core}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'main.py', 'exec'), namespace)
        self.batch.read_namespaced_job.return_value = SimpleNamespace(status=SimpleNamespace(succeeded=0, failed=1))
        result = namespace['get_request'](request_id)
        self.assertIn('branch already exists', result['error'])
        self.assertEqual(self.get_request(request_id)['error'], result['error'])
        self.batch.read_namespaced_job.return_value = SimpleNamespace(status=SimpleNamespace(succeeded=1, failed=0))
        namespace['get_request'](request_id)
        self.assertIsNone(self.get_request(request_id)['error'])

    def test_prompt_context_is_bounded_by_bytes(self):
        prompt = self.namespace['build_prompt']('Latest request', context='Earlier response ' * 10000)
        self.assertLess(len(prompt.encode('utf-8')), 50000)
        self.assertIn('omitted due to size limits', prompt)
        self.assertIn('Latest request', prompt)


@unittest.skipUnless(shutil.which('git') and shutil.which('bash'), 'Requires Git and Bash')
class RunnerScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.seed.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.environment = {
            **os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'REPO_URL': str(self.remote), 'TASK_B64': base64.b64encode(b'Review and fix inputs').decode(),
            'REPO_TOKEN': 'test-read', 'WRITE_TOKEN': 'test-write', 'COPILOT_GITHUB_TOKEN': 'test-copilot',
            'PATH': str(self.bin) + os.pathsep + os.environ['PATH'], 'HOME': str(self.root),
            'BRANCH_NAME': 'fix/reviewed-inputs', 'GH_CALLS': str(self.root / 'gh-calls.jsonl'),
        }
        self.git('init', '--bare', '--initial-branch=main', str(self.remote))
        self.git('init', '--initial-branch=main', str(self.seed))
        self.git('config', 'user.name', 'Test Author', cwd=self.seed)
        self.git('config', 'user.email', 'test@example.invalid', cwd=self.seed)
        (self.seed / 'main.txt').write_text('original\n')
        self.git('add', 'main.txt', cwd=self.seed)
        self.git('commit', '-m', 'Initial test commit', cwd=self.seed)
        self.git('remote', 'add', 'origin', str(self.remote), cwd=self.seed)
        self.git('push', 'origin', 'main', cwd=self.seed)
        self.sha = self.git('rev-parse', 'HEAD', cwd=self.seed).stdout.strip()
        self.environment['EXPECTED_SHA'] = self.sha
        (self.seed / 'main.txt').write_text('reviewed fix\n')
        patch = self.git('diff', '--binary', cwd=self.seed).stdout.encode()
        (self.seed / 'main.txt').write_text('original\n')
        self.environment['PATCH_B64'] = base64.b64encode(patch).decode()
        fixtures = {
            'copilot': '''#!/usr/bin/env python3
import os, sys
from pathlib import Path
print('Assistant test report')
if '--allow-tool=write' in sys.argv:
    Path('main.txt').write_text('reviewed fix\\n')
raise SystemExit(int(os.getenv('FAKE_EXIT', '0')))
''',
            'gh': '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['GH_CALLS'], 'a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
print('https://github.com/owner/repo/pull/1')
''',
        }
        for name, content in fixtures.items():
            executable = self.bin / name
            executable.write_text(content)
            executable.chmod(0o755)
        source = Path(__file__).parents[1].joinpath('runner/run.sh').read_text()
        self.script = self.root / 'run.sh'
        self.script.write_text(source.replace('/workspace', str(self.root / 'workspace')).replace('/tmp/git-askpass', str(self.root / 'git-askpass')))

    def git(self, *arguments, cwd=None):
        return subprocess.run(['git', *arguments], cwd=cwd, env=self.environment, text=True, capture_output=True, check=True)

    def run_mode(self, mode, **environment):
        return subprocess.run(['bash', str(self.script)], env={**self.environment, 'RUN_MODE': mode, **environment}, text=True, capture_output=True, timeout=20)

    def test_apply_creates_only_the_user_branch_and_pr(self):
        result = self.run_mode('apply')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git('--git-dir', str(self.remote), 'show', 'fix/reviewed-inputs:main.txt').stdout, 'reviewed fix\n')
        self.assertEqual(self.git('--git-dir', str(self.remote), 'show', 'main:main.txt').stdout, 'original\n')
        calls = [json.loads(line) for line in (self.root / 'gh-calls.jsonl').read_text().splitlines()]
        create = calls[0]
        self.assertEqual(create[create.index('--base') + 1], 'main')
        self.assertEqual(create[create.index('--head') + 1], 'fix/reviewed-inputs')
        self.assertEqual(create[create.index('--repo') + 1], str(self.remote))

    def test_existing_branch_is_never_overwritten(self):
        self.git('push', 'origin', 'HEAD:refs/heads/fix/reviewed-inputs', cwd=self.seed)
        result = self.run_mode('apply')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('branch already exists', result.stdout)
        self.assertFalse((self.root / 'gh-calls.jsonl').exists())
        self.assertEqual(self.git('--git-dir', str(self.remote), 'show', 'fix/reviewed-inputs:main.txt').stdout, 'original\n')

    def test_changed_base_prevents_apply(self):
        result = self.run_mode('apply', EXPECTED_SHA='b' * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('base branch changed', result.stdout)
        self.assertFalse((self.root / 'gh-calls.jsonl').exists())

    def test_missing_branch_prevents_apply(self):
        result = self.run_mode('apply', BRANCH_NAME='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('new branch name is required', result.stdout)

    def test_copilot_failure_is_not_reported_as_success(self):
        result = self.run_mode('analyze', FAKE_EXIT='7')
        self.assertEqual(result.returncode, 7)
        self.assertIn('=== REPORT_BEGIN ===', result.stdout)
        self.assertIn('Assistant test report', result.stdout)

    def test_proposal_emits_reviewable_patch_without_pushing(self):
        result = self.run_mode('propose')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        encoded = result.stdout.split('=== PATCH_B64_BEGIN ===')[1].split('=== PATCH_B64_END ===')[0].strip()
        self.assertIn('+reviewed fix', base64.b64decode(encoded).decode())
        self.assertFalse((self.root / 'gh-calls.jsonl').exists())


if __name__ == '__main__':
    unittest.main()