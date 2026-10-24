"""Deploy history from GitHub. Read-only.

Per repo, the first of these that has any data is the deploy history:
1. Deployments API (what CD tools like Actions environments, Argo or Heroku record)
2. merged pull requests into the default branch
3. commits on the default branch

Env: GITHUB_TOKEN, GITHUB_REPOS ("owner/repo=service,..."), optional
GITHUB_ENVIRONMENT (only deployments to this environment, e.g. production),
GITHUB_API_URL (GitHub Enterprise), GITHUB_LOOKBACK_HOURS (window when none is given).
"""
import os
import re
from datetime import datetime, timedelta

import httpx

import tenant

import telemetry as tm
from redact import redact
from sources import parse_mapping
from sources.base import DeploysSource
from sources.httpkit import RateLimited, get, next_url

MAX_PAGES = 5  # per listing; 500 items is far more than an incident window needs
MAX_DIFF_CHARS = 4000  # the tool output cap is 6000 and JSON escaping adds some
MAX_FILE_CHARS = 1500  # one huge file must not crowd out the others
MAX_LISTED_FILES = 40

# Changes in these files are almost never the root cause and are often huge.
NOISE = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|go\.sum|"
                   r"Cargo\.lock|composer\.lock|Gemfile\.lock)$|(^|/)(vendor|dist|build|node_modules)/|"
                   r"\.min\.(js|css)$|\.snap$|\.svg$")
