"""Flask application: the analyzer UI plus Settings, Accounts, Repos, Runs and Logs.

Two modes, chosen by :func:`create_app`:

* **standalone** (``tflens serve --dir PATH``): only the analyzer page and its JSON API
  over one :class:`~tflens.webapi.ModelStore`. No data directory, no database, no log
  file: nothing outside ``PATH/tflens-out`` (or ``--model``) is written.
* **managed** (``tflens serve``): the data directory (``TFLENS_HOME`` / ``~/.tflens``)
  with the settings pages. The database and the secret key are created lazily, on the
  first save. Recorded runs open in the same analyzer at ``/runs/<id>/`` (read-only).

Security (both modes): the Host header must name this local server (DNS rebinding);
HTML form POSTs carry a per-session CSRF token; the JSON API's POST needs the
``X-TFLens: 1`` header (not sendable cross-site without a preflight, which is never
granted); every response has a ``default-src 'self'`` CSP, so the UI never loads
anything from elsewhere.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from markupsafe import Markup

from . import __version__, tfrepo
from .accounts import AUTH_MODE_LABELS, AUTH_MODES, Account, AccountStore, MemoryVault
from .accounts import discover_profiles as _discover_profiles
from .config import AppPaths, default_paths
from .crypto import LazySecretBox
from .db import reading, writing
from .logging_setup import LEVELS, LOG_FILE, configure_logging, read_log
from .server import CSP
from .settings import (
    MAX_DISPLAY_NAME,
    MAX_RETENTION_RUNS,
    MIN_RETENTION_RUNS,
    REGIONS,
    Settings,
    SettingsStore,
)
from .webapi import STATIC_DIR, Api, ModelStore
from .webapi import Response as ApiResponse

UTC = timezone.utc

log = logging.getLogger(__name__)

# DNS-rebinding protection: only these Host header names are served.
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_FORM_BYTES = 1024 * 1024
# Recorded runs whose model is kept in memory (LRU).
RUN_CACHE = 8
LOG_LIMIT_DEFAULT, LOG_LIMIT_MAX = 500, 5000
# The analyzer's JSON API routes (X-TFLens header instead of the form CSRF token).
API_ENDPOINTS = frozenset({"api", "run_api"})


def _ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name.removeprefix("[").removesuffix("]"))
    except ValueError:
        return False
    return True


def host_allowed(host: str, port: int | None, *, ip_literals: bool = False) -> bool:
    """True if ``host`` (a raw Host header) names this local server.

    With a known ``port`` the header must carry exactly that port (a bare name
    is only valid for port 80). With ``port=None`` any numeric port is accepted.
    ``ip_literals`` (``tflens serve --allow-remote``) also accepts any IP address as the
    name: DNS rebinding always arrives under a hostname, never an IP literal.
    """
    host = host.strip().lower()
    if host.startswith("["):  # [IPv6]:port
        name, _, rest = host.partition("]")
        name += "]"
        sep, host_port = (":", rest[1:]) if rest.startswith(":") else ("", rest)
    else:
        name, sep, host_port = host.partition(":")
    if name not in ALLOWED_HOSTS and not (ip_literals and _ip_literal(name)):
        return False
    if port is None:
        return not sep or host_port.isdigit()
    return host_port == str(port) if sep else port == 80


def utc_iso(value: Any) -> str | None:
    """Normalise a stored timestamp to ``YYYY-MM-DDTHH:MM:SSZ``.

    Stored times are timezone-aware (UTC); naive values (log file lines) are in the
    server's local time. Returns None for empty/unparseable input.
    """
    if not value:
        return None
    try:
        ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_time(value: Any) -> Markup | str:
    """``<time>`` element that static/localtime.js re-renders in the browser's time zone.

    The UTC text is only the no-JavaScript fallback; the ISO value stays in the tooltip.
    """
    iso = utc_iso(value)
    if iso is None:
        return value or ""
    fallback = iso.replace("T", " ").removesuffix("Z") + " UTC"
    return Markup('<time class="localtime" datetime="{0}" title="{0}">{1}</time>').format(
        iso, fallback
    )


def _flask_secret(paths: AppPaths) -> bytes:
    p = paths.flask_secret_path
    if p.exists():
        return p.read_bytes()
    key = secrets.token_bytes(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def create_app(
    home: str | os.PathLike[str] | None = None,
    *,
    store: ModelStore | None = None,
    testing: bool = False,
    port: int | None = None,
    allow_remote: bool = False,
    git_runner: tfrepo.Runner | None = None,
    git_protocols: tuple[str, ...] = tfrepo.ALLOWED_PROTOCOLS,
) -> Flask:
    """Build the app. With ``store`` it is the standalone analyzer of that model (no data
    directory at all); otherwise the managed app over ``home``. ``port`` is the port the
    server listens on; when given, the Host header must carry it (see
    :func:`host_allowed`; ``allow_remote`` also accepts IP-address Host headers).
    ``git_runner`` / ``git_protocols`` are for tests."""
    app = Flask(__name__, static_folder=None)
    app.config.update(
        TESTING=testing,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        MAX_CONTENT_LENGTH=MAX_FORM_BYTES,
    )
    ext: dict[str, Any] = {
        "managed": store is None,
        "port": port,
        "allow_remote": allow_remote,
    }
    app.extensions["tflens"] = ext
    if store is not None:
        # Standalone: a per-process session key, nothing written anywhere.
        app.config["SECRET_KEY"] = secrets.token_bytes(32)
        ext["api"] = Api(store)
        _register_common(app)
        _register_standalone(app)
        return app

    paths = default_paths(home).ensure()
    app.config["SECRET_KEY"] = _flask_secret(paths)
    settings_store = SettingsStore(paths.db_path)
    # The key file is only created when the first secret is stored.
    box = LazySecretBox(paths.key_path)
    ext.update(
        paths=paths,
        store=settings_store,
        box=box,
        # Memory-only credentials live in this process for the lifetime of the app.
        accounts=AccountStore(paths.db_path, box, MemoryVault()),
        git_runner=git_runner,
        git_protocols=git_protocols,
        runs=OrderedDict(),
        runs_lock=threading.Lock(),
    )
    apply_log_dir(app, settings_store.load())
    log.info("TFLens %s started (data dir %s)", __version__, paths.home)
    _register_common(app)
    _register_managed(app)
    return app


def log_dir_for(paths: AppPaths, settings: Settings) -> Path:
    return Path(settings.log_dir).expanduser() if settings.log_dir else paths.default_log_dir


def apply_log_dir(app: Flask, settings: Settings) -> Path:
    paths: AppPaths = app.extensions["tflens"]["paths"]
    log_dir = log_dir_for(paths, settings)
    configure_logging(log_dir)
    app.extensions["tflens"]["log_dir"] = log_dir
    return log_dir


def _ext() -> dict[str, Any]:
    return current_app.extensions["tflens"]


def _paths() -> AppPaths:
    return _ext()["paths"]


def _store() -> SettingsStore:
    return _ext()["store"]


def _accounts() -> AccountStore:
    return _ext()["accounts"]


def _safe_next(target: str | None, default: str) -> str:
    """Only same-site paths ("/..."), never "//host", a scheme or a backslash trick."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


