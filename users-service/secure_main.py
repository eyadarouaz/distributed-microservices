from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from datetime import datetime
from functools import wraps
import os
import time
import redis
import json
import re
import logging

app = Flask(__name__)

# Security Configuration
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 
    'postgresql://app_user:app_secure_password@db:5432/userdb'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

db = SQLAlchemy(app)

# CORS Configuration - restrictive
cors_origins = os.getenv('CORS_ORIGINS', 'http://localhost:8080').split(',')
CORS(app, origins=cors_origins, supports_credentials=True)

# Rate Limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[
        os.getenv('RATE_LIMIT_PER_MINUTE', '60 per minute'),
        os.getenv('RATE_LIMIT_PER_HOUR', '1000 per hour')
    ],
    storage_uri=os.getenv('REDIS_URL', 'redis://redis:6379/1')
)

# Redis connection
redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')
try:
    redis_client = redis.from_url(redis_url, decode_responses=True)
    redis_client.ping()
    logger.info("Redis connection established!")
except Exception as e:
    logger.error(f"Redis connection failed: {e}")
    redis_client = None

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

CACHE_HITS = Counter(
    'cache_hits_total',
    'Total cache hits',
    ['endpoint']
)

CACHE_MISSES = Counter(
    'cache_misses_total',
    'Total cache misses',
    ['endpoint']
)

SECURITY_EVENTS = Counter(
    'security_events_total',
    'Security events detected',
    ['type']
)

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

# Security Middleware
@app.before_request
def security_logging():
    """Log suspicious requests"""
    suspicious_patterns = [
        "'", '"', '--', ';', 
        'DROP', 'DELETE FROM', 'INSERT INTO',
        '<script', 'javascript:', 'onerror=',
        '../', '..\\', 'etc/passwd'
    ]
    
    request_data = str(request.data) + str(request.args) + str(request.form)
    
    for pattern in suspicious_patterns:
        if pattern.lower() in request_data.lower():
            SECURITY_EVENTS.labels(type='suspicious_pattern').inc()
            logger.warning(
                f"Suspicious request from {request.remote_addr}: "
                f"Pattern '{pattern}' detected in {request.method} {request.path}"
            )

@app.after_request
def security_headers(response):
    """Add security headers"""
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Instance-ID'] = os.getenv('INSTANCE_ID', 'unknown')
    
    # Remove sensitive headers in production
    if os.getenv('ENVIRONMENT') == 'production':
        response.headers.pop('Server', None)
    
    return response

