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
    'products_http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

REQUEST_LATENCY = Histogram(
    'products_http_request_duration_seconds',
    'HTTP request latency',
    ['method', 'endpoint']
)

HTTP_ERRORS = Counter(
    'products_http_errors_total',
    'Total HTTP errors',
    ['method', 'endpoint', 'status']
)

SERVICE_UP = Gauge(
    'products_service_up',
    'Service availability (1 = up, 0 = down)'
)

ACTIVE_PRODUCTS = Gauge(
    'active_products_total',
    'Total number of products in database'
)

# Set service as up
SERVICE_UP.set(1)

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'price': self.price,
            'stock': self.stock,
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

def update_product_count():
    try:
        count = Product.query.count()
        ACTIVE_PRODUCTS.set(count)
    except:
        pass

@app.route('/metrics')
def metrics():
    update_product_count()
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}

@app.route('/health')
@track_metrics
def health():
    return jsonify({'status': 'healthy', 'service': 'products'})

@app.route('/products', methods=['GET'])
@track_metrics
def get_products():
    products = Product.query.all()
    update_product_count()
    return jsonify([p.to_dict() for p in products])

@app.route('/products/<int:id>', methods=['GET'])
@track_metrics
def get_product(id):
    product = Product.query.get_or_404(id)
    return jsonify(product.to_dict())

@app.route('/products', methods=['POST'])
@track_metrics
def create_product():
    data = request.get_json()
    if not data or 'name' not in data or 'price' not in data:
        return jsonify({'error': 'name and price are required'}), 400
    
    try:
        product = Product(
            name=data['name'],
            description=data.get('description', ''),
            price=float(data['price']),
            stock=int(data.get('stock', 0))
        )
        db.session.add(product)
        db.session.commit()
        update_product_count()
        return jsonify(product.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': 'Invalid price or stock value'}), 400

@app.route('/products/<int:id>', methods=['DELETE'])
@track_metrics
def delete_product(id):
    product = Product.query.get_or_404(id)
    db.session.delete(product)
    db.session.commit()
    update_product_count()
    return '', 204

if __name__ == '__main__':
    with app.app_context():
        wait_for_db()
        db.create_all()
        print("Products table created/verified!")
        update_product_count()
    app.run(host='0.0.0.0', port=5002, debug=True)