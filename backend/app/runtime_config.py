"""Canonical public model settings; credentials never enter this snapshot."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
from urllib.parse import urlsplit


def provider_endpoint_fingerprint(value):
    """Hash a credential-free endpoint, never a URL carrying authentication."""
    try:
        endpoint = urlsplit(str(value))
        endpoint.port  # Validate without echoing a malformed port in an error.
    except ValueError:
        raise ValueError('Invalid model endpoint URL') from None
    if (endpoint.scheme not in ('http', 'https') or not endpoint.hostname
            or endpoint.username is not None or endpoint.password is not None
            or endpoint.query or endpoint.fragment):
        raise ValueError('Model endpoint must be a credential-free HTTP(S) base URL')
    # Match the provider/pricing code's exact normalization. Host case or an
    # explicit default port can change the verified-pricing allowlist even when
    # an HTTP client would route both URLs to the same host.
    canonical = str(value).rstrip('/')
    return hashlib.sha256(canonical.encode()).hexdigest()


def positive_decimal(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError()
        return format(number.normalize(), 'f')
    except Exception:
        # Do not echo a malformed setting, which might accidentally be a secret.
        raise ValueError('Invalid positive model runtime setting') from None


def model_runtime_snapshot(configuration=None, environ=None):
    """Read an explicit allowlist, not settings.__dict__ or the whole environment.

    Runtime Settings supply the same model/endpoint/timeout that the agent uses.
    Price/bound/retention overrides are read dynamically, as model_usage does.
    Budget limit, ledger and evaluation DB are bound separately by the runner.
    """
    if configuration is None:
        from .config import settings
        configuration = settings
    environ = os.environ if environ is None else environ
    input_rate = environ.get('MODEL_BUDGET_INPUT_CNY_PER_M')
    output_rate = environ.get('MODEL_BUDGET_OUTPUT_CNY_PER_M')
    if (input_rate is None) != (output_rate is None):
        raise ValueError('Both explicit model price ceilings are required')
    try:
        vision_bound = max(1048576, int(environ.get('MODEL_BUDGET_VISION_INPUT_BOUND', '1048576')))
        retention = int(environ.get('MODEL_USAGE_RETENTION_DAYS', '30'))
    except (ValueError, TypeError):
        raise ValueError('Invalid integer model runtime setting') from None
    values = {
        'model': str(configuration.deepseek_model),
        'provider_base_url_sha256': provider_endpoint_fingerprint(configuration.deepseek_base_url),
        'model_timeout_seconds': positive_decimal(configuration.model_timeout),
        'agent_enabled': bool(configuration.agent_enabled),
        'input_price_override_cny_per_m': positive_decimal(input_rate) if input_rate is not None else None,
        'output_price_override_cny_per_m': positive_decimal(output_rate) if output_rate is not None else None,
        'vision_input_bound': vision_bound,
        'usage_retention_days': retention,
    }
    return {'schema_version': 1, 'values': values,
            'sha256': hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
