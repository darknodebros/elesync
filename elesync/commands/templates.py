"""Template CLI commands."""

from __future__ import annotations


def _shadowed(vault, name: str, templates) -> bool:
    return (templates.templates_dir(vault) / f"{name}.json").is_file()


def cmd_template_list(args) -> int:
    from .. import cli, templates

    vault = cli._resolve_vault(args)
    available, errors = templates.load_all(vault)
    shipped = set(templates.BUILTINS)
    print(cli._("template_list_header", len(available)))
    for name in sorted(available):
        template = available[name]
        origin = "user" if name not in shipped or _shadowed(vault, name, templates) else "built-in"
        fields = ", ".join(template.fields) or "—"
        print(f"  {name:<16} [{origin}]  {template.description}")
        print(
            f"  {'':<16} type={template.type}  tags={','.join(template.tags) or '—'}  fields: {fields}"
        )
    if errors:
        cli._err(cli._("template_unreadable", len(errors)))
        for filename, message in sorted(errors.items()):
            cli._err(f"  {filename}: {message}")
        return 1
    return 0


def cmd_template_show(args) -> int:
    from .. import cli, templates

    vault = cli._resolve_vault(args)
    try:
        template = templates.get(vault, args.name)
    except templates.TemplateError as error:
        cli._err(error)
        return 1
    print(f"{template.name} — {template.description}")
    print(f"  type:   {template.type}")
    print(f"  tags:   {','.join(template.tags) or '—'}")
    print(f"  scopes: {','.join(template.scopes) or '—'}")
    print(f"  source: {template.source or '—'}")
    print(cli._("template_body_fields", ", ".join(template.fields) or "—"))
    print(template.body or "—")
    return 0


def cmd_template_new(args) -> int:
    from .. import cli, templates
    from ..models import normalize_tags

    vault = cli._resolve_vault(args)
    try:
        if args.copy_from:
            data = templates.get(vault, args.copy_from).to_dict()
        else:
            data = {"description": "", "type": "note", "tags": [], "scopes": [], "body": ""}
        if args.description is not None:
            data["description"] = args.description
        if args.type is not None:
            data["type"] = args.type
        if args.tags is not None:
            data["tags"] = normalize_tags(args.tags)
        if args.scopes is not None:
            data["scopes"] = cli.scopes_mod.parse(args.scopes)
        if args.source is not None:
            data["source"] = args.source
        if args.body is not None:
            data["body"] = args.body
        template = templates.Template.from_dict(args.name, data)
        path = templates.save(vault, template)
    except templates.TemplateError as error:
        cli._err(error)
        return 1
    except OSError as error:
        cli._err(f"Could not write template: {error}")
        return 1
    print(cli._("template_saved", template.name, str(path)))
    return 0


def cmd_template_remove(args) -> int:
    from .. import cli, templates

    vault = cli._resolve_vault(args)
    name = (args.name or "").strip().lower()
    if not templates.remove(vault, name):
        cli._err(cli._("template_no_file", name))
        return 1
    if name in templates.BUILTINS:
        print(cli._("template_restored_builtin", name))
    else:
        print(cli._("template_removed", name))
    return 0
