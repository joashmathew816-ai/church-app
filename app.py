from flask import Flask, render_template, request, redirect
from optimizer import optimize_morning
from models import db, User
from flask_login import LoginManager, login_user, login_required, logout_user, current_user

app = Flask(__name__)

# --------------------------
# CONFIG
# --------------------------
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SECRET_KEY'] = 'secret123'
app.config['SESSION_PERMANENT'] = False  # logout when browser closes

# --------------------------
# INIT DB + LOGIN
# --------------------------
db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"  # force login

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --------------------------
# SIGNUP
# --------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        user = User(
            first_name=request.form.get("first_name"),
            last_name=request.form.get("last_name"),
            address=request.form.get("address"),
            password=request.form.get("password"),
            role="user"
        )
        db.session.add(user)
        db.session.commit()
        return redirect("/login")

    return render_template("signup.html")

# --------------------------
# LOGIN (USER)
# --------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(first_name=request.form.get("first_name")).first()

        if user and user.password == request.form.get("password"):
            login_user(user, remember=False)

            if user.role == "admin":
                return redirect("/admin")
            else:
                return redirect("/dashboard")

        return "Invalid login"

    return render_template("login.html")

# --------------------------
# LOGOUT
# --------------------------
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")

# --------------------------
# USER DASHBOARD
# --------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=current_user)

# --------------------------
# ADMIN DASHBOARD
# --------------------------
@app.route("/admin")
@login_required
def admin():
    if current_user.role != "admin":
        return "Access denied"

    return render_template("admin.html", user=current_user)

# --------------------------
# ROUTE GENERATION (ADMIN ONLY)
# --------------------------
@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    if current_user.role != "admin":
        return redirect("/dashboard")

    result = None

    if request.method == "POST":

        drivers = []
        passengers = []

        i = 0
        while True:
            name = request.form.get(f"driver_name_{i}")
            if not name:
                break

            drivers.append({
                "name": name,
                "address": request.form.get(f"driver_address_{i}"),
                "capacity": int(request.form.get(f"driver_capacity_{i}")),
                "morning": True,
                "is_returning": True
            })
            i += 1

        i = 0
        while True:
            name = request.form.get(f"passenger_name_{i}")
            if not name:
                break

            passengers.append({
                "name": name,
                "address": request.form.get(f"passenger_address_{i}"),
                "morning": True,
                "is_returning": True
            })
            i += 1

        church = "114 Lane St, Guelph, ON"
        result = optimize_morning(drivers, passengers, church)

    return render_template("index.html", result=result, user=current_user)

# --------------------------
# CREATE DB COMMAND
# --------------------------
@app.cli.command("create-db")
def create_db():
    db.create_all()
    print("Database created!")

# --------------------------
# RUN
# --------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")