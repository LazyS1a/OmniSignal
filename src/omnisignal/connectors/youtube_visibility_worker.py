"""yt-dlp search adapter: metadata only, bounded flat extraction, no media downloads."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from itertools import islice
from pathlib import Path
from pydantic import ValidationError
from .public_search_worker import _write_result
from .youtube_visibility_analysis import VideoEntry
from .youtube_visibility_policy import YouTubeVisibilityPolicy


def collect(policy: YouTubeVisibilityPolicy) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.version import __version__
    warnings: set[str] = set()
    class Logger:
        def debug(self, message):
            pass
        def warning(self, message):
            warnings.add("extractor_warning")
        def error(self, message):
            warnings.add("extractor_warning")
    options = {"quiet": True, "no_warnings": False, "logger": Logger(),
               "extract_flat": True, "skip_download": True, "lazy_playlist": True,
               "playlistend": policy.top_k, "socket_timeout": 10, "retries": 0,
               "extractor_retries": 0, "fragment_retries": 0, "cachedir": False,
               "ignoreerrors": False, "geo_bypass": False,
               "extractor_args": {"youtube": {"lang": [policy.language]}}}
    entries = []
    with YoutubeDL(options) as client:
        result = client.extract_info(f"ytsearch{policy.top_k}:{policy.query}", download=False)
        if not isinstance(result, dict) or "entries" not in result:
            raise ValueError("search schema changed")
        for position, entry in enumerate(islice(result["entries"], policy.top_k), start=1):
            try:
                video = VideoEntry(position=position, video_id=entry["id"], title=entry["title"], channel_id=entry.get("channel_id"))
            except (ValidationError, KeyError, TypeError):
                warnings.add("invalid_entry")
            else:
                entries.append(video.model_dump())
    return {"protocol_version": "1.0", "query": policy.query, "top_k": policy.top_k,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "collector_version": f"yt-dlp/{__version__}",
            "entries": entries, "warnings": sorted(warnings)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        policy = YouTubeVisibilityPolicy.model_validate_json(args.job.read_text(encoding="utf-8"))
        result = collect(policy)
    except ImportError:
        result = {"error": "dependency_missing"}
    except ValueError:
        result = {"error": "schema_drift"}
    except Exception:
        result = {"error": "upstream_unavailable"}
    _write_result(args.result, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
