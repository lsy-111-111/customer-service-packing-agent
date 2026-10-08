"""Local release fingerprints. No database, credentials or provider calls."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_ID = 'daily-good-store'
API_CONTRACT_VERSION = 3


def verify_release_health(health, source_sha, frontend_build_id=None):
    """Bind a running backend's identity, contract and served build to a release."""
    expected = {'app_id': APP_ID, 'api_contract_version': API_CONTRACT_VERSION,
                'source_sha256': source_sha}
    if frontend_build_id is not None:
        expected['frontend_build_id'] = frontend_build_id
    for key, value in expected.items():
        if health.get(key) != value:
            raise ValueError('Backend release health mismatch: '+key)
    build = health.get('frontend_build_id')
    if not isinstance(build, str) or re.fullmatch(r'[0-9a-f]{64}', build) is None:
        raise ValueError('Backend release health missing or invalid: frontend_build_id')
    return {key:health[key] for key in ('app_id','api_contract_version','source_sha256','frontend_build_id')}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_snapshot(root=ROOT):
    root = Path(root)
    paths = set((root / 'backend/app').glob('*.py'))
    paths.update(p for p in (root / 'backend/app/assets').rglob('*') if p.is_file())
    for suffix in ('*.ts', '*.tsx', '*.css'):
        paths.update((root / 'frontend/src').rglob(suffix))
    paths.update(p for p in (root / 'frontend/public').rglob('*') if p.is_file())
    for name in ('scripts/start.sh', 'scripts/serve.py', 'scripts/build_release.py',
                 'scripts/run_acceptance_suite.py', 'scripts/publish_acceptance.py',
                 'scripts/start_upgrade.py', 'scripts/prepare_upgrade.py', 'scripts/probe_model.py',
                 'scripts/campaign_stage.py', '启动客服试用版.command',
                 'backend/requirements.txt', 'frontend/package.json',
                 'frontend/pnpm-lock.yaml', 'frontend/package-lock.json', 'frontend/vite.config.ts',
                 'frontend/tsconfig.json', 'frontend/index.html', '启动日常好物.command'):
        if (root / name).is_file():
            paths.add(root / name)
    files = {p.relative_to(root).as_posix(): digest(p) for p in sorted(paths)}
    return {'files': files, 'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def artifact_files(directory):
    directory = Path(directory)
    return {p.relative_to(directory).as_posix(): digest(p)
            for p in sorted(directory.rglob('*')) if p.is_file() and p.name != 'release.json'}


def verify_frontend(directory, source_sha=None):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'release.json').read_text())
    if manifest.get('app_id') != APP_ID or manifest.get('api_contract_version') != API_CONTRACT_VERSION:
        raise ValueError('前端接口版本不匹配，请重新构建并验证。')
    if not (directory / 'index.html').is_file() or artifact_files(directory) != manifest.get('files'):
        raise ValueError('前端构建文件已变化，请重新构建并验证。')
    if source_sha is not None and manifest.get('source_sha256') != source_sha:
        raise ValueError('代码与前端不是同一版本，请使用匹配的构建。')
    expected = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()
    if manifest.get('build_id') != expected:
        raise ValueError('前端构建指纹不匹配。')
    return manifest


def accepted_frontend(root=ROOT):
    root = Path(root).resolve()
    approval = json.loads((root / '.local/releases/accepted.json').read_text())
    directory = Path(approval['frontend_dir']).resolve()
    if not directory.is_relative_to(root / '.local/releases') or approval.get('accepted') is not True:
        raise ValueError('未找到已验收的本地版本。')
    manifest = verify_frontend(directory, source_snapshot(root)['sha256'])
    if approval.get('build_id') != manifest['build_id'] or approval.get('source_sha256') != manifest['source_sha256']:
        raise ValueError('验收记录与当前版本不匹配。')
    if approval.get('schema_version') == 2:
        from .acceptance_release import read_proof, verify_manifest
        evidence = read_proof(root, approval.get('acceptance_evidence'))
        verify_manifest(root, evidence, manifest['source_sha256'], manifest['build_id'])
    return directory
