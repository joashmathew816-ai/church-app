from flask import Flask, make_response, render_template, request, redirect, jsonify, send_from_directory
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Poll, PollResponse, Feedback, PushToken, PasswordResetRequest, RouteRelease
from optimizer import optimize_morning, optimize_return
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, quote_plus
import threading
import time
import os
import json
import requests as http_requests

app = Flask(__name__)

database_url = os.environ.get('DATABASE_URL', 'sqlite:///users.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret123')

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

LOCAL_TZ = ZoneInfo("America/Toronto")

# ntfy topic — set this as an environment variable on Render
# Users subscribe to this topic in the ntfy app
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "church-app-notifications")
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ----------------------
# HELPERS
# ----------------------
def now_utc_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_all_polls_json():
    try:
        now    = now_utc_naive()
        result = []
        for p in Poll.query.all():
            try:
                closes_at_str = None
                is_closed     = False
                if p.closes_at is not None:
                    closes_at_str = p.closes_at.strftime("%Y-%m-%dT%H:%M")
                    is_closed     = p.closes_at < now
                result.append({
                    "id":        p.id,
                    "title":     p.title or "",
                    "target":    p.target or "everyone",
                    "closes_at": closes_at_str,
                    "is_closed": is_closed
                })
            except Exception as e:
                app.logger.error(f"Error processing poll {p.id}: {e}")
        return result
    except Exception as e:
        app.logger.error(f"get_all_polls_json error: {e}")
        return []


def profile_complete(user):
    return bool(user.address and user.phone)


def poll_is_closed(poll):
    if poll.closes_at is None:
        return False
    return poll.closes_at < now_utc_naive()


def user_eligible_for_poll(user, poll):
    if poll.target == "everyone":
        return True
    if poll.target == "drivers" and user.is_driver:
        return True
    if poll.target == "passengers" and not user.is_driver:
        return True
    return False


def get_unread_count():
    try:
        if current_user.is_authenticated and current_user.role == "superuser":
            return PasswordResetRequest.query.filter_by(
                is_handled=False).count()
    except Exception:
        pass
    return 0


def get_feedback_unread_count():
    try:
        if current_user.is_authenticated and current_user.role == "superuser":
            return Feedback.query.filter_by(is_read=False).count()
    except Exception:
        pass
    return 0


# ----------------------
# NTFY NOTIFICATIONS
# ----------------------
def send_ntfy(title, message, priority="default", tags=None):
    """
    Send a notification via ntfy.sh.
    Priority options: min, low, default, high, urgent
    urgent = stays until acknowledged (perfect for polls and routes)
    """
    try:
        headers = {
            "Title":    title,
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = ",".join(tags)

        http_requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10
        )
        app.logger.info(f"ntfy sent: {title}")
    except Exception as e:
        app.logger.error(f"ntfy error: {e}")


def notify_new_poll(poll):
    """Notify everyone when a new poll is created."""
    try:
        send_ntfy(
            title    = "📊 New Poll Available",
            message  = f'"{poll.title}" is open for responses. '
                       f'Open Church App to vote!',
            priority = "high",
            tags     = ["ballot_box"]
        )
    except Exception as e:
        app.logger.error(f"notify_new_poll error: {e}")


def notify_route_released(direction, destination):
    """Notify everyone when routes are released."""
    try:
        dir_label = {"morning": "Morning", "return": "Return",
                     "both": "Morning & Return"}.get(direction, direction)
        send_ntfy(
            title    = "🚗 Routes Released",
            message  = f'{dir_label} routes to {destination} have been '
                       f'released. Open Church App to see your assignment!',
            priority = "urgent",
            tags     = ["car", "rotating_light"]
        )
    except Exception as e:
        app.logger.error(f"notify_route_released error: {e}")


def check_closing_notifications():
    """Background task — send poll closing warnings."""
    with app.app_context():
        try:
            now   = now_utc_naive()
            polls = Poll.query.filter(Poll.closes_at.isnot(None)).all()
            for poll in polls:
                if poll_is_closed(poll):
                    continue
                mins_left = (poll.closes_at - now).total_seconds() / 60
                if 28 <= mins_left <= 32:
                    send_ntfy(
                        title    = "⏰ Poll Closing Soon",
                        message  = f'"{poll.title}" closes in about '
                                   f'30 minutes! Open Church App to vote.',
                        priority = "high",
                        tags     = ["warning"]
                    )
                elif 3 <= mins_left <= 7:
                    send_ntfy(
                        title    = "🚨 Last Chance to Vote",
                        message  = f'"{poll.title}" closes in about '
                                   f'5 minutes!',
                        priority = "urgent",
                        tags     = ["rotating_light"]
                    )
        except Exception as e:
            app.logger.error(f"check_closing_notifications error: {e}")


