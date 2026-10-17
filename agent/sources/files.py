"""Deploy history from a JSON file (the demo's state/deploys.json): the default."""
import json
from datetime import datetime

import telemetry as tm
from sources.base import DeploysSource


class FileDeploys(DeploysSource):
    def _load(self) -> list[dict]:
        # Read on every call: chaos.py rewrites the file while the agent runs.
        try:
            with open(tm.DEPLOYS_FILE) as f:
                deploys = json.load(f)
        except FileNotFoundError:
            return []
        for d in deploys:
            d["deployed_at_dt"] = datetime.fromisoformat(d["deployed_at"])
        return sorted(deploys, key=lambda d: d["deployed_at_dt"])

    def list_deploys(self, start, end):
        return [d for d in self._load()
                if (start is None or start <= d["deployed_at_dt"]) and (end is None or d["deployed_at_dt"] <= end)]

    def get_diff(self, deploy_id):
        for d in self._load():
            if d["id"] == deploy_id:
                return {"deploy_id": deploy_id, "service": d["service"], "version": d["version"], "diff": d["diff"]}
        raise ValueError(f"no deploy with id {deploy_id!r}")
