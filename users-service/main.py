from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from datetime import datetime
from functools import wraps
import os
import time

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 
    'postgresql://postgres:postgres@db:5432/userdb'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Prometheus metrics
REQUEST_COUNT = Counter(
    'http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

REQUEST_LATENCY = Histogram(
    'http_request_duration_seconds',
    'HTTP request latency',
    ['method', 'endpoint']
)

HTTP_ERRORS = Counter(
    'http_errors_total',
    'Total HTTP errors',
    ['method', 'endpoint', 'status']
)

SERVICE_UP = Gauge(
    'service_up',
    'Service availability (1 = up, 0 = down)'
)

ACTIVE_USERS = Gauge(
    'active_users_total',
    'Total number of users in database'
)

# Set service as up
SERVICE_UP.set(1)

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'email': self.email,
            'created_at': self.created_at.isoformat()
        }

def track_metrics(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        endpoint = request.endpoint or 'unknown'
        method = request.method
        
        try:
            response = f(*args, **kwargs)
            status = response[1] if isinstance(response, tuple) else 200
            
            # Track metrics
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=status).inc()
            REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(time.time() - start_time)
            
            if status >= 400:
                HTTP_ERRORS.labels(method=method, endpoint=endpoint, status=status).inc()
            
            return response
        except Exception as e:
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=500).inc()
            HTTP_ERRORS.labels(method=method, endpoint=endpoint, status=500).inc()
            REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(time.time() - start_time)
            raise e
    
    return decorated_function

def wait_for_db():
    max_retries = 30
    retry_count = 0
    while retry_count < max_retries:
        try:
            db.session.execute(db.text('SELECT 1'))
            print("Database connection established!")
            return True
        except Exception as e:
            retry_count += 1
            print(f"Waiting for database... ({retry_count}/{max_retries})")
            time.sleep(1)
    return False

def update_user_count():
    try:
        count = User.query.count()
        ACTIVE_USERS.set(count)
    except:
        pass

@app.route('/metrics')
def metrics():
    update_user_count()
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}

@app.route('/health')
@track_metrics
def health():
    return jsonify({'status': 'healthy'})

# User
@app.route('/users', methods=['GET'])
@track_metrics
def get_users():
    users = User.query.all()
    update_user_count()
    return jsonify([u.to_dict() for u in users])

@app.route('/users/<int:id>', methods=['GET'])
@track_metrics
def get_user(id):
    user = User.query.get_or_404(id)
    return jsonify(user.to_dict())

@app.route('/users', methods=['POST'])
@track_metrics
def create_user():
    data = request.get_json()
    if not data or 'name' not in data or 'email' not in data:
        return jsonify({'error': 'name and email are required'}), 400
    
    user = User(name=data['name'], email=data['email'])
    db.session.add(user)
    db.session.commit()
    update_user_count()
    return jsonify(user.to_dict()), 201

@app.route('/users/<int:id>', methods=['PUT'])
@track_metrics
def update_user(id):
    user = User.query.get_or_404(id)
    data = request.get_json()
    user.name = data.get('name', user.name)
    user.email = data.get('email', user.email)
    db.session.commit()
    return jsonify(user.to_dict())

@app.route('/users/<int:id>', methods=['DELETE'])
@track_metrics
def delete_user(id):
    user = User.query.get_or_404(id)
    db.session.delete(user)
    db.session.commit()
    update_user_count()
    return '', 204

if __name__ == '__main__':
    with app.app_context():
        wait_for_db()
        db.create_all()
        print("Database tables created/verified!")
        update_user_count()
    app.run(host='0.0.0.0', port=5000, debug=True)