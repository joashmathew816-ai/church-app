from flask import Flask, make_response, render_template, request, redirect, jsonify, send_from_directory
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Poll, PollResponse, Feedback
from optimizer import optimize_morning, optimize_return
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote
import os

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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------------
# HELPERS
# ----------------------
def get_all_polls_json():
    now = datetime.utcnow()
    return [
        {
            "id":        p.id,
            "title":     p.title,
            "target":    p.target,
            "closes_at": p.closes_at.strftime("%Y-%m-%dT%H:%M") if p.closes_at else None,
            "is_closed": p.closes_at is not None and p.closes_at < now
        }
        for p in Poll.query.all()
    ]


def profile_complete(user):
    return bool(user.address and user.phone)


def poll_is_closed(poll):
    if poll.closes_at is None:
        return False
    return poll.closes_at < datetime.utcnow()


def user_eligible_for_poll(user, poll):
    if poll.target == "everyone":
        return True
    if poll.target == "drivers" and user.is_driver:
        return True
    if poll.target == "passengers" and not user.is_driver:
        return True
    return False

# ----------------------
# SERVICE WORKER
# ----------------------
@app.route("/sw.js")
def service_worker():
    response = make_response(
        send_from_directory("static", "sw.js")
    )
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    return response

# Serve manifest
@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json",
                               mimetype="application/manifest+json")


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
# DASHBOARD
# ----------------------
@app.route("/dashboard")
@login_required
def dashboard():
    incomplete   = not profile_complete(current_user)
    unread_count = 0
    if current_user.role == "superuser":
        unread_count = Feedback.query.filter_by(is_read=False).count()

    return render_template("dashboard.html",
                           incomplete=incomplete,
                           unread_count=unread_count)


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
                                   error_address="Please select a valid address from the dropdown")

        old_is_driver      = current_user.is_driver
        is_driver          = request.form.get("is_driver") == "on"
        current_user.is_driver = is_driver

        if is_driver:
            try:
                cap = int(request.form.get("capacity", 0))
                if cap < 1 or cap > 8:
                    return render_template("profile.html",
                                           error_capacity="Capacity must be between 1 and 8")
                current_user.capacity = cap
            except:
                return render_template("profile.html",
                                       error_capacity="Invalid capacity")
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
        return render_template("profile.html", success="✅ Changes Saved!")

    return render_template("profile.html")


