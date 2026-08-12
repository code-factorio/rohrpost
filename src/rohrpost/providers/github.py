"""GitHub provider: sync tickets ↔ GitHub issues (spec §8.5).

The **`gh` CLI is the preferred transport** — it is pre-authenticated in agent
environments (`gh auth login`), handles pagination, and is present wherever
developers run `rp`. When `gh` is not on PATH (or not authenticated) the provider
falls back to the REST API via :class:`httpx.Client` (Bearer token from
``GITHUB_TOKEN`` / ``ROHRPOST_GITHUB_TOKEN``).

GitHub issues expose ``title``, ``body``, ``state`` (``open``/``closed``) and
``labels``. The field mapping in ``config.toml`` translates between those and the
local vocabulary, including the many-to-one status → state map.

The client takes an injectable ``client`` (httpx, for :class:`httpx.MockTransport`
tests) and/or a ``gh_runner`` (returns ``gh`` stdout) so tests never touch the
network or require the binary.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from rohrpost.exceptions import RemoteItemNotFoundError


def _token(env: Mapping[str, str]) -> str | None:
    return env.get("GITHUB_TOKEN") or env.get("ROHRPOST_GITHUB_TOKEN")


class GitHubProvider:
    """Fetch/push GitHub issues as ``{local_field: value}`` maps.

    Prefers the ``gh`` CLI (``gh api``); falls back to httpx REST when ``gh`` is
    unavailable.
    """

    remote = "github"

    def __init__(
        self,
        config: dict[str, Any],
        *,
        client: httpx.Client | None = None,
        env: Mapping[str, str] | None = None,
        gh_runner: Callable[[list[str]], str] | None = None,
        prefer_gh: bool | None = None,
    ) -> None:
        self.config = config
        self.repo = str(config.get("repo", ""))
        self.base_url = str(config.get("url", "https://api.github.com")).rstrip("/")
        self.fields = config.get("fields", {}) if isinstance(config.get("fields"), dict) else {}
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._client = client
        self._gh_runner = gh_runner
        # Prefer gh when it is available (overridable for tests / force-REST).
        self._prefer_gh = prefer_gh if prefer_gh is not None else shutil.which("gh") is not None

    # -- transport selection ------------------------------------------------
    def _try_gh(self, args: list[str]) -> dict[str, Any] | None:
        """Run ``gh api`` and return parsed JSON, or ``None`` if gh is unusable.

        Separate except clauses (not a tuple) keep this robust to environments
        whose formatter mishandles ``except (A, B):``.
        """
        try:
            if self._gh_runner is not None:
                stdout = self._gh_runner(args)
            else:
                proc = subprocess.run(
                    ["gh", *args], check=True, capture_output=True, text=True, timeout=30
                )
                stdout = proc.stdout
            return json.loads(stdout) if stdout.strip() else {}
        except FileNotFoundError:
            return None
        except subprocess.CalledProcessError:
            return None  # gh present but errored (e.g. not authenticated)
        except json.JSONDecodeError:
            return None

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        token = _token(self._env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _issue_path(self, ref: str) -> str:
        return f"repos/{self.repo}/issues/{ref}"

    # -- field mapping ------------------------------------------------------
    def _to_remote(self, local_fields: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for local_name, remote_name in self._scalar_map().items():
            if local_name in local_fields:
                payload[remote_name] = local_fields[local_name]
        if "labels" in local_fields and "labels" in self.fields:
            payload["labels"] = list(local_fields["labels"])
        if "status" in local_fields:
            state = self._status_to_state(str(local_fields["status"]))
            if state is not None:
                payload["state"] = state
        return payload

    def _to_local(self, issue: dict[str, Any]) -> dict[str, Any]:
        local: dict[str, Any] = {}
        for local_name, remote_name in self._scalar_map().items():
            if remote_name in issue:
                local[local_name] = issue[remote_name]
        if "labels" in self.fields and "labels" in issue:
            local["labels"] = sorted(label.get("name", "") for label in issue["labels"])
        state = issue.get("state")
        if state is not None:
            status = self._state_to_status(str(state))
            if status is not None:
                local["status"] = status
        return local

    def _scalar_map(self) -> dict[str, str]:
        return {
            name: str(target) for name, target in self.fields.items() if isinstance(target, str)
        }

    def _status_map(self) -> dict[str, str]:
        sm = self.fields.get("status")
        return sm if isinstance(sm, dict) else {}

    def _status_to_state(self, status: str) -> str | None:
        return self._status_map().get(status)

    def _state_to_status(self, state: str) -> str | None:
        for status, mapped in self._status_map().items():
            if mapped == state:
                return str(status)
        return None

    # -- network (gh preferred, httpx fallback) -----------------------------
    def fetch(self, ref: str) -> dict[str, Any]:
        if self._prefer_gh:
            data = self._try_gh(["api", self._issue_path(ref)])
            if data is not None:
                return self._to_local(data)
        resp = self._http().get(f"{self.base_url}/{self._issue_path(ref)}", headers=self._headers())
        if resp.status_code == 404:
            raise RemoteItemNotFoundError(f"GitHub issue {ref} no longer exists")
        resp.raise_for_status()
        return self._to_local(resp.json())

    def push(self, ref: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = self._to_remote(fields)
        if self._prefer_gh:
            args = ["api", "-X", "PATCH", self._issue_path(ref)]
            args.extend(_gh_field_args(payload))
            data = self._try_gh(args)
            if data is not None:
                return self._to_local(data)
        resp = self._http().patch(
            f"{self.base_url}/{self._issue_path(ref)}", json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        return self._to_local(resp.json())


def _gh_field_args(payload: dict[str, Any]) -> list[str]:
    """Translate a flat ``{field: value}`` payload to repeated ``gh api -f`` args."""
    args: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            for item in value:
                args.extend(["-f", f"{key}[]={item}"])
        else:
            args.extend(["-f", f"{key}={value}"])
    return args


__all__ = ["GitHubProvider"]
