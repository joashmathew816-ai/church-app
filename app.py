from flask import (Flask, make_response, render_template, request,
                   redirect, jsonify, send_from_directory)
from flask_login import (LoginManager, login_user, login_required,
                         logout_user, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from models import (db, User, Poll, PollResponse, Feedback, PushToken,
                    PasswordResetRequest, RouteRelease, RouteAcknowledgement)
from optimizer import optimize_morning, optimize_return
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, quote_plus
import threading
import time
import os
import json
import re
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

# Base ntfy server
NTFY_SERVER = "https://ntfy.sh"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ----------------------
# HELPERS
# ----------------------
def now_utc_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_user_ntfy_topic(user):
    """
    Generate a unique private ntfy topic for this user.
    Format: church-[base_topic]-u[user_id]
    Example: church-guelph-2024-u7
    """
    base = os.environ.get("NTFY_TOPIC", "church-app")
    return f"{base}-u{user.id}"


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


def validate_phone(phone):
    """Phone must be exactly 10 digits, no letters."""
    digits_only = re.sub(r'\D', '', phone)
    return len(digits_only) == 10


def validate_password(password):
    """Password must be at least 6 characters."""
    return len(password) >= 6


# ----------------------
# NTFY — PERSONALISED
# ----------------------
def send_ntfy_to_user(user, title, message, priority="default", tags=None):
    """
    Send a notification to ONE specific user via their personal ntfy topic.
    Only sends if the user has a ntfy_topic set (i.e. they have set it up).
    """
    if not user.ntfy_topic:
        return False

    try:
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip()
        if not safe_title:
            safe_title = "Church App"

        headers = {"Title": safe_title, "Priority": priority}
        if tags:
            headers["Tags"] = ",".join(tags)

        full_message = (f"{title}\n{message}"
                        if title != safe_title else message)

        url      = f"{NTFY_SERVER}/{user.ntfy_topic}"
        response = http_requests.post(
            url,
            data=full_message.encode("utf-8"),
            headers=headers,
            timeout=15
        )
        return response.status_code == 200

    except Exception as e:
        app.logger.error(
            f"ntfy error for user {user.id} "
            f"topic={user.ntfy_topic}: {e}"
        )
        return False


def notify_new_poll(poll):
    """Notify only eligible users when a new poll is created."""
    try:
        users = User.query.all()
        for user in users:
            if not user_eligible_for_poll(user, poll):
                continue
            if not user.ntfy_topic:
                continue

            target_label = {
                "everyone":   "everyone",
                "drivers":    "drivers",
                "passengers": "passengers"
            }.get(poll.target, "everyone")

            send_ntfy_to_user(
                user,
                title    = "New Poll Available",
                message  = (
                    f'"{poll.title}" is now open for {target_label}. '
                    f'Open Church App to vote!'
                ),
                priority = "high",
                tags     = ["ballot_box"]
            )
    except Exception as e:
        app.logger.error(f"notify_new_poll error: {e}")


def get_user_route_message(user, active_release):
    """
    Build a personalised route message for a specific user.
    Returns None if the user has no assignment.
    """
    if not active_release:
        return None

    try:
        route_data = json.loads(active_release.route_data)
        user_name  = user.first_name + " " + user.last_name

        for direction in ["morning", "return"]:
            result = route_data.get(direction)
            if not result or "routes" not in result:
                continue

            dir_label = "Morning" if direction == "morning" else "Return"

            for route in result["routes"]:
                if route["driver"] == user_name:
                    if route["stops"]:
                        lines = []
                        for stop in route["stops"]:
                            names = ", ".join(stop["passengers"])
                            lines.append(
                                f"- {names} from {stop['address']}")
                        stops_text = "\n".join(lines)
                        return (
                            f"{dir_label} to "
                            f"{active_release.destination}\n"
                            f"You are DRIVING.\nPick up:\n{stops_text}\n"
                            f"({route['time_min']} min, "
                            f"{route['distance_km']} km)\n"
                            f"Open Church App for Google Maps link."
                        )
                    else:
                        return (
                            f"{dir_label} to "
                            f"{active_release.destination}\n"
                            f"You are DRIVING directly. No passengers."
                        )

                for stop in route["stops"]:
                    if user_name in stop["passengers"]:
                        return (
                            f"{dir_label} to "
                            f"{active_release.destination}\n"
                            f"Your driver: {route['driver']}\n"
                            f"Pickup: {stop['address']}\n"
                            f"Open Church App to acknowledge."
                        )

    except Exception as e:
        app.logger.error(
            f"get_user_route_message error for {user.first_name}: {e}")

    return None


def notify_route_released(release):
    """
    Send personalised notifications to only those users
    who have an assignment in this release.
    Only users with a ntfy_topic set will receive notifications.
    """
    try:
        users = User.query.all()
        notified = 0
        for user in users:
            if not user.ntfy_topic:
                continue
            msg = get_user_route_message(user, release)
            if msg:
                send_ntfy_to_user(
                    user,
                    title    = "Your Route Assignment",
                    message  = msg + "\nTap Acknowledge in Church App.",
                    priority = "urgent",
                    tags     = ["car"]
                )
                notified += 1
        app.logger.info(
            f"Route release notifications sent to {notified} users")
    except Exception as e:
        app.logger.error(f"notify_route_released error: {e}")


def send_route_reminders():
    """
    Every 6 hours, re-notify assigned users who have NOT acknowledged.
    Only runs if there are visible releases.
    """
    with app.app_context():
        try:
            visible_releases = RouteRelease.query.filter_by(
                is_visible=True).all()
            if not visible_releases:
                return

            users = User.query.all()
            for user in users:
                if not user.ntfy_topic:
                    continue

                for release in visible_releases:
                    acked = RouteAcknowledgement.query.filter_by(
                        user_id    = user.id,
                        release_id = release.id
                    ).first()
                    if acked:
                        continue

                    msg = get_user_route_message(user, release)
                    if msg:
                        send_ntfy_to_user(
                            user,
                            title    = "Route Reminder",
                            message  = (
                                msg +
                                "\nPlease open Church App "
                                "and tap Acknowledge."
                            ),
                            priority = "urgent",
                            tags     = ["bell", "car"]
                        )

        except Exception as e:
            app.logger.error(f"send_route_reminders error: {e}")


def check_closing_notifications():
    """Send 30-min and 5-min poll closing warnings to eligible users only."""
    with app.app_context():
        try:
            now   = now_utc_naive()
            polls = Poll.query.filter(Poll.closes_at.isnot(None)).all()
            for poll in polls:
                if poll_is_closed(poll):
                    continue
                mins_left = (poll.closes_at - now).total_seconds() / 60

                if not (28 <= mins_left <= 32 or 3 <= mins_left <= 7):
                    continue

                users = User.query.all()
                for user in users:
                    if not user.ntfy_topic:
                        continue
                    if not user_eligible_for_poll(user, poll):
                        continue

                    # Only notify if they have not voted yet
                    voted = PollResponse.query.filter_by(
                        user_id = user.id,
                        poll_id = poll.id
                    ).first()
                    if voted:
                        continue

                    if 28 <= mins_left <= 32:
                        send_ntfy_to_user(
                            user,
                            title    = "Poll Closing Soon",
                            message  = (
                                f'"{poll.title}" closes in ~30 min. '
                                f'Open Church App to vote!'
                            ),
                            priority = "high",
                            tags     = ["warning"]
                        )
                    elif 3 <= mins_left <= 7:
                        send_ntfy_to_user(
                            user,
                            title    = "Last Chance to Vote",
                            message  = (
                                f'"{poll.title}" closes in ~5 min!'
                            ),
                            priority = "urgent",
                            tags     = ["rotating_light"]
                        )

        except Exception as e:
            app.logger.error(f"check_closing_notifications error: {e}")


def check_poll_reminders():
    """
    Every 6 hours remind each eligible user about open polls
    they have NOT voted in yet.
    One notification per user, not one per poll.
    """
    with app.app_context():
        try:
            all_polls  = Poll.query.all()
            open_polls = [p for p in all_polls if not poll_is_closed(p)]
            if not open_polls:
                return

            users = User.query.all()
            for user in users:
                if not user.ntfy_topic:
                    continue

                # Find polls this user is eligible for AND has not voted in
                pending = []
                for poll in open_polls:
                    if not user_eligible_for_poll(user, poll):
                        continue
                    voted = PollResponse.query.filter_by(
                        user_id = user.id,
                        poll_id = poll.id
                    ).first()
                    if not voted:
                        pending.append(poll)

                if not pending:
                    continue

                titles = [f'"{p.title}"' for p in pending]
                send_ntfy_to_user(
                    user,
                    title    = "Open Polls Reminder",
                    message  = (
                        f"You have {len(pending)} poll(s) waiting: "
                        f"{', '.join(titles)}. "
                        f"Open Church App to vote!"
                    ),
                    priority = "default",
                    tags     = ["reminder"]
                )

        except Exception as e:
            app.logger.error(f"check_poll_reminders error: {e}")


def start_scheduler():
    def run():
        counter = 0
        while True:
            time.sleep(300)
            check_closing_notifications()
            counter += 1
            if counter >= 72:   # 6 hours
                check_poll_reminders()
                send_route_reminders()
                counter = 0
    thread = threading.Thread(target=run, daemon=True)
    thread.start()


with app.app_context():
    start_scheduler()


# ----------------------
# STATIC FILES
# ----------------------
@app.route("/sw.js")
def service_worker():
    r = make_response(send_from_directory(app.static_folder, "sw.js"))
    r.headers["Content-Type"]           = "application/javascript"
    r.headers["Service-Worker-Allowed"] = "/"
    return r


@app.route("/manifest.json")
def manifest():
    r = make_response(
        send_from_directory(app.static_folder, "manifest.json"))
    r.headers["Content-Type"] = "application/manifest+json"
    return r


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

        # Validate password length
        if not validate_password(password):
            return render_template("signup.html",
                                   error_password="Password must be at least 6 characters.")

        # Validate phone — 10 digits, no letters
        if not validate_phone(phone):
            return render_template("signup.html",
                                   error_phone="Phone number must be exactly 10 digits with no letters.")

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
            return render_template("signup.html",
                                   error="Please fill in all fields")

        if User.query.filter_by(first_name=first).first():
            return render_template("signup.html",
                                   error="That name is already taken")

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

            old_reqs = PasswordResetRequest.query.filter_by(
                user_id=user.id, is_handled=False).all()
            for old in old_reqs:
                db.session.delete(old)
            db.session.flush()

            db.session.add(PasswordResetRequest(user_id=user.id))
            db.session.commit()

            # Notify all superusers
            superusers = User.query.filter_by(role="superuser").all()
            for su in superusers:
                send_ntfy_to_user(
                    su,
                    title    = "Password Reset Request",
                    message  = (
                        f"{user.first_name} {user.last_name} needs "
                        f"a password reset. Open Church App → Manage Users."
                    ),
                    priority = "high",
                    tags     = ["key"]
                )

            return render_template("forgot_password.html",
                                   success="Request sent. The superuser will "
                                   "set a temporary password for you soon.")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Forgot password error: {e}")
            return render_template("forgot_password.html",
                                   error="Something went wrong. Try again.")

    return render_template("forgot_password.html")


# ----------------------
# DASHBOARD
# ----------------------
@app.route("/dashboard")
@login_required
def dashboard():
    incomplete       = not profile_complete(current_user)
    visible_releases = []
    user_assignments = []  # list of {release, driver_info, passenger_info, acknowledged}

    try:
        visible_releases = RouteRelease.query.filter_by(
            is_visible=True).order_by(
            RouteRelease.released_at.desc()).all()

        user_name = (current_user.first_name + " " +
                     current_user.last_name)

        for release in visible_releases:
            driver_info    = None
            passenger_info = None

            ack = RouteAcknowledgement.query.filter_by(
                user_id    = current_user.id,
                release_id = release.id
            ).first()
            acknowledged = ack is not None

            try:
                route_data = json.loads(release.route_data)

                for direction in ["morning", "return"]:
                    result = route_data.get(direction)
                    if not result or "routes" not in result:
                        continue

                    for route in result["routes"]:
                        if route["driver"] == user_name:
                            passengers_list = []
                            for stop in route["stops"]:
                                for pname in stop["passengers"]:
                                    passengers_list.append({
                                        "name":    pname,
                                        "address": stop["address"]
                                    })
                            all_stops = [s["address"]
                                         for s in route["stops"]]
                            maps_url  = build_maps_url(
                                all_stops, release.destination)
                            driver_info = {
                                "direction":   direction,
                                "destination": release.destination,
                                "passengers":  passengers_list,
                                "time_min":    route["time_min"],
                                "distance_km": route["distance_km"],
                                "maps_url":    maps_url
                            }

                        for stop in route["stops"]:
                            for pname in stop["passengers"]:
                                if pname == user_name:
                                    passenger_info = {
                                        "direction":   direction,
                                        "destination": release.destination,
                                        "driver":      route["driver"],
                                        "address":     stop["address"],
                                    }

            except Exception as e:
                app.logger.error(
                    f"Dashboard release parse error: {e}")

            # Only add to user's view if they have an assignment
            if driver_info or passenger_info:
                user_assignments.append({
                    "release":      release,
                    "driver_info":  driver_info,
                    "passenger_info": passenger_info,
                    "acknowledged": acknowledged
                })

    except Exception as e:
        app.logger.error(f"Dashboard error: {e}")

    ntfy_topic = make_user_ntfy_topic(current_user)

    return render_template("dashboard.html",
                           incomplete=incomplete,
                           unread_count=get_unread_count(),
                           feedback_unread=get_feedback_unread_count(),
                           user_assignments=user_assignments,
                           ntfy_topic=ntfy_topic)


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

        if not validate_phone(phone):
            return render_template("profile.html",
                                   error_phone="Phone must be exactly 10 digits, no letters.",
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

        current_user.address    = address
        current_user.phone      = phone
        current_user.ntfy_topic = make_user_ntfy_topic(current_user)

        if old_is_driver != is_driver:
            for poll in Poll.query.all():
                eligible = (poll.target in ("everyone", "drivers")
                            if is_driver
                            else poll.target in ("everyone", "passengers"))
                if not eligible:
                    stale = PollResponse.query.filter_by(
                        user_id=current_user.id,
                        poll_id=poll.id).first()
                    if stale:
                        db.session.delete(stale)

        db.session.commit()
        return render_template("profile.html",
                               success="Changes Saved!",
                               unread_count=get_unread_count())

    return render_template("profile.html",
                           unread_count=get_unread_count())


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
    if not validate_password(new_pw):
        return render_template("profile.html",
                               password_error="Password must be at least 6 characters.",
                               unread_count=get_unread_count())

    current_user.password = generate_password_hash(new_pw)
    db.session.commit()
    return render_template("profile.html",
                           password_success="Password updated!",
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
# SET NTFY TOPIC
# ----------------------
@app.route("/set_ntfy_topic", methods=["POST"])
@login_required
def set_ntfy_topic():
    """
    Called when user saves their profile — assigns their personal topic.
    """
    try:
        topic = make_user_ntfy_topic(current_user)
        current_user.ntfy_topic = topic
        db.session.commit()
        return jsonify({"status": "ok", "topic": topic})
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
                    can_vote = (
                        poll.target in ("everyone", "drivers")
                        if current_user.is_driver
                        else poll.target in ("everyone", "passengers")
                    )
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

            user_eligible = (
                poll.target in ("everyone", "drivers")
                if current_user.is_driver
                else poll.target in ("everyone", "passengers")
            )
            closed        = poll_is_closed(poll)
            options_list  = (
                [o.strip() for o in poll.options.split("|||") if o.strip()]
                if poll.options else []
            )
            responses     = PollResponse.query.filter_by(
                poll_id=poll.id).all()

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
            user_answer = (
                existing.answer
                if existing and existing.answer in options_list
                else None
            )

            closes_at_str = None
            if poll.closes_at:
                try:
                    local_dt = poll.closes_at.replace(
                        tzinfo=timezone.utc).astimezone(LOCAL_TZ)
                    closes_at_str = local_dt.strftime(
                        "%b %d, %Y at %I:%M %p")
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
                f"/superuser/users?error_id=0"
                f"&error_msg={quote('User not found')}")

        address = request.form.get("address", "").strip()
        if not address:
            return redirect(
                f"/superuser/users?error_id={user_id}"
                f"&error_msg={quote('Please select a valid address')}")

        phone = request.form.get("phone", "").strip()
        if phone and not validate_phone(phone):
            return redirect(
                f"/superuser/users?error_id={user_id}"
                f"&error_msg={quote('Phone must be 10 digits, no letters')}")

        new_role = (
            "superuser" if user.id == current_user.id
            else request.form.get("role", "user")
        )
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
                eligible = (
                    poll.target in ("everyone", "drivers") if is_driver
                    else poll.target in ("everyone", "passengers")
                )
                if not eligible:
                    stale = PollResponse.query.filter_by(
                        user_id=user.id, poll_id=poll.id).first()
                    if stale:
                        db.session.delete(stale)

        new_password = request.form.get("new_password", "").strip()
        if new_password:
            if not validate_password(new_password):
                return redirect(
                    f"/superuser/users?error_id={user_id}"
                    f"&error_msg={quote('Password must be at least 6 characters')}")
            user.password = generate_password_hash(new_password)
            for req in PasswordResetRequest.query.filter_by(
                    user_id=user.id, is_handled=False).all():
                req.is_handled = True
            send_ntfy_to_user(
                user,
                title    = "Password Reset",
                message  = (
                    f"Your Church App password has been reset. "
                    f"Temporary: {new_password} "
                    f"Please log in and change it from your Profile."
                ),
                priority = "high",
                tags     = ["key"]
            )

        user.first_name = (
            request.form.get("first_name", "").strip() or user.first_name)
        user.last_name  = (
            request.form.get("last_name", "").strip() or user.last_name)
        user.address    = address
        user.phone      = phone
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
        RouteAcknowledgement.query.filter_by(user_id=user_id).delete()
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
                    closes_at_str, "%Y-%m-%dT%H:%M").replace(
                    tzinfo=LOCAL_TZ)
                if local_dt < datetime.now(LOCAL_TZ):
                    return render_template(
                        "create_poll.html",
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
            poll = Poll(title=title,
                        options="|||".join(options),
                        target=target,
                        closes_at=closes_at)
            db.session.add(poll)
            db.session.commit()
            notify_new_poll(poll)
        except Exception as e:
            db.session.rollback()
            return render_template("create_poll.html",
                                   error=f"Could not save: {str(e)}",
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

    options_list = (
        [o.strip() for o in poll.options.split("|||") if o.strip()]
        if poll.options else []
    )
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
        "id": poll.id, "title": poll.title,
        "target": poll.target,
        "options": options_list, "counts": counts,
        "voters": voters, "closes_at": closes_str
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
                closes_at_str, "%Y-%m-%dT%H:%M").replace(
                tzinfo=LOCAL_TZ)
            if local_dt < datetime.now(LOCAL_TZ):
                return render_template(
                    "create_poll.html",
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

    old_options = (
        [o.strip() for o in poll.options.split("|||") if o.strip()]
        if poll.options else []
    )
    for removed in [o for o in old_options if o not in new_options]:
        for resp in PollResponse.query.filter_by(
                poll_id=poll.id, answer=removed).all():
            db.session.delete(resp)

    poll.title     = new_title
    poll.options   = "|||".join(new_options)
    poll.target    = new_target
    poll.closes_at = new_closes_at
    db.session.commit()

    return render_template("create_poll.html",
                           edit_success="Poll updated!",
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
# Returns polls with driver eligibility info
# ----------------------
@app.route("/admin/polls_for_routes")
@login_required
def polls_for_routes():
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    try:
        result = []
        for poll in Poll.query.all():
            options_list = (
                [o.strip() for o in poll.options.split("|||") if o.strip()]
                if poll.options else []
            )
            counts = {opt: 0 for opt in options_list}
            voters = {opt: [] for opt in options_list}

            for r in PollResponse.query.filter_by(poll_id=poll.id).all():
                if r.answer in counts:
                    counts[r.answer] += 1
                    u = db.session.get(User, r.user_id)
                    if u:
                        voters[r.answer].append(u.first_name)

            result.append({
                "id":        poll.id,
                "title":     poll.title,
                "target":    poll.target,
                "options":   options_list,
                "counts":    counts,
                "voters":    voters,
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
def generate_routes_api():
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
        return jsonify({"error": "No drivers selected. Please pick a driver poll option."})
    if not passenger_names:
        return jsonify({"error": "No passengers selected. Please pick a passenger poll option."})

    # Build driver objects — verify each is actually a driver
    drivers        = []
    non_drivers    = []
    for name in driver_names:
        u = User.query.filter_by(first_name=name).first()
        if not u:
            continue
        if not u.is_driver:
            non_drivers.append(name)
            continue
        if not u.address:
            return jsonify({
                "error": f"{name} does not have an address in their profile."})
        if not u.capacity or u.capacity < 1:
            return jsonify({
                "error": f"{name} does not have a valid capacity set."})
        drivers.append({
            "name":         u.first_name + " " + u.last_name,
            "address":      u.address,
            "capacity":     u.capacity,
            "morning":      True,
            "is_returning": True
        })

    # Build passenger objects — treat as passengers regardless of is_driver
    passengers = []
    for name in passenger_names:
        u = User.query.filter_by(first_name=name).first()
        if u and u.address:
            passengers.append({
                "name":         u.first_name + " " + u.last_name,
                "address":      u.address,
                "morning":      True,
                "is_returning": True
            })

    if not drivers:
        msg = "None of the selected drivers are registered as drivers."
        if non_drivers:
            msg += (f" These people are passengers, not drivers: "
                    f"{', '.join(non_drivers)}.")
        return jsonify({"error": msg})

    if not passengers:
        return jsonify({
            "error": "None of the selected passengers have valid addresses."})

    warnings = []
    if non_drivers:
        warnings.append(
            f"Note: {', '.join(non_drivers)} are not registered as "
            f"drivers and were skipped.")

    try:
        response = {"warnings": warnings}
        errors   = []

        if mode in ("morning", "both"):
            result = optimize_morning(drivers, passengers, church_address)
            if "error" in result:
                errors.append("Morning: " + "; ".join(result["error"]))
            else:
                response["morning"] = result

        if mode in ("return", "both"):
            result = optimize_return(drivers, passengers, church_address)
            if "error" in result:
                errors.append("Return: " + "; ".join(result["error"]))
            else:
                response["return"] = result

        if errors and "morning" not in response and "return" not in response:
            return jsonify({"error": " | ".join(errors)})

        if errors:
            response["partial_errors"] = errors

        return jsonify(response)

    except Exception as e:
        app.logger.error(f"generate_routes error: {e}")
        return jsonify({"error": f"Route generation failed: {str(e)}"})


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
        release = RouteRelease(
            created_by  = current_user.id,
            direction   = direction,
            destination = destination,
            route_data  = json.dumps(route_data),
            is_visible  = True,
            released_at = now_utc_naive()
        )
        db.session.add(release)
        db.session.commit()

        notify_route_released(release)

        return jsonify({
            "status":  "ok",
            "message": "Routes released to eligible users!",
            "id":      release.id
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Release routes error: {e}")
        return jsonify({"error": str(e)})


# ----------------------
# ACKNOWLEDGE ROUTE
# ----------------------
@app.route("/acknowledge_route", methods=["POST"])
@login_required
def acknowledge_route():
    try:
        data       = request.get_json()
        release_id = data.get("release_id")

        if not release_id:
            return jsonify({"error": "No release_id provided"})

        existing = RouteAcknowledgement.query.filter_by(
            user_id    = current_user.id,
            release_id = release_id
        ).first()

        if not existing:
            db.session.add(RouteAcknowledgement(
                user_id    = current_user.id,
                release_id = release_id
            ))
            db.session.commit()

        return jsonify({"status": "ok"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)})


# ----------------------
# ADMIN: ROUTE HISTORY PAGE
# ----------------------
@app.route("/admin/route_history")
@login_required
def route_history():
    if current_user.role not in ("admin", "superuser"):
        return "Access Denied"

    releases = RouteRelease.query.order_by(
        RouteRelease.created_at.desc()).all()

    history = []
    for r in releases:
        released_str = None
        if r.released_at:
            try:
                local_dt     = r.released_at.replace(
                    tzinfo=timezone.utc).astimezone(LOCAL_TZ)
                released_str = local_dt.strftime("%b %d, %Y at %I:%M %p")
            except Exception:
                released_str = "Unknown"

        created_str = None
        try:
            local_dt    = r.created_at.replace(
                tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            created_str = local_dt.strftime("%b %d, %Y at %I:%M %p")
        except Exception:
            created_str = "Unknown"

        history.append({
            "id":          r.id,
            "direction":   r.direction,
            "destination": r.destination,
            "is_visible":  r.is_visible,
            "released_at": released_str,
            "created_at":  created_str,
            "route_data":  r.route_data
        })

    return render_template("route_history.html",
                           history=history,
                           unread_count=get_unread_count())


# ----------------------
# ADMIN: TOGGLE ROUTE VISIBILITY
# ----------------------
@app.route("/admin/toggle_route/<int:release_id>", methods=["POST"])
@login_required
def toggle_route(release_id):
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    release = db.session.get(RouteRelease, release_id)
    if not release:
        return jsonify({"error": "Release not found"}), 404

    release.is_visible = not release.is_visible
    if release.is_visible:
        release.released_at = now_utc_naive()
        db.session.commit()
        notify_route_released(release)
    else:
        db.session.commit()

    return jsonify({
        "status":     "ok",
        "is_visible": release.is_visible
    })


# ----------------------
# ADMIN: DELETE ROUTE RELEASE
# ----------------------
@app.route("/admin/delete_route/<int:release_id>", methods=["POST"])
@login_required
def delete_route(release_id):
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    release = db.session.get(RouteRelease, release_id)
    if release:
        RouteAcknowledgement.query.filter_by(
            release_id=release_id).delete()
        db.session.delete(release)
        db.session.commit()

    return jsonify({"status": "ok"})


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
        db.session.add(Feedback(user_id=current_user.id,
                                message=message))
        db.session.commit()
        return render_template("feedback.html",
                               success="Thanks for your feedback!",
                               unread_count=get_unread_count())
    return render_template("feedback.html",
                           unread_count=get_unread_count())


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
    for fb in Feedback.query.order_by(
            Feedback.created_at.desc()).all():
        u = db.session.get(User, fb.user_id)
        try:
            local_dt    = fb.created_at.replace(
                tzinfo=timezone.utc).astimezone(LOCAL_TZ)
            created_str = local_dt.strftime("%b %d, %Y at %I:%M %p")
        except Exception:
            created_str = "Unknown"
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
@app.route("/admin/delete_feedback/<int:feedback_id>",
           methods=["POST"])
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
    return render_template("routes.html",
                           unread_count=get_unread_count())


# ----------------------
# LOGOUT
# ----------------------
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ----------------------
# TEST ROUTES
# ----------------------
@app.route("/test_ntfy")
@login_required
def test_ntfy():
    if current_user.role != "superuser":
        return "Access denied", 403

    # Assign topic if not set
    if not current_user.ntfy_topic:
        current_user.ntfy_topic = make_user_ntfy_topic(current_user)
        db.session.commit()

    topic = current_user.ntfy_topic
    url   = f"{NTFY_SERVER}/{topic}"

    try:
        response = http_requests.post(
            url,
            data="Test from Church App!".encode("utf-8"),
            headers={"Title": "Church App Test", "Priority": "high"},
            timeout=15
        )
        if response.status_code == 200:
            return (
                f"Notification sent!<br>"
                f"Your personal topic: <b>{topic}</b><br>"
                f"Subscribe to this in ntfy to receive your notifications."
            )
        else:
            return f"Failed: status {response.status_code}"
    except Exception as e:
        return f"Error: {str(e)}"


@app.route("/test_poll_notification")
@login_required
def test_poll_notification():
    if current_user.role != "superuser":
        return "Access denied", 403

    users    = User.query.all()
    sent     = 0
    skipped  = 0
    for user in users:
        if not user.ntfy_topic:
            skipped += 1
            continue
        send_ntfy_to_user(
            user,
            title    = "New Poll Available",
            message  = "TEST: A new poll is open. Open Church App to vote!",
            priority = "high",
            tags     = ["ballot_box"]
        )
        sent += 1

    return (
        f"Poll notification test complete.<br>"
        f"Sent to: {sent} users<br>"
        f"Skipped (no ntfy topic set): {skipped} users<br>"
        f"Users need to save their profile at least once "
        f"to get a personal topic assigned."
    )


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
                phone="0000000000",
                address="114 Lane St, Guelph, ON"
            ))

        if not User.query.filter_by(first_name="Super").first():
            db.session.add(User(
                first_name="Super", last_name="User",
                password=generate_password_hash("super123"),
                role="superuser", is_driver=False, capacity=0,
                phone="0000000000",
                address="114 Lane St, Guelph, ON"
            ))

        db.session.commit()
        return ("Setup complete!<br>"
                "Admin: Admin / admin123<br>"
                "Superuser: Super / super123")
    except Exception as e:
        return f"Error: {str(e)}"


@app.route("/migrate")
def migrate():
    try:
        db.create_all()
        results = ["All tables created/verified"]

        with db.engine.connect() as conn:
            for table, column, col_type in [
                ("poll",  "closes_at", "TIMESTAMP NULL"),
                ("user",  "ntfy_topic", "VARCHAR(100)"),
                ("route_release", "released_at", "TIMESTAMP NULL"),
                ("route_release", "is_visible",
                 "BOOLEAN DEFAULT FALSE"),
            ]:
                try:
                    conn.execute(db.text(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN {column} {col_type}"
                    ))
                    conn.commit()
                    results.append(f"Added {column} to {table}")
                except Exception as e:
                    err = str(e).lower()
                    if "already exists" in err or "duplicate" in err:
                        results.append(
                            f"{table}.{column} already exists")
                    else:
                        results.append(
                            f"Error {table}.{column}: {str(e)}")

        return "<br>".join(results)
    except Exception as e:
        return f"Error: {str(e)}"


# ----------------------
# GOOGLE MAPS HELPER
# ----------------------
def build_maps_url(stops, destination):
    if not stops:
        return None
    base  = "https://www.google.com/maps/dir/"
    parts = [quote_plus(s) for s in stops]
    parts.append(quote_plus(destination))
    return base + "/".join(parts)


@app.cli.command("create-db")
def create_db():
    db.create_all()
    print("Database created!")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")