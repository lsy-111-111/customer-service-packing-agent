#!/usr/bin/env python3
"""Portable, local-only entry point for the public source distribution."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / '.venv'
PYTHON = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
LOCAL = ROOT / '.local'
RUNTIME = LOCAL / 'public-runtime'
BUILD_RECORD = LOCAL / 'public-build.json'
ALLOWED_CONFIG = {
    'AGENT_ENABLED', 'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL',
    'DEEPSEEK_MODEL', 'MODEL_TIMEOUT', 'MODEL_BUDGET_CNY',
    'MODEL_BUDGET_INPUT_CNY_PER_M', 'MODEL_BUDGET_OUTPUT_CNY_PER_M',
}


def safe_path(path):
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError('运行目录或配置文件不能链接到项目外。')
    return resolved


def check_workspace():
    for path in (VENV, LOCAL, RUNTIME, ROOT / '.env', BUILD_RECORD):
        safe_path(path)
    if (LOCAL / 'active-campaign.json').exists() or (LOCAL / 'upgrade-20261003').exists():
        raise ValueError('检测到历史运行配置，请使用独立的公开版目录。')
    if (ROOT / 'api密钥.rtfd').exists():
        raise ValueError('公开版目录不应包含历史密钥文件。')


def clean_environment():
    # Explicit .env configuration only: do not inherit another project's AI or DB.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(('APP_', 'DEEPSEEK_', 'MODEL_', 'AGENT_'))
        and key not in ('PYTHONPATH', 'PYTHONHOME')
    }
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    return environment


def run(command, *, cwd=ROOT, capture=False):
    return subprocess.run(command, cwd=cwd, env=clean_environment(), check=True,
                          text=True, capture_output=capture)


def tool(name):
    executable = shutil.which(name)
    if not executable:
        raise ValueError(f'未找到 {name}，请按 README 安装并重新打开终端。')
    # npm's Windows global shims are batch files, not native executables.
    if os.name == 'nt' and Path(executable).suffix.lower() in ('.cmd', '.bat'):
        return [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', executable]
    return [executable]


def check_tools():
    node = tool('node')
    version = run(node + ['--version'], capture=True).stdout.strip()
    match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (22, 13, 0):
        raise ValueError('需要 Node.js 22.13 或更新版本，推荐 Node.js 24 LTS。')
    pnpm = tool('pnpm')
    version = run(pnpm + ['--version'], capture=True).stdout.strip()
    if not re.match(r'^10\.', version):
        raise ValueError('需要 pnpm 10；请运行 npm install -g pnpm@10。')
    return node, pnpm


def use_virtual_environment(create=False):
    if not PYTHON.is_file():
        if not create:
            raise ValueError('尚未准备环境，请先运行 python scripts/run_public.py setup。')
        print('正在创建本项目的 Python 虚拟环境……', flush=True)
        run([sys.executable, '-m', 'venv', str(VENV)])
    if Path(sys.prefix).resolve() != VENV.resolve():
        environment = clean_environment()
        os.execve(str(PYTHON), [str(PYTHON), '-B', str(Path(__file__).resolve()),
                              *sys.argv[1:]], environment)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def build_frontend(node):
    # Generated PDF assets must exist before taking the source fingerprint.
    run(node + [str(ROOT / 'frontend/scripts/prepare-pdf-assets.mjs')])
    from backend.app.release import APP_ID, API_CONTRACT_VERSION, source_snapshot, artifact_files, verify_frontend
    source = source_snapshot()
    releases = safe_path(LOCAL / 'releases')
    releases.mkdir(parents=True, exist_ok=True)
    destination = safe_path(releases / source['sha256'] / 'frontend')
    if destination.exists():
        manifest = verify_frontend(destination, source['sha256'])
    else:
        temporary = Path(tempfile.mkdtemp(prefix='build-', dir=releases))
        try:
            run(node + [str(ROOT / 'frontend/node_modules/typescript/bin/tsc'), '-b'], cwd=ROOT / 'frontend')
            run(node + [str(ROOT / 'frontend/node_modules/vite/bin/vite.js'), 'build',
                        '--outDir', str(temporary / 'frontend')], cwd=ROOT / 'frontend')
            if source_snapshot() != source:
                raise ValueError('构建期间源文件发生变化，请重新运行 setup。')
            files = artifact_files(temporary / 'frontend')
            manifest = {'app_id': APP_ID, 'api_contract_version': API_CONTRACT_VERSION,
                        'source_sha256': source['sha256'], 'files': files,
                        'build_id': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
            save_json(temporary / 'frontend/release.json', manifest)
            temporary.rename(destination.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        verify_frontend(destination, source['sha256'])
    save_json(BUILD_RECORD, {'frontend_directory': destination.relative_to(ROOT).as_posix(),
                            'source_sha256': source['sha256'], 'build_id': manifest['build_id']})


def setup():
    node, pnpm = check_tools()
    use_virtual_environment(create=True)
    print('正在安装 Python 依赖……', flush=True)
    run([str(PYTHON), '-m', 'pip', 'install', '-r', str(ROOT / 'backend/requirements.txt')])
    print('正在按锁文件安装前端依赖……', flush=True)
    run(pnpm + ['install', '--frozen-lockfile'], cwd=ROOT / 'frontend')
    build_frontend(node)
    print('准备完成。运行 python scripts/run_public.py start；AI 默认关闭。', flush=True)


def runtime_environment():
    from dotenv import dotenv_values
    config = {key: value for key, value in dotenv_values(ROOT / '.env', interpolate=False).items()
              if value is not None}
    if set(config) - ALLOWED_CONFIG:
        raise ValueError('.env 包含不支持的配置项；请参照 .env.example，不要复制其他项目的运行配置。')
    flag = config.get('AGENT_ENABLED', '0').strip().lower()
    if flag not in ('0', 'false', 'off', '1', 'true', 'on'):
        raise ValueError('AGENT_ENABLED 请填写 0 或 1。')
    enabled = flag in ('1', 'true', 'on')
    environment = clean_environment()
    environment.update({key: value.strip() for key, value in config.items() if value.strip()})
    environment['AGENT_ENABLED'] = '1' if enabled else '0'
    environment['DEEPSEEK_API_KEY'] = config.get('DEEPSEEK_API_KEY', '').strip() if enabled else ''
    environment['APP_DB_PATH'] = str(safe_path(RUNTIME / 'shop.sqlite3'))
    environment['APP_UPLOAD_DIR'] = str(safe_path(RUNTIME / 'uploads'))
    ledger = safe_path(RUNTIME / 'model-budget.sqlite3')
    if enabled:
        if not environment['DEEPSEEK_API_KEY'] or environment['DEEPSEEK_API_KEY'].lower().startswith(('your_', 'your-', 'replace')):
            raise ValueError('启用 AI 前，请在 .env 填写你自己的 DEEPSEEK_API_KEY。')
        try:
            limit = Decimal(config.get('MODEL_BUDGET_CNY', ''))
            if not limit.is_finite() or limit <= 0 or limit * 1_000_000 != (limit * 1_000_000).to_integral_value():
                raise InvalidOperation
        except InvalidOperation:
            raise ValueError('启用 AI 前，请填写正数 MODEL_BUDGET_CNY，最多六位小数。') from None
        environment['MODEL_BUDGET_CNY'] = str(limit)
        environment['MODEL_BUDGET_LEDGER'] = str(ledger)
        if ledger.exists():
            import sqlite3
            with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as connection:
                previous = connection.execute('SELECT limit_micros FROM budget_limit WHERE id=1').fetchone()
            if previous and previous[0] != int(limit * 1_000_000):
                raise ValueError('费用上限与已有公开版账本不一致；请保留原上限和用量，不要删除账本。')
    else:
        environment.pop('MODEL_BUDGET_CNY', None)
        environment.pop('MODEL_BUDGET_LEDGER', None)
    return environment


def start(port):
    use_virtual_environment()
    if not BUILD_RECORD.is_file():
        raise ValueError('前端尚未准备完成，请先运行 python scripts/run_public.py setup。')
    from backend.app.release import source_snapshot, verify_frontend
    record = json.loads(BUILD_RECORD.read_text(encoding='utf-8'))
    frontend = safe_path(ROOT / record['frontend_directory'])
    if not frontend.is_relative_to((LOCAL / 'releases').resolve()):
        raise ValueError('公开版构建目录无效，请重新运行 setup。')
    manifest = verify_frontend(frontend, source_snapshot()['sha256'])
    if any(record[key] != manifest[key] for key in ('source_sha256', 'build_id')):
        raise ValueError('构建记录不匹配，请重新运行 setup。')
    environment = runtime_environment()
    environment['APP_FRONTEND_DIST'] = str(frontend)
    os.environ.clear()
    os.environ.update(environment)
    # Reserve the socket before application import/DB initialization; never replace a service.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            # Reuse sockets in TIME_WAIT after our own graceful shutdown.
            # SO_REUSEPORT is intentionally absent: an active listener still wins.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('127.0.0.1', port))
        except OSError:
            raise ValueError(f'端口 {port} 无法使用，请选择其他端口；未停止任何已有进程。') from None
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        import uvicorn
        ai_status = '已启用，按你配置的费用上限计费' if environment['AGENT_ENABLED'] == '1' else '关闭'
        print(f'客服与箱唛公开版：http://127.0.0.1:{port}\nAI：{ai_status}\n按 Ctrl+C 正常停止；数据保存在本项目 .local/public-runtime。', flush=True)
        server = uvicorn.Server(uvicorn.Config('backend.app.main:app', host='127.0.0.1',
                                               port=port, log_level='info'))
        server.run(sockets=[listener])
        if not server.started:
            raise ValueError('服务未成功启动，请检查上方错误。')


def main():
    parser = argparse.ArgumentParser(description='公开版环境准备与本机启动（默认关闭 AI）')
    subparsers = parser.add_subparsers(dest='command', required=True)
    subparsers.add_parser('setup', help='安装依赖并构建前端，需要联网')
    launch = subparsers.add_parser('start', help='启动本机服务，不自动打开浏览器')
    launch.add_argument('--port', type=int, default=8020)
    args = parser.parse_args()
    try:
        if sys.version_info[:2] not in ((3, 11), (3, 12)):
            raise ValueError('请使用 Python 3.11 或 3.12 运行此脚本。')
        check_workspace()
        os.chdir(ROOT)
        sys.path.insert(0, str(ROOT))
        if args.command == 'setup':
            setup()
        else:
            if not 1024 <= args.port <= 65535 or args.port in (8001, 8017, 8018, 8019):
                raise ValueError('请使用 1024–65535 的空闲端口，并避开保留端口 8001、8017、8018、8019。')
            start(args.port)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(f'准备步骤失败（退出码 {error.returncode}），请修复上方错误后重试。', file=sys.stderr)
        elif isinstance(error, (KeyError, OSError)):
            print('文件、环境或配置无法读取；请检查路径、权限和依赖后重试。', file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
