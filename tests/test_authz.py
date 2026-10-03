"""Unit tests for caller authentication and per-user project authorization."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from dashboard import authz
from dashboard.authz import (
    Authorizer,
    ProjectTarget,
    UnauthenticatedError,
    _iam_member_for_email,
    _parse_resource_grants,
    authenticate_request,
    verify_bearer_jwt,
    verify_iap_jwt,
)
from dashboard.queries import Repository, clear_cache


def _make_request(headers: dict[str, str]) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
    }
    return Request(scope)


def test_strip_prefix_and_iam_member_selector() -> None:
    assert _iam_member_for_email("accounts.google.com:Alice@Example.com") == (
        "user:alice@example.com"
    )
    assert _iam_member_for_email(
        "qms-collector@krishngupt-argolis.iam.gserviceaccount.com"
    ) == ("serviceAccount:qms-collector@krishngupt-argolis.iam.gserviceaccount.com")


def test_verify_iap_jwt_accepts_valid_assertion(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_verify_token(
        jwt_str: str, req: Any, audience: Any = None, certs_url: str = ""
    ) -> dict:
        assert jwt_str == "signed-iap-jwt"
        assert certs_url == authz.IAP_CERTS_URL
        return {
            "iss": "https://cloud.google.com/iap",
            "sub": "accounts.google.com:12345",
            "email": "accounts.google.com:Admin@Krishngupt.Altostrat.com",
        }

    monkeypatch.setattr(authz.id_token, "verify_token", fake_verify_token)
    caller = verify_iap_jwt("signed-iap-jwt")
    assert caller.email == "admin@krishngupt.altostrat.com"
    assert caller.auth_source == "iap"
    assert caller.sub == "accounts.google.com:12345"


def test_verify_iap_jwt_rejects_wrong_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authz.id_token,
        "verify_token",
        lambda *_a, **_kw: {"iss": "https://evil.example.com", "email": "a@b.com"},
    )
    with pytest.raises(UnauthenticatedError, match="Unexpected IAP JWT issuer"):
        verify_iap_jwt("bad-issuer-token")


def test_verify_bearer_jwt_accepts_google_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        authz.id_token,
        "verify_oauth2_token",
        lambda *_a, **_kw: {
            "iss": "https://accounts.google.com",
            "email": "viewer@example.com",
            "sub": "999",
        },
    )
    caller = verify_bearer_jwt("oidc-token")
    assert caller.email == "viewer@example.com"
    assert caller.auth_source == "bearer"


def test_unsigned_email_header_alone_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An attacker sending X-Goog-Authenticated-User-Email without a signed JWT must fail."""
    monkeypatch.delenv("QMS_AUTHZ_MODE", raising=False)
    req = _make_request(
        {"x-goog-authenticated-user-email": "accounts.google.com:admin@corp.com"}
    )
    with pytest.raises(UnauthenticatedError):
        authenticate_request(req)


def test_dev_mode_blocked_when_k_service_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QMS_AUTHZ_MODE", "dev")
    monkeypatch.setenv("QMS_DEV_USER_EMAIL", "dev@example.com")
    monkeypatch.setenv("K_SERVICE", "qms-dashboard")
    req = _make_request({})
    with pytest.raises(UnauthenticatedError):
        authenticate_request(req)


def test_parse_resource_grants_extracts_org_folder_and_project() -> None:
    payload = {
        "mainAnalysis": {
            "analysisResults": [
                {
                    "attachedResourceFullName": (
                        "//cloudresourcemanager.googleapis.com/folders/610643910605"
                    ),
                    "resourceList": [
                        {
                            "fullResourceName": (
                                "//cloudresourcemanager.googleapis.com/folders/610643910605"
                            )
                        },
                        {
                            "fullResourceName": (
                                "//cloudresourcemanager.googleapis.com/projects/114680847754"
                            )
                        },
                    ],
                }
            ]
        }
    }
    orgs, folders, projs = _parse_resource_grants(payload)
    assert orgs == set()
    assert folders == {"610643910605"}
    assert projs == {"114680847754"}


