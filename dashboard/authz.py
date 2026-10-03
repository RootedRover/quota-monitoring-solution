"""Caller authentication and per-user project authorization (Option B).

Two-layer defence for the dashboard:

1. **Authentication (Who are you?)**:
   - **Direct Cloud Run IAP** (primary browser path): verifies the ES256-signed
     ``x-goog-iap-jwt-assertion`` header against Google's IAP JWK set
     (``https://www.gstatic.com/iap/verify/public_key-jwk``) and checks
     ``iss == "https://cloud.google.com/iap"``.
   - **Cloud Run IAM + OIDC Bearer token** (CLI / ``gcloud run services proxy``
     path): verifies the RS256 OIDC ID token in ``Authorization: Bearer ...``
     (or ``X-Forwarded-Authorization``) against Google's OAuth2 certs and
     checks ``iss in ("https://accounts.google.com", "accounts.google.com")``.
   - Raw unsigned headers such as ``x-goog-authenticated-user-email`` are never
     trusted on their own.

2. **Per-User Project Authorization (What may you see?)**:
   Customers cannot be expected to grant every workload owner org-wide quota
   viewer access just to use the dashboard. Instead, once the caller's email is
   verified, the dashboard uses Cloud Asset Inventory's ``analyzeIamPolicy`` API
   to determine which Organization, Folder, or Project nodes grant that caller
   ``cloudquotas.quotaInfos.list`` (via ``roles/cloudquotas.viewer``,
   ``roles/viewer``, or any custom role, including Google Group inheritance).
   Every dashboard view, KPI counter, quality breakdown, and history query is
   filtered in-memory to the caller's authorized ``project_id`` set.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import google.auth
import requests
from fastapi import Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from requests.adapters import HTTPAdapter

_LOG = logging.getLogger(__name__)

IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
OIDC_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
CLOUD_ASSET_BASE = "https://cloudasset.googleapis.com/v1"
CRM_V3_BASE = "https://cloudresourcemanager.googleapis.com/v3"
DEFAULT_PERMISSION = "cloudquotas.quotaInfos.list"
_FALLBACK_VIEWER_ROLES = frozenset(
    {
        "roles/cloudquotas.viewer",
        "roles/cloudquotas.admin",
        "roles/monitoring.viewer",
        "roles/monitoring.editor",
        "roles/monitoring.admin",
        "roles/viewer",
        "roles/editor",
        "roles/owner",
    }
)
AUTHZ_CACHE_TTL = int(os.environ.get("QMS_AUTHZ_CACHE_TTL", "300"))
MAX_CRM_FALLBACK_PROJECTS = int(os.environ.get("QMS_MAX_CRM_FALLBACK_PROJECTS", "60"))
_AUTHZ_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="qms-authz")


class UnauthenticatedError(Exception):
    """Raised when a request carries no valid IAP JWT or OIDC Bearer token."""


@dataclass(frozen=True)
class CallerIdentity:
    """Cryptographically verified caller identity."""

    email: str
    auth_source: str  # "iap", "bearer", or "dev"
    sub: str = ""


@dataclass(frozen=True)
class ProjectTarget:
    """A project tracked in the warehouse along with its hierarchy ancestry."""

    project_id: str
    project_number: str = ""
    folder_id: str = ""
    org_id: str = ""


@dataclass(frozen=True)
class AuthzContext:
    """Resolved per-request authorization context passed to templates & APIs."""

    email: str
    auth_source: str
    permission: str
    allowed_projects: frozenset[str]
    total_projects: int

    @property
    def allowed_count(self) -> int:
        return len(self.allowed_projects)


def _strip_account_prefix(raw_email: str) -> str:
    """Normalise ``accounts.google.com:user@example.com`` to ``user@example.com``."""
    value = raw_email.strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value.lower()


def _iam_member_for_email(email: str) -> str:
    """Return the IAM member selector (`user:` or `serviceAccount:`) for an email."""
    clean = _strip_account_prefix(email)
    if clean.endswith(".gserviceaccount.com"):
        return f"serviceAccount:{clean}"
    return f"user:{clean}"


_http_session_lock = threading.Lock()
_transport_req: google_requests.Request | None = None


def _get_transport_request() -> google_requests.Request:
    global _transport_req
    with _http_session_lock:
        if _transport_req is None:
            session = requests.Session()
            _transport_req = google_requests.Request(session=session)
        return _transport_req


def verify_iap_jwt(
    token: str,
    *,
    audience: str | None = None,
    transport_request: Any = None,
) -> CallerIdentity:
    """Verify an ``x-goog-iap-jwt-assertion`` ES256 JWT from Google IAP."""
    req = transport_request or _get_transport_request()
    expected_aud = (
        audience if audience is not None else (os.environ.get("QMS_IAP_AUDIENCE") or None)
    )
    try:
        claims = id_token.verify_token(
            token,
            req,
            audience=expected_aud,
            certs_url=IAP_CERTS_URL,
        )
    except Exception as exc:
        raise UnauthenticatedError(f"Invalid IAP JWT assertion: {exc}") from exc

    issuer = claims.get("iss", "")
    if issuer != IAP_ISSUER:
        raise UnauthenticatedError(f"Unexpected IAP JWT issuer: {issuer!r}")

    email = _strip_account_prefix(str(claims.get("email") or ""))
    if not email:
        raise UnauthenticatedError("IAP JWT assertion does not contain an email claim")

    return CallerIdentity(
        email=email,
        auth_source="iap",
        sub=str(claims.get("sub") or ""),
    )


def verify_bearer_jwt(
    token: str,
    *,
    transport_request: Any = None,
) -> CallerIdentity:
    """Verify a Google OIDC Bearer ID token (e.g. from ``gcloud run services proxy``)."""
    req = transport_request or _get_transport_request()
    try:
        claims = id_token.verify_oauth2_token(token, req)
    except Exception as exc:
        raise UnauthenticatedError(f"Invalid OIDC bearer token: {exc}") from exc

    issuer = claims.get("iss", "")
    if issuer not in OIDC_ISSUERS:
        raise UnauthenticatedError(f"Unexpected OIDC issuer: {issuer!r}")

    email = _strip_account_prefix(str(claims.get("email") or ""))
    if not email:
        raise UnauthenticatedError("OIDC bearer token does not contain an email claim")

    return CallerIdentity(
        email=email,
        auth_source="bearer",
        sub=str(claims.get("sub") or ""),
    )


def authenticate_request(request: Request) -> CallerIdentity:
    """Extract and verify the caller's identity from IAP or OIDC headers."""
    # 1. Direct Cloud Run IAP / External LB IAP signed assertion header.
    iap_jwt = (request.headers.get("x-goog-iap-jwt-assertion") or "").strip()
    if iap_jwt:
        return verify_iap_jwt(iap_jwt)

    # 2. OIDC Bearer token (gcloud run services proxy or direct CLI call).
    for header_name in ("x-forwarded-authorization", "authorization"):
        raw_auth = (request.headers.get(header_name) or "").strip()
        if raw_auth.lower().startswith("bearer "):
            bearer_token = raw_auth[7:].strip()
            if bearer_token:
                return verify_bearer_jwt(bearer_token)

    # 3. Local developer mode (strictly blocked on Cloud Run where K_SERVICE is set).
    mode = os.environ.get("QMS_AUTHZ_MODE", "enforced").strip().lower()
    if mode == "dev" and not os.environ.get("K_SERVICE"):
        dev_email = _strip_account_prefix(
            request.headers.get("x-qms-dev-user") or os.environ.get("QMS_DEV_USER_EMAIL", "")
        )
        if dev_email:
            return CallerIdentity(email=dev_email, auth_source="dev")

    raise UnauthenticatedError(
        "Authentication required. Access the dashboard through Cloud Run IAP or "
        "attach a valid Google OIDC Bearer token."
    )


