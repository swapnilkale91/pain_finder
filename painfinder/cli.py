"""pain_finder CLI.

Usage:
  python -m painfinder.cli ingest-hn [--max 500]
  python -m painfinder.cli ingest-reviews --app "notion" [--pages 5] [--max-rating 3]
  python -m painfinder.cli extract [--limit N] [--heuristic]
  python -m painfinder.cli score [--heuristic]
  python -m painfinder.cli stats
"""

import argparse
import sys

from . import db as dbm
from . import extract as extract_mod
from . import score as score_mod


def cmd_ingest_hn(args):
    from .ingest import hn_jobs
    thread_title, items = hn_jobs.ingest(max_comments=args.max)
    conn = dbm.connect(args.db)
    new = dbm.insert_raw_items(conn, items)
    print(f"{thread_title}: fetched {len(items)} job posts, {new} new")


def cmd_ingest_reviews(args):
    from .ingest import app_reviews
    app_name, items = app_reviews.ingest(
        app_term=args.app, app_id=args.app_id, country=args.country,
        pages=args.pages, max_rating=args.max_rating,
    )
    conn = dbm.connect(args.db)
    new = dbm.insert_raw_items(conn, items)
    print(f"{app_name}: fetched {len(items)} reviews, {new} new")


def cmd_extract(args):
    conn = dbm.connect(args.db)
    items = dbm.unextracted_items(conn, limit=args.limit)
    if not items:
        print("Nothing to extract — run an ingest command first.")
        return
    total_pains = 0
    for i, item in enumerate(items, 1):
        try:
            pains = extract_mod.extract(
                item["source"], item["title"], item["text"], heuristic=args.heuristic,
            )
        except Exception as e:
            print(f"  [{i}/{len(items)}] item {item['id']}: FAILED ({e})", file=sys.stderr)
            continue
        dbm.save_pains(conn, item["id"], pains)
        total_pains += len(pains)
        print(f"  [{i}/{len(items)}] item {item['id']} ({item['source']}): {len(pains)} pain(s)")
    print(f"Done: {total_pains} pains from {len(items)} items")


def cmd_score(args):
    conn = dbm.connect(args.db)
    rows = dbm.all_pains(conn)
    if not rows:
        print("No pains extracted yet — run `extract` first.")
        return
    pains = [dict(r) for r in rows]
    themes = score_mod.build_themes(pains, heuristic=args.heuristic)
    dbm.replace_themes(conn, themes)
    print(f"Built {len(themes)} themes:\n")
    for t in themes:
        print(f"  {t['score']:>6.2f}  {t['name']}  "
              f"({len(t['pain_ids'])} pains, {t['source_count']} source type(s), "
              f"avg severity {t['avg_severity']})")


def cmd_stats(args):
    conn = dbm.connect(args.db)
    for k, v in dbm.stats(conn).items():
        print(f"{k:>12}: {v}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="painfinder")
    parser.add_argument("--db", default=dbm.DEFAULT_DB_PATH, help="SQLite path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest-hn", help="Ingest the latest HN Who is Hiring thread")
    p.add_argument("--max", type=int, default=500, help="Max job posts to fetch")
    p.set_defaults(func=cmd_ingest_hn)

    p = sub.add_parser("ingest-reviews", help="Ingest App Store reviews for one app")
    p.add_argument("--app", help="App name to search for (e.g. 'notion')")
    p.add_argument("--app-id", type=int, help="Explicit App Store track ID")
    p.add_argument("--country", default="us")
    p.add_argument("--pages", type=int, default=5, help="Feed pages (~50 reviews each, max 10)")
    p.add_argument("--max-rating", type=int, default=3,
                   help="Keep only reviews at or below this star rating (default 3; 5 keeps all)")
    p.set_defaults(func=cmd_ingest_reviews)

    p = sub.add_parser("extract", help="Extract pain points from ingested items")
    p.add_argument("--limit", type=int, help="Max items to process this run")
    p.add_argument("--heuristic", action="store_true",
                   help="Keyword rules instead of Claude (no API key needed)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("score", help="Cluster pains into themes and rank them")
    p.add_argument("--heuristic", action="store_true",
                   help="Category grouping instead of Claude clustering")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("stats", help="Show pipeline counts")
    p.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
