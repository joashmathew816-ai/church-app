from flask import Flask, render_template, request
from optimizer import optimize_morning
from models import db, User
from flask_login import LoginManager

# --------------------------
# CREATE APP FIRST
# --------------------------
app = Flask(__name__)

# --------------------------
# CONFIG
# --------------------------
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SECRET_KEY'] = 'secret123'

# --------------------------
# INIT DB + LOGIN
# --------------------------
db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --------------------------
# ROUTES
# --------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    result = None
    error = None

    if request.method == "POST":

        drivers = []
        passengers = []

        # --------------------------
        # GET ALL DRIVERS
        # --------------------------
        i = 0
        while True:
            name = request.form.get(f"driver_name_{i}")
            if not name:
                break

            street = request.form.get(f"driver_street_{i}")
            city = request.form.get(f"driver_city_{i}")
            province = request.form.get(f"driver_province_{i}")
            capacity_raw = request.form.get(f"driver_capacity_{i}")

            if not all([street, city, province, capacity_raw]):
                return render_template("index.html", error="❌ Fill all driver fields")

            try:
                capacity = int(capacity_raw)
                if capacity < 1 or capacity > 8:
                    raise ValueError
            except:
                return render_template("index.html", error="❌ Capacity must be 1–8")

            drivers.append({
                "name": name,
                "address": f"{street}, {city}, {province}",
                "capacity": capacity,
                "morning": True,
                "is_returning": True
            })

            i += 1

        # --------------------------
        # GET ALL PASSENGERS
        # --------------------------
        i = 0
        while True:
            name = request.form.get(f"passenger_name_{i}")
            if not name:
                break

            street = request.form.get(f"passenger_street_{i}")
            city = request.form.get(f"passenger_city_{i}")
            province = request.form.get(f"passenger_province_{i}")

            if not all([street, city, province]):
                return render_template("index.html", error="❌ Fill all passenger fields")

            passengers.append({
                "name": name,
                "address": f"{street}, {city}, {province}",
                "morning": True,
                "is_returning": True
            })

            i += 1

        # --------------------------
        # VALIDATION
        # --------------------------
        if not drivers:
            return render_template("index.html", error="❌ Add at least 1 driver")

        if not passengers:
            return render_template("index.html", error="❌ Add at least 1 passenger")

        # --------------------------
        # RUN OPTIMIZER
        # --------------------------
        church = "114 Lane St, Guelph, ON"

        result = optimize_morning(drivers, passengers, church)

        if "error" in result:
            return render_template("index.html", error=result["error"])

    return render_template("index.html", result=result)


# --------------------------
# CREATE DATABASE (RUN ONCE)
# --------------------------
@app.cli.command("create-db")
def create_db():
    db.create_all()
    print("✅ Database created!")


# --------------------------
# RUN APP
# --------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0")