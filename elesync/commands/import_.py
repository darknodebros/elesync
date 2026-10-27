"""Import CLI commands."""

from __future__ import annotations


def cmd_import(args) -> int:
    from .. import cli, normalize

    if getattr(args, "path", None) == "-":
        return import_stdin(args)
    with cli._store(args) as store:
        try:
            items = normalize.normalize_file(args.path, source=args.source)
        except (ValueError, OSError) as error:
            cli._err(error)
            return 1
        result = store.import_many(items, progress=cli._progress("Importing"))
        print(cli._("imported", result["added"], result["skipped_duplicates"], result["total"]))
        return 0


def import_stdin(args) -> int:
    """Read plain text from stdin and import one memory per paragraph or line."""
    from .. import cli, normalize

    if cli.sys.stdin.isatty():
        print(cli._("paste_prompt"))
    text = cli.sys.stdin.read()
    if not text or not text.strip():
        cli._err(cli._("paste_empty"))
        return 1
    with cli._store(args) as store:
        items = normalize.from_plaintext(text, source=args.source or "manual")
        result = store.import_many(items)
        print(cli._("imported", result["added"], result["skipped_duplicates"], result["total"]))
        return 0
