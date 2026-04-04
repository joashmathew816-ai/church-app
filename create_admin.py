from app import app
from models import db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    admin = User(
        first_name="Admin",
        last_name="User",
        address="Admin Address",
        is_driver=False,
        capacity=0,
        password=generate_password_hash("admin123"),
        role="admin"
    )

    db.session.add(admin)
    db.session.commit()

    print("✅ Admin created!")