def check_poll_reminders():
    """
    Every 6 hours, send a reminder for any open polls
    that the user has not responded to yet.
    """
    with app.app_context():
        try:
            open_polls = [p for p in Poll.query.all()
                          if not poll_is_closed(p)]
            if not open_polls:
                return

            poll_titles = [f'"{p.title}"' for p in open_polls]
            send_ntfy(
                title    = "📊 Open Polls Reminder",
                message  = f"You have {len(open_polls)} open poll(s) "
                           f"waiting for your response: "
                           f"{', '.join(poll_titles)}. "
                           f"Open Church App to vote!",
                priority = "default",
                tags     = ["reminder"]
            )
        except Exception as e:
            app.logger.error(f"check_poll_reminders error: {e}")


def start_scheduler():
    def run():
        reminder_counter = 0
        while True:
            time.sleep(300)  # 5 minutes
            check_closing_notifications()
            reminder_counter += 1
            # Every 72 cycles of 5 minutes = 6 hours
            if reminder_counter >= 72:
                check_poll_reminders()
                reminder_counter = 0
    thread = threading.Thread(target=run, daemon=True)
    thread.start()


with app.app_context():
    start_scheduler()


# ----------------------
# GOOGLE MAPS ROUTE LINK
# ----------------------
def build_maps_url(stops, destination):
    """
    Build a Google Maps URL with waypoints for drivers.
    stops = list of address strings in order
    destination = final destination address
    """
    if not stops:
        return None

    base = "https://www.google.com/maps/dir/"

    # Add each stop as a waypoint, end at destination
    parts = [quote_plus(s) for s in stops]
    parts.append(quote_plus(destination))

    return base + "/".join(parts)


# ----------------------
# STATIC FILES
# ----------------------
@app.route("/sw.js")
def service_worker():
    response = make_response(
        send_from_directory(app.static_folder, "sw.js"))
    response.headers["Content-Type"]           = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/manifest.json")
def manifest():
    response = make_response(
        send_from_directory(app.static_folder, "manifest.json"))
    response.headers["Content-Type"] = "application/manifest+json"
    return response


@app.route("/offline")
def offline():
    return render_template("offline.html")


# ----------------------
# HOME
# ----------------------
@app.route("/")
def home():
    return redirect("/login")


# ----------------------
# SIGNUP
# ----------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        first         = request.form.get("first_name", "").strip()
        last          = request.form.get("last_name", "").strip()
        password      = request.form.get("password", "")
        phone         = request.form.get("phone", "").strip()
        address       = request.form.get("address", "").strip()
        is_driver_val = request.form.get("is_driver", "no")
        is_driver     = is_driver_val == "yes"

        capacity = 0
        if is_driver:
            try:
                capacity = int(request.form.get("capacity", 0))
                if capacity < 1 or capacity > 8:
                    return render_template("signup.html",
                                           error_capacity="Capacity must be between 1 and 8")
            except ValueError:
                return render_template("signup.html",
                                       error_capacity="Invalid capacity")

        if not all([first, last, password, phone, address]):
            return render_template("signup.html", error="Please fill in all fields")

        if User.query.filter_by(first_name=first).first():
            return render_template("signup.html", error="That name is already taken")

        db.session.add(User(
            first_name=first, last_name=last,
            password=generate_password_hash(password),
            phone=phone, address=address,
            role="user", is_driver=is_driver, capacity=capacity
        ))
        db.session.commit()
        return redirect("/login")

    return render_template("signup.html")


# ----------------------
# LOGIN
# ----------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        first    = request.form.get("first_name")
        password = request.form.get("password")
        user = User.query.filter_by(first_name=first).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect("/dashboard")
        return render_template("login.html", error="Invalid login")
    return render_template("login.html")