LOW_PRIORITY = re.compile(r"(^|/)(tests?|__tests__|spec|docs?)/|(_test|\.test|\.spec)\.\w+$|(^|/)test_[^/]+$|\.md$")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class GitHubDeploys(DeploysSource):
    def __init__(self, repos: dict[str, str], client: httpx.Client, environment: str = "", lookback_hours: float = 24):
        self.repos = repos  # "owner/repo" -> service name
        self.client = client
        self.environment = environment
        self.lookback = timedelta(hours=lookback_hours)
        self._notes: list[str] = []
        self._ranges: dict[str, tuple] = {}  # deploy id -> (repo, service, version, base sha, head sha)
        self._default_branch: dict[str, str] = {}

    @classmethod
    def from_env(cls):
        token = tenant.setting("GITHUB_TOKEN", "")
        repos = parse_mapping(tenant.setting("GITHUB_REPOS", ""))
        if not token or not repos:
            raise ValueError("LOKI_DEPLOYS_SOURCE=github needs GITHUB_TOKEN and GITHUB_REPOS")
        client = httpx.Client(
            base_url=tenant.setting("GITHUB_API_URL") or "https://api.github.com",  # compose passes ""
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=15,
        )
        return cls(repos, client, tenant.setting("GITHUB_ENVIRONMENT", ""),
                   float(tenant.setting("GITHUB_LOOKBACK_HOURS") or 24))

    def notes(self):
        return list(self._notes)

    # ---------- listing ----------

    def list_deploys(self, start, end):
        end = end or tm.now()  # tm.now, not the wall clock, so a replayed 'present' is respected
        start = start or end - self.lookback
        self._notes = []
        out = []
        for repo, service in self.repos.items():
            try:
                out += self._repo_deploys(repo, service, start, end)
            except RateLimited as e:
                # Same token for every repo, so the rest would fail too.
                self._notes.append(f"GitHub {e}: deploy list is partial, stopped at {repo}")
                break
            except httpx.HTTPError as e:
                self._notes.append(f"GitHub: could not read deploys of {repo}: {_short_error(e)}")
        return sorted(out, key=lambda d: d["deployed_at_dt"])

    def _repo_deploys(self, repo, service, start, end):
        raw, has_any = self._deployments(repo, start)
        if has_any:
            return self._from_deployments(repo, service, raw, start, end)
        branch = self._branch(repo)
        prs, has_any = self._merged_prs(repo, branch, start)
        if has_any:
            return [self._from_pr(repo, service, pr) for pr in prs if start <= _ts(pr["merged_at"]) <= end]
        return [self._from_commit(repo, service, c) for c in self._commits(repo, branch, start, end)]

    def _paged(self, path, params, stop):
        """Yield items page by page until stop(item) says the rest is older than we need.
        Returns quietly at MAX_PAGES, with a note, rather than walking all history."""
        url, page = path, 0
        while url:
            resp = get(self.client, url, params if page == 0 else None)  # next links carry the params
            items = resp.json()
            for item in items:
                if stop(item):
                    return
                yield item
            page += 1
            url = next_url(resp)
            if url and page >= MAX_PAGES:
                self._notes.append(f"GitHub: {path} has more than {MAX_PAGES} pages; older entries skipped")
                return

    def _deployments(self, repo, start):
        """Deployments newest first, down to the first one older than start (the base for a diff)."""
        params = {"per_page": 100}
        if self.environment:
            params["environment"] = self.environment
        out, older = [], []

        def stop(d):
            if _ts(d["created_at"]) >= start:
                return False
            older.append(d)
            # Stop once every environment in the window has its previous deployment (the diff base).
            return {x["environment"] for x in out} <= {x["environment"] for x in older}

        for d in self._paged(f"/repos/{repo}/deployments", params, stop):
            if _ts(d["created_at"]) >= start:
                out.append(d)
        all_seen = out + older
        return all_seen, bool(all_seen)

    def _from_deployments(self, repo, service, raw, start, end):
        deploys = []
        raw = sorted(raw, key=lambda d: d["created_at"])
        previous_sha: dict[str, str] = {}  # environment -> sha of the deployment before
        for d in raw:
            base = previous_sha.get(d["environment"])
            previous_sha[d["environment"]] = d["sha"]
            at = _ts(d["created_at"])
            if not (start <= at <= end):
                continue
            deploy_id = f"{repo}:deploy:{d['id']}"
            ref = d.get("ref") or ""
            version = ref if ref and ref != d["sha"] else d["sha"][:7]
            message = d.get("description") or f"{d.get('task') or 'deploy'} {version} to {d['environment']}"
            self._ranges[deploy_id] = (repo, service, version, base if base != d["sha"] else None, d["sha"])
            deploys.append(_deploy(deploy_id, service, version, (d.get("creator") or {}).get("login", "?"), message, at))
        return deploys

    def _branch(self, repo):
        if repo not in self._default_branch:
            self._default_branch[repo] = get(self.client, f"/repos/{repo}").json()["default_branch"]
        return self._default_branch[repo]

    def _merged_prs(self, repo, branch, start):
        params = {"state": "closed", "base": branch, "sort": "updated", "direction": "desc", "per_page": 100}
        seen = []
        # merged_at <= updated_at, so once updated_at is before start nothing older can be in the window.
        for pr in self._paged(f"/repos/{repo}/pulls", params, lambda pr: _ts(pr["updated_at"]) < start):
            seen.append(pr)
        has_any = bool(seen) or bool(get(self.client, f"/repos/{repo}/pulls",
                                         {"state": "closed", "base": branch, "per_page": 1}).json())
        return [pr for pr in seen if pr.get("merged_at")], has_any

    def _from_pr(self, repo, service, pr):
        deploy_id = f"{repo}:pr:{pr['number']}"
        version = f"#{pr['number']} {(pr.get('merge_commit_sha') or '')[:7]}".strip()
        self._ranges[deploy_id] = (repo, service, version, pr["base"]["sha"], pr["head"]["sha"])
        return _deploy(deploy_id, service, version, (pr.get("user") or {}).get("login", "?"), pr["title"],
                       _ts(pr["merged_at"]))

    def _commits(self, repo, branch, start, end):
        params = {"sha": branch, "since": start.isoformat(), "until": end.isoformat(), "per_page": 100}
        return list(self._paged(f"/repos/{repo}/commits", params, lambda c: False))

    def _from_commit(self, repo, service, c):
        sha = c["sha"]
        deploy_id = f"{repo}:commit:{sha[:12]}"
        self._ranges[deploy_id] = (repo, service, sha[:7], None, sha)
        author = (c.get("author") or {}).get("login") or c["commit"]["author"]["name"]
        message = c["commit"]["message"].splitlines()[0] if c["commit"]["message"] else ""
        return _deploy(deploy_id, service, sha[:7], author, message, _ts(c["commit"]["committer"]["date"]))

    # ---------- diffs ----------

    def get_diff(self, deploy_id):
        self._notes = []
        rng = self._ranges.get(deploy_id) or self._range_from_id(deploy_id)
        repo, service, version, base, head = rng
        try:
            if base:
                data = get(self.client, f"/repos/{repo}/compare/{base}...{head}").json()
            else:
                data = get(self.client, f"/repos/{repo}/commits/{head}").json()
        except RateLimited as e:
            raise ValueError(f"GitHub {e}; try git_diff again later") from None
        diff = summarize_diff(data.get("files") or [], total_commits=data.get("total_commits"))
        out = {"deploy_id": deploy_id, "service": service, "version": version, "diff": redact(diff)}
        return {**out, "note": "; ".join(self._notes)} if self._notes else out

    def _range_from_id(self, deploy_id):
        """An id we didn't list in this process (e.g. after a restart): look it up directly."""
        m = re.fullmatch(r"([\w.-]+/[\w.-]+):(deploy|pr|commit):([\w]+)", deploy_id)
        if not m or m.group(1) not in self.repos:
            raise ValueError(f"no deploy with id {deploy_id!r}")
        repo, kind, ref = m.groups()
        service = self.repos[repo]
        try:
            if kind == "deploy":
                sha = get(self.client, f"/repos/{repo}/deployments/{ref}").json()["sha"]
                # Without the previous deployment we only know the head commit.
                self._notes.append("diff shows only the deployed head commit, not everything since the previous deploy")
                return repo, service, sha[:7], None, sha
            if kind == "pr":
                pr = get(self.client, f"/repos/{repo}/pulls/{ref}").json()
                return repo, service, f"#{ref}", pr["base"]["sha"], pr["head"]["sha"]
        except httpx.HTTPStatusError:
            raise ValueError(f"no deploy with id {deploy_id!r}") from None
        return repo, service, ref[:7], None, ref