def to_flask(resp: ApiResponse) -> Response:
    out = Response(resp.body, status=resp.status, content_type=resp.content_type)
    for k, v in resp.headers.items():
        out.headers[k] = v
    return out


def _api_target(sub: str) -> str:
    query = request.query_string.decode("latin-1")
    return f"/api/{sub}" + (f"?{query}" if query else "")


def _register_common(app: Flask) -> None:
    # Registered first so it runs before anything else touches the request.
    @app.before_request
    def _trusted_host() -> None:
        # Raw header: werkzeug's request.host drops default ports.
        host = request.headers.get("Host", "")
        if not host_allowed(host, _ext()["port"], ip_literals=_ext()["allow_remote"]):
            log.warning(
                "rejected request %s with untrusted Host header %r", request.path, host[:100]
            )
            abort(400, "invalid Host header")

    @app.before_request
    def _csrf() -> None:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method != "POST":
            return
        if request.endpoint in API_ENDPOINTS:
            # JSON API (fetch from the analyzer page): a custom header cannot be sent
            # cross-site without a CORS preflight, and none is ever granted.
            if request.headers.get("X-TFLens") != "1":
                log.warning("rejected POST %s without the X-TFLens header", request.path)
                abort(403, "missing X-TFLens header")
            return
        sent = request.form.get("csrf_token", "")
        if not secrets.compare_digest(sent, session["csrf"]):
            log.warning("rejected POST %s with bad CSRF token", request.path)
            abort(400, "invalid CSRF token")

    @app.after_request
    def _headers(resp: Response) -> Response:
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.context_processor
    def _globals() -> dict[str, Any]:
        return {
            "csrf_token": session.get("csrf", ""),
            "managed": _ext()["managed"],
            "version": __version__,
        }

    app.add_template_filter(local_time, "localtime")

    @app.get("/static/<path:filename>")
    def static(filename: str):
        return send_from_directory(STATIC_DIR, filename, max_age=0)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    def _error(e):
        code = getattr(e, "code", 400) or 400
        message = getattr(e, "description", "") or "Error"
        if code == 404 and message.startswith("The requested URL"):
            message = "Not found"
        if request.endpoint in API_ENDPOINTS or not _ext()["managed"]:
            return Response(f"{message}\n", status=code, content_type="text/plain; charset=utf-8")
        return render_template("error.html", message=message), code


