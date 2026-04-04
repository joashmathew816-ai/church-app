from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)

    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))

    address = db.Column(db.String(200))

    is_driver = db.Column(db.Boolean, default=False)
    capacity = db.Column(db.Integer, default=0)

    coming = db.Column(db.Boolean, default=False)
    is_returning = db.Column(db.Boolean, default=False)

    password = db.Column(db.String(200))

    role = db.Column(db.String(10), default="user")  # user or admin