# ----------------------
# FORGOT PASSWORD
# ----------------------
@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last  = request.form.get("last_name", "").strip()

        if not first or not last:
            return render_template("forgot_password.html",
                                   error="Please fill in both fields.")
        try:
            user = User.query.filter_by(
                first_name=first, last_name=last).first()
            if not user:
                return render_template("forgot_password.html",
                                       error="No account found with that name.")

            old_requests = PasswordResetRequest.query.filter_by(
                user_id=user.id, is_handled=False).all()
            for old in old_requests:
                db.session.delete(old)
            db.session.flush()

            db.session.add(PasswordResetRequest(user_id=user.id))
            db.session.commit()

            try:
                send_ntfy(
                    title    = "🔑 Password Reset Request",
                    message  = f"{user.first_name} {user.last_name} has "
                               f"requested a password reset. Open Church App "
                               f"→ Manage Users to handle this.",
                    priority = "high",
                    tags     = ["key"]
                )
            except Exception:
                pass

            return render_template("forgot_password.html",
                                   success="Your request has been sent. "
                                   "The superuser will set a temporary "
                                   "password for you soon.")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Forgot password error: {e}")
            return render_template("forgot_password.html",
                                   error="Something went wrong. Please try again.")

    return render_template("forgot_password.html")


# ----------------------
# DASHBOARD
# ----------------------
@app.route("/dashboard")
@login_required
def dashboard():
    # Get the most recent active route release
    active_release = RouteRelease.query.filter_by(
        is_active=True).order_by(RouteRelease.created_at.desc()).first()

    driver_info    = None
    passenger_info = None

    if active_release:
        try:
            route_data = json.loads(active_release.route_data)
            user_name  = (current_user.first_name + " " +
                          current_user.last_name)

            # Look through morning and return routes
            for direction in ["morning", "return"]:
                result = route_data.get(direction)
                if not result or "routes" not in result:
                    continue

                for route in result["routes"]:
                    # Check if this user is the driver
                    if route["driver"] == user_name:
                        stops        = [s["address"] for s in route["stops"]]
                        passengers_list = []
                        for stop in route["stops"]:
                            for pname in stop["passengers"]:
                                passengers_list.append({
                                    "name":    pname,
                                    "address": stop["address"]
                                })

                        # Build Google Maps URL
                        all_stops = [s["address"] for s in route["stops"]]
                        maps_url  = build_maps_url(
                            all_stops, active_release.destination)

                        driver_info = {
                            "direction":   direction,
                            "destination": active_release.destination,
                            "passengers":  passengers_list,
                            "time_min":    route["time_min"],
                            "distance_km": route["distance_km"],
                            "maps_url":    maps_url
                        }

                    # Check if this user is a passenger in any stop
                    for stop in route["stops"]:
                        for pname in stop["passengers"]:
                            if pname == user_name:
                                passenger_info = {
                                    "direction":   direction,
                                    "destination": active_release.destination,
                                    "driver":      route["driver"],
                                    "address":     stop["address"],
                                }

        except Exception as e:
            app.logger.error(f"Dashboard route parse error: {e}")

    return render_template("dashboard.html",
                           incomplete=not profile_complete(current_user),
                           unread_count=get_unread_count(),
                           feedback_unread=get_feedback_unread_count(),
                           driver_info=driver_info,
                           passenger_info=passenger_info,
                           active_release=active_release)


# ----------------------
# PROFILE
# ----------------------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        address = request.form.get("address", "").strip()
        phone   = request.form.get("phone", "").strip()

        if not address:
            return render_template("profile.html",
                                   error_address="Please select a valid address",
                                   unread_count=get_unread_count())

        old_is_driver      = current_user.is_driver
        is_driver          = request.form.get("is_driver") == "on"
        current_user.is_driver = is_driver

        if is_driver:
            try:
                cap = int(request.form.get("capacity", 0))
                if cap < 1 or cap > 8:
                    return render_template("profile.html",
                                           error_capacity="Capacity must be between 1 and 8",
                                           unread_count=get_unread_count())
                current_user.capacity = cap
            except Exception:
                return render_template("profile.html",
                                       error_capacity="Invalid capacity",
                                       unread_count=get_unread_count())
        else:
            current_user.capacity = 0

        current_user.address = address
        current_user.phone   = phone

        if old_is_driver != is_driver:
            for poll in Poll.query.all():
                eligible = (poll.target in ("everyone", "drivers") if is_driver
                            else poll.target in ("everyone", "passengers"))
                if not eligible:
                    stale = PollResponse.query.filter_by(
                        user_id=current_user.id, poll_id=poll.id).first()
                    if stale:
                        db.session.delete(stale)

        db.session.commit()
        return render_template("profile.html",
                               success="✅ Changes Saved!",
                               unread_count=get_unread_count())

    return render_template("profile.html", unread_count=get_unread_count())


