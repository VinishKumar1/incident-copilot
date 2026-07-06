from __future__ import annotations

import base64
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import httpx

from .config import settings
from .models import Issue

# Stack-trace file references, e.g. "OrderMapper.java:42" or "service.py", line 88.
_FILE_REF = re.compile(r"\b([A-Za-z_][\w$]*\.(?:java|kt|scala|py|ts|tsx|js|go|rb|cs))\b(?::(\d+))?")
# Application exception class names (CamelCase ending in Exception/Error).
_EXC = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Exception|Error))\b")


def _search_phrases(issue: Issue) -> List[str]:
    """Stable, literal substrings of the log message to search for in code."""
    phrases: List[str] = []
    for sample in (issue.samples or [issue.title])[:3]:
        # Cut the stack-trace tail / JSON remnant, then the ": value" part of "msg: value".
        head = re.split(r"[|{]", sample, maxsplit=1)[0]
        head = re.split(r":\s", head, maxsplit=1)[0]
        head = re.sub(r"\s+", " ", head).strip()
        words = head.split(" ")
        if len(words) >= 2 and len(head) >= 14:
            phrases.append(" ".join(words[:8]))
    # Fall back to / augment with exception class names (e.g. WebClientResponseException).
    for sample in (issue.samples or [issue.title]):
        for m in _EXC.finditer(sample):
            phrases.append(m.group(1))
    # De-dupe preserving order, keep only meaningfully long terms.
    seen, out = set(), []
    for p in phrases:
        k = p.lower()
        if k not in seen and len(p) >= 8:
            seen.add(k)
            out.append(p)
    return out[:3]


def _file_refs(issue: Issue) -> List[tuple]:
    refs = []
    for sample in (issue.samples or [issue.title]):
        for m in _FILE_REF.finditer(sample):
            refs.append((m.group(1), int(m.group(2)) if m.group(2) else None))
    # de-dupe by filename
    seen, out = set(), []
    for fn, ln in refs:
        if fn not in seen:
            seen.add(fn)
            out.append((fn, ln))
    return out[:4]


class GitHubClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None

    def _tok(self) -> str:
        if self._token is not None:
            return self._token
        tok = settings.github_token
        if not tok:
            try:  # convenience for local dev: reuse the gh CLI's token
                tok = subprocess.run(
                    ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
                ).stdout.strip()
            except Exception:
                tok = ""
        self._token = tok
        return tok

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._tok()}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def enabled(self) -> bool:
        return bool(self._tok())

    async def repo_info(self, client: httpx.AsyncClient, repo: str) -> Optional[dict]:
        r = await client.get(f"{settings.github_api}/repos/{settings.github_org}/{repo}", headers=self._headers())
        if r.status_code == 200:
            return r.json()
        return None

    async def search_code(self, client: httpx.AsyncClient, repo: str, phrase: str) -> List[dict]:
        q = f'"{phrase}" repo:{settings.github_org}/{repo}'
        r = await client.get(
            f"{settings.github_api}/search/code",
            headers=self._headers("application/vnd.github.text-match+json"),
            params={"q": q, "per_page": 5},
        )
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
        out = []
        for it in items:
            frags = [tm.get("fragment", "") for tm in it.get("text_matches", [])]
            out.append({"path": it["path"], "html_url": it.get("html_url", ""), "fragments": frags})
        return out

    async def get_file(self, client: httpx.AsyncClient, repo: str, path: str, ref: str, around: Optional[int]) -> Optional[str]:
        r = await client.get(
            f"{settings.github_api}/repos/{settings.github_org}/{repo}/contents/{path}",
            headers=self._headers("application/vnd.github.raw+json"),
            params={"ref": ref},
        )
        if r.status_code != 200:
            return None
        text = r.text
        lines = text.splitlines()
        maxn = settings.code_match_max_file_lines
        if around and len(lines) > maxn:
            lo = max(0, around - maxn // 2)
            hi = min(len(lines), lo + maxn)
            window = lines[lo:hi]
            return f"// lines {lo + 1}-{hi} of {path}\n" + "\n".join(window)
        return "\n".join(lines[:maxn])

    async def find_tree_path(self, client: httpx.AsyncClient, repo: str, ref: str, filename: str) -> Optional[str]:
        r = await client.get(
            f"{settings.github_api}/repos/{settings.github_org}/{repo}/git/trees/{ref}",
            headers=self._headers(),
            params={"recursive": "1"},
        )
        if r.status_code != 200:
            return None
        for node in r.json().get("tree", []):
            if node.get("type") == "blob" and node.get("path", "").endswith("/" + filename) or node.get("path") == filename:
                return node["path"]
        return None

    async def gather_context(self, issue: Issue) -> dict:
        """Find code likely related to the issue: search for logged message strings and
        resolve any stack-trace file references, then fetch those files' relevant parts."""
        repo = issue.service
        if not self.enabled:
            return {"found": False, "reason": "no_github_token", "repo": repo}

        async with httpx.AsyncClient(timeout=25.0) as client:
            info = await self.repo_info(client, repo)
            if info is None:
                return {"found": False, "reason": "repo_not_found", "repo": repo}
            ref = info.get("default_branch", "main")
            repo_url = info.get("html_url", "")

            candidates: Dict[str, dict] = {}  # path -> {html_url, fragments, line}

            # 1) search for the logged message literals
            for phrase in _search_phrases(issue):
                for hit in await self.search_code(client, repo, phrase):
                    c = candidates.setdefault(hit["path"], {"html_url": hit["html_url"], "fragments": [], "line": None})
                    c["fragments"].extend(hit["fragments"])

            # 2) resolve stack-trace file references
            for filename, line in _file_refs(issue):
                path = await self.find_tree_path(client, repo, ref, filename)
                if path:
                    c = candidates.setdefault(path, {"html_url": f"{repo_url}/blob/{ref}/{path}", "fragments": [], "line": None})
                    if line:
                        c["line"] = line

            # 3) fetch contents for the top candidates
            files = []
            for path, meta in list(candidates.items())[: settings.code_match_max_files]:
                content = await self.get_file(client, repo, path, ref, meta.get("line"))
                if content:
                    files.append(
                        {
                            "path": path,
                            "url": meta.get("html_url") or f"{repo_url}/blob/{ref}/{path}",
                            "line": meta.get("line"),
                            "content": content,
                            "fragments": meta.get("fragments", [])[:2],
                        }
                    )

            return {
                "found": bool(files),
                "repo": repo,
                "repo_url": repo_url,
                "default_branch": ref,
                "search_terms": _search_phrases(issue),
                "files": files,
            }

    # ---- write side (used by the "Fix it" feature) ----
    async def can_push(self, client: httpx.AsyncClient, repo: str) -> bool:
        info = await self.repo_info(client, repo)
        return bool(info and info.get("permissions", {}).get("push"))

    async def get_file_full(self, client: httpx.AsyncClient, repo: str, path: str, ref: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (full_content, blob_sha) for a file on a ref — needed to commit an update."""
        r = await client.get(
            f"{settings.github_api}/repos/{settings.github_org}/{repo}/contents/{path}",
            headers=self._headers(),
            params={"ref": ref},
        )
        if r.status_code != 200:
            return None, None
        j = r.json()
        try:
            content = base64.b64decode(j.get("content", "")).decode("utf-8", "replace")
        except Exception:
            return None, None
        return content, j.get("sha")

    async def _branch_sha(self, client: httpx.AsyncClient, repo: str, branch: str) -> Optional[str]:
        r = await client.get(
            f"{settings.github_api}/repos/{settings.github_org}/{repo}/git/ref/heads/{branch}",
            headers=self._headers(),
        )
        if r.status_code == 200:
            return r.json().get("object", {}).get("sha")
        return None

    async def create_fix_pr(
        self, repo: str, base: str, changes: List[dict], title: str, body: str
    ) -> dict:
        """changes: [{path, new_content, sha}]. Creates a branch off `base`, commits each
        file, and opens a DRAFT PR back into `base`. Returns {ok, pr_url, branch, error}."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            base_sha = await self._branch_sha(client, repo, base)
            if base_sha is None:
                return {"ok": False, "error": f"base branch '{base}' not found in {repo}"}

            org = settings.github_org
            branch = f"ai-fix/{base}-{int(time.time())}"
            cr = await client.post(
                f"{settings.github_api}/repos/{org}/{repo}/git/refs",
                headers=self._headers(),
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )
            if cr.status_code not in (200, 201):
                return {"ok": False, "error": f"could not create branch ({cr.status_code}): {cr.text[:200]}"}

            for ch in changes:
                cm = await client.put(
                    f"{settings.github_api}/repos/{org}/{repo}/contents/{ch['path']}",
                    headers=self._headers(),
                    json={
                        "message": f"fix: {title}\n\n{ch['path']}",
                        "content": base64.b64encode(ch["new_content"].encode("utf-8")).decode("ascii"),
                        "branch": branch,
                        "sha": ch["sha"],
                    },
                )
                if cm.status_code not in (200, 201):
                    return {"ok": False, "error": f"commit failed for {ch['path']} ({cm.status_code}): {cm.text[:200]}", "branch": branch}

            pr = await client.post(
                f"{settings.github_api}/repos/{org}/{repo}/pulls",
                headers=self._headers(),
                json={"title": title, "head": branch, "base": base, "body": body, "draft": True},
            )
            if pr.status_code not in (200, 201):
                return {"ok": False, "error": f"PR creation failed ({pr.status_code}): {pr.text[:200]}", "branch": branch}
            j = pr.json()
            return {"ok": True, "pr_url": j.get("html_url", ""), "pr_number": j.get("number"), "branch": branch}


github_client = GitHubClient()
