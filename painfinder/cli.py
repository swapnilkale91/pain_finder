"""pain_finder CLI.

Usage:
  python -m painfinder.cli ingest-hn [--max 500]
  python -m painfinder.cli ingest-hn-discussions --query "bookkeeping" [--max 100]
  python -m painfinder.cli ingest-reviews --app "notion" [--pages 5] [--max-rating 3]
  python -m painfinder.cli ingest-github --query "bookkeeping" [--max 200]
  python -m painfinder.cli extract [--limit N] [--budget-usd 5] [--heuristic]
  python -m painfinder.cli score [--heuristic]
  python -m painfinder.cli run --apps "notion,quickbooks" [--budget-usd 5] [--loop --interval-hours 12]
  python -m painfinder.cli usage
  python -m painfinder.cli stats
"""

import argparse
import os
import sys
import time

from . import db as dbm
from . import extract as extract_mod
from . import score as score_mod
from . import usage as usage_mod


def _ingest_hn(conn, max_comments: int) -> None:
    from .ingest import hn_jobs
    thread_title, items = hn_jobs.ingest(max_comments=max_comments)
    new = dbm.insert_raw_items(conn, items)
    print(f"{thread_title}: fetched {len(items)} job posts, {new} new")


def _ingest_hn_discussions(conn, query, domain=None, max_comments=100,
                           max_stories=10, days=730) -> None:
    from .ingest import hn_discussions
    query, items = hn_discussions.ingest(
        query, domain=domain, max_comments=max_comments,
        max_stories=max_stories, days=days,
    )
    new = dbm.insert_raw_items(conn, items)
    print(f"HN query {query!r}: fetched {len(items)} discussions, {new} new")


def _ingest_reviews(conn, app=None, app_id=None, country="us", pages=5, max_rating=3,
                    domain=None) -> None:
    from .ingest import app_reviews
    app_name, items = app_reviews.ingest(
        app_term=app, app_id=app_id, country=country, pages=pages, max_rating=max_rating,
        domain=domain,
    )
    new = dbm.insert_raw_items(conn, items)
    print(f"{app_name}: fetched {len(items)} reviews, {new} new")


def _ingest_domain(conn, domain, query=None, country="us", top=10, pages=3,
                   max_rating=3) -> None:
    from .ingest import app_reviews
    names, items = app_reviews.ingest_domain(
        domain, query=query, country=country, top=top, pages=pages, max_rating=max_rating,
    )
    new = dbm.insert_raw_items(conn, items)
    print(f"domain {domain!r}: {len(names)} apps ({', '.join(names[:5])}"
          f"{'...' if len(names) > 5 else ''}), {len(items)} reviews, {new} new")


def _ingest_github(conn, repo=None, query=None, state="all", max_issues=200,
                   max_repos=10, labels=None, domain=None) -> None:
    from .ingest import github_issues
    target, items = github_issues.ingest(
        repo=repo, query=query, state=state, max_issues=max_issues, max_repos=max_repos,
        labels=labels, domain=domain,
    )
    new = dbm.insert_raw_items(conn, items)
    kind = "repo" if repo else "query"
    print(f"GitHub {kind} {target!r}: fetched {len(items)} issues, {new} new")


def _extract_all(conn, limit=None, heuristic=False, budget_usd=None, batch=False) -> None:
    items = dbm.unextracted_items(conn, limit=limit)
    if not items:
        print("Extract: nothing new.")
        return
    spent = 0.0

    def sink(stage, model, tokens, batch=False):
        nonlocal spent
        spent += usage_mod.record(conn, stage, model, tokens, batch=batch)

    if batch and not heuristic:
        est_per_item = extract_mod.estimate_batch_cost_per_item()
        if budget_usd is not None:
            max_items = max(1, int(budget_usd / est_per_item))
            if len(items) > max_items:
                print(f"Budget ${budget_usd:.2f} covers ~{max_items} of {len(items)} "
                      f"pending items — submitting the first {max_items}.")
                items = items[:max_items]
        print(f"Extract (batch, {extract_mod.EXTRACT_MODEL}): {len(items)} items, "
              f"estimated ≤ ${est_per_item * len(items):.2f}")
        results = extract_mod.extract_batch_with_claude(
            [dict(it) for it in items], usage_sink=sink,
        )
        total_pains = 0
        for item in items:
            pains = results.get(item["id"])
            if pains is None:
                continue  # failed request — stays unextracted, retried next run
            dbm.save_pains(conn, item["id"], pains)
            total_pains += len(pains)
        print(f"Extract done: {total_pains} pains from {len(results)}/{len(items)} items, "
              f"${spent:.4f} spent")
        return

    total_pains = 0
    for i, item in enumerate(items, 1):
        if budget_usd is not None and spent >= budget_usd:
            print(f"Budget of ${budget_usd:.2f} reached after {i - 1} items — stopping. "
                  f"Re-run extract to continue.")
            break
        try:
            pains = extract_mod.extract(
                item["source"], item["title"], item["text"],
                heuristic=heuristic, usage_sink=sink,
            )
        except Exception as e:
            print(f"  [{i}/{len(items)}] item {item['id']}: FAILED ({e})", file=sys.stderr)
            continue
        dbm.save_pains(conn, item["id"], pains)
        total_pains += len(pains)
        print(f"  [{i}/{len(items)}] item {item['id']} ({item['source']}): {len(pains)} pain(s)")
    print(f"Extract done: {total_pains} pains" +
          (f", ${spent:.4f} spent" if not heuristic else ""))


