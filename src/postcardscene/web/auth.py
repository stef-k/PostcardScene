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
    manager = LoginManager(app)
    manager.login_view = "auth.login"
    manager.login_message = None
    manager.session_protection = "strong"
    manager.user_loader(load_user)
    app.before_request(require_secret)
    CSRFProtect(app)
    app.after_request(private_response)
    app.register_blueprint(auth)
    app.cli.add_command(auth_cli)


def require_secret():
    if current_app.secret_key is None and request.endpoint != "static":
        abort(503)


def private_response(response):
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@auth.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("control.index"))
    form = LoginForm()
    failed = False
    if form.validate_on_submit():
        identity = authenticate(
            current_app.extensions["postcardscene.database"],
            form.username.data,
            form.password.data,
        )
        if identity:
            session.clear()
            login_user(WebUser(identity), remember=False)
            return redirect(url_for("control.index"))
        failed = True
    return render_template(
        "login.html", form=form, failed=failed
    ), 401 if failed else 200


@auth.post("/logout")
@login_required
def logout():
    revoke_sessions(current_app.extensions["postcardscene.database"])
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
