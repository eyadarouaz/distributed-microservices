#!/bin/bash

echo "=== Testing Users Service API ==="
echo ""

# Wait for service to be ready
echo "Waiting for service to start..."
sleep 5

# Test 1: Health check
echo "1. Health Check"
curl -s http://localhost:5000/health | jq .
echo ""

# Test 2: Create users
echo "2. Creating users..."
curl -s -X POST http://localhost:5000/users \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice Dupont","email":"alice@example.com"}' | jq .
echo ""

curl -s -X POST http://localhost:5000/users \
  -H "Content-Type: application/json" \
  -d '{"name":"Bob Martin","email":"bob@example.com"}' | jq .
echo ""

curl -s -X POST http://localhost:5000/users \
  -H "Content-Type: application/json" \
  -d '{"name":"Charlie Bernard","email":"charlie@example.com"}' | jq .
echo ""

# Test 3: Get all users
echo "3. Getting all users..."
curl -s http://localhost:5000/users | jq .
echo ""

# Test 4: Get single user
echo "4. Getting user with ID 1..."
curl -s http://localhost:5000/users/1 | jq .
echo ""

# Test 5: Update user
echo "5. Updating user with ID 1..."
curl -s -X PUT http://localhost:5000/users/1 \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice Dupont-Updated","email":"alice.updated@example.com"}' | jq .
echo ""

# Test 6: Generate some traffic for metrics
echo "6. Generating traffic for metrics..."
for i in {1..20}; do
  curl -s http://localhost:5000/users > /dev/null
  sleep 0.1
done
echo "Traffic generated!"
echo ""

# Test 7: Test error case
echo "7. Testing error case (user not found)..."
curl -s http://localhost:5000/users/999
echo ""

# Test 8: Check metrics
echo "8. Checking metrics endpoint..."
curl -s http://localhost:5000/metrics | grep -E "(http_requests_total|http_request_duration|service_up|active_users)"
echo ""

echo "=== Testing Complete ==="
echo ""
echo "Access points:"
echo "- Users API: http://localhost:5000"
echo "- Prometheus: http://localhost:9090"
echo "- Grafana: http://localhost:3000 (admin/admin)"
echo ""
echo "In Grafana, go to Dashboards to see 'Users Service Monitoring'"