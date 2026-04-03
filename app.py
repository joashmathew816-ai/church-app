from flask import Flask, render_template, request
from optimizer import optimize_morning

print("Starting app...")
app = Flask(__name__)


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

            # Validation
            if not all([street, city, province, capacity_raw]):
                error = "❌ Fill all driver fields"
                return render_template("index.html", error=error)

            try:
                capacity = int(capacity_raw)
                if capacity < 1 or capacity > 8:
                    raise ValueError
            except:
                error = "❌ Capacity must be between 1 and 8"
                return render_template("index.html", error=error)

            drivers.append({
                "name": name,
                "address": f"{street}, {city}, {province}",
                "capacity": capacity,
                "morning": True,
                "return": True
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
                error = "❌ Fill all passenger fields"
                return render_template("index.html", error=error)

            passengers.append({
                "name": name,
                "address": f"{street}, {city}, {province}",
                "morning": True,
                "return": True
            })

            i += 1

        # --------------------------
        # FINAL VALIDATION
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


if __name__ == "__main__":
    # IMPORTANT: allows phone access
    app.run(debug=True, host="0.0.0.0")