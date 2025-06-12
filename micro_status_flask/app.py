from flask import Flask, render_template, request
import sqlite3
from datetime import datetime, timedelta

from flask_login import login_required, current_user

import settings
from auth import setup_auth, user_info

app = Flask(__name__)

app, login_manager = setup_auth(app)

# def get_data():
#     conn = sqlite3.connect(settings.DB_LOCATION)
#     cursor = conn.cursor()
#     cursor.execute("SELECT id, path_on_fast_store, imaging_status, processing_status, moved, moving, paused FROM dataset")
#     columns = [description[0] for description in cursor.description]
#     rows = cursor.fetchall()
#     conn.close()
#     return columns, rows


# Connect to database and fetch data
def get_data(date_filter, name_filter):
    conn = sqlite3.connect(settings.DB_LOCATION)
    cursor = conn.cursor()

    base_query = "SELECT id, path_on_fast_store, imaging_status, processing_status, moved, moving, paused FROM dataset"
    conditions = []
    params = []

    if date_filter == 'week':
        since = datetime.now() - timedelta(weeks=1)
    elif date_filter == 'month':
        since = datetime.now() - timedelta(days=30)
    elif date_filter == 'year':
        since = datetime.now() - timedelta(days=365)
    else:
        since = None

    if since:
        since_str = since.strftime("%Y-%m-%d_%H-%M-%S")
        conditions.append(f'created > "{since_str}"')

    if name_filter:
        # conditions.append(f'pi = {name_filter}')
        conditions.append(f'pi = {name_filter}')

    if conditions:
        base_query += ' WHERE ' + ' AND '.join(conditions)

    print("base_query", base_query)
    cursor.execute(base_query)
    rows = cursor.fetchall()
    columns = [description[0] for description in cursor.description]
    conn.close()
    return columns, rows


@app.route('/')
@login_required
def index():
    date_filter = request.args.get('time', 'week')
    pi_id_filter = request.args.get('name', '')  # still using 'name' as the param, but it's actually the id

    # Fetch PI names
    conn = sqlite3.connect(settings.DB_LOCATION)
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM pi")
    pi_options = cursor.fetchall()
    conn.close()

    if current_user.get_id() == "CBI_Admin":
        columns, rows = get_data(date_filter, pi_id_filter)
    else:
        pi_id = [pi_id for pi_id, pi_name in pi_options if pi_name == current_user.get_id()]
        if len(pi_id):
            pi_id_filter = pi_id[0]
            columns, rows = get_data(date_filter, pi_id_filter)
        else:
            columns, rows = [], []

    return render_template(
        'index.html',
        columns=columns,
        rows=rows,
        selected_filter=date_filter,
        selected_name=pi_id_filter,
        pi_options=pi_options,
        current_user=current_user
    )


if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=1414)