def _score_all(conn, heuristic=False) -> None:
    rows = dbm.all_pains(conn)
    if not rows:
        print("Score: no pains extracted yet.")
        return
    spent = 0.0

    def sink(stage, model, tokens, batch=False):
        nonlocal spent
        spent += usage_mod.record(conn, stage, model, tokens, batch=batch)

    pains = [dict(r) for r in rows]
    themes = score_mod.build_themes(pains, heuristic=heuristic, usage_sink=sink)
    dbm.replace_themes(conn, themes)
    # High-water mark: lets `run` skip the expensive re-cluster when no pain
    # has been added since. (Counting unclustered pains doesn't work — the
    # clusterer intentionally leaves one-off pains out of themes.)
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM pains").fetchone()[0]
    dbm.set_meta(conn, "last_scored_max_pain_id", str(max_id))
    dbm.set_meta(conn, "last_scored_at", dbm.now_iso())
    print(f"Built {len(themes)} themes" +
          (f" (${spent:.4f} spent)" if not heuristic else "") + ":\n")
    for t in themes:
        print(f"  {t['score']:>6.2f}  {t['name']}  "
              f"({len(t['pain_ids'])} pains, {t['source_count']} source type(s), "
              f"avg severity {t['avg_severity']})")


# --- Commands -----------------------------------------------------------------

def cmd_ingest_hn(args):
    _ingest_hn(dbm.connect(args.db), args.max)


def cmd_ingest_hn_discussions(args):
    _ingest_hn_discussions(
        dbm.connect(args.db), args.query, domain=args.domain,
        max_comments=args.max, max_stories=args.stories, days=args.days,
    )


def cmd_ingest_reviews(args):
    _ingest_reviews(dbm.connect(args.db), app=args.app, app_id=args.app_id,
                    country=args.country, pages=args.pages, max_rating=args.max_rating,
                    domain=args.domain)


def cmd_ingest_domain(args):
    _ingest_domain(dbm.connect(args.db), args.domain, query=args.query,
                   country=args.country, top=args.top, pages=args.pages,
                   max_rating=args.max_rating)


def cmd_ingest_github(args):
    _ingest_github(dbm.connect(args.db), repo=args.repo, query=args.query, state=args.state,
                   max_issues=args.max, max_repos=args.repos,
                   labels=args.labels, domain=args.domain)


def cmd_extract(args):
    _extract_all(dbm.connect(args.db), limit=args.limit, heuristic=args.heuristic,
                 budget_usd=args.budget_usd, batch=args.batch)


def cmd_score(args):
    _score_all(dbm.connect(args.db), heuristic=args.heuristic)