# ----------------------
# POLLS
# ----------------------
@app.route("/polls", methods=["GET", "POST"])
@login_required
def polls():
    if not profile_complete(current_user):
        return render_template("polls.html", polls=[], profile_warning=True)

    if request.method == "POST":
        poll_id = request.form.get("poll_id")
        answer  = request.form.get("answer")

        if poll_id and answer:
            poll = Poll.query.get(int(poll_id))
            if poll and not poll_is_closed(poll):
                can_vote = (poll.target in ("everyone", "drivers")
                            if current_user.is_driver
                            else poll.target in ("everyone", "passengers"))
                if can_vote:
                    old = PollResponse.query.filter_by(
                        user_id=current_user.id, poll_id=poll_id).first()
                    if old:
                        db.session.delete(old)
                    db.session.add(PollResponse(
                        user_id=current_user.id,
                        poll_id=poll_id, answer=answer))
                    db.session.commit()

    poll_data = []
    for poll in Poll.query.all():
        if current_user.role not in ("admin", "superuser"):
            if not (poll.target == "everyone"
                    or (poll.target == "drivers"    and current_user.is_driver)
                    or (poll.target == "passengers" and not current_user.is_driver)):
                continue

        user_eligible = (poll.target in ("everyone", "drivers")
                         if current_user.is_driver
                         else poll.target in ("everyone", "passengers"))
        closed       = poll_is_closed(poll)
        options_list = ([o for o in poll.options.split("|||") if o.strip()]
                        if poll.options else [])
        responses    = PollResponse.query.filter_by(poll_id=poll.id).all()

        counts = {opt: 0 for opt in options_list}
        users  = {opt: [] for opt in options_list}
        for r in responses:
            if r.answer in counts:
                counts[r.answer] += 1
                u = User.query.get(r.user_id)
                if u:
                    users[r.answer].append(u.first_name)

        existing = PollResponse.query.filter_by(
            user_id=current_user.id, poll_id=poll.id).first()

        closes_at_str = None
        if poll.closes_at:
            local_dt = poll.closes_at.replace(
                tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ)
            closes_at_str = local_dt.strftime("%b %d, %Y at %I:%M %p")

        poll_data.append({
            "id":            poll.id,
            "title":         poll.title,
            "target":        poll.target,
            "options":       options_list,
            "counts":        counts,
            "users":         users,
            "total":         len(responses),
            "user_eligible": user_eligible,
            "user_answer":   existing.answer if existing else None,
            "closed":        closed,
            "closes_at":     closes_at_str,
        })

    return render_template("polls.html", polls=poll_data, profile_warning=False)


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
        user    = User.query.get(user_id)

        if not user:
            return redirect(f"/superuser/users?error_id=0&error_msg={quote('User not found')}")

        address = request.form.get("address", "").strip()
        if not address:
            return redirect(
                f"/superuser/users?error_id={user_id}"
                f"&error_msg={quote('Please select a valid address from the dropdown')}")

        new_role  = "superuser" if user.id == current_user.id else request.form.get("role", "user")
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
            except:
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

        user.address   = address
        user.phone     = request.form.get("phone", "").strip()
        user.role      = new_role
        user.is_driver = is_driver
        user.capacity  = capacity
        db.session.commit()

        return redirect(f"/superuser/users?saved={user_id}")

    users = User.query.order_by(User.last_name).all()
    return render_template("superuser_users.html", users=users)


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

    user = User.query.get(user_id)
    if user:
        PollResponse.query.filter_by(user_id=user_id).delete()
        Feedback.query.filter_by(user_id=user_id).delete()
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
                                   all_polls=get_all_polls_json())

        closes_at     = None
        closes_at_str = request.form.get("closes_at", "").strip()
        if closes_at_str:
            try:
                local_dt = datetime.strptime(
                    closes_at_str, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_TZ)
                if local_dt < datetime.now(LOCAL_TZ):
                    return render_template("create_poll.html",
                                           error="Closing time cannot be in the past",
                                           all_polls=get_all_polls_json())
                closes_at = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            except ValueError:
                return render_template("create_poll.html",
                                       error="Invalid date format",
                                       all_polls=get_all_polls_json())

        db.session.add(Poll(
            title=title, options="|||".join(options),
            target=target, closes_at=closes_at))
        db.session.commit()

        return render_template("create_poll.html",
                               success="Poll created!",
                               all_polls=get_all_polls_json())

    return render_template("create_poll.html", all_polls=get_all_polls_json())


