from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func

db = SQLAlchemy()

class Dataset(db.Model):
    __tablename__ = 'dataset'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.Text)
    path_on_fast_store = db.Column(db.Text)
    cl_number = db.Column(db.Integer, db.ForeignKey('clnumber.id'), nullable=True)
    pi = db.Column(db.Integer, db.ForeignKey('pi.id'), nullable=True)
    imaging_status = db.Column(db.Text, nullable=False, default='in_progress')
    processing_status = db.Column(db.Text, nullable=False, default='not_started')
    path_on_hive = db.Column(db.Text)
    job_number = db.Column(db.Text)
    imaris_file_path = db.Column(db.Text)
    channels = db.Column(db.Integer, nullable=False, default=1)
    z_layers_total = db.Column(db.Integer)
    z_layers_current = db.Column(db.Integer)
    ribbons_total = db.Column(db.Integer)
    ribbons_finished = db.Column(db.Integer)
    tiles_total = db.Column(db.Integer)
    tiles_finished = db.Column(db.Integer)
    tiles_x = db.Column(db.Integer)
    tiles_y = db.Column(db.Integer)
    resolution_xy = db.Column(db.Text)
    resolution_z = db.Column(db.Text)
    imaging_no_progress_time = db.Column(db.Text)
    processing_no_progress_time = db.Column(db.Text)
    processing_summary = db.Column(db.Text)
    z_layers_checked = db.Column(db.Integer)
    keep_composites = db.Column(db.Integer, default=0)
    delete_405 = db.Column(db.Integer, default=0)
    created = db.Column(db.Text, default=None)
    modality = db.Column(db.Text, default=None)
    is_brain = db.Column(db.Integer, default=0)
    peace_json_created = db.Column(db.Integer, default=0)
    imaging_summary = db.Column(db.Text)
    moved = db.Column(db.Integer, default=0)
    moving = db.Column(db.Integer, default=0)
    paused = db.Column(db.Integer, default=0)


class PI(db.Model):
    __tablename__ = 'pi'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.Text, nullable=False)
    public_folder_name = db.Column(db.Text)

    # Optional: define reverse relationship if needed
    datasets = db.relationship('Dataset', backref='pi_obj', lazy=True)

    def __repr__(self):
        return f"<PI {self.id}: {self.name}>"


class CLNumber(db.Model):
    __tablename__ = 'clnumber'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.Text, nullable=False, unique=True)
    pi = db.Column(db.Integer, db.ForeignKey('pi.id'), nullable=True)

    # Optional relationship to PI model
    pi_obj = db.relationship('PI', backref='clnumbers', lazy=True)

    # Optional reverse relationship if Dataset has cl_number FK
    datasets = db.relationship('Dataset', backref='cl_obj', lazy=True)

    def __repr__(self):
        return f"<CLNumber {self.id}: {self.name}>"
