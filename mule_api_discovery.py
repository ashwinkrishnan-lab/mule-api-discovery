#!/usr/bin/env python3
"""
MuleSoft API Discovery Tool
============================

Connects to MuleSoft Anypoint Platform APIs to gather comprehensive information
about all APIs in your organization. Collects data from:

- Exchange API: API specifications, documentation, categories
- API Manager API: Deployed instances, policies
- Runtime Manager API: Applications, vCore usage, status
- Accounts API: Organizations, business groups, environments
- Visualizer API: Application network graph (dependencies)

Usage:
    python mule_api_discovery.py \\
        --client-id "YOUR_CLIENT_ID" \\
        --client-secret "YOUR_CLIENT_SECRET" \\
        --org-id "YOUR_ORG_ID"

For large organizations (500+ assets), use conservative rate limiting:
    python mule_api_discovery.py ... --rps 2
"""

import os
import sys
import json
import time
import argparse
import requests
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field
from pathlib import Path
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class AnypointConfig:
    """Configuration for Anypoint Platform API access."""
    client_id: str
    client_secret: str
    org_id: str
    region: str = "us"
    
    @property
    def base_url(self) -> str:
        if self.region == "eu":
            return "https://eu1.anypoint.mulesoft.com"
        elif self.region == "gov":
            return "https://gov.anypoint.mulesoft.com"
        return "https://anypoint.mulesoft.com"
    
    @property
    def exchange_url(self) -> str:
        return f"{self.base_url}/exchange/api/v2"
    
    @property
    def api_manager_url(self) -> str:
        return f"{self.base_url}/apimanager/api/v1"
    
    @property
    def accounts_url(self) -> str:
        return f"{self.base_url}/accounts/api"
    
    @property
    def cloudhub_url(self) -> str:
        return f"{self.base_url}/cloudhub/api/v2"
    
    @property
    def arm_url(self) -> str:
        return f"{self.base_url}/hybrid/api/v1"
    
    @property
    def visualizer_url(self) -> str:
        return f"{self.base_url}/visualizer/api/v1"
    
    @property
    def auth_url(self) -> str:
        return f"{self.base_url}/accounts/api/v2/oauth2/token"


@dataclass
class RateLimitConfig:
    """
    Configuration for API rate limiting.
    
    MuleSoft platform APIs have rate limits. This configuration controls
    how the tool paces requests to avoid hitting those limits.
    """
    # Requests per second (lower = safer for large orgs)
    requests_per_second: float = 5.0
    
    @property
    def request_delay(self) -> float:
        return 1.0 / self.requests_per_second
    
    # Max retries on rate limit errors
    max_retries: int = 5
    
    # Initial backoff delay (doubles with each retry)
    initial_backoff: float = 2.0
    
    # Max backoff delay
    max_backoff: float = 60.0
    
    # Pause between batches of assets
    batch_size: int = 50
    batch_pause: float = 5.0
    
    # Number of parallel threads for asset processing (1 = sequential)
    max_workers: int = 1


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class Organization:
    """Organization/Business Group data."""
    org_id: str
    org_name: str
    parent_org_id: str = ""
    is_master: bool = False


@dataclass
class Environment:
    """Environment data."""
    env_id: str
    env_name: str
    env_type: str
    org_id: str = ""


@dataclass
class Application:
    """Deployed application data."""
    app_name: str
    app_id: str
    org_id: str
    env_id: str
    env_type: str
    target: str
    min_size: float = 0.0
    max_size: float = 0.0
    memory: int = 0
    workers: int = 0
    region: str = ""
    status: str = ""
    mule_version: str = ""
    last_update_time: str = ""


@dataclass
class VisualizerNode:
    """Node from Visualizer graph."""
    node_id: str
    node_type: str
    label: str
    system_label: str = ""
    organization_id: str = ""
    environment_id: str = ""
    deployment_target: str = ""
    host: str = ""
    mule_version: str = ""
    layer: Dict = field(default_factory=dict)
    label_source: str = ""
    number_of_client_applications: int = 0


@dataclass
class VisualizerEdge:
    """Edge from Visualizer graph (dependency)."""
    edge_id: str
    source_id: str
    target_id: str


@dataclass
class VisualizerGraph:
    """Complete graph from Visualizer."""
    nodes: List[VisualizerNode] = field(default_factory=list)
    edges: List[VisualizerEdge] = field(default_factory=list)


@dataclass
class APIEndpoint:
    """Endpoint from API specification."""
    method: str
    path: str
    summary: str = ""
    description: str = ""
    parameters: List[Dict] = field(default_factory=list)


@dataclass
class APISpecification:
    """API specification content."""
    spec_type: str
    version: str
    title: str
    description: str
    base_uri: str
    endpoints: List[APIEndpoint] = field(default_factory=list)
    data_types: Dict = field(default_factory=dict)
    raw_spec: Optional[str] = None