def test_authorizer_org_folder_and_project_scoping() -> None:
    targets = [
        ProjectTarget(
            project_id="krishngupt-argolis",
            project_number="114680847754",
            folder_id="",
            org_id="957650833838",
        ),
        ProjectTarget(
            project_id="google-mpf-0bcvg17tfnnt",
            project_number="222222222222",
            folder_id="610643910605",
            org_id="957650833838",
        ),
    ]

    # Case 1: Folder-scoped user only sees projects inside folder 610643910605.
    session_folder = MagicMock()
    resp_folder = MagicMock(status_code=200)
    resp_folder.json.return_value = {
        "mainAnalysis": {
            "analysisResults": [
                {
                    "attachedResourceFullName": (
                        "//cloudresourcemanager.googleapis.com/folders/610643910605"
                    ),
                }
            ]
        }
    }
    session_folder.get.return_value = resp_folder
    auth_folder = Authorizer(session=session_folder)
    assert auth_folder.allowed_projects("folder-owner@example.com", targets) == frozenset(
        {"google-mpf-0bcvg17tfnnt"}
    )

    # Case 2: Project-number grant matches project_id via project_number.
    session_proj = MagicMock()
    resp_proj = MagicMock(status_code=200)
    resp_proj.json.return_value = {
        "mainAnalysis": {
            "analysisResults": [
                {
                    "attachedResourceFullName": (
                        "//cloudresourcemanager.googleapis.com/projects/114680847754"
                    ),
                }
            ]
        }
    }
    session_proj.get.return_value = resp_proj
    auth_proj = Authorizer(session=session_proj)
    assert auth_proj.allowed_projects("proj-owner@example.com", targets) == frozenset(
        {"krishngupt-argolis"}
    )

    # Case 3: Org-level grant authorizes every project under that org.
    session_org = MagicMock()
    resp_org = MagicMock(status_code=200)
    resp_org.json.return_value = {
        "mainAnalysis": {
            "analysisResults": [
                {
                    "attachedResourceFullName": (
                        "//cloudresourcemanager.googleapis.com/organizations/957650833838"
                    ),
                }
            ]
        }
    }
    session_org.get.return_value = resp_org
    auth_org = Authorizer(session=session_org)
    assert auth_org.allowed_projects("org-admin@example.com", targets) == frozenset(
        {"krishngupt-argolis", "google-mpf-0bcvg17tfnnt"}
    )


def test_authorizer_falls_back_when_org_scope_forbidden() -> None:
    """If the dashboard SA lacks org-level cloudasset.viewer, it falls back to folder/project."""
    targets = [
        ProjectTarget(
            project_id="proj-a",
            project_number="101",
            folder_id="500",
            org_id="900",
        ),
        ProjectTarget(
            project_id="proj-b",
            project_number="102",
            folder_id="",
            org_id="900",
        ),
    ]

    def fake_get(url: str, params: Any = None, timeout: int = 15) -> MagicMock:
        del params, timeout
        resp = MagicMock()
        if "organizations/900:analyzeIamPolicy" in url:
            resp.status_code = 403
            resp.text = "Permission denied on org"
            return resp
        if "folders/500:analyzeIamPolicy" in url:
            resp.status_code = 200
            resp.json.return_value = {
                "mainAnalysis": {
                    "analysisResults": [
                        {
                            "attachedResourceFullName": (
                                "//cloudresourcemanager.googleapis.com/folders/500"
                            )
                        }
                    ]
                }
            }
            return resp
        if "projects/proj-b:analyzeIamPolicy" in url:
            resp.status_code = 200
            resp.json.return_value = {"mainAnalysis": {}}
            return resp
        raise AssertionError(f"unexpected URL {url}")

    session = MagicMock()
    session.get.side_effect = fake_get
    auth = Authorizer(session=session)
    assert auth.allowed_projects("team-lead@example.com", targets) == frozenset({"proj-a"})


