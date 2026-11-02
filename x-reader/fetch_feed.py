"""Read worker. Credentials arrive via its environment, never arguments."""

import argparse
import json
import os
import sys
from pathlib import Path

from twitter_cli.client import TwitterClient
from twitter_cli.serialization import tweets_to_data


EXPECTED_SCREEN_NAME = "8opqg6"


def main():
    lock_path=Path('/var/lib/picoclaw-readers/media.lock')
    if os.name=='posix' and lock_path.exists():
        import fcntl
        media_lock=lock_path.open('r+');fcntl.flock(media_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    parser = argparse.ArgumentParser()
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--kind", choices=("for-you", "following"))
    source.add_argument("--post-id", type=lambda value:str(int(value)))
    parser.add_argument("--limit", type=int, choices=range(1, 21), required=True)
    args = parser.parse_args()

    # No browser-cookie extraction, user config, global result cache, or CLI dispatch.
    client = TwitterClient(
        auth_token=os.environ["TWITTER_AUTH_TOKEN"],
        ct0=os.environ["TWITTER_CT0"],
        rate_limit_config={"maxCount": 20, "maxRetries": 0},
    )
    profile = client.fetch_me()
    if profile.screen_name.casefold() != EXPECTED_SCREEN_NAME.casefold():
        raise RuntimeError("The active X account does not match the configured test account.")

    if args.post_id:posts=client.fetch_tweet_detail(args.post_id,count=args.limit)
    else:
        fetch = client.fetch_home_timeline if args.kind == "for-you" else client.fetch_following_feed
        posts = fetch(args.limit)
    data=tweets_to_data(posts[:args.limit])
    # Bound text independently of the number of posts.
    for row in data:
        for key,value in list(row.items()):
            if isinstance(value,str):row[key]=value[:2400]
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"X read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
