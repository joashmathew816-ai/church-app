from app import app
from models import db, User

with app.app_context():
    admin = User(
        first_name="admin",
        last_name="user",
        address="church",
        password="admin123",
        role="admin"
    )
    db.session.add(admin)
    db.session.commit()

    print("Admin created!")