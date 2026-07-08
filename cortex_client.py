"""Thin REST client for Cortex — the sibling memory-server project.

Cortex (~/Desktop/App lab/cortex) runs as its own FastAPI process (default
http://localhost:8000). SlackMind talks to it purely over HTTP — it does NOT
try to spawn or speak Cortex's MCP stdio server directly, since that's a
different process with a different transport than a Slack bot process can
usefully attach to.

Every method here mirrors an exact endpoint from cortex/main.py:

    POST /remember   {namespace, agent, content, kind, salience, source}
                      -> {"memory": {...}}
    POST /recall      {namespace, agent, query, k}
                      -> {"memories": [...]}
    POST /chat        {namespace, agent, message, system}
                      -> {"answer": str, "retrieved": [...], "stored": [...]}
                      (503 if Cortex has no DASHSCOPE_API_KEY configured)
    GET  /memories    ?namespace=...
                      -> {"namespace": str, "memories": [...]}
    GET  /audit       ?namespace=...&memory_id=...
                      -> {"namespace": str, "audit": [...]}
    GET  /health      -> {"status": "ok", "version": ..., "qwen_key": bool, "store_dir": ...}

All methods fail soft: on any network/HTTP error they log and return None
(or an empty list/dict as appropriate) rather than raising, so a Cortex
outage degrades SlackMind's answers instead of crashing the Slack process.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

log = logging.getLogger("slackmind.cortex")

DEFAULT_TIMEOUT = 10  # seconds — Cortex's /chat path calls an LLM, give it room
DEFAULT_AGENT = "slackmind"


class CortexClient:
    def __init__(self, base_url: str, agent: str = DEFAULT_AGENT, timeout: int = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.timeout = timeout

    # -- internal helper -----------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> Optional[requests.Response]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(method, url, timeout=self.timeout, **kwargs)
            return resp
        except requests.exceptions.RequestException as e:
            log.warning("Cortex request failed: %s %s -> %s", method, url, e)
            return None

    # -- health ----------------------------------------------------------
    def health(self) -> Optional[dict]:
        resp = self._request("GET", "/health")
        if resp is None or resp.status_code != 200:
            return None
        return resp.json()

    # -- POST /remember ---------------------------------------------------
    def remember(
        self,
        namespace: str,
        content: str,
        kind: str = "fact",
        salience: float = 0.6,
        source: str = "",
        agent: Optional[str] = None,
    ) -> Optional[dict]:
        body = {
            "namespace": namespace,
            "agent": agent or self.agent,
            "content": content,
            "kind": kind,
            "salience": salience,
            "source": source,
        }
        resp = self._request("POST", "/remember", json=body)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                log.warning("remember() failed: %s %s", resp.status_code, resp.text[:300])
            return None
        return resp.json().get("memory")

    # -- POST /recall -----------------------------------------------------
    def recall(
        self,
        namespace: str,
        query: str,
        k: int = 5,
        agent: Optional[str] = None,
    ) -> list[dict]:
        body = {
            "namespace": namespace,
            "agent": agent or self.agent,
            "query": query,
            "k": k,
        }
        resp = self._request("POST", "/recall", json=body)
        if resp is None or resp.status_code != 200:
            if resp is not None:
                log.warning("recall() failed: %s %s", resp.status_code, resp.text[:300])
            return []
        return resp.json().get("memories", [])

    # -- POST /chat ---------------------------------------------------------
    def chat(
        self,
        namespace: str,
        message: str,
        system: Optional[str] = None,
        agent: Optional[str] = None,
    ) -> Optional[dict]:
        """Cortex's own recall+generate+store-in-one-call endpoint.

        Requires Cortex to have its own model key (DASHSCOPE_API_KEY) configured;
        returns None (rather than raising) on the 503 Cortex sends when it doesn't,
        or on any other failure, so callers can fall back cleanly.
        """
        body: dict[str, Any] = {
            "namespace": namespace,
            "agent": agent or self.agent,
            "message": message,
        }
        if system:
            body["system"] = system
        resp = self._request("POST", "/chat", json=body)
        if resp is None:
            return None
        if resp.status_code == 503:
            log.info("Cortex /chat unavailable (no model key configured on Cortex side)")
            return None
        if resp.status_code != 200:
            log.warning("chat() failed: %s %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()

    # -- GET /memories --------------------------------------------------
    def list_memories(self, namespace: str) -> list[dict]:
        resp = self._request("GET", "/memories", params={"namespace": namespace})
        if resp is None or resp.status_code != 200:
            return []
        return resp.json().get("memories", [])

    # -- GET /audit -------------------------------------------------------
    def audit(self, namespace: str, memory_id: Optional[str] = None) -> list[dict]:
        params = {"namespace": namespace}
        if memory_id:
            params["memory_id"] = memory_id
        resp = self._request("GET", "/audit", params=params)
        if resp is None or resp.status_code != 200:
            return []
        return resp.json().get("audit", [])
