"""One immutable, independently funded upgrade campaign for all live entry points."""
import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIVE = ROOT / '.local/active-campaign.json'


def _verify_authorization(connection, authorization_id, expected, new, error, require_history=False):
    auth = connection.execute('SELECT receipt_json FROM budget_authorizations WHERE authorization_id=?',
                              (authorization_id,)).fetchone()
    try:
        receipt = json.loads(auth[0]) if auth else {}
    except (TypeError, json.JSONDecodeError):
        raise ValueError(error) from None
    if not isinstance(receipt, dict):
        raise ValueError(error)
    before, after = receipt.get('before', {}), receipt.get('after', {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError(error)
    held = before.get('held_micros')
    if (type(receipt.get('expected_limit_micros')) is not int
            or receipt['expected_limit_micros'] != expected
            or type(receipt.get('new_limit_micros')) is not int
            or receipt['new_limit_micros'] != new
            or type(held) is not int or held < 0
            or type(after.get('held_micros')) is not int or after['held_micros'] != held):
        raise ValueError(error)
    # Legacy 50 -> 70 receipts may only contain held preservation. Any supplied
    # SHA must agree; subsequent extensions require historical SHA evidence.
    if require_history or 'history_sha256' in before or 'history_sha256' in after:
        history_sha = before.get('history_sha256')
        if type(history_sha) is not str or not history_sha or after.get('history_sha256') != history_sha:
            raise ValueError(error)


def verify_ledger(ledger, expected_limit=None):
    ledger = Path(ledger)
    if not ledger.is_file() or ledger.is_symlink() or ledger.stat().st_size == 0:
        raise ValueError('本次费用账本缺失或为空；请恢复原账，不能重置费用。')
    try:
        with sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro', uri=True) as connection:
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('费用账本完整性检查失败，请恢复原账。')
            limit = connection.execute('SELECT limit_micros FROM budget_limit WHERE id=1').fetchone()
            if not limit or limit[0] not in (50_000_000, 70_000_000, 80_000_000, 130_000_000) or (expected_limit is not None and limit[0] != expected_limit):
                raise ValueError('本次费用上限记录异常，请恢复原账。')
            if limit[0] >= 70_000_000:
                _verify_authorization(connection, 'packing-six-upgrades-20261003-user70',
                                      50_000_000, 70_000_000,
                                      '缺少本次追加20元的完整授权记录，请恢复原账。')
            if limit[0] >= 80_000_000:
                _verify_authorization(connection, 'packing-continuation-20261004-user80',
                                      70_000_000, 80_000_000,
                                      '缺少本次追加10元且保留原费用的完整授权记录，请恢复原账。',
                                      require_history=True)
            if limit[0] == 130_000_000:
                _verify_authorization(connection, 'packing-completion-20261005-user130',
                                      80_000_000, 130_000_000,
                                      '缺少本次追加50元且保留原费用的完整授权记录，请恢复原账。',
                                      require_history=True)
            connection.execute('SELECT attempt_id,reserved_micros,held_micros,status,created_at,updated_at FROM budget_reservations LIMIT 1').fetchall()
            stage=connection.execute('SELECT ceiling_micros FROM campaign_stage WHERE id=1').fetchone()
            if not stage or not 0 < stage[0] <= limit[0]:
                raise ValueError('费用阶段记录异常，请恢复原账。')
    except sqlite3.DatabaseError:
        raise ValueError('费用账本损坏或缺少原有记录，请恢复原账；不会自动新建。') from None


def active_campaign():
    if not ACTIVE.exists():
        directory = ROOT / '.local/upgrade-20261003'
        if any((directory/name).exists() for name in ('funding.json','model-budget.sqlite3','trial')):
            raise ValueError('本次费用入口记录缺失，请恢复原配置；不能绕过或重新建立账本。')
        return None
    value = json.loads(ACTIVE.read_text())
    directory = (ROOT / value['directory']).resolve()
    if (value.get('id') != 'upgrade-20261003' or value.get('limit_cny') not in (50, 70, 80, 130)
            or directory != (ROOT / '.local/upgrade-20261003').resolve()):
        raise ValueError('本次费用配置不一致，已停止模型调用。')
    ledger = directory / 'model-budget.sqlite3'
    verify_ledger(ledger, int(value['limit_cny'] * 1_000_000))
    return {**value, 'path': directory, 'ledger': ledger}


def budget_environment():
    campaign = active_campaign()
    if not campaign:
        return {}
    return {'MODEL_BUDGET_LEDGER': str(campaign['ledger']), 'MODEL_BUDGET_CNY': str(campaign['limit_cny'])}


def configure_environment():
    values = budget_environment()
    if values:
        os.environ.update(values)
        # Current provider's conservative peak prices are verified in model_usage.
        os.environ.pop('MODEL_BUDGET_INPUT_CNY_PER_M', None)
        os.environ.pop('MODEL_BUDGET_OUTPUT_CNY_PER_M', None)
    return values


def run_directory(root=None):
    if root is not None and Path(root).resolve() != ROOT.resolve():
        return Path(root) / '.local/upgrade-v3'
    campaign = active_campaign()
    return campaign['path'] if campaign else ROOT / '.local/upgrade-v3'


def ledger_path(root=None):
    return run_directory(root) / 'model-budget.sqlite3'
