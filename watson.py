import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
sys.dont_write_bytecode = True

# Direct script invocations use the same local environment as watson.cmd.
if __name__ == "__main__":
    _local_env = Path(__file__).resolve().parent / ".venv"
    _local_python = _local_env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if _local_python.is_file() and Path(sys.prefix).resolve() != _local_env.resolve():
        os.execv(str(_local_python), [str(_local_python), "-B", "-X", "utf8",
                                     str(Path(__file__).resolve()), *sys.argv[1:]])
import webbrowser

from rich.console import Console
from rich.progress import Progress, ProgressColumn
from rich.text import Text

from modules.common import HttpClient
from modules.common import normalize
from modules import ADAPTERS, select_services
from modules.common import compact_report, result_fields, run_checks

console = Console(highlight=False)
NOTICE = "Number formatting does not confirm activity, ownership or registration. +7 is shared by KZ and RU."
STATUS_LABELS = {
    "linked": ("[+]", "registered"),
    "not_linked": ("[-]", "not registered"),
    "rate_limited": ("[?]", "rate limit"),
    "error": ("[x]", "error"),
    "unknown": ("[!]", "unknown"),
    "auth_required": ("[!]", "auth required"),
    "user_action_required": ("[!]", "action required"),
    "unsupported": ("[!]", "unavailable"),
}
STATUS_STYLES = {"linked": "green", "not_linked": "magenta", "rate_limited": "red"}
SERVICE_ORDER = ("amazon", "instagram", "microsoft", "snapchat", "odnoklassniki", "zoho", "facebook")
SERVICE_NAMES = {
    "amazon": "Amazon", "instagram": "Instagram", "microsoft": "Microsoft",
    "snapchat": "Snapchat", "odnoklassniki": "Odnoklassniki", "zoho": "Zoho", "facebook": "Facebook",
}
DETAIL_LABELS = {
    "name": "Name", "display_name": "Name", "masked_name": "Masked name",
    "username": "Username", "user_id": "ID", "profile_url": "Profile",
    "masked_email": "Masked email", "masked_phone": "Masked phone",
    "email": "Email", "sign_in_alias": "Sign-in alias",
    "profile_info": "Profile info", "registered_at": "Created",
    "avatar": "Avatar", "matched_accounts": "Matched accounts",
    "recovery_method": "Recovery method",
    "allowed_modes": "Sign-in methods", "datacenter": "Datacenter",
    "account_status": "Account status", "challenge_type": "Sign-in step",
}


class SearchProgressColumn(ProgressColumn):
    def __init__(self, output):
        super().__init__()
        self.output = output

    @staticmethod
    def clock(seconds):
        if seconds is None:
            return "?"
        seconds = max(0, int(seconds))
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"

    def render(self, task):
        total = max(0, int(task.total or 0))
        completed = min(total, max(0, int(task.completed)))
        elapsed = task.finished_time if task.finished else task.elapsed
        elapsed = elapsed or 0
        rate = completed / elapsed if completed and elapsed > 0 else None
        remaining = (total - completed) / rate if rate else None
        if task.finished:
            remaining = 0
        speed = f"{rate:.2f}" if rate is not None else "?"
        percent = 100 if task.finished else min(99, int(task.percentage))
        prefix = "Interrupted " if task.fields.get("interrupted") else ""
        left = f"{prefix}{percent:3d}%|"
        right = f"| {completed}/{total} [{self.clock(elapsed)}<{self.clock(remaining)}, {speed}it/s]"
        width = max(1, self.output.width - len(left) - len(right))
        filled = width * completed // total if total else 0
        line = Text(left, style="grey78", no_wrap=True, overflow="crop")
        line.append("█" * filled, style="grey78")
        line.append(" " * (width - filled))
        line.append(right, style="grey78")
        return line


def show_number(phone):
    width = max(17, len(phone.e164) + 4)
    border = "*" * width
    console.print(Text(border))
    console.print(Text("  " + phone.e164, style="bold"))
    console.print(Text(border))
    console.print()


