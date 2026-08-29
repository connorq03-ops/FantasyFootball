"""
config.py - Loader for config.yaml.

The league format (PPR / superflex `OP` / dynasty / season) lives in exactly
one place: config.yaml. Every client method and prefetch call defaults to
those filters, so no code edit is needed between seasons.
"""

import copy
import os
from typing import Any, Dict, Optional

import yaml

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')

_CACHE: Dict[str, Dict[str, Any]] = {}


def load_config(path: Optional[str] = None, reload: bool = False) -> Dict[str, Any]:
    """Load and memoize config.yaml. Returns a deep copy so callers can't mutate it."""
    path = os.path.abspath(path or DEFAULT_CONFIG_PATH)
    if reload or path not in _CACHE:
        with open(path, 'r') as fh:
            _CACHE[path] = yaml.safe_load(fh) or {}
    return copy.deepcopy(_CACHE[path])


def get_api_filters(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the league API filters (sport/scoring/position/type/season/week)."""
    config = config or load_config()
    return dict(config.get('api_filters', {}))


def resolve_path(config: Dict[str, Any], key: str, base_dir: Optional[str] = None) -> str:
    """Resolve a `paths:` entry relative to the repo root (or `base_dir`)."""
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    value = config.get('paths', {}).get(key, key)
    return value if os.path.isabs(value) else os.path.join(base_dir, value)
