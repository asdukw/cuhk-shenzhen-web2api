"""Persisted server timings and report summaries; no client/server clock mixing."""

from statistics import median


def durations(record: dict) -> dict:
    pairs = {
        "queue_seconds": ("created_at", "sent_at"),
        "first_text_seconds": ("sent_at", "first_text_at"),
        "generation_seconds": ("sent_at", "finished_at"),
        "total_seconds": ("created_at", "finished_at"),
    }
    return {
        name: max(0.0, record[end] - record[start])
        if isinstance(record.get(start), (int, float))
        and isinstance(record.get(end), (int, float))
        else None
        for name, (start, end) in pairs.items()
    }


def summarize(rows: list[dict], elapsed: float) -> dict:
    passed = sum(bool(row.get("passed")) for row in rows)
    latency = {}
    for source, fields in [
        ("client", ("first_text_seconds", "total_seconds")),
        (
            "server",
            (
                "queue_seconds",
                "first_text_seconds",
                "generation_seconds",
                "total_seconds",
            ),
        ),
    ]:
        for field in fields:
            values = [row.get(source, {}).get(field) for row in rows]
            values = [v for v in values if isinstance(v, (int, float))]
            latency[f"{source}_{field}"] = {
                "median": median(values) if values else None,
                "max": max(values) if values else None,
            }
    return {
        "success_rate": passed / len(rows) if rows else 0,
        "successful_requests_per_minute": passed * 60 / elapsed if elapsed > 0 else 0,
        "rate_limit_count": sum(row.get("rate_limit_count", 0) for row in rows),
        "unknown_count": sum(row.get("status") == "unknown" for row in rows),
        "latencies": latency,
    }
