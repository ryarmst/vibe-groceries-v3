"""`vibe` command-line tool for the server operator (run inside the container):

  vibe master-key                  print a new random MASTER_KEY value
  vibe pcid status                 is the household signed in to PC ID? (never prints tokens)
  vibe pcid login start            sign-in URL, if you'd rather not use Settings in the web app
  vibe pcid login finish ADDRESS   the address the PC ID tab ended on (or - to read it from stdin)
  vibe pcid check                  renew the token and fetch past orders, to prove it works
  vibe users                       people who have used the app (from Cloudflare Access)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vibe", description="vibe-groceries server administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("master-key", help="print a new MASTER_KEY")
    pc = sub.add_parser("pcid", help="the household's PC ID (No Frills) sign-in")
    pc.add_argument("action", choices=["status", "login", "check", "logout"])
    pc.add_argument("step", nargs="?", choices=["start", "finish"])
    pc.add_argument("address", nargs="?")
    sub.add_parser("users", help="list users")
    args = p.parse_args(argv)

    if args.cmd == "master-key":
        print(base64.b64encode(os.urandom(32)).decode())
        return 0

    from shopping import context
    from shopping.config import settings
    from shopping.auth.session import AuthRequired, SessionUnavailable
    from shopping.database import run_migrations, session_scope
    from shopping.pcid.oauth import AuthError

    run_migrations()
    if args.cmd == "users":
        from sqlalchemy import select

        from shopping.models import User

        with session_scope() as session:
            for d in session.scalars(select(User).order_by(User.created_at)):
                print(f"{d.id}  last seen {d.last_seen_at or '-'}  {d.name}")
        return 0

    def show() -> None:
        st = context.auth.status()
        if st.state != "connected":
            print("No Frills isn't signed in.")
            return
        print(f"Signed in (customer {st.customer_id}) by {st.signed_in_by or '-'} at {st.signed_in_at}; "
              f"last renewed {st.last_updated}.")

    try:
        if args.action == "status":
            show()
        elif args.action == "login" and args.step == "start":
            print(context.pcid.start_login())
            print("\nSign in, then: vibe pcid login finish '<address the tab ended on>'  (within 30 minutes)")
        elif args.action == "login" and args.step == "finish":
            address = args.address if args.address and args.address != "-" else sys.stdin.readline()
            asyncio.run(context.pcid.finish_login(address, by="cli"))
            show()
        elif args.action == "check":
            async def check():
                c = context.client()
                orders, cart = await c.list_past_orders(), await c.get_cart()
                await context.shutdown()
                return orders, cart
            orders, cart = asyncio.run(check())
            print(f"No Frills accepted the session: {len(orders)} past orders.")
            print(f"Your cart's store: {cart.store_name} (STORE_ID={cart.store_id})"
                  + ("" if cart.store_id == settings.store_id else f"; this server is set to STORE_ID={settings.store_id or '(empty)'}"))
            show()
        elif args.action == "logout":
            asyncio.run(context.auth.clear())
            print("Signed out.")
        else:
            p.error("pcid login needs start or finish")
    except (AuthError, AuthRequired, SessionUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