# ----------------------
# CHANGE PASSWORD
# ----------------------
@app.route("/change_password", methods=["POST"])
@login_required
def change_password():
    current_pw = request.form.get("current_password", "")
    new_pw     = request.form.get("new_password", "").strip()
    confirm_pw = request.form.get("confirm_password", "").strip()

    if not check_password_hash(current_user.password, current_pw):
        return render_template("profile.html",
                               password_error="Current password is incorrect.",
                               unread_count=get_unread_count())
    if new_pw != confirm_pw:
        return render_template("profile.html",
                               password_error="New passwords do not match.",
                               unread_count=get_unread_count())
    if len(new_pw) < 6:
        return render_template("profile.html",
                               password_error="Password must be at least 6 characters.",
                               unread_count=get_unread_count())

    current_user.password = generate_password_hash(new_pw)
    db.session.commit()
    return render_template("profile.html",
                           password_success="✅ Password updated!",
                           unread_count=get_unread_count())


# ----------------------
# SAVE PUSH TOKEN
# ----------------------
@app.route("/save_push_token", methods=["POST"])
@login_required
def save_push_token():
    try:
        data  = request.get_json()
        token = data.get("token")
        if token:
            existing = PushToken.query.filter_by(
                user_id=current_user.id).first()
            if existing:
                existing.token   = token
                existing.updated = now_utc_naive()
            else:
                db.session.add(PushToken(
                    user_id=current_user.id, token=token))
            db.session.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


# ----------------------
# POLLS
# ----------------------
@app.route("/polls", methods=["GET", "POST"])
@login_required
def polls():
    if not profile_complete(current_user):
        return render_template("polls.html", polls=[],
                               profile_warning=True,
                               unread_count=get_unread_count())

    if request.method == "POST":
        poll_id = request.form.get("poll_id")
        answer  = request.form.get("answer")
        if poll_id and answer:
            try:
                poll = db.session.get(Poll, int(poll_id))
                if poll and not poll_is_closed(poll):
                    can_vote = (poll.target in ("everyone", "drivers")
                                if current_user.is_driver
                                else poll.target in ("everyone", "passengers"))
                    if can_vote:
                        old = PollResponse.query.filter_by(
                            user_id=current_user.id,
                            poll_id=poll_id).first()
                        if old:
                            db.session.delete(old)
                        db.session.add(PollResponse(
                            user_id=current_user.id,
                            poll_id=poll_id, answer=answer))
                        db.session.commit()
            except Exception as e:
                app.logger.error(f"Poll vote error: {e}")
                db.session.rollback()

    try:
        poll_data = []
        for poll in Poll.query.all():
            if current_user.role not in ("admin", "superuser"):
                if not (poll.target == "everyone"
                        or (poll.target == "drivers"
                            and current_user.is_driver)
                        or (poll.target == "passengers"
                            and not current_user.is_driver)):
                    continue

            user_eligible = (poll.target in ("everyone", "drivers")
                             if current_user.is_driver
                             else poll.target in ("everyone", "passengers"))
            closed        = poll_is_closed(poll)
            options_list  = ([o.strip() for o in poll.options.split("|||")
                               if o.strip()] if poll.options else [])
            responses     = PollResponse.query.filter_by(poll_id=poll.id).all()

            counts = {opt: 0 for opt in options_list}
            users  = {opt: [] for opt in options_list}
            for r in responses:
                if r.answer in counts:
                    counts[r.answer] += 1
                    u = db.session.get(User, r.user_id)
                    if u:
                        users[r.answer].append(u.first_name)

            existing    = PollResponse.query.filter_by(
                user_id=current_user.id, poll_id=poll.id).first()
            user_answer = (existing.answer
                           if existing and existing.answer in options_list
                           else None)

            closes_at_str = None
            if poll.closes_at:
                try:
                    local_dt = poll.closes_at.replace(
                        tzinfo=timezone.utc).astimezone(LOCAL_TZ)
                    closes_at_str = local_dt.strftime("%b %d, %Y at %I:%M %p")
                except Exception:
                    pass

            poll_data.append({
                "id":            poll.id,
                "title":         poll.title,
                "target":        poll.target,
                "options":       options_list,
                "counts":        counts,
                "users":         users,
                "total":         len(responses),
                "user_eligible": user_eligible,
                "user_answer":   user_answer,
                "closed":        closed,
                "closes_at":     closes_at_str,
            })

        return render_template("polls.html", polls=poll_data,
                               profile_warning=False,
                               unread_count=get_unread_count())
    except Exception as e:
        app.logger.error(f"Polls page error: {e}")
        return render_template("polls.html", polls=[],
                               profile_warning=False,
                               unread_count=get_unread_count())