def format_result(result):
    marker, label = STATUS_LABELS.get(result.status, ("[!]", "unknown"))
    name = SERVICE_NAMES.get(result.service, result.service)
    line = f"{marker} {name}"
    if result.status not in ("linked", "not_linked", "rate_limited"):
        line += f" — {label}"
        if result.reason:
            line += f": {result.reason}"
    return line


def show_results(results, details=False):
    order = {name: index for index, name in enumerate(SERVICE_ORDER)}
    console.print()
    for result in sorted(results, key=lambda item: order.get(item.service, len(order))):
        style = STATUS_STYLES.get(result.status, "red")
        console.print(Text(format_result(result), style=style))
        fields = result_fields(result)
        if not details:
            visible = {
                "odnoklassniki": {"masked_name", "masked_email", "masked_phone", "profile_info",
                                   "registered_at", "username", "user_id", "profile_url", "avatar"},
                "microsoft": {"masked_email", "email"},
            }.get(result.service, set())
            fields = {key: value for key, value in fields.items() if key in visible and result.status == "linked"}
        for key, value in fields.items():
            if value != "" and value is not None:
                console.print(Text(f"    {DETAIL_LABELS.get(key, key)}: {value}", style="cyan"))
        if details:
            if result.status == "rate_limited" and result.reason:
                console.print(Text(f"    {result.reason}", style=style))
            console.print(Text(f"    {result.duration_ms / 1000:.2f} s", style="dim"))
    legend = Text()
    for status in ("linked", "not_linked", "rate_limited"):
        if legend:
            legend.append(" ")
        marker, label = STATUS_LABELS[status]
        legend.append(f"{marker} {label}", style=STATUS_STYLES[status])
    console.print()
    console.print(legend)


def show_methods(adapters):
    console.print("service | method | required_action", style="cyan")
    for adapter in adapters:
        console.print(f"{adapter.name} | {adapter.method} | {adapter.requirement}", markup=False)


