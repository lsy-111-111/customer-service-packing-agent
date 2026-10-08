"""Validate a complete local campaign manifest before making it startable."""
import hashlib
import json
from pathlib import Path
from .acceptance_policy import COMPLETION, PLANNED, suite_rules, requirement_business_pass, verify as verify_policy

EXPECTED = dict(PLANNED)
REQUIRED_CHECKS = {'free_regression','rule_matrix303','changed_ui','migration_restore','packing_files'}
QUESTION_FILES = {'new100':'evals/service_v3_scenarios.json',
                  'holdout20':'evals/service_v3_holdout20_r1.json', 'original40':'evals/scenarios.json',
                  'conversations':'evals/upgrade_conversations.json', 'assessments':'evals/upgrade_scenarios_v2.json',
                  'images':'evals/upgrade_image_conversations.json',
                  'after_sale_chat':'evals/upgrade_after_sale_conversations.json',
                  'draft':'evals/upgrade_draft_correction.json',
                  'packing30':'docs/packing-acceptance/fixtures/manifest.json'}
MANIFEST_FILES = {'new100':'evals/service_v3_manifest.json',
                  'holdout20':'evals/service_v3_holdout20_r1_manifest.json',
                  'conversations':'evals/upgrade_gold_manifest_v2.json',
                  'assessments':'evals/upgrade_gold_manifest_v2.json',
                  'images':'evals/upgrade_image_manifest.json',
                  'after_sale_chat':'evals/upgrade_after_sale_chat_manifest.json',
                  'draft':'evals/upgrade_draft_correction_manifest.json',
                  'packing30':'docs/packing-acceptance/fixtures-sha256.json'}
SEMANTIC_GROUPS = ('mixed_intents','long_memory','dynamic_policy','human_loop','image_and_errors')


def read_proof(root, proof):
    if not isinstance(proof, dict) or not isinstance(proof.get('path'), str) or not proof['path']:
        raise ValueError('Missing acceptance evidence reference')
    path = (root / proof['path']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Acceptance evidence is missing or outside this project')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != proof.get('sha256'):
        raise ValueError('Acceptance evidence changed after review')
    return json.loads(content)


def raw_cases(raw, name, planned, *, allow_critical=False):
    """Count observed original rows, independently of the seal's declarations."""
    if raw.get('is_subset') or raw.get('interrupted') or raw.get('run_status', 'completed') != 'completed':
        raise ValueError('Raw suite is a subset or interrupted: '+name)
    if name == 'draft':
        identity = raw.get('scenario', {}).get('id')
        if (raw.get('planned') != 1 or raw.get('completed') != 1
                or not raw.get('finished_at') or not identity):
            raise ValueError('Raw draft scenario is incomplete')
        rows = [{**raw, 'id':identity}]
        ids = [identity]
    else:
        rows = raw.get('results', [])
        ids = ([row.get('id') for row in rows] if name == 'packing30' else raw.get('scenario_ids', []))
        if name == 'packing30':
            if raw.get('planned_packages') != 30 or raw.get('all_complete') is not True:
                raise ValueError('Raw packing30 is incomplete')
        elif not raw.get('finished_at'):
            raise ValueError('Raw suite has not finished: '+name)
    if (not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids)
            or len(ids) != planned or len(set(ids)) != planned or len(rows) != planned
            or len({row.get('id') for row in rows}) != planned
            or {row.get('id') for row in rows} != set(ids)):
        raise ValueError('Raw planned and observed scenario identities are incomplete: '+name)
    if not allow_critical and any(row.get('hard_failures') for row in rows):
        raise ValueError('Raw report contains a critical failure: '+name)
    return {row['id']: row for row in rows}


def independent_cases(root, review, raw_sha):
    """Load the actual per-case review, including the native upgrade sidecar."""
    independent = review.get('independent_review', {})
    if 'reviews' in independent:
        document = independent
    elif independent.get('file') and independent.get('sha256'):
        document = read_proof(root, {'path':independent['file'], 'sha256':independent['sha256']})
    else:
        document = review
    if document.get('source_report_sha256') != raw_sha or not isinstance(document.get('reviews'), dict):
        raise ValueError('Independent per-case review is missing or not bound to the original report')
    return document['reviews']


