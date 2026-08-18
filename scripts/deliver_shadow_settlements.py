"""Deliver one bounded batch from the durable shadow settlement outbox."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import create_app  # noqa: E402
from services.shadow_settlement import deliver_shadow_settlement_outbox  # noqa: E402
from services.strathmark_shadow import (  # noqa: E402
    ShadowClientConfig,
    StrathmarkShadowClient,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deliver a bounded batch of durable STRATHMARK shadow outcomes."
    )
    parser.add_argument("--limit", type=int, default=25, choices=range(1, 101))
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        client = StrathmarkShadowClient(ShadowClientConfig.from_mapping(app.config))
        result = deliver_shadow_settlement_outbox(client=client, limit=args.limit, commit=True)
    print(
        "shadow settlement delivery: "
        f"attempted={result.attempted} recorded={result.recorded} "
        f"retryable_failed={result.retryable_failed} "
        f"remaining_eligible={result.remaining_eligible} status={result.status}"
    )
    if result.retryable_failed:
        return 1
    if result.remaining_eligible:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