def cmd_run(args):
    """Full pipeline: ingest everything -> extract new items -> re-score.

    One-shot by default; --loop repeats every --interval-hours. Ingestion is
    free and idempotent; only NEW items reach the (paid) extract stage.
    """
    apps = [a.strip() for a in (args.apps or "").split(",") if a.strip()]
    domains = [d.strip() for d in (args.domains or "").split(",") if d.strip()]
    github_repos = [r.strip() for r in (args.github_repos or "").split(",") if r.strip()]
    failed_stages = []
    while True:
        conn = dbm.connect(args.db)
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"=== pipeline run @ {started} ===")
        # Each stage is isolated: a failure is reported but never discards the
        # work of earlier stages (which is already committed to the DB).
        try:
            _ingest_hn(conn, args.max)
        except Exception as e:
            failed_stages.append("ingest-hn")
            print(f"HN ingest failed: {e}", file=sys.stderr)
        if not args.skip_hn_discussions:
            for domain in domains:
                try:
                    _ingest_hn_discussions(
                        conn, domain, domain=domain,
                        max_comments=args.hn_discussion_max,
                        max_stories=args.hn_discussion_top,
                        days=args.hn_discussion_days,
                    )
                except Exception as e:
                    failed_stages.append(f"ingest-hn-discussions:{domain}")
                    print(f"HN discussion search for {domain!r} failed: {e}", file=sys.stderr)
        for app in apps:
            try:
                _ingest_reviews(conn, app=app, pages=args.pages, max_rating=args.max_rating)
            except Exception as e:
                failed_stages.append(f"ingest-reviews:{app}")
                print(f"Review ingest for {app!r} failed: {e}", file=sys.stderr)
        countries = [c.strip() for c in (args.countries or "us").split(",") if c.strip()]
        for domain in domains:
            for country in countries:
                try:
                    _ingest_domain(conn, domain, country=country, top=args.top,
                                   pages=args.pages, max_rating=args.max_rating)
                except Exception as e:
                    failed_stages.append(f"ingest-domain:{domain}:{country}")
                    print(f"Domain ingest for {domain!r} ({country}) failed: {e}",
                          file=sys.stderr)
        if not args.skip_github:
            for domain in domains:
                try:
                    _ingest_github(conn, query=domain, state=args.github_state,
                                   max_issues=args.github_max, max_repos=args.github_top,
                                   domain=domain)
                except Exception as e:
                    failed_stages.append(f"ingest-github-query:{domain}")
                    print(f"GitHub search for {domain!r} failed: {e}", file=sys.stderr)
        for repo in github_repos:
            try:
                _ingest_github(conn, repo=repo, state=args.github_state,
                               max_issues=args.github_max)
            except Exception as e:
                failed_stages.append(f"ingest-github:{repo}")
                print(f"GitHub ingest for {repo!r} failed: {e}", file=sys.stderr)
        try:
            _extract_all(conn, heuristic=args.heuristic, budget_usd=args.budget_usd,
                         batch=args.batch)
        except Exception as e:
            failed_stages.append("extract")
            print(f"Extract failed: {e}", file=sys.stderr)
        # Re-clustering runs over ALL pains on the big model (several $ at
        # scale), so skip it when nothing new was extracted since the last
        # scoring run.
        max_pain = conn.execute("SELECT COALESCE(MAX(id), 0) FROM pains").fetchone()[0]
        last_scored = int(dbm.get_meta(conn, "last_scored_max_pain_id") or 0)
        has_themes = conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] > 0
        # Budget guard: with several pipeline runs per day, re-clustering on
        # every trickle of new reviews would multiply the day's biggest cost.
        # Cluster at most ~once a day; extraction still runs every time.
        recently_scored = False
        last_scored_at = dbm.get_meta(conn, "last_scored_at")
        if last_scored_at:
            from datetime import datetime, timedelta, timezone
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last_scored_at)
            recently_scored = age < timedelta(hours=20)
        if has_themes and max_pain <= last_scored:
            print("Score: skipped (no new pains since last clustering).")
        elif has_themes and recently_scored:
            print("Score: deferred (clustered within the last 20h; new pains "
                  "will be themed on the next daily pass).")
        else:
            try:
                _score_all(conn, heuristic=args.heuristic)
            except Exception as e:
                failed_stages.append("score")
                print(f"Score failed: {e}", file=sys.stderr)
        try:
            from . import digest as digest_mod
            digest_path = os.path.join(os.path.dirname(args.db) or ".", "digest.md")
            with open(digest_path, "w") as f:
                f.write(digest_mod.build_digest(conn))
            print(f"Digest written to {digest_path}")
        except Exception as e:
            print(f"Digest generation failed: {e}", file=sys.stderr)
        s = usage_mod.summary(conn)["total"]
        print(f"Cumulative LLM spend: ${s['cost_usd']:.2f} "
              f"({s['input_tokens']:,} in / {s['output_tokens']:,} out tokens)")
        conn.close()
        if not args.loop:
            break
        failed_stages = []
        print(f"Sleeping {args.interval_hours}h...\n")
        time.sleep(args.interval_hours * 3600)
    if failed_stages:
        print(f"Run finished with failures: {', '.join(failed_stages)}", file=sys.stderr)
        sys.exit(1)