def _parse_resource_grants(payload: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """Extract granted (orgs, folders, projects_or_numbers) from analyzeIamPolicy JSON."""
    allowed_orgs: set[str] = set()
    allowed_folders: set[str] = set()
    allowed_projects: set[str] = set()

    def _record(resource_name: str) -> None:
        if not resource_name:
            return
        parts = [p for p in resource_name.strip("/").split("/") if p]
        for idx, segment in enumerate(parts[:-1]):
            target_id = parts[idx + 1]
            if segment == "organizations":
                allowed_orgs.add(target_id)
            elif segment == "folders":
                allowed_folders.add(target_id)
            elif segment == "projects":
                allowed_projects.add(target_id)

    analysis = payload.get("mainAnalysis") or {}
    for item in analysis.get("analysisResults") or []:
        _record(str(item.get("attachedResourceFullName") or ""))
        for res in item.get("resourceList") or []:
            _record(str(res.get("fullResourceName") or ""))
        for acl in item.get("accessControlLists") or []:
            for res in acl.get("resources") or []:
                _record(str(res.get("fullResourceName") or ""))

    return allowed_orgs, allowed_folders, allowed_projects


class Authorizer:
    """Resolves per-user project access across Org, Folder, and Project hierarchy."""

    def __init__(
        self,
        *,
        permission: str | None = None,
        quota_project: str | None = None,
        default_org_id: str | None = None,
        session: requests.Session | None = None,
        cache_ttl: int = AUTHZ_CACHE_TTL,
        max_crm_fallback_projects: int = MAX_CRM_FALLBACK_PROJECTS,
    ) -> None:
        self.permission = (
            permission or os.environ.get("QMS_REQUIRED_PERMISSION") or DEFAULT_PERMISSION
        )
        self.quota_project = quota_project or os.environ.get("QMS_PROJECT", "")
        self.default_org_id = default_org_id or os.environ.get("QMS_ORG", "")
        self._session = session
        self._cache_ttl = cache_ttl
        self.max_crm_fallback_projects = max_crm_fallback_projects
        self._cache: dict[str, tuple[frozenset[str], float]] = {}
        self._refreshing: set[str] = set()
        self._lock = threading.Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._refreshing.clear()

    def _authed_session(self) -> requests.Session:
        with self._lock:
            if self._session is not None:
                return self._session
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if self.quota_project and hasattr(creds, "with_quota_project"):
                creds = creds.with_quota_project(self.quota_project)
            sess = google_requests.AuthorizedSession(creds)
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            self._session = sess
            return self._session

    def _custom_role_has_permission(self, role_name: str) -> bool:
        """Check whether a custom IAM role (`projects/.../roles/...` or `organizations/.../roles/...`) includes `self.permission`."""
        url = f"https://iam.googleapis.com/v1/{role_name.lstrip('/')}"
        try:
            resp = self._authed_session().get(url, timeout=10)
        except Exception:  # noqa: BLE001
            return False
        if resp.status_code != 200:
            return False
        perms = (resp.json() or {}).get("includedPermissions") or []
        return self.permission in perms

    def _crm_has_viewer_binding(self, scope: str, email: str) -> bool:
        """Check direct IAM bindings on ``scope`` via strongly-consistent CRM v3 getIamPolicy."""
        url = f"{CRM_V3_BASE}/{scope}:getIamPolicy"
        try:
            resp = self._authed_session().post(url, json={}, timeout=15)
        except Exception:  # noqa: BLE001
            return False
        if resp.status_code != 200:
            return False
        member = _iam_member_for_email(email).lower()
        for binding in (resp.json() or {}).get("bindings") or []:
            members = [str(m).lower() for m in (binding.get("members") or [])]
            if member not in members:
                continue
            role = str(binding.get("role") or "")
            if role in _FALLBACK_VIEWER_ROLES or "cloudquotas" in role.lower():
                return True
            if role.startswith(("organizations/", "projects/")) and (
                self._custom_role_has_permission(role)
            ):
                return True
        return False

    def _analyze_scope_with_status(
        self, scope: str, email: str
    ) -> tuple[set[str], set[str], set[str], bool]:
        """Evaluate ``scope`` via Cloud Asset ``analyzeIamPolicy`` + CRM ``getIamPolicy``.

        Returns ``(orgs, folders, projects, cai_ok)``.
        When ``cai_ok`` is True on an ``organizations/{id}`` scope and already
        returns >=1 granted resource, Cloud Asset Inventory has already searched
        the entire Org + Folder + Project hierarchy in a single RPC.
        """
        url = f"{CLOUD_ASSET_BASE}/{scope}:analyzeIamPolicy"
        params: list[tuple[str, str]] = [
            ("analysisQuery.identitySelector.identity", _iam_member_for_email(email)),
            ("analysisQuery.options.expandGroups", "true"),
            ("analysisQuery.options.expandResources", "true"),
        ]
        for role in sorted(_FALLBACK_VIEWER_ROLES):
            params.append(("analysisQuery.accessSelector.roles", role))

        orgs: set[str] = set()
        folders: set[str] = set()
        projs: set[str] = set()
        cai_ok = False

        resp = self._authed_session().get(url, params=params, timeout=15)
        if resp.status_code == 200:
            cai_ok = True
            orgs, folders, projs = _parse_resource_grants(resp.json())

        # If Cloud Asset already returned grants in this hierarchy, no CRM
        # getIamPolicy call is needed on this scope. Only check CRM when CAI
        # returned 0 grants (e.g., fresh IAM binding within CAI's indexing lag
        # window or custom role) or when CAI failed (e.g., HTTP 403).
        has_any_cai_grant = bool(orgs or folders or projs)
        if not has_any_cai_grant and self._crm_has_viewer_binding(scope, email):
            crm_orgs, crm_folders, crm_projs = _parse_resource_grants(
                {
                    "mainAnalysis": {
                        "analysisResults": [
                            {
                                "attachedResourceFullName": (
                                    f"//cloudresourcemanager.googleapis.com/{scope}"
                                )
                            }
                        ]
                    }
                }
            )
            orgs |= crm_orgs
            folders |= crm_folders
            projs |= crm_projs
            return orgs, folders, projs, cai_ok

        if cai_ok:
            return orgs, folders, projs, True

        raise RuntimeError(
            f"analyzeIamPolicy on {scope} returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    def _analyze_scope(self, scope: str, email: str) -> tuple[set[str], set[str], set[str]]:
        orgs, folders, projs, _ = self._analyze_scope_with_status(scope, email)
        return orgs, folders, projs

    def _compute_allowed_projects(
        self,
        clean_email: str,
        target_list: list[ProjectTarget],
    ) -> frozenset[str]:
        allowed_orgs: set[str] = set()
        allowed_folders: set[str] = set()
        allowed_projs: set[str] = set()
        cai_explored_orgs: set[str] = set()

        def _is_target_authorized(t: ProjectTarget) -> bool:
            return bool(
                (t.org_id and t.org_id in allowed_orgs)
                or (t.folder_id and t.folder_id in allowed_folders)
                or (t.project_id in allowed_projs)
                or (t.project_number and str(t.project_number) in allowed_projs)
            )

        # 1. Check Organization scope first.
        #    When Cloud Asset Inventory (expandResources=true, expandGroups=true)
        #    succeeds on an Org and returns >=1 authorized target, the entire
        #    Org + Folder + Project tree has already been evaluated in 1 RPC.
        org_ids = sorted(
            {t.org_id for t in target_list if t.org_id}
            | ({self.default_org_id} if self.default_org_id else set())
        )
        for org_id in org_ids:
            try:
                orgs, folders, projs, cai_ok = self._analyze_scope_with_status(
                    f"organizations/{org_id}", clean_email
                )
                allowed_orgs |= orgs
                allowed_folders |= folders
                allowed_projs |= projs
                if cai_ok:
                    cai_explored_orgs.add(org_id)
            except Exception:  # noqa: BLE001 - fall back to folder/project scopes
                _LOG.debug(
                    "org-level IAM check unavailable for organizations/%s; "
                    "checking folder/project scopes",
                    org_id,
                )

        # Determine which targets still need Folder / Project checks:
        # - Targets in an org where CAI failed (t.org_id not in cai_explored_orgs)
        #   need full _analyze_scope fallback on their Folder/Project.
        # - Targets in an org where CAI succeeded (HTTP 200) and ALREADY granted
        #   >=1 project in that org do NOT need 100-500 redundant per-project RPCs!
        # - Targets in an org where CAI succeeded (HTTP 200) but returned 0
        #   authorized projects are checked via strongly-consistent CRM getIamPolicy
        #   (handles fresh project/folder IAM grants during CAI indexing lag).
        orgs_with_grants = {
            t.org_id for t in target_list if t.org_id and _is_target_authorized(t)
        }

        remaining_after_org: Sequence[ProjectTarget] = [
            t
            for t in target_list
            if not _is_target_authorized(t)
            and (t.org_id not in cai_explored_orgs or t.org_id not in orgs_with_grants)
        ]

        # 2. Check distinct Folder scopes for remaining targets.
        folder_ids = sorted({t.folder_id for t in remaining_after_org if t.folder_id})
        for folder_id in folder_ids:
            try:
                orgs, folders, projs = self._analyze_scope(f"folders/{folder_id}", clean_email)
                allowed_orgs |= orgs
                allowed_folders |= folders
                allowed_projs |= projs
            except Exception:  # noqa: BLE001
                _LOG.debug(
                    "folder-level IAM check unavailable for folders/%s; checking project scope",
                    folder_id,
                )

        # 3. Check remaining Project scopes (bounded by max_crm_fallback_projects
        #    and executed concurrently when multiple projects remain).
        orgs_with_grants = {
            t.org_id for t in target_list if t.org_id and _is_target_authorized(t)
        }
        remaining_after_folder: list[ProjectTarget] = [
            t
            for t in remaining_after_org
            if not _is_target_authorized(t)
            and (t.org_id not in cai_explored_orgs or t.org_id not in orgs_with_grants)
        ][: self.max_crm_fallback_projects]

        def _check_project(target: ProjectTarget) -> tuple[set[str], set[str], set[str]]:
            try:
                # If Org CAI already returned 200 OK (empty), skip redundant
                # project-level CAI call and check strongly-consistent CRM directly.
                if target.org_id in cai_explored_orgs:
                    if self._crm_has_viewer_binding(
                        f"projects/{target.project_id}", clean_email
                    ):
                        return set(), set(), {target.project_id}
                    return set(), set(), set()
                return self._analyze_scope(f"projects/{target.project_id}", clean_email)
            except Exception:  # noqa: BLE001
                _LOG.debug(
                    "project-level IAM check unavailable for projects/%s",
                    target.project_id,
                )
                return set(), set(), set()

        if len(remaining_after_folder) == 1:
            orgs, folders, projs = _check_project(remaining_after_folder[0])
            allowed_orgs |= orgs
            allowed_folders |= folders
            allowed_projs |= projs
        elif len(remaining_after_folder) > 1:
            futs = [_AUTHZ_POOL.submit(_check_project, t) for t in remaining_after_folder]
            for fut in futs:
                orgs, folders, projs = fut.result()
                allowed_orgs |= orgs
                allowed_folders |= folders
                allowed_projs |= projs

        authorized = {t.project_id for t in target_list if _is_target_authorized(t)}
        return frozenset(authorized)

    def _background_refresh(
        self,
        cache_key: str,
        clean_email: str,
        target_list: list[ProjectTarget],
    ) -> None:
        try:
            result = self._compute_allowed_projects(clean_email, target_list)
            with self._lock:
                self._cache[cache_key] = (result, time.monotonic() + self._cache_ttl)
        except Exception:  # noqa: BLE001
            _LOG.warning("background authz refresh failed for %s", clean_email, exc_info=True)
        finally:
            with self._lock:
                self._refreshing.discard(cache_key)

    def allowed_projects(
        self,
        email: str,
        targets: Iterable[ProjectTarget],
    ) -> frozenset[str]:
        """Return the subset of ``targets`` where ``email`` holds quota viewer access."""
        target_list: list[ProjectTarget] = list(targets)
        if not target_list:
            return frozenset()

        clean_email = _strip_account_prefix(email)
        target_fingerprint = ",".join(
            sorted(
                f"{t.org_id}:{t.folder_id}:{t.project_id}:{t.project_number}"
                for t in target_list
            )
        )
        cache_key = f"{clean_email}|{self.permission}|{target_fingerprint}"

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                if cached[1] > now:
                    return cached[0]
                # Stale-while-revalidate: serve cached projects in 0ms while
                # refreshing IAM bindings asynchronously in the background.
                if cache_key not in self._refreshing:
                    self._refreshing.add(cache_key)
                    _AUTHZ_POOL.submit(
                        self._background_refresh, cache_key, clean_email, target_list
                    )
                return cached[0]

        result = self._compute_allowed_projects(clean_email, target_list)
        with self._lock:
            self._cache[cache_key] = (result, time.monotonic() + self._cache_ttl)
        return result


_authorizer: Authorizer | None = None


def authorizer() -> Authorizer:
    global _authorizer
    if _authorizer is None:
        _authorizer = Authorizer()
    return _authorizer


def clear_authz_cache() -> None:
    if _authorizer is not None:
        _authorizer.clear_cache()
