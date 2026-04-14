from flask import Flask, render_template, request, redirect, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Poll, PollResponse

app = Flask(__name__)

import os
database_url = os.environ.get('DATABASE_URL', 'sqlite:///users.db')
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SECRET_KEY'] = 'secret123'

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----------------------
# HELPER: build all_polls_json
# ----------------------
def get_all_polls_json():
    return [
        {"id": p.id, "title": p.title, "target": p.target}
        for p in Poll.query.all()
    ]


# ----------------------
# HELPER: profile complete check
# ----------------------
def profile_complete(user):
    return bool(user.address and user.phone)


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
        first    = request.form.get("first_name", "").strip()
        last     = request.form.get("last_name", "").strip()
        password = request.form.get("password", "")
        phone    = request.form.get("phone", "").strip()
        address  = request.form.get("address", "").strip()
        is_driver_val = request.form.get("is_driver", "no")
        is_driver = is_driver_val == "yes"

        capacity = 0
        if is_driver:
            try:
                capacity = int(request.form.get("capacity", 0))
                if capacity < 1 or capacity > 8:
                    return render_template("signup.html",
                                           error="Capacity must be between 1 and 8")
            except ValueError:
                return render_template("signup.html", error="Invalid capacity")

        if not all([first, last, password, phone, address]):
            return render_template("signup.html", error="Please fill in all fields")

        existing = User.query.filter_by(first_name=first).first()
        if existing:
            return render_template("signup.html", error="That name is already taken")

        user = User(
            first_name=first,
            last_name=last,
            password=generate_password_hash(password),
            phone=phone,
            address=address,
            role="user",
            is_driver=is_driver,
            capacity=capacity
        )

        db.session.add(user)
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
    incomplete = not profile_complete(current_user)
    return render_template("dashboard.html", incomplete=incomplete)


# ----------------------
# PROFILE
# ----------------------
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.address = request.form.get("address", "").strip()
        current_user.phone   = request.form.get("phone", "").strip()

        old_is_driver = current_user.is_driver
        is_driver = request.form.get("is_driver")
        new_is_driver = True if is_driver == "on" else False
        current_user.is_driver = new_is_driver

        if current_user.is_driver:
            try:
                cap = int(request.form.get("capacity"))
                if cap < 1 or cap > 8:
                    return render_template("profile.html", error="Capacity must be 1–8")
                current_user.capacity = cap
            except:
                return render_template("profile.html", error="Invalid capacity")
        else:
            current_user.capacity = 0

        if old_is_driver != new_is_driver:
            all_polls = Poll.query.all()
            for poll in all_polls:
                if new_is_driver:
                    eligible = poll.target in ("everyone", "drivers")
                else:
                    eligible = poll.target in ("everyone", "passengers")

                if not eligible:
                    stale = PollResponse.query.filter_by(
                        user_id=current_user.id,
                        poll_id=poll.id
                    ).first()
                    if stale:
                        db.session.delete(stale)

        db.session.commit()
        return render_template("profile.html", success="Saved!")

    return render_template("profile.html")


# ----------------------
# POLLS
# ----------------------
@app.route("/polls", methods=["GET", "POST"])
@login_required
def polls():

    # Block voting if profile is incomplete (shouldn't happen via signup
    # anymore, but guards legacy accounts or direct POST attempts)
    if not profile_complete(current_user):
        return render_template("polls.html", polls=[],
                               profile_warning=True)

    if request.method == "POST":
        poll_id = request.form.get("poll_id")
        answer  = request.form.get("answer")

        if not poll_id or not answer:
            return redirect("/polls")

        poll = Poll.query.get(int(poll_id))
        if poll:
            if current_user.is_driver:
                can_vote = poll.target in ("everyone", "drivers")
            else:
                can_vote = poll.target in ("everyone", "passengers")

            if can_vote:
                old = PollResponse.query.filter_by(
                    user_id=current_user.id,
                    poll_id=poll_id
                ).first()
                if old:
                    db.session.delete(old)

                response = PollResponse(
                    user_id=current_user.id,
                    poll_id=poll_id,
                    answer=answer
                )
                db.session.add(response)
                db.session.commit()

    all_polls = Poll.query.all()
    poll_data = []

    for poll in all_polls:

        if current_user.role != "admin":
            if not (
                poll.target == "everyone"
                or (poll.target == "drivers" and current_user.is_driver)
                or (poll.target == "passengers" and not current_user.is_driver)
            ):
                continue

        if current_user.is_driver:
            user_eligible = poll.target in ("everyone", "drivers")
        else:
            user_eligible = poll.target in ("everyone", "passengers")

        if poll.options:
            options_list = [o for o in poll.options.split("|||") if o.strip()]
        else:
            options_list = []

        responses = PollResponse.query.filter_by(poll_id=poll.id).all()

        counts = {opt: 0 for opt in options_list}
        users  = {opt: [] for opt in options_list}

        for r in responses:
            if r.answer in counts:
                counts[r.answer] += 1
                u = User.query.get(r.user_id)
                if u:
                    users[r.answer].append(u.first_name)

        existing_response = PollResponse.query.filter_by(
            user_id=current_user.id,
            poll_id=poll.id
        ).first()
        user_answer = existing_response.answer if existing_response else None

        poll_data.append({
            "id":           poll.id,
            "title":        poll.title,
            "target":       poll.target,
            "options":      options_list,
            "counts":       counts,
            "users":        users,
            "total":        len(responses),
            "user_eligible": user_eligible,
            "user_answer":  user_answer,
        })

    return render_template("polls.html", polls=poll_data, profile_warning=False)


