"""Optional local Publikreach web application."""
from __future__ import annotations
from pathlib import Path

def create_app(workspace=None, testing=False, send_transport=None):
    from vcoutreach.web.server.app import create_app as factory
    return factory(workspace,testing=testing,send_transport=send_transport)

def seed_example_deck(app):
    """First run only: copy the bundled example deck into the workspace."""
    try:
        from vcoutreach.web.server.workspace_settings import ensure_example_deck
        with app.app_context():
            return ensure_example_deck()
    except Exception:
        return False

def serve(workspace=None, *, port=None, no_browser=False):
    import contextlib
    import io
    import sys
    import webbrowser
    try:
        from werkzeug.serving import make_server
        import flask
    except ImportError:
        print('The web app needs Flask. Run: pip install -e ".[web]"',file=sys.stderr)
        return 2
    if port is not None and (isinstance(port,bool) or not isinstance(port,int) or not 1024<=port<=65535):
        print('Port must be an integer between 1024 and 65535.',file=sys.stderr)
        return 2
    app=create_app(workspace or Path.cwd())
    seed_example_deck(app)
    server=None
    selected=None
    for candidate in ([port] if port is not None else range(8765,8786)):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                server=make_server('127.0.0.1',candidate,app,threaded=True)
            selected=candidate
            break
        except (OSError,SystemExit):
            continue
    if server is None:
        print('cannot start ui: requested port is unavailable.' if port is not None else 'cannot start ui: no available port from 8765 through 8785.',file=sys.stderr)
        return 2
    app.config['PORT']=selected
    link=f'http://127.0.0.1:{selected}/?token={app.config["ACCESS_TOKEN"]}'
    print('Open this link: '+link,flush=True)
    if not no_browser:
        webbrowser.open(link)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        from vcoutreach.web.gmail.scheduler import stop_scheduler
        stop_scheduler()
    return 0

__all__=['create_app','seed_example_deck','serve']
