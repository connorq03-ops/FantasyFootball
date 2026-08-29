"""
fantasypros_client.py - FantasyPros public API client for NFL player values.

Mirrors the architecture of golf/datagolf_client.py in NCAAProjectCH.

Auth: `x-api-key` HTTP request header (NOT Bearer, NOT ?key= query param).
Base URL: https://api.fantasypros.com/public/v2/json

Every method defaults its query params to the LEAGUE filters in config.yaml
(sport=NFL, scoring=PPR, position=OP superflex, type=dynasty, current season)
so the wrong format cannot be pulled by accident. Explicit kwargs override
them for one-off pulls (e.g. per-position QB/RB/WR/TE fetches).

RATE LIMIT: 500 calls per DAY and 1 request per second (premium key). Always go
through cache.cached_call() so cache hits never spend the daily budget.
"""

import os
import time
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from config import get_api_filters, load_config

# Primary env var name (provided as an org secret at runtime). The second name
# is a fallback for environments where the secret is registered as `FantasyPros`.
API_KEY_ENV_VARS = ('FANTASYPROS_API_KEY', 'FantasyPros')


class FantasyProsClient:
    """Client for interacting with the FantasyPros public API."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 config: Optional[Dict[str, Any]] = None):
        """
        Initialize the FantasyPros API client.

        Args:
            api_key: API key for authentication. If not provided, read from the
                FANTASYPROS_API_KEY environment variable (org secret).
            base_url: Override the API base URL (defaults to config.yaml).
            config: Pre-loaded config dict (defaults to config.yaml).
        """
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
        load_dotenv()

        self.config = config or load_config()
        api_cfg = self.config.get('api', {})
        self.filters = get_api_filters(self.config)
        self.base_url = (base_url or api_cfg.get('base_url',
                         'https://api.fantasypros.com/public/v2/json')).rstrip('/')
        self.auth_header = api_cfg.get('auth_header', 'x-api-key')
        self.timeout = api_cfg.get('timeout_seconds', 30)
        self.min_interval = float(api_cfg.get('min_interval_seconds', 1.0))
        self._last_request_ts = 0.0

        self.api_key = api_key or next(
            (os.getenv(name) for name in API_KEY_ENV_VARS if os.getenv(name)), None)
        if not self.api_key:
            raise ValueError(
                "API key is required. Provide it directly or set the "
                "FANTASYPROS_API_KEY environment variable."
            )

        self.session = requests.Session()
        # FantasyPros uses header auth, not query-param or Bearer auth.
        self.session.headers.update({
            self.auth_header: self.api_key,
            'Accept': 'application/json',
        })

    # ── Core request ─────────────────────────────────────────────────────────

    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """
        Make a request to the FantasyPros API.

        Args:
            endpoint: API endpoint path, e.g. 'nfl/2026/consensus-rankings'.
            params: Query parameters for the request.

        Returns:
            The parsed JSON response.

        Raises:
            requests.exceptions.RequestException: If the request fails.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        self._throttle()
        try:
            response = self.session.get(url, params=params or {}, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error making request to {url}: {type(e).__name__}")
            raise

    def _throttle(self) -> None:
        """Respect the API's 1 request/second limit."""
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.time() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.time()

    def _league_params(self, scoring: Optional[str] = None, position: Optional[str] = None,
                       ranking_type: Optional[str] = None, week: Optional[Any] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Build a params dict defaulting to the league filters from config.yaml."""
        params: Dict[str, Any] = {
            'scoring': scoring or self.filters.get('scoring', 'PPR'),
            'position': position or self.filters.get('position', 'OP'),
        }
        resolved_type = ranking_type or self.filters.get('type', 'dynasty')
        if resolved_type:
            params['type'] = resolved_type
        resolved_week = self.filters.get('week', 0) if week is None else week
        if resolved_week is not None:
            params['week'] = resolved_week
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _season(self, season: Optional[Any] = None) -> Any:
        return season if season is not None else self.filters.get('season')

    # ── Rankings / ADP / players ─────────────────────────────────────────────

    def get_consensus_rankings(self, season: Optional[Any] = None, scoring: Optional[str] = None,
                               position: Optional[str] = None, ranking_type: Optional[str] = None,
                               week: Optional[Any] = None, limit: Optional[int] = None,
                               offset: Optional[int] = None) -> Any:
        """
        Consensus / ECR rankings for the league format.

        Defaults to PPR + superflex (`position=OP`) + dynasty from config.yaml.
        `limit`/`offset` are honored by paid FantasyPros tiers; the free tier
        caps every response at the top 10 players and ignores them.
        """
        params = self._league_params(scoring, position, ranking_type, week,
                                     extra={'limit': limit, 'offset': offset})
        return self._make_request(f"nfl/{self._season(season)}/consensus-rankings", params)

    def get_dynasty_rankings(self, season: Optional[Any] = None, scoring: Optional[str] = None,
                             position: Optional[str] = None, week: Optional[Any] = None,
                             limit: Optional[int] = None, offset: Optional[int] = None) -> Any:
        """Dynasty rankings (`type=dynasty`) — the board's superflex baseline source."""
        return self.get_consensus_rankings(season=season, scoring=scoring, position=position,
                                           ranking_type='dynasty', week=week,
                                           limit=limit, offset=offset)

    def get_adp(self, season: Optional[Any] = None, scoring: Optional[str] = None,
                position: Optional[str] = None, week: Optional[Any] = None,
                limit: Optional[int] = None, offset: Optional[int] = None) -> Any:
        """
        Average draft position (`type=adp`).

        FantasyPros only publishes ADP for `position=ALL`; the superflex (`OP`)
        filter returns zero ADP rows. ADP is informational only — the board
        baseline comes from the OP dynasty rankings — so `adp_position` in
        config.yaml defaults to ALL.
        """
        position = position or self.filters.get('adp_position', 'ALL')
        return self.get_consensus_rankings(season=season, scoring=scoring, position=position,
                                           ranking_type='adp', week=week,
                                           limit=limit, offset=offset)

    def get_players(self, season: Optional[Any] = None, position: str = 'ALL') -> Any:
        """
        Player universe (ids, names, positions, teams, bye weeks).

        `position=ALL` here is correct: this endpoint is an id/metadata lookup,
        not a ranking, so the superflex filter does not apply.
        """
        params = {'position': position}
        season = self._season(season)
        if season is not None:
            params['season'] = season
        return self._make_request('nfl/players', params)