def _register_standalone(app: Flask) -> None:
    @app.get("/")
    @app.get("/index.html")
    def index():
        return to_flask(_ext()["api"].handle("GET", "/"))

    @app.route("/api/<path:sub>", methods=["GET", "HEAD", "POST"])
    def api(sub: str):
        return to_flask(_ext()["api"].handle(request.method, _api_target(sub)))


# -- managed mode ------------------------------------------------------------------------


def _read():
    """Context manager: a read connection, or None before the database exists."""
    return reading(_paths().db_path)


def _write():
    """Context manager: a write connection (creates the database on first use)."""
    return writing(_paths().db_path)


def _repo_or_404(repo_id: int) -> dict[str, Any]:
    with _read() as conn:
        repo = tfrepo.get_repo(conn, repo_id) if conn is not None else None
    if repo is None:
        abort(404)
    return repo


def _run_api(run_id: int) -> Api:
    """The read-only analyzer API of a recorded run (cached, LRU)."""
    ext = _ext()
    with ext["runs_lock"]:
        cached = ext["runs"].get(run_id)
        if cached is not None:
            ext["runs"].move_to_end(run_id)
            return cached
    with _read() as conn:
        run = tfrepo.get_run(conn, run_id) if conn is not None else None
    if run is None or not run["model_path"]:
        abort(404)
    title = run["repo"] + (f" ({run['env']})" if run["env"] else "") + f" · run {run_id}"
    api = Api(
        ModelStore(
            Path(run["root_path"] or Path(run["model_path"]).parent),
            Path(run["model_path"]),
            readonly=True,
            title=title,
        )
    )
    with ext["runs_lock"]:
        ext["runs"][run_id] = api
        while len(ext["runs"]) > RUN_CACHE:
            ext["runs"].popitem(last=False)
    return api


def _forget_run(run_id: int) -> None:
    with _ext()["runs_lock"]:
        _ext()["runs"].pop(run_id, None)


