"""USB CLI command implementations.

The command functions receive the existing argparse namespace. CLI helpers are
looked up lazily to preserve parser and import behavior while command families
are extracted incrementally.
"""

from __future__ import annotations


def cmd_usb_list(args) -> int:
    from .. import cli, usb

    drives = usb.list_removable_drives()
    if not drives:
        print(cli._("usb_none_detected"))
        print(cli._("usb_plug_tip"))
        return 0

    print(
        f"{cli._('usb_col_drive'):<20} {cli._('usb_col_label'):<20} "
        f"{cli._('usb_col_size'):>6}  {cli._('usb_col_fs'):<8}  "
        f"{cli._('usb_col_vault')}"
    )
    print("-" * 70)

    for drive in drives:
        vault = "🐘 EleSync" if usb.is_usb_vault(drive["path"]) else "—"
        print(
            f"{drive['path']:<20} {drive['label']:<20} "
            f"{drive['size_gb']:>5.1f}G  {drive['fs']:<8}  {vault}"
        )

    return 0


def cmd_usb_install(args) -> int:
    from .. import cli, usb

    drive = args.drive
    passphrase = cli._get_passphrase(confirm=True)
    print(f"\n🐘  Setting up EleSync portable vault on {drive} …")

    try:
        result = usb.install(drive, passphrase, label=args.label or "USB")
    except (OSError, ValueError, RuntimeError) as error:
        cli._err(error)
        return 1

    print(f"✅  Vault created:   {result['vault_dir']}")
    print("   Encrypted:      yes (XSalsa20-Poly1305 + argon2id)")
    print(f"   Launcher:       {result['launcher']}")
    print(f"   README:         {result['readme']}")
    print()
    print("Next steps:")
    print(f"  Import memories:  ele --vault {result['vault_dir']} import <file> --source chatgpt")
    print(f"  Connect Claude:   ele usb attach {drive}")
    print("  On another machine: plug in the drive and run  python RUN_ME.py")
    return 0


def cmd_usb_attach(args) -> int:
    from .. import cli, usb

    drive = args.drive
    passphrase = cli._get_passphrase(confirm=False)
    print(f"\n🔌  Attaching USB vault from {drive} …")

    try:
        result = usb.attach(
            drive,
            passphrase,
            name=args.name or "elesync-usb",
        )
    except (FileNotFoundError, RuntimeError, ValueError) as error:
        cli._err(error)
        return 1

    print(f"✅  Vault wired into your AI client as '{result['server_key']}'.")
    print(f"   Config: {result['config_path']}")
    print()
    print("Restart your AI client — your USB memories are live.")
    print(f"When done, run:  ele usb detach {drive}  (before unplugging)")
    return 0


def cmd_usb_detach(args) -> int:
    from .. import usb

    drive = args.drive
    result = usb.detach(drive, name=args.name or "elesync-usb")

    if result["removed"]:
        print("✅  USB vault disconnected from your AI client.")
        print(f"   Config: {result['config_path']}")
        print()
        print("You can now safely eject the drive.")
        if args.eject:
            print(usb.eject(drive))
    else:
        print(f"ℹ️   {result.get('reason', 'Nothing to detach.')}")

    return 0


def cmd_usb_status(args) -> int:
    from .. import cli, usb

    status = usb.status(args.drive)
    print(cli._("usb_status_drive", status["drive"]))
    print(
        cli._(
            "usb_status_vault_present",
            cli._("yes") if status["vault_present"] else cli._("no"),
        )
    )

    if status["vault_present"]:
        print(
            cli._(
                "usb_status_encrypted",
                (cli._("yes") if status["encrypted"] else cli._("usb_status_encrypted_no")),
            )
        )
        print(cli._("usb_status_memory_files", status["note_count"]))
        print(
            cli._(
                "usb_status_launcher",
                (
                    cli._("usb_launcher_present")
                    if status["launcher_present"]
                    else cli._("usb_launcher_missing")
                ),
            )
        )

    print(cli._("usb_status_free_space", status["size_gb"]))
    return 0


def cmd_usb_eject(args) -> int:
    from .. import usb

    print(usb.eject(args.drive))
    return 0


def cmd_usb(args) -> int:
    """Dispatch USB subcommands."""
    return args.usb_func(args)