def test_repository_filters_all_views_and_kpis_by_allowed_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_cache()
    monkeypatch.setattr("dashboard.queries.bigquery.Client", lambda **_kw: MagicMock())
    r = Repository(project="host-proj", dataset="quota_monitoring", location="asia-south1")

    monkeypatch.setattr(
        r,
        "_all_risk",
        lambda: [
            {
                "org_id": "900",
                "folder_id": "",
                "project_id": "proj-a",
                "project_number": "101",
                "service": "compute.googleapis.com",
                "quota_metric": "compute.googleapis.com/cpus",
                "peak_ratio_30d": 0.95,
                "peak_ratio_7d": 0.90,
                "last_seen": dt.date(2026, 10, 1),
            },
            {
                "org_id": "900",
                "folder_id": "500",
                "project_id": "proj-b",
                "project_number": "102",
                "service": "iam.googleapis.com",
                "quota_metric": "iam.googleapis.com/service_accounts",
                "peak_ratio_30d": 0.85,
                "peak_ratio_7d": 0.80,
                "last_seen": dt.date(2026, 10, 2),
            },
        ],
    )
    monkeypatch.setattr(
        r,
        "_all_movers",
        lambda: [
            {"project_id": "proj-a", "delta": 0.25},
            {"project_id": "proj-b", "delta": 0.15},
        ],
    )
    monkeypatch.setattr(
        r,
        "_all_hierarchy",
        lambda: [
            {"org_id": "900", "folder_id": "", "project_id": "proj-a", "quotas_tracked": 1},
            {"org_id": "900", "folder_id": "500", "project_id": "proj-b", "quotas_tracked": 1},
        ],
    )
    monkeypatch.setattr(
        r,
        "_all_quality_by_project",
        lambda: [
            {
                "project_id": "proj-a",
                "flag": "LIMIT_SCOPE_NOT_COMPARABLE",
                "is_comparable": False,
                "row_count": 10,
                "quota_count": 2,
            },
            {
                "project_id": "proj-b",
                "flag": "LIMIT_SCOPE_NOT_COMPARABLE",
                "is_comparable": False,
                "row_count": 20,
                "quota_count": 5,
            },
        ],
    )
    monkeypatch.setattr(r, "freshness", lambda: {"rows_total": 30})

    # Scoped to proj-a only:
    snap_a = r.snapshot(allowed_projects={"proj-a"})
    assert [row["project_id"] for row in snap_a["risk"]] == ["proj-a"]
    assert [row["project_id"] for row in snap_a["movers"]] == ["proj-a"]
    assert [row["project_id"] for row in snap_a["hierarchy"]] == ["proj-a"]
    assert snap_a["quality"] == [
        {
            "flag": "LIMIT_SCOPE_NOT_COMPARABLE",
            "is_comparable": False,
            "row_count": 10,
            "quota_count": 2,
        }
    ]
    assert snap_a["summary"] == {
        "tracked": 1,
        "critical": 1,
        "warning": 0,
        "projects": 1,
        "services": 1,
        "last_seen": dt.date(2026, 10, 1),
        "excluded": 2,
    }

    # Empty allowed_projects set (user has access to 0 projects) must return 0 rows!
    snap_none = r.snapshot(allowed_projects=frozenset())
    assert snap_none["risk"] == []
    assert snap_none["movers"] == []
    assert snap_none["hierarchy"] == []
    assert snap_none["quality"] == []
    assert snap_none["summary"]["tracked"] == 0
    assert snap_none["summary"]["projects"] == 0
    assert snap_none["summary"]["excluded"] == 0


def test_authorizer_crm_get_iam_policy_fallback_when_cloud_asset_forbidden() -> None:
    """When analyzeIamPolicy returns 403, CRM v3 getIamPolicy fallback resolves direct bindings."""
    targets = [
        ProjectTarget(
            project_id="krishngupt-argolis",
            project_number="114680847754",
            folder_id="",
            org_id="957650833838",
        ),
    ]
    session = MagicMock()
    get_resp = MagicMock(status_code=403, text="Permission denied")
    session.get.return_value = get_resp

    def fake_post(url: str, json: Any = None, timeout: int = 15) -> MagicMock:
        del json, timeout
        resp = MagicMock(status_code=200)
        if "organizations/957650833838:getIamPolicy" in url:
            resp.json.return_value = {
                "bindings": [
                    {
                        "role": "roles/cloudquotas.viewer",
                        "members": ["user:admin@krishngupt.altostrat.com"],
                    }
                ]
            }
        else:
            resp.json.return_value = {"bindings": []}
        return resp

    session.post.side_effect = fake_post
    auth = Authorizer(session=session)
    assert auth.allowed_projects("admin@krishngupt.altostrat.com", targets) == frozenset(
        {"krishngupt-argolis"}
    )
    assert auth.allowed_projects("other@krishngupt.altostrat.com", targets) == frozenset()