# ----------------------
# SUPERUSER: MANAGE USERS
# ----------------------
@app.route("/superuser/users", methods=["GET", "POST"])
@login_required
def superuser_users():
    if current_user.role != "superuser":
        return "Access Denied", 403

    if request.method == "POST":
        user_id = request.form.get("user_id")
        if not user_id:
            return redirect("/superuser/users")

        user_id = int(user_id)
        user    = db.session.get(User, user_id)

        if not user:
            return redirect(
                f"/superuser/users?error_id=0&error_msg={quote('User not found')}")

        address = request.form.get("address", "").strip()
        if not address:
            return redirect(
                f"/superuser/users?error_id={user_id}"
                f"&error_msg={quote('Please select a valid address')}")

        new_role = ("superuser" if user.id == current_user.id
                    else request.form.get("role", "user"))
        if new_role not in ("user", "admin", "superuser"):
            new_role = user.role

        is_driver = request.form.get("is_driver") == "on"
        capacity  = 0

        if is_driver:
            try:
                cap = int(request.form.get("capacity", 0))
                if cap < 1 or cap > 8:
                    return redirect(
                        f"/superuser/users?error_id={user_id}"
                        f"&error_msg={quote('Capacity must be between 1 and 8')}")
                capacity = cap
            except Exception:
                return redirect(
                    f"/superuser/users?error_id={user_id}"
                    f"&error_msg={quote('Invalid capacity value')}")

        old_is_driver = user.is_driver
        if old_is_driver != is_driver:
            for poll in Poll.query.all():
                eligible = (poll.target in ("everyone", "drivers") if is_driver
                            else poll.target in ("everyone", "passengers"))
                if not eligible:
                    stale = PollResponse.query.filter_by(
                        user_id=user.id, poll_id=poll.id).first()
                    if stale:
                        db.session.delete(stale)

        new_password = request.form.get("new_password", "").strip()
        if new_password:
            user.password = generate_password_hash(new_password)
            for req in PasswordResetRequest.query.filter_by(
                    user_id=user.id, is_handled=False).all():
                req.is_handled = True
            try:
                send_ntfy(
                    title    = "🔑 Password Reset",
                    message  = f"Hi {user.first_name}, your Church App "
                               f"password has been reset. Temporary password: "
                               f"{new_password} — please log in and change it "
                               f"from your Profile page.",
                    priority = "high",
                    tags     = ["key"]
                )
            except Exception as e:
                app.logger.error(f"ntfy reset notify error: {e}")

        user.first_name = (request.form.get("first_name", "").strip()
                           or user.first_name)
        user.last_name  = (request.form.get("last_name", "").strip()
                           or user.last_name)
        user.address    = address
        user.phone      = request.form.get("phone", "").strip()
        user.role       = new_role
        user.is_driver  = is_driver
        user.capacity   = capacity
        db.session.commit()

        return redirect(f"/superuser/users?saved={user_id}")

    users          = User.query.order_by(User.last_name).all()
    reset_requests = PasswordResetRequest.query.filter_by(
        is_handled=False).all()
    reset_user_ids = {r.user_id for r in reset_requests}

    return render_template("superuser_users.html",
                           users=users,
                           reset_user_ids=reset_user_ids,
                           unread_count=get_unread_count())


# ----------------------
# SUPERUSER: DELETE USER
# ----------------------
@app.route("/superuser/delete_user/<int:user_id>", methods=["POST"])
@login_required
def delete_user(user_id):
    if current_user.role != "superuser":
        return "Access Denied", 403

    if user_id == current_user.id:
        return redirect(
            f"/superuser/users?error_id={user_id}"
            f"&error_msg={quote('You cannot delete your own account')}")

    user = db.session.get(User, user_id)
    if user:
        PollResponse.query.filter_by(user_id=user_id).delete()
        Feedback.query.filter_by(user_id=user_id).delete()
        PushToken.query.filter_by(user_id=user_id).delete()
        PasswordResetRequest.query.filter_by(user_id=user_id).delete()
        db.session.delete(user)
        db.session.commit()

    return redirect("/superuser/users?deleted=1")


