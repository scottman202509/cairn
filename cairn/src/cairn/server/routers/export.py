from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

import yaml
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from cairn.server.db import get_conn
from cairn.server.services import expire_reason_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, intents, sources_by_intent


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [{"id": f["id"], "description": f["description"]} for f in facts]

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------
#
# A polished, self-contained test report suitable for handoff to a human
# reviewer (security lead, project owner, downstream agent). Reuses the same
# blackboard data as yaml/timeline but organizes it into a narrative:
#
#   1. Header + status + key timestamps
#   2. Executive summary (counts, outcome, abandonment reason if any)
#   3. Origin / Goal block
#   4. Operator hints (chronological)
#   5. Findings -- every fact, with origin/goal/abandonment called out
#   6. Exploration log -- intents and their conclusions, in time order
#   7. Appendix -- the raw YAML snapshot, so the report is self-describing
#
# The output is plain Markdown (no extra deps). Render anywhere, paste into
# tickets, or pipe through pandoc for PDF.


def _md_escape(text: str | None) -> str:
    """Trim and inline-escape Markdown so user content can't break headings.

    We only neutralise the small set of chars that hurt inline rendering
    (`#`, `*`, `_`, backtick, `<`, `>`) and leave the rest readable.
    """
    if not text:
        return ""
    text = text.strip()
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _md_blockquote(text: str | None) -> str:
    """Render multi-line text as a Markdown block quote."""
    if not text:
        return "> _(empty)_"
    lines = text.strip().splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


_GOAL_TAG_RE = re.compile(r"^\s*\[(GOAL|ABANDONED)\]\s*", re.IGNORECASE)


def _fact_tag(description: str) -> str | None:
    """Return uppercase tag if a fact description starts with `[GOAL]` /
    `[ABANDONED]`, else None."""
    m = _GOAL_TAG_RE.match(description or "")
    return m.group(1).upper() if m else None


def _project_outcome(proj, facts, intents) -> dict:
    """Compute the project's terminal narrative.

    Returns a dict with:
        status:        one of completed / abandoned / stopped / active
        completed_at:  iso ts, if any (intent->goal concluded_at or abandon ts)
        finisher:      who/what closed the loop (worker or creator)
        reason:        human text -- goal description for completion or
                       abandon reason for abandonment
    """
    facts_by_id = {f["id"]: f["description"] for f in facts}
    completed_intent = None
    abandon_intent = None
    for i in intents:
        if i["to_fact_id"] == "goal" and i["concluded_at"]:
            completed_intent = i
        elif i["to_fact_id"] and (i["description"] or "").startswith("abandon:"):
            abandon_intent = i

    raw_status = proj["status"]

    if completed_intent is not None:
        return {
            "status": "completed",
            "completed_at": completed_intent["concluded_at"],
            "finisher": completed_intent["worker"] or completed_intent["creator"],
            "reason": facts_by_id.get("goal", ""),
        }

    if abandon_intent is not None:
        # The abandon fact carries the reason (`[ABANDONED] <text>`).
        abandon_fact_id = abandon_intent["to_fact_id"]
        abandon_text = facts_by_id.get(abandon_fact_id, "")
        clean = _GOAL_TAG_RE.sub("", abandon_text).strip()
        return {
            "status": "abandoned",
            "completed_at": abandon_intent["concluded_at"],
            "finisher": abandon_intent["worker"] or abandon_intent["creator"],
            "reason": clean,
        }

    return {
        "status": raw_status,
        "completed_at": None,
        "finisher": None,
        "reason": "",
    }


_OUTCOME_BADGE = {
    "completed": ("Completed", "Goal reached"),
    "abandoned": ("Abandoned", "Exploration was given up"),
    "stopped": ("Stopped", "Paused by operator"),
    "active": ("Active", "Exploration is still running"),
}


