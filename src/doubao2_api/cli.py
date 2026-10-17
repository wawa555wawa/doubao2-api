from __future__ import annotations

import argparse
import sys

import uvicorn

from .config import Settings, get_settings
from .credentials import CredentialStore, format_expiry
from .session_refresh import SessionRefreshError, refresh_credentials


def _print_status(store: CredentialStore, settings: Settings) -> None:
    print(f"凭证文件：{store.path}")
    if store.is_empty():
        print("登录态：无")
        print("提示：运行 `doubao2-api login` 扫码登录")
    else:
        expires_at = store.session_expires_at
        remaining = store.session_time_remaining()
        if remaining is not None and remaining <= 0:
            state = "已过期"
        elif expires_at is None:
            state = "有效（有效期未知）"
        else:
            state = "有效"
        print(f"登录态：{state}")
        print(f"到期时间：{format_expiry(expires_at)}")
        print(f"Cookie 数量：{len(store.cookies)}")
        print(f"设备参数：{'、'.join(sorted(store.device)) or '（缺失，风控会拒绝）'}")
        if remaining is not None and remaining <= 0:
            print("提示：运行 `doubao2-api login` 重新扫码")

    profile = settings.resolved_profile_dir
    print(f"保活：{'已启用' if settings.keepalive_enabled else '未启用'}"
          f"（每 {settings.keepalive_interval / 3600:.1f} 小时访问一次）")
    print(f"浏览器 profile：{profile}（{'已创建' if profile.exists() else '尚未创建'}）")
    if settings.keepalive_enabled and not profile.exists():
        print("提示：先运行 `doubao2-api login --profile` 在专用 profile 里登录一次")


def _login(settings: Settings, use_profile: bool, headless: bool | None) -> int:
    store = CredentialStore(settings.data_dir / "credentials.json")
    headless = settings.headless if headless is None else headless
    try:
        if use_profile:
            from .keepalive import login_with_profile

            print(f"使用专用浏览器 profile：{settings.resolved_profile_dir}")
            print("若 profile 尚未登录，请在弹出的窗口里用豆包 App 扫码。")
            login_with_profile(
                store,
                settings.resolved_profile_dir,
                timeout=settings.login_timeout,
                headless=headless,
                channel=settings.browser_channel,
            )
        else:
            refresh_credentials(store, settings.login_timeout, headless)
    except SessionRefreshError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1
    print(f"登录成功，凭证已保存到 {store.path}")
    print(f"有效期至：{format_expiry(store.session_expires_at)}")
    if use_profile:
        print(
            "该 profile 已登录，后续保活无需扫码；"
            "如需自动保活请设置 KEEPALIVE_ENABLED=true 后重启 serve。"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="doubao2-api")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="启动 API 服务")
    login = sub.add_parser("login", help="扫码登录，刷新凭证")
    login.add_argument(
        "--profile",
        action="store_true",
        help="在持久化浏览器 profile 里登录（配合 KEEPALIVE_ENABLED 可免重复扫码）",
    )
    login.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="无头模式（profile 已登录时可用；未登录则看不到二维码）",
    )
    sub.add_parser("status", help="查看登录态、有效期与保活配置")
    args = parser.parse_args()

    settings = get_settings()
    if args.command == "status":
        _print_status(CredentialStore(settings.data_dir / "credentials.json"), settings)
        return

    if args.command == "login":
        code = _login(settings, args.profile, args.headless)
        if code:
            raise SystemExit(code)
        return

    uvicorn.run("doubao2_api.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