@dataclass
class ExchangeAsset:
    """API asset from Exchange."""
    asset_id: str
    group_id: str
    name: str
    version: str
    asset_type: str
    description: str = ""
    status: str = ""
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    custom_fields: Dict = field(default_factory=dict)
    specification: Optional[APISpecification] = None
    documentation_pages: List[Dict] = field(default_factory=list)
    dependencies: List[Dict] = field(default_factory=list)
    dependents: List[Dict] = field(default_factory=list)
    files: List[Dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    created_by: str = ""


@dataclass
class DeployedAPIInstance:
    """API instance from API Manager."""
    instance_id: str
    asset_id: str
    asset_version: str
    environment_id: str
    environment_name: str
    environment_type: str
    instance_label: str = ""
    status: str = ""
    endpoint_uri: str = ""
    technology: str = ""
    policies: List[Dict] = field(default_factory=list)


@dataclass
class DiscoveryOutput:
    """Complete output from the discovery process."""
    discovery_timestamp: str = ""
    master_org: Optional[Organization] = None
    organizations: List[Organization] = field(default_factory=list)
    environments: List[Environment] = field(default_factory=list)
    applications: List[Application] = field(default_factory=list)
    sandbox_graph: Optional[VisualizerGraph] = None
    production_graph: Optional[VisualizerGraph] = None
    exchange_assets: List[ExchangeAsset] = field(default_factory=list)
    assets_by_type: Dict[str, int] = field(default_factory=dict)
    api_instances: List[DeployedAPIInstance] = field(default_factory=list)


# =============================================================================
# API Client with Rate Limiting
# =============================================================================

class AnypointClient:
    """HTTP client for Anypoint Platform APIs with rate limiting and thread safety."""
    
    def __init__(self, config: AnypointConfig, rate_config: RateLimitConfig = None):
        self.config = config
        self.rate_config = rate_config or RateLimitConfig()
        self.access_token: Optional[str] = None
        self.session = requests.Session()
        
        self._last_request_time: float = 0
        self._rate_limit_remaining: Optional[int] = None
        self._rate_limit_reset: Optional[float] = None
        
        # Thread safety lock for rate limiting
        self._lock = threading.Lock()
        
        self.stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "rate_limited_requests": 0,
            "failed_requests": 0,
            "retries": 0,
            "total_wait_time": 0.0
        }
    
    def _wait_for_rate_limit(self) -> None:
        """Wait to respect rate limits. Thread-safe."""
        with self._lock:
            now = time.time()
            
            if self._rate_limit_remaining is not None and self._rate_limit_remaining <= 1:
                if self._rate_limit_reset and self._rate_limit_reset > now:
                    wait_time = self._rate_limit_reset - now + 0.5
                    logger.info(f"⏳ Rate limit reached. Waiting {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    self.stats["total_wait_time"] += wait_time
                    now = time.time()
            
            elapsed = now - self._last_request_time
            if elapsed < self.rate_config.request_delay:
                wait_time = self.rate_config.request_delay - elapsed
                time.sleep(wait_time)
                self.stats["total_wait_time"] += wait_time
            
            self._last_request_time = time.time()
    
    def _update_rate_limit_from_headers(self, response: requests.Response) -> None:
        """Extract rate limit info from response headers."""
        remaining = response.headers.get('X-RateLimit-Remaining') or \
                   response.headers.get('X-Ratelimit-Remaining')
        reset = response.headers.get('X-RateLimit-Reset') or \
               response.headers.get('X-Ratelimit-Reset')
        
        if remaining:
            try:
                self._rate_limit_remaining = int(remaining)
            except ValueError:
                pass
        
        if reset:
            try:
                reset_val = float(reset)
                if reset_val > 1700000000:
                    self._rate_limit_reset = reset_val
                else:
                    self._rate_limit_reset = time.time() + reset_val
            except ValueError:
                pass
    
    def _handle_rate_limit_error(self, response: requests.Response, retry_count: int) -> float:
        """Calculate wait time for rate limit error."""
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        
        return min(
            self.rate_config.initial_backoff * (2 ** retry_count),
            self.rate_config.max_backoff
        )
    
    def authenticate(self) -> bool:
        """Get access token using client credentials."""
        logger.info("Authenticating with Anypoint Platform...")
        
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret
        }
        
        try:
            response = self.session.post(
                self.config.auth_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30
            )
            response.raise_for_status()
            
            token_data = response.json()
            self.access_token = token_data.get("access_token")
            
            self.session.headers.update({
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json"
            })
            
            logger.info("✅ Authentication successful")
            return True
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Authentication failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"❌ Authentication error: {e}")
            return False
    
    def get(self, url: str, **kwargs) -> Optional[Any]:
        """Make GET request with rate limiting and retry."""
        for retry in range(self.rate_config.max_retries + 1):
            try:
                self._wait_for_rate_limit()
                self.stats["total_requests"] += 1
                
                response = self.session.get(url, timeout=60, **kwargs)
                self._update_rate_limit_from_headers(response)
                
                if response.status_code == 429:
                    self.stats["rate_limited_requests"] += 1
                    if retry < self.rate_config.max_retries:
                        wait_time = self._handle_rate_limit_error(response, retry)
                        logger.warning(f"⚠️  Rate limited. Retry {retry + 1}/{self.rate_config.max_retries}. Waiting {wait_time:.1f}s...")
                        time.sleep(wait_time)
                        self.stats["retries"] += 1
                        self.stats["total_wait_time"] += wait_time
                        continue
                    else:
                        logger.error(f"❌ Rate limit exceeded after {self.rate_config.max_retries} retries: {url}")
                        self.stats["failed_requests"] += 1
                        return None
                
                if response.status_code in [500, 502, 503, 504]:
                    if retry < self.rate_config.max_retries:
                        wait_time = self.rate_config.initial_backoff * (2 ** retry)
                        logger.warning(f"⚠️  Server error ({response.status_code}). Retry {retry + 1}. Waiting {wait_time:.1f}s...")
                        time.sleep(wait_time)
                        self.stats["retries"] += 1
                        continue
                
                # Log non-success responses for debugging (except 404 which is expected for some endpoints)
                if response.status_code == 403:
                    logger.debug(f"🔒 Access denied (403): {url} - Check Connected App scopes")
                    self.stats["failed_requests"] += 1
                    self._track_permission_error(url)
                    return None
                
                if response.status_code == 404:
                    # 404 is often expected (e.g., no spec file exists)
                    self.stats["successful_requests"] += 1
                    return None
                
                response.raise_for_status()
                self.stats["successful_requests"] += 1
                
                if response.content:
                    return response.json()
                return {}
                
            except requests.exceptions.HTTPError as e:
                self.stats["failed_requests"] += 1
                if hasattr(e, 'response') and e.response is not None:
                    logger.debug(f"HTTP error {e.response.status_code}: {url}")
                return None
            except requests.exceptions.Timeout:
                if retry < self.rate_config.max_retries:
                    self.stats["retries"] += 1
                    logger.warning(f"⚠️  Timeout on {url}. Retry {retry + 1}...")
                    continue
                self.stats["failed_requests"] += 1
                logger.warning(f"❌ Timeout after {self.rate_config.max_retries} retries: {url}")
                return None
            except Exception as e:
                self.stats["failed_requests"] += 1
                logger.debug(f"Request error: {url} - {e}")
                return None
        
        return None
    
    def _track_permission_error(self, url: str) -> None:
        """Track 403 errors to report scope issues at the end."""
        if "permission_errors" not in self.stats:
            self.stats["permission_errors"] = set()
        # Extract the API type from the URL for summary
        if "/apimanager/" in url:
            self.stats["permission_errors"].add("API Manager")
        elif "/exchange/" in url:
            self.stats["permission_errors"].add("Exchange")
        elif "/visualizer/" in url:
            self.stats["permission_errors"].add("Visualizer")
        elif "/cloudhub/" in url or "/amc/" in url:
            self.stats["permission_errors"].add("Runtime Manager")
        elif "/hybrid/" in url:
            self.stats["permission_errors"].add("Runtime Manager (Hybrid)")
    
    def get_text(self, url: str) -> Optional[str]:
        """Make GET request and return text response."""
        for retry in range(self.rate_config.max_retries + 1):
            try:
                self._wait_for_rate_limit()
                self.stats["total_requests"] += 1
                
                response = self.session.get(url, timeout=60)
                self._update_rate_limit_from_headers(response)
                
                if response.status_code == 429:
                    self.stats["rate_limited_requests"] += 1
                    if retry < self.rate_config.max_retries:
                        wait_time = self._handle_rate_limit_error(response, retry)
                        time.sleep(wait_time)
                        self.stats["retries"] += 1
                        continue
                    return None
                
                if response.status_code == 403:
                    logger.debug(f"🔒 Access denied (403): {url}")
                    self._track_permission_error(url)
                    self.stats["failed_requests"] += 1
                    return None
                
                if response.status_code == 404:
                    # 404 is expected when spec/doc doesn't exist
                    self.stats["successful_requests"] += 1
                    return None
                
                response.raise_for_status()
                self.stats["successful_requests"] += 1
                return response.text
                
            except Exception:
                self.stats["failed_requests"] += 1
                return None
        
        return None
    
    def print_stats(self) -> None:
        """Print request statistics."""
        print(f"\n📊 API Request Statistics:")
        print(f"  Total requests: {self.stats['total_requests']}")
        print(f"  Successful: {self.stats['successful_requests']}")
        print(f"  Rate limited (429): {self.stats['rate_limited_requests']}")
        print(f"  Failed: {self.stats['failed_requests']}")
        print(f"  Retries: {self.stats['retries']}")
        print(f"  Total wait time: {self.stats['total_wait_time']:.1f}s")
        
        # Report permission issues
        if self.stats.get("permission_errors"):
            print(f"\n⚠️  Permission Issues Detected (403 Forbidden):")
            print(f"   The Connected App may be missing scopes for:")
            for api in sorted(self.stats["permission_errors"]):
                print(f"     - {api}")
            print(f"   See CUSTOMER_SETUP.md for required scopes.")