def verify_frozen_inputs(root, freeze):
    """Verify frozen bytes only; this never parses independent holdout questions."""
    root = Path(root).resolve()
    inputs = freeze.get('source', {}).get('evaluation_inputs_sha256')
    required = set(QUESTION_FILES.values()) | set(MANIFEST_FILES.values())
    if not isinstance(inputs, dict) or not required <= set(inputs):
        raise ValueError('Final freeze lacks complete question and manifest byte hashes')
    for relative, expected in inputs.items():
        path = (root / relative).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
            raise ValueError('Question, manifest or fixture bytes differ from final freeze')


def _automatic_pass(row, name):
    if name == 'packing30':return row.get('passed') is True
    if name == 'draft' and 'automatic_passed' in row:
        return row['automatic_passed'] is True and row.get('status', 'passed') == 'passed'
    return row.get('status') == 'passed'


def _verify_candidate_binding(root, name, raw, binding, policy, source_sha, build_id, runtime, freeze):
    source = freeze['source']
    if (binding.get('source') != source or binding.get('source_after') != source
            or binding.get('acceptance_policy') != policy
            or binding.get('model_runtime_frozen') != runtime
            or binding.get('model_runtime_expected') != runtime
            or binding.get('model_runtime_after') != runtime
            or binding.get('frontend_manifest', {}).get('build_id') != build_id
            or binding.get('frontend_manifest', {}).get('source_sha256') != source_sha):
        raise ValueError('Suite source, policy, runtime or frontend differs from final candidate: '+name)
    if name != 'packing30':
        if binding.get('backend_release_unchanged') is not True:
            raise ValueError('Suite backend release identity was not preserved: '+name)
        for health in (binding.get('health', {}), binding.get('backend_release_after', {})):
            if health.get('source_sha256') != source_sha or health.get('frontend_build_id') != build_id:
                raise ValueError('Suite backend served another final candidate: '+name)
    raw_source = raw.get('source_before', raw.get('source'))
    if raw_source is not None:
        if (not isinstance(raw_source, dict) or raw_source.get('sha256') != source_sha
                or raw_source.get('evaluation_inputs_sha256') != source.get('evaluation_inputs_sha256')):
            raise ValueError('Original report belongs to another source or question freeze: '+name)
    if raw.get('source_sha256', source_sha) != source_sha:
        raise ValueError('Original report belongs to another source: '+name)
    if raw.get('acceptance_policy', policy) != policy:
        raise ValueError('Original report uses another acceptance policy: '+name)
    embedded = raw.get('freeze')
    if embedded is not None and any(embedded.get(key) != freeze.get(key) for key in ('source','model_runtime','acceptance_policy','budget_limit_micros')):
        raise ValueError('Original report has another candidate freeze: '+name)
    if name in ('new100','holdout20'):
        if embedded is None or embedded.get('suite', 'service_v3') != ('holdout20' if name == 'holdout20' else 'service_v3'):
            raise ValueError('Semantic report lacks its suite freeze: '+name)
        if name == 'holdout20' and not embedded.get('parent_freeze_sha256'):
            raise ValueError('Holdout report lacks its main-freeze linkage')
    inputs = source['evaluation_inputs_sha256']
    expected_gold = inputs[QUESTION_FILES[name]]
    actual_gold = (raw.get('manifest_sha256') if name == 'packing30' else raw.get('scenario_spec_sha256') if name == 'original40'
                   else raw.get('gold_sha256', {}).get(name) if isinstance(raw.get('gold_sha256'), dict) else raw.get('gold_sha256'))
    if actual_gold != expected_gold:
        raise ValueError('Original report uses another frozen question set: '+name)
    if name in ('new100','holdout20') and raw.get('manifest_sha256', embedded.get('manifest_sha256')) != inputs[MANIFEST_FILES[name]]:
        raise ValueError('Original semantic report uses another frozen manifest: '+name)


def _pointer_exists(result, pointers):
    if not isinstance(pointers, list) or not pointers:return False
    for pointer in pointers:
        if not isinstance(pointer, str) or not pointer.startswith('/'):return False
        parts = [part.replace('~1','/').replace('~0','~') for part in pointer[1:].split('/')]
        if parts[0] not in {'steps','snapshot','http','provider_attempts','error','cleanup_error','telemetry_error'}:return False
        value = result
        try:
            for part in parts:value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, IndexError, ValueError, TypeError):return False
    return True