def _analyze(repo: dict[str, Any], env: dict[str, Any] | None) -> int | None:
    """Create a run (flashes the outcome); returns its id, None on failure."""
    label = repo["name"] + (f" / {env['env']}" if env else "")
    try:
        with _write() as conn:
            run_id = tfrepo.create_run(conn, repo, env, _paths().out_dir, _ext()["box"].decrypt)
            pruned = tfrepo.prune_runs(
                conn,
                _paths().out_dir,
                _store().load().retention_runs,
                repo["id"],
                env["env"] if env else "",
            )
            run = tfrepo.get_run(conn, run_id)
    except ValueError as exc:
        flash(f"Analyze {label}: {exc}", "error")
        return None
    except Exception:
        log.exception("analysis of repo=%s env=%s failed", repo["id"], env and env["id"])
        flash(f"Analyze {label} failed. See the log for details.", "error")
        return None
    if pruned:
        with _ext()["runs_lock"]:
            _ext()["runs"].clear()  # pruned runs must not be served from the cache
    log.info(
        "run %s: repo=%s env=%s nodes=%s edges=%s (%d old run(s) pruned)",
        run_id,
        repo["id"],
        env["env"] if env else "-",
        run["node_count"] if run else "?",
        run["edge_count"] if run else "?",
        pruned,
    )
    flash(
        Markup('Analyzed {0}: run {1} — <a href="{2}">open in the analyzer</a>').format(
            label, run_id, url_for("run_view", run_id=run_id)
        ),
        "ok",
    )
    return run_id


def _sync(repo: dict[str, Any]) -> bool:
    ext = _ext()
    kwargs: dict[str, Any] = {"allowed_protocols": ext["git_protocols"]}
    if ext["git_runner"] is not None:
        kwargs["runner"] = ext["git_runner"]
    log.info("sync of repo=%s (%s) started", repo["id"], repo["source"])
    result = tfrepo.sync_repo(repo, _paths().repos_dir, ext["box"].decrypt, **kwargs)
    with _write() as conn:
        tfrepo.record_sync(conn, repo["id"], result)
    if not result.ok:
        log.warning("sync of repo=%s failed: %s", repo["id"], result.detail)
        flash(f"Sync of {repo['name']} failed: {result.detail}", "error")
        return False
    log.info("sync of repo=%s ok: commit %s", repo["id"], result.commit[:12] or "-")
    commit = f" at {result.commit[:12]}" if result.commit else ""
    flash(f"Synced {repo['name']}{commit} ({result.detail})", "ok")
    return True


def _repo_form_fields(f) -> dict[str, Any]:
    return {
        "name": f.get("name", ""),
        "path": f.get("path", ""),
        "url": f.get("url", ""),
        "branch": f.get("branch", ""),
        "subdir": f.get("subdir", ""),
        "git_auth": f.get("git_auth", "agent"),
        "ssh_key_path": f.get("ssh_key_path", ""),
        "token_user": f.get("token_user", ""),
    }