# =============================================================================
# API Discovery Classes
# =============================================================================

class AccountsDiscovery:
    """Retrieves organization and environment data."""
    
    def __init__(self, client: AnypointClient):
        self.client = client
        self.config = client.config
    
    def get_organization(self, org_id: str) -> Optional[Dict]:
        url = f"{self.config.accounts_url}/organizations/{org_id}"
        return self.client.get(url)
    
    def get_sub_organizations(self, org_id: str) -> List[Dict]:
        url = f"{self.config.accounts_url}/organizations/{org_id}/hierarchy"
        result = self.client.get(url)
        if not result:
            return []
        orgs = []
        self._flatten_org_hierarchy(result, orgs)
        return orgs
    
    def _flatten_org_hierarchy(self, org: Dict, result: List[Dict]) -> None:
        result.append({
            "id": org.get("id", ""),
            "name": org.get("name", ""),
            "parentId": org.get("parentId", "")
        })
        for sub_org in org.get("subOrganizations", []):
            self._flatten_org_hierarchy(sub_org, result)
    
    def get_environments(self, org_id: str) -> List[Dict]:
        url = f"{self.config.accounts_url}/organizations/{org_id}/environments"
        result = self.client.get(url)
        return result.get("data", []) if result else []


class RuntimeManagerDiscovery:
    """Retrieves deployed application data."""
    
    def __init__(self, client: AnypointClient):
        self.client = client
        self.config = client.config
    
    def get_cloudhub_applications(self, org_id: str, env_id: str) -> List[Dict]:
        url = f"{self.config.cloudhub_url}/applications"
        headers = {"X-ANYPNT-ORG-ID": org_id, "X-ANYPNT-ENV-ID": env_id}
        result = self.client.get(url, headers=headers)
        return result if isinstance(result, list) else []
    
    def get_cloudhub2_applications(self, org_id: str, env_id: str) -> List[Dict]:
        url = f"{self.config.base_url}/amc/application-manager/api/v2/organizations/{org_id}/environments/{env_id}/deployments"
        result = self.client.get(url)
        return result.get("items", []) if result else []
    
    def get_hybrid_applications(self, org_id: str, env_id: str) -> List[Dict]:
        url = f"{self.config.arm_url}/applications"
        headers = {"X-ANYPNT-ORG-ID": org_id, "X-ANYPNT-ENV-ID": env_id}
        result = self.client.get(url, headers=headers)
        return result.get("data", []) if result else []


class VisualizerDiscovery:
    """Retrieves application network graph."""
    
    def __init__(self, client: AnypointClient):
        self.client = client
        self.config = client.config
    
    def get_graph(self, org_id: str, env_ids: List[str]) -> Optional[Dict]:
        if not env_ids:
            return None
        env_filter = ",".join(env_ids)
        url = f"{self.config.visualizer_url}/organizations/{org_id}/graph"
        return self.client.get(url, params={"environmentIds": env_filter})
    
    def get_graph_for_all_envs(self, org_id: str, environments: List[Environment]) -> Tuple[Optional[Dict], Optional[Dict]]:
        sandbox_ids = [e.env_id for e in environments if e.env_type == "sandbox"]
        production_ids = [e.env_id for e in environments if e.env_type == "production"]
        
        sandbox_graph = self.get_graph(org_id, sandbox_ids) if sandbox_ids else None
        production_graph = self.get_graph(org_id, production_ids) if production_ids else None
        
        return sandbox_graph, production_graph