# ----------------------
# ADMIN: GET POLL
# ----------------------
@app.route("/admin/get_poll/<int:poll_id>")
@login_required
def get_poll(poll_id):
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    poll = Poll.query.get(poll_id)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404

    options_list = ([o for o in poll.options.split("|||") if o.strip()]
                    if poll.options else [])
    counts = {opt: 0 for opt in options_list}
    voters = {opt: [] for opt in options_list}

    for r in PollResponse.query.filter_by(poll_id=poll_id).all():
        if r.answer in counts:
            counts[r.answer] += 1
            u = User.query.get(r.user_id)
            if u:
                voters[r.answer].append(u.first_name)

    closes_str = ""
    if poll.closes_at:
        closes_str = poll.closes_at.replace(
            tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ).strftime("%Y-%m-%dT%H:%M")

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
    poll    = Poll.query.get(int(poll_id))
    if not poll:
        return render_template("create_poll.html",
                               error="Poll not found",
                               all_polls=get_all_polls_json())

    new_title  = request.form.get("title", "").strip()
    new_target = request.form.get("target")
    new_options = []
    for i in range(20):
        opt = request.form.get(f"edit_option_{i}", "").strip()
        if opt:
            new_options.append(opt)

    if not new_options:
        return render_template("create_poll.html",
                               error="Poll must have at least 1 option",
                               all_polls=get_all_polls_json())

    new_closes_at = None
    closes_at_str = request.form.get("closes_at", "").strip()
    if closes_at_str:
        try:
            local_dt = datetime.strptime(
                closes_at_str, "%Y-%m-%dT%H:%M").replace(tzinfo=LOCAL_TZ)
            if local_dt < datetime.now(LOCAL_TZ):
                return render_template("create_poll.html",
                                       error="Closing time cannot be in the past",
                                       all_polls=get_all_polls_json())
            new_closes_at = local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        except ValueError:
            return render_template("create_poll.html",
                                   error="Invalid date format",
                                   all_polls=get_all_polls_json())

    old_options     = ([o for o in poll.options.split("|||") if o.strip()]
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
                           all_polls=get_all_polls_json())


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
        poll = Poll.query.get(int(poll_id))
        if poll:
            PollResponse.query.filter_by(poll_id=poll.id).delete()
            db.session.delete(poll)
    db.session.commit()

    count = len(ids_to_delete)
    msg   = (f"Deleted {count} poll{'s' if count != 1 else ''} successfully!"
             if count > 0 else "No polls were selected.")

    return render_template("create_poll.html",
                           delete_success=msg,
                           all_polls=get_all_polls_json())


# ----------------------
# ADMIN: POLLS FOR ROUTES
# ----------------------
@app.route("/admin/polls_for_routes")
@login_required
def polls_for_routes():
    if current_user.role not in ("admin", "superuser"):
        return jsonify({"error": "Access Denied"}), 403

    result = []
    for poll in Poll.query.all():
        options_list = ([o for o in poll.options.split("|||") if o.strip()]
                        if poll.options else [])
        counts = {opt: 0 for opt in options_list}
        voters = {opt: [] for opt in options_list}

        for r in PollResponse.query.filter_by(poll_id=poll.id).all():
            if r.answer in counts:
                counts[r.answer] += 1
                u = User.query.get(r.user_id)
                if u:
                    voters[r.answer].append(u.first_name)

        result.append({
            "id": poll.id, "title": poll.title, "target": poll.target,
            "options": options_list, "counts": counts, "voters": voters,
            "is_closed": poll_is_closed(poll)
        })

    return jsonify(result)


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
# FEEDBACK
# ----------------------
@app.route("/feedback", methods=["GET", "POST"])
@login_required
def feedback():
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            return render_template("feedback.html",
                                   error="Please write something before submitting.")
        db.session.add(Feedback(user_id=current_user.id, message=message))
        db.session.commit()
        return render_template("feedback.html", success="✅ Thanks for your feedback!")

    return render_template("feedback.html")


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
        u        = User.query.get(fb.user_id)
        local_dt = fb.created_at.replace(
            tzinfo=ZoneInfo("UTC")).astimezone(LOCAL_TZ)
        feedback_data.append({
            "id":         fb.id,
            "name":       (u.first_name + " " + u.last_name) if u else "Unknown",
            "message":    fb.message,
            "created_at": local_dt.strftime("%b %d, %Y at %I:%M %p")
        })

    return render_template("admin_feedback.html", feedback_list=feedback_data)


# ----------------------
# SUPERUSER: DELETE FEEDBACK
# ----------------------
@app.route("/admin/delete_feedback/<int:feedback_id>", methods=["POST"])
@login_required
def delete_feedback(feedback_id):
    if current_user.role != "superuser":
        return "Access Denied", 403

    fb = Feedback.query.get(feedback_id)
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
    return render_template("routes.html")


# ----------------------
# LOGOUT
# ----------------------
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


# ----------------------
# SETUP
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


@app.cli.command("create-db")
def create_db():
    db.create_all()
    print("Database created!")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")