def cmd_brief(args):
    from . import brief as brief_mod
    conn = dbm.connect(args.db)

    if args.rank is not None:
        row = conn.execute(
            "SELECT * FROM themes ORDER BY score DESC LIMIT 1 OFFSET ?",
            (args.rank - 1,)).fetchone()
        if row is None:
            sys.exit(f"No theme at rank {args.rank}.")
    else:
        matches = conn.execute(
            "SELECT * FROM themes WHERE name LIKE ? ORDER BY score DESC",
            (f"%{args.theme}%",)).fetchall()
        if not matches:
            sys.exit(f"No theme matching {args.theme!r}.")
        if len(matches) > 1:
            print(f"{len(matches)} themes match — using the highest scored:")
            for m in matches[:5]:
                print(f"  {m['score']:>6.1f}  {m['name']}")
        row = matches[0]

    spent = 0.0

    def sink(stage, model, tokens, batch=False):
        nonlocal spent
        spent += usage_mod.record(conn, stage, model, tokens, batch=batch)

    print(f"Generating brief for: {row['name']} (score {row['score']})...")
    md = brief_mod.generate_brief(conn, row, usage_sink=sink)
    dbm.save_brief(conn, row["name"], row["domain"], row["score"], md)
    print(f"Done (${spent:.3f}).\n")
    if args.output:
        with open(args.output, "w") as f:
            f.write(md)
        print(f"Written to {args.output}")
    else:
        print(md)


def cmd_digest(args):
    from . import digest as digest_mod
    md = digest_mod.build_digest(dbm.connect(args.db))
    if args.output:
        with open(args.output, "w") as f:
            f.write(md)
        print(f"Digest written to {args.output}")
    else:
        print(md)


def cmd_usage(args):
    s = usage_mod.summary(dbm.connect(args.db))
    t = s["total"]
    print(f"Total: {t['calls']} calls, {t['input_tokens']:,} input tokens, "
          f"{t['output_tokens']:,} output tokens, ${t['cost_usd']:.4f}")
    for st in s["stages"]:
        print(f"  {st['stage']:>8}: {st['calls']} calls, "
              f"{st['input_tokens']:,} in / {st['output_tokens']:,} out, "
              f"${st['cost_usd']:.4f}")


