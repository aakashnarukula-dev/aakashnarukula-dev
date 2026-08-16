#!/usr/bin/env python3
"""Command line entry point for the medicine reminder agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from app.config import ConfigError, load_config  # noqa: E402
from app.models import RunStatus  # noqa: E402
from app.web import build_services, create_app  # noqa: E402

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=LOG_FORMAT,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(f"timezone            : {config.timezone}")
    print(f"provider            : {config.provider.name}")
    print(f"confirmation mode   : {config.call.confirmation_mode}")
    print(f"attempts / retry    : {config.call.max_attempts} calls, "
          f"{config.call.retry_delay_seconds}s apart")
    print(f"telegram alerts     : {'on' if config.telegram.enabled else 'off'}")
    print(f"public base url     : {config.provider.public_base_url or '(none — polling mode)'}")
    print(f"database            : {config.database_path}")
    print("\nrecipients:")
    for recipient in config.recipients.values():
        print(f"  - {recipient.id:<12} {recipient.name} <{recipient.phone}> "
              f"[{recipient.language}/{recipient.voice}]")
    print("\nschedules:")
    for schedule in config.schedules:
        state = "" if schedule.enabled else "  (disabled)"
        print(f"  - {schedule.id:<12} cron '{schedule.cron}' -> "
              f"{config.recipient(schedule.recipient_id).name}{state}")
        print(f"    \"{schedule.message}\"")
    print("\nconfig OK")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.config:
        os.environ["CONFIG_PATH"] = args.config
    load_config(args.config)  # fail fast on bad config before binding the port
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )
    return 0


async def _call_now(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    services = build_services(config)
    try:
        schedule = config.schedule(args.schedule)
        run_id = await services.engine.trigger_schedule(schedule)
        if run_id is None:
            print("this reminder was already triggered for the current minute")
            return 1

        deadline = args.wait
        elapsed = 0
        while elapsed < deadline:
            await asyncio.sleep(5)
            elapsed += 5
            await services.engine.tick()
            run = services.store.get_run(run_id)
            if run is None:
                break
            print(f"  [{elapsed:>4}s] status={run.status.value} attempt={run.attempt} "
                  f"outcome={run.last_outcome.value if run.last_outcome else '-'}")
            if run.status in {RunStatus.ACKNOWLEDGED, RunStatus.ESCALATED,
                              RunStatus.FAILED}:
                break

        run = services.store.get_run(run_id)
        print(f"\nfinal status: {run.status.value if run else 'unknown'}")
        return 0 if run and run.status is RunStatus.ACKNOWLEDGED else 2
    finally:
        await services.notifier.aclose()
        await services.engine.provider.aclose()
        services.store.close()


async def _test_telegram(args: argparse.Namespace) -> int:
    from app.notifier import TelegramNotifier

    config = load_config(args.config)
    notifier = TelegramNotifier(config.telegram)
    try:
        ok = await notifier.send(
            "✅ <b>Medicine reminder agent</b>\nTelegram alerts are wired up correctly."
        )
    finally:
        await notifier.aclose()
    print("sent" if ok else "failed — check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="medicine-reminder-agent",
        description="Calls people at fixed times to remind them to take their medicine.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the scheduler and webhook server")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="validate the config and print the plan")
    check.set_defaults(func=cmd_check)

    call_now = sub.add_parser("call-now", help="place one reminder immediately")
    call_now.add_argument("schedule", help="schedule id from config.yaml")
    call_now.add_argument("--wait", type=int, default=420,
                          help="seconds to follow the run for (default 420)")
    call_now.set_defaults(func=lambda a: asyncio.run(_call_now(a)))

    telegram = sub.add_parser("test-telegram", help="send a test Telegram alert")
    telegram.set_defaults(func=lambda a: asyncio.run(_test_telegram(a)))

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args((argv or []) + ["serve"])

    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
