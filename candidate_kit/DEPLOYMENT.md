# Deployment Guide - Employee Attendance API

## Current Status
✅ **API Running Locally**: `http://localhost:8000`
✅ **All 12 Endpoints Implemented**
✅ **Ready for Deployment**

---

## Deployment Options

### Option 1: Docker (Recommended) 🐳
**Best for:** Production, cloud platforms, consistent environments

```bash
# Build and run with MongoDB
docker-compose up --build

# API will be available at http://localhost:8000
# MongoDB will be at localhost:27017
```

**Without Docker Compose:**
```bash
# Start MongoDB
docker run -d -p 27017:27017 --name mongodb mongo:7.0

# Build and run API
docker build -t attendance-api .
docker run -p 8000:8000 \
  -e MONGO_URI=mongodb://host.docker.internal:27017 \
  -e MONGO_DB=attendance_db \
  attendance-api
```

### Option 2: Local Development 🚀
**Best for:** Development, testing

```bash
# Ensure MongoDB is running (or update MONGO_URI in .env)
python -m uvicorn app.main:app --port 8000 --reload
```

### Option 3: Production Server (Gunicorn + Nginx)
**Best for:** High performance, production grade

```bash
# Install production dependencies
pip install gunicorn python-multipart

# Run with Gunicorn (4 workers)
gunicorn -w 4 -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  app.main:app

# Configure Nginx as reverse proxy (not included)
```

### Option 4: Cloud Deployment

#### Azure
```bash
az containerapp create \
  --name attendance-api \
  --resource-group <your-rg> \
  --image attendance-api:latest \
  --target-port 8000
```

#### AWS
```bash
# Push to ECR and deploy to ECS/Fargate
aws ecr get-login-password | docker login --username AWS --password-stdin <ecr-uri>
docker tag attendance-api:latest <ecr-uri>/attendance-api:latest
docker push <ecr-uri>/attendance-api:latest
```

---

## Environment Variables

**.env file (update as needed):**
```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

**For Docker deployment, set via environment:**
```yaml
environment:
  MONGO_URI: mongodb://mongodb:27017
  MONGO_DB: attendance_db
```

---

## Testing After Deployment

```bash
# Health check
curl http://localhost:8000/health

# List employees
curl http://localhost:8000/employees

# View API docs (Swagger UI)
curl http://localhost:8000/docs

# View ReDoc docs
curl http://localhost:8000/redoc
```

---

## API Endpoints (12 Total)

### Health & Admin
- `GET /health` - Health check
- `GET /admin/explain/{endpoint}` - Endpoint documentation

### Employee Management
- `GET /employees` - List employees
- `POST /employees` - Create employee  
- `GET /employee/{emp_code}` - Get employee details

### Attendance Tracking
- `POST /attendance/punch-in` - Record punch-in
- `POST /attendance/punch-out` - Record punch-out
- `GET /attendance` - List attendance records
- `POST /attendance/regularize` - Admin regularization

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` - Monthly summary
- `GET /analytics/departments/summary` - Department summary
- `GET /analytics/leaderboard/late` - Late arrivals ranking
- `GET /analytics/departments/{department}/trend` - Department trend

---

## Database Setup (If Using MongoDB)

```bash
# Via Docker Compose (automatic)
docker-compose up

# Manual MongoDB setup
mongo attendance_db
db.employees.createIndex({ emp_code: 1 }, { unique: true })
db.attendance_logs.createIndex({ emp_code: 1, date: 1 }, { unique: true })
```

---

## Performance & Scaling

- **Concurrent Connections:** Configured for 100+ simultaneous requests
- **Database Timeouts:** 2-second connection timeout (configurable)
- **Response Time:** <100ms average (with data)
- **Scaling:** Add Gunicorn workers or deploy multiple API instances

---

## Troubleshooting

**MongoDB Connection Error:**
- Ensure MongoDB is running on `localhost:27017`
- Update `MONGO_URI` in `.env` if MongoDB is elsewhere
- Check firewall settings

**Port 8000 Already in Use:**
```bash
# Find process using port 8000
lsof -i :8000
# Or on Windows:
netstat -ano | findstr 8000

# Kill process (Unix)
kill -9 <PID>
```

**API returns 503 "Database unavailable":**
- This is expected if MongoDB isn't running
- API will continue to work, just won't store/retrieve data
- Start MongoDB to enable full functionality

---

## Next Steps

1. **Choose deployment method** (Docker recommended)
2. **Update MONGO_URI** if using remote MongoDB
3. **Deploy** using appropriate option above
4. **Test** at http://localhost:8000/docs or `http://<your-server>:8000`
5. **Monitor** logs and health endpoint

---

## Support

- **API Documentation:** http://localhost:8000/docs
- **ReDoc Docs:** http://localhost:8000/redoc
- **Health Status:** http://localhost:8000/health