def _deploy(deploy_id, service, version, author, message, at):
    return {"id": deploy_id, "service": service, "version": version, "author": redact(author),
            "message": redact(message[:200]), "deployed_at": at.isoformat(), "deployed_at_dt": at, "diff": None}


def _short_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def _file_rank(f):
    name = f["filename"]
    tier = 2 if NOISE.search(name) else 1 if LOW_PRIORITY.search(name) else 0
    return tier, -(f.get("changes") or 0)


def _clip_patch(patch: str, budget: int) -> str:
    """Whole hunks only, so the model never sees half a hunk and misreads it."""
    hunks = re.split(r"(?m)^(?=@@)", patch)
    out = ""
    for h in hunks:
        if len(out) + len(h) > budget:
            return out + ("" if out else h[:budget]) + "\n... [more hunks in this file omitted]\n"
        out += h
    return out


def summarize_diff(files: list[dict], total_commits=None, max_chars=MAX_DIFF_CHARS) -> str:
    """A file list (every file, with +/- counts) and then the most relevant hunks
    (source before tests/docs, lockfiles and generated files never) until the budget runs out."""
    head = [f"{len(files)} files changed" + (f" in {total_commits} commits" if total_commits else "") + ":"]
    for f in files[:MAX_LISTED_FILES]:
        head.append(f"  {f.get('status', 'modified')} {f['filename']} (+{f.get('additions', 0)} -{f.get('deletions', 0)})")
    if len(files) > MAX_LISTED_FILES:
        head.append(f"  ... and {len(files) - MAX_LISTED_FILES} more files")
    text = "\n".join(head) + "\n\n"
    skipped = []
    for f in sorted(files, key=_file_rank):
        patch = f.get("patch")
        if not patch or NOISE.search(f["filename"]):
            continue
        old = f.get("previous_filename", f["filename"])
        block = f"--- a/{old}\n+++ b/{f['filename']}\n"
        room = min(MAX_FILE_CHARS, max_chars - len(text) - len(block))
        if room < 200:
            skipped.append(f["filename"])
            continue
        text += block + _clip_patch(patch, room).rstrip("\n") + "\n"
    if skipped:
        text += f"... [diff truncated: hunks omitted for {len(skipped)} files: {', '.join(skipped[:10])}]\n"
    return text