class ExchangeDiscovery:
    """Retrieves data from MuleSoft Exchange."""
    
    def __init__(self, client: AnypointClient):
        self.client = client
        self.config = client.config
    
    def get_all_assets(self, org_id: str, limit: int = 500) -> List[Dict]:
        logger.info("Fetching assets from Exchange...")
        all_assets = []
        offset = 0
        page_size = 100
        
        while offset < limit:
            url = (f"{self.config.exchange_url}/assets"
                   f"?organizationId={org_id}&offset={offset}&limit={page_size}"
                   f"&includeSnapshots=false")
            result = self.client.get(url)
            if not result:
                break
            assets = result if isinstance(result, list) else result.get("assets", [])
            if not assets:
                break
            all_assets.extend(assets)
            if len(assets) < page_size:
                break
            offset += page_size
        
        logger.info(f"  Found {len(all_assets)} Exchange assets")
        return all_assets
    
    def get_asset_details(self, group_id: str, asset_id: str, version: str) -> Optional[Dict]:
        url = f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}"
        return self.client.get(url)
    
    def get_asset_specification(self, group_id: str, asset_id: str, version: str) -> Optional[str]:
        spec_urls = [
            f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/api/spec",
            f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/files/api.raml",
            f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/files/api.json",
            f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/files/openapi.json",
            f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/files/openapi.yaml",
        ]
        for url in spec_urls:
            spec = self.client.get_text(url)
            if spec:
                return spec
        return None
    
    def get_asset_documentation(self, group_id: str, asset_id: str, version: str) -> List[Dict]:
        """Fetches documentation pages and their content."""
        url = f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/pages"
        pages = self.client.get(url)
        if not pages or not isinstance(pages, list):
            return []
        
        # Fetch content for each page
        for page in pages:
            page_path = page.get("pagePath", "")
            if page_path:
                content = self._get_page_content(group_id, asset_id, version, page_path)
                if content:
                    page["content"] = content
        
        return pages
    
    def _get_page_content(self, group_id: str, asset_id: str, version: str, page_path: str) -> Optional[str]:
        """Fetches the content of a specific documentation page."""
        # URL-encode the page path to handle special characters (spaces, slashes, etc.)
        encoded_path = quote(page_path, safe='')
        url = f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/pages/{encoded_path}"
        return self.client.get_text(url)
    
    def get_asset_dependencies(self, group_id: str, asset_id: str, version: str) -> Dict:
        deps_url = f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/dependencies"
        dependents_url = f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/dependents"
        dependencies = self.client.get(deps_url) or []
        dependents = self.client.get(dependents_url) or []
        return {
            "dependencies": dependencies if isinstance(dependencies, list) else [],
            "dependents": dependents if isinstance(dependents, list) else []
        }
    
    def get_asset_files(self, group_id: str, asset_id: str, version: str) -> List[Dict]:
        url = f"{self.config.exchange_url}/assets/{group_id}/{asset_id}/{version}/files"
        result = self.client.get(url)
        return result if isinstance(result, list) else []


class APIManagerDiscovery:
    """Retrieves data from API Manager."""
    
    def __init__(self, client: AnypointClient):
        self.client = client
        self.config = client.config
    
    def get_api_instances(self, org_id: str, env_id: str) -> List[Dict]:
        url = f"{self.config.api_manager_url}/organizations/{org_id}/environments/{env_id}/apis"
        result = self.client.get(url)
        return result.get("apis", []) if result else []
    
    def get_api_policies(self, org_id: str, env_id: str, api_id: str) -> List[Dict]:
        url = f"{self.config.api_manager_url}/organizations/{org_id}/environments/{env_id}/apis/{api_id}/policies"
        result = self.client.get(url)
        return result.get("policies", []) if result else []


# =============================================================================
# Specification Parser
# =============================================================================

class SpecificationParser:
    """Parses RAML and OAS specifications."""
    
    @staticmethod
    def parse_openapi(spec_content: str) -> Optional[APISpecification]:
        try:
            # Handle empty or whitespace-only content
            if not spec_content or not spec_content.strip():
                return None
            
            content = spec_content.strip()
            if content.startswith('{'):
                spec = json.loads(content)
            else:
                try:
                    import yaml
                    spec = yaml.safe_load(content)
                except ImportError:
                    # If yaml not installed, try JSON as fallback
                    logger.debug("PyYAML not installed, attempting JSON parse")
                    spec = json.loads(content)
                except Exception as yaml_err:
                    logger.debug(f"YAML parse failed: {yaml_err}")
                    return None
            
            if not isinstance(spec, dict):
                return None
            
            oas_version = "OAS3" if spec.get("openapi", "").startswith("3") else "OAS2"
            endpoints = []
            
            for path, methods in spec.get("paths", {}).items():
                for method, details in methods.items():
                    if method.lower() in ['get', 'post', 'put', 'delete', 'patch', 'options', 'head']:
                        endpoints.append(APIEndpoint(
                            method=method.upper(),
                            path=path,
                            summary=details.get("summary", ""),
                            description=details.get("description", ""),
                            parameters=[p for p in details.get("parameters", [])]
                        ))
            
            data_types = {}
            if oas_version == "OAS3":
                data_types = spec.get("components", {}).get("schemas", {})
            else:
                data_types = spec.get("definitions", {})
            
            servers = spec.get("servers", [{}])
            base_uri = servers[0].get("url", "") if oas_version == "OAS3" else spec.get("basePath", "")
            
            return APISpecification(
                spec_type=oas_version,
                version=spec.get("info", {}).get("version", ""),
                title=spec.get("info", {}).get("title", ""),
                description=spec.get("info", {}).get("description", ""),
                base_uri=base_uri,
                endpoints=endpoints,
                data_types=data_types,
                raw_spec=spec_content[:10000]
            )
        except Exception:
            return None
    
    @staticmethod
    def parse_raml(spec_content: str) -> Optional[APISpecification]:
        try:
            lines = spec_content.split('\n')
            title, version, base_uri, description = "", "", "", ""
            
            for line in lines[:50]:
                line = line.strip()
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip()
                elif line.startswith("version:"):
                    version = line.split(":", 1)[1].strip()
                elif line.startswith("baseUri:"):
                    base_uri = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
            
            endpoints = []
            current_path = ""
            
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith('/') and ':' in stripped:
                    current_path = stripped.split(':')[0].strip()
                elif stripped.startswith(('get:', 'post:', 'put:', 'delete:', 'patch:')):
                    method = stripped.split(':')[0].upper()
                    if current_path:
                        endpoints.append(APIEndpoint(method=method, path=current_path))
            
            raml_version = "RAML1.0" if "#%RAML 1.0" in spec_content else "RAML0.8"
            
            return APISpecification(
                spec_type=raml_version,
                version=version,
                title=title,
                description=description,
                base_uri=base_uri,
                endpoints=endpoints,
                raw_spec=spec_content[:10000]
            )
        except Exception:
            return None
    
    @classmethod
    def parse(cls, spec_content: str) -> Optional[APISpecification]:
        if not spec_content:
            return None
        if "#%RAML" in spec_content[:100]:
            return cls.parse_raml(spec_content)
        elif '"openapi"' in spec_content[:500] or 'openapi:' in spec_content[:500]:
            return cls.parse_openapi(spec_content)
        elif '"swagger"' in spec_content[:500]:
            return cls.parse_openapi(spec_content)
        else:
            result = cls.parse_raml(spec_content)
            if result and result.endpoints:
                return result
            return cls.parse_openapi(spec_content)


