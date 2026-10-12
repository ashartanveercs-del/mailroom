"""Plain-text rendering of run reports and the allowlist, for the CLI."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from .models import Outcome
from .pipeline import RunReport


def render_outcomes(outcomes: Iterable[Outcome]) -> list[str]:
    rows = []
    for o in outcomes:
        where = o.path or ""
        why = f"({o.reason})" if o.reason else ""
        rows.append((o.kind.value, o.key, " ".join(part for part in (where, why) if part)))
    width = max((len(key) for _, key, _ in rows), default=0)
    return [f"  {kind:<9} {key:<{width}}  {detail}".rstrip() for kind, key, detail in rows]


def render_counts(outcomes: Iterable[Outcome]) -> str:
    counts = Counter(o.kind.value for o in outcomes)
    return ", ".join(f"{kind} {n}" for kind, n in sorted(counts.items())) or "nothing new"


def render_run(report: RunReport) -> str:
    mode = "preview (nothing was written)" if report.preview else "live"
    lines = [f"mailroom run, {mode}: {report.messages_seen} new message(s), "
             f"{len(report.outcomes)} document outcome(s)"]
    lines += render_outcomes(report.outcomes)
    for message_id, reason in report.messages_ignored:
        lines.append(f"  ignored   message {message_id} ({reason})")
    lines.append(f"counts: {render_counts(report.outcomes)}")
    rec = report.recovery
    settled = len(rec.completed) + len(rec.resumed) + len(rec.rolled_back)
    if settled or not rec.clean:
        lines.append(f"recovery: {len(rec.completed)} rolled forward, {len(rec.resumed)} "
                     f"resumed, {len(rec.rolled_back)} rolled back, {len(rec.conflicts)} "
                     f"conflict(s), {len(rec.orphans)} orphan(s)")
    lines += [f"error: {err}" for err in report.errors]
    return "\n".join(lines)


def render_allowlist(rows: Sequence[dict[str, object]]) -> str:
    lines = [f"{'rule':<26} {'method':<6} {'host':<24} path / query"]
    for row in rows:
        query = ", ".join(row["query"]) if row["query"] else "-"  # type: ignore[arg-type]
        body = "  [body checked]" if row["body_checked"] else ""
        lines.append(f"{row['name']:<26} {row['method']:<6} {row['host']:<24} "
                     f"{row['path']}  ? {query}{body}")
    return "\n".join(lines)