def cmd_stats(args):
    conn = dbm.connect(args.db)
    for k, v in dbm.stats(conn).items():
        print(f"{k:>12}: {v}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="painfinder")
    parser.add_argument("--db", default=dbm.DEFAULT_DB_PATH, help="SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest-hn", help="Ingest the latest HN Who is Hiring thread (free)")
    p.add_argument("--max", type=int, default=500, help="Max job posts to fetch")
    p.set_defaults(func=cmd_ingest_hn)

    p = sub.add_parser("ingest-hn-discussions",
                       help="Search recent Hacker News discussions for a market")
    p.add_argument("--query", required=True, help="Market-wide HN comment search query")
    p.add_argument("--domain", help="Optional market/domain label (defaults to query)")
    p.add_argument("--max", type=int, default=100, help="Max comments to fetch")
    p.add_argument("--stories", type=int, default=10,
                   help="Top matching stories to sample")
    p.add_argument("--days", type=int, default=730, help="How far back to search")
    p.set_defaults(func=cmd_ingest_hn_discussions)

    p = sub.add_parser("ingest-reviews", help="Ingest App Store reviews for one app (free)")
    p.add_argument("--app", help="App name to search for (e.g. 'notion')")
    p.add_argument("--app-id", type=int, help="Explicit App Store track ID")
    p.add_argument("--country", default="us")
    p.add_argument("--pages", type=int, default=5, help="Feed pages (~50 reviews each, max 10)")
    p.add_argument("--max-rating", type=int, default=3,
                   help="Keep only reviews at or below this star rating (default 3; 5 keeps all)")
    p.add_argument("--domain", help="Optional domain label to tag these reviews with")
    p.set_defaults(func=cmd_ingest_reviews)

    p = sub.add_parser("ingest-domain",
                       help="Sweep a whole domain: reviews for the top N apps matching it (free)")
    p.add_argument("domain", help="Domain label, e.g. 'crypto exchange' or 'bookkeeping'")
    p.add_argument("--query", help="Custom App Store search query (defaults to the domain)")
    p.add_argument("--top", type=int, default=10, help="How many apps to ingest (default 10)")
    p.add_argument("--country", default="us")
    p.add_argument("--pages", type=int, default=3, help="Review pages per app (~50 each)")
    p.add_argument("--max-rating", type=int, default=3)
    p.set_defaults(func=cmd_ingest_domain)

    p = sub.add_parser("ingest-github",
                       help="Search GitHub issues by market query or repository")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--query", help="Market-wide issue search query")
    target.add_argument("--repo", help="Repository in owner/repo form")
    p.add_argument("--state", choices=["open", "closed", "all"], default="all")
    p.add_argument("--max", type=int, default=200, help="Max issues to fetch")
    p.add_argument("--repos", type=int, default=10,
                   help="Top repositories to sample for a market query")
    p.add_argument("--labels", help="Comma-separated GitHub labels to require")
    p.add_argument("--domain", help="Optional market/domain label for these issues")
    p.set_defaults(func=cmd_ingest_github)

    p = sub.add_parser("extract", help="Extract pain points from ingested items (paid: Claude)")
    p.add_argument("--limit", type=int, help="Max items to process this run")
    p.add_argument("--budget-usd", type=float,
                   help="Stop extracting once this run's spend reaches the cap")
    p.add_argument("--batch", action="store_true",
                   help="Use the Message Batches API (50%% cheaper, minutes of latency)")
    p.add_argument("--heuristic", action="store_true",
                   help="Keyword rules instead of Claude (free, low quality)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("score", help="Cluster pains into themes and rank them")
    p.add_argument("--heuristic", action="store_true",
                   help="Category grouping instead of Claude clustering")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("run", help="Full pipeline: ingest -> extract -> score")
    p.add_argument("--apps", help="Comma-separated app names for review ingestion")
    p.add_argument("--domains",
                   help="Comma-separated domains to sweep (top N apps each), "
                        "e.g. 'crypto exchange,bookkeeping'")
    p.add_argument("--github-repos",
                   help="Comma-separated GitHub repositories in owner/repo form")
    p.add_argument("--github-state", choices=["open", "closed", "all"], default="all")
    p.add_argument("--github-max", type=int, default=100,
                   help="Max issues per GitHub domain search or repository")
    p.add_argument("--github-top", type=int, default=10,
                   help="Top GitHub repositories to sample per domain")
    p.add_argument("--skip-github", action="store_true",
                   help="Do not search GitHub for the configured domains")
    p.add_argument("--hn-discussion-max", type=int, default=100,
                   help="Max HN discussion comments per domain")
    p.add_argument("--hn-discussion-top", type=int, default=10,
                   help="Top matching HN stories to sample per domain")
    p.add_argument("--hn-discussion-days", type=int, default=730,
                   help="How far back to search HN discussions")
    p.add_argument("--skip-hn-discussions", action="store_true",
                   help="Do not search HN discussions for the configured domains")
    p.add_argument("--top", type=int, default=10, help="Apps per domain sweep")
    p.add_argument("--countries", default="us",
                   help="Comma-separated App Store storefronts for domain sweeps, "
                        "e.g. 'us,in,gb' — gives the geography dimension its data")
    p.add_argument("--max", type=int, default=500, help="Max HN job posts")
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--max-rating", type=int, default=3)
    p.add_argument("--budget-usd", type=float, default=5.0,
                   help="Per-run extraction spend cap (default $5)")
    p.add_argument("--batch", action="store_true",
                   help="Use the Message Batches API for extraction (50%% cheaper)")
    p.add_argument("--loop", action="store_true", help="Keep running on an interval")
    p.add_argument("--interval-hours", type=float, default=12.0)
    p.add_argument("--heuristic", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("brief",
                       help="Generate an opportunity brief for one theme (Opus, ~$0.30-0.60)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--rank", type=int, help="Theme rank on the leaderboard (1 = top)")
    target.add_argument("--theme", help="Substring of the theme name")
    p.add_argument("--output", help="Write markdown to a file instead of stdout")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("digest", help="Markdown digest: top themes, movers, new themes")
    p.add_argument("--output", help="Write to a file instead of stdout")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("usage", help="Show LLM token usage and estimated cost")
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser("stats", help="Show pipeline counts")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
