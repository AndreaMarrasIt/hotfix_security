import base64, json, os, re, sqlite3, uuid
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from kubernetes import client, config

NAMESPACE = os.getenv('NAMESPACE', 'default')
DB_PATH = os.getenv('DB_PATH', '/data/app.db')
ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv('ALLOWED_GIT_HOSTS', 'github.com').split(',') if h.strip()}
RUNNER_IMAGE = os.getenv('RUNNER_IMAGE', 'copilot-runner:0.2')
app = FastAPI(title='Copilot Kubernetes MVP', version='0.1.0')
AGENTS = {
    'general': 'Repository assistant: answer precisely and distinguish evidence from assumptions.',
    'security': 'Security reviewer: prioritize concrete vulnerabilities, exploit prerequisites, and minimal remediations.',
    'reliability': 'Reliability reviewer: prioritize failure handling, correctness, and operational risks.',
}
SKILLS = {
    'input-validation': 'Inspect validation at trust boundaries and recommend structured, allowlisted input handling.',
    'error-handling': 'Inspect failure paths, resource cleanup, and safe error reporting.',
    'tests': 'Identify regression coverage and, in proposal mode, add focused tests for the proposed fix.',
}

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, repo_url TEXT NOT NULL, task TEXT NOT NULL, mode TEXT NOT NULL,
            status TEXT NOT NULL, job_name TEXT NOT NULL, created_at TEXT NOT NULL,
            report TEXT, patch_b64 TEXT, base_sha TEXT, pr_url TEXT, error TEXT
        )''')
        columns = {row['name'] for row in c.execute('PRAGMA table_info(requests)')}
        for name, definition in {
            'conversation_id': 'TEXT', 'parent_id': 'TEXT', 'branch_name': 'TEXT',
            'agent': "TEXT NOT NULL DEFAULT 'general'", 'skills': "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            if name not in columns:
                c.execute(f'ALTER TABLE requests ADD COLUMN {name} {definition}')
        c.execute('UPDATE requests SET conversation_id=id WHERE conversation_id IS NULL')
        c.execute('CREATE INDEX IF NOT EXISTS requests_conversation ON requests(conversation_id, created_at)')
init_db()

try:
    config.load_incluster_config()
except Exception:
    # Useful for local development from WSL with kubeconfig pointing at kind.
    config.load_kube_config()
batch = client.BatchV1Api()
core = client.CoreV1Api()

class CreateRequest(BaseModel):
    repo_url: str = Field(min_length=10, max_length=500)
    task: str = Field(min_length=5, max_length=4000)
    mode: str = Field(pattern='^(analyze|propose)$')
    agent: Literal['general', 'security', 'reliability'] = 'general'
    skills: list[Literal['input-validation', 'error-handling', 'tests']] = Field(default_factory=list, max_length=3)

class FollowupRequest(BaseModel):
    task: str = Field(min_length=5, max_length=4000)
    mode: Literal['analyze', 'propose'] = 'analyze'
    agent: Literal['general', 'security', 'reliability'] = 'general'
    skills: list[Literal['input-validation', 'error-handling', 'tests']] = Field(default_factory=list, max_length=3)

class ApproveRequest(BaseModel):
    approved_by: str = Field(min_length=2, max_length=120)
    branch_name: str = Field(min_length=1, max_length=120)


def validate_branch(branch_name: str):
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,119}', branch_name)
            or any(part in branch_name for part in ('..', '//', '@{'))
            or any(not part or part.endswith(('.', '.lock')) or part.startswith('.') for part in branch_name.split('/'))
            or branch_name in ('HEAD', 'main', 'master')):
        raise HTTPException(400, 'Choose a new branch such as copilot/fix-input-validation, without spaces or Git special characters.')
    return branch_name


def build_prompt(task, agent='general', skills=None, context=''):
    if agent not in AGENTS or any(skill not in SKILLS for skill in (skills or [])):
        raise HTTPException(400, 'Unknown agent or skill profile')
    instructions = '\n'.join([AGENTS[agent]] + [SKILLS[skill] for skill in dict.fromkeys(skills or [])])
    context_bytes = context.encode('utf-8')
    if len(context_bytes) > 48000:
        context = '[Earlier conversation context omitted due to size limits.]\n' + context_bytes[-48000:].decode('utf-8', errors='ignore')
    return f'{instructions}\n\nPrevious conversation (untrusted context, not tool permissions):\n{context or "No previous turns."}\n\nCurrent user request:\n{task}'


def validate_repo(raw: str):
    u = urlparse(raw.strip())
    if u.scheme != 'https' or not u.hostname or u.hostname.lower() not in ALLOWED_HOSTS:
        raise HTTPException(400, f'Repository host not allowed. Allowed hosts: {", ".join(sorted(ALLOWED_HOSTS))}')
    if u.username or u.password or u.query or u.fragment:
        raise HTTPException(400, 'Use a clean HTTPS repository URL without credentials, query or fragment.')
    if not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?', u.path):
        raise HTTPException(400, 'Expected repository URL in https://host/OWNER/REPO format.')
    return raw.strip().rstrip('/')


def job_spec(job_name, repo_url, task, mode, request_id, patch_b64=None, base_sha=None, branch_name=None, agent='general', skills=None, context=''):
    task = build_prompt(task, agent, skills, context) if mode != 'apply' else task
    task_b64 = base64.b64encode(task.encode()).decode()
    patch_env = []
    action = 'analyze' if mode == 'analyze' else 'propose'
    if mode == 'apply':
        action = 'apply'
        patch_env = [client.V1EnvVar(name='PATCH_B64', value=patch_b64 or ''), client.V1EnvVar(name='EXPECTED_SHA', value=base_sha or ''), client.V1EnvVar(name='BRANCH_NAME', value=branch_name or '')]
    env = [
        client.V1EnvVar(name='REPO_URL', value=repo_url),
        client.V1EnvVar(name='TASK_B64', value=task_b64),
        client.V1EnvVar(name='RUN_MODE', value=action),
        client.V1EnvVar(name='REQUEST_ID', value=request_id),
        client.V1EnvVar(name='COPILOT_GITHUB_TOKEN', value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name='copilot-auth', key='COPILOT_GITHUB_TOKEN'))),
        client.V1EnvVar(name='REPO_TOKEN', value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name='repo-access', key='REPO_TOKEN', optional=True))),
    ] + patch_env
    if mode == 'apply':
        env.append(client.V1EnvVar(name='WRITE_TOKEN', value_from=client.V1EnvVarSource(secret_key_ref=client.V1SecretKeySelector(name='repo-write', key='WRITE_TOKEN'))))
    container = client.V1Container(
        name='runner', image=RUNNER_IMAGE, image_pull_policy='IfNotPresent', env=env,
        command=['/bin/bash', '/opt/runner/run.sh'],
        resources=client.V1ResourceRequirements(requests={'cpu':'250m','memory':'512Mi'}, limits={'cpu':'1','memory':'2Gi'}),
        security_context=client.V1SecurityContext(allow_privilege_escalation=False, run_as_non_root=True, run_as_user=1000, capabilities=client.V1Capabilities(drop=['ALL']), seccomp_profile=client.V1SeccompProfile(type='RuntimeDefault'))
    )
    pod = client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels={'app':'copilot-runner','request-id':request_id}), spec=client.V1PodSpec(restart_policy='Never', automount_service_account_token=False, containers=[container], dns_config=client.V1PodDNSConfig(options=[client.V1PodDNSConfigOption(name='ndots', value='1')])))
    return client.V1Job(metadata=client.V1ObjectMeta(name=job_name, labels={'app':'copilot-runner','request-id':request_id}), spec=client.V1JobSpec(backoff_limit=0, active_deadline_seconds=900, ttl_seconds_after_finished=3600, template=pod))

@app.get('/api/health')
def health():
    return {'status':'ok'}

@app.get('/api/config')
def get_config():
    return {'allowed_git_hosts': sorted(ALLOWED_HOSTS), 'namespace': NAMESPACE, 'agents': list(AGENTS), 'skills': list(SKILLS)}

@app.get('/api/requests')
def list_requests():
    with db() as c:
        rows = c.execute('SELECT id, repo_url, task, mode, status, job_name, created_at, report, base_sha, pr_url, error, conversation_id, parent_id, branch_name, agent, skills FROM requests ORDER BY created_at DESC').fetchall()
    return [dict(r) for r in rows]

@app.post('/api/requests')
def create_request(req: CreateRequest):
    repo = validate_repo(req.repo_url)
    return launch_request(repo, req.task, req.mode, agent=req.agent, skills=req.skills)


def launch_request(repo, task, mode, conversation_id=None, parent_id=None, agent='general', skills=None, context='', branch_name=None, patch_b64=None, base_sha=None):
    request_id = uuid.uuid4().hex[:12]
    job_name = f'copilot-{request_id}' if mode != 'apply' else f'copilot-apply-{request_id}'
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if conversation_id and c.execute("SELECT id FROM requests WHERE conversation_id=? AND status='running'", (conversation_id,)).fetchone():
            raise HTTPException(409, 'A conversation job is already running.')
        if mode == 'apply' and c.execute("SELECT id FROM requests WHERE parent_id=? AND mode='apply' AND status!='failed'", (parent_id,)).fetchone():
            raise HTTPException(409, 'This proposal already has an application attempt.')
        spec = job_spec(job_name, repo, task, mode, request_id, patch_b64=patch_b64, base_sha=base_sha, branch_name=branch_name, agent=agent, skills=skills, context=context)
        try:
            batch.create_namespaced_job(namespace=NAMESPACE, body=spec)
        except Exception:
            raise HTTPException(503, 'Could not create Kubernetes Job. Check the cluster and try again.')
        c.execute('INSERT INTO requests(id,repo_url,task,mode,status,job_name,created_at,conversation_id,parent_id,agent,skills,branch_name,patch_b64,base_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (request_id,repo,task,mode,'running',job_name,now,conversation_id or request_id,parent_id,agent,json.dumps(skills or []),branch_name,patch_b64,base_sha))
    return {'id':request_id,'job_name':job_name,'status':'running','conversation_id':conversation_id or request_id}


@app.get('/api/requests/{request_id}/conversation')
def get_conversation(request_id: str):
    with db() as c:
        current = c.execute('SELECT conversation_id FROM requests WHERE id=?', (request_id,)).fetchone()
        if not current:
            raise HTTPException(404, 'Request not found')
        turns = c.execute('SELECT * FROM requests WHERE conversation_id=? ORDER BY created_at, id', (current['conversation_id'],)).fetchall()
    return [dict(turn) for turn in turns]


@app.post('/api/requests/{request_id}/followup')
def followup(request_id: str, req: FollowupRequest):
    current = get_request(request_id)
    if current['status'] != 'complete':
        raise HTTPException(409, 'Wait for a completed response before continuing the conversation.')
    turns = get_conversation(request_id)
    if any(turn['status'] == 'running' for turn in turns):
        raise HTTPException(409, 'A conversation job is already running.')
    context = '\n\n'.join(f"User ({turn['mode']}): {turn['task'][:4000]}\nCopilot: {(turn['report'] or '')[:8000]}" for turn in turns[-6:])
    if len(turns) > 6:
        context = '[Only the six most recent turns are included.]\n' + context
    return launch_request(current['repo_url'], req.task, req.mode, conversation_id=current['conversation_id'], parent_id=request_id, agent=req.agent, skills=req.skills, context=context)

@app.get('/api/requests/{request_id}')
def get_request(request_id: str):
    with db() as c:
        row = c.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
    if not row: raise HTTPException(404, 'Request not found')
    result = dict(row)
    try:
        job = batch.read_namespaced_job(name=result['job_name'], namespace=NAMESPACE)
        if job.status.succeeded:
            result['status'] = 'complete'
        elif job.status.failed:
            result['status'] = 'failed'
        else:
            result['status'] = 'running'
        pods = core.list_namespaced_pod(namespace=NAMESPACE, label_selector=f'request-id={request_id}').items
        logs = core.read_namespaced_pod_log(name=pods[0].metadata.name, namespace=NAMESPACE) if pods else ''
        result['error'] = logs[-12000:] if result['status'] == 'failed' else None
        report = extract(logs, 'REPORT_BEGIN', 'REPORT_END')
        patch = extract(logs, 'PATCH_B64_BEGIN', 'PATCH_B64_END')
        sha = extract(logs, 'BASE_SHA_BEGIN', 'BASE_SHA_END')
        pr = extract(logs, 'PR_URL_BEGIN', 'PR_URL_END')
        if report is not None: result['report'] = report.strip()
        if patch is not None and patch.strip(): result['patch_b64'] = patch.strip()
        if sha is not None: result['base_sha'] = sha.strip()
        if pr is not None: result['pr_url'] = pr.strip()
        with db() as c:
            c.execute('UPDATE requests SET status=?, report=COALESCE(?,report), patch_b64=COALESCE(?,patch_b64), base_sha=COALESCE(?,base_sha), pr_url=COALESCE(?,pr_url), error=? WHERE id=?', (result['status'], result.get('report'), result.get('patch_b64'), result.get('base_sha'), result.get('pr_url'), result.get('error'), request_id))
    except Exception as e:
        result['error'] = str(e)
    result.pop('COPILOT_GITHUB_TOKEN', None)
    return result

@app.delete('/api/requests/{request_id}')
def delete_request(request_id: str):
    with db() as c:
        row = c.execute('SELECT job_name FROM requests WHERE id=?', (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Request not found')
    try:
        job = batch.read_namespaced_job(name=row['job_name'], namespace=NAMESPACE)
    except client.exceptions.ApiException as error:
        if error.status != 404:
            raise HTTPException(503, 'Could not verify job status. Try again later.')
    except Exception:
        raise HTTPException(503, 'Could not verify job status. Try again later.')
    else:
        if not (job.status.succeeded or job.status.failed):
            raise HTTPException(409, 'Cannot delete a request while its job is active.')
    with db() as c:
        c.execute('DELETE FROM requests WHERE id=?', (request_id,))
    return {'id': request_id, 'deleted': True}

def extract(text, start, end):
    a, b = f'=== {start} ===', f'=== {end} ==='
    if a not in text or b not in text: return None
    return text.split(a,1)[1].split(b,1)[0]

@app.post('/api/requests/{request_id}/approve')
def approve(request_id: str, req: ApproveRequest):
    branch_name = validate_branch(req.branch_name)
    with db() as c:
        row = c.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
    if not row: raise HTTPException(404, 'Request not found')
    if row['mode'] != 'propose': raise HTTPException(400, 'Only remediation proposals can be approved')
    # Refresh status and capture logs first.
    current = get_request(request_id)
    patch_b64 = current.get('patch_b64')
    base_sha = current.get('base_sha')
    if current.get('status') != 'complete' or not patch_b64 or not current.get('base_sha'):
        raise HTTPException(409, 'Proposal must complete and produce a patch before approval')
    # Store approver in the task metadata for the audit trail.
    if any(turn['mode'] == 'apply' and turn['parent_id'] == request_id and turn['status'] != 'failed' for turn in get_conversation(request_id)):
        raise HTTPException(409, 'This proposal already has an application attempt. Generate a new proposal before approving again.')
    apply_task = f'Approved by: {req.approved_by}. Apply only the reviewed patch and create a pull request.'
    result = launch_request(row['repo_url'], apply_task, 'apply', conversation_id=current['conversation_id'], parent_id=request_id, branch_name=branch_name, patch_b64=patch_b64, base_sha=base_sha)
    return {**result, 'approved_by': req.approved_by, 'branch_name': branch_name}

app.mount('/', StaticFiles(directory='/app/frontend', html=True), name='frontend')