async def execute(phone, adapters, args):
    # Timeout each stalled network operation, without limiting a whole lookup.
    http = HttpClient(timeout=60, min_interval=0, retries=0)
    tokens = {"_allow_recovery": args.allow_recovery,
              "_snapchat_interactive": getattr(args, "snapchat_interactive", False),
              "_amazon_interactive": getattr(args, "amazon_interactive", False)}
    try:
        if args.json:
            return await run_checks(phone, adapters, http, tokens, timeout=args.timeout, output=args.output)
        with Progress(SearchProgressColumn(console), console=console,
                      expand=True, refresh_per_second=15) as progress:
            task = progress.add_task("Searching", total=len(adapters))

            def display(result):
                progress.advance(task)

            results, interrupted = await run_checks(phone, adapters, http, tokens, timeout=args.timeout,
                                                    output=args.output, on_result=display)
            progress.update(task, completed=len(results), interrupted=interrupted)
            progress.stop_task(task)
            return results, interrupted
    finally:
        await http.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="watson", description="Account identifier lookups for your own phone number.")
    sub = parser.add_subparsers(dest="command", required=True)
    methods_parser = sub.add_parser("methods", help="List lookup methods and requirements")
    methods_parser.add_argument("--services")
    open_parser = sub.add_parser("open", help="Open a service page in your browser")
    open_parser.add_argument("service")
    normalize_parser = sub.add_parser("normalize", help="Show number formats and metadata")
    normalize_parser.add_argument("number")
    normalize_parser.add_argument("--region")
    check = sub.add_parser(
        "check",
        help="Look up your number using sign-in and recovery forms",
        description="Zoho runs only when explicitly selected.",
    )
    check.add_argument("number")
    check.add_argument("--region")
    check.add_argument("--services", "--modules", dest="services")
    for adapter in ADAPTERS:
        flags = ["--" + adapter.name]
        if adapter.name == "odnoklassniki":
            flags.append("--ok")
        check.add_argument(*flags, dest="selected_services", action="append_const", const=adapter.name,
                           help=f"Select {SERVICE_NAMES[adapter.name]}")
    check.add_argument("--dry-run", action="store_true", help="Show the plan without network requests")
    check.add_argument("--output", help="Save compact JSON as results arrive")
    formats = check.add_mutually_exclusive_group()
    formats.add_argument("--format", dest="output_format", choices=("text", "json"), help="Output format (default: text)")
    formats.add_argument("--json", dest="output_format", action="store_const", const="json", help="Print compact JSON only")
    formats.add_argument("--text", dest="output_format", action="store_const", const="text", help="Print normal text (default)")
    check.add_argument("--details", action="store_true", help="Show additional fields and per-service timing")
    check.add_argument("--timeout", type=float, default=None, help="Optional overall timeout in seconds; unlimited by default")
    check.add_argument("--allow-recovery", action="store_true", help="Allow identifier/recovery POST requests; the service may send a notification")
    check.add_argument("--snapchat-interactive", action="store_true", help="Open Chrome for manual Snapchat CAPTCHA; wait up to 5 minutes")
    check.add_argument("--amazon-interactive", action="store_true", help="Open Chrome for manual Amazon CAPTCHA; wait up to 5 minutes")
    args = None
    try:
        if not argv:
            if not sys.stdin.isatty():
                parser.print_help()
                return 2
            console.print(NOTICE, markup=False)
            number = input("Phone number: ").strip()
            region = input("ISO region (required without +; otherwise Enter): ").strip()
            show_methods(ADAPTERS)
            names = input("Services, comma-separated (Enter for defaults without Zoho): ").strip()
            argv = ["check", number]
            if region:
                argv += ["--region", region]
            if names:
                argv += ["--services", names]
            output = input("JSON file (Enter to skip saving): ").strip()
            if output:
                argv += ["--output", output]
            if input("Allow identifier/recovery requests? Services may send notifications. [y/N]: ").lower() != "y":
                return 0
            argv += ["--allow-recovery"]
        elif argv[0] not in ("check", "methods", "open", "normalize", "-h", "--help"):
            argv.insert(0, "check")
            if "--allow-recovery" not in argv:
                argv.append("--allow-recovery")
        args = parser.parse_args(argv)
        args.json = getattr(args, "output_format", None) == "json"
        if args.command == "open":
            adapters = select_services([args.service])
            if not webbrowser.open(adapters[0].source_url, new=2):
                console.print(adapters[0].source_url, markup=False)
            return 0
        names = getattr(args, "services", None)
        selected = getattr(args, "selected_services", None)
        if names is not None and selected:
            raise ValueError("Use either --services or individual service flags, not both.")
        adapters = select_services(names.split(",") if names is not None else selected)
        if args.command == "methods":
            show_methods(adapters if names is not None else ADAPTERS)
            return 0
        phone = normalize(args.number, args.region)
        if args.command == "normalize":
            print(json.dumps({**phone.to_dict(), "notice": NOTICE}, ensure_ascii=False, indent=2))
            return 0
        if args.timeout is not None and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600):
            raise ValueError("--timeout must be greater than 0 and at most 3600 seconds.")
        if not phone.possible or not phone.valid:
            raise ValueError("Number is not possible/valid according to numbering metadata; no requests sent.")
        if args.dry_run:
            print(json.dumps({"phone": phone.masked, "network_requests": 0, "would_send_code": False, "methods": [
                {"service": a.name, "method": a.method, "required_action": a.requirement} for a in adapters
            ]}, ensure_ascii=False, indent=2))
            return 0
        if not args.json:
            show_number(phone)
        results, interrupted = asyncio.run(execute(phone, adapters, args))
        if args.json:
            print(json.dumps(compact_report(results, interrupted=interrupted), ensure_ascii=False, indent=2))
        else:
            show_results(results, details=args.details)
        return 130 if interrupted else 1 if any(r.status == "error" for r in results) else 0
    except (ValueError, OSError) as error:
        if args is not None and args.json:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
        else:
            console.print(f"Error: {error}", markup=False)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
