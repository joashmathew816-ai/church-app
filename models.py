from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)

    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))

    address = db.Column(db.String(200))
    phone = db.Column(db.String(20))

    is_driver = db.Column(db.Boolean, default=False)
    capacity = db.Column(db.Integer, default=0)

    password = db.Column(db.String(200))

    role = db.Column(db.String(10), default="user")  # "user" or "admin"

class Poll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    options = db.Column(db.Text)
    target = db.Column(db.String(50))

class PollResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    poll_id = db.Column(db.Integer, db.ForeignKey('poll.id'))

    answer = db.Column(db.String(200))

class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    poll_id = db.Column(db.Integer, db.ForeignKey("poll.id"))

    selected_option = db.Column(db.String(200))