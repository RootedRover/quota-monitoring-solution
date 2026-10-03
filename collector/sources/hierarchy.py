"""Resource-hierarchy discovery via Cloud Resource Manager v3.

v5 used CRM **v1** ``projects.list`` with no filter, which returns every
project the caller can see anywhere -- not the projects in the configured org
-- and silently ignored the ``folders`` configuration variable entirely.
This module walks the actual tree so that org and folder rollups mean
something.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import google.auth
import google.auth.transport.requests
import requests

_LOG = logging.getLogger(__name__)

_CRM = "https://cloudresourcemanager.googleapis.com/v3"


@dataclass(frozen=True)
class ProjectNode:
    project_id: str
    project_number: str
    display_name: str
    parent: str
    folder_id: str | None
    org_id: str | None
    ancestry: tuple[str, ...]


class HierarchySource:
    def __init__(
        self,
        *,
        billing_project: str,
        session: requests.Session | None = None,
        timeout: int = 60,
    ) -> None:
        self.billing_project = billing_project
        self.timeout = timeout
        self._session = session or requests.Session()
        self._credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )

    def _token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(google.auth.transport.requests.Request())
        return self._credentials.token

    def _list(self, path: str, parent: str, key: str) -> list[dict]:
        items: list[dict] = []
        page_token = ""
        while True:
            params = {"parent": parent, "pageSize": "500"}
            if page_token:
                params["pageToken"] = page_token
            response = self._session.get(
                f"{_CRM}/{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "x-goog-user-project": self.billing_project,
                },
                timeout=self.timeout,
            )
            payload = response.json()
            if "error" in payload:
                _LOG.warning(
                    "%s under %s failed: %s", path, parent, payload["error"].get("message")
                )
                return items
            items.extend(payload.get(key, []))
            page_token = payload.get("nextPageToken", "")
            if not page_token:
                break
        return items

    def walk(self, root: str) -> list[ProjectNode]:
        """Depth-first walk of ``root`` (``organizations/123`` or ``folders/456``)."""
        projects: list[ProjectNode] = []
        org_id = root.split("/")[1] if root.startswith("organizations/") else None
        self._walk(root, (root,), org_id, projects)
        return projects

    def _walk(
        self,
        parent: str,
        ancestry: tuple[str, ...],
        org_id: str | None,
        out: list[ProjectNode],
    ) -> None:
        for raw in self._list("projects", parent, "projects"):
            if raw.get("state") != "ACTIVE":
                continue
            folder_id = next(
                (a.split("/")[1] for a in reversed(ancestry) if a.startswith("folders/")),
                None,
            )
            out.append(
                ProjectNode(
                    project_id=raw["projectId"],
                    project_number=raw["name"].split("/")[1],
                    display_name=raw.get("displayName", raw["projectId"]),
                    parent=parent,
                    folder_id=folder_id,
                    org_id=org_id,
                    ancestry=ancestry,
                )
            )

        for raw in self._list("folders", parent, "folders"):
            if raw.get("state") != "ACTIVE":
                continue
            self._walk(raw["name"], ancestry + (raw["name"],), org_id, out)