def _export_markdown(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)
    facts_by_id = {f["id"]: f["description"] for f in facts}
    outcome = _project_outcome(proj, facts, intents)

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")

    created_at = format_export_timestamp(proj["created_at"]) or "—"
    concluded_at = format_export_timestamp(outcome["completed_at"]) or "—"
    label, blurb = _OUTCOME_BADGE.get(outcome["status"], (outcome["status"], ""))

    intent_count = len(intents)
    concluded_count = sum(1 for i in intents if i["concluded_at"])
    in_flight_count = intent_count - concluded_count
    # The pseudo-facts `origin` and `goal` are inputs, not findings, so we
    # surface them separately and exclude them from the "discovered" count.
    finding_count = sum(1 for f in facts if f["id"] not in ("origin", "goal"))

    out: list[str] = []
    out.append(f"# Cairn Test Report — {_md_escape(proj['title'])}")
    out.append("")
    out.append(
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Project ID | `{proj['id']}` |\n"
        f"| Status | **{label}** — {blurb} |\n"
        f"| Created | {created_at} |\n"
        f"| Concluded | {concluded_at} |"
        + (f"\n| Closed by | `{_md_escape(outcome['finisher'])}` |" if outcome["finisher"] else "")
    )
    out.append("")

    # Executive summary --------------------------------------------------
    out.append("## Executive Summary")
    out.append("")
    out.append(f"- **Outcome**: {label} — {blurb}")
    if outcome["status"] == "completed" and outcome["reason"]:
        out.append(f"- **Goal reached**: {_md_escape(outcome['reason'])}")
    elif outcome["status"] == "abandoned" and outcome["reason"]:
        out.append(f"- **Reason for abandonment**: {_md_escape(outcome['reason'])}")
    out.append(
        f"- **Volume**: {finding_count} finding(s), {intent_count} intent(s) "
        f"({concluded_count} concluded, {in_flight_count} in flight), {len(hints)} hint(s)"
    )
    out.append("")

    # Brief -------------------------------------------------------------
    out.append("## Brief")
    out.append("")
    out.append("### Origin")
    out.append("")
    out.append(_md_blockquote(origin_desc))
    out.append("")
    out.append("### Goal")
    out.append("")
    out.append(_md_blockquote(goal_desc))
    out.append("")

    # Hints --------------------------------------------------------------
    if hints:
        out.append("## Operator Hints")
        out.append("")
        for h in hints:
            ts = format_export_timestamp(h["created_at"]) or ""
            out.append(f"- **{_md_escape(h['creator'])}** · _{ts}_")
            for line in (h["content"] or "").strip().splitlines() or [""]:
                out.append(f"  > {line}" if line else "  >")
        out.append("")

    # Findings -----------------------------------------------------------
    if finding_count > 0:
        out.append("## Findings")
        out.append("")
        out.append(
            "Each finding below is a fact produced by an intent during exploration. "
            "Highlighted entries are the project's terminal state."
        )
        out.append("")
        for f in facts:
            if f["id"] in ("origin", "goal"):
                continue
            tag = _fact_tag(f["description"])
            body = _GOAL_TAG_RE.sub("", f["description"]).strip()
            if tag == "ABANDONED":
                out.append(f"### `{f['id']}` · ⛔ Abandoned")
            elif tag == "GOAL":
                out.append(f"### `{f['id']}` · ✅ Goal reached")
            else:
                out.append(f"### `{f['id']}`")
            out.append("")
            out.append(_md_blockquote(body))
            out.append("")

    # Exploration log ----------------------------------------------------
    if intents:
        out.append("## Exploration Log")
        out.append("")
        for i in intents:
            src = sources_by_intent.get(i["id"], [])
            from_str = ", ".join(f"`{s}`" for s in src) or "—"
            created_ts = format_export_timestamp(i["created_at"]) or ""
            out.append(
                f"### `{i['id']}` · {_md_escape(i['description'])}"
            )
            out.append("")
            out.append(
                f"- **Declared by** `{_md_escape(i['creator'])}` at _{created_ts}_"
            )
            out.append(f"- **From**: {from_str}")
            if i["worker"] and not i["concluded_at"]:
                out.append(
                    f"- **Worker**: `{_md_escape(i['worker'])}` _(in progress)_"
                )
            if i["concluded_at"]:
                actor = i["worker"] or i["creator"]
                concluded_ts = format_export_timestamp(i["concluded_at"]) or ""
                if i["to_fact_id"] == "goal":
                    out.append(
                        f"- **Concluded** by `{_md_escape(actor)}` at _{concluded_ts}_ — "
                        f"**🎯 goal reached**"
                    )
                elif i["to_fact_id"]:
                    out.append(
                        f"- **Concluded** by `{_md_escape(actor)}` at _{concluded_ts}_ → "
                        f"produced `{i['to_fact_id']}`"
                    )
                    fact_desc = facts_by_id.get(i["to_fact_id"], "")
                    fact_body = _GOAL_TAG_RE.sub("", fact_desc).strip()
                    if fact_body:
                        out.append("")
                        out.append(_md_blockquote(fact_body))
                else:
                    out.append(
                        f"- **Concluded** by `{_md_escape(actor)}` at _{concluded_ts}_ "
                        "_(no fact produced)_"
                    )
            out.append("")

    # Raw appendix -------------------------------------------------------
    out.append("## Appendix: Raw Graph (YAML)")
    out.append("")
    out.append("```yaml")
    out.append(_export_yaml(conn, project_id).rstrip())
    out.append("```")
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# HTTP entry point
# ---------------------------------------------------------------------------

_FORMAT_META = {
    "yaml": ("yaml", "text/plain; charset=utf-8"),
    "timeline": ("log", "text/plain; charset=utf-8"),
    "markdown": ("md", "text/markdown; charset=utf-8"),
}


def _slugify(text: str) -> str:
    """Produce an ASCII, filesystem- and HTTP-header-friendly slug.

    HTTP headers are latin-1, so the slug used in the plain ``filename=``
    parameter must be ASCII. We deliberately strip everything outside
    ``[a-z0-9-]`` (Python's ``\\w`` is Unicode-aware and would happily keep
    CJK characters). The unicode title still survives via the RFC 5987
    ``filename*=UTF-8'...`` fallback added by the caller.
    """
    s = re.sub(r"[^a-z0-9\-]+", "-", (text or "").strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "project"


@router.get("/projects/{project_id}/export")
def export_project(
    project_id: str,
    format: str = "yaml",
    download: bool = Query(
        False,
        description=(
            "If true, set Content-Disposition: attachment so browsers prompt a save "
            "dialog. Always true for markdown reports."
        ),
    ),
):
    if format not in _FORMAT_META:
        raise HTTPException(400, "Supported formats: yaml, timeline, markdown")

    extension, media_type = _FORMAT_META[format]

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "markdown":
            text = _export_markdown(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        # Pull title for the filename. _export_* already called
        # get_project_or_404 via _load_project_data, so this is cheap and
        # guaranteed to exist.
        row = conn.execute(
            "SELECT title FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        title_slug = _slugify(row["title"] if row else "")

    headers: dict[str, str] = {}
    if download or format == "markdown":
        # ASCII filename (header-safe) plus an RFC 5987 fallback that carries
        # the full unicode title in case the client surfaces it (browsers do).
        ascii_name = f"{project_id}-{title_slug}.{extension}"
        unicode_title = (row["title"] if row else "") or project_id
        unicode_name = f"{project_id}-{unicode_title}.{extension}"
        encoded = quote(unicode_name, safe="")
        headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded}"
        )

    return Response(content=text, media_type=media_type, headers=headers)