def _semantic_score(name, rows, reviews, summary, policy):
    """Recount actual independent atoms; a positive summary alone is insufficient."""
    rules = suite_rules(policy, name)
    totals = {'facts_correct':0,'facts_total':0,'intents_covered':0,'intents_total':0,'service_passed':0}
    passed = set()
    for identity, row in rows.items():
        item = reviews[identity]
        if any(item.get(key) is not True for key in ('reviewed','fact_inventory_complete','intent_inventory_complete')):
            raise ValueError('Independent semantic inventories are incomplete: '+identity)
        inventories = {}
        for key, decision in (('factual_assertions','correct'),('intent_coverage','covered'),('requirements','passed')):
            atoms = item.get(key)
            if not isinstance(atoms, list) or (not atoms and key != 'factual_assertions'):
                raise ValueError('Independent semantic atoms missing: '+identity)
            ids = [atom.get('id') for atom in atoms if isinstance(atom, dict)]
            if len(ids) != len(atoms) or len(set(ids)) != len(ids) or not all(isinstance(i,str) and i.strip() for i in ids):
                raise ValueError('Independent semantic atom identities invalid: '+identity)
            if any(type(atom.get(decision)) is not bool or not atom.get('text') or not atom.get('reason')
                   or not _pointer_exists(row, atom.get('evidence')) for atom in atoms):
                raise ValueError('Independent semantic atom verdict or evidence missing: '+identity)
            inventories[key] = atoms
        if not inventories['factual_assertions'] and not item.get('no_factual_assertions_reason'):
            raise ValueError('An empty factual inventory requires an independent reason')
        service = item.get('service_quality', {})
        if (type(service.get('passed')) is not bool or not service.get('reason')
                or not _pointer_exists(row, service.get('evidence'))):
            raise ValueError('Independent service evidence is incomplete: '+identity)
        blockers = (row.get('status') != 'automatic_pass' or any(row.get(k) for k in ('error','cleanup_error','telemetry_error'))
                    or any(check.get('passed') is not True for check in row.get('checks', []))
                    or any(attempt.get('status') in ('blocked_budget','blocked_configuration')
                           or 'timeout' in str(attempt.get('error_type','')).lower() for attempt in row.get('provider_attempts', [])))
        facts = inventories['factual_assertions']; intents = inventories['intent_coverage']; requirements = inventories['requirements']
        correct = sum(atom['correct'] for atom in facts); covered = sum(atom['covered'] for atom in intents)
        totals['facts_correct'] += correct; totals['facts_total'] += len(facts)
        totals['intents_covered'] += covered; totals['intents_total'] += len(intents)
        totals['service_passed'] += int(service['passed'] and not blockers)
        polish = any(not atom['passed'] and requirement_business_pass(atom, rules) for atom in requirements)
        if polish and service['passed']:raise ValueError('Presentation defects cannot be hidden from the service score')
        verdict = (not blockers and not row.get('hard_failures') and not item.get('hard_failures')
                   and correct == len(facts) and covered == len(intents)
                   and (service['passed'] or not rules['service_required_for_business'])
                   and all(requirement_business_pass(atom,rules) for atom in requirements))
        if verdict:passed.add(identity)
        if summary.get('per_scenario', {}).get(identity, {}).get('passed') is not bool(verdict):
            raise ValueError('Semantic per-case score differs from actual independent atoms: '+identity)
    groups = {group:{'planned':sum(row.get('group') == group for row in rows.values()),
                     'passed':sum(rows[identity].get('group') == group for identity in passed)} for group in SEMANTIC_GROUPS}
    if summary.get('groups') != groups:raise ValueError('Semantic group score differs from observed cases')
    metrics = (('factual_correctness','facts_correct','facts_total',policy['facts_percent'],100),
               ('intent_coverage','intents_covered','intents_total',policy['intents_percent'],100),
               ('service_quality','service_passed',None,rules['service_minimum'],rules['planned']))
    metrics_ok = True
    for key, numerator, denominator, minimum, planned in metrics:
        n = totals[numerator]; d = len(rows) if denominator is None else totals[denominator]
        met = d > 0 and n * planned >= d * minimum
        observed = summary.get(key, {})
        if (observed.get('numerator') != n or observed.get('denominator') != d
                or observed.get('threshold') != minimum/planned or observed.get('threshold_met') is not met):
            raise ValueError('Semantic metric differs from actual independent atoms: '+key)
        metrics_ok = metrics_ok and met
    group_ok = (all(group['planned'] == 20 and group['passed'] >= rules['per_group_minimum'] for group in groups.values())
                if name == 'new100' else all(group['planned'] == 4 for group in groups.values()))
    long_ok = True
    if name == 'new100':
        counts = [sum(message.get('role') in ('user','assistant') for message in row.get('snapshot',{}).get('messages',[])) for row in rows.values()]
        count = sum(total > policy['long_messages_strictly_greater_than'] for total in counts)
        long_ok = count >= policy['long_dialogues_minimum']
        if summary.get('long_dialogue_evidence',{}).get('qualifying_scenarios') != count:
            raise ValueError('Long dialogue count differs from actual API snapshots')
    return len(passed), bool(metrics_ok and group_ok and long_ok)