# ----------------------
# ADMIN: CREATE POLL
# ----------------------
@app.route("/admin/create_poll", methods=["GET", "POST"])
@login_required
def create_poll():
    if current_user.role not in ("admin", "superuser"):
        return "Access Denied"

    if request.method == "POST":
        title  = request.form.get("title", "").strip()
        target = request.form.get("target")

        options = []
        for i in range(10):
            opt = request.form.get(f"option_{i}", "").strip()
            if opt:
                options.append(opt)

        if not options:
            return render_template("create_poll.html",
                                   error="Add at least 1 option",
                                   all_polls=get_all_polls_json(),
                                   unread_count=get_unread_count())

        closes_at     = None
        closes_at_str = request.form.get("closes_at", "").strip()
        if closes_at_str:
            try:
                local_dt = datetime.strptime(
                    closes_at_str, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_TZ)
                if local_dt < datetime.now(LOCAL_TZ):
                    return render_template("create_poll.html",
                                           error="Closing time cannot be in the past",
                                           all_polls=get_all_polls_json(),
                                           unread_count=get_unread_count())
                closes_at = local_dt.astimezone(
                    timezone.utc).replace(tzinfo=None)
            except ValueError:
                return render_template("create_poll.html",
                                       error="Invalid date format",
                                       all_polls=get_all_polls_json(),
                                       unread_count=get_unread_count())

        try:
            poll = Poll(title=title, options="|||".join(options),
                        target=target, closes_at=closes_at)
            db.session.add(poll)
            db.session.commit()
            notify_new_poll(poll)
        except Exception as e:
            db.session.rollback()
            return render_template("create_poll.html",
                                   error=f"Could not save poll: {str(e)}",
                                   all_polls=get_all_polls_json(),
                                   unread_count=get_unread_count())

        return render_template("create_poll.html",
                               success="Poll created!",
                               all_polls=get_all_polls_json(),
                               unread_count=get_unread_count())

    return render_template("create_poll.html",
                           all_polls=get_all_polls_json(),
                           unread_count=get_unread_count())


