"""Publikreach macOS entry point. GUI imports stay outside headless helpers."""
from __future__ import annotations

import argparse
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from runtime import SingleInstanceLock, bootstrap_workspace, exchange_token, self_test, start_server


# pywebview 6 normally creates JS API methods with new Function(). This static
# closure implements our single allowlisted method without relaxing script-src.
CSP_BRIDGE = r'''
;if (window.pywebview) {
  window.pywebview._createApi = function () {
    var bridge = window.pywebview;
    bridge._returnValuesCallbacks.choose_oauth_client = {};
    bridge.api.choose_oauth_client = function (csrf) {
      var id = String(Math.random()).slice(2);
      return new Promise(function (resolve, reject) {
        bridge._checkValue('choose_oauth_client', resolve, reject, id);
        bridge._jsApiCallback('choose_oauth_client', [csrf], id);
      });
    };
  };
}
'''


def configure_csp_bridge(window):
    """Adapt generated API creation and replies to the unchanged strict CSP."""
    direct = window.run_js

    def run_script(script):
        # The bridge definition is appended to the initial pywebview injection,
        # before its separate API-creation script executes. Replies run directly.
        return direct(script + '\n' + CSP_BRIDGE)

    window.run_js = run_script
    window.evaluate_js = run_script


class NativeApi:
    """Only a user-selected file can cross this bridge; arbitrary paths cannot."""

    def __init__(self, app, base_url, csrf):
        self._app = app
        self._base_url = base_url
        self._csrf = csrf
        self._window = None

    def choose_oauth_client(self, csrf):
        try:
            def authorized():
                current = urlsplit(self._window.get_current_url() or '')
                return (f'{current.scheme}://{current.netloc}' == self._base_url
                        and current.path in {'/settings', '/knowledge', '/setup'}
                        and isinstance(csrf, str) and secrets.compare_digest(csrf, self._csrf))
            if not authorized():
                return {'ok': False, 'error': 'Open Settings to choose your OAuth client.'}
            import webview
            selected = self._window.create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=False,
                file_types=('Google OAuth client (*.json)',),
            )
            if not selected:
                return {'ok': False, 'cancelled': True}
            if not authorized():
                return {'ok': False, 'error': 'Open Settings to choose your OAuth client.'}
            self._app.extensions['gmail_setup'].import_file(selected[0])
            return {'ok': True}
        except Exception:
            return {'ok': False, 'error': 'Choose a valid Google Cloud Desktop app OAuth client JSON file.'}


def launch(workspace):
    import webview
    from Foundation import NSHTTPCookie, NSURL
    from PyObjCTools import AppHelper

    runtime = start_server(workspace)
    try:
        cookie = exchange_token(runtime.app, runtime.base_url)
        bridge = NativeApi(runtime.app, runtime.base_url, cookie.csrf)
        window = webview.create_window(
            'Publikreach', html='<html><body>Opening Publikreach...</body></html>',
            js_api=bridge, width=1280, height=860, min_size=(900, 600), text_select=True,
        )
        bridge._window = window
        failed = []
        installed = []

        configure_csp_bridge(window)

        def install_session():
            # Called on Cocoa's main thread after the loading document is ready.
            # NSWindow.contentView is now the actual WKWebView in pywebview 6.
            try:
                native_cookies = NSHTTPCookie.cookiesWithResponseHeaderFields_forURL_(
                    {'Set-Cookie': f'{cookie.name}={cookie.value}; Path=/; HttpOnly; SameSite=Strict'},
                    NSURL.URLWithString_(runtime.base_url + '/'),
                )
                if len(native_cookies) != 1 or not native_cookies[0].isHTTPOnly():
                    raise RuntimeError('Session cookie unavailable.')
                view = window.native.contentView()
                store = view.configuration().websiteDataStore().httpCookieStore()
                store.setCookie_completionHandler_(native_cookies[0], lambda: window.load_url(runtime.base_url + '/'))
            except Exception:
                failed.append(True)
                window.destroy()

        def first_document_loaded():
            if not installed:
                installed.append(True)
                AppHelper.callAfter(install_session)

        window.events.loaded += first_document_loaded
        window.events.closed += runtime.stop
        webview.settings['ALLOW_FILE_URLS'] = False
        webview.start(gui='cocoa', debug=False, private_mode=True)
        if failed:
            raise RuntimeError('Native session setup failed.')
    finally:
        runtime.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Publikreach native macOS application')
    parser.add_argument('--self-test', action='store_true', help='Check loopback health and pages without a window.')
    parser.add_argument('--workspace', type=Path, help='Override the workspace for local verification.')
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            if args.workspace:
                self_test(args.workspace)
            else:
                with tempfile.TemporaryDirectory(prefix='publikreach-check-') as root:
                    self_test(Path(root))
            if sys.stdout:
                print('Publikreach self-test passed.')
            return 0
        # Acquire before bootstrapping so a second launch cannot reset a live
        # workspace or rotate any running session state.
        root = (args.workspace or Path.home() / 'Documents' / 'Publikreach').expanduser().resolve()
        root.joinpath('data').mkdir(parents=True, exist_ok=True)
        lock = SingleInstanceLock(root / 'data' / '.app.lock')
        if not lock.acquire():
            return 0
        try:
            launch(bootstrap_workspace(root))
        finally:
            lock.release()
        return 0
    except Exception:
        message = 'Publikreach could not start. Check workspace permissions and try again.'
        if args.self_test:
            message = 'Publikreach self-test failed.'
        if sys.stderr:
            print(message, file=sys.stderr)
        if not args.self_test:
            from AppKit import NSAlert
            alert = NSAlert.alloc().init()
            alert.setMessageText_('Publikreach')
            alert.setInformativeText_(message)
            alert.runModal()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
