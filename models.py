from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)

    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))

    address = db.Column(db.String(200))

    password = db.Column(db.String(200))

    role = db.Column(db.String(20), default="user")  # 🔥 IMPORTANT