# ----------------------
# ADMIN: GET POLL
# ----------------------
@app.route("/admin/get_poll/<int:poll_id>")
@login_required
def get_poll(poll_id):
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    poll = db.session.get(Poll, poll_id)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404

    options_list = ([o.strip() for o in poll.options.split("|||") if o.strip()]
                    if poll.options else [])
    counts = {opt: 0 for opt in options_list}
    voters = {opt: [] for opt in options_list}

    for r in PollResponse.query.filter_by(poll_id=poll_id).all():
        if r.answer in counts:
            counts[r.answer] += 1
            u = db.session.get(User, r.user_id)
            if u:
                voters[r.answer].append(u.first_name)

    closes_str = ""
    if poll.closes_at:
        try:
            closes_str = poll.closes_at.replace(
                tzinfo=timezone.utc).astimezone(
                LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")
        except Exception:
            pass

    return jsonify({
        "id": poll.id, "title": poll.title, "target": poll.target,
        "options": options_list, "counts": counts, "voters": voters,
        "closes_at": closes_str
    })


# ----------------------
# ADMIN: EDIT POLL
# ----------------------
@app.route("/admin/edit_poll", methods=["POST"])
@login_required
def edit_poll():
    if current_user.role not in ("admin", "superuser"):
        return "Access Denied"

    poll_id = request.form.get("poll_id")
    poll    = db.session.get(Poll, int(poll_id))
    if not poll:
        return render_template("create_poll.html",
                               error="Poll not found",
                               all_polls=get_all_polls_json(),
                               unread_count=get_unread_count())

    new_title   = request.form.get("title", "").strip()
    new_target  = request.form.get("target")
    new_options = []
    for i in range(20):
        opt = request.form.get(f"edit_option_{i}", "").strip()
        if opt:
            new_options.append(opt)

    if not new_options:
        return render_template("create_poll.html",
                               error="Poll must have at least 1 option",
                               all_polls=get_all_polls_json(),
                               unread_count=get_unread_count())

    new_closes_at = None
    closes_at_str = request.form.get("closes_at", "").strip()
    if closes_at_str:
        try:
            local_dt = datetime.strptime(
                closes_at_str, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_TZ)
            if local_dt < datetime.now(LOCAL_TZ):
                return render_template("create_poll.html",
                                       error="Closing time cannot be in the past",
                                       all_polls=get_all_polls_json(),
                                       unread_count=get_unread_count())
            new_closes_at = local_dt.astimezone(
                timezone.utc).replace(tzinfo=None)
        except ValueError:
            return render_template("create_poll.html",
                                   error="Invalid date format",
                                   all_polls=get_all_polls_json(),
                                   unread_count=get_unread_count())

    old_options     = ([o.strip() for o in poll.options.split("|||") if o.strip()]
                       if poll.options else [])
    removed_options = [o for o in old_options if o not in new_options]

    for removed_opt in removed_options:
        for resp in PollResponse.query.filter_by(
                poll_id=poll.id, answer=removed_opt).all():
            db.session.delete(resp)

    poll.title     = new_title
    poll.options   = "|||".join(new_options)
    poll.target    = new_target
    poll.closes_at = new_closes_at
    db.session.commit()

    return render_template("create_poll.html",
                           edit_success="Poll updated successfully!",
                           all_polls=get_all_polls_json(),
                           unread_count=get_unread_count())


# ----------------------
# ADMIN: DELETE POLLS
# ----------------------
@app.route("/admin/delete_polls", methods=["POST"])
@login_required
def delete_polls():
    if current_user.role not in ("admin", "superuser"):
        return "Access Denied"

    ids_to_delete = request.form.getlist("delete_ids")
    for poll_id in ids_to_delete:
        poll = db.session.get(Poll, int(poll_id))
        if poll:
            PollResponse.query.filter_by(poll_id=poll.id).delete()
            db.session.delete(poll)
    db.session.commit()

    count = len(ids_to_delete)
    msg   = (f"Deleted {count} poll{'s' if count != 1 else ''} successfully!"
             if count > 0 else "No polls were selected.")

    return render_template("create_poll.html",
                           delete_success=msg,
                           all_polls=get_all_polls_json(),
                           unread_count=get_unread_count())


# ----------------------
# ADMIN: POLLS FOR ROUTES
# ----------------------
@app.route("/admin/polls_for_routes")
@login_required
def polls_for_routes():
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403
    try:
        result = []
        for poll in Poll.query.all():
            options_list = ([o.strip() for o in poll.options.split("|||")
                              if o.strip()] if poll.options else [])
            counts = {opt: 0 for opt in options_list}
            voters = {opt: [] for opt in options_list}
            for r in PollResponse.query.filter_by(poll_id=poll.id).all():
                if r.answer in counts:
                    counts[r.answer] += 1
                    u = db.session.get(User, r.user_id)
                    if u:
                        voters[r.answer].append(u.first_name)
            result.append({
                "id": poll.id, "title": poll.title, "target": poll.target,
                "options": options_list, "counts": counts, "voters": voters,
                "is_closed": poll_is_closed(poll)
            })
        return jsonify(result)
    except Exception as e:
        app.logger.error(f"polls_for_routes error: {e}")
        return jsonify([])


# ----------------------
# ADMIN: GENERATE ROUTES
# ----------------------
@app.route("/admin/generate_routes", methods=["POST"])
@login_required
def generate_routes():
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    data            = request.get_json()
    driver_names    = data.get("driver_names", [])
    passenger_names = data.get("passenger_names", [])
    church_address  = data.get("church_address", "").strip()
    mode            = data.get("mode", "both")

    if not church_address:
        return jsonify({"error": "Please enter the destination address"})
    if not driver_names:
        return jsonify({"error": "Please select at least one driver option"})
    if not passenger_names:
        return jsonify({"error": "Please select at least one passenger option"})

    drivers = []
    for name in driver_names:
        u = User.query.filter_by(first_name=name).first()
        if u and u.address and u.capacity:
            drivers.append({
                "name": u.first_name + " " + u.last_name,
                "address": u.address, "capacity": u.capacity,
                "morning": True, "is_returning": True
            })

    passengers = []
    for name in passenger_names:
        u = User.query.filter_by(first_name=name).first()
        if u and u.address:
            passengers.append({
                "name": u.first_name + " " + u.last_name,
                "address": u.address, "morning": True, "is_returning": True
            })

    if not drivers:
        return jsonify({"error": "None of the selected drivers have a valid profile"})
    if not passengers:
        return jsonify({"error": "None of the selected passengers have a valid profile"})

    response = {}
    if mode in ("morning", "both"):
        response["morning"] = optimize_morning(drivers, passengers, church_address)
    if mode in ("return", "both"):
        response["return"]  = optimize_return(drivers, passengers, church_address)

    return jsonify(response)


# ----------------------
# ADMIN: RELEASE ROUTES
# ----------------------
@app.route("/admin/release_routes", methods=["POST"])
@login_required
def release_routes():
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    data        = request.get_json()
    route_data  = data.get("route_data")
    destination = data.get("destination", "").strip()
    direction   = data.get("direction", "both")

    if not route_data or not destination:
        return jsonify({"error": "Missing route data or destination"})

    try:
        # Deactivate any previous releases
        RouteRelease.query.update({"is_active": False})

        # Save the new release
        release = RouteRelease(
            created_by  = current_user.id,
            direction   = direction,
            destination = destination,
            route_data  = json.dumps(route_data),
            is_active   = True
        )
        db.session.add(release)
        db.session.commit()

        # Notify everyone via ntfy
        notify_route_released(direction, destination)

        return jsonify({"status": "ok", "message": "Routes released!"})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Release routes error: {e}")
        return jsonify({"error": str(e)})


# ----------------------
# ADMIN: CLEAR RELEASED ROUTES
# ----------------------
@app.route("/admin/clear_routes", methods=["POST"])
@login_required
def clear_routes():
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403
    try:
        RouteRelease.query.update({"is_active": False})
        db.session.commit()
        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)})