def test_verify_iap_jwt_real_es256_cryptographic_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end ES256 signature verification using google-auth + cryptography (no pyjwt)."""
    import time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from google.auth import crypt, jwt

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )

    signer = crypt.ES256Signer.from_string(private_pem, key_id="iap-key-1")
    now = int(time.time())
    aud = "/projects/114680847754/locations/asia-south1/services/qms-dashboard"
    signed_jwt = jwt.encode(
        signer,
        {
            "iss": "https://cloud.google.com/iap",
            "aud": aud,
            "sub": "accounts.google.com:111222333",
            "email": "admin@krishngupt.altostrat.com",
            "iat": now - 10,
            "exp": now + 300,
        },
    ).decode("utf-8")

    monkeypatch.setattr(
        authz.id_token,
        "_fetch_certs",
        lambda _req, certs_url: (
            {"iap-key-1": public_pem}
            if certs_url == "https://www.gstatic.com/iap/verify/public_key"
            else {}
        ),
    )

    caller = verify_iap_jwt(signed_jwt, audience=aud)
    assert caller.email == "admin@krishngupt.altostrat.com"
    assert caller.auth_source == "iap"


def test_authorizer_crm_supplements_empty_200_from_cloud_asset_and_cascades_to_project() -> (
    None
):
    """When CAI returns HTTP 200 with empty analysisResults, CRM getIamPolicy supplements and cascades."""
    targets = [
        ProjectTarget(
            project_id="krishngupt-argolis",
            project_number="114680847754",
            folder_id="",
            org_id="957650833838",
        ),
        ProjectTarget(
            project_id="google-mpf-0bcvg17tfnnt",
            project_number="964047330988",
            folder_id="610643910605",
            org_id="957650833838",
        ),
    ]
    session = MagicMock()
    # Simulate CAI returning 200 OK with empty analysisResults (e.g. indexing lag)
    empty_cai_resp = MagicMock(status_code=200)
    empty_cai_resp.json.return_value = {"mainAnalysis": {"fullyExplored": True}}
    session.get.return_value = empty_cai_resp

    # Simulate user having roles/cloudquotas.admin only on projects/krishngupt-argolis
    def fake_post(url: str, json: Any = None, timeout: int = 15) -> MagicMock:
        del json, timeout
        resp = MagicMock(status_code=200)
        if "projects/krishngupt-argolis:getIamPolicy" in url:
            resp.json.return_value = {
                "bindings": [
                    {
                        "role": "roles/cloudquotas.admin",
                        "members": ["user:workload-owner@krishngupt.altostrat.com"],
                    }
                ]
            }
        elif "organizations/957650833838:getIamPolicy" in url:
            resp.json.return_value = {
                "bindings": [
                    {
                        "role": "roles/cloudquotas.viewer",
                        "members": ["user:admin@krishngupt.altostrat.com"],
                    }
                ]
            }
        else:
            resp.json.return_value = {"bindings": []}
        return resp

    session.post.side_effect = fake_post
    auth = Authorizer(session=session)
    assert auth.allowed_projects("admin@krishngupt.altostrat.com", targets) == frozenset(
        {"krishngupt-argolis", "google-mpf-0bcvg17tfnnt"}
    )
    assert auth.allowed_projects(
        "workload-owner@krishngupt.altostrat.com", targets
    ) == frozenset({"krishngupt-argolis"})
    assert auth.allowed_projects("nobody@krishngupt.altostrat.com", targets) == frozenset()


def test_favicon_and_logo_assets_served() -> None:
    """Verify the extracted transparent logo and favicons exist and are wired into base.html."""
    from dashboard.app import STATIC_DIR, favicon_ico, favicon_png, static_logo_png

    for asset in ("favicon.ico", "favicon.png", "logo.png"):
        path = STATIC_DIR / asset
        assert path.is_file()
        assert path.stat().st_size > 100

    assert favicon_ico().media_type == "image/x-icon"
    assert favicon_png().media_type == "image/png"
    assert static_logo_png().media_type == "image/png"

    base_html = (STATIC_DIR.parent / "templates" / "base.html").read_text()
    assert 'href="/favicon.png?v=6"' in base_html
    assert 'href="/favicon.ico?v=6"' in base_html
    assert 'src="/static/logo.png?v=6"' in base_html