def _register_managed(app: Flask) -> None:
    @app.get("/")
    def overview():
        return redirect(url_for("tfrepos_page"))

    # -- settings ---------------------------------------------------------------------

    @app.get("/settings")
    def settings_page():
        return render_template(
            "settings.html",
            s=_store().load(),
            default_log_dir=_paths().default_log_dir,
            retention_limits=(MIN_RETENTION_RUNS, MAX_RETENTION_RUNS),
            paths=_paths(),
            db_ready=_paths().db_path.is_file(),
        )

    @app.post("/settings")
    def settings_save():
        f = request.form
        try:
            _store().save(
                log_dir=f.get("log_dir", ""),
                host=f.get("host") if "host" in f else None,
                port=f.get("port") if "port" in f else None,
                retention_runs=f.get("retention_runs") if "retention_runs" in f else None,
                # Checkboxes are absent when unticked: only for the full form.
                allow_remote=f.get("allow_remote") == "1" if "server_form" in f else None,
                auto_regenerate=f.get("auto_regenerate") == "1" if "server_form" in f else None,
            )
        except ValueError as exc:
            flash(f"Settings not saved: {exc}", "error")
            return redirect(url_for("settings_page"))
        saved = _store().load()
        new_dir = apply_log_dir(current_app, saved)
        log.info(
            "settings saved: log_dir=%s host=%s port=%s allow_remote=%s auto_regenerate=%s "
            "retention_runs=%s",
            new_dir,
            saved.host,
            saved.port,
            saved.allow_remote,
            saved.auto_regenerate,
            saved.retention_runs,
        )
        flash("Settings saved", "ok")
        return redirect(url_for("settings_page"))

    # -- logs ----------------------------------------------------------------------------

    @app.get("/logs")
    def logs():
        level = request.args.get("level", "")
        q = request.args.get("q", "")
        limit = min(max(request.args.get("limit", LOG_LIMIT_DEFAULT, type=int), 1), LOG_LIMIT_MAX)
        log_dir = _ext()["log_dir"]
        entries = read_log(log_dir, min_level=level, q=q, limit=limit)
        return render_template(
            "logs.html",
            entries=entries,
            level=level,
            q=q,
            limit=limit,
            limit_max=LOG_LIMIT_MAX,
            levels=LEVELS,
            log_path=log_dir / LOG_FILE,
        )

    # -- accounts (parity with IPLens; not used by the analysis) ---------------------------

    @app.get("/accounts")
    def accounts_page():
        return render_template(
            "accounts.html", accounts=[a.public_dict() for a in _accounts().list()]
        )

    def _account_form(acct: Account, status: int = 200):
        profiles = _discover_profiles()
        return render_template(
            "account_form.html",
            a=acct.public_dict(),
            modes=AUTH_MODES,
            mode_labels=AUTH_MODE_LABELS,
            regions=REGIONS,
            profiles=profiles,
            profile_known=acct.profile in {p.name for p in profiles},
            max_name=MAX_DISPLAY_NAME,
        ), status

    def _save_account(account_id: int | None):
        f = request.form
        fields: dict[str, Any] = {
            "display_name": f.get("display_name", ""),
            "region": f.get("region_custom", "").strip() or f.get("region", ""),
            "auth_mode": f.get("auth_mode", "env"),
            "profile": f.get("profile", ""),
            "memory_only": f.get("memory_only") == "on",
        }
        try:
            new_id = _accounts().save(
                account_id,
                **fields,
                access_key_id=f.get("access_key_id", ""),
                secret_access_key=f.get("secret_access_key") or None,
                paste=f.get("temporary_paste") or None,
            )
        except ValueError as exc:
            # Messages never quote secrets, and the form is re-rendered without them.
            flash(str(exc), "error")
            shown = _accounts().get(account_id) or Account()
            for key, value in fields.items():
                if key != "auth_mode" or value in AUTH_MODES:
                    setattr(shown, key, value)
            return _account_form(shown, 400)
        acct = _accounts().get(new_id)
        log.info(
            "account %s: id=%s auth_mode=%s region=%s memory_only=%s",
            "created" if account_id is None else "updated",
            new_id,
            acct.auth_mode if acct else "?",
            acct.region if acct else "?",
            acct.memory_only if acct else "?",
        )
        flash("Account saved", "ok")
        return redirect(url_for("accounts_page"))

    @app.route("/accounts/new", methods=["GET", "POST"])
    def account_new():
        if request.method == "GET":
            return _account_form(Account())
        return _save_account(None)

    @app.route("/accounts/<int:account_id>/edit", methods=["GET", "POST"])
    def account_edit(account_id: int):
        acct = _accounts().get(account_id)
        if acct is None:
            abort(404)
        if request.method == "GET":
            return _account_form(acct)
        return _save_account(account_id)

    @app.post("/accounts/<int:account_id>/delete")
    def account_delete(account_id: int):
        if not _accounts().delete(account_id):
            abort(404)
        log.info("account deleted: id=%s", account_id)
        flash("Account deleted", "ok")
        return redirect(url_for("accounts_page"))

    # -- repositories and environments -------------------------------------------------

    @app.get("/repos")
    def tfrepos_page():
        with _read() as conn:
            repos = tfrepo.list_repos(conn) if conn is not None else []
        return render_template(
            "repos.html",
            repos=repos,
            sources=tfrepo.SOURCE_LABELS,
            auth_modes=tfrepo.GIT_AUTH_LABELS,
            form=request.args,
        )

    @app.post("/repos")
    def tfrepo_add():
        f = request.form
        source = f.get("source", "local")
        try:
            with _write() as conn:
                repo_id = tfrepo.add_repo(
                    conn,
                    repos_dir=_paths().repos_dir,
                    source=source,
                    token=f.get("token", ""),
                    encrypt=_ext()["box"].encrypt,
                    allowed_protocols=_ext()["git_protocols"],
                    **_repo_form_fields(f),
                )
        except ValueError as exc:
            flash(f"Repository not added: {exc}", "error")
            return redirect(url_for("tfrepos_page"))
        # Never the token; the URL has no credentials (refused by validate_git_url).
        log.info("repository added: id=%s source=%s auth=%s", repo_id, source, f.get("git_auth"))
        flash("Repository added", "ok")
        return redirect(url_for("settings_tfrepo", repo_id=repo_id))

    @app.get("/repos/<int:repo_id>")
    def settings_tfrepo(repo_id: int):
        repo = tfrepo.public_repo(_repo_or_404(repo_id))
        with _read() as conn:
            envs = tfrepo.list_envs(conn, repo_id)
            runs = tfrepo.list_runs(conn, repo_id, limit=20)
        return render_template(
            "tfrepo.html",
            repo=repo,
            envs=envs,
            runs=runs,
            auth_modes=tfrepo.GIT_AUTH_LABELS,
            root_exists=Path(repo["path"]).is_dir(),
        )

    @app.post("/repos/<int:repo_id>")
    def tfrepo_save(repo_id: int):
        _repo_or_404(repo_id)
        f = request.form
        try:
            with _write() as conn:
                tfrepo.update_repo(
                    conn,
                    repo_id,
                    token=f.get("token", ""),
                    encrypt=_ext()["box"].encrypt,
                    allowed_protocols=_ext()["git_protocols"],
                    **_repo_form_fields(f),
                )
        except ValueError as exc:
            flash(f"Repository not saved: {exc}", "error")
            return redirect(url_for("settings_tfrepo", repo_id=repo_id))
        log.info("repository updated: id=%s auth=%s", repo_id, f.get("git_auth", "-"))
        flash("Repository saved", "ok")
        return redirect(url_for("settings_tfrepo", repo_id=repo_id))

    @app.post("/repos/<int:repo_id>/delete")
    def tfrepo_delete(repo_id: int):
        with _write() as conn:
            if not tfrepo.delete_repo(conn, repo_id, _paths().repos_dir):
                abort(404)
        log.info("repository removed: id=%s", repo_id)
        flash("Repository removed from TFLens (a local directory itself is never touched)", "ok")
        return redirect(url_for("tfrepos_page"))

    @app.post("/repos/<int:repo_id>/sync")
    def tfrepo_sync(repo_id: int):
        repo = _repo_or_404(repo_id)
        ok = _sync(repo)
        if ok and _store().load().auto_regenerate:
            repo = _repo_or_404(repo_id)
            with _read() as conn:
                envs = tfrepo.list_envs(conn, repo_id)
            for env in envs or [None]:
                _analyze(repo, env)
        return redirect(
            _safe_next(request.form.get("next"), url_for("settings_tfrepo", repo_id=repo_id))
        )

    @app.post("/repos/<int:repo_id>/analyze")
    def tfrepo_analyze(repo_id: int):
        repo = _repo_or_404(repo_id)
        env = None
        raw = request.form.get("env_id", "")
        if raw:
            with _read() as conn:
                env = tfrepo.get_env(conn, repo_id, int(raw)) if raw.isdigit() else None
            if env is None:
                abort(404)
        run_id = _analyze(repo, env)
        if run_id is not None and request.form.get("open") == "1":
            return redirect(url_for("run_view", run_id=run_id))
        return redirect(url_for("settings_tfrepo", repo_id=repo_id))

    @app.post("/repos/<int:repo_id>/envs")
    def tfrepo_env_add(repo_id: int):
        _repo_or_404(repo_id)
        f = request.form
        try:
            with _write() as conn:
                env_id = tfrepo.add_env(
                    conn,
                    repo_id,
                    f.get("env", ""),
                    f.get("var_files", ""),
                    f.get("extra_vars", ""),
                    _ext()["box"].encrypt,
                )
        except ValueError as exc:  # names the line / key, never a value
            flash(f"Environment not added: {exc}", "error")
            return redirect(url_for("settings_tfrepo", repo_id=repo_id))
        log.info("environment added: repo=%s env_id=%s", repo_id, env_id)
        flash("Environment added", "ok")
        return redirect(url_for("settings_tfrepo", repo_id=repo_id))

    @app.post("/repos/<int:repo_id>/envs/<int:env_id>")
    def tfrepo_env_save(repo_id: int, env_id: int):
        _repo_or_404(repo_id)
        f = request.form
        extra: str | None = None
        if f.get("extra_clear") == "1":
            extra = ""
        elif f.get("extra_vars", "").strip():
            extra = f["extra_vars"]
        try:
            with _write() as conn:
                tfrepo.update_env(
                    conn,
                    repo_id,
                    env_id,
                    f.get("env", ""),
                    f.get("var_files", ""),
                    extra,
                    _ext()["box"].encrypt,
                )
        except ValueError as exc:
            flash(f"Environment not saved: {exc}", "error")
            return redirect(url_for("settings_tfrepo", repo_id=repo_id))
        log.info("environment updated: repo=%s env_id=%s", repo_id, env_id)
        flash("Environment saved", "ok")
        return redirect(url_for("settings_tfrepo", repo_id=repo_id))

    @app.post("/repos/<int:repo_id>/envs/<int:env_id>/delete")
    def tfrepo_env_delete(repo_id: int, env_id: int):
        with _write() as conn:
            if not tfrepo.delete_env(conn, repo_id, env_id):
                abort(404)
        log.info("environment deleted: repo=%s env_id=%s", repo_id, env_id)
        flash("Environment deleted", "ok")
        return redirect(url_for("settings_tfrepo", repo_id=repo_id))

    # -- runs -------------------------------------------------------------------------

    @app.get("/runs")
    def runs_page():
        raw = request.args.get("repo", "")
        repo_id = int(raw) if raw.isdigit() else None
        with _read() as conn:
            runs = tfrepo.list_runs(conn, repo_id) if conn is not None else []
            repos = tfrepo.list_repos(conn) if conn is not None else []
        return render_template("runs.html", runs=runs, repos=repos, repo_id=repo_id)

    @app.get("/runs/<int:run_id>/")
    def run_view(run_id: int):
        return to_flask(_run_api(run_id).handle("GET", "/"))

    @app.route("/runs/<int:run_id>/api/<path:sub>", methods=["GET", "HEAD", "POST"])
    def run_api(run_id: int, sub: str):
        return to_flask(_run_api(run_id).handle(request.method, _api_target(sub)))

    @app.post("/runs/<int:run_id>/delete")
    def run_delete(run_id: int):
        with _write() as conn:
            if not tfrepo.delete_run(conn, run_id, _paths().out_dir):
                abort(404)
        _forget_run(run_id)
        log.info("run deleted: id=%s", run_id)
        flash(f"Run {run_id} deleted", "ok")
        return redirect(_safe_next(request.form.get("next"), url_for("runs_page")))
