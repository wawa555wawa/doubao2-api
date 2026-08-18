from __future__ import annotations

import argparse

import uvicorn

from .config import get_settings
from .credentials import CredentialStore
from .session_refresh import refresh_credentials


def main() -> None:
    parser = argparse.ArgumentParser(prog="doubao2-api")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动 API 服务")
    sub.add_parser("login", help="扫码登录，刷新凭证")
    args = parser.parse_args()

    settings = get_settings()
    if args.command == "login":
        store = CredentialStore(settings.data_dir / "credentials.json")
        refresh_credentials(store, settings.login_timeout, settings.headless)
        print(f"登录成功，凭证已保存到 {store.path}")
    else:
        uvicorn.run(
            "doubao2_api.main:app", host=settings.host, port=settings.port
        )


if __name__ == "__main__":
    main()
