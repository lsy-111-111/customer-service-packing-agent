"""Versioned acceptance rules. No scenario answers or model calls live here."""
from copy import deepcopy
import hashlib
import json

STRICT = 'strict-v1'
MODERATE = 'moderate-demo-20261004-v1'
COMPLETION = 'moderate-completion-20261005-v1'
PROFILES = (STRICT, MODERATE, COMPLETION)
PLANNED = {'new100': 100, 'holdout20': 20, 'original40': 40, 'conversations': 120,
           'assessments': 303, 'images': 8, 'after_sale_chat': 12, 'draft': 1, 'packing30': 30}
ASSESSMENT_PLANNED = {'complete': 240, 'refund': 80, 'exchange': 80,
                      'resend': 80, 'needs_info': 63}


def snapshot(profile=STRICT):
    if profile not in PROFILES:
        raise ValueError('Unknown acceptance profile')
    value = {'schema_version': 1, 'id': profile,
             'new100': {'planned': 100, 'business_minimum': 95, 'per_group_minimum': 19,
                        'service_minimum': 95, 'service_required_for_business': True},
             'holdout20': {'planned': 20, 'business_minimum': 19, 'service_minimum': 19,
                           'service_required_for_business': True},
             'facts_percent': 98, 'intents_percent': 95, 'critical_maximum': 0,
             'long_dialogues_minimum': 10, 'long_messages_strictly_greater_than': 30,
             'other_suites': {'original40': 38, 'conversations': 114, 'assessments': 288,
                              'images': 8, 'after_sale_chat': 12, 'draft': 1},
             'assessment_subgroups': {'complete': 228, 'refund': 76, 'exchange': 76,
                                     'resend': 76, 'needs_info': 60}}
    if profile in (MODERATE, COMPLETION):
        value['new100'].update(per_group_minimum=18, service_minimum=90,
                               service_required_for_business=False,
                               presentation_only_defects=['minor_repetition', 'not_concise'],
                               presentation_requirement='真人体验需具体、简洁，不能用固定售后模板覆盖额外问题。')
    if profile == COMPLETION:
        value['new100']['presentation_only_defects'].append('ordinary_wording')
        value['holdout20'].update(business_minimum=18, service_minimum=18,
                                 service_required_for_business=False,
                                 presentation_only_defects=deepcopy(value['new100']['presentation_only_defects']),
                                 presentation_requirement=value['new100']['presentation_requirement'])
        value['other_suites'].update(original40=37, conversations=111, assessments=279,
                                     images=7, after_sale_chat=11)
        value['assessment_subgroups'].update(complete=221, refund=74, exchange=74,
                                             resend=74, needs_info=58)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {**value, 'sha256': hashlib.sha256(encoded).hexdigest()}


def verify(value):
    if not isinstance(value, dict) or value != snapshot(value.get('id')):
        raise ValueError('Acceptance policy differs from its versioned definition')
    return deepcopy(value)


def frozen_policy(freeze, requested=None):
    value = freeze.get('acceptance_policy')
    policy = snapshot(STRICT) if value is None else verify(value)
    if requested is not None and requested != policy['id']:
        raise ValueError('Acceptance profile differs from the run freeze')
    return policy


def report_policy(report, requested=None):
    """Legacy reports retain strict scoring; frozen runs cannot change profiles."""
    freeze = report.get('freeze', {})
    if not isinstance(freeze, dict):
        raise ValueError('Invalid run freeze')
    explicit = report.get('acceptance_policy')
    if explicit is None:
        return frozen_policy(freeze, requested)
    policy = verify(explicit)
    if freeze.get('acceptance_policy') is not None and frozen_policy(freeze) != policy:
        raise ValueError('Report acceptance policy differs from the run freeze')
    if requested is not None and requested != policy['id']:
        raise ValueError('Acceptance profile differs from the original report')
    return policy


def suite_rules(policy, suite):
    verify(policy)
    suite = 'new100' if suite == 'service_v3' else suite
    if suite in ('new100', 'holdout20'):
        return deepcopy(policy[suite])
    if suite == 'matrix':
        return {'planned': 303, 'business_minimum': 303, 'service_gate_applies': False}
    if suite not in PLANNED:
        raise ValueError('Unknown acceptance suite')
    rules = {'planned': PLANNED[suite],
             'business_minimum': 30 if suite == 'packing30' else policy['other_suites'][suite],
             'service_gate_applies': False}
    if suite == 'assessments':
        rules['subgroups'] = {key: {'planned': ASSESSMENT_PLANNED[key], 'business_minimum': minimum}
                              for key, minimum in policy['assessment_subgroups'].items()}
    return rules


def requirement_business_pass(requirement, rules):
    """Only explicitly reviewed polish defects can be service-only.

    Facts, missing intents and operational failures remain separate hard inputs.
    A business omission in the mixed service requirement has no exception.
    """
    if requirement['passed']:
        return True
    return (requirement.get('presentation_only') is True
            and requirement.get('defect_kind') in rules.get('presentation_only_defects', [])
            and requirement['text'] == rules.get('presentation_requirement'))
