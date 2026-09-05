"""Verify GitHub review records. No model calls, paid secrets, or PR code execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

OWNER = "arunvenkatadri"
AI_REVIEWERS = {"chatgpt-codex-connector[bot]", "claude[bot]"}
STATUS_CONTEXT = "Review policy"


def evaluate(pr: dict[str, Any], reviews: list[dict[str, Any]]) -> tuple[str, str]:
    """Require a current AI review and, for other authors, owner's current approval."""
    if pr.get("draft"):
        return "pending", "Mark this PR ready for review."
    head = pr["head"]["sha"]
    latest_ai: dict[str, dict[str, Any]] = {}
    owner_decision = None
    for review in sorted(reviews, key=lambda r: r["id"]):
        login = review.get("user", {}).get("login", "").lower()
        state = review["state"]
        if state == "PENDING":
            continue
        if login == OWNER and state != "COMMENTED":
            owner_decision = review
        if login in AI_REVIEWERS and review.get("user", {}).get("type") == "Bot":
            latest_ai[login] = review
    if owner_decision and owner_decision["state"] == "CHANGES_REQUESTED":
        return "failure", "Arun requested changes; a new approval is required."
    if any(r["state"] == "CHANGES_REQUESTED" for r in latest_ai.values()):
        return "failure", "An AI reviewer requested changes; obtain a fresh review."
    if not any(
        r.get("commit_id") == head and r["state"] in {"APPROVED", "COMMENTED"}
        for r in latest_ai.values()
    ):
        return "pending", "Waiting for a Codex or Claude review of the current commit."
    if pr["user"]["login"].lower() != OWNER:
        if not owner_decision or (
            owner_decision["state"] != "APPROVED" or owner_decision.get("commit_id") != head
        ):
            return "pending", "Waiting for Arun's approval of the current commit."
    return "success", "Current commit has all required reviews."


class GitHub:
    def __init__(self) -> None:
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.token = os.environ["GH_TOKEN"]

    def request(self, path: str, data: dict[str, Any] | None = None) -> Any:
        request = Request(
            f"https://api.github.com/repos/{self.repo}/{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def pages(self, path: str) -> list[dict[str, Any]]:
        result = []
        separator = "&" if "?" in path else "?"
        page = 1
        while True:
            batch = self.request(f"{path}{separator}per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def publish(self, sha: str, state: str, description: str) -> None:
        self.request(
            f"statuses/{sha}",
            {
                "state": state,
                "context": STATUS_CONTEXT,
                "description": description,
                "target_url": (
                    f"https://github.com/{self.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                ),
            },
        )


def run(api: GitHub, numbers: list[int]) -> None:
    for number in numbers:
        pr = api.request(f"pulls/{number}")
        if pr["state"] != "open" or pr["base"]["ref"] != "main":
            continue
        sha = pr["head"]["sha"]
        # Fail closed on API errors rather than retaining an earlier green result.
        api.publish(sha, "pending", "Checking current review records.")
        state, description = evaluate(pr, api.pages(f"pulls/{number}/reviews"))
        if api.request(f"pulls/{number}")["head"]["sha"] != sha:
            continue
        api.publish(sha, state, description)
        print(f"PR #{number}: {state}: {description}")


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    api = GitHub()
    if "pull_request" in event:
        numbers = [event["pull_request"]["number"]]
    elif event.get("inputs", {}).get("pull_request"):
        numbers = [int(event["inputs"]["pull_request"])]
    else:
        numbers = [p["number"] for p in api.pages("pulls?state=open&base=main")]
    run(api, numbers)


if __name__ == "__main__":
    main()