# ----------------------
# ADMIN: CREATE POLL
# ----------------------
@app.route("/admin/create_poll", methods=["GET", "POST"])
@login_required
def create_poll():

    if current_user.role != "admin":
        return "Access Denied"

    if request.method == "POST":
        title  = request.form.get("title")
        target = request.form.get("target")

        options = []
        for i in range(10):
            opt = request.form.get(f"option_{i}")
            if opt is None:
                continue
            opt = opt.strip()
            if opt != "":
                options.append(opt)

        if not options:
            return render_template("create_poll.html",
                                   error="Add at least 1 option",
                                   all_polls=get_all_polls_json())

        poll = Poll(
            title=title,
            options="|||".join(options),
            target=target
        )

        db.session.add(poll)
        db.session.commit()

        return render_template("create_poll.html",
                               success="Poll created!",
                               all_polls=get_all_polls_json())

    return render_template("create_poll.html", all_polls=get_all_polls_json())


# ----------------------
# ADMIN: GET POLL DATA (for edit form, via fetch)
# ----------------------
@app.route("/admin/get_poll/<int:poll_id>")
@login_required
def get_poll(poll_id):
    if current_user.role != "admin":
        return jsonify({"error": "Access Denied"}), 403

    poll = Poll.query.get(poll_id)
    if not poll:
        return jsonify({"error": "Poll not found"}), 404

    options_list = [o for o in poll.options.split("|||") if o.strip()] if poll.options else []

    return jsonify({
        "id":      poll.id,
        "title":   poll.title,
        "target":  poll.target,
        "options": options_list
    })


# ----------------------
# ADMIN: EDIT POLL
# ----------------------
@app.route("/admin/edit_poll", methods=["POST"])
@login_required
def edit_poll():
    if current_user.role != "admin":
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
        opt = request.form.get(f"edit_option_{i}")
        if opt is None:
            continue
        opt = opt.strip()
        if opt != "":
            new_options.append(opt)

    if not new_options:
        return render_template("create_poll.html",
                               error="Poll must have at least 1 option",
                               all_polls=get_all_polls_json())

    old_options     = [o for o in poll.options.split("|||") if o.strip()] if poll.options else []
    removed_options = [o for o in old_options if o not in new_options]

    for removed_opt in removed_options:
        stale_responses = PollResponse.query.filter_by(
            poll_id=poll.id,
            answer=removed_opt
        ).all()
        for resp in stale_responses:
            db.session.delete(resp)

    poll.title   = new_title
    poll.options = "|||".join(new_options)
    poll.target  = new_target

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
    if current_user.role != "admin":
        return "Access Denied"

    ids_to_delete = request.form.getlist("delete_ids")

    for poll_id in ids_to_delete:
        poll = Poll.query.get(int(poll_id))
        if poll:
            PollResponse.query.filter_by(poll_id=poll.id).delete()
            db.session.delete(poll)

    db.session.commit()

    deleted_count = len(ids_to_delete)
    msg = (
        f"Deleted {deleted_count} poll{'s' if deleted_count != 1 else ''} successfully!"
        if deleted_count > 0 else "No polls were selected."
    )

    return render_template("create_poll.html",
                           delete_success=msg,
                           all_polls=get_all_polls_json())


# ----------------------
# ADMIN ROUTES PAGE
# ----------------------
@app.route("/admin/routes")
@login_required
def routes():
    if current_user.role != "admin":
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
# DB INIT COMMAND
# ----------------------
@app.cli.command("create-db")
def create_db():
    db.create_all()
    print("Database created!")

# ----------------------
# SETUP ROUTE (run once after deploy, then remove)
# ----------------------
@app.route("/setup")
def setup():
    db.create_all()

    # Check if admin already exists to avoid duplicates
    existing = User.query.filter_by(first_name="Admin").first()
    if not existing:
        admin = User(
            first_name="Admin",
            last_name="User",
            password=generate_password_hash("admin123"),
            role="admin",
            is_driver=False,
            capacity=0,
            phone="0000000000",
            address="114 Lane St, Guelph, ON"
        )
        db.session.add(admin)
        db.session.commit()
        return "✅ Database created and admin account set up!"

    return "✅ Database already exists, nothing changed."


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")