# ----------------------
# FEEDBACK
# ----------------------
@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            return render_template("feedback.html",
                                   error="Please write something.",
                                   unread_count=get_unread_count())
        db.session.add(Feedback(user_id=current_user.id, message=message))
        db.session.commit()
        return render_template("feedback.html",
                               success="✅ Thanks for your feedback!",
                               unread_count=get_unread_count())
    return render_template("feedback.html", unread_count=get_unread_count())


# ----------------------
# SUPERUSER: VIEW FEEDBACK
# ----------------------
@app.route("/admin/feedback")
@login_required
def admin_feedback():
    if current_user.role != "superuser":
        return "Access Denied", 403

    for fb in Feedback.query.filter_by(is_read=False).all():
        fb.is_read = True
    db.session.commit()

    feedback_data = []
    for fb in Feedback.query.order_by(Feedback.created_at.desc()).all():
        u = db.session.get(User, fb.user_id)
        try:
            local_dt    = fb.created_at.replace(
                tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            created_str = local_dt.strftime("%b %d, %Y at %I:%M %p")
        except Exception:
            created_str = "Unknown time"
        feedback_data.append({
            "id":         fb.id,
            "name":       (u.first_name + " " + u.last_name) if u else "Unknown",
            "message":    fb.message,
            "created_at": created_str
        })

    return render_template("admin_feedback.html",
                           feedback_list=feedback_data,
                           unread_count=get_unread_count(),
                           feedback_unread=get_feedback_unread_count())


# ----------------------
# SUPERUSER: DELETE FEEDBACK
# ----------------------
@app.route("/admin/delete_feedback/<int:feedback_id>", methods=["POST"])
@login_required
def delete_feedback(feedback_id):
    if current_user.role != "superuser":
        return "Access Denied", 403
    fb = db.session.get(Feedback, feedback_id)
    if fb:
        db.session.delete(fb)
        db.session.commit()
    return redirect("/admin/feedback")


# ----------------------
# ADMIN ROUTES PAGE
# ----------------------
@app.route("/admin/routes")
@login_required
def routes():
    if current_user.role not in ("admin", "superuser"):
        return "Access Denied"
    return render_template("routes.html", unread_count=get_unread_count())


# ----------------------
# LOGOUT
# ----------------------
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ----------------------
# SETUP + MIGRATE
# ----------------------
@app.route("/setup")
def setup():
    try:
        db.create_all()

        if not User.query.filter_by(first_name="Admin").first():
            db.session.add(User(
                first_name="Admin", last_name="User",
                password=generate_password_hash("admin123"),
                role="admin", is_driver=False, capacity=0,
                phone="0000000000", address="114 Lane St, Guelph, ON"
            ))

        if not User.query.filter_by(first_name="Super").first():
            db.session.add(User(
                first_name="Super", last_name="User",
                password=generate_password_hash("super123"),
                role="superuser", is_driver=False, capacity=0,
                phone="0000000000", address="114 Lane St, Guelph, ON"
            ))

        db.session.commit()
        return ("✅ Setup complete!<br>"
                "Admin: Admin / admin123<br>"
                "Superuser: Super / super123")
    except Exception as e:
        return f"❌ Error: {str(e)}"


@app.route("/migrate")
def migrate():
    try:
        db.create_all()
        results = ["✅ All tables created/verified"]

        with db.engine.connect() as conn:
            for table, column, col_type in [
                ("poll", "closes_at", "TIMESTAMP NULL"),
            ]:
                try:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN {column} {col_type}"
                    ))
                    conn.commit()
                    results.append(f"✅ Added {column} to {table}")
                except Exception as e:
                    err = str(e).lower()
                    if "already exists" in err or "duplicate" in err:
                        results.append(
                            f"✅ {table}.{column} already exists")
                    else:
                        results.append(
                            f"❌ {table}.{column}: {str(e)}")

        return "<br>".join(results)
    except Exception as e:
        return f"❌ Error: {str(e)}"


@app.cli.command("create-db")
def create_db():
    db.create_all()
    print("Database created!")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")