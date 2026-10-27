"""Search-related CLI commands."""

from __future__ import annotations


def cmd_search(args) -> int:
    from .. import cli

    with cli._store(args) as store:
        _mode, semantic_weight = cli._resolve_search_params(args)
        hits = store.search(
            args.query,
            limit=args.limit,
            source=args.source,
            mtype=args.type,
            grant=cli._grant(args),
            semantic_weight=semantic_weight,
        )
        if not hits:
            print(cli._("no_matches"))
            return 0
        for hit in hits:
            scopes = "" if not hit.scopes else f" {cli.scopes_mod.label_of(hit.scopes)}"
            print(f"\n[{hit.type}/{hit.source}]{scopes} id={hit.id[:8]}")
            snippet = hit.content[: cli.SEARCH_SNIPPET_CHARS]
            print(snippet + ("…" if len(hit.content) > cli.SEARCH_SNIPPET_CHARS else ""))
        return 0