def validate_email(email):
    """Validate email format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_name(name):
    """Validate name format"""
    if not name or not isinstance(name, str):
        return False
    if len(name) < 2 or len(name) > 100:
        return False
    # Only allow letters, spaces, hyphens, and apostrophes
    pattern = r"^[a-zA-ZÀ-ÿ\s'-]+$"
    return re.match(pattern, name) is not None

def sanitize_input(data):
    """Sanitize user input"""
    if isinstance(data, str):
        # Remove potentially dangerous characters
        dangerous_chars = ['<', '>', '"', "'", ';', '--']
        for char in dangerous_chars:
            data = data.replace(char, '')
    return data

def track_metrics(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = time.time()
        endpoint = request.endpoint or 'unknown'
        method = request.method
        
        try:
            response = f(*args, **kwargs)
            status = response[1] if isinstance(response, tuple) else 200
            
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=status).inc()
            REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(time.time() - start_time)
            
            if status >= 400:
                HTTP_ERRORS.labels(method=method, endpoint=endpoint, status=status).inc()
            
            return response
        except Exception as e:
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=500).inc()
            HTTP_ERRORS.labels(method=method, endpoint=endpoint, status=500).inc()
            REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(time.time() - start_time)
            
            # Log error but don't expose details to client
            logger.error(f"Error in {endpoint}: {str(e)}")
            
            # Generic error message in production
            if os.getenv('ENVIRONMENT') == 'production':
                return jsonify({'error': 'Internal server error'}), 500
            else:
                return jsonify({'error': str(e)}), 500
    
    return decorated_function

def wait_for_db():
    max_retries = 30
    retry_count = 0
    while retry_count < max_retries:
        try:
            db.session.execute(db.text('SELECT 1'))
            logger.info("Database connection established!")
            return True
        except Exception as e:
            retry_count += 1
            logger.info(f"Waiting for database... ({retry_count}/{max_retries})")
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
@limiter.exempt
def health():
    instance_id = os.getenv('INSTANCE_ID', 'unknown')
    return jsonify({
        'status': 'healthy',
        'instance': instance_id,
        'service': 'users'
    })

@app.route('/users', methods=['GET'])
@track_metrics
@limiter.limit("30 per minute")
def get_users():
    cache_key = 'users:all'
    
    if redis_client:
        try:
            cached_data = redis_client.get(cache_key)
            if cached_data:
                CACHE_HITS.labels(endpoint='get_users').inc()
                return jsonify({
                    'data': json.loads(cached_data),
                    'source': 'cache'
                })
        except Exception as e:
            logger.error(f"Redis error: {e}")
    
    CACHE_MISSES.labels(endpoint='get_users').inc()
    
    users = User.query.all()
    users_data = [u.to_dict() for u in users]
    
    if redis_client:
        try:
            redis_client.setex(cache_key, 60, json.dumps(users_data))
        except Exception as e:
            logger.error(f"Failed to cache data: {e}")
    
    update_user_count()
    return jsonify({
        'data': users_data,
        'source': 'database'
    })

@app.route('/users/<int:id>', methods=['GET'])
@track_metrics
@limiter.limit("60 per minute")
def get_user(id):
    if id <= 0:
        return jsonify({'error': 'Invalid user ID'}), 400
    
    user = User.query.get_or_404(id)
    return jsonify(user.to_dict())

@app.route('/users', methods=['POST'])
@track_metrics
@limiter.limit("10 per minute")
def create_user():
    data = request.get_json()
    
    # Validation
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    if 'name' not in data or 'email' not in data:
        return jsonify({'error': 'name and email are required'}), 400
    
    # Validate name
    if not validate_name(data['name']):
        SECURITY_EVENTS.labels(type='invalid_input').inc()
        return jsonify({'error': 'Invalid name format. Use 2-100 characters, letters only'}), 400
    
    # Validate email
    if not validate_email(data['email']):
        SECURITY_EVENTS.labels(type='invalid_email').inc()
        return jsonify({'error': 'Invalid email format'}), 400
    
    # Check if email already exists
    existing_user = User.query.filter_by(email=data['email']).first()
    if existing_user:
        return jsonify({'error': 'Email already exists'}), 409
    
    try:
        user = User(
            name=sanitize_input(data['name']),
            email=data['email'].lower().strip()
        )
        db.session.add(user)
        db.session.commit()
        
        # Invalidate cache
        if redis_client:
            try:
                redis_client.delete('users:all')
            except Exception as e:
                logger.error(f"Failed to invalidate cache: {e}")
        
        update_user_count()
        logger.info(f"User created: {user.email}")
        return jsonify(user.to_dict()), 201
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating user: {e}")
        return jsonify({'error': 'Failed to create user'}), 500

@app.route('/users/<int:id>', methods=['PUT'])
@track_metrics
@limiter.limit("20 per minute")
def update_user(id):
    if id <= 0:
        return jsonify({'error': 'Invalid user ID'}), 400
    
    user = User.query.get_or_404(id)
    data = request.get_json()
    
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Validate if provided
    if 'name' in data:
        if not validate_name(data['name']):
            return jsonify({'error': 'Invalid name format'}), 400
        user.name = sanitize_input(data['name'])
    
    if 'email' in data:
        if not validate_email(data['email']):
            return jsonify({'error': 'Invalid email format'}), 400
        user.email = data['email'].lower().strip()
    
    try:
        db.session.commit()
        
        if redis_client:
            redis_client.delete('users:all')
        
        logger.info(f"User updated: {user.id}")
        return jsonify(user.to_dict())
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating user: {e}")
        return jsonify({'error': 'Failed to update user'}), 500

@app.route('/users/<int:id>', methods=['DELETE'])
@track_metrics
@limiter.limit("10 per minute")
def delete_user(id):
    if id <= 0:
        return jsonify({'error': 'Invalid user ID'}), 400
    
    user = User.query.get_or_404(id)
    
    try:
        db.session.delete(user)
        db.session.commit()
        
        if redis_client:
            redis_client.delete('users:all')
        
        update_user_count()
        logger.info(f"User deleted: {id}")
        return '', 204
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting user: {e}")
        return jsonify({'error': 'Failed to delete user'}), 500

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Resource not found'}), 404

@app.errorhandler(429)
def ratelimit_handler(e):
    SECURITY_EVENTS.labels(type='rate_limit_exceeded').inc()
    return jsonify({'error': 'Rate limit exceeded. Please try again later.'}), 429

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal error: {error}")
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    with app.app_context():
        wait_for_db()
        db.create_all()
        logger.info("Database tables created/verified!")
        update_user_count()
    app.run(host='0.0.0.0', port=5000, debug=(os.getenv('ENVIRONMENT') != 'production'))