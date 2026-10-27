"""Memory and search CLI command implementations.

The command functions receive the existing argparse namespace. CLI helpers are
looked up lazily to preserve the current parser and import behavior while the
command surface is extracted incrementally.
"""

from __future__ import annotations


def cmd_add(args) -> int:
    from .. import cli, templates
    from ..models import MemoryItem, normalize_tags

    content = args.content
    fields = {
        "type": args.type,
        "tags": normalize_tags(args.tags or ""),
        "source": args.source or "manual",
        "scopes": cli.scopes_mod.parse(args.scopes or ""),
    }
    if getattr(args, "template", None):
        try:
            tpl = templates.get(cli._resolve_vault(args), args.template)
            values = cli._parse_set_values(getattr(args, "set", None))
            if tpl.body.strip():
                if content:
                    raise ValueError(
                        f"Template {tpl.name!r} supplies its own content; pass --set "
                        "key=value instead of a positional content argument."
                    )
                content = tpl.render(values)
            elif values:
                raise ValueError(
                    f"Template {tpl.name!r} has no body, so --set has nothing to fill."
                )
            fields = templates.apply_defaults(
                tpl,
                type=args.type if args.type != "note" else None,
                tags=args.tags,
                scopes=cli.scopes_mod.parse(args.scopes) if args.scopes else None,
                source=args.source,
            )
        except (templates.TemplateError, ValueError) as error:
            cli._err(error)
            return 1
    if not content:
        cli._err(cli._("add_needs_content"))
        return 1
    with cli._store(args) as store:
        item = MemoryItem(
            content=content,
            type=fields["type"],
            tags=fields["tags"],
            source=fields["source"],
            scopes=fields["scopes"],
        )
        memory_id, inserted = store.upsert_status(item)
        where = (
            "" if not item.scopes else cli._("saved_scope", cli.scopes_mod.label_of(item.scopes))
        )
        if inserted:
            print(cli._("saved", memory_id[:8], where))
        else:
            print(cli._("already_exists", memory_id[:8]))
        if inserted and getattr(args, "supersedes", None):
            from ..service import MemoryService

            print(MemoryService(store)._supersede(memory_id, args.supersedes))
        return 0