# =============================================================================
# Main Discovery
# =============================================================================

class MuleSoftAPIDiscovery:
    """Main class that orchestrates data gathering from all API sources."""
    
    def __init__(self, config: AnypointConfig, rate_config: RateLimitConfig = None):
        self.config = config
        self.rate_config = rate_config or RateLimitConfig()
        self.client = AnypointClient(config, self.rate_config)
        self.accounts = AccountsDiscovery(self.client)
        self.runtime = RuntimeManagerDiscovery(self.client)
        self.visualizer = VisualizerDiscovery(self.client)
        self.exchange = ExchangeDiscovery(self.client)
        self.api_manager = APIManagerDiscovery(self.client)
        self.output: Optional[DiscoveryOutput] = None
    
    def run_discovery(self,
                      include_exchange: bool = True,
                      include_specs: bool = True,
                      include_docs: bool = True,
                      include_visualizer: bool = True,
                      include_runtime: bool = True,
                      asset_types: List[str] = None) -> DiscoveryOutput:
        """Run the complete discovery process."""
        
        logger.info("=" * 70)
        logger.info("🚀 Starting MuleSoft API Discovery")
        logger.info("=" * 70)
        
        if not self.client.authenticate():
            raise RuntimeError("Failed to authenticate with Anypoint Platform")
        
        self.output = DiscoveryOutput(discovery_timestamp=datetime.utcnow().isoformat())
        
        # 1. Get organization structure
        logger.info("\n📦 Fetching organization structure...")
        org_info = self.accounts.get_organization(self.config.org_id)
        if org_info:
            self.output.master_org = Organization(
                org_id=org_info.get("id", ""),
                org_name=org_info.get("name", ""),
                is_master=True
            )
            logger.info(f"  Master org: {self.output.master_org.org_name}")
        
        sub_orgs = self.accounts.get_sub_organizations(self.config.org_id)
        for org in sub_orgs:
            self.output.organizations.append(Organization(
                org_id=org.get("id", ""),
                org_name=org.get("name", ""),
                parent_org_id=org.get("parentId", "")
            ))
        logger.info(f"  Found {len(self.output.organizations)} business groups")
        
        # 2. Get environments
        logger.info("\n📦 Fetching environments...")
        all_environments = []
        org_ids = [self.config.org_id] + [o.org_id for o in self.output.organizations]
        
        for org_id in set(org_ids):
            envs = self.accounts.get_environments(org_id)
            for env in envs:
                environment = Environment(
                    env_id=env.get("id", ""),
                    env_name=env.get("name", ""),
                    env_type=env.get("type", ""),
                    org_id=org_id
                )
                all_environments.append(environment)
                self.output.environments.append(environment)
        logger.info(f"  Found {len(self.output.environments)} environments")
        
        # 3. Get deployed applications
        if include_runtime:
            logger.info("\n🔄 Fetching deployed applications...")
            for env in all_environments:
                logger.info(f"  Environment: {env.env_name} ({env.env_type})")
                
                # CloudHub 1.0
                ch1_apps = self.runtime.get_cloudhub_applications(env.org_id, env.env_id)
                for app in ch1_apps:
                    self.output.applications.append(Application(
                        app_name=app.get("domain", ""),
                        app_id=app.get("id", str(app.get("domain", ""))),
                        org_id=env.org_id,
                        env_id=env.env_id,
                        env_type=env.env_type,
                        target="CH1.0",
                        min_size=app.get("workers", {}).get("type", {}).get("weight", 0),
                        max_size=app.get("workers", {}).get("type", {}).get("weight", 0),
                        workers=app.get("workers", {}).get("amount", 0),
                        region=app.get("region", ""),
                        status=app.get("status", ""),
                        mule_version=app.get("muleVersion", ""),
                        last_update_time=app.get("lastUpdateTime", "")
                    ))
                
                # CloudHub 2.0 / RTF
                ch2_apps = self.runtime.get_cloudhub2_applications(env.org_id, env.env_id)
                for app in ch2_apps:
                    target_info = app.get("target", {})
                    replicas = app.get("replicas", [])
                    total_vcores = sum(r.get("state", {}).get("weight", 0) for r in replicas)
                    
                    self.output.applications.append(Application(
                        app_name=app.get("name", ""),
                        app_id=app.get("id", ""),
                        org_id=env.org_id,
                        env_id=env.env_id,
                        env_type=env.env_type,
                        target=target_info.get("provider", "CH2.0"),
                        min_size=total_vcores,
                        max_size=total_vcores,
                        workers=len(replicas),
                        status=app.get("status", ""),
                        last_update_time=app.get("lastModifiedDate", "")
                    ))
                
                # Hybrid
                hybrid_apps = self.runtime.get_hybrid_applications(env.org_id, env.env_id)
                for app in hybrid_apps:
                    self.output.applications.append(Application(
                        app_name=app.get("name", ""),
                        app_id=str(app.get("id", "")),
                        org_id=env.org_id,
                        env_id=env.env_id,
                        env_type=env.env_type,
                        target="hybrid",
                        status=app.get("lastReportedStatus", ""),
                        last_update_time=app.get("lastModifiedDate", "")
                    ))
            
            logger.info(f"  Found {len(self.output.applications)} total applications")
        
        # 4. Get Visualizer graph
        if include_visualizer:
            logger.info("\n🔗 Fetching Visualizer graph...")
            sandbox_raw, production_raw = self.visualizer.get_graph_for_all_envs(
                self.config.org_id, all_environments
            )
            
            if sandbox_raw:
                self.output.sandbox_graph = self._parse_visualizer_graph(sandbox_raw)
                logger.info(f"  Sandbox: {len(self.output.sandbox_graph.nodes)} nodes, {len(self.output.sandbox_graph.edges)} edges")
            
            if production_raw:
                self.output.production_graph = self._parse_visualizer_graph(production_raw)
                logger.info(f"  Production: {len(self.output.production_graph.nodes)} nodes, {len(self.output.production_graph.edges)} edges")
        
        # 5. Get Exchange assets
        if include_exchange:
            logger.info("\n📚 Fetching Exchange assets...")
            raw_assets = self.exchange.get_all_assets(self.config.org_id)
            
            if asset_types:
                raw_assets = [a for a in raw_assets if a.get("type") in asset_types]
                logger.info(f"  Filtered to {len(raw_assets)} assets of types: {asset_types}")
            
            total_assets = len(raw_assets)
            max_workers = self.rate_config.max_workers
            
            if total_assets > 100:
                estimated_calls = total_assets * 5
                # Adjust estimate for parallel execution
                effective_rps = self.rate_config.requests_per_second * min(max_workers, 3)
                estimated_time = estimated_calls / effective_rps
                logger.info(f"  ⏱️  Estimated: {estimated_time/60:.1f} min for {total_assets} assets (workers: {max_workers})")
            
            type_counts: Dict[str, int] = {}
            type_counts_lock = threading.Lock()
            processed_count = [0]  # Use list for mutable counter in closure
            
            def process_single_asset(raw_asset: Dict) -> ExchangeAsset:
                """Process a single asset - can be run in parallel."""
                asset_type = raw_asset.get("type", "unknown")
                
                with type_counts_lock:
                    type_counts[asset_type] = type_counts.get(asset_type, 0) + 1
                
                asset = self._build_exchange_asset(raw_asset)
                
                try:
                    details = self.exchange.get_asset_details(asset.group_id, asset.asset_id, asset.version)
                    if details:
                        self._enrich_asset_from_details(asset, details)
                    
                    if include_specs and asset_type in ['rest-api', 'http-api', 'raml', 'raml-fragment', 'oas']:
                        spec_content = self.exchange.get_asset_specification(asset.group_id, asset.asset_id, asset.version)
                        if spec_content:
                            asset.specification = SpecificationParser.parse(spec_content)
                    
                    if include_docs:
                        asset.documentation_pages = self.exchange.get_asset_documentation(asset.group_id, asset.asset_id, asset.version)
                    
                    deps = self.exchange.get_asset_dependencies(asset.group_id, asset.asset_id, asset.version)
                    asset.dependencies = deps.get("dependencies", [])
                    asset.dependents = deps.get("dependents", [])
                    
                    asset.files = self.exchange.get_asset_files(asset.group_id, asset.asset_id, asset.version)
                except Exception as e:
                    logger.warning(f"  ⚠️  Error processing asset {asset.asset_id}: {e}")
                
                with type_counts_lock:
                    processed_count[0] += 1
                    if processed_count[0] % 10 == 0 or processed_count[0] == total_assets:
                        pct = (processed_count[0] / total_assets) * 100
                        logger.info(f"  Processing: {processed_count[0]}/{total_assets} ({pct:.0f}%)")
                
                return asset
            
            if max_workers > 1:
                # Parallel execution using ThreadPoolExecutor
                logger.info(f"  🚀 Using {max_workers} parallel workers")
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(process_single_asset, raw_asset): raw_asset 
                              for raw_asset in raw_assets}
                    
                    for future in as_completed(futures):
                        try:
                            asset = future.result()
                            self.output.exchange_assets.append(asset)
                        except Exception as e:
                            raw_asset = futures[future]
                            logger.warning(f"  ⚠️  Failed to process asset: {raw_asset.get('assetId', 'unknown')}: {e}")
            else:
                # Sequential execution with batch pauses
                batch_size = self.rate_config.batch_size
                for i, raw_asset in enumerate(raw_assets):
                    asset = process_single_asset(raw_asset)
                    self.output.exchange_assets.append(asset)
                    
                    if (i + 1) % batch_size == 0 and (i + 1) < total_assets:
                        logger.info(f"  💤 Batch pause ({self.rate_config.batch_pause}s)...")
                        time.sleep(self.rate_config.batch_pause)
            
            self.output.assets_by_type = type_counts
        
        # 6. Get API Manager instances
        logger.info("\n📋 Fetching API Manager instances...")
        for env in all_environments:
            instances = self.api_manager.get_api_instances(env.org_id, env.env_id)
            for instance in instances:
                api_id = str(instance.get("id", ""))
                policies = self.api_manager.get_api_policies(env.org_id, env.env_id, api_id)
                
                self.output.api_instances.append(DeployedAPIInstance(
                    instance_id=api_id,
                    asset_id=instance.get("exchangeAssetName", ""),
                    asset_version=instance.get("assetVersion", ""),
                    environment_id=env.env_id,
                    environment_name=env.env_name,
                    environment_type=env.env_type,
                    instance_label=instance.get("instanceLabel", ""),
                    status=instance.get("status", ""),
                    endpoint_uri=instance.get("endpointUri", ""),
                    technology=instance.get("technology", ""),
                    policies=policies
                ))
        logger.info(f"  Found {len(self.output.api_instances)} API instances")
        
        # Summary
        logger.info("\n" + "=" * 70)
        logger.info("✅ Discovery Complete!")
        logger.info("=" * 70)
        self._print_summary()
        self.client.print_stats()
        
        return self.output
    
    def _parse_visualizer_graph(self, raw_graph: Dict) -> VisualizerGraph:
        graph = VisualizerGraph()
        for node in raw_graph.get("nodes", []):
            graph.nodes.append(VisualizerNode(
                node_id=node.get("id", ""),
                node_type=node.get("type", ""),
                label=node.get("label", ""),
                system_label=node.get("systemLabel", ""),
                organization_id=node.get("organizationId", ""),
                environment_id=node.get("environmentId", ""),
                deployment_target=node.get("deploymentTarget", ""),
                host=node.get("host", ""),
                mule_version=node.get("muleVersion", ""),
                layer=node.get("layer", {}),
                label_source=node.get("labelSource", ""),
                number_of_client_applications=node.get("numberOfClientApplications", 0)
            ))
        for edge in raw_graph.get("edges", []):
            graph.edges.append(VisualizerEdge(
                edge_id=edge.get("id", ""),
                source_id=edge.get("sourceId", ""),
                target_id=edge.get("targetId", "")
            ))
        return graph
    
    def _build_exchange_asset(self, raw: Dict) -> ExchangeAsset:
        return ExchangeAsset(
            asset_id=raw.get("assetId", ""),
            group_id=raw.get("groupId", self.config.org_id),
            name=raw.get("name", ""),
            version=raw.get("version", ""),
            asset_type=raw.get("type", "unknown"),
            description=raw.get("description", ""),
            status=raw.get("status", ""),
            created_at=raw.get("createdAt", ""),
            updated_at=raw.get("updatedAt", ""),
            categories=raw.get("categories", []),
            tags=[t.get("value", "") for t in raw.get("labels", [])]
        )
    
    def _enrich_asset_from_details(self, asset: ExchangeAsset, details: Dict) -> None:
        asset.description = details.get("description", asset.description)
        asset.created_by = details.get("createdBy", {}).get("userName", "")
        custom_fields = {}
        for field_item in details.get("customFields", []):
            custom_fields[field_item.get("key", "")] = field_item.get("value", "")
        asset.custom_fields = custom_fields
        categories = []
        for cat in details.get("categories", []):
            categories.append(cat.get("displayName", ""))
        asset.categories = categories
    
    def _print_summary(self) -> None:
        print(f"\n  Organization: {self.output.master_org.org_name if self.output.master_org else 'N/A'}")
        print(f"  Business Groups: {len(self.output.organizations)}")
        print(f"  Environments: {len(self.output.environments)}")
        print(f"  Applications: {len(self.output.applications)}")
        print(f"  Exchange Assets: {len(self.output.exchange_assets)}")
        print(f"  API Instances: {len(self.output.api_instances)}")
        if self.output.sandbox_graph:
            print(f"  Sandbox Graph: {len(self.output.sandbox_graph.nodes)} nodes, {len(self.output.sandbox_graph.edges)} edges")
        if self.output.production_graph:
            print(f"  Production Graph: {len(self.output.production_graph.nodes)} nodes, {len(self.output.production_graph.edges)} edges")
        
        # Data completeness check
        self._print_data_completeness()
    
    def _print_data_completeness(self) -> None:
        """Print data completeness diagnostics."""
        api_types = ['rest-api', 'http-api', 'raml', 'raml-fragment', 'oas']
        api_assets = [a for a in self.output.exchange_assets if a.asset_type in api_types]
        
        if not api_assets:
            return
        
        with_specs = sum(1 for a in api_assets if a.specification)
        with_docs = sum(1 for a in api_assets if a.documentation_pages)
        with_files = sum(1 for a in api_assets if a.files)
        with_deps = sum(1 for a in self.output.exchange_assets if a.dependencies or a.dependents)
        
        print(f"\n📋 Data Completeness for API Assets ({len(api_assets)} APIs):")
        print(f"  With specifications: {with_specs}/{len(api_assets)}")
        print(f"  With documentation: {with_docs}/{len(api_assets)}")
        print(f"  With file listings: {with_files}/{len(api_assets)}")
        print(f"  With dependencies: {with_deps}/{len(self.output.exchange_assets)}")
        
        # Potential issues
        issues = []
        if len(api_assets) > 0 and with_specs == 0:
            issues.append("No specs found - APIs may be proxies without embedded specs, or /api/spec endpoint returned 404")
        if len(api_assets) > 0 and with_docs == 0:
            issues.append("No documentation found - docs may not be published, or /pages endpoint inaccessible")
        if len(self.output.applications) > 0 and len(self.output.api_instances) == 0:
            issues.append("No API instances despite having apps - API Manager scope may be missing")
        if len(self.output.environments) > 0 and not self.output.sandbox_graph and not self.output.production_graph:
            issues.append("No Visualizer data - Visualizer scope may be missing or not enabled")
        
        if issues:
            print(f"\n⚠️  Potential Data Gaps:")
            for issue in issues:
                print(f"    • {issue}")
    
    def save_output(self, output_dir: str = "discovery_output") -> Dict[str, str]:
        """Save discovery output to files."""
        if not self.output:
            raise ValueError("No output to save. Run discovery first.")
        
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_files = {}
        
        # Full output
        full_file = output_path / f"full_discovery_{timestamp}.json"
        with open(full_file, 'w') as f:
            json.dump(self._to_dict(self.output), f, indent=2, default=str)
        saved_files["full_output"] = str(full_file)
        
        # Summary
        summary_file = output_path / f"summary_{timestamp}.json"
        with open(summary_file, 'w') as f:
            json.dump(self._build_summary(), f, indent=2)
        saved_files["summary"] = str(summary_file)
        
        # Specs
        specs_dir = output_path / "api_specs"
        specs_dir.mkdir(exist_ok=True)
        for asset in self.output.exchange_assets:
            if asset.specification and asset.specification.raw_spec:
                safe_name = asset.asset_id.replace("/", "_").replace(" ", "_")
                spec_file = specs_dir / f"{safe_name}_{asset.version}.txt"
                with open(spec_file, 'w') as f:
                    f.write(asset.specification.raw_spec)
        saved_files["specs_directory"] = str(specs_dir)
        
        logger.info(f"\n📁 Output saved to: {output_path}")
        for name, path in saved_files.items():
            logger.info(f"  • {name}: {path}")
        
        return saved_files
    
    def _build_summary(self) -> Dict:
        return {
            "discovery_timestamp": self.output.discovery_timestamp,
            "organization": {
                "id": self.output.master_org.org_id if self.output.master_org else "",
                "name": self.output.master_org.org_name if self.output.master_org else ""
            },
            "counts": {
                "business_groups": len(self.output.organizations),
                "environments": len(self.output.environments),
                "applications": len(self.output.applications),
                "exchange_assets": len(self.output.exchange_assets),
                "api_instances": len(self.output.api_instances),
                "sandbox_nodes": len(self.output.sandbox_graph.nodes) if self.output.sandbox_graph else 0,
                "sandbox_edges": len(self.output.sandbox_graph.edges) if self.output.sandbox_graph else 0,
                "production_nodes": len(self.output.production_graph.nodes) if self.output.production_graph else 0,
                "production_edges": len(self.output.production_graph.edges) if self.output.production_graph else 0
            },
            "assets_by_type": self.output.assets_by_type,
            "environments": [
                {"id": e.env_id, "name": e.env_name, "type": e.env_type}
                for e in self.output.environments
            ],
            "applications": [
                {"name": a.app_name, "target": a.target, "status": a.status,
                 "env_type": a.env_type, "vcore_min": a.min_size, "vcore_max": a.max_size}
                for a in self.output.applications
            ]
        }
    
    def _to_dict(self, obj) -> Any:
        if hasattr(obj, '__dataclass_fields__'):
            return {k: self._to_dict(v) for k, v in asdict(obj).items()}
        elif isinstance(obj, list):
            return [self._to_dict(i) for i in obj]
        elif isinstance(obj, dict):
            return {k: self._to_dict(v) for k, v in obj.items()}
        return obj


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MuleSoft API Discovery Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full discovery
  python mule_api_discovery.py --client-id xxx --client-secret xxx --org-id xxx

  # Skip Exchange (faster)
  python mule_api_discovery.py ... --no-exchange

  # Large organization (conservative rate)
  python mule_api_discovery.py ... --rps 2

  # Fast execution with parallel workers (recommended for large orgs)
  python mule_api_discovery.py ... --workers 4

  # Maximum speed (may hit rate limits)
  python mule_api_discovery.py ... --workers 5 --rps 10
        """
    )
    
    # Credentials
    parser.add_argument("--client-id", required=True, help="Connected App Client ID")
    parser.add_argument("--client-secret", required=True, help="Connected App Client Secret")
    parser.add_argument("--org-id", required=True, help="Anypoint Organization ID")
    parser.add_argument("--region", choices=["us", "eu", "gov"], default="us", help="Anypoint region")
    
    # Discovery options
    parser.add_argument("--no-exchange", action="store_true", help="Skip Exchange assets")
    parser.add_argument("--no-specs", action="store_true", help="Skip API specifications")
    parser.add_argument("--no-docs", action="store_true", help="Skip documentation")
    parser.add_argument("--no-visualizer", action="store_true", help="Skip Visualizer graph")
    parser.add_argument("--no-runtime", action="store_true", help="Skip Runtime Manager apps")
    parser.add_argument("--asset-types", nargs="+", help="Filter to specific asset types")
    
    # Rate limiting and performance
    parser.add_argument("--rps", type=float, default=5.0, help="Requests per second (default: 5)")
    parser.add_argument("--batch-size", type=int, default=50, help="Assets per batch (default: 50)")
    parser.add_argument("--batch-pause", type=float, default=5.0, help="Pause between batches (default: 5s)")
    parser.add_argument("--max-retries", type=int, default=5, help="Max retries on errors (default: 5)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers for asset processing (default: 1). Use 3-5 for faster execution.")
    
    # Output
    parser.add_argument("--output-dir", default="discovery_output", help="Output directory")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    config = AnypointConfig(
        client_id=args.client_id,
        client_secret=args.client_secret,
        org_id=args.org_id,
        region=args.region
    )
    
    rate_config = RateLimitConfig(
        requests_per_second=args.rps,
        batch_size=args.batch_size,
        batch_pause=args.batch_pause,
        max_retries=args.max_retries,
        max_workers=args.workers
    )
    
    workers_info = f", workers: {args.workers}" if args.workers > 1 else ""
    print(f"\n⚡ Rate Limiting: {args.rps} req/sec, batch {args.batch_size}, pause {args.batch_pause}s{workers_info}")
    
    discovery = MuleSoftAPIDiscovery(config, rate_config)
    
    try:
        discovery.run_discovery(
            include_exchange=not args.no_exchange,
            include_specs=not args.no_specs,
            include_docs=not args.no_docs,
            include_visualizer=not args.no_visualizer,
            include_runtime=not args.no_runtime,
            asset_types=args.asset_types
        )
        
        discovery.save_output(args.output_dir)
        print(f"\n📁 Output saved to: {args.output_dir}")
        
    except Exception as e:
        logger.error(f"Discovery failed: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