def verify_suite(root, name, entry, policy, source_sha, build_id, model_runtime, freeze, *, allow_rejected=False):
    """Validate one complete suite and return its independently recounted verdict."""
    root = Path(root)
    policy = verify_policy(policy)
    if name not in EXPECTED:raise ValueError('Unknown acceptance suite')
    planned = EXPECTED[name]
    if (freeze.get('source', {}).get('sha256') != source_sha or freeze.get('acceptance_policy') != policy
            or not model_runtime or freeze.get('model_runtime') != model_runtime):
        raise ValueError('Suite candidate differs from its run freeze')
    if policy['id'] == COMPLETION:verify_frozen_inputs(root,freeze)
    raw = read_proof(root, entry.get('raw'))
    review = read_proof(root, entry.get('review'))
    receipt = read_proof(root, entry.get('receipt'))
    binding = read_proof(root, entry.get('binding'))
    rows = raw_cases(raw, name, planned, allow_critical=allow_rejected)
    reviews = independent_cases(root, review, entry['raw']['sha256'])
    if (set(reviews) != set(rows) or any(not isinstance(item, dict) or not item.get('reason')
                                      or not isinstance(item.get('hard_failures', []), list) for item in reviews.values())):
        raise ValueError('Independent review is incomplete or contains a critical failure: '+name)
    if (receipt.get('raw_sha256') != entry['raw']['sha256']
            or receipt.get('review_sha256') != entry['review']['sha256']
            or receipt.get('suite') != name or receipt.get('source_sha256') != source_sha
            or receipt.get('acceptance_policy') != policy):
        raise ValueError('Suite receipt is not bound to its actual raw report and review: '+name)
    if (binding.get('source', {}).get('sha256', binding.get('source_sha256')) != source_sha
            or binding.get('source_unchanged') is not True
            or binding.get('model_runtime_unchanged') is not True
            or binding.get('frontend_unchanged') is not True
            or binding.get('server_still_draining')):
        raise ValueError('Suite runtime binding is incomplete: '+name)
    if receipt.get('binding_sha256', entry['binding']['sha256']) != entry['binding']['sha256']:
        raise ValueError('Suite receipt refers to another runtime binding: '+name)
    if policy['id'] == COMPLETION:
        if receipt.get('binding_sha256') != entry['binding']['sha256']:
            raise ValueError('Suite receipt lacks its runtime binding hash: '+name)
        _verify_candidate_binding(root,name,raw,binding,policy,source_sha,build_id,model_runtime,freeze)
    if any(receipt.get(k) != planned for k in ('planned','finished','reviewed')) or receipt.get('is_subset') is not False:
        raise ValueError('Missing, incomplete or subset scenarios: '+name)
    critical = any(row.get('hard_failures') or reviews[identity].get('hard_failures') for identity,row in rows.items())
    if not isinstance(receipt.get('critical_failures'), list) or bool(receipt['critical_failures']) != critical:
        raise ValueError('Receipt critical verdict differs from original and independent evidence: '+name)
    minimum = suite_rules(policy,name)['business_minimum']
    if type(receipt.get('passed')) is not int or not 0 <= receipt['passed'] <= planned:
        raise ValueError('Invalid business score: '+name)
    accepted = not critical and receipt['passed'] >= minimum
    if name in ('new100','holdout20'):
        summary = review.get('semantic_summary', {})
        if (summary.get('acceptance_policy') != policy
                or summary.get('passed') != receipt['passed']
                or summary.get('reviewed') != planned
                or review.get('independent_review', {}).get('source_report_sha256') != entry['raw']['sha256']):
            raise ValueError('Independent semantic score does not accept this suite: '+name)
        if policy['id'] == COMPLETION:
            count, score_met = _semantic_score(name,rows,reviews,summary,policy)
            if count != receipt['passed']:raise ValueError('Receipt differs from independently recounted semantic score: '+name)
            accepted = accepted and score_met
        else:
            accepted = accepted and summary.get('accepted_full_suite') is True
        if summary.get('accepted_full_suite') is not accepted:
            raise ValueError('Semantic acceptance verdict differs from actual scores: '+name)
    else:
        if any(type(item.get('passed')) is not bool for item in reviews.values()):
            raise ValueError('Independent per-case verdict missing: '+name)
        intersection = sum(reviews[identity]['passed'] and
                           _automatic_pass(row,name)
                           for identity, row in rows.items())
        if intersection != receipt['passed']:
            raise ValueError('Receipt differs from actual automatic and independent intersection: '+name)
    if name == 'assessments':
        groups = receipt.get('subgroups', {})
        correct = [row for identity, row in rows.items() if row.get('status') == 'passed' and reviews[identity]['passed']]
        binary = [row for row in correct if row.get('gold', {}).get('decision') in ('approve', 'reject')]
        actual = {'complete':len(binary), 'needs_info':sum(row.get('gold', {}).get('decision') == 'needs_info' for row in correct)}
        categories = {'refund':'return_refund', 'exchange':'exchange', 'resend':'reship'}
        actual.update({key:sum(row.get('category') == category for row in binary) for key,category in categories.items()})
        if any(groups.get(key, {}).get('passed') != actual[key] for key in policy['assessment_subgroups']):
            raise ValueError('Assessment subgroup score differs from observed cases')
        accepted = accepted and all(actual[key] >= value for key,value in policy['assessment_subgroups'].items())
        if policy['id'] == COMPLETION:
            binary_rows = [row for row in rows.values() if row.get('gold',{}).get('decision') in ('approve','reject')]
            if (len(binary_rows) != 240 or sum(row.get('gold',{}).get('decision') == 'needs_info' for row in rows.values()) != 63
                    or any(sum(row.get('category') == category for row in binary_rows) != 80 for category in categories.values())):
                raise ValueError('Assessment subgroup denominators are incomplete')
    if name == 'packing30':
        fields = [field for row in rows.values() for field in row.get('fields', []) if field.get('field') != '商品行数']
        if (receipt.get('field_total', 0) <= 0
                or receipt.get('field_total') != len(fields)
                or receipt.get('field_correct') != sum(field.get('passed') is True for field in fields)
                ):
            raise ValueError('Packing correctness requirements not met')
        accepted = (accepted and receipt.get('released_critical_fields_correct') is True
                    and receipt.get('known_severe_anomalies_blocked') is True
                    and receipt.get('field_correct', -1)*100 >= receipt['field_total']*98)
    if type(receipt.get('accepted')) is not bool or receipt['accepted'] is not bool(accepted):
        raise ValueError('Receipt acceptance differs from independently verified scores: '+name)
    if not accepted and not allow_rejected:raise ValueError('Acceptance threshold not met: '+name)
    return bool(accepted)


