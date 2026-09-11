"""Flask session and CSRF integration over the shared account boundary."""

from dataclasses import dataclass

from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf import CSRFProtect, FlaskForm
from sqlalchemy import select
from wtforms import PasswordField, StringField
from wtforms.validators import InputRequired, Length

from postcardscene.accounts import Administrator, authenticate, revoke_sessions
from postcardscene.session_secret import read_secret
from postcardscene.web.auth_cli import auth_cli
from postcardscene.web.login_limiter import LoginLimiter

auth = Blueprint("auth", __name__)


@dataclass
class WebUser(UserMixin):
    id: str


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[InputRequired(), Length(max=64)])
    password = PasswordField("Password", validators=[InputRequired(), Length(max=128)])


def load_user(identity):
    database = current_app.extensions["postcardscene.database"]
    with database.transaction() as transaction:
        found = transaction.scalar(
            select(Administrator.id).where(Administrator.session_id == identity)
        )
    return WebUser(identity) if found else None


def init_auth(app):
    # Missing authority keeps explicit setup/migration CLI reachable, but web fails closed.
    try:
        app.secret_key = read_secret(app.config["SESSION_SECRET_PATH"])
    except FileNotFoundError:
        app.secret_key = None
    # The protected file is the sole signing authority, including verification.
    app.config["SECRET_KEY_FALLBACKS"] = None
    manager = LoginManager(app)
    manager.login_view = "auth.login"
    manager.login_message = None
    manager.session_protection = "strong"
    manager.user_loader(load_user)
    app.before_request(require_secret)
    app.extensions["postcardscene.login_limiter"] = LoginLimiter()
    app.before_request(limit_login)
    CSRFProtect(app)
    app.register_blueprint(auth)
    app.cli.add_command(auth_cli)


def require_secret():
    if current_app.secret_key is None and request.endpoint != "static":
        abort(503)


def limited_response(retry):
    return (
        render_template("login.html", form=LoginForm(formdata=None), failed=True),
        429,
        {"Retry-After": str(retry)},
    )


def limit_login():
    if request.endpoint == "auth.login" and request.method == "POST":
        retry = current_app.extensions["postcardscene.login_limiter"].retry_after(
            request.remote_addr
        )
        if retry:
            return limited_response(retry)


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("control.index"))
    form = LoginForm()
    if request.method != "POST":
        return render_template("login.html", form=form, failed=False)
    limiter = current_app.extensions["postcardscene.login_limiter"]
    entry, retry = limiter.begin(request.remote_addr)
    if retry:
        return limited_response(retry)
    identity = None
    completed = False
    try:
        if form.validate_on_submit():
            identity = authenticate(
                current_app.extensions["postcardscene.database"],
                form.username.data,
                form.password.data,
            )
        completed = True
    finally:
        limiter.finish(
            request.remote_addr,
            entry,
            success=bool(identity),
            failed=completed and not identity,
        )
    if identity:
        session.clear()
        login_user(WebUser(identity), remember=False)
        return redirect(url_for("control.index"))
    return render_template(
        "login.html", form=LoginForm(formdata=None), failed=True
    ), 401


@auth.post("/logout")
@login_required
def logout():
    revoke_sessions(current_app.extensions["postcardscene.database"])
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
