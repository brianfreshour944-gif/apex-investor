"""On-demand ingestion CLI (M2): fetch allowlisted documents for HELD tickers only.

Run:  uv run python scripts/ingest.py [--seeds data/seeds.json] [--date YYYY-MM-DD]

Configuration:
  - data/allowlist.json  (git-ignored) — approved domains; see data/allowlist.example.json
  - data/seeds.json      (git-ignored) — operator-provided seed URLs per ticker
  - data/portfolio.json  (git-ignored) — the ledger; only HELD tickers (shares > 0) are ingested

Safety:
  - No ledger mutation, no model, no orders. This only fetches approved documents into data/docs/.
  - Refuses to run without an allowlist AND a User-Agent (D6).
  - Every request and redirect hop is allowlist- and DNS-checked; responses are capped at 5 MiB.
  - The run is deterministic: at most 10 documents are stored; the result distinguishes stored,
    duplicates, skipped tickers and failures, and flags an incomplete run.
  - An append-only manifest (data/docs/manifest.jsonl) records one line per attempted URL
    (requested_url, final_url, ticker, outcome, retrieved_at). A manifest write failure is reported
    but never affects an already-stored document.

This CLI does NOT touch scripts/daily_report.py and is not wired into the Phase 1 report.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apex_investor.docstore import DocumentStore  # noqa: E402
from apex_investor.fetch import DEFAULT_RATE_LIMIT_SECONDS, Fetcher, FetchError  # noqa: E402
from apex_investor.ingest import ingest, manifest_locking_supported  # noqa: E402
from apex_investor.ledger import Ledger, Side, Tag, Transaction  # noqa: E402
from apex_investor.sources import Allowlist, SourcePolicyError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = ROOT / "data" / "allowlist.json"
SEEDS_FILE = ROOT / "data" / "seeds.json"
PORTFOLIO_FILE = ROOT / "data" / "portfolio.json"
DOCS_DIR = ROOT / "data" / "docs"
# The User-Agent identifies the application and a contact, as SEC fair-access guidance requires.
# It is required configuration: there is deliberately no invented default (D6).
USER_AGENT_FILE = ROOT / "data" / "user_agent.txt"


def load_allowlist(path: Path) -> Allowlist:
    if not path.exists():
        raise SourcePolicyError(f"no allowlist at {path} (copy data/allowlist.example.json)")
    return Allowlist.from_config(json.loads(path.read_text()))


def load_seeds(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        raise SourcePolicyError(f"no seeds at {path} (one or more approved https URLs per ticker)")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SourcePolicyError("seeds file must be a JSON object of ticker -> [urls]")
    return {str(k): list(v) for k, v in data.items()}


def load_portfolio(path: Path) -> Ledger:
    spec = json.loads(path.read_text())
    tags, txs, cost = {}, [], 0.0
    for h in spec["holdings"]:
        tk = h["ticker"]
        txs.append(Transaction("2000-01-01", tk, Side.BUY, h["shares"], h["cost_basis_ps"]))
        cost += h["shares"] * h["cost_basis_ps"]
        tags[tk] = Tag(h["tag"])
    return Ledger.from_transactions(txs, starting_cash=spec["starting_cash"] + cost, tags=tags)


def load_user_agent(path: Path) -> str:
    if not path.exists():
        raise FetchError(
            f"no User-Agent configured at {path}; refusing to run without one (D6). "
            "Provide an identifying UA with a contact, e.g. 'apex-investor/0.1 (contact@example.com)'")
    ua = path.read_text().strip()
    if not ua:
        raise FetchError(f"User-Agent file {path} is empty; refusing to run")
    return ua


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest allowlisted documents for held tickers (M2)")
    ap.add_argument("--allowlist", type=Path, default=ALLOWLIST_FILE)
    ap.add_argument("--seeds", type=Path, default=SEEDS_FILE)
    ap.add_argument("--portfolio", type=Path, default=PORTFOLIO_FILE)
    ap.add_argument("--user-agent-file", type=Path, default=USER_AGENT_FILE)
    ap.add_argument("--docs-dir", type=Path, default=DOCS_DIR)
    ap.add_argument("--date", default=None, help="retrieved_at override (YYYY-MM-DD)")
    args = ap.parse_args()

    if not manifest_locking_supported():
        print("unsupported platform: ingestion requires POSIX flock (Linux/macOS); "
              "native Windows is not supported.", file=sys.stderr)
        return 2

    try:
        allowlist = load_allowlist(args.allowlist)
        seeds = load_seeds(args.seeds)
        user_agent = load_user_agent(args.user_agent_file)
    except (SourcePolicyError, FetchError, ValueError, OSError) as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    if not args.portfolio.exists():
        print(f"no portfolio snapshot at {args.portfolio}; nothing to ingest.", file=sys.stderr)
        return 0
    ledger = load_portfolio(args.portfolio)

    fetcher = Fetcher(allowlist=allowlist, user_agent=user_agent,
                      rate_limit_seconds=DEFAULT_RATE_LIMIT_SECONDS)
    manifest = args.docs_dir / "manifest.jsonl"
    result = ingest(ledger, allowlist, seeds, DocumentStore(args.docs_dir), fetcher=fetcher,
                    today=args.date, manifest=manifest)

    print(f"ingestion complete: {result.summary()}")
    for doc_id in result.stored:
        print(f"  stored: {doc_id}")
    for doc_id in result.duplicates:
        print(f"  duplicate: {doc_id}")
    for ticker in result.tickers_skipped:
        print(f"  skipped (no seed URL): {ticker}")
    for failure in result.failures:
        print(f"  FAILED {failure['ticker']} {failure['url']}: {failure['reason']}")
    for failure in result.manifest_failures:
        print(f"  MANIFEST WRITE FAILED {failure['url']}: {failure['reason']}")
    if result.incomplete:
        print("  NOTE: budget reached — ingestion may be incomplete (Phase 1 report unaffected)")
    return 1 if (result.failures or result.manifest_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