def verify_manifest(root, evidence, source_sha, build_id):
    root = Path(root)
    policy = verify_policy(evidence.get('acceptance_policy'))
    if (evidence.get('schema_version') != 1 or evidence.get('phase','final') != 'final'
            or evidence.get('source_sha256') != source_sha or evidence.get('frontend_build_id') != build_id):
        raise ValueError('Acceptance version or final phase does not match this release')
    freeze = read_proof(root, evidence.get('freeze'))
    if freeze.get('source', {}).get('sha256') != source_sha or freeze.get('acceptance_policy') != policy:
        raise ValueError('Acceptance policy or source differs from final freeze')
    runtime = evidence.get('model_runtime')
    if not runtime or freeze.get('model_runtime') != runtime:
        raise ValueError('Acceptance model runtime differs from final freeze')
    if policy['id'] == COMPLETION:verify_frozen_inputs(root,freeze)
    checks = evidence.get('checks', {})
    if set(checks) != REQUIRED_CHECKS:raise ValueError('Required free verification is incomplete')
    for name, proof in checks.items():
        check = read_proof(root, proof)
        if check.get('passed') is not True or check.get('source_sha256') != source_sha:
            raise ValueError('Free verification failed or belongs to another source: '+name)
    suites = evidence.get('suites', {})
    if set(suites) != set(EXPECTED):raise ValueError('All 604 scenarios and the separate packing30 are required')
    for name, entry in suites.items():
        if name == 'holdout20' and policy['id'] == COMPLETION:
            raw = read_proof(root,entry['raw'])
            if raw.get('freeze', {}).get('parent_freeze_sha256') != evidence['freeze']['sha256']:
                raise ValueError('Holdout belongs to another main freeze')
        verify_suite(root,name,entry,policy,source_sha,build_id,runtime,freeze)
    return True
